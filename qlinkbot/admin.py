"""/管理 指令：markdown 按钮卡片菜单、会话级解析开关、指令面板注册。"""

from __future__ import annotations

import logging
from typing import Any

from .api import QQApi, QQApiError
from .config import Config
from .events import InboundMessage, InteractionEvent
from .sender import Sender
from .sessionconfig import (
    BiliOptions,
    DouyinOptions,
    PLATFORM_BILIBILI,
    PLATFORM_DOUYIN,
    SessionConfigStore,
)

log = logging.getLogger(__name__)

# 按钮回调 data 约定（平台原样回传，保持简短稳定，勿改动已发出的按钮）
DATA_MENU = "admin:menu"
DATA_BILI_SETTINGS = "admin:bili"
DATA_BILI_TOGGLE = "admin:bilitoggle:"
DATA_DOUYIN_SETTINGS = "admin:douyin"
DATA_DOUYIN_TOGGLE = "admin:douyintoggle:"

# 按钮文字（label 平台限制 10 字符内）
TOGGLE_BUTTON_LABELS = {
    "enabled": "B站解析",
    "cover": "发送封面",
    "title": "发送标题",
    "intro": "发送简介",
    "stats": "视频数据",
    "link": "原视频链接",
    "video": "发送视频",
}
# 设置卡片中可翻转的开关及排列顺序
TOGGLE_ORDER = ("enabled", "cover", "title", "intro", "stats", "link", "video")
# 抖音设置卡片中可翻转的开关（暂仅总开关）
DOUYIN_TOGGLE_ORDER = ("enabled",)

PANEL_DESC = "解析功能管理"

AT_USER_FMT = '<qqbot-at-user id="{openid}" />\n'


def _at_prefix(openid: str | None) -> str:
    return AT_USER_FMT.format(openid=openid) if openid else ""


def _button(label: str, data: str, admin_only: bool, style: int = 1) -> dict:
    """回调按钮；群聊卡片由平台侧限制仅管理员可点。"""
    return {
        "id": data.replace(":", "_"),
        "render_data": {"label": label, "visited_label": label,
                        "style": style},
        "action": {
            "type": 1,  # 回调按钮：点击触发 INTERACTION_CREATE
            "permission": {"type": 1 if admin_only else 2},
            "data": data,
        },
    }


def _menu_card(at_openid: str | None,
               admin_only: bool) -> tuple[str, dict]:
    text = (_at_prefix(at_openid)
            + "## 解析管理\n请选择要管理的解析平台：")
    buttons = [_button("bilibili 解析设置", DATA_BILI_SETTINGS, admin_only,
                       style=3),
               _button("douyin 解析设置", DATA_DOUYIN_SETTINGS, admin_only,
                       style=3)]
    return text, {"content": {"rows": [{"buttons": buttons}]}}


def _bili_settings_card(opts: BiliOptions,
                        admin_only: bool) -> tuple[str, dict]:
    text = "## bilibili 解析设置\n点击按钮切换对应开关："

    def toggle(key: str) -> dict:
        on = getattr(opts, key)
        label = ("✅ " if on else "⛔ ") + TOGGLE_BUTTON_LABELS[key]
        return _button(label, DATA_BILI_TOGGLE + key, admin_only,
                       style=3 if on else 0)

    rows = [
        {"buttons": [toggle("enabled")]},
        {"buttons": [toggle("cover"), toggle("title")]},
        {"buttons": [toggle("intro"), toggle("stats")]},
        {"buttons": [toggle("link"), toggle("video")]},
        {"buttons": [_button("↩ 返回主菜单", DATA_MENU, admin_only)]},
    ]
    return text, {"content": {"rows": rows}}


DOUYIN_TOGGLE_BUTTON_LABELS = {
    "enabled": "抖音解析",
}


