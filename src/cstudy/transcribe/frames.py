"""抽视频关键帧。

笔记原来只从字幕生成，**老师屏幕上写的代码和 PPT 全丢了**——对 C 语言课来说这是大头：
字幕说"我们写一个 for 循环"，真正的代码在屏幕上。

这里用 yt-dlp 下视频、ffmpeg 按画面变化抽帧，再交给 AI 读屏幕内容
（见 `ai/slides.py`）。

**只下"能看清字"的最低清晰度**：实测 480p 的两分钟视频才 1MB，比下高清便宜得多，
而代码和 PPT 上的字照样看得清。
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..tools import ffmpeg_dir, find_yt_dlp

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_MAX_FRAMES = 60
DEFAULT_WIDTH = 960
DEFAULT_MAX_HEIGHT = 480

# ffmpeg 的 showinfo 每个抽中的帧打一行，里面有 pts_time:12.34
PTS_TIME = re.compile(r"pts_time:([\d.]+)")


class FrameError(RuntimeError):
    """抽帧失败。消息显示给用户。"""


@dataclass
class Frame:
    """一张关键帧，以及它在视频里的位置。

    **必须带时间**：字幕是有时间戳的，屏幕内容也带上时间，笔记才能把
    "老师讲到哪"和"屏幕上放着什么"对上。
    """

    path: Path
    seconds: float = 0.0

    @property
    def timestamp(self) -> str:
        total = max(0, int(self.seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


def _setting(config: Config, key: str, default, cast):
    """读配置，写错就退回默认值——一个笔误不该让整个导入跑不起来。"""
    raw = config.get(f"frames.{key}", default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log.warning("frames.%s 配置看不懂（%r），用默认值 %s", key, raw, default)
        return default


def download_video(
    url: str,
    out_dir: Path,
    config: Config,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """下最低清晰度的视频。抽帧不需要声音，也不需要高清。"""
    yt_dlp = find_yt_dlp()
    if not yt_dlp:
        raise FrameError(
            "找不到 yt-dlp，没法下载视频来抽关键帧。\n"
            "请在终端运行：winget install yt-dlp.yt-dlp"
        )
    if not ffmpeg_dir():
        raise FrameError(
            "找不到 ffmpeg，没法抽关键帧。\n请在终端运行：winget install Gyan.FFmpeg"
        )

    max_height = _setting(config, "max_height", DEFAULT_MAX_HEIGHT, int)
    out_dir.mkdir(parents=True, exist_ok=True)

    command = [
        yt_dlp,
        "--no-playlist",
        # 只要视频流，不要声音——抽帧用不上，省一半下载量
        "-f", f"worstvideo[height<={max_height}]/worstvideo/worst",
        "--ffmpeg-location", ffmpeg_dir() or "",
        "--output", str(out_dir / "%(id)s.%(ext)s"),
        "--no-warnings",
        "--no-part",
        url,
    ]

    if on_progress:
        on_progress("正在下载视频（低清晰度，用来抽关键帧）…")
    log.info("下载视频：%s", url)

    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800,
        )
    except subprocess.TimeoutExpired as exc:
        raise FrameError("下载视频超时（超过 30 分钟）。视频可能太长，或网络太慢。") from exc
    except OSError as exc:
        raise FrameError(f"启动 yt-dlp 失败：{exc}") from exc

    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").strip().splitlines()[-5:])
        raise FrameError(f"下载视频失败（yt-dlp 退出码 {result.returncode}）：\n{tail}")

    candidates = [
        path
        for path in out_dir.iterdir()
        if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".flv"} and path.stat().st_size > 0
    ]
    if not candidates:
        raise FrameError("yt-dlp 跑完了，但没找到下载下来的视频文件。")
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    log.info("视频已就绪：%s（%.1f MB）", newest.name, newest.stat().st_size / 1024 / 1024)
    return newest


def extract_keyframes(
    video: Path,
    out_dir: Path,
    config: Config,
    on_progress: ProgressCallback | None = None,
) -> list[Frame]:
    """按**画面变化**抽帧——画面变了才存一张，不是每 N 秒存一张。

    这样一节课抽出来的帧数少得多（实测 2 分钟才 7 张），而且每张都是"切屏了"的
    关键时刻，不会攒一堆几乎一样的画面白花钱。
    """
    ffmpeg = ffmpeg_dir()
    if not ffmpeg:
        raise FrameError("找不到 ffmpeg，没法抽关键帧。")

    threshold = _setting(config, "scene_threshold", DEFAULT_SCENE_THRESHOLD, float)
    width = _setting(config, "width", DEFAULT_WIDTH, int)

    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.jpg"):
        stale.unlink()   # 上一次跑剩的帧别混进来

    if on_progress:
        on_progress("正在按画面变化抽关键帧…")

    # showinfo 负责把每张帧的时间打出来（`pts_time:`），好跟字幕的时间戳对上。
    # 所以这里用 info 级别，不能用 error——不然连这行日志都没有。
    base = [
        str(Path(ffmpeg) / "ffmpeg"), "-loglevel", "info", "-y",
        "-i", str(video),
        "-vf", f"select='gt(scene,{threshold})',scale={width}:-1,showinfo",
    ]
    tail = ["-q:v", "4", str(out_dir / "%04d.jpg")]

    # -fps_mode 是 ffmpeg 5 之后的名字，老版本叫 -vsync。两个都试一下，
    # 免得用户机器上的 ffmpeg 版本旧一点就整个功能不可用。
    stderr = ""
    for mode in (["-fps_mode", "vfr"], ["-vsync", "vfr"]):
        result = subprocess.run(base + mode + tail, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1800)
        stderr = result.stderr or ""
        if result.returncode == 0:
            break
        if "Unrecognized option" not in stderr:
            raise FrameError(f"抽关键帧失败：{stderr.strip()[:300]}")
    else:
        raise FrameError("抽关键帧失败：这台机器上的 ffmpeg 版本不认抽帧参数。")

    paths = sorted(out_dir.glob("*.jpg"))
    if not paths:
        log.warning("没有抽到任何关键帧（画面可能一直没变）")
        return []

    times = [float(value) for value in PTS_TIME.findall(stderr)]
    if len(times) != len(paths):
        # 对不上就放弃时间戳，只丢一个"时间对不上"的后果，不影响主流程
        log.warning("抽到 %d 张帧但只解析出 %d 个时间戳，这次不带时间", len(paths), len(times))
        times = [0.0] * len(paths)

    frames = [Frame(path=path, seconds=second) for path, second in zip(paths, times)]
    frames = _thin_out(frames, _setting(config, "max_frames", DEFAULT_MAX_FRAMES, int))
    log.info("抽到 %d 张关键帧", len(frames))
    if on_progress:
        on_progress(f"抽到 {len(frames)} 张关键帧，正在让 AI 读屏幕上的内容…")
    return frames


def _thin_out(frames: list[Frame], limit: int) -> list[Frame]:
    """帧太多就**均匀**挑，不是砍掉尾巴——后面的内容同样重要。"""
    if limit <= 0 or len(frames) <= limit:
        return frames
    step = len(frames) / limit
    picked = [frames[int(index * step)] for index in range(limit)]
    log.info("关键帧 %d 张超过上限 %d，均匀挑成 %d 张", len(frames), limit, len(picked))
    return picked
