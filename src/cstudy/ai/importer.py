"""导入现成题库：把用户粘贴的一段题目文字，整理成结构化题目。

和"从视频出题"的区别：这条路**不做 AI 复查**。原文是权威的（尤其是真题），
让 AI 去"纠正"答案反而可能把对的改错。改成入库前先给用户看一遍预览，人来把关。

这块只负责"文字 → 题目"，存不存、存哪些由界面决定。
"""

from __future__ import annotations

import re
from typing import Callable

from ..config import Config
from ..logging_setup import get_logger
from ..models import DataError, Question, parse_questions
from . import prompts
from .client import AIError, DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_MAX_CHARS = 8000

# 题号的各种写法：1.  1、  1)  1）  第1题
QUESTION_START = re.compile(r"^\s*(?:\d{1,3}\s*[.、,，)）]|第\s*\d+\s*题)", re.MULTILINE)


def split_by_questions(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """按题号把文字切成若干块，每块尽量不超过 max_chars。

    **不按字符硬切**——切在题目中间会把这题毁掉，AI 拿到半道题只能丢掉。
    整段找不到题号就原样返回一块，交给 AI 自己看着办。
    """
    starts = [match.start() for match in QUESTION_START.finditer(text)]
    if not starts:
        return [text]

    blocks = [
        text[start : starts[index + 1] if index + 1 < len(starts) else len(text)]
        for index, start in enumerate(starts)
    ]
    if starts[0] > 0:
        # 第一个题号之前可能还有说明文字（"一、选择题"之类），别丢了
        blocks.insert(0, text[: starts[0]])

    chunks: list[str] = []
    current = ""
    for block in blocks:
        if current and len(current) + len(block) > max_chars:
            chunks.append(current)
            current = block
        else:
            current += block
    if current.strip():
        chunks.append(current)
    return chunks


def dedupe(questions: list[Question]) -> tuple[list[Question], int]:
    """去掉题干完全一样的重复题。返回 (去重后的列表, 去掉了几道)。"""
    seen: set[str] = set()
    kept: list[Question] = []
    for question in questions:
        key = " ".join(question.question.split())
        if key in seen:
            continue
        seen.add(key)
        kept.append(question)
    return kept, len(questions) - len(kept)


class QuestionImporter:
    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        self.max_chars = int(
            config.get("import.max_chars_per_call", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS
        )

    def parse(
        self, text: str, on_progress: ProgressCallback | None = None
    ) -> list[Question]:
        """把粘贴的文字解析成题目列表。解析不出任何题目会抛异常。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("没有可导入的内容")

        chunks = split_by_questions(text, self.max_chars)
        log.info("导入题库：%d 字，切成 %d 块", len(text), len(chunks))

        questions: list[Question] = []
        failed = 0
        for index, chunk in enumerate(chunks, start=1):
            if on_progress:
                on_progress(f"正在解析第 {index}/{len(chunks)} 段…")
            try:
                raw = self.client.chat_json(
                    [
                        {"role": "system", "content": prompts.IMPORT_SYSTEM},
                        {"role": "user", "content": prompts.import_user_prompt(chunk)},
                    ]
                )
                questions.extend(parse_questions(raw))
            except DataError:
                # 这一段里本来就没题目（比如开头的说明文字），不算失败
                log.info("第 %d 段里没有题目，跳过", index)
            except Exception as exc:
                failed += 1
                log.warning("第 %d 段解析失败：%s: %s", index, type(exc).__name__, exc)

        if not questions:
            if failed == len(chunks):
                raise AIError(
                    "这段文字没能解析出题目。可能是格式太乱，或者里面没有 C 语言题。\n"
                    "可以只粘贴题目部分再试一次。"
                )
            raise DataError("这段文字里没有解析出任何 C 语言题目。")

        questions, dropped = dedupe(questions)
        if dropped:
            log.info("去掉了 %d 道重复题", dropped)

        log.info("导入解析完成：%d 道（%d 段失败）", len(questions), failed)
        return questions
