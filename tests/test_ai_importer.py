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
from cstudy.config import Config  # noqa: E402
from cstudy.models import Question  # noqa: E402


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


def questions_json(count: int, prefix: str = "题") -> str:
    return json.dumps(
        {
            "questions": [
                {
                    "type": "choice",
                    "difficulty": "easy",
                    "point": "测试点",
                    "question": f"{prefix}{i}",
                    "options": ["A. 甲", "B. 乙"],
                    "answer": "A",
                    "explanation": "因为甲",
                }
                for i in range(1, count + 1)
            ]
        },
        ensure_ascii=False,
    )


def make_questions(count: int, prefix: str = "题") -> list[Question]:
    return [
        Question(
            type="choice",
            question=f"{prefix}{i}",
            answer="A",
            options=["A. 甲", "B. 乙"],
        )
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
        result = QuestionImporter(client, make_config()).parse("1. 甲\n2. 乙\n3. 丙\n")

        assert len(result) == 3
        assert len(client.calls) == 1

    def test_empty_text_rejected(self):
        with pytest.raises(ValueError):
            QuestionImporter(FakeClient("{}"), make_config()).parse("   ")

    def test_dedupes_across_chunks(self):
        """两块都返回了同名题，最后要去重。"""
        text = "".join(f"{i}. {'x' * 60}\n" for i in range(1, 9))
        client = FakeClient([questions_json(2, "重复"), questions_json(2, "重复")])
        result = QuestionImporter(client, make_config({"max_chars_per_call": 200})).parse(text)

        assert len(result) == 2

    def test_one_bad_chunk_does_not_kill_the_rest(self):
        """某一块解析失败，其他块照常收——不能因为一段烂文字全军覆没。"""
        text = "".join(f"{i}. {'x' * 60}\n" for i in range(1, 9))

        class FlakyClient(FakeClient):
            def chat(self, messages, temperature=None, json_mode=False):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    raise RuntimeError("这一段炸了")
                return questions_json(2, "好的")

        result = QuestionImporter(
            FlakyClient(), make_config({"max_chars_per_call": 200})
        ).parse(text)
        assert len(result) == 2

    def test_all_chunks_failing_raises_ai_error(self):
        client = FakeClient(raises=RuntimeError("网络断了"))
        with pytest.raises(AIError) as exc:
            QuestionImporter(client, make_config()).parse("1. 甲\n")
        assert "没能解析出题目" in str(exc.value)

    def test_chunk_without_questions_is_skipped_quietly(self):
        """开头那段"一、选择题"里本来就没题目，不算失败。"""
        text = "一、选择题\n" + "".join(f"{i}. {'x' * 60}\n" for i in range(1, 6))
        client = FakeClient(questions_json(2))
        result = QuestionImporter(client, make_config({"max_chars_per_call": 300})).parse(text)
        assert result  # 有拿到题就行，不因为某块没题就报错
