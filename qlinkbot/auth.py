"""Access Token 管理。"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
# 提前刷新余量（秒）
REFRESH_MARGIN = 120


class TokenManager:
    def __init__(self, appid: str, secret: str) -> None:
        self._appid = appid
        self._secret = secret
        self._token: str = ""
        self._expire_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self, session: aiohttp.ClientSession) -> str:
        if self._token and time.monotonic() < self._expire_at - REFRESH_MARGIN:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expire_at - REFRESH_MARGIN:
                return self._token
            await self._refresh(session)
            return self._token

    async def invalidate_and_get(self, session: aiohttp.ClientSession) -> str:
        async with self._lock:
            self._token = ""
            self._expire_at = 0.0
            await self._refresh(session)
            return self._token

    async def _refresh(self, session: aiohttp.ClientSession) -> None:
        payload = {"appId": self._appid, "clientSecret": self._secret}
        for attempt in range(3):
            try:
                async with session.post(TOKEN_URL, json=payload,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json(content_type=None)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("获取 access_token 失败(第%d次): %s", attempt + 1, e)
                if attempt == 2:
                    raise
                await asyncio.sleep(2 ** attempt)
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        expires_in = int(data.get("expires_in", 7200))
        self._token = token
        self._expire_at = time.monotonic() + expires_in
        log.info("access_token 已刷新，有效期 %d 秒", expires_in)
