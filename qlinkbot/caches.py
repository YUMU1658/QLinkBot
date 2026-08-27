"""缓存与限速实现。"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path

log = logging.getLogger(__name__)


class _TTLMap:
    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()

    def get(self, key: str):
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if time.monotonic() > expires_at:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > 512:
            self._data.popitem(last=False)

    def touch(self, key: str) -> None:
        """重置该键的过期时间。"""
        item = self._data.get(key)
        if item is not None:
            self._data[key] = (time.monotonic() + self._ttl, item[1])
            self._data.move_to_end(key)

    def cleanup(self) -> list[tuple[str, object]]:
        """移除并返回全部过期条目的 (键, 值)。"""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if now > exp]
        out = []
        for k in expired:
            out.append((k, self._data.pop(k)[1]))
        return out

    def live_values(self) -> list:
        now = time.monotonic()
        return [v for exp, v in self._data.values() if now <= exp]


class FileCache:
    """已解析视频文件缓存：key -> 本地文件路径。"""

    def __init__(self, ttl: int) -> None:
        self._map = _TTLMap(ttl)

    def get(self, key: str) -> Path | None:
        path = self._map.get(key)
        if path is not None and not path.is_file():
            self._map._data.pop(key, None)
            return None
        return path

    def put(self, key: str, path: Path) -> None:
        self._map.set(key, path)

    def purge_expired(self) -> list[Path]:
        """移除过期条目并返回对应的本地路径，供上层删除文件。"""
        return [v for _, v in self._map.cleanup() if v is not None]

    def live_paths(self) -> list[Path]:
        """当前未过期的全部缓存路径。"""
        return [p for p in self._map.live_values() if p is not None]

    def refresh(self, key: str) -> None:
        self._map.touch(key)


class MetadataCache:
    """视频元数据缓存：key -> VideoMeta.to_cache() 字典。"""

    def __init__(self, ttl: int) -> None:
        self._map = _TTLMap(ttl)

    def get(self, key: str):
        return self._map.get(key)

    def put(self, key: str, data: dict) -> None:
        self._map.set(key, data)

    def purge_expired(self) -> None:
        self._map.cleanup()


class DuplicateLimiter:
    """会话隔离的重复视频速率限制。"""

    def __init__(self, window: int) -> None:
        self._window = window
        # session_key -> {video_key -> timestamp}
        self._seen: dict[str, dict[str, float]] = defaultdict(dict)

    def check_and_mark(self, session_key: str, video_key: str,
                       mark: bool) -> bool:
        """返回 True 表示在窗口内重复。mark=True 时同时记录本次。"""
        now = time.monotonic()
        seen = self._seen.get(session_key)
        if seen:
            expired = [k for k, ts in seen.items() if now - ts > self._window]
            for k in expired:
                del seen[k]
            if video_key in seen:
                return True
        if mark:
            self._seen.setdefault(session_key, {})[video_key] = now
        return False

    def purge_expired(self) -> None:
        """清扫所有会话中窗口外的记录，并删除空的会话条目。"""
        now = time.monotonic()
        empty = []
        for session_key, seen in self._seen.items():
            expired = [k for k, ts in seen.items() if now - ts > self._window]
            for k in expired:
                del seen[k]
            if not seen:
                empty.append(session_key)
        for session_key in empty:
            del self._seen[session_key]


class RateLimiter:
    """全局滑动窗口限流：window 秒内最多 count 次。"""

    def __init__(self, count: int, window: int) -> None:
        self._count = count
        self._window = window
        self._events: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        while self._events and now - self._events[0] > self._window:
            self._events.popleft()
        if len(self._events) >= self._count:
            return False
        self._events.append(now)
        return True

    def release(self) -> None:
        """回退一次占用（用于未实际发送的解析）。"""
        if self._events:
            self._events.pop()


class MessageDedup:
    """入站消息 id 去重（平台可能重复推送）。"""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._ids:
            self._ids.move_to_end(message_id)
            return True
        self._ids[message_id] = None
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)
        return False
