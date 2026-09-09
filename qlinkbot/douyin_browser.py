"""经无头浏览器直取抖音视频（元数据 + 播放直链）。

背景：抖音 detail 接口被 Argus 风控拦截（"Blocked by ArgusSecurityPlugin
Uifid Not Found"）：UIFID 等指纹需由页面 JS 计算，纯 HTTP 拿不到；且
即使带上浏览器 cookies，用 yt-dlp（Python 请求栈）调依然被拦——Argus
还校验请求指纹。浏览器自己发的请求指纹正确，detail 返回 200。

本模块：Playwright 打开视频页 → 拦截 `aweme/v1/web/aweme/detail`
响应 JSON（元数据 + play_addr 直链 + 封面）→ 返回结构化结果。
调用方下载视频时直接用 aiohttp 下载直链（直链本身无额外风控）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_DOUYIN_HOME = "https://www.douyin.com/"
_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# 服务器/容器环境运行所需
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
]


@dataclass
class BrowserVideoInfo:
    aweme_id: str = ""
    desc: str = ""
    author_nickname: str = ""
    author_unique_id: str = ""
    plays: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    collects: int = 0
    duration_ms: int = 0
    cover_urls: list[str] = field(default_factory=list)
    play_urls: list[str] = field(default_factory=list)
    data_size: int = 0
    is_video: bool = True


def _parse_detail(detail: dict) -> BrowserVideoInfo | None:
    """从 aweme_detail JSON 解析视频信息；图集/非视频返回 is_video=False。"""
    if not isinstance(detail, dict):
        return None
    # 图集内容：image_post_info 存在且 video 无 play_addr
    video = detail.get("video") or {}
    play = video.get("play_addr") or {}
    play_urls = [u for u in (play.get("url_list") or []) if u]
    cover = video.get("cover") or {}
    cover_urls = [u for u in (cover.get("url_list") or []) if u]
    if not cover_urls:
        for key in ("origin_cover", "dynamic_cover"):
            c = video.get(key) or {}
            cover_urls = [u for u in (c.get("url_list") or []) if u]
            if cover_urls:
                break
    author = detail.get("author") or {}
    stats = detail.get("statistics") or {}
    info = BrowserVideoInfo(
        aweme_id=str(detail.get("aweme_id") or ""),
        desc=str(detail.get("desc") or ""),
        author_nickname=str(author.get("nickname") or ""),
        author_unique_id=str(author.get("unique_id") or ""),
        plays=int(stats.get("play_count") or 0),
        likes=int(stats.get("digg_count") or 0),
        comments=int(stats.get("comment_count") or 0),
        shares=int(stats.get("share_count") or 0),
        collects=int(stats.get("collect_count") or 0),
        duration_ms=int(video.get("duration") or 0),
        cover_urls=cover_urls,
        play_urls=play_urls,
        data_size=int(play.get("data_size") or 0),
        is_video=bool(play_urls),
    )
    if not info.aweme_id:
        return None
    return info


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


async def _launch_browser(pw, proxy: str = ""):
    kwargs: dict = {"headless": True, "args": _LAUNCH_ARGS}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return await pw.chromium.launch(**kwargs)


async def fetch_video_info(video_url: str,
                           timeout_ms: int = 60000,
                           proxy: str = "") -> BrowserVideoInfo | None:
    """打开视频页，拦截 detail 响应并解析；失败/图集无直链返回 None 或 is_video=False。"""
    if not _playwright_available():
        log.debug("playwright 未安装，跳过浏览器直取")
        return None
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        log.debug("playwright 导入失败: %s", e)
        return None

    payloads: list[dict] = []
    try:
        async with async_playwright() as pw:
            browser = await _launch_browser(pw, proxy)
            try:
                context = await browser.new_context(
                    user_agent=_CHROME_UA,
                    locale="zh-CN",
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()

                async def on_response(resp) -> None:
                    if ("aweme/v1/web/aweme/detail" in resp.url
                            and resp.status == 200):
                        try:
                            payloads.append(await resp.json())
                        except Exception:
                            pass

                page.on("response", on_response)
                await page.goto(video_url, wait_until="domcontentloaded",
                                timeout=timeout_ms)
                # 等待 detail 请求完成（最多等 15s）
                for _ in range(30):
                    if payloads:
                        break
                    await asyncio.sleep(0.5)
                if not payloads:
                    log.warning("浏览器未拦截到 detail 响应: %s", video_url)
                    return None
            finally:
                await browser.close()
    except Exception as e:
        log.warning("浏览器直取抖音视频失败: %s", e)
        return None

    for payload in payloads:
        detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
        info = _parse_detail(detail or {})
        if info is not None:
            log.info("浏览器直取成功 aweme=%s 直链=%d 封面=%d",
                     info.aweme_id, len(info.play_urls),
                     len(info.cover_urls))
            return info
    log.warning("detail 响应中无有效 aweme_detail")
    return None


async def fetch_cookies_via_browser(
        video_url: str = "",
        timeout_ms: int = 45000,
        proxy: str = "") -> dict[str, str] | None:
    """兼容旧接口：用浏览器访问并导出 douyin 域 cookies（供 yt-dlp 兜底用）。"""
    if not _playwright_available():
        return None
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return None
    try:
        async with async_playwright() as pw:
            browser = await _launch_browser(pw, proxy)
            try:
                context = await browser.new_context(
                    user_agent=_CHROME_UA,
                    locale="zh-CN",
                    viewport={"width": 1366, "height": 768},
                )
                page = await context.new_page()
                await page.goto(_DOUYIN_HOME, wait_until="domcontentloaded",
                                timeout=timeout_ms)
                await page.wait_for_timeout(5000)
                if video_url:
                    try:
                        await page.goto(video_url,
                                        wait_until="domcontentloaded",
                                        timeout=timeout_ms)
                        await page.wait_for_timeout(3000)
                    except Exception as e:
                        log.debug("浏览器打开视频页失败（继续用首页cookies）: %s",
                                  e)
                raw = await context.cookies()
                cookies = {c["name"]: c["value"] for c in raw
                           if c.get("name") and c.get("value") is not None
                           and "douyin.com" in (c.get("domain") or "")}
                log.info("浏览器获取到抖音 cookies（%d 个）: %s",
                         len(cookies), sorted(cookies))
                return cookies or None
            finally:
                await browser.close()
    except Exception as e:
        log.warning("浏览器获取抖音 cookies 失败: %s", e)
        return None
