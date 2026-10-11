"""学情统计：这几周练得怎么样。

`answers` 表一直在记（什么时候答的、对没对、用了多久），但界面上只有"本批答对 X"，
关掉就没了。备考六个月，**看趋势比看单次分数重要**。

**按周统计不按天**：一天没做题很正常，按天看全是窟窿，看着打击人；
按周看才是"这周比上周多做了多少、正确率涨没涨"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .db import Database, parse_db_time
from .logging_setup import get_logger
from .models import EXAM_TYPE_LABELS

log = get_logger(__name__)

DEFAULT_WEEKS = 8


@dataclass
class Tally:
    """一段时间的答题账。"""

    total: int = 0
    right: int = 0

    def bump(self, correct: bool) -> None:
        self.total += 1
        if correct:
            self.right += 1

    @property
    def accuracy(self) -> int:
        """正确率百分比（整数）。一道没做时返回 0。"""
        return round(self.right * 100 / self.total) if self.total else 0

    def describe(self) -> str:
        if not self.total:
            return "还没做过题"
        return f"{self.total} 道，对 {self.right} 道（{self.accuracy}%）"


@dataclass
class WeekBar:
    """一周的答题情况。"""

    label: str        # "9/29"，这一周的周一
    tally: Tally = field(default_factory=Tally)

    def bar(self, peak: int, width: int = 24) -> str:
        """用字符画一条长度表示做题量的横条——比一堆数字直观。

        `peak` 是这几周里最多的一周做了多少道（满格参考）。
        **显式传进来而不是记在模块变量里**：不然画图依赖"谁先调用了谁"，很脆。
        """
        if not self.tally.total or peak <= 0:
            return ""
        filled = max(1, round(self.tally.total / peak * width))
        return "█" * min(width, filled)


@dataclass
class Overview:
    all_time: Tally = field(default_factory=Tally)
    last_7_days: Tally = field(default_factory=Tally)
    last_30_days: Tally = field(default_factory=Tally)


def _iter_answers(db: Database):
    """逐条读作答记录，时间已经转成本地时间。"""
    for row in db.conn.execute("SELECT answered_at, is_correct FROM answers"):
        when = parse_db_time(row["answered_at"])
        if when is not None:
            yield when, bool(row["is_correct"])


def overview(db: Database) -> Overview:
    """总览：全部、最近 7 天、最近 30 天。"""
    result = Overview()
    now = datetime.now()
    for when, correct in _iter_answers(db):
        result.all_time.bump(correct)
        if when >= now - timedelta(days=30):
            result.last_30_days.bump(correct)
        if when >= now - timedelta(days=7):
            result.last_7_days.bump(correct)
    return result


def _week_start(when: datetime) -> date:
    """这一周的周一（自然周，周一算第一天——跟日历习惯一致）。"""
    return (when - timedelta(days=when.weekday())).date()


def by_week(db: Database, weeks: int = DEFAULT_WEEKS) -> list[WeekBar]:
    """最近 N 周，每周做了多少、对了多少。**最近的排最后**（从左到右是时间顺序）。"""
    now = datetime.now()
    this_monday = _week_start(now)
    starts = [this_monday - timedelta(weeks=weeks - 1 - index) for index in range(weeks)]
    buckets = {start: Tally() for start in starts}
    earliest = this_monday - timedelta(weeks=weeks)

    for when, correct in _iter_answers(db):
        if when.date() < earliest:
            continue
        tally = buckets.get(_week_start(when))
        if tally is not None:
            tally.bump(correct)

    return [WeekBar(label=f"{start.month}/{start.day}", tally=buckets[start]) for start in starts]


def by_exam_type(db: Database) -> list[tuple[str, Tally]]:
    """按**考试题型**看正确率——哪个题型弱，一目了然。

    分类型算而不是只算总分：总分 70% 可能掩盖"程序填空只有 30%"这种事。
    """
    buckets: dict[str, Tally] = {}
    rows = db.conn.execute(
        """
        SELECT q.exam_type AS kind, a.is_correct AS ok
        FROM answers a
        JOIN questions q ON q.id = a.question_id
        """
    ).fetchall()
    for row in rows:
        kind = str(row["kind"] or "fill")
        buckets.setdefault(kind, Tally()).bump(bool(row["ok"]))

    def label(kind: str) -> str:
        return EXAM_TYPE_LABELS.get(kind, kind)

    # 按考试题型固定顺序排，不是按题量——这样每次打开位置都一样
    order = ("fill", "code_fill", "reading", "coding")
    return [(label(kind), buckets[kind]) for kind in order if kind in buckets]


def summarize(db: Database) -> str:
    """给状态栏用的一句话。"""
    data = overview(db)
    if not data.all_time.total:
        return "还没做过题"
    return (
        f"累计 {data.all_time.total} 道，正确率 {data.all_time.accuracy}%"
        f"（最近 7 天 {data.last_7_days.total} 道）"
    )
