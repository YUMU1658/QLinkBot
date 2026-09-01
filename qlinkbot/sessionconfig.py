"""会话级解析配置：按会话（群/私聊）独立存储各解析器的开关。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class BiliOptions:
    """bilibili 解析在单个会话内的输出开关，默认全部开启。"""
    enabled: bool = True   # 会话内 bilibili 解析总开关（关闭则完全禁用）
    cover: bool = True     # 发送封面
    title: bool = True     # 发送标题（含 UP 主名称）
    intro: bool = True     # 发送简介
    stats: bool = True     # 发送视频数据（播放/弹幕/点赞/收藏/投币/评论）
    link: bool = True      # 发送原视频链接
    video: bool = True     # 发送视频文件


_FIELD_NAMES = tuple(f.name for f in fields(BiliOptions))


class SessionConfigStore:
    """session_key -> 各解析器配置；配置了持久化路径时变更即落盘。

    session_key 由 events 层生成：群聊 "group:{group_openid}"，
    私聊 "c2c:{user_openid}"，天然会话隔离。
    """

    def __init__(self, store_path: str | Path = "") -> None:
        self._path = Path(store_path) if store_path else None
        self._data: dict[str, BiliOptions] = {}
        if self._path is not None:
            self._load()

    def get(self, session_key: str) -> BiliOptions:
        """取会话配置；未设置过的会话返回全开默认值。"""
        opts = self._data.get(session_key)
        if opts is None:
            opts = BiliOptions()
            self._data[session_key] = opts
        return opts

    def update(self, session_key: str, **changes: bool) -> BiliOptions:
        opts = self.get(session_key)
        for key, value in changes.items():
            if key in _FIELD_NAMES:
                setattr(opts, key, bool(value))
        if self._path is not None:
            self._save()
        return opts

    # ---- 持久化 ----

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            log.warning("会话配置加载失败 %s: %s", self._path, e)
            return
        if not isinstance(raw, dict):
            return
        for session_key, item in raw.items():
            if not isinstance(item, dict):
                continue
            kwargs = {k: bool(v) for k, v in item.items()
                      if k in _FIELD_NAMES}
            self._data[session_key] = BiliOptions(**kwargs)
        log.info("已加载 %d 个会话的解析配置 (%s)",
                 len(self._data), self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {k: asdict(v) for k, v in self._data.items()}
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("会话配置保存失败 %s: %s", self._path, e)
