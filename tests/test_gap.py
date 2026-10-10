"""按知识点补题——只测不调 AI 的那部分。

重点是**每道题都必须有答案**：这是用户明确要求过的。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cstudy.ai.gap import GapFiller  # noqa: E402
from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.models import DataError  # noqa: E402
from fill_gaps import thin_points  # noqa: E402


class FakeClient:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[list] = []

    def chat_json(self, messages, temperature=None):
        self.calls.append(messages)
        return self.payload


def make_config(gap_cfg: dict | None = None) -> Config:
    return Config({"gap": gap_cfg or {}, "quiz": {"prefer": {"fill": 5, "reading": 2, "coding": 2}}, "paths": {}}, Path("."))


def payload(questions: list[dict]) -> str:
    return json.dumps({"questions": questions}, ensure_ascii=False)


def question(**overrides) -> dict:
    base = {
        "type": "fill",
        "difficulty": "easy",
        "point": "模型自己写的知识点",
        "question": "题干",
        "options": [],
        "answer": "答案",
        "explanation": "因为所以",
    }
    base.update(overrides)
    return base


class TestGapFiller:
    def test_returns_the_generated_questions(self):
        client = FakeClient(payload([question(question="甲"), question(question="乙")]))

        result = GapFiller(client, make_config()).generate("指针", "第8章")

        assert [q.question for q in result] == ["甲", "乙"]

    def test_overwrites_the_point_with_the_syllabus_name(self):
        """知识点是给定的，不能让模型自己另起一个名字——那样又对不上考纲了。"""
        client = FakeClient(payload([question(point="模型自己写的知识点")]))

        result = GapFiller(client, make_config()).generate("指针的概念、类型与分类", "第8章")

        assert result[0].point == "指针的概念、类型与分类"

    def test_passes_the_point_and_chapter_to_the_prompt(self):
        client = FakeClient(payload([question()]))

        GapFiller(client, make_config()).generate("文件定位", "第10章")

        sent = json.dumps(client.calls[0], ensure_ascii=False)
        assert "文件定位" in sent
        assert "第10章" in sent

    def test_asks_for_the_configured_number(self):
        client = FakeClient(payload([question()]))

        GapFiller(client, make_config({"count": 9})).generate("指针")

        assert "9 道" in json.dumps(client.calls[0], ensure_ascii=False)

    def test_caps_at_the_requested_count(self):
        client = FakeClient(payload([question(question=f"题{i}") for i in range(20)]))

        assert len(GapFiller(client, make_config({"count": 3})).generate("指针")) == 3

    def test_broken_count_falls_back(self):
        client = FakeClient(payload([question()]))

        assert GapFiller(client, make_config({"count": "六道"})).generate("指针")

    def test_no_question_without_an_answer_gets_through(self):
        """**每道题都必须有答案**——这是用户明确要求过的。"""
        client = FakeClient(
            payload([question(question="有答案的"), question(question="没答案的", answer="   ")])
        )

        result = GapFiller(client, make_config()).generate("指针")

        assert [q.question for q in result] == ["有答案的"]

    def test_questions_without_answers_never_even_reach_us(self):
        """`parse_questions` 在构造 Question 时就拒绝空答案。"""
        client = FakeClient(payload([question(answer="")]))

        with pytest.raises(DataError):
            GapFiller(client, make_config()).generate("指针")

    def test_empty_result_is_returned_not_raised(self):
        """出不了题（比如纯概念知识点）不算错误，返回空列表就行。"""
        assert GapFiller(FakeClient(payload([])), make_config()).generate("简史") == []


class TestThinPoints:
    @pytest.fixture
    def db(self, tmp_path: Path) -> Database:
        database = Database(tmp_path / "test.db")
        database.init_schema()
        return database

    def test_lists_points_below_the_threshold(self, db: Database):
        db.upsert_syllabus_point("指针", "第8章", 1)
        db.upsert_syllabus_point("循环", "第5章", 2)
        point_id = db.upsert_syllabus_point("数组", "第6章", 3)
        from cstudy.models import Question

        db.save_questions(
            [Question(type="fill", question=f"题{i}", answer="1") for i in range(10)]
        )
        db.assign_points({"": point_id})

        thin = {row["name"] for row in thin_points(db, below=5)}

        assert thin == {"指针", "循环"}

    def test_sorted_thinnest_first(self, db: Database):
        from cstudy.models import Question

        db.upsert_syllabus_point("空的知识点", "第1章", 1)
        point_id = db.upsert_syllabus_point("有两道的", "第2章", 2)
        db.save_questions([Question(type="fill", question="甲", answer="1")])
        db.assign_points({"": point_id})

        names = [row["name"] for row in thin_points(db, below=5)]

        assert names[0] == "空的知识点"

    def test_returns_nothing_when_everything_is_covered(self, db: Database):
        from cstudy.models import Question

        point_id = db.upsert_syllabus_point("循环", "第5章", 1)
        db.save_questions(
            [Question(type="fill", question=f"题{i}", answer="1") for i in range(8)]
        )
        db.assign_points({"": point_id})

        assert thin_points(db, below=5) == []
