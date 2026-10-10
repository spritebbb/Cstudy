"""错题本：做错的题按遗忘曲线安排复习。

数据其实一直在记——`record_answer()` 早就往 `wrong_questions` 写错次数和最近错的
时刻了，只是一直没有界面看。这个模块把它读出来，算清楚"哪些今天该复习"。

**"掌握"是算出来的，不写库**：一道题连续答对 N 次就算掌握。
这样不用维护额外的状态字段，也就不会出现"字段说掌握了、实际记录不是"的不一致。
（`mastered` 那一列留给用户**手动**把某道题移出错题本，那是另一回事。）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .db import Database
from .logging_setup import get_logger
from .models import Question

log = get_logger(__name__)

# 错第 1 次隔 1 天复习，第 2 次 3 天，第 3 次 7 天，第 4 次起都是 15 天。
# 错得越多，说明越不熟，但也不该天天刷同一道——间隔拉开才叫遗忘曲线。
DEFAULT_INTERVALS = (1, 3, 7, 15)

# 连续答对这么多次就算掌握了，不再出现在"今天该复习"里
DEFAULT_MASTER_AFTER = 2


@dataclass
class WrongItem:
    """错题本里的一道题。"""

    question: Question
    question_id: int
    wrong_count: int
    last_wrong_at: datetime | None = None
    consecutive_correct: int = 0
    due_at: datetime | None = None
    manually_removed: bool = False
    master_after: int = DEFAULT_MASTER_AFTER

    @property
    def mastered(self) -> bool:
        """连续答对够了就算掌握。手动移出的也算。"""
        return self.manually_removed or self.consecutive_correct >= self.master_after

    @property
    def due(self) -> bool:
        """今天该复习了吗。"""
        if self.mastered or self.due_at is None:
            return False
        return self.due_at <= datetime.now()

    def describe(self) -> str:
        """给用户看的一行说明。"""
        parts = [f"错过 {self.wrong_count} 次"]
        if self.last_wrong_at:
            parts.append(f"最近 {self.last_wrong_at.strftime('%m-%d %H:%M')}")
        if self.mastered:
            parts.append("已掌握")
        elif self.due:
            parts.append("该复习了")
        elif self.due_at:
            parts.append(f"下次 {self.due_at.strftime('%m-%d')}")
        return "，".join(parts)


def intervals_from_config(config: Config) -> tuple[int, ...]:
    """从 config.yaml 读复习间隔。写错了就用默认值，不让它把功能搞挂。"""
    raw = config.get("wrong_book.review_intervals", list(DEFAULT_INTERVALS))
    if not isinstance(raw, (list, tuple)) or not raw:
        log.warning("wrong_book.review_intervals 不是列表（%r），用默认值", raw)
        return DEFAULT_INTERVALS
    days: list[int] = []
    for item in raw:
        try:
            value = int(item)
        except (TypeError, ValueError):
            log.warning("复习间隔里有个看不懂的值（%r），跳过", item)
            continue
        if value > 0:
            days.append(value)
    return tuple(days) or DEFAULT_INTERVALS


def master_after_from_config(config: Config) -> int:
    raw = config.get("wrong_book.master_after_correct", DEFAULT_MASTER_AFTER)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("wrong_book.master_after_correct 看不懂（%r），用默认值", raw)
        return DEFAULT_MASTER_AFTER
    return value if value > 0 else DEFAULT_MASTER_AFTER


def interval_days(wrong_count: int, intervals: tuple[int, ...] = DEFAULT_INTERVALS) -> int:
    """错了这么多次之后，隔几天再复习。错得越多间隔越长，超出表长就用最后一个。"""
    if not intervals:
        intervals = DEFAULT_INTERVALS
    index = max(0, min(wrong_count, len(intervals)) - 1)
    return intervals[index]


def _parse_time(value) -> datetime | None:
    """SQLite 的时间戳是字符串，解析不了就当没有——不该因为一条坏数据崩掉。

    **SQLite 的 CURRENT_TIMESTAMP 是 UTC**，而界面上显示、跟"现在"比都是本地时间。
    这里统一转成本地时间再往外传，否则「该复习了」会差好几个小时，
    显示的"最近错于"也会对不上用户表。
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


def _consecutive_correct(db: Database) -> dict[int, int]:
    """每道题**最近连续**答对了几次。

    一次查全部、按题目分组倒着数，比每道题查一次省事。遇到第一个答错的就停——
    再往前对多少次都跟"现在掌握了没有"无关。
    """
    rows = db.conn.execute(
        "SELECT question_id, is_correct FROM answers ORDER BY question_id, id DESC"
    ).fetchall()

    streak: dict[int, int] = {}
    stopped: set[int] = set()
    for row in rows:
        question_id = int(row["question_id"])
        if question_id in stopped:
            continue
        if row["is_correct"]:
            streak[question_id] = streak.get(question_id, 0) + 1
        else:
            stopped.add(question_id)
    return streak


def list_wrong(
    db: Database,
    intervals: tuple[int, ...] = DEFAULT_INTERVALS,
    master_after: int = DEFAULT_MASTER_AFTER,
) -> list[WrongItem]:
    """错题本里的题，最近错的排前面。"""
    streaks = _consecutive_correct(db)
    rows = db.conn.execute(
        """
        SELECT wq.question_id, wq.wrong_count, wq.last_wrong_at, wq.mastered,
               q.type, q.question, q.answer, q.explanation, q.options,
               q.difficulty, q.point, q.point_id
        FROM wrong_questions wq
        JOIN questions q ON q.id = wq.question_id
        ORDER BY wq.last_wrong_at DESC, wq.question_id
        """
    ).fetchall()

    items: list[WrongItem] = []
    for row in rows:
        try:
            question = Question.from_row(row)
        except Exception:
            # 一条坏数据不该让整个错题本打不开
            log.warning("错题本里跳过一道读不出来的题：id=%s", row["question_id"], exc_info=True)
            continue

        wrong_count = int(row["wrong_count"] or 1)
        last_wrong = _parse_time(row["last_wrong_at"])
        due_at = (
            last_wrong + timedelta(days=interval_days(wrong_count, intervals))
            if last_wrong
            else None
        )
        items.append(
            WrongItem(
                question=question,
                question_id=int(row["question_id"]),
                wrong_count=wrong_count,
                last_wrong_at=last_wrong,
                consecutive_correct=streaks.get(int(row["question_id"]), 0),
                due_at=due_at,
                manually_removed=bool(row["mastered"]),
                master_after=master_after,
            )
        )
    return items


def due_items(items: list[WrongItem]) -> list[WrongItem]:
    """今天该复习的那些，最该复习的（错得最多、隔得最久）排前面。"""
    due = [item for item in items if item.due]
    due.sort(key=lambda item: (item.last_wrong_at or datetime.min, -item.wrong_count))
    return due
