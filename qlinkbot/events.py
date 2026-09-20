"""事件归一化：把 C2C / 群@ / 群全量消息统一为 InboundMessage。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 消息开头 @ 段的三种已知形态：
# - <qqbot-at-user id="..." />：v2 富文本 at 段（本项目发送侧即此格式）
# - <@!id> / <@id>：频道风格 at 标签
# - @昵称（含全角＠）：纯文本 at
_AT_SEGMENT_RE = re.compile(
    r"(?:<qqbot-at-(?:user|everyone)\b[^>]*>"
    r"|<@!?[^>\s]+>"
    r"|[＠@]\S+)")


def strip_at_prefix(content: str) -> str:
    """循环剥离开头的 @ 段与前导空白；只处理消息开头，不碰中部文本。"""
    text = (content or "").lstrip()
    while True:
        m = _AT_SEGMENT_RE.match(text)
        if not m:
            return text.strip()
        text = text[m.end():].lstrip()


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
    # 群消息发送者角色：member/admin/owner；私聊为空
    member_role: str = ""


@dataclass(frozen=True)
class InteractionEvent:
    """卡片按钮点击回调（INTERACTION_CREATE，type=11）。"""
    interaction_id: str
    session_key: str
    user_openid: str      # 私聊场景即点击者
    group_openid: str | None
    clicker_openid: str   # 点击者：群聊为 group_member_openid，私聊为 user_openid
    is_group: bool
    button_id: str
    button_data: str


def parse_event(event_type: str, data: dict) -> InboundMessage | None:
    if event_type not in ("C2C_MESSAGE_CREATE",
                          "GROUP_AT_MESSAGE_CREATE",
                          "GROUP_MESSAGE_CREATE"):
        return None
    author = data.get("author") or {}
    user_openid = (author.get("user_openid")
                   or author.get("member_openid")
                   or author.get("id")
                   or author.get("openid")
                   or "")
    group_openid = data.get("group_openid")
    if event_type == "C2C_MESSAGE_CREATE":
        session_key = f"c2c:{user_openid}"
    else:
        if not group_openid:
            log.warning("%s 缺少 group_openid: %s", event_type, data)
            return None
        session_key = f"group:{group_openid}"
    # 全量消息模式下 @机器人 的消息 content 会保留 @ 段（与文档不符，
    # 以 GROUP_MESSAGE_CREATE 推送），统一剥掉再交给下游
    raw = str(data.get("content", ""))
    content = strip_at_prefix(raw)
    if content != raw.strip():
        log.info("剥离 @ 前缀 (%s): %r -> %r", event_type, raw, content)
    return InboundMessage(
        event_type=event_type,
        message_id=str(data.get("id", "")),
        content=content,
        session_key=session_key,
        user_openid=user_openid,
        group_openid=group_openid,
        is_group=(event_type != "C2C_MESSAGE_CREATE"),
        member_role=str(author.get("member_role", "") or ""),
    )


def parse_interaction(event_type: str, data: dict) -> InteractionEvent | None:
    if event_type != "INTERACTION_CREATE":
        return None
    # type=11 为消息按钮回调，其余互动场景暂不处理
    if data.get("type") not in (11, "11"):
        return None
    resolved = (data.get("data") or {}).get("resolved") or {}
    group_openid = data.get("group_openid")
    user_openid = data.get("user_openid") or ""
    chat_type = data.get("chat_type")  # 1=群聊 2=单聊
    is_group = bool(group_openid) and (user_openid == "" or chat_type == 1)
    if is_group:
        if not group_openid:
            log.warning("INTERACTION_CREATE 群场景缺少 group_openid: %s", data)
            return None
        clicker = (data.get("group_member_openid")
                   or data.get("user_openid") or "")
        session_key = f"group:{group_openid}"
    else:
        if not user_openid:
            log.warning("INTERACTION_CREATE 缺少 user_openid: %s", data)
            return None
        clicker = user_openid
        session_key = f"c2c:{user_openid}"
    return InteractionEvent(
        interaction_id=str(data.get("id", "")),
        session_key=session_key,
        user_openid=clicker,
        group_openid=group_openid,
        clicker_openid=clicker,
        is_group=is_group,
        button_id=str(resolved.get("button_id", "") or ""),
        button_data=str(resolved.get("button_data", "") or ""),
    )
