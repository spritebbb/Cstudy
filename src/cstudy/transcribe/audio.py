"""下载并抽取音频。

用 yt-dlp 下音频，交给 ffmpeg 转成 whisper 要的 16kHz 单声道 wav。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from ..bilibili.auth import yt_dlp_cookie_file
from ..config import Config
from ..logging_setup import get_logger
from ..tools import ffmpeg_dir, find_yt_dlp

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]


class AudioError(RuntimeError):
    """音频下载或转换失败。消息显示给用户。"""


def download_audio(
    url: str,
    out_dir: Path,
    config: Config,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """下载视频音频并转成 16kHz 单声道 wav，返回文件路径。"""
    yt_dlp = find_yt_dlp()
    if not yt_dlp:
        raise AudioError(
            "找不到 yt-dlp，无法下载视频音频。\n"
            "请在终端运行：winget install yt-dlp.yt-dlp"
        )
    if not ffmpeg_dir():
        raise AudioError(
            "找不到 ffmpeg，无法转换音频格式。\n"
            "请在终端运行：winget install Gyan.FFmpeg"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(id)s.%(ext)s")

    command = [
        yt_dlp,
        "--no-playlist",
        "--extract-audio",
        "--audio-format", "wav",
        "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
        "--ffmpeg-location", ffmpeg_dir() or "",
        "--output", template,
        "--no-warnings",
    ]

    # 带上登录态：B 站对匿名下载限速，带 cookie 快得多
    cookies = yt_dlp_cookie_file(config)
    if cookies:
        command += ["--cookies", str(cookies)]
    command.append(url)

    if on_progress:
        on_progress("正在下载音频…")
    log.info("下载音频：%s", url)
    log.debug("命令：%s", " ".join(command))

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("下载音频超时（超过 30 分钟）。视频可能太长，或网络太慢。") from exc
    except OSError as exc:
        raise AudioError(f"启动 yt-dlp 失败：{exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-5:]) if detail else "（没有输出）"
        raise AudioError(
            f"下载音频失败（yt-dlp 退出码 {result.returncode}）：\n{tail}"
        )

    # yt-dlp 转码后的文件名不确定，找最新的 wav
    candidates = sorted(out_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise AudioError("yt-dlp 跑完了，但没有找到生成的 wav 文件。")
    log.info("音频已就绪：%s", candidates[0])
    return candidates[0]
