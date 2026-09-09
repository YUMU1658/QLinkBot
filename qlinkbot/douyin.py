"""Douyin 视频解析实现（优先无头浏览器直取，yt-dlp 兜底）。

仅支持普通视频（www.douyin.com/video/<数字ID>），图集（note）、
直播、用户/合集页不在本次范围内，由 extract 层直接过滤。

主链路：Playwright 打开视频页 → 拦截 detail 响应（元数据 + 播放直链）
→ aiohttp 直接下载。原因：抖音 detail 接口有 Argus 请求指纹风控，
纯 HTTP（包括带浏览器 cookies 的 yt-dlp）会被 403，只有浏览器自身
请求能通过。yt-dlp 保留为浏览器不可用时的兜底。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

import aiohttp

from .ytdlp_common import (
    ParseError,
    _USER_AGENT,
    _estimate_size,
    _probe_real_size,
    _run_ytdlp,
    _selected_formats,
)

log = logging.getLogger(__name__)

# yt-dlp 报此错即 cookies 缺失/过期（与 412/429/5xx 暂时性错误正交）
_FRESH_COOKIES_MSG = "Fresh cookies"

_MANUAL_COOKIES_HINT = (
    "抖音 cookies 自动刷新后仍被拒绝，可手动处理：浏览器打开 "
    "https://www.douyin.com/ 后导出 Netscape 格式 cookies，"
    "覆盖到抖音 cookies 文件后重试（详见 README 抖音小节）"
)

# 抖音站内 Referer：yt-dlp 请求与 CDN 大小探测共用
_DOUYIN_REFERER = "https://www.douyin.com/"

# 短视频取 720P 上限：兼顾清晰度与 30MB 发送限制；
# probe 与 download 共用同一 selector，保证预检对象与实际下载一致
_DOUYIN_FORMAT = "best[height<=720][ext=mp4]/best[height<=720]/best[ext=mp4]/best"

_TAG_RE = re.compile(r"#(\S+?)(?=\s|#|$)")


@dataclass
class DouyinMeta:
    aweme_id: str = ""
    title: str = ""
    author: str = ""
    plays: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    description: str = ""
    tags: list[str] = field(default_factory=list)
    webpage_url: str = ""
    duration: float = 0.0
    estimated_size: int = 0
    # 浏览器直取的播放直链（download 优先用它直下；为空则走 yt-dlp）
    play_urls: list[str] = field(default_factory=list)

    def to_cache(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_cache(cls, data: dict) -> "DouyinMeta":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def _extract_tags(info: dict, description: str) -> list[str]:
    """从 yt-dlp 的 tags 字段取话题；缺失时从简介里的 #话题 回补。"""
    tags: list[str] = []
    raw = info.get("tags")
    if isinstance(raw, list):
        tags = [str(t).lstrip("#").strip() for t in raw if str(t).strip()]
    if not tags and description:
        tags = [m.group(1) for m in _TAG_RE.finditer(description)]
    # 保序去重，最多保留 10 个
    seen: set[str] = set()
    unique: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)
        if len(unique) >= 10:
            break
    return unique


def _pick_author(info: dict) -> str:
    """作者昵称优先取 creator/channel（中文昵称），回落 uploader（ID 串）。"""
    return str(info.get("creator") or info.get("channel")
               or info.get("uploader") or "")


def _is_cookie_error(err: Exception) -> bool:
    """是否为 cookies 缺失/过期错误（需刷新 cookies 后重试一次）。"""
    return _FRESH_COOKIES_MSG in str(err)


