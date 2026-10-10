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

from .ai import (
    DeepSeekClient,
    NoteGenerator,
    QuizGenerator,
    Reviewer,
    SlideReader,
    render_note_file,
)
from .ai.notes import safe_filename
from .bilibili import (
    BilibiliError,
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
from .models import Question, ReviewReport
from .subtitle import Segment
from .transcribe import Transcriber, download_audio, download_video, extract_keyframes

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
    # 视频画面上的内容（屏幕上的代码、课件文字），见 _read_slides()。
    # 抽帧失败就是空字符串——笔记退化成"只有字幕"，不该因此生成不了。
    visual_text: str = ""

    @property
    def subtitle_label(self) -> str:
        return SOURCE_LABELS.get(self.subtitle_source, self.subtitle_source)


@dataclass
class NoteResult:
    """第 2 步的产物。body 是复查之后的版本。"""

    note_id: int
    path: Path
    body: str
    review: ReviewReport = field(default_factory=ReviewReport)


@dataclass
class QuizResult:
    """第 3 步的产物。questions 是复查之后留下来的那些。"""

    questions: list[Question] = field(default_factory=list)
    question_ids: list[int] = field(default_factory=list)
    review: ReviewReport = field(default_factory=ReviewReport)

    @property
    def summary(self) -> str:
        if not self.questions:
            return "0 道（这一节没有可考的 C 语言内容，多半是开场或导学课，跳过即可）"
        if not self.review.ok:
            return f"{len(self.questions)} 道（复查没成功，这批题没经过二次把关）"
        if self.review.removed:
            return f"{len(self.questions)} 道（复查删掉了 {len(self.review.removed)} 道废题）"
        return f"{len(self.questions)} 道"


@dataclass
class ImportResult:
    """一键跑完的全部产物。"""

    video: LoadedVideo
    note: NoteResult
    quiz: QuizResult

    @property
    def questions(self) -> list[Question]:
        return self.quiz.questions

    @property
    def question_ids(self) -> list[int]:
        return self.quiz.question_ids

    @property
    def summary(self) -> str:
        return (
            f"《{self.video.title}》\n"
            f"字幕：{self.video.subtitle_label}，{len(self.video.segments)} 条\n"
            f"笔记：{self.note.path.name}\n"
            f"题目：{self.quiz.summary}"
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
        self.reviewer = Reviewer(self.client, config)

    # ---------- 第 1 步：导入链接 ----------

    def load(self, link: str, on_progress: ProgressCallback | None = None) -> LoadedVideo:
        progress = on_progress or _noop

        bvid, page = parse_video_ref(link)
        # 登录态在这里读一次，视频信息和字幕都用它：
        # 有些视频不登录连标题都拿不到。
        credential = load_credential(credential_path(self.config))
        progress("正在获取视频信息…")
        info = fetch_video_info(bvid, page, credential=credential, config=self.config)
        log.info("视频：%s 第 %d P（%s）", bvid, page, info.display_title)

        video_id = self.db.upsert_video(
            bvid=bvid,
            page=page,
            title=info.display_title,
            url=info.url,
            duration=info.duration,
        )
        segments, source = self._resolve_subtitle(video_id, info, credential, progress)

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
        self,
        video_id: int,
        info: VideoInfo,
        credential,
        on_progress: ProgressCallback,
    ) -> tuple[list[Segment], str]:
        """按 缓存 → B站CC → 本地转写 的顺序拿字幕。

        中间任何一步失败都要继续往下退，不能因为字幕接口抽风就
        让整个导入失败——用户要的是字幕，从哪来的无所谓。
        """
        cached = self.db.get_subtitle_segments(video_id)
        if cached:
            on_progress(f"用已缓存的字幕（{len(cached)} 条）")
            return cached, "cache"

        subtitle_error: str | None = None

        if credential:
            on_progress("正在从 B 站获取字幕…")
            try:
                segments = fetch_subtitle(info, credential=credential, config=self.config)
            except Exception as exc:
                # 字幕这一步再出什么意外，都不该拖垮整个导入——退到语音转写就行。
                # BilibiliError 的消息本来就是写给人看的，直接用；其他异常记全栈。
                if isinstance(exc, BilibiliError):
                    subtitle_error = str(exc)
                    log.warning("获取 CC 字幕失败，改用语音转写：%s", exc)
                else:
                    subtitle_error = f"意外错误（{type(exc).__name__}）：{exc}"
                    log.warning("获取 CC 字幕时出现意外错误，改用语音转写", exc_info=True)
                on_progress("B 站字幕没拿到，改用本地语音转写…")
                segments = None
            if segments:
                self.db.save_subtitle(video_id, "bilibili_cc", segments)
                return segments, "bilibili_cc"
        else:
            subtitle_error = "还没有登录 B 站（双击 扫码登录.bat 扫码登录后就能拿到 CC 字幕）"
            on_progress("尚未登录 B 站，拿不到 CC 字幕，直接走语音转写")

        if not self.config.get("transcribe.enabled", True):
            if subtitle_error:
                raise PipelineError(
                    "抓 B 站字幕失败了，而语音转写已在 config.yaml 里关闭，这次没法继续。\n"
                    f"失败原因：{subtitle_error}\n"
                    "可以稍后重新导入试试，或者把 transcribe.enabled 改成 true，"
                    "让语音转写兜底。"
                )
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

    def _read_slides(self, video: LoadedVideo, progress: ProgressCallback) -> str:
        """下低清视频 → 抽关键帧 → 让 AI 读屏幕上的代码和课件。

        **这一步出任何问题都不该让笔记生成不了**：它要多下一次视频、多花几次
        AI 调用，网络、B 站接口、ffmpeg 版本都可能出岔子。真出岔子就退化成
        "只从字幕生成"——少点内容，总比什么都没有强。
        """
        if not self.config.get("frames.enabled", True):
            return ""

        frames_dir = self.config.path("frames")
        video_path = None
        try:
            video_path = download_video(video.url, frames_dir, self.config, progress)
            # **按视频分目录存**：都塞进一个 keyframes/ 的话，下一集导入时
            # 开跑前会清空，上一集的画面就找不回来了——而"留着能回看"正是
            # 留帧的意义（笔记里的代码是 AI 读的，想核对得看原图）。
            frames = extract_keyframes(
                video_path,
                frames_dir / "keyframes" / f"{video.bvid}_p{video.page}",
                self.config,
                progress,
            )
        except Exception as exc:
            log.warning("抽关键帧失败，这次只从字幕生成笔记：%s", exc)
            progress(f"抽关键帧没成功（{exc}），这次只从字幕生成笔记")
            return ""
        finally:
            # 视频文件几十上百 MB，抽完帧就删；帧留着，用户想看还能看
            if video_path is not None:
                try:
                    video_path.unlink()
                except OSError:
                    log.debug("删除临时视频失败", exc_info=True)

        if not frames:
            return ""

        try:
            return SlideReader(self.client, self.config).read(frames, progress)
        except Exception as exc:
            log.warning("读屏幕内容失败，这次只从字幕生成笔记：%s", exc)
            progress("读屏幕内容没成功，这次只从字幕生成笔记")
            return ""

    def make_notes(
        self, video: LoadedVideo, on_progress: ProgressCallback | None = None
    ) -> NoteResult:
        progress = on_progress or _noop
        # 先读屏幕内容：字幕只有老师说的话，代码和课件在屏幕上。
        # 这一步失败不影响生成笔记，退化成"只有字幕"就行。
        video.visual_text = self._read_slides(video, progress)

        progress("正在生成学习笔记…")
        body = self.note_generator.generate(
            video.segments, video.title, progress, video.visual_text
        )

        # 生成完先让 AI 复查一遍。写文件和入库的都是复查后的版本，
        # 免得没把关的内容落盘之后还得手工清。
        body, review = self.reviewer.review_note(body, video.segments, progress)
        if not review.ok:
            progress("笔记复查没成功，这一份没有经过二次把关")

        path = self._write_note_file(video, body)
        note_id = self.db.save_note(
            video_id=video.video_id, title=video.title, content=body, file_path=str(path)
        )
        return NoteResult(note_id=note_id, path=path, body=body, review=review)

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
    ) -> QuizResult:
        progress = on_progress or _noop
        progress("正在出题…")
        questions = self.quiz_generator.generate(note_body, progress)

        # 出完再复查一遍：答案错的改掉，广告题和废题删掉。
        # 只有活下来的才入库——用户拿到的必须是过了关的东西。
        questions, review = self.reviewer.review_questions(questions, note_body, progress)
        if not review.ok:
            progress("题目复查没成功，这批题没有经过二次把关")

        question_ids = self.db.save_questions(questions, note_id=note_id, source=source)
        return QuizResult(questions=questions, question_ids=question_ids, review=review)

    # ---------- 一键跑完 ----------

    def run(self, link: str, on_progress: ProgressCallback | None = None) -> ImportResult:
        video = self.load(link, on_progress)
        note = self.make_notes(video, on_progress)
        quiz = self.make_quiz(
            note.note_id, note.body, f"video:{video.bvid}#p{video.page}", on_progress
        )
        result = ImportResult(video=video, note=note, quiz=quiz)
        log.info("导入完成：%s", result.summary.replace("\n", " | "))
        return result
