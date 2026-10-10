"""抽关键帧——只测不调 ffmpeg / yt-dlp 的那部分。

真正抽帧要下视频、跑 ffmpeg，测试里不跑；这里盯的是"时间戳对不对"、
"帧太多怎么挑"、"配置写错会不会炸"这些纯逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.pipeline import StudyPipeline  # noqa: E402
from cstudy.transcribe.frames import (  # noqa: E402
    DEFAULT_MAX_FRAMES,
    DEFAULT_SCENE_THRESHOLD,
    Frame,
    _setting,
    _thin_out,
)


def make_config(frames_cfg: dict | None = None) -> Config:
    return Config({"frames": frames_cfg or {}, "paths": {}}, Path("."))


class TestFrameTimestamp:
    """帧的时间戳要跟字幕的格式一致，笔记那边才能对上。"""

    def test_short_video(self):
        assert Frame(path=Path("a.jpg"), seconds=75).timestamp == "01:15"

    def test_over_an_hour(self):
        assert Frame(path=Path("a.jpg"), seconds=3725).timestamp == "1:02:05"

    def test_zero(self):
        assert Frame(path=Path("a.jpg"), seconds=0).timestamp == "00:00"

    def test_fractional_seconds_are_floored(self):
        assert Frame(path=Path("a.jpg"), seconds=9.8).timestamp == "00:09"


class TestThinOut:
    def _frames(self, count: int) -> list[Frame]:
        return [Frame(path=Path(f"{i:04d}.jpg"), seconds=float(i)) for i in range(count)]

    def test_keeps_everything_when_under_the_limit(self):
        assert len(_thin_out(self._frames(10), 20)) == 10

    def test_caps_at_the_limit(self):
        assert len(_thin_out(self._frames(100), 20)) == 20

    def test_spreads_evenly_instead_of_cutting_the_tail(self):
        """砍尾巴会丢掉后半节课的内容——要均匀挑。"""
        picked = _thin_out(self._frames(100), 10)

        assert picked[0].seconds < 15          # 开头保留
        assert picked[-1].seconds > 85         # 结尾也保留
        assert picked == sorted(picked, key=lambda f: f.seconds)   # 顺序不乱

    def test_zero_limit_means_no_cap(self):
        assert len(_thin_out(self._frames(100), 0)) == 100


class TestSetting:
    def test_reads_the_configured_value(self):
        assert _setting(make_config({"max_frames": 12}), "max_frames", 60, int) == 12

    def test_bad_value_falls_back(self):
        """配置写错了不该让整个导入跑不起来。"""
        assert _setting(make_config({"max_frames": "很多"}), "max_frames", 60, int) == 60

    def test_missing_key_uses_the_default(self):
        assert _setting(make_config(), "scene_threshold", 0.3, float) == 0.3


class TestDisabledByDefault:
    """`frames.enabled` 默认关着——每生成一份笔记都要多下一次视频，
    而题库里已经有一千多道真题带着代码了，不值当。"""

    def test_pipeline_skips_downloading_when_disabled(self, tmp_path: Path):
        config = Config({"frames": {"enabled": False}, "paths": {}}, tmp_path)
        db = Database(tmp_path / "test.db")
        db.init_schema()
        pipeline = StudyPipeline(config, db)

        class Boom:
            def __getattr__(self, name):
                raise AssertionError("关掉之后不该去碰下载或 AI")

        # 传一个假的 video：只要它别被用到就行
        result = pipeline._read_slides(Boom(), lambda text: None)

        assert result == ""


class TestDefaults:
    def test_scene_threshold_is_sane(self):
        assert 0 < DEFAULT_SCENE_THRESHOLD < 1

    def test_max_frames_is_bounded(self):
        """帧数直接决定花多少钱，得有个上限。"""
        assert 0 < DEFAULT_MAX_FRAMES <= 200


class TestDownloadProgress:
    """下载进度要能被解析出来——**不显示进度，用户会以为卡死了**。

    B 站给的 CDN 节点时快时慢，同一份 10.82 MB 的文件，实测从 4 秒到 4 分半
    都遇到过。所以进度必须报出来。
    """

    def test_parses_yt_dlp_progress_lines(self):
        from cstudy.transcribe.frames import DOWNLOAD_PERCENT

        line = "[download]  45.3% of 10.82MiB at 1.20MiB/s ETA 00:03"
        match = DOWNLOAD_PERCENT.search(line)

        assert match and float(match.group(1)) == 45.3

    def test_ignores_other_lines(self):
        from cstudy.transcribe.frames import DOWNLOAD_PERCENT

        assert DOWNLOAD_PERCENT.search("[download] Destination: v.mp4") is None
        assert DOWNLOAD_PERCENT.search("[info] 下载完成") is None
