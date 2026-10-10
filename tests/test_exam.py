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


def stock_code_fill(db: Database, count: int) -> None:
    """程序填空题：题干得是**代码里挖了不止一个空**，不然会被当成普通填空。

    这就是 `exam_type` 的判据——见 `models.looks_like_code_fill`。
    """
    db.save_questions(
        [
            Question(
                type="fill",
                question=(
                    "#include <stdio.h>\nint main() {\n"
                    f"    int a = ______;   /* 第 {i} 题 */\n"
                    "    int b = ______;\n"
                    '    printf("%d\\n", a + b);\n    return 0;\n}'
                ),
                answer="1; 2",
            )
            for i in range(count)
        ]
    )


def stock_every_type(db: Database, count: int = 40) -> None:
    stock(db, "fill", count)
    stock(db, "reading", count)
    stock(db, "coding", count)
    stock_code_fill(db, count)


class TestExamPlan:
    def test_counts_add_up_to_the_requested_total(self):
        """按比例分完必须正好等于要的总数，不能少一道多一道。"""
        for total in (5, 7, 10, 20, 33, 150):
            plan = build_exam_plan(make_config({"total_questions": total}))
            assert plan.total == total, f"total={total} 时算出来 {plan.total}"

    def test_ratio_follows_the_score_distribution(self):
        """分值最高的填空，题量也该最多。"""
        plan = build_exam_plan(make_config({"total_questions": 20}))

        assert max(plan.counts, key=lambda k: plan.counts[k]) == "fill"
        # 48:36:35:31，20 道的话填空是 6 道、程序填空和程序阅读各 5 道、程序设计 4 道
        assert plan.counts["fill"] == 6
        assert plan.counts["code_fill"] == 5
        assert plan.counts["reading"] == 5
        assert plan.counts["coding"] == 4

    def test_every_listed_type_gets_at_least_one(self):
        """「程序设计 0 道」的卷子没意义。"""
        plan = build_exam_plan(make_config({"total_questions": 6}))

        assert all(count >= 1 for count in plan.counts.values())

    def test_zero_ratio_type_is_left_out(self):
        plan = build_exam_plan(
            make_config({"total_questions": 10, "ratios": {"fill": 48, "code_fill": 36, "coding": 0}})
        )

        assert "coding" not in plan.counts

    def test_broken_total_falls_back(self):
        plan = build_exam_plan(make_config({"total_questions": "很多"}))

        assert plan.total == 20

    def test_broken_ratio_falls_back(self):
        plan = build_exam_plan(make_config({"ratios": {"fill": "好多", "code_fill": 36, "reading": 35}}))

        assert plan.total == 20
        assert plan.counts["fill"] > 0

    def test_all_zero_ratios_fall_back_to_default(self):
        plan = build_exam_plan(
            make_config(
                {"total_questions": 10, "ratios": {"fill": 0, "code_fill": 0, "reading": 0, "coding": 0}}
            )
        )

        assert plan.total == 10

    def test_describe_is_readable(self):
        plan = build_exam_plan(make_config({"total_questions": 20}))

        assert "填空题" in plan.describe()


class TestExamPaper:
    def test_picks_the_planned_number_of_questions(self, db: Database):
        stock_every_type(db)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert len(paper.questions) == 20
        assert len(paper.question_ids) == 20
        assert not paper.shortfall

    def test_never_repeats_a_question(self, db: Database):
        """一套卷子里同一道题出现两次就废了。"""
        stock_every_type(db, 30)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert len(set(paper.question_ids)) == len(paper.question_ids)

    def test_type_mix_matches_the_plan(self, db: Database):
        stock_every_type(db)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        got: dict[str, int] = {}
        for question in paper.questions:
            got[question.exam_type] = got.get(question.exam_type, 0) + 1
        assert got == paper.plan.counts

    def test_code_fill_is_its_own_type(self, db: Database):
        """**程序填空单独占一类**——考试里它是独立的大题（36 分），
        不能跟普通填空混在一起，不然组出来的卷子跟考试结构对不上。"""
        stock_every_type(db)

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert paper.plan.counts["code_fill"] > 0
        kinds = {q.exam_type for q in paper.questions}
        assert "code_fill" in kinds

    def test_reports_shortfall_instead_of_padding(self, db: Database):
        """某题型不够时如实少给，**不拿别的题型凑数**。"""
        stock(db, "fill", 40)
        stock(db, "reading", 40)
        stock(db, "coding", 40)
        stock_code_fill(db, 2)          # 程序填空只有 2 道

        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert paper.shortfall.get("code_fill", 0) > 0
        code_fills = [q for q in paper.questions if q.exam_type == "code_fill"]
        assert len(code_fills) == 2
        assert "题库里不够" in paper.summary

    def test_empty_bank_gives_an_empty_paper(self, db: Database):
        paper = build_exam_paper(db, make_config({"total_questions": 20}))

        assert paper.questions == []
        assert paper.shortfall

    def test_summary_mentions_the_count(self, db: Database):
        stock_every_type(db)

        assert "20 道题" in build_exam_paper(db, make_config()).summary
