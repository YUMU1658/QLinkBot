"""从消息文本中提取 Bilibili 视频目标。

规则：
- 一条消息中存在多个结果时，只取文本位置最靠前的第一个；
- 支持完整视频链接、b23.tv 短链接、BV 号、AV 号；
- 直播、番剧等非视频内容不识别。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

from .bilibili import _USER_AGENT

log = logging.getLogger(__name__)

_BVID = r"BV[0-9A-Za-z]{10}"

FULL_LINK_RE = re.compile(
    rf"https?://(?:www\.|m\.)?bilibili\.com/video/({_BVID}|av\d+)\b",
    re.IGNORECASE,
)
SHORT_LINK_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)
BV_ID_RE = re.compile(rf"\b({_BVID})\b")
AV_ID_RE = re.compile(r"\bav(\d{1,15})\b", re.IGNORECASE)

# 优先级：链接类 > 裸 BV > 裸 AV
_PRIORITY_FULL = 0
_PRIORITY_SHORT = 1
_PRIORITY_BV = 2
_PRIORITY_AV = 3

_EXPAND_TIMEOUT = 10


@dataclass(frozen=True)
class Target:
    """一个待解析的视频目标。"""
    kind: str          # "bvid" | "aid" | "url"
    value: str         # 对应的标识或待展开的短链

    @property
    def cache_key(self) -> str:
        return f"{self.kind}:{self.value.lower()}"

    @property
    def resolve_url(self) -> str:
        if self.kind == "bvid":
            return f"https://www.bilibili.com/video/{self.value}/"
        if self.kind == "aid":
            return f"https://www.bilibili.com/video/{self.value}/"
        return self.value


def extract_target(content: str) -> Target | None:
    candidates: list[tuple[int, int, Target]] = []

    for m in FULL_LINK_RE.finditer(content):
        ident = m.group(1)
        if ident.lower().startswith("bv"):
            target = Target("bvid", ident)
        else:
            target = Target("aid", ident.lower())
        candidates.append((m.start(), _PRIORITY_FULL, target))

    for m in SHORT_LINK_RE.finditer(content):
        candidates.append(
            (m.start(), _PRIORITY_SHORT, Target("url", m.group(0))))

    for m in BV_ID_RE.finditer(content):
        candidates.append((m.start(), _PRIORITY_BV, Target("bvid", m.group(1))))

    for m in AV_ID_RE.finditer(content):
        # 与链接内 av 号位置重叠时，按优先级排序后自然由链接候选胜出
        candidates.append((m.start(), _PRIORITY_AV, Target("aid", f"av{m.group(1)}")))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[0][2]


async def expand_short_link(session: aiohttp.ClientSession,
                            url: str) -> str | None:
    """展开 b23.tv 短链，返回最终 URL。

    Location 头可能是相对路径（如 /video/BVxxx/?...），手动逐跳拼接会
    丢域名；交给 aiohttp 自动跟随重定向，resp.url 即绝对化的最终地址。
    """
    try:
        async with session.get(url,
                               allow_redirects=True,
                               max_redirects=5,
                               timeout=aiohttp.ClientTimeout(total=_EXPAND_TIMEOUT),
                               headers={"User-Agent": _USER_AGENT}) as resp:
            resp.release()
            return str(resp.url)
    except (aiohttp.ClientError, TimeoutError) as e:
        log.warning("展开短链 %s 失败: %s", url, e)
        return None


async def normalize_target(session: aiohttp.ClientSession,
                           target: Target) -> Target | None:
    """把短链展开并归一化为 bvid/aid；非视频页面返回 None。"""
    resolved = target
    if target.kind == "url":
        final_url = await expand_short_link(session, target.value)
        if final_url is None:
            return None
        m = FULL_LINK_RE.search(final_url)
        if not m:
            log.info("短链 %s 指向非视频页面: %s", target.value, final_url)
            return None
        ident = m.group(1)
        resolved = (Target("bvid", ident)
                    if ident.lower().startswith("bv") else Target("aid", ident.lower()))
    elif target.kind == "aid":
        pass
    return resolved
