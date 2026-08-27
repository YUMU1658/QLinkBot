"""发送实现：纯文本回复、封面图片/视频富媒体分片上传、被动回复。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import aiohttp

from .api import QQApi, QQApiError
from .bilibili import VideoMeta
from .events import InboundMessage

log = logging.getLogger(__name__)

FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2

REPLY_TEMPLATE = """{title}
UP：{uploader}

播放 {views}　弹幕 {danmaku}　点赞 {likes}
收藏 {favorites}　投币 {coins}　评论 {comments}

{description}

原视频：https://www.bilibili.com/video/{bvid}"""

DESC_MAX = 100


def _fmt_count(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if n >= 10_000:
        return f"{n / 10_000:.1f}万"
    return str(n)


def _truncate_desc(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > DESC_MAX:
        return text[:DESC_MAX] + "…"
    return text or "（无简介）"


def build_text_reply(meta: VideoMeta) -> str:
    return REPLY_TEMPLATE.format(
        title=meta.title,
        uploader=meta.uploader,
        views=_fmt_count(meta.views),
        danmaku=_fmt_count(meta.danmaku),
        likes=_fmt_count(meta.likes),
        favorites=_fmt_count(meta.favorites),
        coins=_fmt_count(meta.coins),
        comments=_fmt_count(meta.comments),
        description=_truncate_desc(meta.description),
        bvid=meta.bvid,
    )


class Sender:
    def __init__(self, api: QQApi) -> None:
        self._api = api

    async def reply_text(self, msg: InboundMessage, content: str,
                         seq: int) -> dict:
        body = self._base_body(seq)
        body["msg_type"] = 0
        body["content"] = content
        return await self._send(msg, body)

    async def send_cover(self, msg: InboundMessage, cover_path: Path,
                         seq: int) -> dict:
        """上传本地图片并作为被动回复单独发送。"""
        media = await self._prepare_media(msg, cover_path, FILE_TYPE_IMAGE)
        return await self._send_media(msg, media, seq)

    async def reply_cover_with_text(self, msg: InboundMessage,
                                    cover_path: Path, text: str,
                                    seq: int) -> dict:
        """图文同条：media 消息附带 content 文本；平台不支持时由调用方降级。"""
        media = await self._prepare_media(msg, cover_path, FILE_TYPE_IMAGE)
        body = self._base_body(seq)
        body["msg_type"] = 7
        body["media"] = media
        body["content"] = text
        return await self._send(msg, body)

    async def send_video(self, msg: InboundMessage, file_path: Path,
                         seq: int, file_name: str | None = None) -> dict:
        """上传本地视频并作为被动回复发送。"""
        media = await self._prepare_media(msg, file_path, FILE_TYPE_VIDEO,
                                          file_name)
        return await self._send_media(msg, media, seq)

    @staticmethod
    def _base_body(seq: int) -> dict:
        # 被动回复：msg_seq 随多条回复递增（由调用方统一编号）
        return {"msg_id": "", "msg_seq": seq}

    async def _prepare_media(self, msg: InboundMessage, file_path: Path,
                             file_type: int,
                             file_name: str | None = None) -> dict:
        endpoint_id = (msg.group_openid if msg.is_group
                       else msg.user_openid)
        return await self._upload_media(endpoint_id, msg.is_group,
                                        file_path, file_type, file_name)

    async def _send_media(self, msg: InboundMessage, media: dict,
                          seq: int) -> dict:
        body = self._base_body(seq)
        body["msg_type"] = 7
        body["media"] = media
        return await self._send(msg, body)

    def _with_msg_id(self, msg: InboundMessage, body: dict) -> dict:
        if msg.message_id:
            body["msg_id"] = msg.message_id
        else:
            body.pop("msg_id", None)
        return body

    async def _send(self, msg: InboundMessage, body: dict) -> dict:
        self._with_msg_id(msg, body)
        if msg.is_group:
            return await self._api.send_group_message(msg.group_openid, body)
        return await self._api.send_user_message(msg.user_openid, body)

    # ---- 富媒体分片上传 ----

    async def _upload_media(self, endpoint_id: str, is_group: bool,
                            file_path: Path, file_type: int,
                            file_name: str | None) -> dict:
        data = file_path.read_bytes()
        size = len(data)
        name = file_name or file_path.name or "file"
        md5 = hashlib.md5(data).hexdigest()
        sha1 = hashlib.sha1(data).hexdigest()
        md5_10m = hashlib.md5(data[:10_002_432]).hexdigest()

        prepare_body = {
            "file_type": file_type,
            "file_size": str(size),
            "file_name": name,
            "md5": md5,
            "sha1": sha1,
            "md5_10m": md5_10m,
        }
        prepare = await self._files_call(endpoint_id, is_group,
                                         "/upload_prepare", prepare_body)

        upload_id = prepare.get("upload_id")
        parts = prepare.get("parts") or []
        block_size = int(prepare.get("block_size") or 0)
        if not upload_id or not parts:
            raise QQApiError(0, f"upload_prepare 响应异常: {prepare}")

        concurrency = int((prepare.get("upload_config") or {})
                          .get("concurrency") or 3)
        await self._put_parts(parts, data, block_size, concurrency)

        for part in parts:
            finish_body = {"upload_id": upload_id,
                           "part_index": part.get("index")}
            await self._files_call(endpoint_id, is_group,
                                   "/upload_part_finish", finish_body)

        merge_body = {"file_type": file_type,
                      "upload_id": upload_id,
                      "srv_send_msg": False}
        result = await self._files_call(endpoint_id, is_group, "/files",
                                        merge_body)
        file_info = result.get("file_info")
        if not file_info:
            raise QQApiError(0, f"上传合并未返回 file_info: {result}")
        return {"file_info": file_info}

    async def _files_call(self, endpoint_id: str, is_group: bool,
                          suffix: str, body: dict) -> dict:
        # suffix 为完整后缀："/files"（合并/URL上传）、
        # "/upload_prepare"、"/upload_part_finish"
        path = (f"/v2/groups/{endpoint_id}" if is_group
                else f"/v2/users/{endpoint_id}") + suffix
        return await self._api.request("POST", path, body)

    async def _put_parts(self, parts: list[dict], data: bytes,
                         block_size: int, concurrency: int) -> None:
        sem = asyncio.Semaphore(max(concurrency, 1))

        # 按 index 排序并用累计偏移切分，兼容非均匀 block_size
        ordered = sorted(parts, key=lambda p: int(p.get("index") or 0))
        chunks: list[tuple[int, bytes]] = []
        offset = 0
        for part in ordered:
            size = int(part.get("block_size") or block_size)
            chunk = data[offset:offset + size] if size else b""
            chunks.append((int(part.get("index") or 0), chunk))
            offset += len(chunk)

        async def put_one(index: int, url: str, chunk: bytes) -> None:
            async with sem:
                for attempt in range(3):
                    try:
                        async with self._api.session.put(
                                url, data=chunk,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
                            resp.release()
                            if resp.status < 400:
                                return
                            log.warning("分片 %d PUT 返回 %d", index, resp.status)
                    except (aiohttp.ClientError, TimeoutError) as e:
                        log.warning("分片 %d PUT 异常: %s", index, e)
                    await asyncio.sleep(1.5 * (attempt + 1))
                raise QQApiError(0, f"分片 {index} 上传失败")

        await asyncio.gather(
            *(put_one(idx, part.get("presigned_url"), chunk)
              for (idx, chunk), part in zip(chunks, ordered)))
