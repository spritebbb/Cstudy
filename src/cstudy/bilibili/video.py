"""B 站视频信息与字幕获取。

依赖 bilibili-api-python 17.x。B 站接口随时可能变，所以：
- 所有调用都有重试
- 失败一律转成说人话的 BilibiliError
- 字幕拿不到返回 None，由上层决定是转写还是跳过
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import requests
from bilibili_api import video as bili_video
from bilibili_api.exceptions import ResponseCodeException

from ..config import Config
from ..logging_setup import get_logger
from ..subtitle import Segment

log = get_logger(__name__)

BV_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")
PAGE_PATTERN = re.compile(r"[?&]p=(\d+)")

T = TypeVar("T")

DEFAULT_RETRIES = 3
RETRY_BACKOFF_SECONDS = 3.0


class BilibiliError(RuntimeError):
    """B 站操作失败。消息会显示给用户，所以要说人话。"""


@dataclass
class VideoInfo:
    bvid: str
    page: int
    title: str
    cid: int
    duration: int
    url: str
    page_title: str = ""

    @property
    def display_title(self) -> str:
        """分 P 有独立标题时拼上，便于区分同一视频的不同集。"""
        if self.page_title and self.page_title != self.title:
            return f"{self.title} - {self.page_title}"
        return self.title


def parse_video_ref(text: str) -> tuple[str, int]:
    """从链接或 BV 号里解析出 (BV号, 第几P)。

    支持这些写法：
        BV1xx411c7mD
        https://www.bilibili.com/video/BV1xx411c7mD
        https://www.bilibili.com/video/BV1xx411c7mD?p=3
    """
    text = (text or "").strip()
    match = BV_PATTERN.search(text)
    if not match:
        raise BilibiliError(
            "没找到 BV 号。请粘贴完整的视频链接，或者直接输入 BV 号（形如 BV1xx411c7mD）。"
        )
    bvid = match.group(0)
    page_match = PAGE_PATTERN.search(text)
    page = max(1, int(page_match.group(1))) if page_match else 1
    return bvid, page


def _retry(func: Callable[[], T], attempts: int, what: str) -> T:
    """给 B 站调用加一层重试。业务错误码不重试，直接翻译成人话抛出。"""
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except ResponseCodeException as exc:
            raise BilibiliError(_explain_response_code(exc)) from exc
        except requests.RequestException as exc:
            last = exc
            if attempt < attempts:
                wait = RETRY_BACKOFF_SECONDS * attempt
                log.warning(
                    "%s 第 %d 次失败（%s），%.0f 秒后重试",
                    what, attempt, type(exc).__name__, wait,
                )
                time.sleep(wait)
    raise BilibiliError(f"{what}失败，已重试 {attempts} 次，请检查网络。原始错误：{last}")


def _explain_response_code(exc: ResponseCodeException) -> str:
    code = getattr(exc, "code", None)
    mapping = {
        -404: "视频不存在，可能已被删除或 BV 号输错了。",
        -403: "没有权限访问这个视频（可能是私密稿件，或者需要登录）。",
        62002: "这个视频不可见（可能已下架或仅作者自己可见）。",
        -400: "请求参数有误。",
    }
    if code in mapping:
        return f"B 站返回错误 {code}：{mapping[code]}"
    return f"B 站返回错误 {code}：{exc}"


def _retries_from(config: Config | None) -> int:
    return int(config.get("bilibili.max_retries", DEFAULT_RETRIES)) if config else DEFAULT_RETRIES


def fetch_video_info(
    bvid: str, page: int, credential=None, config: Config | None = None
) -> VideoInfo:
    """拿视频标题、分 P、时长、CID。"""
    info = _retry(
        lambda: bili_video.Video(bvid=bvid, credential=credential).get_info(),
        attempts=_retries_from(config),
        what="获取视频信息",
    )
    log.debug("视频信息：%s", info.get("title"))

    pages = info.get("pages") or []
    if pages and page > len(pages):
        raise BilibiliError(f"这个视频只有 {len(pages)} 个分 P，你指定的是第 {page} 个。")

    target: dict[str, Any] = {}
    for item in pages:
        if int(item.get("page", 0)) == page:
            target = item
            break
    if not target and pages:
        target = pages[page - 1]

    return VideoInfo(
        bvid=bvid,
        page=page,
        title=str(info.get("title", "")).strip(),
        cid=int(target.get("cid") or 0),
        duration=int(target.get("duration") or info.get("duration") or 0),
        url=f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else ""),
        page_title=str(target.get("part", "")).strip(),
    )


def _extract_subtitle_entries(payload: Any) -> list[dict[str, Any]]:
    """从 get_subtitle() 的返回里挖出字幕列表。

    不同版本可能返回 {"subtitle": {"subtitles": [...]}} 或 {"subtitles": [...]}，
    两种都兼容。
    """
    if not isinstance(payload, dict):
        return []
    nested = payload.get("subtitle")
    if isinstance(nested, dict) and nested.get("subtitles"):
        return list(nested["subtitles"])
    return list(payload.get("subtitles") or [])


def fetch_subtitle(
    info: VideoInfo, credential=None, config: Config | None = None
) -> list[Segment] | None:
    """下载 CC 字幕。没有字幕返回 None。"""
    if not info.cid:
        log.warning("没有 CID，无法查询字幕")
        return None

    payload = _retry(
        lambda: bili_video.Video(bvid=info.bvid, credential=credential).get_subtitle(info.cid),
        attempts=_retries_from(config),
        what="查询字幕",
    )
    entries = _extract_subtitle_entries(payload)
    if not entries:
        log.info("视频 %s 第 %d P 没有 CC 字幕", info.bvid, info.page)
        return None

    # 优先中文
    chosen = next(
        (item for item in entries if str(item.get("lan") or "").startswith("zh")),
        entries[0],
    )

    url = str(chosen.get("subtitle_url") or "")
    if not url:
        log.warning("字幕条目里没有地址")
        return None
    if url.startswith("//"):
        url = "https:" + url

    log.info("下载字幕：%s", chosen.get("lan_doc") or chosen.get("lan"))
    response = _retry(
        lambda: requests.get(url, timeout=30),
        attempts=_retries_from(config),
        what="下载字幕",
    )
    response.raise_for_status()
    body = (response.json() or {}).get("body") or []

    segments = [
        Segment(start=float(item.get("from", 0.0)), text=str(item.get("content", "")))
        for item in body
        if str(item.get("content", "")).strip()
    ]
    if not segments:
        log.warning("字幕文件是空的")
        return None

    log.info("字幕 %d 条", len(segments))
    return segments
