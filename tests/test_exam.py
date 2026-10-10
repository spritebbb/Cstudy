"""组卷：从题库里按考试题型分值挑题拼一套卷子。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.exam import build_exam_paper, build_exam_plan  # noqa: E402
from cstudy.models import Question  # noqa: E402


def make_config(exam_cfg: dict | None = None) -> Config:
    return Config({"exam": exam_cfg or {}, "paths": {}}, Path("."))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


def stock(db: Database, kind: str, count: int) -> None:
    db.save_questions(
        [Question(type=kind, question=f"{kind}题{i}", answer="1") for i in range(count)]
    )


class TestExamPlan:
    def test_counts_add_up_to_the_requested_total(self):
        """按比例分完必须正好等于要的总数，不能少一道多一道。"""
        for total in (5, 7, 10, 20, 33, 150):
            plan = build_exam_plan(make_config({"total_questions": total}))
            assert plan.total == total, f"total={total} 时算出来 {plan.total}"

    def test_ratio_follows_the_score_distribution(self):
        """填空占的分最多，题量也该最多。"""
        plan = build_exam_plan(make_config({"total_questions": 20}))

        assert plan.counts["fill"] > plan.counts["reading"]
        assert plan.counts["fill"] > plan.counts["coding"]
        # 84:35:31，20 道的话填空应该是 11 道左右
        assert 10 <= plan.counts["fill"] <= 12

    def test_every_listed_type_gets_at_least_one(self):
        """「程序设计 0 道」的卷子没意义。"""
        plan = build_exam_plan(make_config({"total_questions": 6}))

        assert all(count >= 1 for count in plan.counts.values())

    def test_zero_ratio_type_is_left_out(self):
        plan = build_exam_plan(
            make_config({"total_questions": 10, "ratios": {"fill": 84, "reading": 35, "coding": 0}})
        )

        assert "coding" not in plan.counts

    def test_broken_total_falls_back(self):
        plan = build_exam_plan(make_config({"total_questions": "很多"}))

        assert plan.total == 20

    def test_broken_ratio_falls_back(self):
        plan = build_exam_plan(make_config({"ratios": {"fill": "好多", "reading": 35, "coding": 31}}))

        assert plan.total == 20
        assert plan.counts["fill"] > 0

    def test_all_zero_ratios_fall_back_to_default(self):
        plan = build_exam_plan(
            make_config({"total_questions": 10, "ratios": {"fill": 0, "reading": 0, "coding": 0}})
        )

        assert plan.total == 10

    def test_describe_is_readable(self):
        plan = build_exam_plan(make_config({"total_questions": 20}))

        assert "填空题" in plan.describe()


class TestExamPaper:
    def test_picks_the_planned_number_of_questions(self, db: Database):
        stock(db, "fill", 40)
        stock(db, "reading", 40)
        stock(db, "coding", 40)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert len(paper.questions) == 20
        assert len(paper.question_ids) == 20
        assert not paper.shortfall

    def test_never_repeats_a_question(self, db: Database):
        """一套卷子里同一道题出现两次就废了。"""
        stock(db, "fill", 30)
        stock(db, "reading", 30)
        stock(db, "coding", 30)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert len(set(paper.question_ids)) == len(paper.question_ids)

    def test_type_mix_matches_the_plan(self, db: Database):
        stock(db, "fill", 40)
        stock(db, "reading", 40)
        stock(db, "coding", 40)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        got: dict[str, int] = {}
        for question in paper.questions:
            got[question.type] = got.get(question.type, 0) + 1
        assert got == paper.plan.counts

    def test_reports_shortfall_instead_of_padding(self, db: Database):
        """某题型不够时如实少给，**不拿别的题型凑数**。"""
        stock(db, "fill", 3)
        stock(db, "reading", 40)
        stock(db, "coding", 40)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert paper.shortfall.get("fill", 0) > 0
        # 填空只有 3 道，就不能给出 11 道填空
        fills = [q for q in paper.questions if q.type == "fill"]
        assert len(fills) == 3
        assert "题库里不够" in paper.summary

    def test_empty_bank_gives_an_empty_paper(self, db: Database):
        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert paper.questions == []
        assert paper.shortfall

    def test_summary_mentions_the_count(self, db: Database):
        stock(db, "fill", 40)
        stock(db, "reading", 40)
        stock(db, "coding", 40)

        assert "20 道题" in build_exam_paper(db, make_config()).summary
