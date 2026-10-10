"""查找外部命令行工具（ffmpeg、yt-dlp）。

winget 装完会改 PATH，但已经打开的进程读不到新值。
所以除了 PATH，还要去 winget 的安装目录里找一遍，
否则用户从快捷方式启动软件时会报"找不到 ffmpeg"。
"""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from .logging_setup import get_logger

log = get_logger(__name__)

_WINGET_PACKAGES = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
)


def _search_winget(pattern: str) -> str | None:
    if not _WINGET_PACKAGES.exists():
        return None
    try:
        for candidate in _WINGET_PACKAGES.glob(pattern):
            if candidate.is_file():
                return str(candidate)
    except OSError:
        log.debug("搜索 winget 目录失败", exc_info=True)
    return None


@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """找 ffmpeg.exe。找不到返回 None。"""
    found = shutil.which("ffmpeg")
    if found:
        return found
    found = _search_winget("Gyan.FFmpeg*/**/bin/ffmpeg.exe")
    if found:
        log.debug("从 winget 目录找到 ffmpeg：%s", found)
    return found


@lru_cache(maxsize=1)
def find_yt_dlp() -> str | None:
    """找 yt-dlp.exe。找不到返回 None。"""
    found = shutil.which("yt-dlp")
    if found:
        return found
    found = _search_winget("yt-dlp.yt-dlp*/**/yt-dlp.exe")
    if found:
        log.debug("从 winget 目录找到 yt-dlp：%s", found)
    return found


def ffmpeg_dir() -> str | None:
    """ffmpeg 所在目录，供 yt-dlp 的 --ffmpeg-location 使用。"""
    found = find_ffmpeg()
    return str(Path(found).parent) if found else None


def describe_missing_tools() -> list[str]:
    """返回缺失工具的中文说明，供界面提示。"""
    missing = []
    if not find_ffmpeg():
        missing.append("ffmpeg（抽音频用）")
    if not find_yt_dlp():
        missing.append("yt-dlp（下载视频用）")
    return missing
