"""yt-dlp 子进程调用与 CDN 大小探测的通用逻辑（各平台解析器共用）。

从 bilibili.py 抽取而来，行为保持不变；_probe_real_size 允许调用方
传入自家 Referer（防盗链站点通常要求正确的 Referer 才会返回内容）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import shutil
import sys
from contextlib import suppress

import aiohttp

log = logging.getLogger(__name__)

YTDLP_TIMEOUT = 300

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class ParseError(Exception):
    pass


class ParseTimeout(ParseError):
    pass


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
                           formats: list[dict],
                           referer: str = "https://www.bilibili.com/") -> int | None:
    """向 CDN 发 Range: bytes=0-0 请求探测选中各流的实际字节大小并求和。

    仅支持单 URL 直链格式；任一流探测失败即返回 None，由调用方回落
    到 playurl API 的 filesize 估算值。
    """
    urls = []
    for f in formats:
        if f.get("fragments") or not f.get("url"):
            return None
        urls.append(f["url"])
    headers = {"User-Agent": _USER_AGENT, "Referer": referer}

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


# 风控/服务端类暂时性错误（如 412 Precondition Failed），重试通常可恢复；
# yt-dlp 对网页请求的 412 不做任何原生重试，只能在本层处理
_TRANSIENT_HTTP_RE = re.compile(r"HTTP Error (?:412|429|5\d\d)")


def _is_transient_error(stderr: str) -> bool:
    return bool(_TRANSIENT_HTTP_RE.search(stderr))


async def _run_ytdlp(args: list[str], timeout: int, retries: int = 0) -> str:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist",
           "--no-warnings", "--socket-timeout", "15"] + args
    log.debug("执行: %s", " ".join(cmd))
    for attempt in range(max(retries, 0) + 1):
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
        if proc.returncode == 0:
            return stdout.decode("utf-8", "replace")
        err = stderr.decode("utf-8", "replace").strip()
        if attempt < retries and _is_transient_error(err):
            delay = min(2 ** attempt, 8) + random.uniform(0, 1)
            log.warning("yt-dlp 暂时性失败(第 %d 次)，%.1fs 后重试: %s",
                        attempt + 1, delay, err[-300:])
            await asyncio.sleep(delay)
            continue
        break
    raise ParseError(f"yt-dlp 失败(code={proc.returncode}): {err[-500:]}")
