"""bilibili 大小预检测试：格式匹配修复与 CDN Range 实际探测。"""

import asyncio
import unittest

import aiohttp

from qlinkbot.bilibili import (
    VideoMeta,
    _content_range_total,
    _estimate_size,
    _probe_real_size,
    _selected_formats,
)


def _dump(format_id: str, formats: list[dict], duration: float = 100.0) -> dict:
    return {"format_id": format_id, "formats": formats, "duration": duration}


class SelectedFormatsTest(unittest.TestCase):
    def test_merged_format_id_split(self):
        video = {"format_id": "30016", "filesize": 111}
        audio = {"format_id": "30216", "filesize": 222}
        other = {"format_id": "30080", "filesize": 999}
        info = _dump("30016+30216", [audio, other, video])
        self.assertEqual(_selected_formats(info), [audio, video])

    def test_single_format_id(self):
        fmt = {"format_id": "16", "filesize": 5}
        self.assertEqual(_selected_formats(_dump("16", [fmt])), [fmt])

    def test_empty_format_id(self):
        self.assertEqual(_selected_formats(_dump("", [{"format_id": "16"}])), [])


class EstimateSizeTest(unittest.TestCase):
    def test_merged_exact_filesize_sum(self):
        info = _dump("30016+30216", [
            {"format_id": "30216", "filesize": 200_000},
            {"format_id": "30016", "filesize": 1_000_000},
            {"format_id": "30080", "filesize": 9_999_999},
        ])
        self.assertEqual(_estimate_size(info), 1_200_000)

    def test_missing_filesize_falls_to_tbr(self):
        info = _dump("30016+30216", [
            {"format_id": "30216", "filesize": 100_000},
            {"format_id": "30016", "tbr": 400},  # 400kbps × 100s
        ])
        self.assertEqual(_estimate_size(info), 100_000 + int(400_000 / 8 * 100))

    def test_no_match_falls_to_blanket_estimate(self):
        info = _dump("30016+30216", [{"format_id": "30080", "filesize": 1}])
        self.assertEqual(_estimate_size(info), int(700_000 / 8 * 100))

    def test_no_duration_no_data(self):
        self.assertEqual(_estimate_size(_dump("", [], duration=0)), 0)


class ContentRangeTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(_content_range_total("bytes 0-0/123456"), 123456)

    def test_unknown_total(self):
        self.assertIsNone(_content_range_total("bytes 0-0/*"))

    def test_empty(self):
        self.assertIsNone(_content_range_total(None))
        self.assertIsNone(_content_range_total(""))


class _FakeResponse:
    def __init__(self, status: int, headers: dict):
        self.status = status
        self.headers = headers


class _FakeContext:
    def __init__(self, outcome):
        self._outcome = outcome  # (status, headers) 或 Exception

    async def __aenter__(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return _FakeResponse(*self._outcome)

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """记录请求 URL 的 aiohttp.ClientSession 桩。"""

    def __init__(self, outcomes: dict):
        self.outcomes = outcomes
        self.requested: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        return _FakeContext(self.outcomes[url])


class ProbeRealSizeTest(unittest.IsolatedAsyncioTestCase):
    async def test_range_206_sum(self):
        session = _FakeSession({
            "https://cdn/v.m4s": (206, {"Content-Range": "bytes 0-0/1000000"}),
            "https://cdn/a.m4s": (206, {"Content-Range": "bytes 0-0/200000"}),
        })
        fmts = [{"url": "https://cdn/v.m4s"}, {"url": "https://cdn/a.m4s"}]
        self.assertEqual(await _probe_real_size(session, fmts), 1_200_000)

    async def test_200_falls_to_content_length(self):
        session = _FakeSession({
            "https://cdn/v.m4s": (200, {"Content-Length": "1000000"}),
            "https://cdn/a.m4s": (200, {"Content-Length": "200000"}),
        })
        fmts = [{"url": "https://cdn/v.m4s"}, {"url": "https://cdn/a.m4s"}]
        self.assertEqual(await _probe_real_size(session, fmts), 1_200_000)

    async def test_client_error_returns_none(self):
        session = _FakeSession({
            "https://cdn/v.m4s": (206, {"Content-Range": "bytes 0-0/100"}),
            "https://cdn/a.m4s": aiohttp.ClientError("boom"),
        })
        fmts = [{"url": "https://cdn/v.m4s"}, {"url": "https://cdn/a.m4s"}]
        self.assertIsNone(await _probe_real_size(session, fmts))

    async def test_bad_status_returns_none(self):
        session = _FakeSession({"https://cdn/v.m4s": (403, {})})
        self.assertIsNone(
            await _probe_real_size(session, [{"url": "https://cdn/v.m4s"}]))

    async def test_fragments_or_missing_url_skips_probe(self):
        session = _FakeSession({})
        self.assertIsNone(await _probe_real_size(
            session, [{"url": "https://cdn/v.m4s", "fragments": [{}]}]))
        self.assertIsNone(await _probe_real_size(session, [{"filesize": 1}]))
        self.assertEqual(session.requested, [])

    async def test_timeout_returns_none(self):
        class _HangingContext:
            async def __aenter__(self):
                await asyncio.sleep(3600)

            async def __aexit__(self, *exc):
                return False

        session = _FakeSession({})
        session.get = lambda url, headers=None, timeout=None: _HangingContext()
        self.assertIsNone(
            await _probe_real_size(session, [{"url": "https://cdn/v.m4s"}]))


class MetaCacheTest(unittest.TestCase):
    def test_roundtrip_includes_estimated_size(self):
        meta = VideoMeta(bvid="BV1xx", estimated_size=12345)
        restored = VideoMeta.from_cache(meta.to_cache())
        self.assertEqual(restored.estimated_size, 12345)

    def test_old_cache_entry_without_estimated_size(self):
        restored = VideoMeta.from_cache({"bvid": "BV1xx", "title": "t"})
        self.assertEqual(restored.estimated_size, 0)


if __name__ == "__main__":
    unittest.main()
