"""入口：加载配置并启动机器人。"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from .admin import register_admin_panel
from .api import QQApi
from .auth import TokenManager
from .config import load_config, validate_config
from .events import parse_event, parse_interaction
from .pipeline import Pipeline
from .ws import WsClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else "config.toml")
    cfg = load_config(config_path)
    validate_config(cfg)

    downloads_dir = Path("downloads")
    downloads_dir.mkdir(exist_ok=True)

    tokens = TokenManager(cfg.bot.appid, cfg.bot.secret)
    api = QQApi(tokens)
    await api.start()

    pipeline = Pipeline(cfg, api, downloads_dir)

    async def on_dispatch(event_type: str, data: dict) -> None:
        # 卡片按钮点击回调
        if event_type == "INTERACTION_CREATE":
            ev = parse_interaction(event_type, data)
            if ev is not None:
                await pipeline.admin.handle_interaction(ev)
            return
        msg = parse_event(event_type, data)
        if msg is None:
            log.debug("忽略未处理事件类型 %s", event_type)
            return
        await pipeline.handle_message(msg)

    ws = WsClient(api, tokens, on_dispatch)

    # 通过指令面板接口注册 /管理 指令（失败仅告警）
    await register_admin_panel(api, cfg)

    await pipeline.start()
    log.info("QLinkBot 已启动 (appid=%s)", cfg.bot.appid)
    try:
        await ws.run()
    finally:
        await pipeline.close()
        await api.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