def _douyin_settings_card(opts: DouyinOptions,
                          admin_only: bool) -> tuple[str, dict]:
    text = "## douyin 解析设置\n点击按钮切换对应开关："

    def toggle(key: str) -> dict:
        on = getattr(opts, key)
        label = ("✅ " if on else "⛔ ") + DOUYIN_TOGGLE_BUTTON_LABELS[key]
        return _button(label, DATA_DOUYIN_TOGGLE + key, admin_only,
                       style=3 if on else 0)

    rows = [
        {"buttons": [toggle("enabled")]},
        {"buttons": [_button("↩ 返回主菜单", DATA_MENU, admin_only)]},
    ]
    return text, {"content": {"rows": rows}}


class AdminService:
    """/管理 指令与卡片按钮回调的处理；会话配置经 SessionConfigStore 存储。"""

    def __init__(self, cfg: Config, api: QQApi, sender: Sender,
                 store: SessionConfigStore) -> None:
        self._cfg = cfg
        self._api = api
        self._sender = sender
        self._store = store
        # 群管理员缓存：group_openid -> {member_openid: role}，从群消息学习，
        # 用于按钮点击的纵深校验（interaction 事件本身不带角色）。
        self._group_roles: dict[str, dict[str, str]] = {}
        self._max_groups = 200

    # ---- 群管理员学习 ----

    def observe(self, msg: InboundMessage) -> None:
        if not msg.is_group or not msg.member_role or not msg.group_openid:
            return
        roles = self._group_roles.setdefault(msg.group_openid, {})
        roles[msg.user_openid] = msg.member_role
        if len(self._group_roles) > self._max_groups:
            self._group_roles.pop(next(iter(self._group_roles)), None)

    def _known_group_admin(self, group_openid: str,
                           member_openid: str) -> bool | None:
        """群成员是否管理员；缓存中未知返回 None（交由平台按钮权限拦截）。"""
        role = self._group_roles.get(group_openid, {}).get(member_openid)
        if role is None:
            return None
        return role in ("admin", "owner")

    # ---- /管理 指令 ----

    async def handle_command(self, msg: InboundMessage) -> None:
        # 群聊仅管理员/群主可用；私聊不受限制
        if msg.is_group and msg.member_role not in ("admin", "owner"):
            await self._deny_command(msg)
            return
        at = (msg.user_openid
              if msg.event_type == "GROUP_AT_MESSAGE_CREATE" else None)
        content, keyboard = _menu_card(at, admin_only=msg.is_group)
        try:
            await self._sender.send_markdown(msg, content, keyboard, seq=1)
        except QQApiError as e:
            log.warning("管理菜单发送失败: %s", e)

    async def _deny_command(self, msg: InboundMessage) -> None:
        if not self._cfg.behavior.report_errors:
            log.info("[%s] 非管理员尝试使用 %s，已静默忽略",
                     msg.session_key, self._cfg.admin.command)
            return
        text = (_at_prefix(msg.user_openid)
                if msg.event_type == "GROUP_AT_MESSAGE_CREATE" else "")
        try:
            await self._sender.reply_text(
                msg, text + "该指令仅限群管理员/群主使用", seq=1)
        except QQApiError as e:
            log.warning("权限提示发送失败: %s", e)

    # ---- 卡片按钮回调 ----

    async def handle_interaction(self, ev: InteractionEvent) -> None:
        data = ev.button_data.strip()
        is_bili_toggle = data.startswith(DATA_BILI_TOGGLE)
        is_douyin_toggle = data.startswith(DATA_DOUYIN_TOGGLE)
        if data not in (DATA_MENU, DATA_BILI_SETTINGS,
                        DATA_DOUYIN_SETTINGS) and not is_bili_toggle \
                and not is_douyin_toggle:
            log.info("未知的按钮回调数据，忽略: %r", data)
            await self._ack(ev, 0)
            return

        if ev.is_group:
            allowed = self._known_group_admin(ev.group_openid,
                                              ev.clicker_openid)
            if allowed is False:
                log.info("[%s] 非管理员点击管理按钮，拒绝", ev.session_key)
                # code=5 仅管理员操作；report_errors 关闭时静默确认
                await self._ack(
                    ev, 5 if self._cfg.behavior.report_errors else 0)
                return

        if is_bili_toggle:
            key = data[len(DATA_BILI_TOGGLE):]
            if key not in TOGGLE_ORDER:
                await self._ack(ev, 0)
                return
            current = self._store.get(ev.session_key, PLATFORM_BILIBILI)
            opts = self._store.update(ev.session_key, PLATFORM_BILIBILI,
                                      **{key: not getattr(current, key)})
            log.info("[%s] bilibili.%s -> %s",
                     ev.session_key, key, getattr(opts, key))
            content, keyboard = _bili_settings_card(opts,
                                                    admin_only=ev.is_group)
        elif is_douyin_toggle:
            key = data[len(DATA_DOUYIN_TOGGLE):]
            if key not in DOUYIN_TOGGLE_ORDER:
                await self._ack(ev, 0)
                return
            current = self._store.get(ev.session_key, PLATFORM_DOUYIN)
            opts = self._store.update(ev.session_key, PLATFORM_DOUYIN,
                                      **{key: not getattr(current, key)})
            log.info("[%s] douyin.%s -> %s",
                     ev.session_key, key, getattr(opts, key))
            content, keyboard = _douyin_settings_card(opts,
                                                      admin_only=ev.is_group)
        elif data == DATA_BILI_SETTINGS:
            opts = self._store.get(ev.session_key, PLATFORM_BILIBILI)
            content, keyboard = _bili_settings_card(opts,
                                                    admin_only=ev.is_group)
        elif data == DATA_DOUYIN_SETTINGS:
            opts = self._store.get(ev.session_key, PLATFORM_DOUYIN)
            content, keyboard = _douyin_settings_card(opts,
                                                      admin_only=ev.is_group)
        else:
            content, keyboard = _menu_card(
                ev.clicker_openid if ev.is_group else None,
                admin_only=ev.is_group)

        await self._ack(ev, 0)
        try:
            # 事件锚定被动回复的 event_id 须带事件类型前缀，
            # 裸 interaction_id 会被平台判 40034025 event_id 无效
            event_id = f"INTERACTION_CREATE:{ev.interaction_id}"
            await self._sender.send_markdown_event(
                ev.is_group,
                ev.group_openid if ev.is_group else ev.user_openid,
                event_id, content, keyboard, seq=1)
        except QQApiError as e:
            log.warning("管理卡片回复失败: %s", e)

    async def _ack(self, ev: InteractionEvent, code: int) -> None:
        if not ev.interaction_id:
            return
        try:
            await self._api.put_interaction(ev.interaction_id, code)
        except Exception as e:
            log.debug("互动回应失败 %s: %s", ev.interaction_id, e)


