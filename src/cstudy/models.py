"""数据模型，以及 AI 返回结果的校验。

AI 返回的是 JSON，属于"系统边界之外的输入"，必须校验后才能入库。
校验失败要给出能看懂的原因，而不是丢一个 KeyError。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

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
            "question": self.question,
            "options": json.dumps(self.options, ensure_ascii=False) if self.options else None,
            "answer": self.answer,
            "explanation": self.explanation,
        }


def parse_questions(payload: str | list[dict]) -> list[Question]:
    """把 AI 返回的题目解析成 Question 列表。

    接受 JSON 字符串或已经解析好的列表。单道题有问题就跳过那道，
    不因为一道题毁掉整次结果。
    """
    if isinstance(payload, str):
        text = payload.strip()
        # 模型有时会用 ```json 包起来
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DataError(f"AI 返回的不是合法 JSON：{exc}") from exc

    if isinstance(payload, dict):
        payload = payload.get("questions", [])
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
        detail = "；".join(problems) if problems else "返回内容是空的"
        raise DataError(f"一道题都没解析出来。{detail}")

    return questions