class DouyinParser:
    platform = "douyin"

    def __init__(self, session: aiohttp.ClientSession | None = None,
                 probe_real_size: bool = True,
                 error_retries: int = 3,
                 cookie_store=None,
                 browser_proxy: str = "",
                 browser_enabled: bool = True) -> None:
        self._session = session
        self._probe_real_size = probe_real_size
        self._error_retries = error_retries
        self._cookie_store = cookie_store
        self._browser_proxy = browser_proxy
        self._browser_enabled = browser_enabled

    def _base_args(self) -> list[str]:
        args = ["--user-agent", _USER_AGENT,
                "--referer", _DOUYIN_REFERER]
        if self._cookie_store is not None:
            args += self._cookie_store.cookie_args()
        return args

    async def _run_with_cookie_retry(self, args: list[str], timeout: int,
                                     url: str) -> str:
        """执行 yt-dlp；命中 cookies 失效时刷新一次并仅重试一次。"""
        try:
            return await _run_ytdlp(args, timeout,
                                    retries=self._error_retries)
        except ParseError as e:
            if not _is_cookie_error(e) or self._cookie_store is None:
                raise
            log.warning("抖音 cookies 失效，尝试自动刷新后重试一次")
            refreshed = await self._cookie_store.ensure_fresh(
                video_url=url, force=True)
            if not refreshed:
                raise
            # 刷新后 cookies 文件已更新：去掉旧 --cookies 参数、重拼后再试一次
            retry_args: list[str] = []
            skip_next = False
            for a in args:
                if skip_next:
                    skip_next = False
                    continue
                if a == "--cookies":
                    skip_next = True
                    continue
                retry_args.append(a)
            retry_args += self._cookie_store.cookie_args()
            try:
                return await _run_ytdlp(retry_args, timeout,
                                        retries=self._error_retries)
            except ParseError as e2:
                if _is_cookie_error(e2):
                    log.warning(_MANUAL_COOKIES_HINT)
                raise

    async def _probe_via_browser(self, url: str) -> DouyinMeta | None:
        """浏览器直取元数据；成功返回 meta（含直链），否则返回 None 以便回落。"""
        from .douyin_browser import fetch_video_info
        try:
            info = await fetch_video_info(url, proxy=self._browser_proxy)
        except Exception as e:
            log.debug("浏览器直取异常: %s", e)
            return None
        if info is None:
            return None
        if not info.is_video:
            raise ParseError("不支持的抖音内容（仅支持单个视频）")
        description = info.desc[:200]
        title = description.splitlines()[0] if description else ""
        return DouyinMeta(
            aweme_id=info.aweme_id,
            title=title,
            author=info.author_nickname or info.author_unique_id,
            plays=info.plays,
            likes=info.likes,
            comments=info.comments,
            shares=info.shares,
            description=description,
            tags=_extract_tags({}, description),
            webpage_url=url,
            duration=info.duration_ms / 1000.0 if info.duration_ms else 0.0,
            estimated_size=info.data_size,
            play_urls=info.play_urls,
        )

    async def _download_direct(self, meta: DouyinMeta, url: str,
                               output_dir: Path,
                               timeout: int) -> Path | None:
        """用浏览器直取的播放直链直接下载；失败返回 None 以便走 yt-dlp。"""
        if self._session is None or not meta.play_urls:
            return None
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"{meta.aweme_id or 'douyin'}.mp4"
        last_err: Exception | None = None
        for play_url in meta.play_urls[:3]:
            try:
                async with self._session.get(
                        play_url,
                        headers={"User-Agent": _USER_AGENT,
                                 "Referer": _DOUYIN_REFERER},
                        timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    if resp.status != 200:
                        last_err = ParseError(
                            f"直链下载 HTTP {resp.status}")
                        continue
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1 << 20):
                            f.write(chunk)
                if dest.is_file() and dest.stat().st_size > 0:
                    return dest
            except (aiohttp.ClientError, TimeoutError, OSError) as e:
                last_err = e
                continue
        log.debug("直链下载失败（回落 yt-dlp）: %s", last_err)
        return None

    async def probe(self, url: str, timeout: int) -> DouyinMeta:
        # 主链路：浏览器直取；不可用/失败时回落 yt-dlp
        if self._browser_enabled:
            meta = await self._probe_via_browser(url)
            if meta is not None:
                return meta
            log.info("浏览器直取失败，回落 yt-dlp: %s", url)
        out = await self._run_with_cookie_retry(
            ["--dump-single-json", "-J",
             *self._base_args(), "-f", _DOUYIN_FORMAT, url],
            timeout, url,
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise ParseError(f"解析结果 JSON 无效: {e}") from e
        if data.get("_type") == "playlist":
            raise ParseError("不支持的抖音内容（仅支持单个视频）")
        description = str(data.get("description") or "")[:200]
        meta = DouyinMeta(
            aweme_id=str(data.get("id", "")),
            title=str(data.get("title") or description.split("\n")[0] or ""),
            author=_pick_author(data),
            plays=int(data.get("view_count") or 0),
            likes=int(data.get("like_count") or 0),
            comments=int(data.get("comment_count") or 0),
            shares=int(data.get("repost_count")
                       or data.get("share_count") or 0),
            description=description,
            tags=_extract_tags(data, description),
            webpage_url=str(data.get("webpage_url") or url),
            duration=float(data.get("duration") or 0),
        )
        meta.estimated_size = _estimate_size(data)
        if self._probe_real_size and self._session is not None:
            real = await _probe_real_size(self._session,
                                          _selected_formats(data),
                                          referer=_DOUYIN_REFERER)
            if real:
                meta.estimated_size = real
        if not meta.aweme_id:
            raise ParseError("未能取得视频 ID（可能不是普通视频内容）")
        return meta

    async def download(self, url: str, output_dir: Path,
                       timeout: int, meta: DouyinMeta | None = None) -> Path:
        # 浏览器直取 probe 到的 meta 自带播放直链，优先直接下载
        if meta is not None and meta.play_urls:
            direct = await self._download_direct(meta, url, output_dir,
                                                 timeout)
            if direct is not None:
                return direct
            log.info("直链下载失败，回落 yt-dlp: %s", url)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / "%(id)s.%(ext)s")
        args = [
            "-f", _DOUYIN_FORMAT,
            "--merge-output-format", "mp4",
            *self._base_args(),
            "-o", output_template,
            "--no-part",
            url,
        ]
        await self._run_with_cookie_retry(args, timeout, url)
        # 通过重新探测 id 定位产物文件
        probe_out = await self._run_with_cookie_retry(
            ["--dump-single-json", "--get-id", "--skip-download",
             *self._base_args(), url],
            timeout // 2, url)
        vid = probe_out.strip().splitlines()[0].strip()
        for ext in ("mp4", "mkv", "webm", "flv"):
            candidate = output_dir / f"{vid}.{ext}"
            if candidate.is_file():
                return candidate
        # 兜底：目录中最新的视频文件
        files = sorted(
            (p for p in output_dir.iterdir()
             if p.is_file() and p.suffix.lower()
             in (".mp4", ".mkv", ".webm", ".flv")),
            key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise ParseError("下载完成但未找到输出文件")
        return files[0]
