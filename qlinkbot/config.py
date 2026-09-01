"""配置加载。"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib


@dataclass
class BotConfig:
    appid: str = ""
    secret: str = ""


@dataclass
class LimitsConfig:
    max_file_size_mb: int = 30
    parse_timeout_seconds: int = 300
    rate_limit_count: int = 10
    rate_limit_window_seconds: int = 60


@dataclass
class CacheConfig:
    file_ttl_seconds: int = 600
    metadata_ttl_seconds: int = 1800
    duplicate_window_seconds: int = 600
    # 缓存定时清理间隔；<=0 表示禁用
    cleanup_interval_seconds: int = 60


@dataclass
class BehaviorConfig:
    report_errors: bool = False
    # 尝试将封面与文字合并在一条消息中发送；平台拒绝时自动拆为两条
    media_with_text: bool = True


@dataclass
class BilibiliPlatformConfig:
    enabled: bool = True


@dataclass
class PlatformsConfig:
    bilibili: BilibiliPlatformConfig = field(default_factory=BilibiliPlatformConfig)


@dataclass
class AdminConfig:
    # 管理指令文本（触发词，需与指令面板注册的名称一致）
    command: str = "/管理"
    # 启动时通过指令面板接口注册该指令
    register_panel: bool = True
    # 会话级解析开关的持久化文件；留空则仅存内存（重启丢失）
    session_store_path: str = "data/session_settings.json"


@dataclass
class Config:
    bot: BotConfig = field(default_factory=BotConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    platforms: PlatformsConfig = field(default_factory=PlatformsConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)

    @property
    def max_file_size_bytes(self) -> int:
        return self.limits.max_file_size_mb * 1024 * 1024


def load_config(path: str | Path) -> Config:
    path = Path(path)
    cfg = Config()
    if not path.is_file():
        return cfg
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    bot = raw.get("bot", {})
    cfg.bot.appid = str(bot.get("appid", ""))
    cfg.bot.secret = str(bot.get("secret", ""))
    limits = raw.get("limits", {})
    for fld in ("max_file_size_mb", "parse_timeout_seconds",
                "rate_limit_count", "rate_limit_window_seconds"):
        if fld in limits:
            setattr(cfg.limits, fld, int(limits[fld]))
    cache = raw.get("cache", {})
    for fld in ("file_ttl_seconds", "metadata_ttl_seconds",
                "duplicate_window_seconds", "cleanup_interval_seconds"):
        if fld in cache:
            setattr(cfg.cache, fld, int(cache[fld]))
    behavior = raw.get("behavior", {})
    if "report_errors" in behavior:
        cfg.behavior.report_errors = bool(behavior["report_errors"])
    if "media_with_text" in behavior:
        cfg.behavior.media_with_text = bool(behavior["media_with_text"])
    platforms = raw.get("platforms", {})
    bili = platforms.get("bilibili", {})
    if "enabled" in bili:
        cfg.platforms.bilibili.enabled = bool(bili["enabled"])
    admin = raw.get("admin", {})
    if "command" in admin:
        cfg.admin.command = str(admin["command"])
    if "register_panel" in admin:
        cfg.admin.register_panel = bool(admin["register_panel"])
    if "session_store_path" in admin:
        cfg.admin.session_store_path = str(admin["session_store_path"])
    return cfg


def validate_config(cfg: Config) -> None:
    if not cfg.bot.appid or not cfg.bot.secret:
        raise SystemExit("config.toml 中必须填写 [bot] appid 和 secret")
