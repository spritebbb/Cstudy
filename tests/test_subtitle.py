"""字幕分块逻辑的测试。这是核心逻辑：切错了会导致笔记漏内容或超长。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.subtitle import Segment, chunk_segments, plain_text, render  # noqa: E402


def make_segments(count: int, seconds_between: float = 5.0, text: str = "内容") -> list[Segment]:
    return [Segment(start=i * seconds_between, text=f"{text}{i}") for i in range(count)]


class TestTimestamp:
    def test_under_one_hour(self):
        assert Segment(start=0, text="").timestamp == "00:00"
        assert Segment(start=65, text="").timestamp == "01:05"
        assert Segment(start=3599, text="").timestamp == "59:59"

    def test_over_one_hour(self):
        assert Segment(start=3600, text="").timestamp == "1:00:00"
        assert Segment(start=3725, text="").timestamp == "1:02:05"

    def test_negative_is_clamped(self):
        assert Segment(start=-5, text="").timestamp == "00:00"


class TestRender:
    def test_includes_timestamps(self):
        result = render([Segment(0, "第一句"), Segment(65, "第二句")])
        assert result == "[00:00] 第一句\n[01:05] 第二句"

    def test_skips_blank_lines(self):
        result = render([Segment(0, "有内容"), Segment(5, "   "), Segment(10, "又有内容")])
        assert result.count("\n") == 1


class TestChunkSegments:
    def test_empty_input(self):
        assert chunk_segments([]) == []

    def test_short_input_stays_one_chunk(self):
        chunks = chunk_segments(make_segments(5), chunk_minutes=10)
        assert len(chunks) == 1
        assert len(chunks[0]) == 5

    def test_splits_by_time(self):
        # 每条间隔 5 秒，120 条共 595 秒；按 1 分钟切应得到多块
        chunks = chunk_segments(make_segments(120), chunk_minutes=1, max_chars=10**9)
        assert len(chunks) > 1
        # 每块的时间跨度不超过上限（最后一块除外）
        for block in chunks[:-1]:
            span = block[-1].start - block[0].start
            assert span <= 60

    def test_splits_by_char_count(self):
        # 每条 100 字，上限 250 字，最多装 2 条
        segments = [Segment(i, "字" * 100) for i in range(6)]
        chunks = chunk_segments(segments, chunk_minutes=999, max_chars=250)
        assert len(chunks) == 3
        assert all(len(block) == 2 for block in chunks)

    def test_no_segment_is_lost(self):
        segments = make_segments(50)
        chunks = chunk_segments(segments, chunk_minutes=1, max_chars=80)
        flattened = [seg for block in chunks for seg in block]
        assert flattened == segments

    def test_single_oversized_segment_is_not_split(self):
        # 单条就超过字数上限时，宁可这块大一点，也不要破坏语义
        segments = [Segment(0, "长" * 1000), Segment(1, "短")]
        chunks = chunk_segments(segments, chunk_minutes=10, max_chars=100)
        assert len(chunks[0]) == 1
        assert chunks[0][0].text == "长" * 1000


class TestPlainText:
    def test_joins_without_timestamps(self):
        assert plain_text([Segment(0, "甲"), Segment(5, "乙")]) == "甲\n乙"
