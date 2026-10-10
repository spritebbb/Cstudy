"""判分逻辑的测试。

这里是"选择题自动判分"的命门：AI 给的答案格式五花八门，
规范化写歪了会把对的判成错的——那比不判分更伤人。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.grading import (  # noqa: E402
    describe_expected,
    grade_choice,
    normalize_choice_answer,
    strip_option_prefix,
    to_halfwidth,
)

OPTIONS = ["A. 甲", "B. 乙", "C. 丙", "D. 丁"]


class TestToHalfwidth:
    def test_converts_fullwidth_letters_and_digits(self):
        assert to_halfwidth("Ｂ") == "B"
        assert to_halfwidth("１２３") == "123"

    def test_leaves_chinese_alone(self):
        assert to_halfwidth("答案") == "答案"

    def test_handles_empty(self):
        assert to_halfwidth("") == ""


class TestStripOptionPrefix:
    def test_strips_common_separators(self):
        assert strip_option_prefix("A. 内容") == "内容"
        assert strip_option_prefix("B、内容") == "内容"
        assert strip_option_prefix("C) 内容") == "内容"
        assert strip_option_prefix("D）内容") == "内容"
        assert strip_option_prefix("A 内容") == "内容"

    def test_keeps_normal_words_intact(self):
        """'Array 的首地址' 不能被削成 'rray 的首地址'。"""
        assert strip_option_prefix("Array 的首地址") == "Array 的首地址"
        assert strip_option_prefix("指针是什么") == "指针是什么"

    def test_handles_empty(self):
        assert strip_option_prefix("") == ""


class TestNormalizeChoiceAnswer:
    def test_plain_letter(self):
        assert normalize_choice_answer("B", 4) == "B"

    def test_lowercase(self):
        assert normalize_choice_answer("b", 4) == "B"

    def test_fullwidth(self):
        assert normalize_choice_answer("Ｂ", 4) == "B"

    def test_letter_with_option_text(self):
        assert normalize_choice_answer("B. 乙", 4) == "B"

    def test_with_chinese_noise_prefix(self):
        assert normalize_choice_answer("答案：B", 4) == "B"
        assert normalize_choice_answer("答案是B", 4) == "B"
        assert normalize_choice_answer("选 B", 4) == "B"
        assert normalize_choice_answer("正确选项是C", 4) == "C"

    def test_letter_glued_to_chinese(self):
        assert normalize_choice_answer("选项B", 4) == "B"

    def test_rejects_letter_outside_option_range(self):
        """只有 4 个选项时，'E' 不可能是答案。"""
        assert normalize_choice_answer("E", 4) is None

    def test_rejects_garbage(self):
        assert normalize_choice_answer("", 4) is None
        assert normalize_choice_answer("不知道", 4) is None
        assert normalize_choice_answer("……", 4) is None

    def test_without_option_count_accepts_any_letter(self):
        assert normalize_choice_answer("E") == "E"


class TestDescribeExpected:
    def test_maps_letter_back_to_option_text(self):
        assert describe_expected(OPTIONS, "B") == "B. 乙"

    def test_unknown_letter_returned_as_is(self):
        assert describe_expected(OPTIONS, "Z") == "Z"

    def test_empty(self):
        assert describe_expected(OPTIONS, "") == ""


class TestGradeChoice:
    def test_correct_choice(self):
        result = grade_choice(1, OPTIONS, "B")  # 下标 1 → B
        assert result.correct is True
        assert result.expected == "B"
        assert result.can_auto_grade

    def test_wrong_choice(self):
        result = grade_choice(0, OPTIONS, "B")  # 选 A，答案 B
        assert result.correct is False
        assert result.expected == "B"

    def test_index_is_converted_to_letter_not_compared_as_index(self):
        """用户选下标 2（C），答案写 'C' —— 要判对。

        如果拿下标 2 直接跟字母比就会判错，这是最容易犯的错。
        """
        assert grade_choice(2, OPTIONS, "C").correct is True

    def test_tolerates_messy_answer_format(self):
        assert grade_choice(1, OPTIONS, "答案：B").correct is True
        assert grade_choice(1, OPTIONS, "b").correct is True

    def test_no_selection_counts_as_wrong(self):
        result = grade_choice(-1, OPTIONS, "B")
        assert result.correct is False
        assert "没有选中" in result.note

    def test_unparseable_answer_degrades_to_self_grading(self):
        """认不出来就老实说认不出来，不猜。"""
        result = grade_choice(0, OPTIONS, "见解析")
        assert result.correct is None
        assert not result.can_auto_grade
        assert "自己对照" in result.note
