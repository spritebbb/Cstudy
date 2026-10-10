"""题库导入的测试。

重点是**切块**：按字符硬切会把一道题劈成两半，AI 拿到半道题只能丢掉，
用户就少了几道题还不知道。所以必须按题号切。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai.client import AIError  # noqa: E402
from cstudy.ai.importer import QuestionImporter, dedupe, split_by_questions  # noqa: E402
from cstudy.config import Config, ConfigError  # noqa: E402
from cstudy.models import DataError, Question  # noqa: E402


class FakeClient:
    """按调用顺序返回预设回复；raises 不为空时每次都抛。"""

    def __init__(self, replies=None, raises=None) -> None:
        if replies is None:
            self.replies: list = []
        elif isinstance(replies, list):
            self.replies = replies
        else:
            self.replies = [replies]
        self.raises = raises
        self.calls: list = []

    def chat(self, messages, temperature=None, json_mode=False):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def chat_json(self, messages, temperature=None):
        return self.chat(messages, json_mode=True)


def make_config(import_cfg: dict | None = None) -> Config:
    return Config({"import": import_cfg or {}, "paths": {}}, Path("."))


def questions_json(count: int, prefix: str = "题", qtype: str = "fill") -> str:
    """造一批题。默认用 fill——选择题会被默认过滤器滤掉，测试里要能留下来。"""
    return json.dumps(
        {
            "questions": [
                {
                    "type": qtype,
                    "difficulty": "easy",
                    "point": "测试点",
                    "question": f"{prefix}{i}",
                    "options": ["A. 甲", "B. 乙"] if qtype == "choice" else [],
                    "answer": "A" if qtype == "choice" else "42",
                    "explanation": "因为甲",
                }
                for i in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


def make_questions(count: int, prefix: str = "题") -> list[Question]:
    return [
        Question(type="fill", question=f"{prefix}{i}", answer="42")
        for i in range(1, count + 1)
    ]


class TestSplitByQuestions:
    def test_single_chunk_when_under_limit(self):
        text = "1. 第一题\nA. 甲\n2. 第二题\nA. 乙\n"
        chunks = split_by_questions(text, max_chars=10**6)
        assert len(chunks) == 1
        assert "第一题" in chunks[0] and "第二题" in chunks[0]

    def test_splits_when_over_limit_and_never_mid_question(self):
        text = "".join(f"{i}. {'x' * 50}\n" for i in range(1, 11))
        chunks = split_by_questions(text, max_chars=200)

        assert len(chunks) > 1
        for chunk in chunks:
            # 每块都要从题号开头——劈在题目中间就会丢掉那题
            assert chunk.strip()[0].isdigit()

    def test_no_question_number_returns_whole_text(self):
        assert split_by_questions("随便一段话，没有题号", 100) == ["随便一段话，没有题号"]

    def test_preamble_before_first_question_is_kept(self):
        """第一个题号之前的"一、选择题"这类说明不能丢。"""
        chunks = split_by_questions("一、选择题\n1. 第一题\n", max_chars=10**6)
        assert "一、选择题" in chunks[0]

    def test_recognises_chinese_style_numbering(self):
        chunks = split_by_questions("第1题 甲\n第2题 乙\n", max_chars=5)
        assert len(chunks) == 2

    def test_recognises_various_separators(self):
        for text in ("1、甲\n2、乙\n", "1) 甲\n2) 乙\n", "1）甲\n2）乙\n"):
            assert len(split_by_questions(text, max_chars=5)) == 2


class TestDedupe:
    def test_removes_identical_question_text(self):
        kept, dropped = dedupe(make_questions(2) + make_questions(2))
        assert len(kept) == 2
        assert dropped == 2

    def test_ignores_whitespace_differences(self):
        a = Question(type="fill", question="甲 乙", answer="1")
        b = Question(type="fill", question="甲  乙", answer="1")
        kept, dropped = dedupe([a, b])
        assert len(kept) == 1 and dropped == 1

    def test_keeps_different_questions(self):
        kept, dropped = dedupe(make_questions(3))
        assert len(kept) == 3 and dropped == 0


class TestQuestionImporterParse:
    def test_parses_single_chunk(self):
        client = FakeClient(questions_json(3))
        outcome = QuestionImporter(client, make_config()).parse("1. 甲\n2. 乙\n3. 丙\n")

        assert outcome.kept == 3
        assert outcome.parsed == 3
        assert len(client.calls) == 1

    def test_empty_text_rejected(self):
        with pytest.raises(ValueError):
            QuestionImporter(FakeClient("{}"), make_config()).parse("   ")

    def test_dedupes_across_chunks_and_reports_the_count(self):
        """两块都返回了同名题，要去重，并且如实报出去掉几道。"""
        text = "".join(f"{i}. {'x' * 60}\n" for i in range(1, 9))
        client = FakeClient([questions_json(2, "重复"), questions_json(2, "重复")])
        outcome = QuestionImporter(client, make_config({"max_chars_per_call": 200})).parse(text)

        assert outcome.kept == 2
        # 解析出几道、去掉几道，数字要对得上（分几块由切块逻辑决定，不写死）
        assert outcome.parsed == outcome.kept + outcome.duplicates
        assert outcome.duplicates > 0

    def test_one_bad_chunk_does_not_kill_the_rest(self):
        """某一块解析失败，其他块照常收——不能因为一段烂文字全军覆没。"""
        text = "".join(f"{i}. {'x' * 60}\n" for i in range(1, 9))

        class FlakyClient(FakeClient):
            def chat(self, messages, temperature=None, json_mode=False):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    raise RuntimeError("这一段炸了")
                return questions_json(2, "好的")

        outcome = QuestionImporter(
            FlakyClient(), make_config({"max_chars_per_call": 200})
        ).parse(text)
        assert outcome.kept == 2
        # 但失败的那段必须报出来——不然用户以为题全解析到了
        assert outcome.failed == 1
        assert "1 段没解析成功" in outcome.summary()

    def test_all_chunks_failing_raises_ai_error(self):
        client = FakeClient(raises=RuntimeError("网络断了"))
        with pytest.raises(AIError) as exc:
            QuestionImporter(client, make_config()).parse("1. 甲\n")
        assert "没能解析出题目" in str(exc.value)

    def test_chunk_without_questions_is_skipped_quietly(self):
        """开头那段"一、选择题"里本来就没题目，不算失败。"""
        text = "一、选择题\n" + "".join(f"{i}. {'x' * 60}\n" for i in range(1, 6))
        client = FakeClient(questions_json(2))
        outcome = QuestionImporter(client, make_config({"max_chars_per_call": 300})).parse(text)
        assert outcome.kept > 0
        # "段里本来就没题"跟"段解析失败"是两回事，不能混进失败数
        assert outcome.failed == 0


class TestTypeFiltering:
    """导入时过滤掉考试不考的题型。

    关键设计：**先让 AI 全部解析出来、再在代码里过滤**，不是让 AI 直接跳过——
    那样万一它顺手把别的题型也跳了，就悄悄丢题了，用户根本发现不了。
    所以 parsed / skipped / duplicates / kept 四个数字都要如实报出来。
    """

    def test_choice_questions_are_filtered_by_default(self):
        payload = json.dumps(
            {
                "questions": [
                    {"type": "choice", "question": "选择题", "answer": "A",
                     "options": ["A. 甲", "B. 乙"]},
                    {"type": "fill", "question": "填空题", "answer": "42"},
                    {"type": "reading", "question": "阅读题", "answer": "输出"},
                ]
            },
            ensure_ascii=False,
        )
        outcome = QuestionImporter(FakeClient(payload), make_config()).parse("1. x\n")

        assert [q.type for q in outcome.questions] == ["fill", "reading"]
        assert outcome.parsed == 3            # 解析出几道如实报
        assert outcome.skipped == {"choice": 1}
        assert "过滤选择题 1 道" in outcome.summary()
        assert "最终保留 2 道" in outcome.summary()

    def test_filter_can_be_turned_off(self):
        client = FakeClient(questions_json(2, qtype="choice"))
        outcome = QuestionImporter(client, make_config({"skip_types": []})).parse("1. x\n")
        assert outcome.kept == 2
        assert outcome.skipped == {}

    def test_all_filtered_raises_with_counts(self):
        """整份材料全是选择题时要说清楚，而不是静默返回 0 道。"""
        client = FakeClient(questions_json(5, qtype="choice"))
        with pytest.raises(DataError) as exc:
            QuestionImporter(client, make_config()).parse("1. x\n")

        message = str(exc.value)
        assert "5 道" in message
        assert "选择题" in message

    def test_bare_string_skip_types_is_not_split_into_characters(self):
        """用户漏写前面的 `-`，写成 `skip_types: choice`。

        直接拿字符串遍历会被逐字符拆开，跟任何题型都不匹配，过滤**静默失效**——
        选择题照旧全进来，用户还以为工具坏了。所以要当成"就这一项"。
        """
        payload = json.dumps(
            {
                "questions": [
                    {"type": "choice", "question": "选择题", "answer": "A",
                     "options": ["A. 甲", "B. 乙"]},
                    {"type": "fill", "question": "填空题", "answer": "42"},
                ]
            },
            ensure_ascii=False,
        )
        outcome = QuestionImporter(
            FakeClient(payload), make_config({"skip_types": "choice"})
        ).parse("1. x\n")

        # 被逐字符拆开的话这里就不会过滤掉任何一道
        assert outcome.skipped == {"choice": 1}
        assert [q.type for q in outcome.questions] == ["fill"]

    def test_bad_skip_types_type_raises_friendly_error(self):
        """写成数字之类时给一句人话，而不是 `'int' object is not iterable`。"""
        with pytest.raises(ConfigError) as exc:
            QuestionImporter(FakeClient("{}"), make_config({"skip_types": 5}))

        message = str(exc.value)
        assert "skip_types" in message
        assert "- choice" in message
