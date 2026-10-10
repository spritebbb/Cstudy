"""出题：基于笔记生成 4 种题型的题目。

题量由 config.yaml 的 `quiz.count` 决定：
    auto  —— 让 AI 按这节课的内容多少自己定，代码只强制一个范围兜底
    数字  —— 固定出这么多道

不管哪种模式，"题型偏好"（quiz.prefer.*）都只是给 AI 的参考，不是硬性数量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..models import Question, parse_questions
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

TYPES = ("choice", "fill", "reading", "coding")

# 配置缺失或写错时的兜底值。用户改了 config.yaml 也不会因为一个笔误就跑不起来。
DEFAULT_MIN_TOTAL = 5
DEFAULT_MAX_TOTAL = 12
DEFAULT_PREFER = {"choice": 3, "fill": 2, "reading": 1, "coding": 1}


@dataclass
class QuizPlan:
    """出题数量的策略。

    mode         "auto"（AI 按内容定）或 "fixed"（固定总数）
    counts       题型偏好，两种模式下都只是"大致比例参考"
    min_total     auto 模式下的下限
    max_total     auto 模式下的上限
    total_target  fixed 模式下的目标总数
    """

    mode: str = "auto"
    counts: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PREFER))
    min_total: int = DEFAULT_MIN_TOTAL
    max_total: int = DEFAULT_MAX_TOTAL
    total_target: int | None = None

    @property
    def cap(self) -> int | None:
        """代码层面强制的题数上限。超过就截断——不能只靠提示词约束。"""
        return self.total_target if self.mode == "fixed" else self.max_total


def _positive_int(config: Config, key: str, default: int) -> int:
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("%s 配置看不懂（%r），用默认值 %d", key, raw, default)
        return default
    if value <= 0:
        log.warning("%s 是 %d，没意义，用默认值 %d", key, value, default)
        return default
    return value


def build_quiz_plan(config: Config) -> QuizPlan:
    """从 config.yaml 读策略。配置写错不抛异常，退回默认值并记日志。"""
    prefer: dict[str, int] = {}
    for kind in TYPES:
        raw = config.get(f"quiz.prefer.{kind}", DEFAULT_PREFER[kind])
        try:
            prefer[kind] = max(0, int(raw))
        except (TypeError, ValueError):
            log.warning(
                "quiz.prefer.%s 配置看不懂（%r），用默认值 %d",
                kind, raw, DEFAULT_PREFER[kind],
            )
            prefer[kind] = DEFAULT_PREFER[kind]

    min_total = _positive_int(config, "quiz.min_count", DEFAULT_MIN_TOTAL)
    max_total = _positive_int(config, "quiz.max_count", DEFAULT_MAX_TOTAL)
    if max_total < min_total:
        log.warning("quiz.max_count(%d) 比 min_count(%d) 还小，按 min_count 处理", max_total, min_total)
        max_total = min_total

    raw_count = config.get("quiz.count", "auto")
    if isinstance(raw_count, str) and raw_count.strip().lower() == "auto":
        return QuizPlan("auto", prefer, min_total, max_total)

    try:
        target = int(raw_count)
    except (TypeError, ValueError):
        log.warning("quiz.count 配置看不懂（%r），按 auto 处理", raw_count)
        return QuizPlan("auto", prefer, min_total, max_total)

    if target <= 0:
        log.warning("quiz.count 是 %d，没意义，按 auto 处理", target)
        return QuizPlan("auto", prefer, min_total, max_total)

    return QuizPlan("fixed", prefer, min_total, max_total, total_target=target)


class QuizGenerator:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.plan = build_quiz_plan(config)

    @property
    def counts(self) -> dict[str, int]:
        """题型偏好。保留这个名字，界面和日志里都在用。"""
        return self.plan.counts

    @property
    def total(self) -> int:
        return sum(self.plan.counts.values())

    def generate(
        self, notes: str, on_progress: ProgressCallback | None = None
    ) -> list[Question]:
        """根据笔记出题。返回校验通过的 Question 列表（可能是空的）。"""
        if not notes.strip():
            raise ValueError("笔记内容是空的，无法出题")

        if on_progress:
            on_progress(self._progress_text())
        log.info("请求出题：mode=%s prefer=%s", self.plan.mode, self.plan.counts)

        raw = self.client.chat_json(
            [
                {"role": "system", "content": prompts.QUIZ_SYSTEM},
                {"role": "user", "content": prompts.quiz_user_prompt(notes, self.plan)},
            ]
        )
        questions = parse_questions(raw)
        questions = self._enforce_cap(questions)

        # 出少了不补题：少几道练着，比硬凑出废题强。只记一笔便于回头看。
        expected = self.plan.cap
        if expected and len(questions) < expected:
            log.info("出题 %d 道，少于期望的 %d 道（不补题）", len(questions), expected)

        log.info("出题完成，共 %d 道", len(questions))
        return questions

    def _progress_text(self) -> str:
        if self.plan.mode == "auto":
            return (
                f"正在出题（题量按内容多少决定，"
                f"{self.plan.min_total}~{self.plan.max_total} 道）…"
            )
        return f"正在出题（共 {self.plan.total_target} 道）…"

    def _enforce_cap(self, questions: list[Question]) -> list[Question]:
        cap = self.plan.cap
        if cap and len(questions) > cap:
            log.warning("AI 出了 %d 道，超过上限 %d，截断", len(questions), cap)
            return questions[:cap]
        return questions
