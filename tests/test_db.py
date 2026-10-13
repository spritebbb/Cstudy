"""数据库层的测试。用临时文件，不碰项目里的真实数据库。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.db import Database  # noqa: E402
from cstudy.models import Question  # noqa: E402
from cstudy.subtitle import Segment  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


class TestSchema:
    def test_init_is_idempotent(self, db: Database):
        db.init_schema()
        db.init_schema()
        assert db.count("videos") == 0

    def test_all_tables_exist(self, db: Database):
        for table in (
            "syllabus_points", "videos", "subtitles", "notes",
            "note_points", "questions", "answers", "wrong_questions",
        ):
            assert db.count(table) == 0

    def test_unknown_table_rejected(self, db: Database):
        with pytest.raises(ValueError, match="不认识的表名"):
            db.count("users; DROP TABLE notes")


class TestVideos:
    def test_insert_then_get_same_id(self, db: Database):
        first = db.upsert_video("BV1xx411c7mD", 1, "标题", "url", 100)
        second = db.upsert_video("BV1xx411c7mD", 1, "标题", "url", 100)
        assert first == second

    def test_different_pages_are_different_rows(self, db: Database):
        one = db.upsert_video("BV1xx411c7mD", 1)
        two = db.upsert_video("BV1xx411c7mD", 2)
        assert one != two
        assert db.count("videos") == 2

    def test_empty_title_does_not_overwrite_existing(self, db: Database):
        db.upsert_video("BV1xx411c7mD", 1, "原标题")
        db.upsert_video("BV1xx411c7mD", 1, "")
        row = db.conn.execute("SELECT title FROM videos").fetchone()
        assert row["title"] == "原标题"


class TestSubtitles:
    def test_roundtrip_keeps_timestamps(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        original = [Segment(0.0, "第一句"), Segment(12.5, "第二句")]
        db.save_subtitle(video_id, "bilibili_cc", original)

        loaded = db.get_subtitle_segments(video_id)
        assert loaded is not None
        assert [s.text for s in loaded] == ["第一句", "第二句"]
        assert loaded[1].start == pytest.approx(12.5)

    def test_missing_returns_none(self, db: Database):
        assert db.get_subtitle_segments(999) is None

    def test_corrupt_json_treated_as_missing(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO subtitles (video_id, source, content, segments) VALUES (?,?,?,?)",
                (video_id, "bilibili_cc", "文本", "{坏掉的 JSON"),
            )
        assert db.get_subtitle_segments(video_id) is None

    def test_readable_text_is_stored_with_timestamps(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        db.save_subtitle(video_id, "whisper", [Segment(65.0, "内容")])
        assert db.get_subtitle_text(video_id) == "[01:05] 内容"


class TestQuestions:
    def test_saves_and_lists(self, db: Database):
        questions = [
            Question(type="choice", question="选哪个", answer="A", options=["A. 1", "B. 2"]),
            Question(type="fill", question="填空", answer="0"),
        ]
        ids = db.save_questions(questions, note_id=None, source="video:BV1#p1")
        assert len(ids) == 2
        rows = db.list_questions()
        assert len(rows) == 2
        assert rows[0]["source"] == "video:BV1#p1"

    def test_options_survive_roundtrip_as_json(self, db: Database):
        db.save_questions([Question(
            type="choice", question="选哪个", answer="A", options=["A. 1", "B. 2"],
        )])
        row = db.list_questions()[0]
        assert "A. 1" in row["options"]

    def test_filter_by_note(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        note_id = db.save_note(video_id, "标题", "正文")
        db.save_questions([Question(type="fill", question="甲", answer="1")], note_id=note_id)
        db.save_questions([Question(type="fill", question="乙", answer="2")], note_id=None)
        assert len(db.list_questions(note_id)) == 1


class TestSyllabus:
    def test_upsert_does_not_duplicate(self, db: Database):
        first = db.upsert_syllabus_point("指针", "指针", 1)
        second = db.upsert_syllabus_point("指针", "指针", 1)
        assert first == second
        assert db.count("syllabus_points") == 1

    def test_empty_chapter_does_not_overwrite(self, db: Database):
        db.upsert_syllabus_point("指针", "指针", 1)
        db.upsert_syllabus_point("指针", "", 1)
        assert db.list_syllabus_points()[0]["chapter"] == "指针"


class TestNotes:
    def test_save_and_get(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        note_id = db.save_note(video_id, "标题", "# 正文", "notes/x.md")
        note = db.get_note(note_id)
        assert note["title"] == "标题"
        assert note["file_path"] == "notes/x.md"

    def test_link_points_is_idempotent(self, db: Database):
        video_id = db.upsert_video("BV1xx411c7mD", 1)
        note_id = db.save_note(video_id, "标题", "正文")
        point_id = db.upsert_syllabus_point("指针", "指针", 1)
        db.link_note_points(note_id, [point_id])
        db.link_note_points(note_id, [point_id])
        assert db.count("note_points") == 1

    def test_link_empty_list_is_noop(self, db: Database):
        db.link_note_points(1, [])
        assert db.count("note_points") == 0


class TestStats:
    def test_stats_shape(self, db: Database):
        stats = db.stats()
        assert set(stats) == {"视频", "字幕", "笔记", "题目", "考纲知识点", "知识关系"}
        assert all(v == 0 for v in stats.values())

    def test_relation_count_shows_up_in_the_stats(self, db: Database):
        """状态栏那几个数要能看出关系存进去了没有——它是"确认过"的证据。"""
        left = db.upsert_syllabus_point("甲", "第1章", 1)
        right = db.upsert_syllabus_point("乙", "第2章", 2)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "prereq", "reason": "先甲后乙"}]
        )

        assert db.stats()["知识关系"] == 1


class TestPointRelations:
    """知识点关系（知识图谱的连线）的存取。

    这一层是"用户确认过的东西"的落脚点，**不能悄悄吞掉也不能悄悄重复**：
    用户勾了 12 条，库里就得是 12 条；再勾一遍还得是 12 条。
    """

    def _two_points(self, db: Database) -> tuple[int, int]:
        return (
            db.upsert_syllabus_point("break 与 continue", "第5章", 1),
            db.upsert_syllabus_point("switch 语句", "第4章", 2),
        )

    def test_saves_and_reads_back_with_both_names(self, db: Database):
        left, right = self._two_points(db)

        added = db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "confuse",
              "reason": "都用来跳出，考试爱放在一起考", "origin": "ai"}]
        )

        rows = db.list_relations()
        assert added == 1
        assert len(rows) == 1
        assert rows[0]["from_name"] == "break 与 continue"
        assert rows[0]["to_name"] == "switch 语句"
        assert rows[0]["origin"] == "ai"

    def test_the_same_relation_twice_does_not_double_up(self, db: Database):
        """同一个关系反复确认不该翻倍——不然用户手抖点两次，图上就两条线。"""
        left, right = self._two_points(db)
        payload = [{"from_id": left, "to_id": right, "kind": "confuse", "reason": "容易混"}]

        assert db.save_relations(payload) == 1
        assert db.save_relations(payload) == 0
        assert len(db.list_relations()) == 1

    def test_the_reverse_direction_is_a_different_relation(self, db: Database):
        """前置关系是有方向的：A→B 和 B→A 是两码事，不能当成重复吞掉。"""
        left, right = self._two_points(db)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "prereq", "reason": "先左后右"}]
        )

        assert db.save_relations(
            [{"from_id": right, "to_id": left, "kind": "prereq", "reason": "反过来"}]
        ) == 1

    def test_deleting_reports_whether_it_hit_anything(self, db: Database):
        left, right = self._two_points(db)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "related", "reason": "有关"}]
        )
        edge_id = int(db.list_relations()[0]["id"])

        assert db.delete_relation(edge_id) is True
        assert db.delete_relation(edge_id) is False
        assert db.list_relations() == []

    def test_deleting_a_point_takes_its_relations_with_it(self, db: Database):
        """换考纲会清掉知识点。留下的连线必须一起走——
        不然下次打开图谱会读到一堆指向虚无的关系。"""
        left, right = self._two_points(db)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "related", "reason": "有关"}]
        )

        db.conn.execute("DELETE FROM syllabus_points WHERE id = ?", (right,))
        db.conn.commit()

        assert db.list_relations() == []

    def test_saving_nothing_is_a_noop(self, db: Database):
        assert db.save_relations([]) == 0

    def test_exam_types_of_a_point(self, db: Database):
        point_id = db.upsert_syllabus_point("指针", "第8章", 1)
        db.save_questions(
            [Question(type="coding", question=f"编程{i}", answer="1", point_id=point_id)
             for i in range(2)]
        )
        db.save_questions(
            [Question(type="fill", question="求值 ___", answer="1", point_id=point_id)]
        )

        kinds = db.exam_types_of_point(point_id)

        assert kinds == {"coding": 2, "fill": 1}

    def test_exam_types_of_an_unknown_point_is_empty(self, db: Database):
        assert db.exam_types_of_point(999) == {}


class TestAnswerRecording:
    def _one_question(self, db: Database) -> int:
        return db.save_questions(
            [Question(type="choice", question="选哪个", answer="A", options=["A. 甲", "B. 乙"])]
        )[0]

    def test_records_the_answer_row(self, db: Database):
        question_id = self._one_question(db)
        db.record_answer(question_id, "A", is_correct=True, time_spent=12)

        assert db.count("answers") == 1
        row = db.conn.execute("SELECT * FROM answers").fetchone()
        assert row["question_id"] == question_id
        assert row["user_answer"] == "A"
        assert row["is_correct"] == 1
        assert row["time_spent"] == 12

    def test_wrong_answer_enters_the_wrong_book(self, db: Database):
        question_id = self._one_question(db)
        db.record_answer(question_id, "B", is_correct=False)

        assert db.count("wrong_questions") == 1
        assert db.list_wrong_question_ids() == [question_id]

    def test_repeated_wrong_answers_increment_the_count(self, db: Database):
        question_id = self._one_question(db)
        db.record_answer(question_id, "B", is_correct=False)
        db.record_answer(question_id, "C", is_correct=False)

        row = db.conn.execute("SELECT wrong_count FROM wrong_questions").fetchone()
        assert row["wrong_count"] == 2

    def test_correct_answer_does_not_touch_the_wrong_book(self, db: Database):
        """答对了也不删记录。

        删了就丢了"我以前错过这道题"这个信息，而错题本的价值恰恰在这里。
        什么时候算掌握、隔多久复习，留给专门的错题本功能去定。
        """
        question_id = self._one_question(db)
        db.record_answer(question_id, "B", is_correct=False)
        db.record_answer(question_id, "A", is_correct=True)

        assert db.count("wrong_questions") == 1            # 记录还在
        assert db.list_wrong_question_ids() == [question_id]  # 仍然算错题
        assert db.count("answers") == 2                    # 两次作答都记了

    def test_missing_question_id_is_skipped(self, db: Database):
        """题目没入库时只当次练习，不写库，更不能崩。"""
        db.record_answer(0, "A", is_correct=True)
        assert db.count("answers") == 0

    def test_point_label_survives_roundtrip(self, db: Database):
        db.save_questions(
            [
                Question(
                    type="choice",
                    question="选哪个",
                    answer="A",
                    options=["A. 甲", "B. 乙"],
                    point="字符与 ASCII",
                )
            ]
        )
        assert db.list_questions()[0]["point"] == "字符与 ASCII"


class TestQueryQuestions:
    """按条件挑题——筛选刷题和组卷都靠它。"""

    @pytest.fixture
    def stocked(self, db: Database) -> Database:
        db.save_questions(
            [
                Question(type="fill", question="填空甲", answer="1"),
                Question(type="fill", question="填空乙", answer="2"),
                Question(type="reading", question="阅读甲", answer="3"),
                Question(type="coding", question="编程甲", answer="4"),
            ],
            source="卷A",
        )
        db.save_questions(
            [Question(type="fill", question="别的卷的填空", answer="5")], source="卷B"
        )
        return db

    def test_no_filter_returns_everything(self, stocked: Database):
        assert len(stocked.query_questions()) == 5

    def test_filters_by_type(self, stocked: Database):
        rows = stocked.query_questions(types=["fill"])
        assert len(rows) == 3
        assert {row["type"] for row in rows} == {"fill"}

    def test_filters_by_multiple_types(self, stocked: Database):
        assert len(stocked.query_questions(types=["reading", "coding"])) == 2

    def test_filters_by_source(self, stocked: Database):
        assert len(stocked.query_questions(sources=["卷A"])) == 4

    def test_combines_conditions(self, stocked: Database):
        rows = stocked.query_questions(types=["fill"], sources=["卷B"])
        assert len(rows) == 1
        assert rows[0]["question"] == "别的卷的填空"

    def test_limit_caps_the_result(self, stocked: Database):
        assert len(stocked.query_questions(limit=2)) == 2

    def test_shuffle_limit_returns_distinct_rows(self, stocked: Database):
        """随机抽 N 道不能抽到重复的——组卷最怕一套卷子里同一道题出现两次。"""
        rows = stocked.query_questions(shuffle=True, limit=4)
        ids = [row["id"] for row in rows]
        assert len(ids) == len(set(ids)) == 4

    def test_empty_type_list_means_no_filter(self, stocked: Database):
        """空列表按"不筛"处理，不然界面上不勾任何题型会一道题都出不来。"""
        assert len(stocked.query_questions(types=[])) == 5

    def test_list_sources(self, stocked: Database):
        assert stocked.list_sources() == ["卷A", "卷B"]


class TestSyllabusCoverage:
    """考纲覆盖度：哪些知识点还没练到。"""

    @pytest.fixture
    def with_points(self, db: Database) -> Database:
        self.chapter1 = db.upsert_syllabus_point("指针", "第8章", 1)
        self.chapter2 = db.upsert_syllabus_point("预处理命令", "第1章", 2)
        db.save_questions([Question(type="fill", question="指针题", answer="1")])
        return db

    def test_point_without_questions_still_shows_up(self, with_points: Database):
        """**没题的知识点必须也在结果里**——那正是用户要看的东西。"""
        rows = {row["name"]: row["question_count"] for row in with_points.coverage()}

        assert rows == {"指针": 0, "预处理命令": 0}

    def test_counts_questions_linked_to_a_point(self, with_points: Database):
        with_points.assign_points({"": with_points.upsert_syllabus_point("指针", "第8章", 1)})

        counts = {row["name"]: row["question_count"] for row in with_points.coverage()}
        assert counts["指针"] == 1
        assert counts["预处理命令"] == 0

    def test_points_grouped_by_chapter(self, with_points: Database):
        grouped = with_points.points_by_chapter()

        assert set(grouped) == {"第8章", "第1章"}
        assert [row["name"] for row in grouped["第8章"]] == ["指针"]


class TestAssignPoints:
    def test_maps_by_point_text(self, db: Database):
        db.save_questions(
            [
                Question(type="fill", question="甲", answer="1", point="递归函数"),
                Question(type="fill", question="乙", answer="2", point="递归函数"),
                Question(type="fill", question="丙", answer="3", point="别的"),
            ]
        )
        point_id = db.upsert_syllabus_point("函数的递归调用", "第7章", 1)

        changed = db.assign_points({"递归函数": point_id})

        assert changed == 2
        assert db.count("questions") == 3
        linked = db.query_questions(point_ids=[point_id])
        assert {row["question"] for row in linked} == {"甲", "乙"}

    def test_does_not_overwrite_an_existing_mapping(self, db: Database):
        """重复跑不能把已经映射好的冲掉，也不能冲掉人工纠正过的。"""
        db.save_questions([Question(type="fill", question="甲", answer="1", point="递归")])
        first = db.upsert_syllabus_point("函数的递归调用", "第7章", 1)
        second = db.upsert_syllabus_point("数组", "第6章", 2)
        db.assign_points({"递归": first})

        changed = db.assign_points({"递归": second})

        assert changed == 0
        assert db.query_questions(point_ids=[first])[0]["question"] == "甲"


class TestPointIdPersistence:
    def test_point_id_survives_a_round_trip(self, db: Database):
        point_id = db.upsert_syllabus_point("指针", "第8章", 1)
        db.save_questions(
            [Question(type="fill", question="甲", answer="1", point="指针", point_id=point_id)]
        )

        restored = Question.from_row(db.query_questions()[0])

        assert restored.point_id == point_id

    def test_missing_point_id_reads_as_none(self, db: Database):
        db.save_questions([Question(type="fill", question="甲", answer="1")])

        assert Question.from_row(db.query_questions()[0]).point_id is None


class TestExamType:
    """考试题型（exam_type）：比 type 细一层，把"程序填空"从"填空"里分出来。"""

    def test_saved_questions_get_an_exam_type(self, db: Database):
        db.save_questions([Question(type="fill", question="表达式 1+1 的值为 ______。", answer="2")])

        assert db.query_questions()[0]["exam_type"] == "fill"

    def test_code_fill_is_told_apart(self, db: Database):
        db.save_questions(
            [
                Question(
                    type="fill",
                    question="int main() {\n int a = ______;\n int b = ______;\n}",
                    answer="1; 2",
                )
            ]
        )

        assert db.query_questions()[0]["exam_type"] == "code_fill"

    def test_filters_by_exam_type(self, db: Database):
        db.save_questions([Question(type="fill", question="表达式 1+1 的值为 ______。", answer="2")])
        db.save_questions(
            [
                Question(
                    type="fill",
                    question="int main() {\n int a = ______;\n int b = ______;\n}",
                    answer="1; 2",
                )
            ]
        )

        assert len(db.query_questions(exam_types=["fill"])) == 1
        assert len(db.query_questions(exam_types=["code_fill"])) == 1
        assert len(db.query_questions(exam_types=["fill", "code_fill"])) == 2

    def test_backfill_fills_the_older_rows(self, db: Database):
        """老库里的题没有 exam_type，启动时应该自动补上。"""
        db.save_questions([Question(type="fill", question="表达式 1+1 的值为 ______。", answer="2")])
        db.conn.execute("UPDATE questions SET exam_type = NULL")
        db.conn.commit()

        filled = db.backfill_exam_types()

        assert filled == 1
        assert db.query_questions()[0]["exam_type"] == "fill"

    def test_backfill_leaves_corrected_rows_alone(self, db: Database):
        """已经算过的不再动——否则以后人工修正过的会被冲掉。"""
        db.save_questions([Question(type="fill", question="甲", answer="1")])
        db.conn.execute("UPDATE questions SET exam_type = 'code_fill'")
        db.conn.commit()

        assert db.backfill_exam_types() == 0
        assert db.query_questions()[0]["exam_type"] == "code_fill"


class TestBackup:
    """数据库备份——**库里唯一不可再生的是用户的作答记录和错题本**。

    题目丢了能用脚本重导，学习轨迹丢了就真没了，而它躺在单个文件里没有任何保护。
    """

    def test_creates_a_dated_backup(self, db: Database):
        path = db.backup()

        assert path is not None and path.exists()
        assert path.name.startswith("cstudy-") and path.name.endswith(".db")

    def test_skips_when_today_is_already_backed_up(self, db: Database):
        """一天备一次就够，每次启动都备会把硬盘塞满。"""
        assert db.backup() is not None
        assert db.backup() is None

    def test_backup_is_a_readable_database(self, db: Database):
        """备份出来得能打开、内容对——不然备了个寂寞。"""
        db.save_questions([Question(type="fill", question="甲", answer="1")])

        path = db.backup()
        copy = Database(path)

        assert copy.count("questions") == 1

    def test_keeps_only_the_newest_few(self, db: Database):
        """老备份要清掉，不然一天一份攒一年。"""
        backup_dir = db.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        for day in range(1, 6):
            (backup_dir / f"cstudy-2026-01-0{day}.db").write_bytes(b"x")

        db.backup(keep=3)

        remaining = sorted(p.name for p in backup_dir.glob("cstudy-*.db"))
        assert len(remaining) == 3, f"应该只留 3 份，实际 {remaining}"

    def test_missing_database_is_not_an_error(self, tmp_path: Path):
        """全新的库还没文件可备，不该报错。"""
        fresh = Database(tmp_path / "还没建过.db")

        assert fresh.backup() is None


class TestSearchNotes:
    def note(self, db: Database, title: str, content: str) -> int:
        video_id = db.upsert_video(f"BV{title}", 1, title, "url", 100)
        return db.save_note(video_id=video_id, title=title, content=content)

    def test_lists_recent_notes_without_a_query(self, db: Database):
        self.note(db, "第一集", "讲变量")
        self.note(db, "第二集", "讲循环")

        assert len(db.search_notes()) == 2

    def test_searches_the_title(self, db: Database):
        self.note(db, "位运算入门", "内容是别的")
        self.note(db, "循环结构", "内容是别的")

        found = db.search_notes("位运算")

        assert [row["title"] for row in found] == ["位运算入门"]

    def test_searches_the_body_too(self, db: Database):
        """用户记不住"哪一集讲过位运算"，只记得住"位运算"这个词——
        所以正文必须一起搜。"""
        self.note(db, "第七集", "这节课讲位运算和移位")
        self.note(db, "第八集", "这节课讲文件操作")

        found = db.search_notes("移位")

        assert [row["title"] for row in found] == ["第七集"]

    def test_no_match_returns_nothing(self, db: Database):
        self.note(db, "第一集", "讲变量")

        assert db.search_notes("绝对搜不到的词") == []

    def test_empty_query_is_treated_as_no_filter(self, db: Database):
        self.note(db, "第一集", "讲变量")

        assert len(db.search_notes("   ")) == 1
