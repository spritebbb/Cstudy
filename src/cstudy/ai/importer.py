"""导入现成题库：把用户粘贴的一段题目文字，整理成结构化题目。

和"从视频出题"的区别：这条路**不做 AI 复查**。原文是权威的（尤其是真题），
让 AI 去"纠正"答案反而可能把对的改错。改成入库前先给用户看一遍预览，人来把关。

这块只负责"文字 → 题目"，存不存、存哪些由界面决定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..config import Config, ConfigError
from ..logging_setup import get_logger
from ..models import TYPE_LABELS, DataError, Question, parse_questions
from . import prompts
from .client import AIError, DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_MAX_CHARS = 8000

# 题号的各种写法：1.  1、  1)  1）  第1题
QUESTION_START = re.compile(r"^\s*(?:\d{1,3}\s*[.、,，)）]|第\s*\d+\s*题)", re.MULTILINE)

# 文末答案区的标题。要**独占一行**才算——"答案填写在答题纸上"这种不是标题。
# 行尾允许 \r：PDF/Word 抽出来的文字常常是 \r\n 换行，漏了它这一行就匹配不上。
ANSWER_HEADING = re.compile(
    r"^[ \t]*(?:参考答案|标准答案|答案与解析|参考答案与解析|试题答案|答案)"
    r"[ \t]*[:：]?[ \t\r]*$",
    re.MULTILINE,
)

# 答案区落在全文这么靠后，才认它是答案区（防止误切中间的"答案"小标题）
ANSWER_ZONE_START = 0.4


def split_answers(text: str) -> tuple[str, str]:
    """把文末的答案区切出来，返回 (题目部分, 答案部分)。

    **必须切出来单独用**：按题号切块时，答案区会被切到另一块去，
    结果那块有答案没题目、这块有题目没答案，AI 只能瞎猜答案。
    实测踩过：一份 8473 字的卷子，答案区从第 7895 字开始，而切块边界在 7976——
    题目和答案正好被切在两块里。
    """
    for match in ANSWER_HEADING.finditer(text):
        if match.start() >= len(text) * ANSWER_ZONE_START:
            return text[: match.start()], text[match.start() :]
    return text, ""


@dataclass
class ImportOutcome:
    """一次导入的结果。

    **所有数字都摆出来**——用户要能核对"有没有丢题"。AI 解析有可能悄悄少给
    几道，只有把数字摊开才看得出来。
    """

    questions: list[Question] = field(default_factory=list)
    parsed: int = 0                                       # AI 一共解析出多少道（过滤前）
    skipped: dict[str, int] = field(default_factory=dict)  # 按题型过滤掉多少
    duplicates: int = 0                                   # 重复去掉多少
    failed: int = 0                                       # 有多少段压根没解析成功

    @property
    def kept(self) -> int:
        return len(self.questions)

    def summary(self) -> str:
        """给用户看的一行交代。"""
        parts = [f"解析出 {self.parsed} 道"]
        if self.failed:
            # 这一段没解析成功 = 里面的题一道都没进来。必须说出来，
            # 否则用户看到"保留 N 道"会以为全都解析到了。
            parts.append(f"有 {self.failed} 段没解析成功（可能少了题）")
        if self.duplicates:
            parts.append(f"去掉重复 {self.duplicates} 道")
        for kind, count in self.skipped.items():
            parts.append(f"过滤{TYPE_LABELS.get(kind, kind)} {count} 道")
        parts.append(f"最终保留 {self.kept} 道")
        return "，".join(parts)


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
        # 要过滤掉的题型。默认过滤选择题——湖北专升本这几所都不考。
        # 注意是"**先让 AI 全解析出来、再在代码里过滤**"，不是让 AI 直接跳过：
        # 那样万一它顺手把别的题型也跳了，就悄悄丢题了，而用户根本发现不了。
        raw = config.get("import.skip_types", ["choice"])
        if raw is None:
            raw = []
        elif isinstance(raw, str):
            # 用户漏了前面的 "-"，写成 `skip_types: choice`。
            # 直接拿字符串遍历会被**逐字符**拆成 {'c','e','h',...}，过滤静默失效，
            # 选择题照旧全进来，用户还以为工具坏了。这里当成"就这一项"。
            raw = [raw]
        elif not isinstance(raw, (list, tuple)):
            raise ConfigError(
                "config.yaml 里 import.skip_types 写错了：它应该是一行一个的列表，比如\n"
                "  skip_types:\n"
                "    - choice\n"
                f"现在读到的是 {type(raw).__name__}，改过来再启动。"
            )
        self.skip_types = {str(item).strip().lower() for item in raw if str(item).strip()}

    def parse(
        self,
        text: str,
        on_progress: ProgressCallback | None = None,
        reference: str = "",
    ) -> ImportOutcome:
        """把文字解析成题目。解析不出任何题目（或被过滤光）会抛异常。

        reference 是**外部的**配套答案（比如同文件夹里的"参考答案.pdf"）。
        文末自带的答案区会自动切出来，不用传。
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("没有可导入的内容")

        # 答案区切出来单独用，别让它跟着题号被切到别的块去
        body, answers = split_answers(text)
        reference = "\n\n".join(part for part in (reference, answers) if part.strip())
        if reference:
            log.info("配套答案 %d 字，会附给每一段", len(reference))

        chunks = split_by_questions(body, self.max_chars)
        log.info("导入题库：%d 字，切成 %d 块", len(body), len(chunks))

        questions: list[Question] = []
        failed = 0
        for index, chunk in enumerate(chunks, start=1):
            if on_progress:
                on_progress(f"正在解析第 {index}/{len(chunks)} 段…")
            try:
                raw = self.client.chat_json(
                    [
                        {"role": "system", "content": prompts.IMPORT_SYSTEM},
                        {
                            "role": "user",
                            "content": prompts.import_user_prompt(chunk, reference),
                        },
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

        outcome = ImportOutcome(parsed=len(questions), failed=failed)

        # 先按题型过滤，再去重——顺序反了会把"重复的选择题"也算进重复里
        kept: list[Question] = []
        for question in questions:
            if question.type in self.skip_types:
                outcome.skipped[question.type] = outcome.skipped.get(question.type, 0) + 1
                continue
            kept.append(question)

        kept, outcome.duplicates = dedupe(kept)
        outcome.questions = kept

        if not kept:
            detail = "、".join(
                f"{TYPE_LABELS.get(kind, kind)} {count} 道"
                for kind, count in outcome.skipped.items()
            )
            raise DataError(
                f"解析出来的 {outcome.parsed} 道题全被过滤掉了（{detail}）。"
                "这份材料里没有你要的题型。"
            )

        log.info("导入解析完成：%s", outcome.summary())
        return outcome
