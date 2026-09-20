"""管理卡片测试：设置卡文案精简后仅保留标题与切换提示，按钮承载状态。"""

import unittest

from qlinkbot.admin import (DATA_BILI_SETTINGS, DATA_BILI_TOGGLE,
                            DATA_DOUYIN_SETTINGS, DATA_DOUYIN_TOGGLE,
                            DOUYIN_TOGGLE_ORDER, TOGGLE_ORDER,
                            _bili_settings_card, _douyin_settings_card,
                            _menu_card)
from qlinkbot.sessionconfig import (BiliOptions, DouyinOptions,
                            SessionConfigStore)


def _toggle_buttons(keyboard: dict,
                    prefix: str = DATA_BILI_TOGGLE) -> dict[str, dict]:
    buttons = (b for row in keyboard["content"]["rows"]
               for b in row["buttons"])
    return {b["action"]["data"][len(prefix):]: b
            for b in buttons
            if b["action"]["data"].startswith(prefix)}


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

    def test_bili_card_group_at_prefix(self):
        text, _ = _bili_settings_card(BiliOptions(), admin_only=True, at_openid="U1")
        self.assertTrue(text.startswith('<qqbot-at-user id="U1" />\n## bilibili 解析设置'))


class DouyinSettingsCardTest(unittest.TestCase):
    def test_rows_and_toggle_keys(self):
        _, keyboard = _douyin_settings_card(DouyinOptions(), admin_only=False)
        rows = keyboard["content"]["rows"]
        self.assertEqual(len(rows), 5)
        toggles = _toggle_buttons(keyboard, DATA_DOUYIN_TOGGLE)
        self.assertEqual(set(toggles), set(DOUYIN_TOGGLE_ORDER))
        others = [b["action"]["data"] for row in rows for b in row["buttons"]
                  if not b["action"]["data"].startswith(DATA_DOUYIN_TOGGLE)]
        self.assertEqual(others, ["admin:menu"])

    def test_button_state_matches_options(self):
        opts = DouyinOptions(enabled=False, stats=False)
        _, keyboard = _douyin_settings_card(opts, admin_only=False)
        toggles = _toggle_buttons(keyboard, DATA_DOUYIN_TOGGLE)
        self.assertEqual(toggles["enabled"]["render_data"]["label"],
                          "⛔ 抖音解析")
        self.assertEqual(toggles["enabled"]["render_data"]["style"], 0)
        self.assertEqual(toggles["title"]["render_data"]["label"],
                          "✅ 发送标题")
        self.assertEqual(toggles["title"]["render_data"]["style"], 3)
        self.assertEqual(toggles["stats"]["render_data"]["label"],
                          "⛔ 视频数据")

    def test_douyin_card_group_at_prefix(self):
        text, _ = _douyin_settings_card(DouyinOptions(), admin_only=True, at_openid="U1")
        self.assertTrue(text.startswith('<qqbot-at-user id="U1" />\n## douyin 解析设置'))


class MenuCardTest(unittest.TestCase):
    def test_menu_card_private_chat(self):
        text, keyboard = _menu_card(None, admin_only=False)
        self.assertEqual(text, "## 解析管理\n请选择要管理的解析平台：")
        buttons = keyboard["content"]["rows"][0]["buttons"]
        self.assertEqual(buttons[0]["action"]["data"], DATA_BILI_SETTINGS)
        self.assertEqual(buttons[1]["action"]["data"], DATA_DOUYIN_SETTINGS)

    def test_menu_card_group_at_prefix(self):
        text, _ = _menu_card("U1", admin_only=True)
        self.assertTrue(text.startswith('<qqbot-at-user id="U1" />'))


class AdminServiceInteractionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock
        from qlinkbot.admin import AdminService
        from qlinkbot.config import Config

        self.cfg = Config()
        self.api = MagicMock()
        self.api.put_interaction = AsyncMock()
        self.sender = MagicMock()
        self.sender.send_markdown = AsyncMock()
        self.sender.send_markdown_event = AsyncMock()
        self.sender.reply_text = AsyncMock()
        self.store = SessionConfigStore()
        self.service = AdminService(self.cfg, self.api, self.sender, self.store)

    async def test_group_interaction_mentions_clicker(self):
        from qlinkbot.events import InboundMessage, InteractionEvent

        admin_msg = InboundMessage(
            event_type="GROUP_AT_MESSAGE_CREATE",
            message_id="m1",
            content="/管理",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            is_group=True,
            member_role="admin",
        )
        self.service.observe(admin_msg)

        # 1. 点击 B站设置按钮
        ev_bili = InteractionEvent(
            interaction_id="i1",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            clicker_openid="ADMIN_1",
            is_group=True,
            button_id="b1",
            button_data=DATA_BILI_SETTINGS,
        )
        await self.service.handle_interaction(ev_bili)
        self.api.put_interaction.assert_awaited_with("i1", 0)
        self.sender.send_markdown_event.assert_awaited()
        call_args = self.sender.send_markdown_event.call_args
        self.assertTrue(call_args.args[3].startswith('<qqbot-at-user id="ADMIN_1" />\n## bilibili 解析设置'))

        # 2. 点击 B站 toggle 按钮
        self.sender.send_markdown_event.reset_mock()
        ev_toggle = InteractionEvent(
            interaction_id="i2",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            clicker_openid="ADMIN_1",
            is_group=True,
            button_id="b2",
            button_data=DATA_BILI_TOGGLE + "enabled",
        )
        await self.service.handle_interaction(ev_toggle)
        call_args = self.sender.send_markdown_event.call_args
        self.assertTrue(call_args.args[3].startswith('<qqbot-at-user id="ADMIN_1" />\n## bilibili 解析设置'))

        # 3. 点击 抖音设置按钮
        self.sender.send_markdown_event.reset_mock()
        ev_dy = InteractionEvent(
            interaction_id="i3",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            clicker_openid="ADMIN_1",
            is_group=True,
            button_id="b3",
            button_data=DATA_DOUYIN_SETTINGS,
        )
        await self.service.handle_interaction(ev_dy)
        call_args = self.sender.send_markdown_event.call_args
        self.assertTrue(call_args.args[3].startswith('<qqbot-at-user id="ADMIN_1" />\n## douyin 解析设置'))

        # 4. 点击 抖音 toggle 按钮
        self.sender.send_markdown_event.reset_mock()
        ev_dy_toggle = InteractionEvent(
            interaction_id="i4",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            clicker_openid="ADMIN_1",
            is_group=True,
            button_id="b4",
            button_data=DATA_DOUYIN_TOGGLE + "video",
        )
        await self.service.handle_interaction(ev_dy_toggle)
        call_args = self.sender.send_markdown_event.call_args
        self.assertTrue(call_args.args[3].startswith('<qqbot-at-user id="ADMIN_1" />\n## douyin 解析设置'))

        # 5. 点击 返回主菜单按钮
        self.sender.send_markdown_event.reset_mock()
        ev_menu = InteractionEvent(
            interaction_id="i5",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            clicker_openid="ADMIN_1",
            is_group=True,
            button_id="b5",
            button_data="admin:menu",
        )
        await self.service.handle_interaction(ev_menu)
        call_args = self.sender.send_markdown_event.call_args
        self.assertTrue(call_args.args[3].startswith('<qqbot-at-user id="ADMIN_1" />\n## 解析管理'))

    async def test_c2c_interaction_does_not_mention(self):
        from qlinkbot.events import InteractionEvent

        ev = InteractionEvent(
            interaction_id="i_c2c",
            session_key="c2c:U_USER",
            user_openid="U_USER",
            group_openid=None,
            clicker_openid="U_USER",
            is_group=False,
            button_id="b1",
            button_data=DATA_BILI_SETTINGS,
        )
        await self.service.handle_interaction(ev)
        call_args = self.sender.send_markdown_event.call_args
        self.assertFalse("<qqbot-at-user" in call_args.args[3])
        self.assertTrue(call_args.args[3].startswith("## bilibili 解析设置"))

    async def test_group_non_admin_interaction_rejected(self):
        from qlinkbot.events import InboundMessage, InteractionEvent

        user_msg = InboundMessage(
            event_type="GROUP_AT_MESSAGE_CREATE",
            message_id="m2",
            content="hello",
            session_key="group:G1",
            user_openid="MEMBER_1",
            group_openid="G1",
            is_group=True,
            member_role="member",
        )
        self.service.observe(user_msg)

        ev = InteractionEvent(
            interaction_id="i_reject",
            session_key="group:G1",
            user_openid="MEMBER_1",
            group_openid="G1",
            clicker_openid="MEMBER_1",
            is_group=True,
            button_id="b1",
            button_data=DATA_BILI_SETTINGS,
        )
        await self.service.handle_interaction(ev)
        self.sender.send_markdown_event.assert_not_called()

    async def test_group_command_mentions_sender_in_full_mode(self):
        from qlinkbot.events import InboundMessage

        msg = InboundMessage(
            event_type="GROUP_MESSAGE_CREATE",
            message_id="m3",
            content="/管理",
            session_key="group:G1",
            user_openid="ADMIN_1",
            group_openid="G1",
            is_group=True,
            member_role="admin",
        )
        await self.service.handle_command(msg)
        self.sender.send_markdown.assert_awaited()
        call_args = self.sender.send_markdown.call_args
        self.assertTrue(call_args.args[1].startswith('<qqbot-at-user id="ADMIN_1" />\n## 解析管理'))


if __name__ == "__main__":
    unittest.main()
