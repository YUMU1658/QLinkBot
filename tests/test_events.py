"""events 归一化测试：@ 前缀剥离（全量消息模式 @机器人 消息修复）。"""

import unittest

from qlinkbot.events import parse_event, parse_interaction, strip_at_prefix


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

    def test_group_author_with_member_openid(self):
        data = {
            "id": "ROBOT1.0_x",
            "author": {
                "id": "ID_123",
                "member_openid": "MEMBER_456",
                "member_role": "member",
            },
            "group_openid": "G1",
            "content": "测试",
        }
        msg = parse_event("GROUP_AT_MESSAGE_CREATE", data)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.user_openid, "MEMBER_456")

    def test_group_author_with_id_only(self):
        data = {
            "id": "ROBOT1.0_x",
            "author": {
                "id": "ID_123",
                "member_role": "member",
            },
            "group_openid": "G1",
            "content": "测试",
        }
        msg = parse_event("GROUP_AT_MESSAGE_CREATE", data)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.user_openid, "ID_123")

    def test_unknown_event_type(self):
        self.assertIsNone(parse_event("SOMETHING_ELSE", {}))


class ParseInteractionTest(unittest.TestCase):
    def test_group_interaction_with_member_openid(self):
        data = {
            "id": "int_1",
            "type": 11,
            "group_openid": "G1",
            "group_member_openid": "M1",
            "data": {
                "resolved": {
                    "button_id": "b1",
                    "button_data": "admin:bili",
                }
            }
        }
        ev = parse_interaction("INTERACTION_CREATE", data)
        self.assertIsNotNone(ev)
        self.assertTrue(ev.is_group)
        self.assertEqual(ev.clicker_openid, "M1")
        self.assertEqual(ev.group_openid, "G1")
        self.assertEqual(ev.button_data, "admin:bili")

    def test_group_interaction_with_user_openid_fallback(self):
        data = {
            "id": "int_2",
            "type": 11,
            "chat_type": 1,
            "group_openid": "G1",
            "user_openid": "U_FALLBACK",
            "data": {
                "resolved": {
                    "button_id": "b2",
                    "button_data": "admin:menu",
                }
            }
        }
        ev = parse_interaction("INTERACTION_CREATE", data)
        self.assertIsNotNone(ev)
        self.assertTrue(ev.is_group)
        self.assertEqual(ev.clicker_openid, "U_FALLBACK")

    def test_c2c_interaction(self):
        data = {
            "id": "int_3",
            "type": 11,
            "user_openid": "U1",
            "data": {
                "resolved": {
                    "button_id": "b3",
                    "button_data": "admin:douyin",
                }
            }
        }
        ev = parse_interaction("INTERACTION_CREATE", data)
        self.assertIsNotNone(ev)
        self.assertFalse(ev.is_group)
        self.assertEqual(ev.clicker_openid, "U1")
        self.assertIsNone(ev.group_openid)

    def test_ignore_non_button_interaction(self):
        data = {
            "id": "int_4",
            "type": 13,
        }
        self.assertIsNone(parse_interaction("INTERACTION_CREATE", data))


if __name__ == "__main__":
    unittest.main()
