"""管理卡片测试：设置卡文案精简后仅保留标题与切换提示，按钮承载状态。"""

import unittest

from qlinkbot.admin import (DATA_BILI_SETTINGS, DATA_BILI_TOGGLE,
                            _bili_settings_card, _menu_card, TOGGLE_ORDER)
from qlinkbot.sessionconfig import BiliOptions


def _toggle_buttons(keyboard: dict) -> dict[str, dict]:
    buttons = (b for row in keyboard["content"]["rows"]
               for b in row["buttons"])
    return {b["action"]["data"][len(DATA_BILI_TOGGLE):]: b
            for b in buttons
            if b["action"]["data"].startswith(DATA_BILI_TOGGLE)}


class BiliSettingsCardTest(unittest.TestCase):
    def test_text_only_title_and_hint(self):
        text, keyboard = _bili_settings_card(BiliOptions(), admin_only=False)
        self.assertEqual(text, "## bilibili 解析设置\n点击按钮切换对应开关：")
        self.assertTrue(keyboard["content"]["rows"])

    def test_keyboard_rows_and_toggle_keys(self):
        _, keyboard = _bili_settings_card(BiliOptions(), admin_only=False)
        rows = keyboard["content"]["rows"]
        self.assertEqual(len(rows), 5)
        toggles = _toggle_buttons(keyboard)
        self.assertEqual(set(toggles), set(TOGGLE_ORDER))
        others = [b["action"]["data"] for row in rows for b in row["buttons"]
                  if not b["action"]["data"].startswith(DATA_BILI_TOGGLE)]
        self.assertEqual(others, ["admin:menu"])

    def test_button_state_matches_options(self):
        opts = BiliOptions(enabled=False, link=False)
        _, keyboard = _bili_settings_card(opts, admin_only=False)
        toggles = _toggle_buttons(keyboard)
        self.assertEqual(toggles["enabled"]["render_data"]["label"],
                         "⛔ B站解析")
        self.assertEqual(toggles["enabled"]["render_data"]["style"], 0)
        self.assertEqual(toggles["cover"]["render_data"]["label"],
                         "✅ 发送封面")
        self.assertEqual(toggles["cover"]["render_data"]["style"], 3)
        self.assertEqual(toggles["link"]["render_data"]["label"],
                         "⛔ 原视频链接")


class MenuCardTest(unittest.TestCase):
    def test_menu_card_private_chat(self):
        text, keyboard = _menu_card(None, admin_only=False)
        self.assertEqual(text, "## 解析管理\n请选择要管理的解析平台：")
        buttons = keyboard["content"]["rows"][0]["buttons"]
        self.assertEqual(buttons[0]["action"]["data"], DATA_BILI_SETTINGS)

    def test_menu_card_group_at_prefix(self):
        text, _ = _menu_card("U1", admin_only=True)
        self.assertTrue(text.startswith('<qqbot-at-user id="U1" />'))


if __name__ == "__main__":
    unittest.main()
