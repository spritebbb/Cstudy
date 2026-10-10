"""把各模块串起来的业务流程。

拆成三步，对应界面的三个按钮，每一步都能单独重跑：
    load()        导入链接 → 视频信息 + 字幕
    make_notes()  字幕 → Markdown 笔记
    make_quiz()   笔记 → 题目

run() 是一键跑完三步，给命令行和测试用。
界面只跟这一层打交道，不直接碰 B 站、whisper、AI 的细节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .ai import DeepSeekClient, NoteGenerator, QuizGenerator, render_note_file
from .ai.notes import safe_filename
from .bilibili import (
    credential_path,
    fetch_subtitle,
    fetch_video_info,
    load_credential,
    parse_video_ref,
)
from .bilibili.video import VideoInfo
from .config import Config
from .db import Database
from .logging_setup import get_logger
from .models import Question
from .subtitle import Segment
from .transcribe import Transcriber, download_audio

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

SOURCE_LABELS = {
    "cache": "缓存",
    "bilibili_cc": "B 站 CC 字幕",
    "whisper": "本地语音转写",
}


class PipelineError(RuntimeError):
    """流程无法继续。消息直接显示给用户，要说人话。"""


@dataclass
class LoadedVideo:
    """第 1 步的产物：视频信息和字幕已经拿到手。"""

    video_id: int
    bvid: str
    page: int
    title: str
    url: str
    segments: list[Segment]
    subtitle_source: str

    @property
    def subtitle_label(self) -> str:
        return SOURCE_LABELS.get(self.subtitle_source, self.subtitle_source)


@dataclass
class NoteResult:
    """第 2 步的产物。"""

    note_id: int
    path: Path
    body: str


@dataclass
class ImportResult:
    """一键跑完的全部产物。"""

    video: LoadedVideo
    note: NoteResult
    questions: list[Question] = field(default_factory=list)
    question_ids: list[int] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"《{self.video.title}》\n"
            f"字幕：{self.video.subtitle_label}，{len(self.video.segments)} 条\n"
            f"笔记：{self.note.path.name}\n"
            f"题目：{len(self.questions)} 道"
        )


def _noop(_: str) -> None:
    pass


class StudyPipeline:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.client = DeepSeekClient(config)
        self.note_generator = NoteGenerator(self.client, config)
        self.quiz_generator = QuizGenerator(self.client, config)

    # ---------- 第 1 步：导入链接 ----------

    def load(self, link: str, on_progress: ProgressCallback | None = None) -> LoadedVideo:
        progress = on_progress or _noop

        bvid, page = parse_video_ref(link)
        progress("正在获取视频信息…")
        info = fetch_video_info(bvid, page, config=self.config)
        log.info("视频：%s 第 %d P（%s）", bvid, page, info.display_title)

        video_id = self.db.upsert_video(
            bvid=bvid,
            page=page,
            title=info.display_title,
            url=info.url,
            duration=info.duration,
        )
        segments, source = self._resolve_subtitle(video_id, info, progress)

        return LoadedVideo(
            video_id=video_id,
            bvid=bvid,
            page=page,
            title=info.display_title,
            url=info.url,
            segments=segments,
            subtitle_source=source,
        )

    def _resolve_subtitle(
        self, video_id: int, info: VideoInfo, on_progress: ProgressCallback
    ) -> tuple[list[Segment], str]:
        """按 缓存 → B站CC → 本地转写 的顺序拿字幕。"""
        cached = self.db.get_subtitle_segments(video_id)
        if cached:
            on_progress(f"用已缓存的字幕（{len(cached)} 条）")
            return cached, "cache"

        credential = load_credential(credential_path(self.config))
        if credential:
            on_progress("正在从 B 站获取字幕…")
            segments = fetch_subtitle(info, credential=credential, config=self.config)
            if segments:
                self.db.save_subtitle(video_id, "bilibili_cc", segments)
                return segments, "bilibili_cc"
        else:
            on_progress("尚未登录 B 站，拿不到 CC 字幕，直接走语音转写")

        if not self.config.get("transcribe.enabled", True):
            raise PipelineError(
                "这个视频没有可用的 CC 字幕，而语音转写已在 config.yaml 里关闭。\n"
                "把 transcribe.enabled 改成 true 可以开启，或者换一个带字幕的视频。"
            )

        on_progress("没有 CC 字幕，改用本地语音转写…")
        audio_path = download_audio(info.url, self.config.path("audio"), self.config, on_progress)
        segments = Transcriber(self.config).transcribe(audio_path, on_progress)

        # 转写成功后删掉音频省硬盘；失败时不删，方便排查
        try:
            audio_path.unlink()
            log.debug("已删除临时音频：%s", audio_path)
        except OSError:
            log.debug("删除临时音频失败", exc_info=True)

        self.db.save_subtitle(video_id, "whisper", segments)
        return segments, "whisper"

    # ---------- 第 2 步：生成笔记 ----------

    def make_notes(
        self, video: LoadedVideo, on_progress: ProgressCallback | None = None
    ) -> NoteResult:
        progress = on_progress or _noop
        progress("正在生成学习笔记…")
        body = self.note_generator.generate(video.segments, video.title, progress)

        path = self._write_note_file(video, body)
        note_id = self.db.save_note(
            video_id=video.video_id, title=video.title, content=body, file_path=str(path)
        )
        return NoteResult(note_id=note_id, path=path, body=body)

    def _write_note_file(self, video: LoadedVideo, body: str) -> Path:
        notes_dir = self.config.path("notes")
        notes_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%d")
        stem = safe_filename(video.title)
        path = notes_dir / f"{stamp} {stem}.md"

        # 同一天重复导入同一个视频时不覆盖，加序号
        counter = 2
        while path.exists():
            path = notes_dir / f"{stamp} {stem} ({counter}).md"
            counter += 1

        path.write_text(
            render_note_file(
                title=video.title,
                body=body,
                url=video.url,
                source=video.subtitle_label,
            ),
            encoding="utf-8",
        )
        log.info("笔记已写入：%s", path)
        return path

    # ---------- 第 3 步：出题 ----------

    def make_quiz(
        self,
        note_id: int,
        note_body: str,
        source: str = "",
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[Question], list[int]]:
        progress = on_progress or _noop
        progress("正在出题…")
        questions = self.quiz_generator.generate(note_body, progress)
        question_ids = self.db.save_questions(questions, note_id=note_id, source=source)
        return questions, question_ids

    # ---------- 一键跑完 ----------

    def run(self, link: str, on_progress: ProgressCallback | None = None) -> ImportResult:
        video = self.load(link, on_progress)
        note = self.make_notes(video, on_progress)
        questions, question_ids = self.make_quiz(
            note.note_id, note.body, f"video:{video.bvid}#p{video.page}", on_progress
        )
        result = ImportResult(
            video=video, note=note, questions=questions, question_ids=question_ids
        )
        log.info("导入完成：%s", result.summary.replace("\n", " | "))
        return result
