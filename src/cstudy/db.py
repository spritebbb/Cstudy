"""SQLite 封装。

每个线程一个连接（PyQt 的后台线程会各自调用，共享连接会出问题）。
写操作走 tx() 事务上下文，出错自动回滚。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .logging_setup import get_logger
from .models import Question
from .subtitle import Segment, render

log = get_logger(__name__)

SCHEMA_FILE = Path(__file__).resolve().parent / "schema.sql"


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._local = threading.local()

    # ---------- 连接 ----------

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            existing = sqlite3.connect(self.path)
            existing.row_factory = sqlite3.Row
            existing.execute("PRAGMA foreign_keys = ON")
            self._local.conn = existing
            log.debug("打开数据库连接：%s", self.path)
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务：正常提交，抛异常回滚。"""
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ---------- 建表 ----------

    def init_schema(self) -> None:
        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        with self.tx() as conn:
            conn.executescript(sql)
            self._migrate(conn)
        log.info("数据库结构已就绪：%s", self.path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给老库补上后来新加的列。

        `CREATE TABLE IF NOT EXISTS` 不会改动已存在的表，所以新加的列必须在这里补，
        否则老用户升级后一 INSERT 就报 "no such column"。
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
        if "point" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN point TEXT")
            log.info("已给 questions 表补上 point 列")

    # ---------- 视频 ----------

    def upsert_video(
        self, bvid: str, page: int = 1, title: str = "", url: str = "", duration: int = 0
    ) -> int:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO videos (bvid, page, title, url, duration)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (bvid, page) DO UPDATE SET
                    title    = COALESCE(NULLIF(excluded.title, ''), videos.title),
                    url      = COALESCE(NULLIF(excluded.url, ''), videos.url),
                    duration = COALESCE(NULLIF(excluded.duration, 0), videos.duration)
                """,
                (bvid, page, title, url, duration),
            )
            row = conn.execute(
                "SELECT id FROM videos WHERE bvid = ? AND page = ?", (bvid, page)
            ).fetchone()
        return int(row["id"])

    # ---------- 字幕 ----------

    def get_subtitle_segments(self, video_id: int) -> list[Segment] | None:
        """读缓存字幕。缓存的是 JSON，读不出来就当作没有缓存。"""
        row = self.conn.execute(
            "SELECT segments FROM subtitles WHERE video_id = ? ORDER BY id DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        if not row or not row["segments"]:
            return None
        try:
            raw = json.loads(row["segments"])
            return [Segment(start=float(item["start"]), text=str(item["text"])) for item in raw]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("缓存字幕解析失败，将重新抓取：%s", exc)
            return None

    def get_subtitle_text(self, video_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT content FROM subtitles WHERE video_id = ? ORDER BY id DESC LIMIT 1",
            (video_id,),
        ).fetchone()
        return row["content"] if row else None

    def save_subtitle(self, video_id: int, source: str, segments: list[Segment]) -> int:
        payload = json.dumps(
            [{"start": seg.start, "text": seg.text} for seg in segments], ensure_ascii=False
        )
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO subtitles (video_id, source, content, segments) VALUES (?, ?, ?, ?)",
                (video_id, source, render(segments), payload),
            )
        return int(cur.lastrowid)

    # ---------- 笔记 ----------

    def save_note(self, video_id: int, title: str, content: str, file_path: str = "") -> int:
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO notes (video_id, title, content, file_path) VALUES (?, ?, ?, ?)",
                (video_id, title, content, file_path),
            )
        return int(cur.lastrowid)

    def link_note_points(self, note_id: int, point_ids: list[int]) -> None:
        if not point_ids:
            return
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO note_points (note_id, point_id) VALUES (?, ?)",
                [(note_id, pid) for pid in point_ids],
            )

    def list_notes(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, title, created_at, file_path FROM notes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def get_note(self, note_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    # ---------- 题目 ----------

    def save_questions(
        self, questions: list[Question], note_id: int | None = None, source: str = ""
    ) -> list[int]:
        ids: list[int] = []
        with self.tx() as conn:
            for q in questions:
                row = q.to_row()
                cur = conn.execute(
                    """
                    INSERT INTO questions
                        (type, difficulty, point, note_id, question, options,
                         answer, explanation, source)
                    VALUES (:type, :difficulty, :point, :note_id, :question, :options,
                            :answer, :explanation, :source)
                    """,
                    {**row, "note_id": note_id, "source": source},
                )
                ids.append(int(cur.lastrowid))
        return ids

    def list_questions(self, note_id: int | None = None) -> list[sqlite3.Row]:
        if note_id is None:
            return self.conn.execute(
                "SELECT * FROM questions ORDER BY id DESC"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM questions WHERE note_id = ? ORDER BY id", (note_id,)
        ).fetchall()

    # ---------- 答题记录 ----------

    def record_answer(
        self,
        question_id: int,
        user_answer: str,
        is_correct: bool,
        time_spent: int | None = None,
    ) -> None:
        """记一次作答。判错时顺带往错题本记一笔（错次数 +1）。

        **判对时不动错题本**：删记录会丢掉"我以前错过这道题"这个信息，
        而错题本的价值恰恰在这里。什么时候算"掌握"、隔多久复习，
        留给专门的错题本功能去定——这里只做累积，不做删除。
        """
        if not question_id:
            # 题目没入库（理论上不该发生）就只当次练习，不写库
            log.warning("这道题没有 id，本次作答不写库")
            return

        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO answers (question_id, user_answer, is_correct, time_spent)
                VALUES (?, ?, ?, ?)
                """,
                (question_id, user_answer, 1 if is_correct else 0, time_spent),
            )
            if not is_correct:
                conn.execute(
                    """
                    INSERT INTO wrong_questions
                        (question_id, wrong_count, last_wrong_at, mastered)
                    VALUES (?, 1, CURRENT_TIMESTAMP, 0)
                    ON CONFLICT (question_id) DO UPDATE SET
                        wrong_count   = wrong_questions.wrong_count + 1,
                        last_wrong_at = CURRENT_TIMESTAMP,
                        mastered      = 0
                    """,
                    (question_id,),
                )

    def list_wrong_question_ids(self) -> list[int]:
        """还没掌握的错题 id，最近错的排前面。"""
        rows = self.conn.execute(
            "SELECT question_id FROM wrong_questions WHERE mastered = 0 "
            "ORDER BY last_wrong_at DESC, question_id"
        ).fetchall()
        return [int(row["question_id"]) for row in rows]

    # ---------- 考纲 ----------

    def upsert_syllabus_point(self, name: str, chapter: str = "", sort_order: int = 0) -> int:
        with self.tx() as conn:
            conn.execute(
                """
                INSERT INTO syllabus_points (name, chapter, sort_order)
                VALUES (?, ?, ?)
                ON CONFLICT (name) DO UPDATE SET
                    chapter    = COALESCE(NULLIF(excluded.chapter, ''), syllabus_points.chapter),
                    sort_order = excluded.sort_order
                """,
                (name, chapter, sort_order),
            )
            row = conn.execute(
                "SELECT id FROM syllabus_points WHERE name = ?", (name,)
            ).fetchone()
        return int(row["id"])

    def list_syllabus_points(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM syllabus_points ORDER BY sort_order, id"
        ).fetchall()

    # ---------- 统计 ----------

    def count(self, table: str) -> int:
        # 表名不能参数化，这里用白名单挡住注入
        allowed = {
            "videos", "subtitles", "notes", "questions",
            "answers", "wrong_questions", "syllabus_points", "note_points",
        }
        if table not in allowed:
            raise ValueError(f"不认识的表名：{table}")
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])

    def stats(self) -> dict[str, Any]:
        return {
            "视频": self.count("videos"),
            "字幕": self.count("subtitles"),
            "笔记": self.count("notes"),
            "题目": self.count("questions"),
            "考纲知识点": self.count("syllabus_points"),
        }
