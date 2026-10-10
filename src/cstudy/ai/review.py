"""AI 二次校准：生成完的笔记和题目，再让 AI 自己核对一遍。

第一遍出错、跑偏、把广告当知识点，都由这一步兜住。用户不会 C 语言、
判不了对错，也负担不起逐题核对，所以这道关必须机器来把。

**复查失败不是致命错误**：原来的内容照常保留，只是这一轮没人把关而已。
所以这里一律不抛异常，用 `ReviewReport(ok=False, error=…)` 表达。
"""

from __future__ import annotations

import json
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..models import (
    Question,
    ReviewNote,
    ReviewReport,
    parse_json_object,
    parse_questions,
    parse_review_report,
)
from ..subtitle import Segment, plain_text
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_MAX_SOURCE_CHARS = 40000
DEFAULT_MAX_NOTE_CHARS = 8000


def _question_to_dict(question: Question) -> dict:
    """转成送给 AI 复查的 JSON 形状，和出题时保持一致。"""
    return {
        "type": question.type,
        "difficulty": question.difficulty,
        "point": question.point,
        "question": question.question,
        "options": question.options,
        "answer": question.answer,
        "explanation": question.explanation,
    }


def _with_subject_text(notes: list[ReviewNote], questions: list[Question]) -> list[ReviewNote]:
    """AI 只给了序号时，把题干补上——报告里光看序号认不出是哪道题。"""
    filled: list[ReviewNote] = []
    for note in notes:
        subject = note.subject
        if subject == "（没说是什么）" and 1 <= note.index <= len(questions):
            subject = questions[note.index - 1].question
        filled.append(ReviewNote(subject=subject, reason=note.reason, index=note.index))
    return filled


class Reviewer:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.enabled = bool(config.get("review.enabled", True))
        self.max_source_chars = int(
            config.get("review.max_source_chars", DEFAULT_MAX_SOURCE_CHARS)
            or DEFAULT_MAX_SOURCE_CHARS
        )
        self.max_note_chars = int(
            config.get("review.max_note_chars", DEFAULT_MAX_NOTE_CHARS)
            or DEFAULT_MAX_NOTE_CHARS
        )

    # ---------- 笔记 ----------

    def review_note(
        self,
        body: str,
        segments: list[Segment],
        on_progress: ProgressCallback | None = None,
    ) -> tuple[str, ReviewReport]:
        """复查笔记，返回 (修正后的笔记, 复查报告)。

        **带上原始字幕一起送**——不带就只能校对文字，查不出"笔记编造了字幕里
        没有的内容"，而编造恰恰是最危险的一种错。
        """
        if not self.enabled:
            return body, ReviewReport()

        if on_progress:
            on_progress("正在复查笔记…")

        source = plain_text(segments)
        if len(source) > self.max_source_chars:
            log.warning(
                "字幕太长（%d 字），复查时截断到 %d 字",
                len(source), self.max_source_chars,
            )
            source = source[: self.max_source_chars]

        try:
            raw = self.client.chat_json(
                [
                    {"role": "system", "content": prompts.NOTE_REVIEW_SYSTEM},
                    {"role": "user", "content": prompts.note_review_user_prompt(body, source)},
                ]
            )
            data = parse_json_object(raw)
        except Exception as exc:
            return body, self._failure("复查笔记", exc)

        revised = str(data.get("revised_note") or "").strip()
        if not revised:
            # 复查把笔记弄没了，宁可要原来那份
            log.warning("复查返回的笔记是空的，保留原笔记")
            return body, ReviewReport(ok=False, error="复查返回的笔记内容为空")

        report = parse_review_report(data)
        log.info(
            "笔记复查完成：修正 %d 处，删除 %d 处",
            len(report.fixed), len(report.removed),
        )
        return revised, report

    # ---------- 题目 ----------

    def review_questions(
        self,
        questions: list[Question],
        note_body: str,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[list[Question], ReviewReport]:
        """复查题目，返回 (保留下来的题目, 复查报告)。

        带笔记一起送——判断答案对不对必须有依据，不能凭印象。
        """
        if not self.enabled or not questions:
            return questions, ReviewReport()

        if on_progress:
            on_progress("正在复查题目…")

        notes = note_body
        if len(notes) > self.max_note_chars:
            log.warning(
                "笔记太长（%d 字），复查时截断到 %d 字", len(notes), self.max_note_chars
            )
            notes = notes[: self.max_note_chars]

        import json

        payload = json.dumps(
            [_question_to_dict(q) for q in questions], ensure_ascii=False, indent=2
        )

        try:
            raw = self.client.chat_json(
                [
                    {"role": "system", "content": prompts.QUIZ_REVIEW_SYSTEM},
                    {"role": "user", "content": prompts.quiz_review_user_prompt(notes, payload)},
                ]
            )
            data = parse_json_object(raw)
            kept = parse_questions(data)
            report = parse_review_report(data)
        except Exception as exc:
            return questions, self._failure("复查题目", exc)

        if not kept and not report.removed:
            # 题全没了却一条删除理由都没给——多半是返回格式出了问题，
            # 不是真的"全是废题"。这种情况退回原题更安全。
            log.warning("复查返回了空题目列表却没说明删了什么，按复查失败处理")
            return questions, ReviewReport(ok=False, error="复查返回的内容不完整")

        report.removed = _with_subject_text(report.removed, questions)
        report.fixed = _with_subject_text(report.fixed, questions)
        log.info(
            "题目复查完成：保留 %d 道，删除 %d 道，修正 %d 道",
            len(kept), len(report.removed), len(report.fixed),
        )
        return kept, report

    # ---------- 失败兜底 ----------

    def _failure(self, what: str, exc: Exception) -> ReviewReport:
        """复查本身出问题时，说人话地记一笔，绝不往上抛。"""
        log.warning("%s没成功：%s: %s", what, type(exc).__name__, exc)
        return ReviewReport(ok=False, error=f"{type(exc).__name__}：{exc}")
