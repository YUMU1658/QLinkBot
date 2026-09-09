"""douyin_cookies 测试：格式转换、刷新、重试一次规则。"""

import asyncio
import tempfile
import unittest
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Iterable
from unittest import mock

from qlinkbot.douyin import DouyinParser, _is_cookie_error
from qlinkbot.douyin_cookies import (
    DouyinCookieStore,
    cookies_to_netscape,
    jar_to_dict,
)
from qlinkbot.ytdlp_common import ParseError


def _make_morsels(cookies: dict[str, str],
                  domain: str = ".douyin.com") -> list:
    """用真实 SimpleCookie/Morsel 构造 jar 条目（与 aiohttp jar 一致）。"""
    jar = SimpleCookie()
    for name, value in cookies.items():
        jar[name] = value
        jar[name]["domain"] = domain
        jar[name]["path"] = "/"
    return list(jar.values())


def _make_host_only_morsels(cookies: dict[str, str]) -> list:
    """无 domain 的 host-only cookie（aiohttp 会记到响应 host，也应收录）。"""
    jar = SimpleCookie()
    for name, value in cookies.items():
        jar[name] = value
    return list(jar.values())


class _FakeJar(list):
    """行为与 aiohttp CookieJar 一致的可迭代 jar（元素为 Morsel）。"""
    pass


def _jar_with(cookies: Iterable) -> _FakeJar:
    return _FakeJar(cookies)


class NetscapeFormatTest(unittest.TestCase):
    def test_roundtrip_through_mozilla_jar(self):
        from http.cookiejar import MozillaCookieJar
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cookies.txt"
            path.write_text(cookies_to_netscape(
                {"ttwid": "abc", "s_v_web_id": "xyz"}), "utf-8")
            jar = MozillaCookieJar(str(path))
            jar.load(ignore_discard=True, ignore_expires=True)
            got = {c.name: c.value for c in jar}
            self.assertEqual(got, {"ttwid": "abc", "s_v_web_id": "xyz"})

    def test_header_present(self):
        text = cookies_to_netscape({"a": "b"})
        self.assertIn("# Netscape HTTP Cookie File", text)
        self.assertIn("\ta\tb", text)


class JarExportTest(unittest.TestCase):
    def test_filters_by_domain(self):
        jar = _jar_with(_make_morsels({"ttwid": "1"}) +
                    _make_morsels({"sid": "2"}, domain=".qq.com") +
                    _make_morsels({"empty": ""},
                                  domain="www.douyin.com"))
        got = jar_to_dict(jar)
        self.assertEqual(got, {"ttwid": "1", "empty": ""})


class CookieErrorDetectTest(unittest.TestCase):
    def test_fresh_cookies_matches(self):
        err = ParseError(
            "yt-dlp 失败(code=1): ERROR: [Douyin] 123: "
            "Fresh cookies (not necessarily logged in) are needed")
        self.assertTrue(_is_cookie_error(err))

    def test_other_errors_do_not_match(self):
        self.assertFalse(_is_cookie_error(ParseError("HTTP Error 412")))
        self.assertFalse(_is_cookie_error(ParseError("")))


