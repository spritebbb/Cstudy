"""学情统计：这几周练得怎么样。

时间不好在测试里等，所以直接改 `answers.answered_at` 来模拟"几天前答的"。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.db import Database  # noqa: E402
from cstudy.models import Question  # noqa: E402
from cstudy.stats import Tally, by_exam_type, by_week, overview, summarize  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


def add_question(db: Database, kind: str = "fill", text: str = "题") -> int:
    return db.save_questions([Question(type=kind, question=text, answer="1")])[0]


def answer(db: Database, question_id: int, correct: bool, days_ago: float = 0) -> None:
    """记一次作答，并把它挪到 N 天前。SQLite 存的是 UTC，这里也按 UTC 算。

    **只改刚插进去的那一条**（取最大 id）——按 question_id 改会把同一道题的
    历史记录全冲掉，那测出来的就不是"三天前答的"而是"全都在三天前答的"。
    """
    db.record_answer(question_id, "x", is_correct=correct)
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.conn.execute(
        "UPDATE answers SET answered_at = ? WHERE id = (SELECT MAX(id) FROM answers)",
        (when.strftime("%Y-%m-%d %H:%M:%S"),),
    )
    db.conn.commit()


class TestTally:
    def test_accuracy_is_a_rounded_percentage(self):
        tally = Tally(total=3, right=2)

        assert tally.accuracy == 67

    def test_no_answers_means_zero_not_a_crash(self):
        """一道没做时不能除零。"""
        assert Tally().accuracy == 0

    def test_describe_reads_naturally(self):
        assert "对 2 道" in Tally(total=3, right=2).describe()

    def test_describe_when_empty(self):
        assert "还没做过" in Tally().describe()


class TestOverview:
    def test_counts_all_time(self, db: Database):
        question_id = add_question(db)
        for correct in (True, True, False):
            answer(db, question_id, correct)

        assert overview(db).all_time.total == 3

    def test_splits_recent_windows(self, db: Database):
        question_id = add_question(db)
        answer(db, question_id, True, days_ago=1)     # 7 天内
        answer(db, question_id, True, days_ago=20)    # 30 天内
        answer(db, question_id, True, days_ago=90)    # 更早

        data = overview(db)

        assert data.all_time.total == 3
        assert data.last_30_days.total == 2
        assert data.last_7_days.total == 1

    def test_empty_database(self, db: Database):
        data = overview(db)

        assert data.all_time.total == 0
        assert summarize(db) == "还没做过题"


class TestByWeek:
    def test_returns_the_requested_number_of_weeks(self, db: Database):
        assert len(by_week(db, weeks=6)) == 6

    def test_each_week_gets_its_own_label(self, db: Database):
        weeks = by_week(db, weeks=4)

        assert len({w.label for w in weeks}) == 4

    def test_this_weeks_answers_land_in_the_last_bucket(self, db: Database):
        question_id = add_question(db)
        answer(db, question_id, True)

        weeks = by_week(db, weeks=4)

        assert weeks[-1].tally.total == 1
        assert weeks[0].tally.total == 0

    def test_answers_land_in_the_previous_week(self, db: Database):
        question_id = add_question(db)
        answer(db, question_id, True, days_ago=8)

        weeks = by_week(db, weeks=4)

        assert weeks[-1].tally.total == 0, "上周的不该算进这周"
        assert sum(w.tally.total for w in weeks) == 1

    def test_answers_older_than_the_window_are_ignored(self, db: Database):
        question_id = add_question(db)
        answer(db, question_id, True, days_ago=200)

        assert sum(w.tally.total for w in by_week(db, weeks=4)) == 0

    def test_empty_weeks_get_an_empty_bar(self, db: Database):
        assert by_week(db, weeks=3)[0].bar(peak=10) == ""


class TestWeekBar:
    def test_bar_length_follows_the_peak(self):
        from cstudy.stats import WeekBar

        half = WeekBar(label="x", tally=Tally(total=5))

        assert len(half.bar(peak=10, width=20)) == 10

    def test_bar_is_never_empty_when_there_is_work(self):
        """做过题就一定画得出来，哪怕只有一道——不然看着像没做。"""
        from cstudy.stats import WeekBar

        assert WeekBar(label="x", tally=Tally(total=1)).bar(peak=100, width=20) != ""

    def test_bar_never_exceeds_the_width(self):
        from cstudy.stats import WeekBar

        assert len(WeekBar(label="x", tally=Tally(total=99)).bar(peak=1, width=20)) == 20


class TestByExamType:
    def test_groups_by_exam_type(self, db: Database):
        fill = add_question(db, "fill", "填空题")
        coding = add_question(db, "coding", "编写程序")
        answer(db, fill, True)
        answer(db, fill, False)
        answer(db, coding, True)

        result = dict(by_exam_type(db))

        assert result["填空题"].total == 2
        assert result["填空题"].accuracy == 50
        assert result["程序设计题"].accuracy == 100

    def test_code_fill_is_its_own_bucket(self, db: Database):
        """程序填空必须单独看——它占 36 分，混进填空里就看不出弱在哪。"""
        code_fill = add_question(
            db, "fill", "int main() {\n int a = ______;\n int b = ______;\n}"
        )
        answer(db, code_fill, True)

        names = [name for name, _ in by_exam_type(db)]

        assert "程序填空题" in names

    def test_untouched_types_are_left_out(self, db: Database):
        fill = add_question(db, "fill")
        answer(db, fill, True)

        assert [name for name, _ in by_exam_type(db)] == ["填空题"]

    def test_empty_database(self, db: Database):
        assert by_exam_type(db) == []
