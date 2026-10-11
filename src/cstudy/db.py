"""SQLite 封装。

每个线程一个连接（PyQt 的后台线程会各自调用，共享连接会出问题）。
写操作走 tx() 事务上下文，出错自动回滚。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .logging_setup import get_logger
from .models import Question, exam_type_for
from .subtitle import Segment, render

log = get_logger(__name__)


def parse_db_time(value) -> datetime | None:
    """把数据库里的时间戳读成**本地时间**。读不出来返回 None。

    **SQLite 的 CURRENT_TIMESTAMP 是 UTC**，而界面上显示、跟"现在"比都是本地时间。
    直接拿来用会差好几个小时（东八区差 8 小时）——错题本的"该复习了"会算错，
    按周统计进度也会把周界划错。所以凡是要用时间戳的地方都走这里。

    （这个坑在 `answers.answered_at`、`wrong_questions.last_wrong_at`、
    `notes.created_at` 上都存在。）
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(str(value), fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    log.warning("时间戳解析不了：%r", value)
    return None

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
            # **先迁移再建表**：schema.sql 里有 `CREATE INDEX ... ON questions(exam_type)`，
            # 老库的 questions 表还没有这一列，直接跑建表语句会报 "no such column"。
            self._migrate(conn)
            conn.executescript(sql)
        # 老库里的题没有考试题型，顺手补上——用户不该记得"升级后要跑一次迁移"
        self.backfill_exam_types()
        log.info("数据库结构已就绪：%s", self.path)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """给老库补上后来新加的列。

        `CREATE TABLE IF NOT EXISTS` 不会改动已存在的表，所以新加的列必须在这里补，
        否则老用户升级后一 INSERT 就报 "no such column"。
        """
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "questions" not in tables:
            return   # 全新的库，建表语句里已经带着这些列了

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)")}
        if "point" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN point TEXT")
            log.info("已给 questions 表补上 point 列")
        if "exam_type" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN exam_type TEXT")
            log.info("已给 questions 表补上 exam_type 列")

    def backfill_exam_types(self) -> int:
        """把还没有考试题型的题算出来补上。返回补了多少道。

        只动 `exam_type IS NULL` 的行——所以这个可以放心地每次启动都跑，
        跑过的不再动，也不会覆盖以后可能的（人工）修正。
        """
        rows = self.conn.execute(
            "SELECT id, type, question FROM questions WHERE exam_type IS NULL"
        ).fetchall()
        if not rows:
            return 0

        # 只拿 type 和题干算，**不构造 Question**——构造会做完整校验
        # （要求有答案、题型合法），一条坏数据就会炸掉整个回填。
        updates = [
            (exam_type_for(str(row["type"] or ""), str(row["question"] or "")), int(row["id"]))
            for row in rows
        ]

        with self.tx() as conn:
            conn.executemany("UPDATE questions SET exam_type = ? WHERE id = ?", updates)
        log.info("已给 %d 道题补上考试题型", len(updates))
        return len(updates)

    # ---------- 备份 ----------

    # 留几份带日期的备份。一天一份，保留最近这些天。
    BACKUP_KEEP = 14

    def backup(self, keep: int | None = None) -> Path | None:
        """把数据库复制一份带日期的备份，返回备份路径；今天备过就返回 None。

        **这个库里唯一不可再生的东西是用户自己的作答记录和错题本**。
        题目丢了能用 scripts/import_bank.py 重导，学习轨迹丢了就真没了——
        而它现在躺在单个文件里，没有任何保护。

        用 SQLite 自己的 backup API，不是直接复制文件：直接复制有可能拿到
        "复制到一半"的库（写操作进行中），备份出来是坏的。
        """
        if not self.path.exists():
            return None   # 全新的库，还没东西可备

        backup_dir = self.path.parent / "backups"
        today = datetime.now().strftime("%Y-%m-%d")
        target = backup_dir / f"cstudy-{today}.db"
        if target.exists():
            return None   # 今天已经备过了

        keep = self.BACKUP_KEEP if keep is None else keep
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = sqlite3.connect(target)
            try:
                with dest:
                    self.conn.backup(dest)
            finally:
                dest.close()
        except (OSError, sqlite3.Error) as exc:
            # 备份失败不该拦住用户用软件
            log.warning("数据库备份失败（%s），这次跳过", exc)
            return None

        log.info("数据库已备份：%s", target.name)
        self._prune_backups(backup_dir, keep)
        return target

    @staticmethod
    def _prune_backups(backup_dir: Path, keep: int) -> None:
        """只留最近 keep 份，老的删掉——不然一天一份攒一年。"""
        if keep <= 0:
            return
        try:
            existing = sorted(backup_dir.glob("cstudy-*.db"))
        except OSError:
            return
        for stale in existing[:-keep]:
            try:
                stale.unlink()
                log.debug("删掉旧备份：%s", stale.name)
            except OSError:
                log.debug("删旧备份失败：%s", stale, exc_info=True)

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

    def search_notes(self, query: str = "", limit: int = 200) -> list[sqlite3.Row]:
        """搜笔记。**标题和正文都搜**——用户记不住"哪一集讲过位运算"，
        但记得住"位运算"这个词。

        不传 query 就是列出最近几篇。
        """
        text = (query or "").strip()
        if not text:
            return self.conn.execute(
                "SELECT id, title, created_at, file_path FROM notes "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()

        like = f"%{text}%"
        return self.conn.execute(
            """
            SELECT id, title, created_at, file_path FROM notes
            WHERE title LIKE ? OR content LIKE ?
            ORDER BY id DESC LIMIT ?
            """,
            (like, like, limit),
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
                        (type, difficulty, point, point_id, exam_type, note_id, question,
                         options, answer, explanation, source)
                    VALUES (:type, :difficulty, :point, :point_id, :exam_type, :note_id,
                            :question, :options, :answer, :explanation, :source)
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

    def query_questions(
        self,
        types: list[str] | None = None,
        exam_types: list[str] | None = None,
        point_ids: list[int] | None = None,
        sources: list[str] | None = None,
        limit: int | None = None,
        shuffle: bool = False,
    ) -> list[sqlite3.Row]:
        """按条件挑题。筛选刷题和组卷都用这个。

        条件都是可选的，一个都不传就是"全部题目"。值一律走参数占位符，
        不拼进 SQL 字符串里。

        `types` 按库里的三类筛（fill/reading/coding）；
        `exam_types` 按**考试的四类**筛（多一个 code_fill 程序填空）。
        """
        where: list[str] = []
        params: list[Any] = []
        if types:
            where.append(f"type IN ({','.join('?' * len(types))})")
            params.extend(types)
        if exam_types:
            where.append(f"exam_type IN ({','.join('?' * len(exam_types))})")
            params.extend(exam_types)
        if point_ids:
            where.append(f"point_id IN ({','.join('?' * len(point_ids))})")
            params.extend(point_ids)
        if sources:
            where.append(f"source IN ({','.join('?' * len(sources))})")
            params.extend(sources)

        sql = "SELECT * FROM questions"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY RANDOM()" if shuffle else " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.conn.execute(sql, params).fetchall()

    def list_sources(self) -> list[str]:
        """题目来自哪些文件（给筛选界面的下拉框用）。"""
        rows = self.conn.execute(
            "SELECT DISTINCT source FROM questions WHERE source != '' ORDER BY source"
        ).fetchall()
        return [str(row["source"]) for row in rows]

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

    def set_mastered(self, question_id: int, mastered: bool = True) -> None:
        """手动把一道题移出错题本（或移回来）。

        "连续答对几次算掌握"是**算出来**的，不写库——只有用户手动点了
        "这个我会了"才动这一列。
        """
        with self.tx() as conn:
            conn.execute(
                "UPDATE wrong_questions SET mastered = ? WHERE question_id = ?",
                (1 if mastered else 0, question_id),
            )

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

    def points_by_chapter(self) -> dict[str, list[sqlite3.Row]]:
        """考纲知识点按章分组，给筛选界面和覆盖度用。"""
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in self.list_syllabus_points():
            grouped.setdefault(str(row["chapter"] or "（没写章节）"), []).append(row)
        return grouped

    def coverage(self) -> list[sqlite3.Row]:
        """每个考纲知识点有多少道题。

        **没题的也要返回（0 道）**——用 LEFT JOIN 就是为了这个。
        用户要看的是"哪些知识点还没练到"，漏掉 0 的那些，这个功能就没意义了。
        """
        return self.conn.execute(
            """
            SELECT sp.id, sp.chapter, sp.name, COUNT(q.id) AS question_count
            FROM syllabus_points sp
            LEFT JOIN questions q ON q.point_id = sp.id
            GROUP BY sp.id
            ORDER BY sp.sort_order, sp.id
            """
        ).fetchall()

    def assign_points(self, mapping: dict[str, int]) -> int:
        """按 point 文本把题对齐到考纲：{point 文本: 考纲知识点 id}。

        只动还没映射过的题（`point_id IS NULL`），所以**重复跑不会覆盖已有结果**，
        也不会把人工纠正过的映射冲掉。返回改了行数。
        """
        if not mapping:
            return 0
        with self.tx() as conn:
            changed = 0
            for text, point_id in mapping.items():
                cur = conn.execute(
                    "UPDATE questions SET point_id = ? WHERE point = ? AND point_id IS NULL",
                    (point_id, text),
                )
                changed += int(cur.rowcount or 0)
        return changed

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
