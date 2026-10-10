"""读视频关键帧上的屏幕内容。

字幕只记了老师**说**的话，屏幕上**写**的代码和 PPT 一个字都没有。这个模块把
关键帧发给 AI（`deepseek-flash` 支持图片输入），让它把屏幕内容抄下来，
再交给笔记生成一起整理。

配套的是 `transcribe/frames.py`——那个负责下视频、抽帧；这个负责"读"。
"""

from __future__ import annotations

import base64
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..transcribe.frames import Frame
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

# 一次送几张。太多会把单次请求撑大，太少又浪费调用次数。
DEFAULT_PER_CALL = 8


class SlideReader:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        raw = config.get("frames.per_call", DEFAULT_PER_CALL)
        try:
            self.per_call = max(1, int(raw))
        except (TypeError, ValueError):
            log.warning("frames.per_call 配置看不懂（%r），用默认值", raw)
            self.per_call = DEFAULT_PER_CALL

    def read(
        self, frames: list[Frame], on_progress: ProgressCallback | None = None
    ) -> str:
        """把关键帧读成文字。某一批失败就跳过那一批，不让整段白跑。"""
        if not frames:
            return ""

        total = len(frames)
        parts: list[str] = []
        failed = 0

        for start in range(0, total, self.per_call):
            batch = frames[start : start + self.per_call]
            if on_progress:
                on_progress(f"正在读屏幕内容（第 {start + 1}~{start + len(batch)}/{total} 张）…")
            try:
                text = self._read_batch(batch, start + 1, total)
            except Exception as exc:
                failed += 1
                log.warning("读第 %d 批关键帧失败：%s: %s", start // self.per_call + 1,
                            type(exc).__name__, exc)
                continue
            if text.strip():
                # 标上时间范围——笔记那边靠这个把"屏幕上的代码"和"老师讲的话"对上
                parts.append(f"【{batch[0].timestamp}~{batch[-1].timestamp}】\n{text}")

        if failed and not parts:
            log.error("所有关键帧都读失败了")
            return ""
        if failed:
            log.warning("%d 批关键帧读失败，已经跳过", failed)

        return "\n\n".join(parts)

    def _read_batch(self, batch: list[Frame], first: int, total: int) -> str:
        content: list[dict] = [
            {"type": "text", "text": prompts.slides_user_prompt(first, total, len(batch))}
        ]
        for frame in batch:
            encoded = base64.b64encode(frame.path.read_bytes()).decode()
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        return self.client.chat(
            [
                {"role": "system", "content": prompts.SLIDES_SYSTEM},
                {"role": "user", "content": content},
            ],
            temperature=0,
        )
