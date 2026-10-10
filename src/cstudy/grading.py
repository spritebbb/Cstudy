"""判分：把 AI 给的答案规范化，再跟用户的选择比对。

**这个模块不 import Qt，也不碰数据库**——判分是最容易出错、最需要单测的部分，
跟界面解耦才测得好。界面只做搬运。

为什么非要"规范化"：`Question.answer` 是 AI 原样返回的自由文本。提示词里虽然
规定选择题只填字母，但实测它并不总是守规矩，见过的形态有：

    "B" / "b" / "Ｂ"（全角） / "B. 选项内容" / "答案：B" / "选 B"

直接拿字符串比会把这些全判成错，用户会以为自己答错了——这比不判分更糟。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 选项前缀：A. / A、 / A) / A） / A． / "A 内容"（字母后跟分隔符或空白）
_OPTION_PREFIX = re.compile(r"^\s*[A-Za-z](?:\s*[.、,，)）:：．]\s*|\s+)")

# 答案里常见的废话："正确答案是 B"、"选 B"、"答案：B"
_ANSWER_NOISE = re.compile(r"^(?:正确答案|正确选项|答案|选项|选|是|为|应该)[\s:：是]*")


@dataclass
class GradeResult:
    """一道选择题的判分结果。"""

    correct: bool | None   # None 表示没法自动判，交给用户自己对照
    expected: str = ""     # 规范化后的正确答案字母，认不出来时是空字符串
    note: str = ""         # 给用户看的一句说明

    @property
    def can_auto_grade(self) -> bool:
        return self.correct is not None


def to_halfwidth(text: str) -> str:
    """全角转半角：Ｂ → B，１ → 1，： → :。"""
    return unicodedata.normalize("NFKC", text or "")


def strip_option_prefix(text: str) -> str:
    """去掉选项前面的字母前缀：'A. 内容' → '内容'。

    只在开头确实是"单个字母 + 分隔符或空白"时才去掉。
    这样 'Array 的首地址' 这种正文不会被削成 'rray 的首地址'。
    """
    return _OPTION_PREFIX.sub("", text or "", count=1).strip()


def normalize_choice_answer(raw: str, option_count: int = 0) -> str | None:
    """把 AI 给的选择题答案规范成单个大写字母（A/B/C/…）。

    认不出来就返回 None——**不猜**。猜错会让用户以为自己答错了，
    比老实说"这题没法自动判"更伤人。
    """
    text = to_halfwidth(raw or "").strip()
    if not text:
        return None

    text = _ANSWER_NOISE.sub("", text, count=1).strip()
    if not text:
        return None

    # 首选：开头就是单个字母（后面是分隔符或直接结束）
    for match in (
        re.match(r"^([A-Za-z])(?![A-Za-z])", text),
        # 退一步：整个串里第一个孤立的字母，对付"选项B""我认为是B"这类
        re.search(r"(?<![A-Za-z])([A-Za-z])(?![A-Za-z])", text),
    ):
        if not match:
            continue
        letter = match.group(1).upper()
        if not option_count or ord(letter) - ord("A") < option_count:
            return letter

    return None


def describe_expected(options: list[str], expected: str) -> str:
    """把答案字母还原成"B. 选项内容"，方便用户对照。"""
    if not expected:
        return ""
    index = ord(expected) - ord("A")
    if 0 <= index < len(options):
        return options[index]
    return expected


def grade_choice(user_index: int, options: list[str], answer_raw: str) -> GradeResult:
    """判一道选择题。

    用户选的是**下标**，这里统一换算成字母再比——比字母不比下标，
    免得 AI 给的选项顺序漂移（同一道题重出一次，B 和 C 调了个个儿）。
    """
    expected = normalize_choice_answer(answer_raw, len(options))
    if expected is None:
        return GradeResult(
            correct=None,
            note="这道题的参考答案格式不标准，没法自动判分，请自己对照。",
        )

    if not 0 <= user_index < len(options):
        return GradeResult(correct=False, expected=expected, note="没有选中任何选项。")

    chosen = chr(ord("A") + user_index)
    return GradeResult(correct=(chosen == expected), expected=expected)
