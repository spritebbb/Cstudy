"""AI 返回结果校验的测试。

模型返回的 JSON 不可信，这里是入库前的最后一道关。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.models import DataError, Question, parse_questions  # noqa: E402


def valid_choice(**overrides) -> dict:
    base = {
        "type": "choice",
        "question": "int 占几个字节？",
        "options": ["A. 1", "B. 2", "C. 4", "D. 8"],
        "answer": "C",
        "explanation": "32 位平台上 int 通常是 4 字节。",
        "difficulty": "easy",
        "point": "基本数据类型",
    }
    base.update(overrides)
    return base


class TestQuestion:
    def test_valid_question_passes(self):
        q = Question(**valid_choice())
        assert q.type_label == "选择题"
        assert q.difficulty == "easy"

    def test_unknown_type_rejected(self):
        with pytest.raises(DataError, match="题目类型"):
            Question(**valid_choice(type="essay"))

    def test_empty_question_rejected(self):
        with pytest.raises(DataError, match="题干是空的"):
            Question(**valid_choice(question="   "))

    def test_missing_answer_rejected(self):
        with pytest.raises(DataError, match="没有答案"):
            Question(**valid_choice(answer=""))

    def test_choice_needs_at_least_two_options(self):
        with pytest.raises(DataError, match="选项少于 2 个"):
            Question(**valid_choice(options=["A. 只有一个"]))

    def test_bad_difficulty_falls_back_to_medium(self):
        # 难度标错不值得整道题作废
        q = Question(**valid_choice(difficulty="很难"))
        assert q.difficulty == "medium"

    def test_fill_question_needs_no_options(self):
        q = Question(type="fill", question="int a = ____;", answer="0")
        assert q.options == []

    def test_to_row_serialises_options_as_json(self):
        row = Question(**valid_choice()).to_row()
        assert json.loads(row["options"]) == ["A. 1", "B. 2", "C. 4", "D. 8"]

    def test_to_row_leaves_options_null_when_empty(self):
        row = Question(type="fill", question="x", answer="y").to_row()
        assert row["options"] is None


class TestParseQuestions:
    def test_parses_plain_json_string(self):
        payload = json.dumps({"questions": [valid_choice()]})
        questions = parse_questions(payload)
        assert len(questions) == 1
        assert questions[0].answer == "C"

    def test_parses_bare_list(self):
        questions = parse_questions([valid_choice()])
        assert len(questions) == 1

    def test_strips_markdown_code_fence(self):
        payload = "```json\n" + json.dumps({"questions": [valid_choice()]}) + "\n```"
        assert len(parse_questions(payload)) == 1

    def test_invalid_json_raises_with_hint(self):
        with pytest.raises(DataError, match="不是合法 JSON"):
            parse_questions("{这不是 JSON")

    def test_bad_question_is_skipped_not_fatal(self):
        payload = {"questions": [valid_choice(), {"type": "choice", "question": "缺答案"}]}
        questions = parse_questions(payload)
        assert len(questions) == 1

    def test_all_bad_raises_with_details(self):
        with pytest.raises(DataError, match="一道题都没解析出来"):
            parse_questions({"questions": [{"type": "choice", "question": "缺答案"}]})

    def test_empty_list_is_valid_not_an_error(self):
        """AI 明确回空列表，表示"这节没有可考的 C 语言内容"（比如开场课）。

        出 0 道题是正确结果——比硬凑出广告题好得多。只有"给了题目但全解析失败"
        才算错误（见上一个用例）。
        """
        assert parse_questions({"questions": []}) == []

    def test_non_list_raises(self):
        with pytest.raises(DataError, match="应该是一个列表"):
            parse_questions({"questions": "不是列表"})


class TestQuestionFromRow:
    """从数据库的一行还原题目。options 存的是 JSON 字符串，这里要解回来。"""

    @staticmethod
    def _row(**overrides) -> dict:
        base = {
            "type": "choice",
            "difficulty": "easy",
            "point": "字符与 ASCII",
            "question": "选哪个",
            "options": '["A. 甲", "B. 乙"]',
            "answer": "A",
            "explanation": "因为甲",
        }
        return {**base, **overrides}

    def test_roundtrips_through_to_row(self):
        original = Question(
            type="choice",
            question="选哪个",
            answer="A",
            options=["A. 甲", "B. 乙"],
            explanation="因为甲",
            difficulty="hard",
            point="指针",
        )
        assert Question.from_row(original.to_row()) == original

    def test_parses_options_json(self):
        assert Question.from_row(self._row()).options == ["A. 甲", "B. 乙"]

    def test_broken_options_json_degrades_to_fill(self):
        """库里一条坏数据不该让整个界面崩掉。

        选项没了自动判分就不可能，退化成填空题——至少还能对着参考答案自己看。
        """
        question = Question.from_row(self._row(options="{不是 JSON"))
        assert question.options == []
        assert question.type == "fill"

    def test_choice_with_too_few_options_degrades_to_fill(self):
        question = Question.from_row(self._row(options='["只有一个"]'))
        assert question.type == "fill"

    def test_null_options_becomes_empty(self):
        row = self._row(options=None, type="fill")
        assert Question.from_row(row).options == []

    def test_missing_columns_fall_back_to_defaults(self):
        question = Question.from_row({"type": "fill", "question": "甲", "answer": "1"})
        assert question.difficulty == "medium"
        assert question.point == ""
        assert question.options == []

    def test_type_is_lowercased(self):
        assert Question.from_row(self._row(type="CHOICE")).type == "choice"
