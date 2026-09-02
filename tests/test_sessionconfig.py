"""会话级解析配置存储测试：平台分层持久化（session_key -> 平台 -> 配置）。"""

import json
import tempfile
import unittest
from pathlib import Path

from qlinkbot.sessionconfig import (BiliOptions, PLATFORM_BILIBILI,
                                    SessionConfigStore)


class SessionConfigStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "session_settings.json"

    def test_default_all_enabled(self):
        store = SessionConfigStore()
        opts = store.get("group:G1", PLATFORM_BILIBILI)
        self.assertIsInstance(opts, BiliOptions)
        self.assertTrue(all(vars(opts).values()))

    def test_update_and_roundtrip(self):
        store = SessionConfigStore(self.path)
        store.update("group:G1", PLATFORM_BILIBILI, enabled=False, link=False)
        reloaded = SessionConfigStore(self.path)
        opts = reloaded.get("group:G1", PLATFORM_BILIBILI)
        self.assertFalse(opts.enabled)
        self.assertFalse(opts.link)
        self.assertTrue(opts.cover)

    def test_sessions_isolated(self):
        store = SessionConfigStore()
        store.update("group:G1", PLATFORM_BILIBILI, enabled=False)
        self.assertTrue(store.get("group:G2", PLATFORM_BILIBILI).enabled)

    def test_nested_file_layout(self):
        store = SessionConfigStore(self.path)
        store.update("group:G1", PLATFORM_BILIBILI, enabled=False)
        raw = json.loads(self.path.read_text("utf-8"))
        self.assertEqual(set(raw["group:G1"]), {PLATFORM_BILIBILI})
        self.assertFalse(raw["group:G1"][PLATFORM_BILIBILI]["enabled"])

    def test_unknown_platform_and_fields_ignored(self):
        self.path.write_text(json.dumps({
            "group:G1": {
                "unknown_platform": {"enabled": False},
                PLATFORM_BILIBILI: {"enabled": False, "nonexistent": True},
            },
        }), "utf-8")
        store = SessionConfigStore(self.path)
        opts = store.get("group:G1", PLATFORM_BILIBILI)
        self.assertFalse(opts.enabled)
        self.assertNotIn("nonexistent", vars(opts))

    def test_old_flat_format_loads_empty(self):
        # 旧版无平台分层的平铺格式：按约定不做迁移，载入为空
        self.path.write_text(json.dumps({
            "group:G1": {"enabled": False, "cover": False},
        }), "utf-8")
        store = SessionConfigStore(self.path)
        opts = store.get("group:G1", PLATFORM_BILIBILI)
        self.assertTrue(opts.enabled)
        self.assertTrue(opts.cover)


if __name__ == "__main__":
    unittest.main()
