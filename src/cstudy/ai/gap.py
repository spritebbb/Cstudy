"""针对考纲上题量不足的知识点补题。

覆盖度一查就露馅：有些知识点只有一两道题（甚至一道没有），而那些往往正是考试的重头
（指针、文件、函数）。这里按知识点出题。

跟 `quiz.py` 那条路不同：那条是**从课程笔记**出题，有原文可依；这条**没有笔记**，
只有知识点本身，所以提示词里要求按"这个知识点真题一般怎么考"来出。
"""

from __future__ import annotations

from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..models import TYPE_LABELS, Question, parse_questions
from . import prompts
from .client import DeepSeekClient
from .quiz import build_quiz_plan

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_COUNT = 6


class GapFiller:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.plan = build_quiz_plan(config)
        raw = config.get("gap.count", DEFAULT_COUNT)
        try:
            self.count = max(1, int(raw))
        except (TypeError, ValueError):
            log.warning("gap.count 配置看不懂（%r），用默认值 %d", raw, DEFAULT_COUNT)
            self.count = DEFAULT_COUNT

    def generate(
        self,
        point_name: str,
        chapter: str = "",
        count: int | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> list[Question]:
        """给一个知识点出一批题。出不出来就返回空列表，不抛异常。"""
        want = count or self.count
        preference = "、".join(
            f"{TYPE_LABELS.get(kind, kind)} {number} 道"
            for kind, number in self.plan.counts.items()
            if number
        ) or "填空题为主"

        if on_progress:
            on_progress(f"正在给「{point_name}」出题…")

        raw = self.client.chat_json(
            [
                {"role": "system", "content": prompts.GAP_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.gap_user_prompt(point_name, chapter, preference, want),
                },
            ]
        )
        questions = parse_questions(raw)

        # **每道题都必须有答案**——用户明确要求过。`parse_questions` 已经在
        # 构造 Question 时挡掉了空答案，这里再兜一道，顺便把"答案不像答案"的也剔掉。
        kept: list[Question] = []
        for question in questions:
            if not question.answer.strip():
                log.warning("补题：跳过一道没有答案的题：%.40s", question.question)
                continue
            kept.append(question)

        # 补题时知识点是给定的，不该让模型自己写——统一盖成考纲上的名字
        for question in kept:
            question.point = point_name

        log.info("给「%s」出了 %d 道题", point_name, len(kept))
        return kept[:want]
