"""douyin 测试：链接提取、文案组装、配置开关、直链下载大小限制。"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from qlinkbot.douyin import DouyinMeta, DouyinParser, _extract_tags
from qlinkbot.extract import Target, extract_target
from qlinkbot.sender import build_douyin_text_reply
from qlinkbot.sessionconfig import (
    PLATFORM_BILIBILI,
    PLATFORM_DOUYIN,
    DouyinOptions,
    SessionConfigStore,
)
from qlinkbot.ytdlp_common import TooLargeError

_MB = 1 << 20


class DouyinExtractTest(unittest.TestCase):
    def test_full_link(self):
        t = extract_target(
            "看看这个 https://www.douyin.com/video/1234567890123456789 不错")
        self.assertEqual(
            t, Target("aweme", "1234567890123456789", PLATFORM_DOUYIN))

    def test_short_link(self):
        t = extract_target("https://v.douyin.com/abc123XYZ/")
        self.assertEqual(t.platform, PLATFORM_DOUYIN)
        self.assertEqual(t.kind, "url")

    def test_note_page_not_matched(self):
        self.assertIsNone(extract_target(
            "https://www.douyin.com/note/1234567890123456789"))
        self.assertIsNone(extract_target(
            "https://www.douyin.com/user/abc123"))

    def test_bilibili_still_works(self):
        t = extract_target("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(t.platform, PLATFORM_BILIBILI)
        self.assertEqual(t.kind, "bvid")

    def test_earliest_position_wins_across_platforms(self):
        t = extract_target(
            "https://www.douyin.com/video/111 先这个 "
            "https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertEqual(t.platform, PLATFORM_DOUYIN)
        t = extract_target(
            "https://www.bilibili.com/video/BV1xx411c7mD 先这个 "
            "https://www.douyin.com/video/111")
        self.assertEqual(t.platform, PLATFORM_BILIBILI)

    def test_cache_key_has_platform_prefix(self):
        bili = Target("bvid", "BV1xx", PLATFORM_BILIBILI).cache_key
        douyin = Target("aweme", "123", PLATFORM_DOUYIN).cache_key
        self.assertTrue(bili.startswith("bilibili:"))
        self.assertTrue(douyin.startswith("douyin:"))
        self.assertNotEqual(bili, douyin)

    def test_aweme_resolve_url(self):
        t = Target("aweme", "123456", PLATFORM_DOUYIN)
        self.assertEqual(t.resolve_url,
                         "https://www.douyin.com/video/123456")


class DouyinTagsTest(unittest.TestCase):
    def test_tags_from_info(self):
        tags = _extract_tags({"tags": ["一起看海", "#夏日"]}, "正文 #多余")
        self.assertEqual(tags, ["一起看海", "夏日"])

    def test_tags_fallback_to_description(self):
        tags = _extract_tags({}, "#一起看海 出现在你的夏日里")
        self.assertEqual(tags, ["一起看海"])

    def test_no_tags(self):
        self.assertEqual(_extract_tags({}, "普通简介无话题"), [])


class DouyinReplyTest(unittest.TestCase):
    def _meta(self) -> DouyinMeta:
        return DouyinMeta(
            aweme_id="123",
            title="海边的夏天",
            author="杨超越",
            plays=12345678,
            likes=345678,
            comments=12345,
            shares=2345,
            description="#一起看海 出现在你的夏日里",
            tags=["一起看海"],
            webpage_url="https://www.douyin.com/video/123",
        )

    def test_full_reply(self):
        text = build_douyin_text_reply(self._meta(), DouyinOptions())
        self.assertIn("海边的夏天", text)
        self.assertIn("作者：杨超越", text)
        self.assertNotIn("播放", text)
        self.assertIn("点赞 34.6万", text)
        self.assertIn("评论 1.2万", text)
        self.assertIn("转发 2345", text)
        self.assertIn("#一起看海 出现在你的夏日里", text)
        self.assertIn("标签：#一起看海", text)
        self.assertIn("原视频：https://www.douyin.com/video/123", text)

    def test_no_opts_equals_all_on(self):
        self.assertEqual(build_douyin_text_reply(self._meta()),
                         build_douyin_text_reply(self._meta(),
                                                 DouyinOptions()))

    def test_title_off_hides_title_and_author(self):
        text = build_douyin_text_reply(self._meta(), DouyinOptions(title=False))
        self.assertNotIn("海边的夏天", text)
        self.assertNotIn("作者：", text)
        self.assertIn("点赞 34.6万", text)
        self.assertIn("标签：#一起看海", text)
        self.assertIn("原视频：", text)

    def test_stats_off_hides_stats_line(self):
        text = build_douyin_text_reply(self._meta(), DouyinOptions(stats=False))
        self.assertNotIn("点赞", text)
        self.assertNotIn("评论", text)
        self.assertNotIn("转发", text)
        self.assertIn("作者：杨超越", text)
        self.assertIn("标签：#一起看海", text)

    def test_intro_off_hides_desc_and_tags(self):
        text = build_douyin_text_reply(self._meta(), DouyinOptions(intro=False))
        self.assertNotIn("#一起看海 出现在你的夏日里", text)
        self.assertNotIn("标签：", text)
        self.assertIn("作者：杨超越", text)
        self.assertIn("原视频：", text)

    def test_link_off_hides_original_link(self):
        text = build_douyin_text_reply(self._meta(), DouyinOptions(link=False))
        self.assertNotIn("原视频：", text)
        self.assertIn("作者：杨超越", text)
        self.assertIn("标签：#一起看海", text)

    def test_all_text_off_returns_empty(self):
        opts = DouyinOptions(title=False, stats=False, link=False, intro=False)
        self.assertEqual(build_douyin_text_reply(self._meta(), opts), "")

    def test_no_tags_line_omitted(self):
        meta = self._meta()
        meta.tags = []
        self.assertNotIn("标签：",
                         build_douyin_text_reply(meta, DouyinOptions()))

    def test_empty_description_placeholder(self):
        meta = self._meta()
        meta.description = ""
        self.assertIn("（无简介）",
                      build_douyin_text_reply(meta, DouyinOptions()))


class DouyinOptionsTest(unittest.TestCase):
    def test_defaults_all_on(self):
        store = SessionConfigStore("")
        opts = store.get("group:X", PLATFORM_DOUYIN)
        for key in ("enabled", "title", "stats", "link", "intro", "video"):
            self.assertTrue(getattr(opts, key), key)

    def test_toggle_and_isolation(self):
        store = SessionConfigStore("")
        store.update("group:X", PLATFORM_DOUYIN, enabled=False, video=False)
        opts = store.get("group:X", PLATFORM_DOUYIN)
        self.assertFalse(opts.enabled)
        self.assertFalse(opts.video)
        self.assertTrue(opts.title)
        # B 站开关不受影响
        self.assertTrue(store.get("group:X", PLATFORM_BILIBILI).enabled)

    def test_config_file_roundtrip(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.json"
            path.write_text(json.dumps(
                {"group:X": {"douyin": {"enabled": False, "intro": False}}}),
                "utf-8")
            store = SessionConfigStore(path)
            opts = store.get("group:X", PLATFORM_DOUYIN)
            self.assertFalse(opts.enabled)
            self.assertFalse(opts.intro)
            self.assertTrue(opts.title)  # 未持久化的字段取默认值
            self.assertIsInstance(opts, DouyinOptions)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunked(self, n):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status: int = 200, headers: dict | None = None,
                 chunks: tuple = ()):
        self.status = status
        self.headers = headers or {}
        self.content = _FakeStream(chunks)


class _FakeContext:
    def __init__(self, outcome):
        self._outcome = outcome  # _FakeResponse 或 Exception

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """记录请求 URL 的 aiohttp.ClientSession 桩（url → _FakeResponse）。"""

    def __init__(self, outcomes: dict):
        self._outcomes = outcomes
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        return _FakeContext(self._outcomes[url])


class DouyinDirectDownloadLimitTest(unittest.IsolatedAsyncioTestCase):
    """直链下载的大小限制：Content-Length 预检与写入累计拦截。"""

    def _meta(self, urls: list[str]) -> DouyinMeta:
        return DouyinMeta(aweme_id="123", play_urls=urls)

    async def test_content_length_over_limit_aborts_without_file(self):
        session = _FakeSession({
            "https://cdn/1": _FakeResponse(
                headers={"Content-Length": str(40 * _MB)}),
        })
        parser = DouyinParser(session, max_file_size_bytes=30 * _MB)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TooLargeError):
                await parser._download_direct(
                    self._meta(["https://cdn/1"]), "u", Path(d), 10)
            self.assertFalse((Path(d) / "123.mp4").exists())
        # 镜像直链是同一文件，超限即终止，不逐个重试
        self.assertEqual(session.requested, ["https://cdn/1"])

    async def test_midstream_over_limit_deletes_partial(self):
        session = _FakeSession({
            "https://cdn/1": _FakeResponse(chunks=[b"x" * _MB] * 40),
        })
        parser = DouyinParser(session, max_file_size_bytes=30 * _MB)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TooLargeError):
                await parser._download_direct(
                    self._meta(["https://cdn/1"]), "u", Path(d), 10)
            self.assertFalse((Path(d) / "123.mp4").exists())

    async def test_under_limit_downloads(self):
        session = _FakeSession({
            "https://cdn/1": _FakeResponse(
                headers={"Content-Length": str(_MB)}, chunks=[b"v" * _MB]),
        })
        parser = DouyinParser(session, max_file_size_bytes=30 * _MB)
        with tempfile.TemporaryDirectory() as d:
            path = await parser._download_direct(
                self._meta(["https://cdn/1"]), "u", Path(d), 10)
            self.assertEqual(path, Path(d) / "123.mp4")
            self.assertEqual(path.stat().st_size, _MB)

    async def test_no_limit_by_default(self):
        session = _FakeSession({
            "https://cdn/1": _FakeResponse(chunks=[b"x" * _MB] * 40),
        })
        parser = DouyinParser(session)  # max_file_size_bytes 默认 0 不限制
        with tempfile.TemporaryDirectory() as d:
            path = await parser._download_direct(
                self._meta(["https://cdn/1"]), "u", Path(d), 10)
            self.assertEqual(path.stat().st_size, 40 * _MB)

    async def test_too_large_propagates_without_ytdlp_fallback(self):
        session = _FakeSession({
            "https://cdn/1": _FakeResponse(
                headers={"Content-Length": str(40 * _MB)}),
        })
        parser = DouyinParser(session, max_file_size_bytes=30 * _MB)
        run = mock.AsyncMock()
        with mock.patch("qlinkbot.douyin._run_ytdlp", run):
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(TooLargeError):
                    await parser.download(
                        "u", Path(d), 10, meta=self._meta(["https://cdn/1"]))
        run.assert_not_called()


class DouyinBrowserProbeSizeTest(unittest.IsolatedAsyncioTestCase):
    """浏览器直取 detail 缺 data_size 时用直链 Range 探测补齐预检大小。"""

    def _info(self, data_size: int = 0,
              urls: tuple = ("https://cdn/1",)):
        from qlinkbot.douyin_browser import BrowserVideoInfo
        return BrowserVideoInfo(aweme_id="123", play_urls=list(urls),
                                data_size=data_size)

    async def _probe(self, session, info, probe_real_size: bool = True):
        parser = DouyinParser(session, probe_real_size=probe_real_size)
        fetch = mock.AsyncMock(return_value=info)
        with mock.patch("qlinkbot.douyin_browser.fetch_video_info", fetch):
            return await parser._probe_via_browser("https://www.douyin.com/video/123")

    async def test_data_size_zero_probed(self):
        probe = mock.AsyncMock(return_value=12 * _MB)
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(_FakeSession({}), self._info())
        self.assertEqual(meta.estimated_size, 12 * _MB)
        probe.assert_awaited_once()
        # 探测目标是第一个播放直链（url_list 为同一文件的 CDN 镜像）
        self.assertEqual(probe.await_args.args[1],
                         [{"url": "https://cdn/1"}])

    async def test_probe_failure_keeps_zero(self):
        probe = mock.AsyncMock(return_value=None)
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(_FakeSession({}), self._info())
        self.assertEqual(meta.estimated_size, 0)

    async def test_data_size_present_skips_probe(self):
        probe = mock.AsyncMock()
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(_FakeSession({}),
                                     self._info(data_size=7 * _MB))
        self.assertEqual(meta.estimated_size, 7 * _MB)
        probe.assert_not_awaited()

    async def test_probe_disabled_by_config(self):
        probe = mock.AsyncMock()
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(_FakeSession({}), self._info(),
                                     probe_real_size=False)
        self.assertEqual(meta.estimated_size, 0)
        probe.assert_not_awaited()

    async def test_no_play_urls_skips_probe(self):
        probe = mock.AsyncMock()
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(_FakeSession({}),
                                     self._info(urls=()))
        self.assertEqual(meta.estimated_size, 0)
        probe.assert_not_awaited()

    async def test_no_session_skips_probe(self):
        probe = mock.AsyncMock()
        with mock.patch("qlinkbot.douyin._probe_real_size", probe):
            meta = await self._probe(None, self._info())
        self.assertEqual(meta.estimated_size, 0)
        probe.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
