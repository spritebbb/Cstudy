"""数据模型，以及 AI 返回结果的校验。

AI 返回的是 JSON，属于"系统边界之外的输入"，必须校验后才能入库。
校验失败要给出能看懂的原因，而不是丢一个 KeyError。
"""

from __future__ import annotations

import json
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


class DataError(ValueError):
    """数据不符合预期。消息会显示给用户，要说人话。"""


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

    def to_row(self) -> dict[str, Any]:
        """转成可以直接写进 questions 表的字典。"""
        return {
            "type": self.type,
            "difficulty": self.difficulty,
            "point": self.point,
            "point_id": self.point_id,
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
