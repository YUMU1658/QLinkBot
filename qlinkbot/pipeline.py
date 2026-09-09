"""核心解析流水线。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from .admin import AdminService
from .api import QQApi, QQApiError
from .bilibili import BilibiliParser
from .caches import DuplicateLimiter, FileCache, MetadataCache, MessageDedup, RateLimiter
from .config import Config
from .douyin import DouyinParser
from .douyin_cookies import DouyinCookieStore
from .events import InboundMessage
from .extract import Target, extract_target, normalize_target
from .sender import Sender, build_douyin_text_reply, build_text_reply
from .sessionconfig import (
    PLATFORM_BILIBILI,
    PLATFORM_DOUYIN,
    SessionConfigStore,
)
from .ytdlp_common import ParseError, ParseTimeout, TooLargeError

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, cfg: Config, api: QQApi,
                 downloads_dir: Path) -> None:
        self._cfg = cfg
        self._api = api
        self._sender = Sender(api)
        self._bili_parser = BilibiliParser(
            api.session, probe_real_size=cfg.limits.probe_real_size,
            error_retries=cfg.limits.error_retry_count)
        self._douyin_cookies = DouyinCookieStore(
            api.session, cfg.platforms.douyin.cookies_file,
            browser_proxy=cfg.platforms.douyin.browser_proxy)
        self._douyin_parser = DouyinParser(
            api.session, probe_real_size=cfg.limits.probe_real_size,
            error_retries=cfg.limits.error_retry_count,
            cookie_store=self._douyin_cookies,
            browser_proxy=cfg.platforms.douyin.browser_proxy,
            browser_enabled=cfg.platforms.douyin.browser_enabled)
        # 旧属性名保留为 bilibili 解析器的别名（兼容外部引用）
        self._parser = self._bili_parser
        self._downloads_dir = downloads_dir
        self.file_cache = FileCache(cfg.cache.file_ttl_seconds)
        # 封面文件缓存与视频文件缓存共用同一 TTL 配置（cache.file_ttl_seconds）
        self.cover_cache = FileCache(cfg.cache.file_ttl_seconds)
        self._covers_dir = downloads_dir / "covers"
        self.meta_cache = MetadataCache(cfg.cache.metadata_ttl_seconds)
        self.duplicate = DuplicateLimiter(cfg.cache.duplicate_window_seconds)
        self.rate_limiter = RateLimiter(cfg.limits.rate_limit_count,
                                        cfg.limits.rate_limit_window_seconds)
        self.msg_dedup = MessageDedup()
        # 会话级解析开关（/管理 指令读写）与对应管理服务
        self.session_options = SessionConfigStore(cfg.admin.session_store_path)
        self.admin = AdminService(cfg, api, self._sender, self.session_options)
        # 限制并发解析任务，防止突发消息拖垮机器
        self._parse_sem = asyncio.Semaphore(3)
        self._session: aiohttp.ClientSession | None = None
        self._janitor: asyncio.Task | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()
        self._janitor = asyncio.create_task(self._cleanup_loop())

    async def close(self) -> None:
        if self._janitor:
            self._janitor.cancel()
            try:
                await self._janitor
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    # ---- 缓存定时清理 ----

    async def _cleanup_loop(self) -> None:
        interval = self._cfg.cache.cleanup_interval_seconds
        if interval <= 0:
            log.info("缓存定时清理已禁用 (cleanup_interval_seconds=%d)", interval)
            return
        log.info("缓存定时清理已启动，间隔 %d 秒", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                self._cleanup_once()
            except Exception:
                log.exception("缓存清理任务异常")

    def _cleanup_once(self) -> None:
        removed = 0
        for path in self.file_cache.purge_expired():
            if self._unlink(path):
                removed += 1
        for path in self.cover_cache.purge_expired():
            if self._unlink(path):
                removed += 1
        removed += self._sweep_orphans()
        self.meta_cache.purge_expired()
        self.duplicate.purge_expired()
        if removed:
            log.info("缓存清理：删除 %d 个过期文件", removed)
        else:
            log.debug("缓存清理：无过期文件")

    def _unlink(self, path: Path) -> bool:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as e:
            log.warning("缓存文件删除失败 %s: %s", path, e)
            return False

    def _sweep_orphans(self) -> int:
        """删除缓存目录中未被索引跟踪且已老化的文件（崩溃残留、中间流文件等）。

        阈值取 file_ttl + parse_timeout：下载中的文件 mtime 持续更新且受
        parse_timeout 约束，仍在缓存索引中的活跃文件一律跳过，均不会被误删。
        """
        max_age = (self._cfg.cache.file_ttl_seconds
                   + self._cfg.limits.parse_timeout_seconds)
        now = time.time()
        live = set(self.file_cache.live_paths()) | set(self.cover_cache.live_paths())
        removed = 0
        for d in (self._downloads_dir, self._covers_dir):
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or f in live:
                    continue
                try:
                    if now - f.stat().st_mtime <= max_age:
                        continue
                    f.unlink()
                except OSError as e:
                    log.warning("缓存文件删除失败 %s: %s", f, e)
                    continue
                removed += 1
        return removed

    async def handle_message(self, msg: InboundMessage) -> None:
        if msg.message_id and self.msg_dedup.seen(msg.message_id):
            return

        # 学习群成员角色（供管理按钮回调的纵深校验）
        self.admin.observe(msg)

        content = msg.content or ""
        # /管理 指令优先于链接解析分发
        if self._is_admin_command(content):
            await self.admin.handle_command(msg)
            return

        # 会话内对应平台解析被 /管理 关闭时完全禁用（指令本身不受影响）
        raw_target = extract_target(content)
        if raw_target is None:
            return
        opts = self.session_options.get(msg.session_key,
                                        raw_target.platform)
        if not opts.enabled:
            return
        if raw_target.platform == PLATFORM_BILIBILI:
            if not self._cfg.platforms.bilibili.enabled:
                return
        elif raw_target.platform == PLATFORM_DOUYIN:
            if not self._cfg.platforms.douyin.enabled:
                return
        else:
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
        meta = self._load_meta(target.platform, video_key)
        if cached_file is not None and meta is not None:
            log.info("[%s] 命中文件缓存 %s", msg.session_key, video_key)
            try:
                await self._send_result(msg, target.platform, meta,
                                        cached_file, video_key, opts)
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
                await self._process(msg, target, video_key, opts)
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

    def _is_admin_command(self, content: str) -> bool:
        """匹配管理指令；content 已在 events 层剥离 @机器人 前缀。"""
        return (content or "").strip() == self._cfg.admin.command

    async def _process(self, msg: InboundMessage, target: Target,
                       video_key: str, opts) -> None:
        timeout_budget = self._cfg.limits.parse_timeout_seconds
        parser = self._parser_for(target.platform)

        meta = self._load_meta(target.platform, video_key)
        if meta is None:
            meta = await parser.probe(target.resolve_url, timeout_budget)
            self.meta_cache.put(video_key, meta.to_cache())

        # 大小预检
        if meta.estimated_size > self._cfg.max_file_size_bytes:
            raise TooLargeError(
                f"预计 {meta.estimated_size / 1024 / 1024:.1f}MB 超过限制")

        file_path = await parser.download(
            target.resolve_url, self._downloads_dir, timeout_budget,
            **({"meta": meta} if target.platform == PLATFORM_DOUYIN else {}))

        actual_size = file_path.stat().st_size
        if actual_size > self._cfg.max_file_size_bytes:
            file_path.unlink(missing_ok=True)
            raise TooLargeError(
                f"实际 {actual_size / 1024 / 1024:.1f}MB 超过限制")

        await self._send_result(msg, target.platform, meta, file_path,
                                video_key, opts)

        # 全部成功：记录各类缓存与计数
        self.file_cache.put(video_key, file_path)
        self.duplicate.check_and_mark(msg.session_key, video_key, mark=True)
        log.info("[%s] 解析完成并已发送 %s (%.1fMB)",
                 msg.session_key, video_key, actual_size / 1024 / 1024)

    def _parser_for(self, platform: str):
        if platform == PLATFORM_DOUYIN:
            return self._douyin_parser
        return self._bili_parser

    def _load_meta(self, platform: str, video_key: str):
        data = self.meta_cache.get(video_key)
        if data is None:
            return None
        if platform == PLATFORM_DOUYIN:
            from .douyin import DouyinMeta
            return DouyinMeta.from_cache(data)
        from .bilibili import VideoMeta
        return VideoMeta.from_cache(data)

    async def _ensure_cover(self, platform: str, meta,
                            video_key: str) -> Path | None:
        """取封面本地文件；未缓存则下载。失败仅告警，不阻断主流程。"""
        cached = self.cover_cache.get(video_key)
        if cached is not None:
            return cached
        if not meta.cover:
            return None
        try:
            path = await self._parser_for(platform).download_cover(
                meta.cover, self._covers_dir, video_key)
        except Exception as e:
            log.warning("封面获取失败 %s: %s", video_key, e)
            return None
        self.cover_cache.put(video_key, path)
        return path

    @staticmethod
    def _at_prefix(msg: InboundMessage) -> str:
        """@ 机器人消息（对应平台"仅@/@最近N条"范围）回复需带上 @用户；
        全量群消息与私聊不加。"""
        if msg.event_type == "GROUP_AT_MESSAGE_CREATE":
            return f'<qqbot-at-user id="{msg.user_openid}" />\n'
        return ""

    async def _send_result(self, msg: InboundMessage, platform: str, meta,
                           file_path: Path, video_key: str,
                           opts) -> None:
        if platform == PLATFORM_DOUYIN:
            # 抖音会话配置暂仅总开关：封面与视频默认发送
            text = build_douyin_text_reply(meta)
            send_cover = True
            send_video = True
        else:
            text = build_text_reply(meta, opts)
            send_cover = bool(getattr(opts, "cover", True))
            send_video = bool(getattr(opts, "video", True))
        prefix = self._at_prefix(msg)
        if prefix:
            text = prefix + text

        seq_box = [0]

        def next_seq() -> int:
            seq_box[0] += 1
            return seq_box[0]

        # 封面被会话配置关闭时不下载、不发送
        cover = await self._ensure_cover(platform, meta, video_key) \
            if send_cover else None

        combined_done = False
        if cover is not None and text and self._cfg.behavior.media_with_text:
            # 优先图文同条发送；平台拒绝时自动拆为两条
            try:
                await self._sender.reply_cover_with_text(
                    msg, cover, text, next_seq())
                combined_done = True
            except QQApiError as e:
                log.warning("图文同条发送失败，降级为分开两条: %s", e)
        if not combined_done and text:
            await self._sender.reply_text(msg, text, next_seq())
        if not combined_done and cover is not None:
            try:
                await self._sender.send_cover(msg, cover, next_seq())
            except QQApiError as e:
                log.warning("封面发送失败（继续发送视频）: %s", e)

        if send_video:
            await self._sender.send_video(msg, file_path, next_seq())
        else:
            log.info("[%s] 会话配置关闭视频发送，跳过 %s",
                     msg.session_key, video_key)

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
            # 独立高位 seq，避免与正常链路已占用的低序号重复组合被平台去重拒发
            await self._sender.reply_text(msg, text, seq=9)
        except Exception as e:
            log.debug("错误提示发送失败（可能超出被动窗口）: %s", e)
