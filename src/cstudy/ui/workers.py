"""后台任务线程。

网络请求、AI 调用、语音转写都很慢，放在主线程跑会让窗口"未响应"。
统一用 TaskWorker 包起来，进度和结果通过信号回到主线程。
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PyQt6.QtCore import QThread, pyqtSignal

from ..logging_setup import get_logger

log = get_logger(__name__)


def friendly_error(exc: BaseException) -> str:
    """把异常转成用户能看懂的一段话。"""
    # 这些是我们自己抛的，消息本来就是写给人看的
    known = (
        "AIError", "BilibiliError", "BilibiliAuthError", "PipelineError",
        "TranscribeError", "AudioError", "DataError", "ConfigError",
    )
    name = type(exc).__name__
    if name in known and str(exc).strip():
        return str(exc)

    detail = str(exc).strip() or "（没有错误信息）"
    return (
        f"出现了预期之外的错误（{name}）：{detail}\n\n"
        "详细信息已经写进 logs/cstudy.log，把最后 50 行发给我可以帮你定位。"
    )


class TaskWorker(QThread):
    """在后台线程里跑一个函数。

    被调用的函数需要接受 on_progress 关键字参数（可为 None）。
    结果通过 finished_ok 发回，异常通过 failed 发回——绝不让线程静默死掉。
    """

    progress = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._func(*self._args, on_progress=self.progress.emit, **self._kwargs)
        except Exception as exc:  # 线程边界，任何异常都必须接住
            log.error("后台任务失败：%s\n%s", exc, traceback.format_exc())
            self.failed.emit(friendly_error(exc))
        else:
            self.finished_ok.emit(result)