# ---- 指令面板注册 ----

def _panel_has_command(panels: Any, name: str) -> bool:
    if isinstance(panels, dict):
        panels = panels.get("items") or panels.get("data") or []
    if not isinstance(panels, list):
        return False
    for panel in panels:
        if not isinstance(panel, dict):
            continue
        container = panel.get("panel") if isinstance(panel.get("panel"),
                                                     dict) else panel
        items = container.get("items") or []
        for item in items:
            if (isinstance(item, dict) and item.get("type") == "command"
                    and item.get("name") == name):
                return True
    return False


async def register_admin_panel(api: QQApi, cfg: Config) -> None:
    """通过指令面板接口注册管理指令；失败仅告警，不影响运行。"""
    if not cfg.admin.register_panel:
        return
    name = cfg.admin.command
    for scope in ("group", "c2c"):
        try:
            existing = await api.list_panels(scope)
            if _panel_has_command(existing, name):
                log.info("指令面板(%s) 已存在 %s，跳过注册", scope, name)
                continue
            await api.create_panel({
                "scope": scope,
                "target_type": "all",
                "panel": {"items": [{"type": "command", "name": name,
                                     "desc": PANEL_DESC}]},
            })
            log.info("指令面板(%s) 注册成功：%s", scope, name)
        except Exception as e:
            log.warning("指令面板(%s) 注册失败（不影响运行）: %s", scope, e)
