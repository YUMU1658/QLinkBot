"""会话级解析配置：按会话（群/私聊）× 平台分层存储各解析器的开关。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# 平台标识：存储文件中 session_key 下的第一层键，各平台配置互不影响
PLATFORM_BILIBILI = "bilibili"
PLATFORM_DOUYIN = "douyin"


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


@dataclass
class DouyinOptions:
    """douyin 解析在单个会话内的输出开关，默认开启；暂仅总开关。"""
    enabled: bool = True   # 会话内 douyin 解析总开关（关闭则完全禁用）


# 平台 -> 该平台的会话配置 dataclass；新增平台时在此注册即可
_OPTIONS_TYPES: dict[str, type] = {
    PLATFORM_BILIBILI: BiliOptions,
    PLATFORM_DOUYIN: DouyinOptions,
}


class SessionConfigStore:
    """session_key -> 平台 -> 该平台配置；配置了持久化路径时变更即落盘。

    session_key 由 events 层生成：群聊 "group:{group_openid}"，
    私聊 "c2c:{user_openid}"，天然会话隔离。
    持久化结构为 session_key 下按平台名分层：
    {"group:XXX": {"bilibili": {"enabled": true, ...}}}
    """

    def __init__(self, store_path: str | Path = "") -> None:
        self._path = Path(store_path) if store_path else None
        self._data: dict[str, dict[str, Any]] = {}
        if self._path is not None:
            self._load()

    def get(self, session_key: str, platform: str) -> Any:
        """取会话内指定平台的配置；未设置过时返回该平台默认值。"""
        opts = self._data.get(session_key, {}).get(platform)
        if opts is None:
            opts = _OPTIONS_TYPES[platform]()
            self._data.setdefault(session_key, {})[platform] = opts
        return opts

    def update(self, session_key: str, platform: str,
               **changes: bool) -> Any:
        opts = self.get(session_key, platform)
        valid = {f.name for f in fields(opts)}
        for key, value in changes.items():
            if key in valid:
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
        for session_key, platforms in raw.items():
            if not isinstance(platforms, dict):
                continue
            for platform, item in platforms.items():
                opts_type = _OPTIONS_TYPES.get(platform)
                if opts_type is None or not isinstance(item, dict):
                    continue
                valid = {f.name for f in fields(opts_type)}
                kwargs = {k: bool(v) for k, v in item.items()
                          if k in valid}
                self._data.setdefault(session_key, {})[platform] = \
                    opts_type(**kwargs)
        log.info("已加载 %d 个会话的解析配置 (%s)",
                 len(self._data), self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                session_key: {platform: asdict(opts)
                              for platform, opts in platforms.items()}
                for session_key, platforms in self._data.items()
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("会话配置保存失败 %s: %s", self._path, e)
