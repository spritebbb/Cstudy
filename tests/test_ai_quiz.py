"""出题数量策略的测试。

题量由 config.yaml 的 quiz.count 决定：auto 让 AI 按内容定（代码强制范围兜底），
数字则固定。以前是写死 7 道，跟内容多少完全脱钩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import prompts  # noqa: E402
from cstudy.ai.quiz import QuizGenerator, build_quiz_plan  # noqa: E402
from cstudy.config import Config  # noqa: E402


class FakeClient:
    """按调用顺序返回预设回复，用来替代 DeepSeekClient。"""

    def __init__(self, replies) -> None:
        self.replies = replies if isinstance(replies, list) else [replies]
        self.calls: list = []

    def chat(self, messages, temperature=None, json_mode=False):
        self.calls.append(messages)
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def chat_json(self, messages, temperature=None):
        return self.chat(messages, json_mode=True)


def make_config(quiz: dict | None = None) -> Config:
    return Config({"quiz": quiz or {}, "paths": {}}, Path("."))


def questions_json(count: int) -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "type": "choice",
                    "difficulty": "easy",
                    "point": "测试点",
                    "question": f"第 {i} 题？",
                    "options": ["A. 甲", "B. 乙"],
                    "answer": "A",
                    "explanation": "因为甲",
                }
                for i in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


class TestBuildQuizPlan:
    def test_defaults_to_auto(self):
        plan = build_quiz_plan(make_config())
        assert plan.mode == "auto"
        assert (plan.min_total, plan.max_total) == (5, 12)

    def test_reads_auto_explicitly(self):
        plan = build_quiz_plan(make_config({"count": "auto", "min_count": 3, "max_count": 8}))
        assert plan.mode == "auto"
        assert plan.cap == 8

    def test_fixed_count(self):
        plan = build_quiz_plan(make_config({"count": 6}))
        assert plan.mode == "fixed"
        assert plan.cap == 6

    def test_garbage_count_falls_back_to_auto(self):
        """配置写错不该让软件跑不起来。"""
        plan = build_quiz_plan(make_config({"count": "随便写的"}))
        assert plan.mode == "auto"

    def test_max_smaller_than_min_is_corrected(self):
        plan = build_quiz_plan(make_config({"min_count": 10, "max_count": 4}))
        assert plan.max_total == 10

    def test_prefer_reads_new_keys_and_keeps_defaults(self):
        plan = build_quiz_plan(make_config({"prefer": {"coding": 5}}))
        assert plan.counts["coding"] == 5
        assert plan.counts["choice"] == 3  # 没写的用默认值


class TestQuizGeneratorRange:
    def test_truncates_when_over_max(self):
        client = FakeClient(questions_json(20))
        gen = QuizGenerator(client, make_config({"count": "auto", "max_count": 8}))
        assert len(gen.generate("笔记内容")) == 8

    def test_does_not_pad_when_under_min(self):
        """出少了不补题——少几道练着，比硬凑出废题强。"""
        client = FakeClient(questions_json(2))
        gen = QuizGenerator(client, make_config({"count": "auto", "min_count": 5}))
        assert len(gen.generate("笔记内容")) == 2

    def test_fixed_mode_truncates_to_target(self):
        client = FakeClient(questions_json(10))
        gen = QuizGenerator(client, make_config({"count": 4}))
        assert len(gen.generate("笔记内容")) == 4

    def test_empty_notes_rejected(self):
        with pytest.raises(ValueError):
            QuizGenerator(FakeClient("{}"), make_config()).generate("   ")


class TestQuizUserPrompt:
    def test_auto_mentions_the_range(self):
        plan = build_quiz_plan(make_config({"count": "auto", "min_count": 4, "max_count": 9}))
        text = prompts.quiz_user_prompt("笔记", plan)
        assert "题量由你决定" in text
        assert "4~9" in text

    def test_fixed_mentions_the_target(self):
        plan = build_quiz_plan(make_config({"count": 6}))
        text = prompts.quiz_user_prompt("笔记", plan)
        assert "期望数量" in text
        assert "选择题 3 道" in text


class TestPromptRules:
    """提示词就是这个项目的"代码"，关键规则掉了要能立刻发现。"""

    def test_duplicate_questions_are_forbidden_in_both_places(self):
        """实测踩过：同一个知识点被问了两遍（两道题都在问"C 语言之父是谁"）。

        出题和复查两处都要明确禁止，光靠模型自觉不行。
        """
        assert "重复" in prompts.QUIZ_SYSTEM
        assert "重复" in prompts.QUIZ_REVIEW_SYSTEM

    def test_review_allows_deleting_everything(self):
        """全是废题时删光是正确结果，提示词必须说清楚。"""
        assert "删光" in prompts.QUIZ_REVIEW_SYSTEM
