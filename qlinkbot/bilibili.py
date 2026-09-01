"""Bilibili 解析实现（基于 yt-dlp，子进程方式以便超时终止）。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
from contextlib import suppress
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

YTDLP_TIMEOUT = 300

# 未登录可获取的最高清晰度即 360P
_FORMAT_MERGED = "bv*[height<=360]+ba/b[height<=360]/b"
_FORMAT_PROGRESSIVE = "b[height<=360][ext=mp4]/b[height<=360]/bv*[height<=360]+ba"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


@dataclass
class VideoMeta:
    bvid: str = ""
    title: str = ""
    uploader: str = ""
    views: int = 0
    danmaku: int = 0
    likes: int = 0
    favorites: int = 0
    coins: int = 0
    comments: int = 0
    description: str = ""
    webpage_url: str = ""
    duration: float = 0.0
    estimated_size: int = 0
    cover: str = ""

    def to_cache(self) -> dict:
        # estimated_size 一并缓存：同视频同清晰度大小固定，
        # 缓存命中路径也能参与大小预检；旧缓存缺该字段时取默认值 0
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_cache(cls, data: dict) -> "VideoMeta":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


class ParseError(Exception):
    pass


class ParseTimeout(ParseError):
    pass


VIEW_API = "https://api.bilibili.com/x/web-interface/view"


async def _enrich_stats(session: aiohttp.ClientSession | None,
                        meta: VideoMeta) -> None:
    """通过 B 站公开 view API 补全弹幕/收藏/硬币等统计与封面。

    yt-dlp 不提供这些字段；API 失败时保留 yt-dlp 的值。
    """
    if session is None or not meta.bvid.startswith("BV"):
        return
    try:
        async with session.get(
                VIEW_API,
                params={"bvid": meta.bvid},
                headers={"User-Agent": _USER_AGENT,
                         "Referer": "https://www.bilibili.com/"},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if not data or data.get("code") != 0:
            log.debug("view API 返回异常: %s", data)
            return
        d = data.get("data") or {}
        stat = d.get("stat") or {}
        meta.views = int(stat.get("view") or meta.views)
        meta.danmaku = int(stat.get("danmaku") or 0)
        meta.likes = int(stat.get("like") or meta.likes)
        meta.favorites = int(stat.get("favorite") or 0)
        meta.coins = int(stat.get("coin") or 0)
        meta.comments = int(stat.get("reply") or meta.comments)
        if d.get("pic"):
            meta.cover = str(d["pic"]).replace("http://", "https://")
        if d.get("title"):
            meta.title = str(d["title"])
        if d.get("owner", {}).get("name"):
            meta.uploader = str(d["owner"]["name"])
        if d.get("desc"):
            meta.description = str(d["desc"])[:200]
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        log.debug("view API 调用失败: %s", e)


class TooLargeError(ParseError):
    """文件超过大小限制。"""


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _selected_formats(info: dict) -> list[dict]:
    """按顶层 format_id 取选中的格式条目。

    yt-dlp 对 bv*+ba 合并格式的顶层 format_id 是 "30016+30216" 这样的
    "+" 拼接串，需拆分后逐条匹配（各流的 filesize 来自 playurl API 的
    精确字节数）。
    """
    ids = {p for p in str(info.get("format_id") or "").split("+") if p}
    return [f for f in info.get("formats") or [] if f.get("format_id") in ids]


def _estimate_size(info: dict) -> int:
    duration = info.get("duration") or 0
    total = 0
    for f in _selected_formats(info):
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            total += int(size)
            continue
        tbr = f.get("tbr") or ((f.get("height") or 360) * 8)
        total += int(tbr * 1000 / 8 * duration)
    if total:
        return total
    # 兜底：按 360P 常见码率粗估
    return int(700 * 1000 / 8 * duration) if duration else 0


_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")


def _content_range_total(value: str | None) -> int | None:
    """从 Content-Range 头取文件总大小，如 "bytes 0-0/123456" → 123456。"""
    if not value:
        return None
    m = _CONTENT_RANGE_RE.search(value)
    return int(m.group(1)) if m else None


async def _probe_real_size(session: aiohttp.ClientSession,
                           formats: list[dict]) -> int | None:
    """向 CDN 发 Range: bytes=0-0 请求探测选中各流的实际字节大小并求和。

    仅支持单 URL 直链格式；任一流探测失败即返回 None，由调用方回落
    到 playurl API 的 filesize 估算值。
    """
    urls = []
    for f in formats:
        if f.get("fragments") or not f.get("url"):
            return None
        urls.append(f["url"])
    headers = {"User-Agent": _USER_AGENT,
               "Referer": "https://www.bilibili.com/"}

    async def probe_one(url: str) -> int | None:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 206:
                return _content_range_total(resp.headers.get("Content-Range"))
            if resp.status == 200:  # CDN 不支持 Range 时退回 Content-Length
                length = resp.headers.get("Content-Length")
                return int(length) if length and length.isdigit() else None
            return None

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(probe_one(u) for u in urls),
                           return_exceptions=True),
            timeout=10)
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        log.debug("CDN 大小探测失败，回落估算: %s", e)
        return None
    total = 0
    for r in results:
        if not isinstance(r, int) or r <= 0:
            return None
        total += r
    return total or None


async def _run_ytdlp(args: list[str], timeout: int) -> str:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
           "--no-warnings", "--socket-timeout", "15"] + args
    log.debug("执行: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
            await proc.wait()
        raise ParseTimeout(f"yt-dlp 执行超时({timeout}s)")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", "replace").strip()
        raise ParseError(f"yt-dlp 失败(code={proc.returncode}): {err[-500:]}")
    return stdout.decode("utf-8", "replace")


class BilibiliParser:
    platform = "bilibili"

    def __init__(self, session: aiohttp.ClientSession | None = None,
                 probe_real_size: bool = True) -> None:
        self._session = session
        self._probe_real_size = probe_real_size

    async def probe(self, url: str, timeout: int) -> VideoMeta:
        # 与 download() 使用同一格式选择器，保证预检对象与实际下载一致
        selector = _FORMAT_MERGED if _has_ffmpeg() else _FORMAT_PROGRESSIVE
        out = await _run_ytdlp(
            ["--dump-single-json", "-J",
             "--user-agent", _USER_AGENT, "-f", selector, url],
            timeout,
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise ParseError(f"解析结果 JSON 无效: {e}") from e
        meta = VideoMeta(
            bvid=str(data.get("id", "")),
            title=str(data.get("title", "")),
            uploader=str(data.get("uploader") or data.get("channel") or ""),
            views=int(data.get("view_count") or 0),
            danmaku=int(data.get("danmaku") or 0),
            likes=int(data.get("like_count") or 0),
            favorites=int(data.get("favorite_count") or 0),
            coins=int(data.get("coin_count") or 0),
            comments=int(data.get("comment_count") or 0),
            description=(str(data.get("description") or ""))[:200],
            webpage_url=str(data.get("webpage_url") or url),
            duration=float(data.get("duration") or 0),
            cover=str(data.get("thumbnail") or ""),
        )
        meta.estimated_size = _estimate_size(data)
        if self._probe_real_size and self._session is not None:
            real = await _probe_real_size(self._session,
                                          _selected_formats(data))
            if real:
                meta.estimated_size = real
        if not meta.bvid:
            raise ParseError("未能取得视频 ID（可能不是普通视频内容）")
        await _enrich_stats(self._session, meta)
        return meta

    async def download_cover(self, url: str, output_dir: Path,
                             key: str) -> Path:
        """下载视频封面到 output_dir/<key>.<ext>，返回本地路径。

        B 站图床要求带 Referer；失败抛 ParseError，由调用方决定降级。
        """
        if self._session is None:
            raise ParseError("无可用 HTTP 会话下载封面")
        ext = Path(urlparse(url).path).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        # key 形如 "bvid:bvxx"（可能含冒号），替换为安全的文件名字符
        name = re.sub(r"[^A-Za-z0-9_-]", "_", key) or "cover"
        output_dir.mkdir(parents=True, exist_ok=True)
        dest = output_dir / f"{name}{ext}"
        try:
            async with self._session.get(
                    url,
                    headers={"User-Agent": _USER_AGENT,
                             "Referer": "https://www.bilibili.com/"},
                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    raise ParseError(f"封面下载 HTTP {resp.status}: {url}")
                data = await resp.read()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ParseError(f"封面下载失败: {e}") from e
        if not data:
            raise ParseError("封面内容为空")
        dest.write_bytes(data)
        return dest

    async def download(self, url: str, output_dir: Path,
                       timeout: int) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / "%(id)s.%(ext)s")
        args = [
            "-f", _FORMAT_MERGED if _has_ffmpeg() else _FORMAT_PROGRESSIVE,
            "--merge-output-format", "mp4",
            "--user-agent", _USER_AGENT,
            "-o", output_template,
            "--no-part" ,
            url,
        ]
        await _run_ytdlp(args, timeout)
        # 通过重新探测 id 定位产物文件
        probe_out = await _run_ytdlp(
            ["--dump-single-json", "--get-id", "--skip-download", url], timeout // 2)
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
