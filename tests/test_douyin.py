"""douyin 测试：链接提取、文案组装、配置开关。"""

import unittest

from qlinkbot.douyin import DouyinMeta, _extract_tags
from qlinkbot.extract import Target, extract_target
from qlinkbot.sender import build_douyin_text_reply
from qlinkbot.sessionconfig import (
    PLATFORM_BILIBILI,
    PLATFORM_DOUYIN,
    DouyinOptions,
    SessionConfigStore,
)


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


if __name__ == "__main__":
    unittest.main()
