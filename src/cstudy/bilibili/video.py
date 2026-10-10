"""B 站视频信息与字幕获取。

依赖 bilibili-api-python 17.x。B 站接口随时可能变，所以：
- 所有调用都有重试
- 失败一律转成说人话的 BilibiliError
- 字幕拿不到返回 None，由上层决定是转写还是跳过
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import requests
from bilibili_api import video as bili_video
from bilibili_api.exceptions import ApiException, NetworkException, ResponseCodeException

# 注意：curl_cffi 的异常**不是** requests 的子类（它挂在 OSError 下面），
# 所以下面 _retry 里必须单独捕一次，否则 TLS 抖动会直接冒到界面上。
from curl_cffi.requests.exceptions import RequestException as CurlRequestException

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


def _run_async(coro):
    """把 bilibili_api 的协程跑完再返回结果。

    库 17.x 的接口（get_info / get_subtitle 等）全是 async 的，但我们这一层
    是同步的——界面把它丢在 QThread 里跑，没有事件循环。所以自己起一个跑完。
    不这么做的话拿到的是协程对象，下一行 .get() 就会炸成
    "'coroutine' object has no attribute 'get'"。
    """
    return asyncio.run(coro)


def _retry(func: Callable[[], T], attempts: int, what: str) -> T:
    """给 B 站调用加一层重试，并把库抛的异常一律翻译成人话。

    这里必须把 bilibili_api 的异常全接住：它抛的 NetworkException 和
    ResponseCodeException 都只是 ApiException 的子类，不是我们的 BilibiliError。
    漏接的话界面就会显示"出现了预期之外的错误（NetworkException）"这种中英夹杂的东西。
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except ResponseCodeException as exc:
            # 业务错误码，重试没意义
            raise BilibiliError(_explain_response_code(exc)) from exc
        except NetworkException as exc:
            status = _status_of(exc)
            if 400 <= status < 500:
                # 4xx 是请求本身的问题（没登录、没权限、限流），重试只会更糟
                raise BilibiliError(_explain_http_status(status, what)) from exc
            last = exc
            if attempt < attempts:
                wait = RETRY_BACKOFF_SECONDS * attempt
                log.warning(
                    "%s 第 %d 次失败（HTTP %s），%.0f 秒后重试",
                    what, attempt, status or "?", wait,
                )
                time.sleep(wait)
        except ApiException as exc:
            # 其他库异常：不重试，翻译了直接抛
            raise BilibiliError(f"{what}失败：{exc}") from exc
        except (requests.RequestException, CurlRequestException) as exc:
            last = exc
            if attempt < attempts:
                wait = RETRY_BACKOFF_SECONDS * attempt
                log.warning(
                    "%s 第 %d 次失败（%s），%.0f 秒后重试",
                    what, attempt, type(exc).__name__, wait,
                )
                time.sleep(wait)
    if isinstance(last, NetworkException) and _status_of(last) >= 500:
        # 重试耗尽还是 5xx：这是 B 站的问题，不能提示用户"检查网络"
        raise BilibiliError(_explain_http_status(_status_of(last), what))
    raise BilibiliError(f"{what}失败，已重试 {attempts} 次，请检查网络。原始错误：{last}")


def _explain_http_status(status: int, what: str, scene: str = "api") -> str:
    """把 HTTP 状态码翻译成用户能看懂的话。

    scene 区分两种场景：调 B 站接口（api）和从 CDN 下字幕文件（cdn）。
    同样是 403，前者多半是登录态过期，后者只是字幕临时地址失效——
    提示错了会把用户引去白扫一次码。
    """
    if scene == "cdn":
        hints = {
            403: "字幕文件的临时地址已经失效了。重新导入一次这个视频就好。",
            404: "字幕文件不存在，可能是这个视频刚更新过。重新导入一次试试。",
            429: "下载太频繁被限流了。等几分钟再导入。",
        }
    else:
        hints = {
            403: "B 站拒绝了这次访问，多半是登录态过期了。双击 扫码登录.bat 重新扫码即可。",
            404: "B 站找不到这个内容，视频可能已下架，或链接失效了。",
            412: "B 站的访问校验没通过，通常是登录态失效。重新扫码登录试试。",
            429: "请求太频繁，被 B 站限流了。等几分钟再试，不要连续导入。",
        }

    if status in hints:
        return f"{what}失败：{hints[status]}（HTTP {status}）"
    if status >= 500:
        return f"{what}失败：B 站服务器出问题了（HTTP {status}），这不是你的网络问题，过一会儿再试。"
    return f"{what}失败（HTTP {status}）。稍后重新导入试试。"


def _status_of(exc: Exception) -> int:
    """取 NetworkException 的 HTTP 状态码。取不到按 0 处理（当成可重试）。"""
    try:
        return int(getattr(exc, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
        lambda: _run_async(
            bili_video.Video(bvid=bvid, credential=credential).get_info()
        ),
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


def _download_subtitle_body(url: str, config: Config | None) -> list[dict[str, Any]]:
    """下载字幕文件并取出 body。

    5xx 让 _retry 去重试；4xx 说明字幕地址本身失效了，重试没意义，
    直接翻译成人话抛出去——不能让用户看到 requests 的英文报错。
    """

    def once() -> requests.Response:
        response = requests.get(url, timeout=30)
        if response.status_code >= 500:
            response.raise_for_status()  # 交给 _retry 重试
        if response.status_code >= 400:
            raise BilibiliError(
                _explain_http_status(response.status_code, "下载字幕", scene="cdn")
            )
        return response

    response = _retry(once, attempts=_retries_from(config), what="下载字幕")

    try:
        payload = response.json()
    except ValueError as exc:
        raise BilibiliError(
            "字幕文件的内容不是预期格式，可能下载到的是错误页面。"
            "过一会儿重新导入试试，或者换一个视频。"
        ) from exc

    if not isinstance(payload, dict):
        return []
    body = payload.get("body") or []
    return list(body) if isinstance(body, list) else []


def fetch_subtitle(
    info: VideoInfo, credential=None, config: Config | None = None
) -> list[Segment] | None:
    """下载 CC 字幕。没有字幕返回 None。"""
    if not info.cid:
        log.warning("没有 CID，无法查询字幕")
        return None

    payload = _retry(
        lambda: _run_async(
            bili_video.Video(bvid=info.bvid, credential=credential).get_subtitle(info.cid)
        ),
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
    body = _download_subtitle_body(url, config)

    # 逐条构造而不是列表推导：字幕文件格式偶尔会有脏数据，
    # 宁可丢掉那一条，也不要让整个导入崩掉。
    segments: list[Segment] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = str(item.get("content") or "").strip()
        if not text:
            continue
        try:
            start = float(item.get("from") or 0.0)
        except (TypeError, ValueError):
            start = 0.0  # 时间戳坏了不值得丢掉整条字幕
        segments.append(Segment(start=start, text=text))
    if not segments:
        log.warning("字幕文件是空的")
        return None

    log.info("字幕 %d 条", len(segments))
    return segments
