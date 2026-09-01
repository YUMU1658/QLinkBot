"""events 归一化测试：@ 前缀剥离（全量消息模式 @机器人 消息修复）。"""

import unittest

from qlinkbot.events import parse_event, strip_at_prefix


class StripAtPrefixTest(unittest.TestCase):
    def test_v2_at_user_tag(self):
        self.assertEqual(strip_at_prefix('<qqbot-at-user id="ABC" /> /管理'),
                         "/管理")

    def test_v2_at_user_tag_compact(self):
        self.assertEqual(strip_at_prefix('<qqbot-at-user id="ABC"/> /管理'),
                         "/管理")

    def test_at_everyone_tag(self):
        self.assertEqual(strip_at_prefix("<qqbot-at-everyone /> /管理"),
                         "/管理")

    def test_channel_style_tags(self):
        self.assertEqual(strip_at_prefix("<@!ABC123> /管理"), "/管理")
        self.assertEqual(strip_at_prefix("<@ABC> /管理"), "/管理")

    def test_plain_text_at(self):
        self.assertEqual(strip_at_prefix("@机器人 /管理"), "/管理")
        self.assertEqual(strip_at_prefix("＠机器人 /管理"), "/管理")

    def test_multiple_segments(self):
        self.assertEqual(strip_at_prefix("@a <@!X> ＠b /管理"), "/管理")
        self.assertEqual(
            strip_at_prefix('<qqbot-at-user id="X" />'
                            '<qqbot-at-user id="Y" /> /管理'), "/管理")

    def test_plain_command_with_spaces(self):
        self.assertEqual(strip_at_prefix(" /管理 "), "/管理")
        self.assertEqual(strip_at_prefix("/管理"), "/管理")

    def test_normal_text_untouched(self):
        self.assertEqual(strip_at_prefix("大家早上好呀"), "大家早上好呀")
        self.assertEqual(strip_at_prefix("邮箱是 xxx@xx.com 哦"),
                         "邮箱是 xxx@xx.com 哦")

    def test_at_only_message(self):
        self.assertEqual(strip_at_prefix("@机器人"), "")
        self.assertEqual(strip_at_prefix('<qqbot-at-user id="X" />'), "")

    def test_empty(self):
        self.assertEqual(strip_at_prefix(""), "")
        self.assertEqual(strip_at_prefix(None), "")


class ParseEventTest(unittest.TestCase):
    GROUP_BASE = {
        "id": "ROBOT1.0_x",
        "author": {"user_openid": "U1", "member_role": "admin"},
        "group_openid": "G1",
    }

    def test_full_mode_at_tag_stripped(self):
        data = dict(self.GROUP_BASE,
                    content='<qqbot-at-user id="BOT" /> /管理')
        msg = parse_event("GROUP_MESSAGE_CREATE", data)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "/管理")
        self.assertEqual(msg.event_type, "GROUP_MESSAGE_CREATE")
        self.assertEqual(msg.session_key, "group:G1")
        self.assertEqual(msg.member_role, "admin")

    def test_at_mode_leading_space_stripped(self):
        data = dict(self.GROUP_BASE, content=" /今日天气 ")
        msg = parse_event("GROUP_AT_MESSAGE_CREATE", data)
        self.assertEqual(msg.content, "/今日天气")

    def test_plain_message_untouched(self):
        data = dict(self.GROUP_BASE, content="大家早上好呀")
        msg = parse_event("GROUP_MESSAGE_CREATE", data)
        self.assertEqual(msg.content, "大家早上好呀")

    def test_c2c_message_untouched(self):
        msg = parse_event("C2C_MESSAGE_CREATE", {
            "id": "ROBOT1.0_y",
            "author": {"user_openid": "U2"},
            "content": "/管理",
        })
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "/管理")
        self.assertEqual(msg.session_key, "c2c:U2")

    def test_unknown_event_type(self):
        self.assertIsNone(parse_event("SOMETHING_ELSE", {}))


if __name__ == "__main__":
    unittest.main()
