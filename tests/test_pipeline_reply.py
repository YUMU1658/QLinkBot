"""测试视频解析结果回复文本组装，确保在群聊 @机器人、全量群聊及私聊下均无多余的 at 标签。"""

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from qlinkbot.bilibili import VideoMeta
from qlinkbot.config import Config
from qlinkbot.events import InboundMessage
from qlinkbot.pipeline import Pipeline
from qlinkbot.sessionconfig import BiliOptions


class PipelineReplyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = MagicMock()
        self.sender = MagicMock()
        self.sender.reply_cover_with_text = AsyncMock()
        self.sender.reply_text = AsyncMock()
        self.sender.send_cover = AsyncMock()
        self.sender.send_video = AsyncMock()

        self.cfg = Config()
        self.pipeline = Pipeline(
            cfg=self.cfg,
            api=self.api,
            downloads_dir=Path("./downloads"),
        )
        self.pipeline._sender = self.sender
        self.meta = VideoMeta(
            bvid="BV1xx411c7mD",
            title="测试视频标题",
            uploader="测试UP主",
            description="视频简介内容",
            cover="",
            views=1000,
            danmaku=10,
            likes=100,
            coins=50,
            favorites=80,
            comments=20,
        )

    async def test_group_at_message_reply_has_no_at_tag(self):
        msg = InboundMessage(
            event_type="GROUP_AT_MESSAGE_CREATE",
            message_id="MSG_1",
            content="BV1xx411c7mD",
            session_key="group:G1",
            user_openid="U1",
            group_openid="G1",
            is_group=True,
        )
        await self.pipeline._send_result(
            msg, "bilibili", self.meta, Path("dummy.mp4"), "BV1xx411c7mD", BiliOptions()
        )

        # 验证发送的文本没有 <qqbot-at-user 前缀
        self.sender.reply_text.assert_awaited()
        sent_text = self.sender.reply_text.call_args[0][1]
        self.assertNotIn("<qqbot-at-user", sent_text)
        self.assertIn("测试视频标题", sent_text)
        self.assertIn("UP：测试UP主", sent_text)

    async def test_group_full_mode_message_reply_has_no_at_tag(self):
        msg = InboundMessage(
            event_type="GROUP_MESSAGE_CREATE",
            message_id="MSG_2",
            content="BV1xx411c7mD",
            session_key="group:G1",
            user_openid="U1",
            group_openid="G1",
            is_group=True,
        )
        await self.pipeline._send_result(
            msg, "bilibili", self.meta, Path("dummy.mp4"), "BV1xx411c7mD", BiliOptions()
        )

        self.sender.reply_text.assert_awaited()
        sent_text = self.sender.reply_text.call_args[0][1]
        self.assertNotIn("<qqbot-at-user", sent_text)
        self.assertIn("测试视频标题", sent_text)

    async def test_c2c_message_reply_has_no_at_tag(self):
        msg = InboundMessage(
            event_type="C2C_MESSAGE_CREATE",
            message_id="MSG_3",
            content="BV1xx411c7mD",
            session_key="c2c:U1",
            user_openid="U1",
            group_openid=None,
            is_group=False,
        )
        await self.pipeline._send_result(
            msg, "bilibili", self.meta, Path("dummy.mp4"), "BV1xx411c7mD", BiliOptions()
        )

        self.sender.reply_text.assert_awaited()
        sent_text = self.sender.reply_text.call_args[0][1]
        self.assertNotIn("<qqbot-at-user", sent_text)
        self.assertIn("测试视频标题", sent_text)


if __name__ == "__main__":
    unittest.main()
