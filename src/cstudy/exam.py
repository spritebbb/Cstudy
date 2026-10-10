"""从题库里按考试题型分值组一套模拟卷。

跟 `ai/quiz.py` 那条路**完全不同**：那条是从笔记**生成新题**（要调 AI）；
这条只是从已有的题里**按比例挑**，不调 AI、不花钱。

分值口径来自目标学校（湖北工程学院）公布的题型分值，考试是**四类**：
填空 48 / 程序填空 36 / 程序阅读 35 / 程序设计 31（共 150 分）。

**注意**：库里的 `type` 只有三类——"填空"和"程序填空"都是 `fill`。
所以按**考试题型** `exam_type` 挑（多一个 `code_fill`），这样才能跟考试的结构对上。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .db import Database
from .logging_setup import get_logger
from .models import EXAM_TYPE_LABELS, Question

log = get_logger(__name__)

DEFAULT_TOTAL = 20

# 考试四类各自的分值。加得起来正好 150 —— 没有给选择题留分。
DEFAULT_RATIOS = {
    "fill": 48,
    "code_fill": 36,
    "reading": 35,
    "coding": 31,
}


@dataclass
class ExamPlan:
    """一套卷子每种题型抽几道。"""

    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def describe(self) -> str:
        parts = [
            f"{EXAM_TYPE_LABELS.get(k, k)} {v} 道" for k, v in self.counts.items() if v
        ]
        return "、".join(parts) if parts else "（没算出题量）"


@dataclass
class ExamPaper:
    questions: list[Question] = field(default_factory=list)
    question_ids: list[int] = field(default_factory=list)
    plan: ExamPlan = field(default_factory=ExamPlan)
    shortfall: dict[str, int] = field(default_factory=dict)   # 哪种题型没凑够，差几道

    @property
    def summary(self) -> str:
        text = f"组了 {len(self.questions)} 道题（{self.plan.describe()}）"
        if self.shortfall:
            missing = "、".join(
                f"{EXAM_TYPE_LABELS.get(k, k)}差 {v} 道" for k, v in self.shortfall.items()
            )
            text += f"；题库里不够：{missing}"
        return text


def _positive_int(config: Config, key: str, default: int) -> int:
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log.warning("%s 配置看不懂（%r），用默认值 %d", key, raw, default)
        return default
    if value <= 0:
        log.warning("%s 是 %d，没意义，用默认值 %d", key, value, default)
        return default
    return value


def build_exam_plan(config: Config) -> ExamPlan:
    """按分值比例算题量。

    用**最大余额法**分配：先按比例取整，余数给小数部分最大的那几个题型。
    逐个四舍五入会让总数对不上（要 20 道，算出来只有 19 道）。
    """
    total = _positive_int(config, "exam.total_questions", DEFAULT_TOTAL)

    weights: dict[str, float] = {}
    for kind, default in DEFAULT_RATIOS.items():
        raw = config.get(f"exam.ratios.{kind}", default)
        try:
            weights[kind] = max(0.0, float(raw))
        except (TypeError, ValueError):
            log.warning("exam.ratios.%s 配置看不懂（%r），用默认值 %s", kind, raw, default)
            weights[kind] = float(default)

    weights = {kind: value for kind, value in weights.items() if value > 0}
    if not weights:
        log.warning("exam.ratios 全是 0，退回默认比例")
        weights = {kind: float(v) for kind, v in DEFAULT_RATIOS.items()}

    scale = sum(weights.values())
    exact = {kind: total * value / scale for kind, value in weights.items()}
    counts = {kind: int(value) for kind, value in exact.items()}

    remainder = total - sum(counts.values())
    for kind in sorted(exact, key=lambda k: exact[k] - counts[k], reverse=True)[:remainder]:
        counts[kind] += 1

    # 比例里列了的题型至少给 1 道——"程序设计 0 道"的卷子没意义。
    # 从最多的那个题型借，保证总数不变。
    if total >= len(counts):
        for kind, count in counts.items():
            if count == 0:
                biggest = max(counts, key=lambda k: counts[k])
                counts[biggest] -= 1
                counts[kind] = 1

    return ExamPlan(counts)


def build_exam_paper(db: Database, config: Config) -> ExamPaper:
    """从题库里按比例随机抽一套卷子。

    某个题型不够时**如实少给并记下来**——不拿别的题型凑数，
    也不重复抽同一道题（`query_questions` 一次一条 SQL 取，天然不重复）。
    """
    plan = build_exam_plan(config)
    questions: list[Question] = []
    ids: list[int] = []
    shortfall: dict[str, int] = {}

    for kind, want in plan.counts.items():
        # 按**考试题型**挑（exam_type），不是库里的 type——不然"程序填空"会跟
        # 普通填空混在一起，组出来的卷子跟考试的题型结构对不上
        rows = db.query_questions(exam_types=[kind], shuffle=True, limit=want)
        if len(rows) < want:
            shortfall[kind] = want - len(rows)
        for row in rows:
            try:
                questions.append(Question.from_row(row))
                ids.append(int(row["id"]))
            except Exception:
                # 库里一条坏数据不该让整张卷子组不出来
                log.warning("跳过一道读不出来的题目：id=%s", row["id"], exc_info=True)

    log.info("组卷完成：%s", ExamPaper(questions, ids, plan, shortfall).summary)
    return ExamPaper(questions, ids, plan, shortfall)
