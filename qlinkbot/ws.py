"""WebSocket Gateway 客户端。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Awaitable, Callable

import aiohttp

from .auth import TokenManager
from .api import QQApi

log = logging.getLogger(__name__)

INTENT_GROUP_AND_C2C_EVENT = 1 << 25
INTENT_INTERACTION = 1 << 26
# 群/C2C 消息 + 卡片按钮点击等互动回调
INTENTS = INTENT_GROUP_AND_C2C_EVENT | INTENT_INTERACTION

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11


class WsClient:
    """负责 gateway 连接、鉴权、心跳、断线恢复；事件通过回调抛出。"""

    def __init__(self, api: QQApi, tokens: TokenManager,
                 on_dispatch: Callable[[str, dict], Awaitable[None]]) -> None:
        self._api = api
        self._tokens = tokens
        self._on_dispatch = on_dispatch
        self._session_id: str | None = None
        self._last_seq: int | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._closing = False

    async def run(self) -> None:
        while not self._closing:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WS 连接异常退出: %s", e)
            if self._closing:
                break
            delay = 3 + random.random() * 3
            log.info("%.1f 秒后重连", delay)
            await asyncio.sleep(delay)

    async def close(self) -> None:
        self._closing = True
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def _run_once(self) -> None:
        url = await self._api.get_gateway()
        log.info("连接 Gateway: %s", url)
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(url) as ws:
                self._ws = ws
                hello_raw = await ws.receive(timeout=30)
                hello = json.loads(hello_raw.data)
                if hello.get("op") != OP_HELLO:
                    raise RuntimeError(f"预期 Hello(op=10)，实际: {hello}")
                heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0

                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(ws, heartbeat_interval))
                try:
                    if self._session_id is not None and self._last_seq is not None:
                        await self._send_resume(ws)
                    else:
                        await self._send_identify(ws)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            if not await self._handle_payload(ws, payload):
                                return  # op=9 invalid session，需重新 Identify
                        elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            raise ConnectionError("WS 已关闭")
                finally:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
                self._ws = None

    async def _send_identify(self, ws) -> None:
        token = await self._tokens.get(self._api.session)
        await ws.send_json({
            "op": OP_IDENTIFY,
            "d": {
                "token": f"QQBot {token}",
                "intents": INTENTS,
                "shard": [0, 1],
                "properties": {},
            },
        })

    async def _send_resume(self, ws) -> None:
        token = await self._tokens.get(self._api.session)
        await ws.send_json({
            "op": OP_RESUME,
            "d": {
                "token": f"QQBot {token}",
                "session_id": self._session_id,
                "seq": self._last_seq,
            },
        })

    async def _handle_payload(self, ws, payload: dict) -> bool:
        op = payload.get("op")
        if op == OP_DISPATCH:
            self._last_seq = payload.get("s", self._last_seq)
            t = payload.get("t")
            d = payload.get("d") or {}
            if t == "READY":
                self._session_id = d.get("session_id")
                log.info("WS READY, session=%s", self._session_id)
            else:
                try:
                    await self._on_dispatch(t, d)
                except Exception:
                    log.exception("处理事件 %s 出错", t)
            return True
        if op == OP_HEARTBEAT_ACK:
            return True
        if op == OP_RECONNECT:
            log.info("服务端要求重连(op=7)")
            return False
        if op == OP_INVALID_SESSION:
            log.info("会话失效(op=9)，重置会话")
            self._session_id = None
            self._last_seq = None
            return False
        return True

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        # 首次心跳前随机抖动，避免多实例同步
        await asyncio.sleep(interval * random.random())
        while True:
            await ws.send_json({"op": OP_HEARTBEAT,
                                "d": self._last_seq})
            await asyncio.sleep(interval)
