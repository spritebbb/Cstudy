"""数据模型，以及 AI 返回结果的校验。

AI 返回的是 JSON，属于"系统边界之外的输入"，必须校验后才能入库。
校验失败要给出能看懂的原因，而不是丢一个 KeyError。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .logging_setup import get_logger

log = get_logger(__name__)

VALID_TYPES = ("choice", "fill", "reading", "coding")
VALID_DIFFICULTY = ("easy", "medium", "hard")

TYPE_LABELS = {
    "choice": "选择题",
    "fill": "填空题",
    "reading": "程序阅读题",
    "coding": "编程大题",
}

# ============ 考试题型（比库里的 type 细一层）============
#
# 考试是**四类**：填空 / 程序填空 / 程序阅读 / 程序设计。
# 而库里的 `type` 只有三类——**"填空"和"程序填空"都存成了 fill**。
#
# 这两个在考试里是两道不同的大题（48 分 vs 36 分），考的能力也不一样：
# 填空考"知不知道"（算个表达式、记个规则），程序填空考"会不会在代码里用"
# （看懂整段程序才能填对第一个空）。混在一起就没法专练。
#
# 所以这里按题干**现算**出考试题型，结果存在 `questions.exam_type` 里。
EXAM_TYPE_CODE_FILL = "code_fill"

EXAM_TYPE_LABELS = {
    "fill": "填空题",
    "code_fill": "程序填空题",
    "reading": "程序阅读题",
    "coding": "程序设计题",
}

# 题干里"要填的地方"的写法。
#
# **不认半角空括号 `()`**：代码里到处都是（`main()`、`printf()`），一认就全乱。
# 只认下划线、【】、全角括号——这些几乎只出现在要填的地方。
#
# 空的写法在真实材料里花样很多，认漏一种就会把一批程序填空当成普通填空：
#   `______` / `【 】` / `（ ）` / `= ;`
#   `【1】` `【2】`（小绿书就是这么编号空的，225 道题栽在这上面）
#   `填空6` `填空7`（有的卷子直接把"填空"当占位符写在代码里）
BLANK_MARK = re.compile(
    r"_{2,}|【\s*】|（\s*）|=\s*;|＝\s*;|【\s*\d+\s*】|填空\s*\d+"
)

# 代码结构。判据看这个，不看"有没有分号"——"语句 int a=5; 执行后……"这种
# 普通填空题也有分号，不能算程序填空。
CODE_START = re.compile(r"\{|#\s*include|main\s*\(")

# **像一段程序**：有函数定义或 #include。
# 比 CODE_START 严——光有个 `{` 可能只是数组初始化（`int a[3]={1,2,3};`），
# 那不叫"一段程序"，拿它判程序阅读题会误判。
PROGRAM_LIKE = re.compile(r"main\s*\(|#\s*include|\w+\s*\([^)]*\)\s*\{")

# **问句里的空**："以下程序运行后的输出结果是 ______" —— 空在问句里，
# 那是程序阅读题（让你写运行结果），不是程序填空（让你填代码）。
ANSWER_BLANK = re.compile(r"(结果|输出|值|答案|运行|为)[^\n。；]{0,10}(_{2,}|【\s*】|（\s*）)")

# 题干里明说"请填空"——普通填空题不会这么说，考的是知不知道
FILL_HINT = re.compile(r"请填空|填空|填写|空缺|填入|补齐")


def strip_answer_blanks(text: str) -> str:
    """把问句里的"答题空"去掉，只留代码。

    不先去掉它，程序阅读题会被当成程序填空——两者题干里都有代码、也都有空，
    区别只在**空在代码里还是空在问句里**。
    """
    return ANSWER_BLANK.sub("", text)


def _blanks_in_code(text: str) -> int:
    """**代码段里面**有几个空。

    先砍掉问句部分再看——"以下程序运行后的输出结果是 ______"里的空在问句里，
    不算代码里的空。
    """
    cleaned = strip_answer_blanks(text)
    start = CODE_START.search(cleaned)
    if not start:
        return 0
    return len(BLANK_MARK.findall(cleaned[start.start() :]))


def looks_like_code_fill(text: str) -> bool:
    """题干是不是**程序填空题**——给一段代码、代码里挖了空让你填。

    两条判据，满足一条就算：

    1. **代码段里的空不少于 2 个**。真程序填空通常挖 2~5 个空
       （`______(...)`、`max= ;`、`___);`）。
    2. **代码里只有 1 个空，但题干明说了"请填空"**。有些程序填空就挖一个空，
       这时候看它有没有那句说明——`填空题`考的是知不知道，不会说"请填空"。

    为什么不用"代码里只要有空就算"：`int a[3][2]={1,2,3}; 则 a[0][0] 的值为___`
    这种普通填空题也带代码、也有空，但那是**背景**，考的还是知不知道。

    **宁可漏判也不要误判**：漏判只是少几道题进"程序填空"，
    误判会让"只刷程序填空"里混进一堆别的题，那比不分类还糟。
    """
    if not CODE_START.search(text):
        return False
    blanks = _blanks_in_code(text)
    if blanks >= 2:
        return True
    return blanks == 1 and bool(FILL_HINT.search(text))


def looks_like_code_reading(text: str) -> bool:
    """题干是不是**程序阅读题**——给一段程序、问运行结果。

    这种题当初常被标成 `fill`（因为题干里有个 `____`），实际是另一道大题。

    要求得**像一段程序**（有函数定义或 `#include`），不能光看有没有 `{`——
    `int a[3]={1,2,3}; 则 a[0] 的值为___` 里的花括号只是数组初始化，
    那道题考的还是知不知道，是普通填空。
    """
    if not PROGRAM_LIKE.search(text):
        return False
    if _blanks_in_code(text):
        return False   # 代码里还有空 → 那是程序填空
    return bool(ANSWER_BLANK.search(text))


class DataError(ValueError):
    """数据不符合预期。消息会显示给用户，要说人话。"""


def exam_type_for(qtype: str, question: str) -> str:
    """**按考试口径**算出题型。`Question.exam_type` 和数据库回填都用这一个。

    跟库里的 `type` 有两处不同，都是因为 `type` 分得不够细或标错了：

    1. `fill` 里混着**程序填空题**——考试里那是独立的大题（36 分），要分出来
    2. `fill` 里还混着一批**程序阅读题**（题干是"以下程序运行后的输出结果是___"），
       当初因为题干里有个 `____` 被标成了填空。考试里这是另外一道大题（35 分）。
    """
    if qtype == "fill":
        if looks_like_code_fill(question):
            return EXAM_TYPE_CODE_FILL
        if looks_like_code_reading(question):
            return "reading"
    return qtype


@dataclass
class Question:
    type: str
    question: str
    answer: str
    explanation: str = ""
    options: list[str] = field(default_factory=list)
    difficulty: str = "medium"
    point: str = ""
    # 对应考纲知识点。point 是 AI 自由写的文本，point_id 才是跟考纲对齐的，
    # 由 scripts/map_syllabus.py 事后映射填上。没映射上就是 None。
    point_id: int | None = None

    def __post_init__(self) -> None:
        if self.type not in VALID_TYPES:
            raise DataError(f"题目类型 '{self.type}' 不认识，只能是 {VALID_TYPES} 之一")
        if self.difficulty not in VALID_DIFFICULTY:
            # 难度标错了不值得整道题作废，退回 medium 并继续
            self.difficulty = "medium"
        if not self.question.strip():
            raise DataError("题干是空的")
        if not self.answer.strip():
            raise DataError(f"题目「{self.question[:30]}…」没有答案")
        if self.type == "choice" and len(self.options) < 2:
            raise DataError(f"选择题「{self.question[:30]}…」的选项少于 2 个")

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.type, self.type)

    @property
    def exam_type(self) -> str:
        """按考试口径算出来的题型。见 `exam_type_for()`。"""
        return exam_type_for(self.type, self.question)

    @property
    def exam_type_label(self) -> str:
        return EXAM_TYPE_LABELS.get(self.exam_type, self.exam_type)

    def to_row(self) -> dict[str, Any]:
        """转成可以直接写进 questions 表的字典。"""
        return {
            "type": self.type,
            "difficulty": self.difficulty,
            "point": self.point,
            "point_id": self.point_id,
            # 考试题型是**算出来的**，不是字段——见 exam_type 属性。
            # 存进库是为了能按它做 SQL 筛选和统计。
            "exam_type": self.exam_type,
            "question": self.question,
            "options": json.dumps(self.options, ensure_ascii=False) if self.options else None,
            "answer": self.answer,
            "explanation": self.explanation,
        }

    @classmethod
    def from_row(cls, row) -> "Question":
        """从数据库的一行还原成 Question。

        options 列存的是 JSON 字符串，这里解析回来。解析失败就当作没有选项——
        库里的一条坏数据不该让整个界面崩掉。
        """
        options: list[str] = []
        raw_options = row["options"] if "options" in row.keys() else None
        if raw_options:
            try:
                loaded = json.loads(raw_options)
            except (TypeError, ValueError):
                log.warning("题目选项解析失败，当作没有选项：%.80s", raw_options)
            else:
                if isinstance(loaded, list):
                    options = [str(item) for item in loaded]

        def field(name: str, default: str = "") -> str:
            if name not in row.keys():
                return default
            return str(row[name] or default).strip()

        qtype = field("type").lower()
        if qtype == "choice" and len(options) < 2:
            # 选项没存下来或者坏了，自动判分已经不可能。退化成填空题，
            # 至少还能让用户对着参考答案自己看——总比整个界面崩掉强。
            log.warning("选择题的选项不可用，退化成填空题：%.40s", field("question"))
            qtype = "fill"

        point_id: int | None = None
        if "point_id" in row.keys() and row["point_id"] is not None:
            try:
                point_id = int(row["point_id"])
            except (TypeError, ValueError):
                log.warning("point_id 不是整数，当作没映射：%r", row["point_id"])

        return cls(
            type=qtype,
            question=field("question"),
            answer=field("answer"),
            explanation=field("explanation"),
            options=options,
            difficulty=field("difficulty", "medium").lower() or "medium",
            point=field("point"),
            point_id=point_id,
        )


def parse_json_object(payload: str | dict) -> dict:
    """把 AI 返回的内容解析成 dict。

    模型有时会用 ```json 围栏包起来，这里一并处理。
    """
    if isinstance(payload, dict):
        return payload

    text = str(payload or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataError(f"AI 返回的不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise DataError("AI 返回的 JSON 顶层应该是一个对象")
    return data


def parse_questions(payload: str | dict | list[dict]) -> list[Question]:
    """把 AI 返回的题目解析成 Question 列表。

    接受 JSON 字符串、含 questions 字段的对象、或已经解析好的列表。
    单道题有问题就跳过那道，不因为一道题毁掉整次结果。
    """
    if isinstance(payload, (str, dict)):
        payload = parse_json_object(payload).get("questions", [])
    if not isinstance(payload, list):
        raise DataError("AI 返回的题目格式不对，应该是一个列表")

    questions: list[Question] = []
    problems: list[str] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            problems.append(f"第 {index} 题不是对象")
            continue
        try:
            questions.append(
                Question(
                    type=str(item.get("type", "")).strip().lower(),
                    question=str(item.get("question", "")).strip(),
                    answer=str(item.get("answer", "")).strip(),
                    explanation=str(item.get("explanation", "")).strip(),
                    options=[str(o) for o in item.get("options") or []],
                    difficulty=str(item.get("difficulty", "medium")).strip().lower(),
                    point=str(item.get("point", "")).strip(),
                )
            )
        except DataError as exc:
            problems.append(f"第 {index} 题被跳过：{exc}")

    if not questions:
        if not payload:
            # AI 明确回了空列表，表示"这节没有可考的 C 语言内容"（比如开场课）。
            # 出 0 道题是正确结果，不是错误。
            return []
        detail = "；".join(problems) if problems else "返回内容是空的"
        raise DataError(f"一道题都没解析出来。{detail}")

    return questions


# ============ AI 复查（二次校准）的结果 ============


@dataclass
class ReviewNote:
    """复查对某一项的处置记录。

    题目复查时 subject 是题干；笔记复查时是"被改动的内容"的简述。
    """

    subject: str
    reason: str
    index: int = 0         # 题目在原始列表里的序号，从 1 开始；笔记复查用不到，为 0

    def __post_init__(self) -> None:
        self.subject = _shorten(self.subject, 60) or "（没说是什么）"
        self.reason = str(self.reason).strip() or "（没说明原因）"

    def describe(self) -> str:
        where = f"第 {self.index} 题" if self.index else "笔记"
        return f"{where}「{self.subject}」：{self.reason}"


@dataclass
class ReviewReport:
    """一次 AI 复查的结果。

    复查失败**不是**致命错误——原来的内容照常保留，只是没人把关而已。
    所以这里用 ok/error 表达，而不是抛异常。
    """

    removed: list[ReviewNote] = field(default_factory=list)
    fixed: list[ReviewNote] = field(default_factory=list)
    ok: bool = True
    error: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.removed or self.fixed)

    def describe(self) -> str:
        """写成给用户看的中文说明。没有改动时返回空字符串。

        返回**纯文本**——它直接进界面的进度区（QPlainTextEdit），用 Markdown 记号会露出来。
        """
        if not self.ok:
            return f"复查没成功（{self.error}），这次的内容没有经过二次把关。"
        if not self.changed:
            return ""
        lines = ["复查做了这些调整："]
        lines += [f"  · 改了 {note.describe()}" for note in self.fixed]
        lines += [f"  · 删了 {note.describe()}" for note in self.removed]
        return "\n".join(lines)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def parse_review_report(payload: Any) -> ReviewReport:
    """从复查返回的 JSON 里取出 removed / fixed。

    这是"系统边界之外的输入"，字段缺了、类型不对都不能抛异常——
    复查本身出问题不该把整个导入搞挂。解析不出来的部分直接忽略。
    """
    if not isinstance(payload, dict):
        return ReviewReport(ok=False, error="AI 返回的不是预期的 JSON 结构")

    def collect(key: str) -> list[ReviewNote]:
        raw = payload.get(key)
        if not isinstance(raw, list):
            return []
        notes: list[ReviewNote] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                index = int(item.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            notes.append(
                ReviewNote(
                    subject=str(item.get("subject") or ""),
                    reason=str(item.get("reason") or ""),
                    index=index,
                )
            )
        return notes

    return ReviewReport(removed=collect("removed"), fixed=collect("fixed"))
