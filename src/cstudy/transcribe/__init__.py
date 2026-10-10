"""下载与本地转写：拿音频、抽视频关键帧。"""

from .audio import AudioError, download_audio
from .frames import Frame, FrameError, download_video, extract_keyframes
from .whisper_runner import TranscribeError, Transcriber

__all__ = [
    "AudioError",
    "Frame",
    "FrameError",
    "TranscribeError",
    "Transcriber",
    "download_audio",
    "download_video",
    "extract_keyframes",
]
