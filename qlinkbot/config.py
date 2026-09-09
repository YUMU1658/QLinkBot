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
    # 下载前向 B 站 CDN 发 Range 请求探测实际文件大小；失败自动回落估算值
    probe_real_size: bool = True
    parse_timeout_seconds: int = 300
    # yt-dlp 解析遇到暂时性错误（HTTP 412/429/5xx）时的重试次数；0 表示不重试
    error_retry_count: int = 3
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
class DouyinPlatformConfig:
    enabled: bool = True
    # 抖音匿名 cookies 文件（Netscape 格式）：yt-dlp 解析时经 --cookies 带上，
    # 用于通过 aweme/detail 接口的匿名风控；程序会在失效时尝试自动刷新，
    # 刷新失败时可手动用浏览器导出覆盖。留空则不使用 cookies 文件
    cookies_file: str = "data/douyin_cookies.txt"
    # 浏览器直取开关：经无头 Chromium 打开视频页拦截 detail 响应
    # （元数据 + 播放直链）。抖音 Argus 风控下纯 HTTP 必被 403，
    # 此为主链路；关闭则回落纯 yt-dlp（大概率报 Fresh cookies）
    browser_enabled: bool = True
    # 浏览器使用的代理（服务器直连抖音有问题时填，如 http://127.0.0.1:7897）；
    # 留空则直连。仅影响浏览器请求，不影响 yt-dlp/aiohttp
    browser_proxy: str = ""


@dataclass
class PlatformsConfig:
    bilibili: BilibiliPlatformConfig = field(default_factory=BilibiliPlatformConfig)
    douyin: DouyinPlatformConfig = field(default_factory=DouyinPlatformConfig)


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
                "error_retry_count",
                "rate_limit_count", "rate_limit_window_seconds"):
        if fld in limits:
            setattr(cfg.limits, fld, int(limits[fld]))
    if "probe_real_size" in limits:
        cfg.limits.probe_real_size = bool(limits["probe_real_size"])
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
    douyin = platforms.get("douyin", {})
    if "enabled" in douyin:
        cfg.platforms.douyin.enabled = bool(douyin["enabled"])
    if "cookies_file" in douyin:
        cfg.platforms.douyin.cookies_file = str(douyin["cookies_file"])
    if "browser_enabled" in douyin:
        cfg.platforms.douyin.browser_enabled = bool(douyin["browser_enabled"])
    if "browser_proxy" in douyin:
        cfg.platforms.douyin.browser_proxy = str(douyin["browser_proxy"])
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
