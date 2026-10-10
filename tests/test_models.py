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


class TestExamType:
    """考试是**四类**题型，而库里的 type 只有三类——这里把差的那些分出来。

    用户考的那所（湖北工程学院）公布的题型分值：填空 48 / 程序填空 36 /
    程序阅读 35 / 程序设计 31，加起来正好 150。**程序填空是独立的一道大题**，
    跟普通填空分开算分，考的能力也不一样，所以必须分得开。
    """

    def make(self, text: str, qtype: str = "fill") -> Question:
        return Question(type=qtype, question=text, answer="1")

    def test_multi_blank_code_is_a_code_fill(self):
        """代码里挖了好几个空 —— 这是正儿八经的程序填空。"""
        text = (
            "#include <stdio.h>\nint main() {\n"
            "    int a, b, max;\n"
            "    ______(\"请输入两个数\");\n"
            "    scanf(\"%d%d\", &a, &b);\n"
            "    max = a;\n"
            "    if (max < b) max = ______;\n"
            '    printf("%d", ______);\n}'
        )

        assert self.make(text).exam_type == "code_fill"

    def test_single_blank_code_with_a_fill_instruction_is_a_code_fill(self):
        """只挖一个空，但题干说了"请填空"——那也是程序填空。"""
        text = "以下程序调用 findmax 函数返回数组中的最大值，请填空。\nfindmax(int *a,int n) { int *p,*s; for(p=a,s=a; ______ ) ...}"

        assert self.make(text).exam_type == "code_fill"

    def test_plain_fill_with_code_as_background_stays_a_fill(self):
        """带代码的普通填空：代码只是**背景**，空在问句里，考的还是知不知道。"""
        text = "在C语言中，有如下语句： int a[3][2]={1,2,3,4,5,6}; 则 a[0][0] 的值为______。"

        assert self.make(text).exam_type == "fill"

    def test_plain_fill_without_any_code(self):
        text = "表达式 21 % 4 的值为 ______。"

        assert self.make(text).exam_type == "fill"

    def test_read_the_program_and_write_the_output_is_reading(self):
        """**这批题当初被标成了 fill**（因为题干里有个 `____`），实际是程序阅读题。

        "以下程序运行后的输出结果是 ______" —— 空在**问句**里，
        让你写运行结果，不是让你填代码。
        """
        text = (
            "以下程序运行后的输出结果是 ______ 。\n"
            "main()\n{ int x=2006,y=2007;\n  printf(\"%d\n\",(x,y));\n }"
        )

        assert self.make(text).exam_type == "reading"

    def test_reading_typed_questions_stay_reading(self):
        text = "阅读下面程序，写出运行后输出的结果。\n#include <stdio.h>\nint main() { return 0; }"

        assert self.make(text, "reading").exam_type == "reading"

    def test_coding_stays_coding(self):
        assert self.make("编写一个完整的程序，输出 Hello World。", "coding").exam_type == "coding"

    def test_labels_are_readable(self):
        assert self.make("表达式 1+1 的值为 ______。").exam_type_label == "填空题"
        assert self.make("int main() { int a=____, b=____; }").exam_type_label == "程序填空题"

    def test_empty_parens_in_code_are_not_a_blank(self):
        """`main()` 里的空括号到处都是，**不能当空算**——一认就全乱。"""
        text = "以下程序运行后的输出结果是 ______ 。\nint main() { printf(\"hi\"); return 0; }"

        assert self.make(text).exam_type == "reading"


class TestBlankMarkers:
    """空的写法在真实材料里花样很多，认漏一种就有一批题被判错。

    小绿书用 `【1】` `【2】` 编号空（225 道），有的卷子直接把"填空6"写进代码。
    """

    def make(self, text: str) -> Question:
        return Question(type="fill", question=text, answer="1")

    def test_numbered_square_bracket_blanks_are_recognised(self):
        """小绿书就是这么标空的。"""
        text = (
            "#include <stdio.h>\nint main() {\n"
            "    int a = 【1】, b = 【2】;\n"
            '    printf("%d", a + b);\n}'
        )

        assert self.make(text).exam_type == "code_fill"

    def test_the_word_fill_as_a_placeholder_is_recognised(self):
        """有的卷子在代码里直接写"填空6""填空7"当占位符。"""
        text = (
            "以下程序的功能是：对数组冒泡排序。请填空：\n"
            "#include <stdio.h>\nvoid sort( 填空6 )\n{\n  int i,j;\n  填空7\n  ...\n}"
        )

        assert self.make(text).exam_type == "code_fill"

    def test_a_numbered_blank_in_prose_is_still_a_plain_fill(self):
        """`【1】` 出现在纯文字题里，也得当成空算——只是没有代码，还是普通填空。"""
        text = "C 语言的源程序必须通过【1】和【2】后，才能被计算机执行。"

        assert self.make(text).exam_type == "fill"

    def test_paper_numbering_is_not_a_blank(self):
        """`【题 6.74】` 是题号，不是空。"""
        text = "【题 6.74】下面程序的功能是输出方阵。请填空。\nint main() { ... }"

        assert self.make(text).exam_type == "fill"
