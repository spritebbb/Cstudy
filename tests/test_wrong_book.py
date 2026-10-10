"""错题本：按遗忘曲线安排复习。

时间不好在测试里等，所以直接改库里的 `last_wrong_at` 来模拟"几天前错的"。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.models import Question  # noqa: E402
from cstudy.wrong_book import (  # noqa: E402
    DEFAULT_INTERVALS,
    DEFAULT_MASTER_AFTER,
    due_items,
    interval_days,
    intervals_from_config,
    list_wrong,
    master_after_from_config,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


def add_question(db: Database, text: str = "题") -> int:
    return db.save_questions([Question(type="fill", question=text, answer="1")])[0]


def backdate(db: Database, question_id: int, days: float) -> None:
    """把"最近错于"改到 N 天前。SQLite 存的是 UTC，所以这里也按 UTC 算。"""
    when = datetime.now(timezone.utc) - timedelta(days=days)
    db.conn.execute(
        "UPDATE wrong_questions SET last_wrong_at = ? WHERE question_id = ?",
        (when.strftime("%Y-%m-%d %H:%M:%S"), question_id),
    )
    db.conn.commit()


def make_config(cfg: dict | None = None) -> Config:
    return Config({"wrong_book": cfg or {}, "paths": {}}, Path("."))


class TestIntervalDays:
    def test_grows_with_wrong_count(self):
        assert interval_days(1) < interval_days(2) < interval_days(3)

    def test_reuses_the_last_value_beyond_the_table(self):
        assert interval_days(99) == DEFAULT_INTERVALS[-1]

    def test_zero_is_treated_as_the_first_interval(self):
        assert interval_days(0) == DEFAULT_INTERVALS[0]

    def test_empty_table_falls_back(self):
        assert interval_days(1, ()) == DEFAULT_INTERVALS[0]


class TestConfig:
    def test_reads_intervals(self):
        assert intervals_from_config(make_config({"review_intervals": [1, 2]})) == (1, 2)

    def test_broken_intervals_fall_back(self):
        """配置写错了不能把功能搞挂，退回默认值就行。"""
        assert intervals_from_config(make_config({"review_intervals": "每天"})) == DEFAULT_INTERVALS

    def test_non_positive_intervals_are_dropped(self):
        assert intervals_from_config(make_config({"review_intervals": [0, 5, -1]})) == (5,)

    def test_reads_master_after(self):
        assert master_after_from_config(make_config({"master_after_correct": 3})) == 3

    def test_broken_master_after_falls_back(self):
        assert (
            master_after_from_config(make_config({"master_after_correct": "两次"}))
            == DEFAULT_MASTER_AFTER
        )


class TestListWrong:
    def test_includes_the_question_and_its_counts(self, db: Database):
        question_id = add_question(db, "错过的题")
        db.record_answer(question_id, "0", is_correct=False)

        items = list_wrong(db)

        assert len(items) == 1
        assert items[0].question_id == question_id
        assert items[0].question.question == "错过的题"
        assert items[0].wrong_count == 1

    def test_ignores_questions_never_answered_wrong(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "1", is_correct=True)

        assert list_wrong(db) == []

    def test_wrong_count_accumulates(self, db: Database):
        question_id = add_question(db)
        for _ in range(3):
            db.record_answer(question_id, "0", is_correct=False)

        assert list_wrong(db)[0].wrong_count == 3


class TestMastery:
    """连续答对够了就算掌握——**连续**，中间错一次要重新数。"""

    def test_answering_correctly_twice_masters_it(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.record_answer(question_id, "1", is_correct=True)
        db.record_answer(question_id, "1", is_correct=True)

        item = list_wrong(db)[0]

        assert item.consecutive_correct == 2
        assert item.mastered

    def test_one_correct_answer_is_not_enough(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.record_answer(question_id, "1", is_correct=True)

        item = list_wrong(db)[0]

        assert not item.mastered

    def test_a_later_mistake_resets_the_streak(self, db: Database):
        """错一次就得重新数——"以前对过两次"不代表现在还熟。"""
        question_id = add_question(db)
        db.record_answer(question_id, "1", is_correct=True)
        db.record_answer(question_id, "1", is_correct=True)
        db.record_answer(question_id, "0", is_correct=False)

        item = list_wrong(db)[0]

        assert item.consecutive_correct == 0
        assert not item.mastered

    def test_master_threshold_is_configurable(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.record_answer(question_id, "1", is_correct=True)

        assert list_wrong(db, master_after=1)[0].mastered

    def test_manually_removed_counts_as_mastered(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.set_mastered(question_id)

        assert list_wrong(db)[0].mastered


class TestDue:
    def test_not_due_right_after_a_mistake(self, db: Database):
        """刚错完不该立刻又冒出来——最早也要隔一天。"""
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)

        assert not list_wrong(db)[0].due

    def test_due_after_the_interval_has_passed(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        backdate(db, question_id, days=2)   # 第一次错，间隔 1 天

        assert list_wrong(db)[0].due

    def test_second_mistake_waits_longer(self, db: Database):
        """错第二次间隔变 3 天，隔 2 天还不该复习。"""
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.record_answer(question_id, "0", is_correct=False)
        backdate(db, question_id, days=2)

        assert not list_wrong(db)[0].due

    def test_mastered_questions_are_never_due(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)
        db.record_answer(question_id, "1", is_correct=True)
        db.record_answer(question_id, "1", is_correct=True)
        backdate(db, question_id, days=30)

        assert not list_wrong(db)[0].due

    def test_timestamps_are_read_as_local_time(self, db: Database):
        """SQLite 存的是 UTC，读出来要转成本地时间，否则会差好几个小时。"""
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)

        recorded = list_wrong(db)[0].last_wrong_at

        assert abs((datetime.now() - recorded).total_seconds()) < 60


class TestDueItems:
    def test_only_returns_due_ones(self, db: Database):
        due_id = add_question(db, "早该复习了")
        fresh_id = add_question(db, "刚错的")
        for question_id in (due_id, fresh_id):
            db.record_answer(question_id, "0", is_correct=False)
        backdate(db, due_id, days=5)

        items = due_items(list_wrong(db))

        assert [item.question_id for item in items] == [due_id]

    def test_most_overdue_first(self, db: Database):
        first = add_question(db, "错得早")
        second = add_question(db, "错得晚一点")
        for question_id in (first, second):
            db.record_answer(question_id, "0", is_correct=False)
        backdate(db, first, days=10)
        backdate(db, second, days=3)

        items = due_items(list_wrong(db))

        assert [item.question_id for item in items] == [first, second]

    def test_empty_when_nothing_is_due(self, db: Database):
        question_id = add_question(db)
        db.record_answer(question_id, "0", is_correct=False)

        assert due_items(list_wrong(db)) == []
