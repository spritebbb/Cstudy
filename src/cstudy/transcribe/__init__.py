"""本地语音转写：没有 CC 字幕时的兜底方案。"""

from .audio import AudioError, download_audio
from .whisper_runner import TranscribeError, Transcriber

__all__ = ["AudioError", "TranscribeError", "Transcriber", "download_audio"]
