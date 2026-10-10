"""笔记生成：分段总结，再合并成一份完整笔记。"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..subtitle import Segment, chunk_segments, render
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]


class NoteGenerator:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.chunk_minutes = int(config.get("notes.chunk_minutes", 10))
        self.max_chars = int(config.get("ai.max_chars_per_chunk", 6000))

    def generate(
        self,
        segments: list[Segment],
        title: str = "",
        on_progress: ProgressCallback | None = None,
        visual: str = "",
    ) -> str:
        """把字幕变成 Markdown 笔记。返回纯笔记正文（不含文件头）。

        visual 是视频画面上的内容（屏幕上写的代码、课件文字），见 `ai/slides.py`。
        字幕只有老师说的话，代码得从画面上来。
        """
        if not segments:
            raise ValueError("没有字幕内容，无法生成笔记")

        blocks = chunk_segments(segments, self.chunk_minutes, self.max_chars)
        log.info("字幕 %d 条，切成 %d 块", len(segments), len(blocks))
        if visual.strip():
            log.info("另有画面内容 %d 字，会一起送给每一段", len(visual))

        partials: list[str] = []
        for index, block in enumerate(blocks, start=1):
            if on_progress:
                on_progress(f"正在总结第 {index}/{len(blocks)} 段…")
            log.info("生成第 %d/%d 段小结", index, len(blocks))
            partials.append(
                self.client.chat(
                    [
                        {"role": "system", "content": prompts.NOTE_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.note_user_prompt(
                                render(block), index, len(blocks), visual
                            ),
                        },
                    ]
                )
            )

        if len(partials) == 1:
            return partials[0]

        if on_progress:
            on_progress("正在合并分段笔记…")
        log.info("合并 %d 段笔记", len(partials))
        return self.client.chat(
            [
                {"role": "system", "content": prompts.MERGE_SYSTEM},
                {"role": "user", "content": prompts.merge_user_prompt(partials)},
            ]
        )


def render_note_file(title: str, body: str, url: str = "", source: str = "") -> str:
    """给笔记加上文件头，写进 notes/ 用。"""
    lines = [f"# {title}", ""]
    lines.append(f"> 来源：{url or '（未记录）'}")
    if source:
        lines.append(f"> 字幕来源：{source}")
    lines.append(f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines) + "\n"


def safe_filename(title: str, max_length: int = 60) -> str:
    """把标题变成能当文件名的字符串。Windows 禁用字符一律换成下划线。"""
    forbidden = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in forbidden else ch for ch in title)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned:
        cleaned = "未命名笔记"
    return cleaned[:max_length]
