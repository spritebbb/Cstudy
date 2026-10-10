"""出题：基于笔记生成 4 种题型的题目。"""

from __future__ import annotations

from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..models import Question, parse_questions
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

TYPES = ("choice", "fill", "reading", "coding")


class QuizGenerator:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.counts = {kind: int(config.get(f"quiz.{kind}", 0) or 0) for kind in TYPES}

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def generate(
        self, notes: str, on_progress: ProgressCallback | None = None
    ) -> list[Question]:
        """根据笔记出题。返回校验通过的 Question 列表。"""
        if not notes.strip():
            raise ValueError("笔记内容是空的，无法出题")

        if on_progress:
            on_progress(f"正在出题（共 {self.total} 道）…")
        log.info("请求出题：%s", self.counts)

        raw = self.client.chat_json(
            [
                {"role": "system", "content": prompts.QUIZ_SYSTEM},
                {"role": "user", "content": prompts.quiz_user_prompt(notes, self.counts)},
            ]
        )
        questions = parse_questions(raw)

        # 数量对不上不算失败，模型偶尔会多给或少给，记一笔就够了
        for kind in TYPES:
            got = sum(1 for q in questions if q.type == kind)
            want = self.counts[kind]
            if got != want:
                log.warning("题型 %s 想要 %d 道，实际拿到 %d 道", kind, want, got)

        log.info("出题完成，共 %d 道", len(questions))
        return questions
