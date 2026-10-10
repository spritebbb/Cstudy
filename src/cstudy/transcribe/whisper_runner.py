"""faster-whisper 转写。

模型第一次使用时才下载（几百 MB），下载位置是 HuggingFace 缓存。
有 NVIDIA 显卡就用 GPU，加载失败自动退回 CPU——不能因为 CUDA 没配好
就让整个功能不可用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..subtitle import Segment

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]


class TranscribeError(RuntimeError):
    """转写失败。消息显示给用户。"""


def _cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        log.debug("检测 CUDA 失败，按不可用处理", exc_info=True)
        return False


class Transcriber:
    def __init__(self, config: Config) -> None:
        self.model_size = str(config.get("transcribe.model_size", "small"))
        self.device_pref = str(config.get("transcribe.device", "auto"))
        self.compute_pref = str(config.get("transcribe.compute_type", "auto"))
        self.language = str(config.get("transcribe.language", "zh")) or None
        self._model = None

    def _resolve(self) -> tuple[str, str]:
        device = self.device_pref
        if device == "auto":
            device = "cuda" if _cuda_available() else "cpu"
        compute = self.compute_pref
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"
        return device, compute

    def _ensure_model(self, on_progress: ProgressCallback | None = None):
        if self._model is not None:
            return self._model

        from faster_whisper import WhisperModel

        device, compute = self._resolve()
        if on_progress:
            on_progress(
                f"首次使用需要下载 whisper 模型（{self.model_size}，{device}），请耐心等待…"
            )
        log.info("加载 whisper 模型：size=%s device=%s compute=%s", self.model_size, device, compute)

        try:
            self._model = WhisperModel(self.model_size, device=device, compute_type=compute)
        except Exception as exc:
            if device == "cpu":
                raise TranscribeError(
                    f"加载 whisper 模型失败：{exc}\n"
                    "可能是首次下载模型时网络中断，重试一次通常就好。"
                ) from exc
            log.warning("GPU 加载失败（%s），改用 CPU 重试", exc)
            if on_progress:
                on_progress("显卡不可用，改用 CPU 转写（会慢一些）…")
            try:
                self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            except Exception as cpu_exc:
                raise TranscribeError(
                    f"CPU 加载 whisper 模型也失败了：{cpu_exc}"
                ) from cpu_exc

        return self._model

    def transcribe(
        self, audio_path: Path, on_progress: ProgressCallback | None = None
    ) -> list[Segment]:
        """把音频转成带时间戳的字幕。"""
        if not audio_path.exists():
            raise TranscribeError(f"音频文件不存在：{audio_path}")

        model = self._ensure_model(on_progress)
        if on_progress:
            on_progress("正在转写（这一步最慢，可以先去做别的）…")
        log.info("开始转写：%s", audio_path)

        try:
            # transcribe 返回的是生成器，必须消费完才真正开始跑
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=self.language,
                vad_filter=True,  # 过滤静音，能省不少时间
                beam_size=5,
            )
            segments = [
                Segment(start=float(seg.start), text=seg.text.strip())
                for seg in raw_segments
                if seg.text.strip()
            ]
        except Exception as exc:
            raise TranscribeError(f"转写失败：{exc}") from exc

        if not segments:
            raise TranscribeError("转写完成，但没有识别出任何文字。音频可能是纯音乐或静音。")

        log.info("转写完成，共 %d 条字幕（音频时长 %.0f 秒）", len(segments), info.duration)
        return segments
