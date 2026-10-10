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
        assert set(stats) == {"视频", "字幕", "笔记", "题目", "考纲知识点"}
        assert all(v == 0 for v in stats.values())
