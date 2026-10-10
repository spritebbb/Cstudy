"""字幕的表示、渲染和分块。

分块是核心逻辑：长视频的字幕不能一次性塞给模型，
按时间切块、逐块总结、再合并，这是"长视频必须分段处理"的落地。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Segment:
    """一条字幕。start 是相对视频开头的秒数。"""

    start: float
    text: str

    @property
    def timestamp(self) -> str:
        total = max(0, int(self.start))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


def render(segments: list[Segment]) -> str:
    """渲染成带时间戳的纯文本，喂给模型。"""
    return "\n".join(f"[{seg.timestamp}] {seg.text.strip()}" for seg in segments if seg.text.strip())


def chunk_segments(
    segments: list[Segment],
    chunk_minutes: int = 10,
    max_chars: int = 6000,
) -> list[list[Segment]]:
    """把字幕切成若干块。

    到达时间上限或字数上限就切一刀。单条字幕超长时不硬切，
    宁可让这一块大一点，也不要破坏语义。
    """
    if not segments:
        return []

    span_limit = max(1, chunk_minutes) * 60
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    current_chars = 0
    chunk_start = segments[0].start

    for seg in segments:
        would_overflow = current and (
            seg.start - chunk_start >= span_limit or current_chars + len(seg.text) > max_chars
        )
        if would_overflow:
            chunks.append(current)
            current = []
            current_chars = 0
            chunk_start = seg.start
        current.append(seg)
        current_chars += len(seg.text)

    if current:
        chunks.append(current)

    return chunks


def plain_text(segments: list[Segment]) -> str:
    """不带时间戳的纯文本，用于统计字数和转写结果。"""
    return "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
