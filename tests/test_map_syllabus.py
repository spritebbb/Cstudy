"""把题目知识点对齐到考纲——只测不调 AI 的那部分。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cstudy.db import Database  # noqa: E402
from cstudy.models import DataError, Question  # noqa: E402
from map_syllabus import map_batch, unmapped_tags  # noqa: E402


class FakeClient:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[list] = []

    def chat_json(self, messages, temperature=None):
        self.calls.append(messages)
        if isinstance(self.payload, list):
            return self.payload[min(len(self.calls) - 1, len(self.payload) - 1)]
        return self.payload


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


class TestUnmappedTags:
    def test_lists_distinct_tags_most_common_first(self, db: Database):
        db.save_questions(
            [
                Question(type="fill", question="甲", answer="1", point="递归函数"),
                Question(type="fill", question="乙", answer="1", point="递归函数"),
                Question(type="fill", question="丙", answer="1", point="指针"),
            ]
        )

        assert unmapped_tags(db) == ["递归函数", "指针"]

    def test_skips_questions_without_a_point(self, db: Database):
        db.save_questions([Question(type="fill", question="甲", answer="1")])

        assert unmapped_tags(db) == []

    def test_skips_already_mapped_questions(self, db: Database):
        """已经对齐过的题不能再冒出来——不然重复跑会一直重复映射。"""
        db.save_questions(
            [Question(type="fill", question="甲", answer="1", point="递归函数")]
        )
        point_id = db.upsert_syllabus_point("函数的递归调用", "第7章", 1)
        db.assign_points({"递归函数": point_id})

        assert unmapped_tags(db) == []


class TestMapBatch:
    def test_reads_the_mapping_back(self):
        payload = json.dumps(
            {"mapping": [{"tag": "递归函数", "point": "函数的递归调用"}]},
            ensure_ascii=False,
        )

        result = map_batch(FakeClient(payload), ["函数的递归调用"], ["递归函数"])

        assert result == {"递归函数": "函数的递归调用"}

    def test_null_stays_null(self):
        payload = json.dumps(
            {"mapping": [{"tag": "程序设计", "point": None}]}, ensure_ascii=False
        )

        assert map_batch(FakeClient(payload), ["数组"], ["程序设计"]) == {"程序设计": None}

    def test_invented_names_are_rejected(self):
        """模型偶尔会自己造个知识点名——清单里没有就不能认，否则统计会乱。"""
        payload = json.dumps(
            {"mapping": [{"tag": "指针", "point": "指针与数组的深层关系"}]},
            ensure_ascii=False,
        )

        assert map_batch(FakeClient(payload), ["数组"], ["指针"]) == {"指针": None}

    def test_missing_mapping_list_raises(self):
        with pytest.raises(DataError):
            map_batch(FakeClient(json.dumps({"nope": []})), ["数组"], ["指针"])
