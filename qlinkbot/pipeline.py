"""核心解析流水线。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from .api import QQApi, QQApiError
from .bilibili import BilibiliParser, ParseError, ParseTimeout, TooLargeError
from .caches import DuplicateLimiter, FileCache, MetadataCache, MessageDedup, RateLimiter
from .config import Config
from .events import InboundMessage
from .extract import extract_target, normalize_target
from .sender import Sender, build_markdown

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg: Config, api: QQApi,
                 downloads_dir: Path) -> None:
        self._cfg = cfg
        self._api = api
        self._sender = Sender(api)
        self._parser = BilibiliParser(api.session)
        self._downloads_dir = downloads_dir
        self.file_cache = FileCache(cfg.cache.file_ttl_seconds)
        self.meta_cache = MetadataCache(cfg.cache.metadata_ttl_seconds)
        self.duplicate = DuplicateLimiter(cfg.cache.duplicate_window_seconds)
        self.rate_limiter = RateLimiter(cfg.limits.rate_limit_count,
                                        cfg.limits.rate_limit_window_seconds)
        self.msg_dedup = MessageDedup()
        # 限制并发解析任务，防止突发消息拖垮机器
        self._parse_sem = asyncio.Semaphore(3)
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def handle_message(self, msg: InboundMessage) -> None:
        if msg.message_id and self.msg_dedup.seen(msg.message_id):
            return

        content = msg.content or ""
        raw_target = extract_target(content)
        if raw_target is None:
            return
        if not self._cfg.platforms.bilibili.enabled:
            return

        target = await normalize_target(self._session, raw_target)
        if target is None:
            return

        video_key = target.cache_key

        # 会话内重复视频限速（不占用解析次数）
        if self.duplicate.check_and_mark(msg.session_key, video_key,
                                         mark=False):
            log.info("[%s] 重复视频 %s，跳过", msg.session_key, video_key)
            await self._maybe_report(msg, "duplicate")
            return

        # 已下载文件缓存命中：直接重发，不再解析、不计次
        cached_file = self.file_cache.get(video_key)
        if cached_file is not None:
            log.info("[%s] 命中文件缓存 %s", msg.session_key, video_key)
            meta = self._load_meta(video_key)
            try:
                await self._send_result(msg, meta, cached_file)
            except QQApiError as e:
                log.warning("缓存文件重发失败: %s", e)
                await self._maybe_report(msg, "failed")
            return

        # 全局限流（预占额度，失败回退）
        if not self.rate_limiter.try_acquire():
            log.info("全局解析限流触发，静默丢弃")
            return

        acquired = False
        async with self._parse_sem:
            try:
                acquired = True
                await self._process(msg, target, video_key)
            except (ParseTimeout,) as e:
                log.warning("解析超时 %s: %s", video_key, e)
                self.rate_limiter.release()
                await self._maybe_report(msg, "timeout")
            except TooLargeError as e:
                log.info("文件过大放弃发送 %s: %s", video_key, e)
                self.rate_limiter.release()
                await self._maybe_report(msg, "too_large")
            except (ParseError, QQApiError, OSError) as e:
                log.warning("解析/发送失败 %s: %s", video_key, e)
                self.rate_limiter.release()
                await self._maybe_report(msg, "failed")
            except Exception as e:
                log.exception("未预期的处理错误 %s: %s", video_key, e)
                self.rate_limiter.release()
                await self._maybe_report(msg, "failed")

    # ---- 内部流程 ----

    async def _process(self, msg: InboundMessage, target,
                       video_key: str) -> None:
        timeout_budget = self._cfg.limits.parse_timeout_seconds

        meta = self._load_meta(video_key)
        if meta is None:
            meta = await self._parser.probe(target.resolve_url, timeout_budget)
            self.meta_cache.put(video_key, meta.to_cache())

        # 大小预检
        if meta.estimated_size > self._cfg.max_file_size_bytes:
            raise TooLargeError(
                f"预计 {meta.estimated_size / 1024 / 1024:.1f}MB 超过限制")

        file_path = await self._parser.download(
            target.resolve_url, self._downloads_dir, timeout_budget)

        actual_size = file_path.stat().st_size
        if actual_size > self._cfg.max_file_size_bytes:
            file_path.unlink(missing_ok=True)
            raise TooLargeError(
                f"实际 {actual_size / 1024 / 1024:.1f}MB 超过限制")

        await self._send_result(msg, meta, file_path)

        # 全部成功：记录各类缓存与计数
        self.file_cache.put(video_key, file_path)
        self.duplicate.check_and_mark(msg.session_key, video_key, mark=True)
        log.info("[%s] 解析完成并已发送 %s (%.1fMB)",
                 msg.session_key, video_key, actual_size / 1024 / 1024)

    def _load_meta(self, video_key: str):
        data = self.meta_cache.get(video_key)
        if data is None:
            return None
        from .bilibili import VideoMeta
        return VideoMeta.from_cache(data)

    async def _send_result(self, msg: InboundMessage, meta, file_path: Path) -> None:
        card = build_markdown(meta, meta.cover)
        await self._sender.reply_markdown(msg, card, seq=1)
        await self._sender.send_video(msg, file_path, seq=2)

    async def _maybe_report(self, msg: InboundMessage,
                            kind: str) -> None:
        """仅在配置开启时回复错误提示；被动窗口可能已过期，失败则忽略。"""
        if not self._cfg.behavior.report_errors:
            return
        text = {
            "duplicate": "该视频近期已分享过，暂不重复解析",
            "timeout": "视频解析超时，已放弃本次解析",
            "too_large": "视频文件超过大小限制，无法发送",
            "failed": "视频解析失败，请稍后重试",
        }.get(kind)
        if not text:
            return
        try:
            await self._sender.reply_text(msg, text, seq=1)
        except Exception as e:
            log.debug("错误提示发送失败（可能超出被动窗口）: %s", e)