class CookieArgsTest(unittest.TestCase):
    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = DouyinCookieStore(None, Path(d) / "nope.txt")
            self.assertEqual(store.cookie_args(), [])

    def test_existing_file_returns_args(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.txt"
            path.write_text("# Netscape HTTP Cookie File\n", "utf-8")
            store = DouyinCookieStore(None, path)
            self.assertEqual(store.cookie_args(),
                             ["--cookies", str(path)])


class _FakeResponse:
    def __init__(self, cookies: list | None = None, status: int = 200,
                 resp_cookies: dict | None = None):
        self._cookies = cookies or []
        self.status = status
        # 模拟 aiohttp 响应的 .cookies（SimpleCookie）
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        for k, v in (resp_cookies or {}).items():
            jar[k] = v
        self.cookies = jar

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def release(self):
        pass


class _FakeSession:
    """模拟 aiohttp 会话：GET/POST 返回预设 cookies 并写入 jar。"""

    def __init__(self, jar: _FakeJar, set_cookies: list):
        self.cookie_jar = jar
        self._set_cookies = set_cookies
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(f"GET {url}")
        self.cookie_jar.extend(self._set_cookies)
        self._set_cookies = []
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(f"POST {url}")
        if "ttwid" in url:
            return _FakeResponse(
                resp_cookies={"ttwid": "ttwid-from-register"})
        return _FakeResponse()


class RefreshTest(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            jar = _jar_with([])
            session = _FakeSession(jar, _make_morsels({"ttwid": "abc"}))
            store = DouyinCookieStore(session, Path(d) / "c.txt")
            with mock.patch(
                    "qlinkbot.douyin_browser.fetch_cookies_via_browser",
                    new_callable=mock.AsyncMock, return_value=None):
                ok = await store.ensure_fresh(
                    video_url="https://www.douyin.com/video/123", force=True)
            self.assertTrue(ok)
            text = (Path(d) / "c.txt").read_text("utf-8")
            self.assertIn("ttwid", text)
            self.assertIn("GET https://www.douyin.com/",
                          session.calls)
            self.assertIn("POST https://ttwid.bytedance.com/"
                          "ttwid/union/register/", session.calls)

    async def test_empty_jar_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.txt"
            path.write_text("# Netscape HTTP Cookie File\n", "utf-8")
            jar = _jar_with([])
            session = _FakeSession(jar, [])
            store = DouyinCookieStore(session, path)
            with mock.patch(
                    "qlinkbot.douyin_browser.fetch_cookies_via_browser",
                    new_callable=mock.AsyncMock, return_value=None):
                ok = await store.ensure_fresh(force=True)
            # jar 为空：返回旧文件是否存在（不断链）
            self.assertTrue(ok)
            self.assertTrue(path.is_file())

    async def test_browser_cookies_preferred(self):
        with tempfile.TemporaryDirectory() as d:
            jar = _jar_with([])
            session = _FakeSession(jar, [])
            store = DouyinCookieStore(session, Path(d) / "c.txt")

            async def _fake_browser(*args, **kwargs):
                return {"ttwid": "from-browser", "UIFID": "uid123"}

            with mock.patch(
                    "qlinkbot.douyin_browser.fetch_cookies_via_browser",
                    _fake_browser):
                ok = await store.ensure_fresh(
                    video_url="https://www.douyin.com/video/1", force=True)
            self.assertTrue(ok)
            text = (Path(d) / "c.txt").read_text("utf-8")
            self.assertIn("UIFID", text)
            # 浏览器命中后不再走 HTTP
            self.assertEqual(session.calls, [])

    async def test_ttwid_copied_to_douyin_domain(self):
        from qlinkbot.douyin_cookies import _copy_ttwid_to_douyin

        class _JarWithUpdate(_FakeJar):
            def __init__(self):
                super().__init__([])
                self.updated: list = []

            def update_cookies(self, cookies, response_url=None):
                self.updated.append((dict(cookies), str(response_url)))
                for k, v in dict(cookies).items():
                    self.extend(_make_morsels({k: v}))

        jar = _JarWithUpdate()

        class _Sess:
            cookie_jar = jar

        resp = _FakeResponse(resp_cookies={"ttwid": "abc123"})
        self.assertTrue(_copy_ttwid_to_douyin(_Sess(), resp))
        self.assertEqual(len(jar.updated), 1)
        self.assertIn("douyin.com", jar.updated[0][1])
        from qlinkbot.douyin_cookies import jar_to_dict
        self.assertEqual(jar_to_dict(jar).get("ttwid"), "abc123")

    async def test_copy_without_ttwid_returns_false(self):
        from qlinkbot.douyin_cookies import _copy_ttwid_to_douyin

        class _Sess:
            cookie_jar = _jar_with([])

        self.assertFalse(
            _copy_ttwid_to_douyin(_Sess(), _FakeResponse()))

    async def test_concurrent_refresh_single_flight(self):
        with tempfile.TemporaryDirectory() as d:
            jar = _jar_with([])
            session = _FakeSession(jar, _make_morsels({"ttwid": "x"}))
            store = DouyinCookieStore(session, Path(d) / "c.txt")
            # 非 force 模式：文件缺失时并发只刷一次
            with mock.patch(
                    "qlinkbot.douyin_browser.fetch_cookies_via_browser",
                    new_callable=mock.AsyncMock, return_value=None):
                results = await asyncio.gather(*[
                    store.ensure_fresh() for _ in range(5)])
            self.assertTrue(all(results))
            gets = [c for c in session.calls
                    if c == "GET https://www.douyin.com/"]
            self.assertEqual(len(gets), 1)


class ProbeCookieRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_error_refreshes_and_retries_once(self):
        cookie_err = ParseError(
            "yt-dlp 失败(code=1): ERROR: [Douyin] 1: Fresh cookies "
            "(not necessarily logged in) are needed")
        calls: list[list[str]] = []

        async def fake_run(args, timeout, retries=0):
            calls.append(list(args))
            if len(calls) == 1:
                raise cookie_err
            return '{"id": "1"}'

        store = mock.Mock()
        store.cookie_args.return_value = ["--cookies", "c.txt"]
        store.ensure_fresh = mock.AsyncMock(return_value=True)
        parser = DouyinParser(session=None, probe_real_size=False,
                              cookie_store=store)
        with mock.patch("qlinkbot.douyin._run_ytdlp", fake_run):
            meta = await parser.probe("https://www.douyin.com/video/1",
                                      timeout=10)
        self.assertEqual(meta.aweme_id, "1")
        self.assertEqual(len(calls), 2)
        # 第二次重试带上了 --cookies
        self.assertIn("--cookies", calls[1])
        store.ensure_fresh.assert_awaited_once()

    async def test_cookie_error_without_store_raises(self):
        cookie_err = ParseError("Fresh cookies are needed")
        parser = DouyinParser(session=None, probe_real_size=False)
        with mock.patch("qlinkbot.douyin._run_ytdlp",
                        mock.AsyncMock(side_effect=cookie_err)):
            with self.assertRaises(ParseError):
                await parser.probe("https://www.douyin.com/video/1",
                                   timeout=10)

    async def test_second_failure_raises(self):
        cookie_err = ParseError("Fresh cookies are needed")
        store = mock.Mock()
        store.cookie_args.return_value = ["--cookies", "c.txt"]
        store.ensure_fresh = mock.AsyncMock(return_value=True)
        parser = DouyinParser(session=None, probe_real_size=False,
                              cookie_store=store)
        with mock.patch("qlinkbot.douyin._run_ytdlp",
                        mock.AsyncMock(side_effect=cookie_err)):
            with self.assertRaises(ParseError):
                await parser.probe("https://www.douyin.com/video/1",
                                   timeout=10)


class RealJarExportTest(unittest.TestCase):
    def test_real_morsel_objects(self):
        # 用真实 Morsel 验证 jar_to_dict 的迭代逻辑
        jar = _jar_with(
            _make_morsels({"ttwid": "abc"}, domain="www.douyin.com") +
            _make_morsels({"s_v_web_id": "xyz"}))
        got = jar_to_dict(jar)
        self.assertEqual(got, {"ttwid": "abc", "s_v_web_id": "xyz"})

    def test_host_only_cookies_included(self):
        jar = _jar_with(_make_host_only_morsels({"ttwid": "abc"}))
        got = jar_to_dict(jar)
        self.assertEqual(got, {"ttwid": "abc"})


if __name__ == "__main__":
    unittest.main()
