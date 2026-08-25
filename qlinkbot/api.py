"""QQ 开放平台 REST API 封装。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from .auth import TokenManager

log = logging.getLogger(__name__)

API_BASE = "https://api.bot.qq.com"

RETRY_STATUS = {429, 500, 502, 503, 504}


class QQApiError(Exception):
    def __init__(self, status: int, data: Any,
                 path: str | None = None) -> None:
        super().__init__(f"HTTP {status}{f' [{path}]' if path else ''}: {data}")
        self.status = status
        self.path = path


class QQApi:
    def __init__(self, tokens: TokenManager) -> None:
        self._tokens = tokens
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "QQApi 尚未 start()"
        return self._session

    async def _request(self, method: str, path: str,
                       json_body: dict | None = None,
                       retry: int = 2) -> Any:
        return await self.request(method, path, json_body, retry)

    async def request(self, method: str, path: str,
                      json_body: dict | None = None,
                      retry: int = 2) -> Any:
        """公开的通用请求入口：自动携带 token，401 强刷重试，429/5xx 退避。"""
        url = API_BASE + path
        last_exc: Exception | None = None
        for attempt in range(retry + 1):
            token = await self._tokens.get(self.session)
            headers = {
                "Authorization": f"QQBot {token}",
                "Content-Type": "application/json",
            }
            try:
                async with self.session.request(
                        method, url, json=json_body, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 401 and attempt < retry:
                        # token 失效：强制刷新后重试一次
                        await self._tokens.invalidate_and_get(self.session)
                        continue
                    if resp.status >= 400:
                        exc = QQApiError(resp.status, data, path)
                        if resp.status in RETRY_STATUS and attempt < retry:
                            log.warning("%s %s 返回 %s，准备重试: %s",
                                        method, path, resp.status, data)
                            last_exc = exc
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        raise exc
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                if attempt < retry:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_exc  # pragma: no cover

    # ---- 发送消息 ----

    async def send_user_message(self, user_openid: str,
                                body: dict) -> Any:
        return await self._request(
            "POST", f"/v2/users/{user_openid}/messages", body)

    async def send_group_message(self, group_openid: str,
                                 body: dict) -> Any:
        return await self._request(
            "POST", f"/v2/groups/{group_openid}/messages", body)

    # ---- 富媒体上传 ----

    async def upload_user_media(self, user_openid: str, file_type: int,
                                file_info_payload: dict) -> Any:
        return await self._request(
            "POST", f"/v2/users/{user_openid}/files", file_info_payload)

    async def upload_group_media(self, group_openid: str, file_type: int,
                                 file_info_payload: dict) -> Any:
        return await self._request(
            "POST", f"/v2/groups/{group_openid}/files", file_info_payload)

    # ---- Gateway ----

    async def get_gateway(self) -> str:
        data = await self._request("GET", "/gateway")
        return data["url"]
