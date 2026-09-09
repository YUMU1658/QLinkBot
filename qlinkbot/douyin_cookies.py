"""抖音匿名 cookies 自动续期（免登录）。

背景：抖音 aweme/detail 接口有匿名风控，要求请求携带新鲜网页
cookies（ttwid / __ac_nonce / s_v_web_id 等）；yt-dlp 自身不做播种，
完全依赖外部经 --cookies 投喂（Netscape 格式文件）。

本模块维护 data/douyin_cookies.txt：yt-dlp 每次调用都带上它；
命中 "Fresh cookies" 报错时刷一次并仅重试一次。刷新为 best-effort：
部分 cookie（如 s_v_web_id）可能是页面 JS 挑战生成的，纯服务端
Set-Cookie 拿不到是预期内的，成败由重试那次 yt-dlp 调用的结果判定。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import aiohttp

from .ytdlp_common import _USER_AGENT

log = logging.getLogger(__name__)

_DOUYIN_HOME = "https://www.douyin.com/"
_DOUYIN_REFERER = "https://www.douyin.com/"
# 仿 yt-dlp 头头条逻辑的 ttwid 注册端点；参数未经线上验证，失败即跳过
_TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_PAYLOAD = {
    "aid": 1128,
    "needFid": False,
    "region": "cn",
    "service": "www.douyin.com",
    "union": True,
}

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

_MANUAL_HINT = (
    "抖音 cookies 自动刷新未生效，可手动处理：浏览器打开 "
    "https://www.douyin.com/ 后用插件导出 Netscape 格式 cookies，"
    "覆盖到 %s 后重试"
)


def _netscape_header() -> str:
    return "# Netscape HTTP Cookie File\n"


def cookies_to_netscape(cookies: dict[str, str],
                        domain: str = ".douyin.com") -> str:
    """cookies 字典转 Netscape 格式文本（yt-dlp --cookies 可读）。"""
    lines = [_netscape_header()]
    for name, value in sorted(cookies.items()):
        # domain / TRUE / path / secure / expiry / name / value
        lines.append(f"{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def _morsel_domain(cookie) -> str:
    """取 Morsel 的 domain；host-only cookie（无 domain）返回空串。"""
    try:
        domain = cookie["domain"] if "domain" in cookie else ""
    except (TypeError, KeyError):
        domain = getattr(cookie, "domain", "") or ""
    return str(domain or "")


def jar_to_dict(jar, suffix: str = "douyin.com") -> dict[str, str]:
    """从 aiohttp CookieJar 按域名后缀导出 cookies 字典。

    aiohttp jar 迭代出的是 http.cookies.Morsel（key/value + dict 风格
    ["domain"]），不是 http.cookiejar.Cookie；此处用鸭子类型兼容两者。
    host-only cookie（无 domain，由响应 host 隐式归属）也一并收录。
    """
    out: dict[str, str] = {}
    for cookie in jar:
        key = getattr(cookie, "key", None) or getattr(cookie, "name", None)
        if not key:
            continue
        domain = _morsel_domain(cookie)
        if domain and suffix not in domain:
            continue
        value = getattr(cookie, "value", "")
        out[str(key)] = str(value or "")
    return out


def _copy_ttwid_to_douyin(session, resp) -> bool:
    """从 ttwid 注册响应取 ttwid 复制到 douyin 域，成功返回 True。

    注册端点下发的域是 bytedance.com，aiohttp 会按域隔离存放，
    请求 douyin.com 时不会带上；复制后 detail 接口才会携带。
    """
    try:
        morsel = resp.cookies.get("ttwid") if resp.cookies else None
        value = getattr(morsel, "value", "") or ""
    except Exception:
        value = ""
    if not value:
        return False
    try:
        from yarl import URL
        session.cookie_jar.update_cookies(
            {"ttwid": value}, response_url=URL(_DOUYIN_HOME))
    except Exception as e:
        log.debug("ttwid 复制到 douyin 域失败: %s", e)
        return False
    return True


class DouyinCookieStore:
    """抖音 cookies 文件的持有与按需刷新。"""
    def __init__(self, session: aiohttp.ClientSession | None,
                 cookies_file: str | Path = "",
                 browser_proxy: str = "") -> None:
        self._session = session
        self._path = Path(cookies_file) if cookies_file else None
        self._browser_proxy = browser_proxy or ""
        self._lock = asyncio.Lock()
        self._last_refresh: float = 0.0

    @property
    def path(self) -> Path | None:
        return self._path

    def cookie_args(self) -> list[str]:
        """yt-dlp 命令行追加参数；文件不存在时返回空（不断链）。"""
        if self._path is not None and self._path.is_file():
            return ["--cookies", str(self._path)]
        return []

    async def ensure_fresh(self, video_url: str = "",
                           force: bool = False,
                           min_interval_seconds: float = 300) -> bool:
        """确保 cookies 文件新鲜；返回是否可用于重试。

        force=True 无视冷却直接刷新；否则距上次刷新不足冷却时间时，
        仅当文件缺失才刷新。并发刷新由锁合并为单次。
        """
        if self._path is None or self._session is None:
            return False
        async with self._lock:
            if not force and self._path.is_file():
                if time.monotonic() - self._last_refresh < min_interval_seconds:
                    return True
            ok = await self._refresh(video_url)
            if ok:
                self._last_refresh = time.monotonic()
            return ok

    async def _refresh(self, video_url: str = "") -> bool:
        assert self._path is not None and self._session is not None
        # 优先浏览器获取（含 JS 生成的 UIFID/s_v_web_id）；失败回落 HTTP
        browser_cookies = await self._refresh_via_browser(video_url)
        if browser_cookies:
            return self._write_cookies(browser_cookies, source="浏览器")
        return await self._refresh_via_http(video_url)

    async def _refresh_via_browser(self, video_url: str = "") -> dict | None:
        from .douyin_browser import fetch_cookies_via_browser
        proxy = self._browser_proxy or ""
        cookies = await fetch_cookies_via_browser(
            video_url=video_url, proxy=proxy)
        if cookies:
            log.info("浏览器刷新到 %d 个 cookies", len(cookies))
        return cookies

    def _write_cookies(self, cookies: dict, source: str = "") -> bool:
        assert self._path is not None
        if not cookies:
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(cookies_to_netscape(cookies), "utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("抖音 cookies 写盘失败 %s: %s", self._path, e)
            return False
        log.info("抖音 cookies 已刷新（%d 个，%s）: %s",
                 len(cookies), source, self._path)
        return True

    async def _refresh_via_http(self, video_url: str = "") -> bool:
        assert self._path is not None and self._session is not None
        session = self._session
        headers = {"User-Agent": _USER_AGENT, "Referer": _DOUYIN_REFERER}
        steps: list[str] = []

        def jar_size() -> int:
            return len(jar_to_dict(session.cookie_jar))

        try:
            # 1. 首页拿服务端 Set-Cookie（ttwid / __ac_nonce 等）
            async with session.get(_DOUYIN_HOME, headers=headers,
                                   timeout=_REQUEST_TIMEOUT) as resp:
                steps.append(f"首页 {resp.status}/{jar_size()}cookies")
                resp.release()
            # 2. ttwid 注册端点补 ttwid（best-effort，失败跳过）。
            # 该端点下发的域是 bytedance.com，需复制到 douyin 域下
            # detail 接口才会带上（仿 yt-dlp 头条 _set_cookie 做法）
            try:
                async with session.post(
                        _TTWID_REGISTER_URL, json=_TTWID_PAYLOAD,
                        headers={**headers,
                                 "Content-Type": "application/json"},
                        timeout=_REQUEST_TIMEOUT) as resp:
                    copied = _copy_ttwid_to_douyin(session, resp)
                    steps.append(
                        f"ttwid {resp.status}/{jar_size()}cookies"
                        + ("(+ttwid复制)" if copied else ""))
                    resp.release()
            except (aiohttp.ClientError, TimeoutError) as e:
                steps.append(f"ttwid 异常({e})")
                log.debug("ttwid 注册失败（跳过）: %s", e)
            # 3. 目标视频页补页面级 cookies
            if video_url:
                try:
                    async with session.get(video_url, headers=headers,
                                           allow_redirects=True,
                                           timeout=_REQUEST_TIMEOUT) as resp:
                        steps.append(
                            f"视频页 {resp.status}/{jar_size()}cookies")
                        resp.release()
                except (aiohttp.ClientError, TimeoutError) as e:
                    steps.append(f"视频页异常({e})")
                    log.debug("视频页预热失败（跳过）: %s", e)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("抖音 cookies 刷新失败: %s", e)
            return self._path.is_file()

        cookies = jar_to_dict(session.cookie_jar)
        if not cookies:
            log.warning("抖音 cookies 刷新后 jar 为空（%s）；" + _MANUAL_HINT,
                        "; ".join(steps), self._path)
            return self._path.is_file()
        return self._write_cookies(cookies, source="HTTP预热")
