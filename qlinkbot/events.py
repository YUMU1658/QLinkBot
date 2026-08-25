"""事件归一化：把 C2C / 群@ / 群全量消息统一为 InboundMessage。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    event_type: str
    message_id: str
    content: str
    # 会话隔离键：群用 group_openid，私聊用 user_openid
    session_key: str
    user_openid: str
    group_openid: str | None
    is_group: bool


def parse_event(event_type: str, data: dict) -> InboundMessage | None:
    if event_type not in ("C2C_MESSAGE_CREATE",
                          "GROUP_AT_MESSAGE_CREATE",
                          "GROUP_MESSAGE_CREATE"):
        return None
    author = data.get("author") or {}
    user_openid = author.get("user_openid") or author.get("openid") or ""
    group_openid = data.get("group_openid")
    if event_type == "C2C_MESSAGE_CREATE":
        session_key = f"c2c:{user_openid}"
    else:
        if not group_openid:
            log.warning("%s 缺少 group_openid: %s", event_type, data)
            return None
        session_key = f"group:{group_openid}"
    return InboundMessage(
        event_type=event_type,
        message_id=str(data.get("id", "")),
        content=str(data.get("content", "")),
        session_key=session_key,
        user_openid=user_openid,
        group_openid=group_openid,
        is_group=(event_type != "C2C_MESSAGE_CREATE"),
    )
