"""考前突击用的临时题库：**完全隔离**的那一栏。

## 为什么要单独一个库文件

主题库里装的是专升本 C 语言的题（题目、知识点、作答记录、错题本）；
这一栏装的是另一门考试（比如《网络安全管理员》）的题。两者混在一个库里，
筛选刷题、组卷、考纲覆盖度、学情统计会全部被污染——用户的原话是
「从这栏导入的题库不和主题库混合，只能在这栏里使用」。

所以隔离做在**库文件**这一层，而不是靠查询条件去绕：
`data/temp_bank/bank.db` 是一个独立的 SQLite 文件，主题库连它的表都看不到。
隔离是结构上的，不依赖任何一处的 SQL 写得对。

代价有两面，写在这里免得以后有人被坑：
- 好处：删掉这个文件＝这一栏彻底消失，主题库纹丝不动；主题库备份/恢复也不影响它。
- 代价：两边的错题本和作答统计不通用（临时栏做错的题不会进主题库错题本）。
  这也是有意的——考完就删，本来就不该污染长期数据。
  这一栏**自己**有错题集：做错的题收进来（默认错 1 次就收，见 `WRONG_MIN_TIMES`），
  连着答对 2 次移出去（见 `WrongItem`）。

## 题型口径

这里只有四类：单选 / 多选 / 判断 / 问答。**故意不复用** `models.exam_type_for()`，
因为那套口径是 C 语言考试的（填空 / 程序填空 / 程序阅读 / 程序设计），
跟这张卷子上的题型压根不是一回事。也不读 `config.import.skip_types`
（那里配的是"过滤掉选择题"，可是这张卷子里选择题占 50 分）。

## 解析路线：规则，不调 AI

这份卷子的排版是机器排的、极规整：

    [单选题]题干（    ）。
    A.选项
    B.选项
    知识点：交换机安全配置     ← 可有可无
    答案：B
    解析：……                  ← 可以是空的

每题都以 `[单选题]` 这种标记开头，答案一个不缺。实测两个文件 141+141 道题、
282 个答案全在，所以用正则拆比让 AI 猜**又准又快又不要钱**，而且能写单测盯住。
代价写在 `parse_bank_text` 的说明里：换一种排版就得改代码。
"""

from __future__ import annotations

import json
import random
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from .file_text import SUPPORTED, extract
from .logging_setup import get_logger

log = get_logger(__name__)

# ---------- 题型 ----------

TYPE_LABELS = {
    "single": "单选题",
    "multi": "多选题",
    "judge": "判断题",
    "short": "问答题",
}
TYPE_ORDER = ("single", "multi", "judge", "short")

# 标记里的词 → 我们的四类。取自卷面写法，不靠猜。
# 顺序就是优先级：**多选必须排在单选前面**——"多项选择题" / "不定项选择题"
# 里也含"选择题"三个字，顺序反了就会被当成单选。
_LABEL_RULES = (
    ("multi", ("多选", "多项选择", "不定项")),
    ("judge", ("判断",)),
    ("short", ("问答", "简答", "名词解释", "论述", "计算")),
    ("single", ("单选", "单项选择", "选择题")),
)

# `[单选题]` / `【多选题】` 这种行首标记。要求方括号里以"题"结尾，
# 免得把正文里的 `[1]`、`[注]` 也当成题目开头。
MARKER = re.compile(r"^[ \t]*[\[【]([^\]】\n]{1,14}?题)[\]】][ \t]*(.*)$", re.MULTILINE)
# 选项行：`A.文本` / `A、文本` / `A）文本` / `A：文本`
OPTION = re.compile(r"^([A-Ha-h])[.、,，)）:：][ \t]*(.*)$")
ANSWER = re.compile(r"^(?:参考)?答案[ \t]*[：:][ \t]*(.*)$")
EXPLAIN = re.compile(r"^(?:解析|解答|说明|分析)[ \t]*[：:][ \t]*(.*)$")
KNOWLEDGE = re.compile(r"^知识点[ \t]*[：:][ \t]*(.*)$")

_JUDGE_TRUE = {"正确", "对", "是", "√", "t", "true", "yes"}
_JUDGE_FALSE = {"错误", "错", "否", "×", "x", "f", "false", "no"}

# 题干里这些字符只影响比对，不影响内容——去重时统一抹掉
_BLANK = re.compile(r"[\s\u3000]+")


class TempBankError(RuntimeError):
    """消息直接给用户看，所以要说人话。"""


# ---------- 数据结构 ----------


@dataclass
class TempQuestion:
    """一道临时题。

    `answer` 的写法定死成：单选 `"B"`、多选 `"ABD"`、判断 `"正确"` / `"错误"`、
    问答是参考答案原文（多行）。展示和判分都按这个约定来。
    """

    qtype: str
    stem: str
    options: list[str] = field(default_factory=list)
    answer: str = ""
    explanation: str = ""
    knowledge: str = ""
    source: str = ""
    id: int | None = None

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.qtype, self.qtype)

    @property
    def stem_key(self) -> str:
        """去重用的键：题干里的空白全部抹掉再比。"""
        return _BLANK.sub("", self.stem)

    def answer_display(self) -> str:
        """参考答案给用户看的样子——光给字母还得回去数第几个。"""
        if self.qtype == "single" and self.options:
            letter = normalize_letters(self.answer)
            if letter:
                index = ord(letter[0]) - ord("A")
                if 0 <= index < len(self.options):
                    return f"{letter[0]}. {self.options[index]}"
        if self.qtype == "multi" and self.options:
            letters = normalize_letters(self.answer)
            if letters:
                parts = []
                for char in letters:
                    index = ord(char) - ord("A")
                    if 0 <= index < len(self.options):
                        parts.append(f"{char}. {self.options[index]}")
                if parts:
                    return "　".join(parts)
        return self.answer

    def check(self, user_answer: str) -> bool | None:
        """判分。问答题返回 None（没法自动判，交给用户自己对照）。"""
        if self.qtype in {"single", "multi"}:
            mine = normalize_letters(user_answer)
            expected = normalize_letters(self.answer)
            if not mine or not expected:
                return None
            return mine == expected
        if self.qtype == "judge":
            mine = normalize_judge(user_answer)
            expected = normalize_judge(self.answer)
            if mine is None or expected is None:
                return None
            return mine == expected
        return None


@dataclass
class ParseOutcome:
    """一个文件解析出来的东西 + 哪里没读懂。"""

    source: str = ""
    total_marks: int = 0          # 认出来的题目标记数
    questions: list[TempQuestion] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def by_type(self) -> dict[str, int]:
        counts = {key: 0 for key in TYPE_ORDER}
        for question in self.questions:
            counts[question.qtype] = counts.get(question.qtype, 0) + 1
        return counts

    def summary_line(self) -> str:
        counts = self.by_type
        parts = [f"{TYPE_LABELS[key]} {counts.get(key, 0)}" for key in TYPE_ORDER if counts.get(key)]
        return "、".join(parts) if parts else "没有题目"


# 错几次才收进错题集。用户最初的原话是"多次做错的题进行搜集"（当时定的是 2 次），
# 2026-10-15 用户直接要求"把错题判断改成1"——错一道就收，宁可多收也不漏。
WRONG_MIN_TIMES = 1
# 连着答对几次就移出错题集。跟主题库错题本一个口径（`wrong_book.master_after_correct`）：
# 状态是**算出来的**，不写库，也就不会出现"字段说掌握了、记录却不是"。
WRONG_MASTER_AFTER = 2


@dataclass
class WrongItem:
    """错题集里的一道题。"""

    question: TempQuestion
    wrong_count: int
    consecutive_correct: int = 0
    last_wrong_at: str | None = None

    @property
    def mastered(self) -> bool:
        """连着答对够了就算掌握（这个属性只是给界面/测试看的，判断在查询里做）。"""
        return self.consecutive_correct >= WRONG_MASTER_AFTER

    def describe(self) -> str:
        parts = [f"错过 {self.wrong_count} 次"]
        if self.last_wrong_at:
            parts.append(f"最近 {self.last_wrong_at[:16].replace('T', ' ')}")
        return "，".join(parts)


# ---------- 纯函数：判分用 ----------


def normalize_letters(raw: str) -> str:
    """`"b、d ,a"` → `"ABD"`。只认 A~H 的字母，其他字符（顿号、空格）当分隔符。"""
    found = {char.upper() for char in re.findall(r"[A-Ha-h]", raw or "")}
    return "".join(sorted(found))


def normalize_judge(raw: str) -> bool | None:
    """判断题答案 → True / False。认不出来的返回 None。"""
    text = (raw or "").strip().lower()
    if text in _JUDGE_TRUE:
        return True
    if text in _JUDGE_FALSE:
        return False
    return None


def label_to_type(label: str) -> str | None:
    for qtype, words in _LABEL_RULES:
        if any(word in label for word in words):
            return qtype
    return None


# ---------- 纯函数：打乱选项 ----------

# 解析里"引用选项字母"的样子。命中就不打乱选项——打乱之后"选项A是中继器功能"
# 会指到别的选项上去，**解析就从对的变成错的**，这比记不住顺序严重。
# 只认这几种明确写法，不认"孤零零一个字母"：真实题库里 `ARP` / `AP` / `IP`
# 这类英文术语满地都是，按单个字母替换会把解析改烂。
# 实测：279 道里只有 2 道命中（99 道有解析）。
EXPLANATION_LETTERS = re.compile(
    r"选项\s*[A-H]|^\s*[A-H]\s*[.、．)）]|[A-H]\s*项", re.MULTILINE
)


def uses_option_letters(question: TempQuestion) -> bool:
    """这道题的解析里提到了选项字母吗？（提到了就不打乱它的选项）"""
    return bool(EXPLANATION_LETTERS.search(question.explanation or ""))


def shuffle_options(question: TempQuestion, rng: random.Random | None = None) -> TempQuestion:
    """把选项顺序打乱，**答案的字母跟着改**。

    答案字母是按**选项位置**记的（`answer="C"` 指第 3 个选项，见 `TempQuestion`），
    所以打乱以后必须重算，否则判分就反了。判断题没有可换的选项（就是"正确/错误"），
    问答题没有选项，这两类原样返回。

    保证换出来的顺序**跟原来不一样**（除非只有一个选项）：用户要的是"重新刷的时候
    换个顺序"，偶尔原样返回会让人以为功能没生效。
    """
    if question.qtype not in {"single", "multi"} or len(question.options) < 2:
        return question
    if uses_option_letters(question):
        return question
    letters = normalize_letters(question.answer)
    if not letters:
        return question
    indexes = [ord(char) - ord("A") for char in letters]
    if any(index >= len(question.options) for index in indexes):
        return question      # 答案对不上选项，这题本身有问题，别动

    order = list(range(len(question.options)))
    (rng or random).shuffle(order)
    if order == list(range(len(order))):        # 洗出来还是原样：手动换一下前两个
        order[0], order[1] = order[1], order[0]

    moved = {old: new for new, old in enumerate(order)}
    new_answer = "".join(sorted(chr(ord("A") + moved[index]) for index in indexes))
    return replace(
        question,
        options=[question.options[index] for index in order],
        answer=new_answer,
    )


# ---------- 解析 ----------


def parse_bank_text(text: str, source: str = "") -> ParseOutcome:
    """把整份卷子的文字拆成题目。

    只认 `[单选题]` 这类行首标记；两个标记之间的所有内容算一道题。
    没标 `[答案]` 或答案对不上选项的题会被**剔掉并报出来**，
    绝不猜一个答案塞进去——猜错的答案在考前一天是会害人的。

    另一面（说清楚代价）：排版一换（没有题型标记、答案单独一个文件、
    扫描件 OCR 出来的乱码）它就一条也拆不出来。这时候 `problems` 会说明情况，
    而不是悄悄给你 0 道题。
    """
    outcome = ParseOutcome(source=source)
    matches = list(MARKER.finditer(text or ""))
    outcome.total_marks = len(matches)

    if not matches:
        outcome.problems.append(
            "这个文件里没有找到 [单选题] / [多选题] / [判断题] / [问答题] 这样的题型标记，"
            "所以一道题都没解析出来。"
        )
        return outcome

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        label = match.group(1)
        qtype = label_to_type(label)
        if qtype is None:
            outcome.problems.append(f"第 {index + 1} 个标记「[{label}]」不认识的题型，跳过。")
            continue
        question, notes = _parse_block(qtype, match.group(2), text[match.end() : end], source)
        outcome.problems.extend(notes)
        if question is not None:
            outcome.questions.append(question)

    return outcome


def _parse_block(
    qtype: str, head: str, body: str, source: str
) -> tuple[TempQuestion | None, list[str]]:
    """拆一道题。返回 (题目, 问题列表)；题目为 None 表示这题不能要。"""
    notes: list[str] = []
    stem_parts: list[str] = [head.strip()] if head.strip() else []
    options: list[str] = []
    answer = ""
    explanation = ""
    knowledge = ""
    mode = "stem"

    for raw_line in body.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        # 知识点可能在答案前面，也可能在最后，所以先单独认掉
        found = KNOWLEDGE.match(line)
        if found:
            knowledge = found.group(1).strip()
            continue

        found = ANSWER.match(line)
        if found:
            answer = found.group(1).strip()
            mode = "answer"
            continue

        found = EXPLAIN.match(line)
        if found:
            explanation = found.group(1).strip()
            mode = "explain"
            continue

        # 选项只可能在"题干之后、答案之前"这段里出现。
        # 字母必须是下一个（A、B、C…）——这样正文里以字母开头的续行不会被误当选项。
        found = OPTION.match(line)
        if found and mode in {"stem", "option"}:
            expected = chr(ord("A") + len(options))
            if found.group(1).upper() == expected:
                options.append(found.group(2).strip())
                mode = "option"
                continue

        if mode == "stem":
            stem_parts.append(line)
        elif mode == "option":
            options[-1] = f"{options[-1]} {line}".strip()   # 选项折行的续行
        elif mode == "answer":
            answer = f"{answer}\n{line}".strip()
        else:
            explanation = f"{explanation}\n{line}".strip()

    stem = "\n".join(part for part in stem_parts if part).strip()
    if not stem:
        notes.append("有一道题只有答案、没有题干，跳过。")
        return None, notes

    question = TempQuestion(
        qtype=qtype,
        stem=stem,
        options=options,
        answer=answer,
        explanation=explanation,
        knowledge=knowledge,
        source=source,
    )

    if not answer:
        notes.append(f"「{_short(stem)}」没写答案，已跳过（不猜）。")
        return None, notes

    if qtype in {"single", "multi"}:
        letters = normalize_letters(answer)
        if not letters:
            notes.append(f"「{_short(stem)}」的答案「{answer}」不是选项字母，已跳过。")
            return None, notes
        if len(options) < 2:
            notes.append(
                f"「{_short(stem)}」的答案写着 {letters}，但没解析出选项，已跳过。"
            )
            return None, notes
        limit = chr(ord("A") + len(options) - 1)
        bad = [char for char in letters if char > limit]
        if bad:
            notes.append(
                f"「{_short(stem)}」的答案 {letters} 里有超出现有选项（A~{limit}）的"
                f"{''.join(bad)}，已跳过。"
            )
            return None, notes
        if qtype == "single" and len(letters) > 1:
            # 卷子上标成单选、答案却是多个字母——照答案走，按多选处理，
            # 免得判分把对的判成错的。但要说出来，用户自己核对。
            question.qtype = "multi"
            notes.append(f"「{_short(stem)}」标的是单选题，答案却是 {letters}，已按多选题处理。")
        question.answer = letters
    elif qtype == "judge":
        if normalize_judge(answer) is None:
            notes.append(f"「{_short(stem)}」的答案「{answer}」不是「正确/错误」，已跳过。")
            return None, notes
        question.answer = "正确" if normalize_judge(answer) else "错误"
    else:  # short
        question.answer = answer

    # 判断题常见的一种脏数据：解析被写在了「答案」上面那一行
    # （例：「…无需重复进行。」下一行是「需持续测试」）。题干本来就该在
    # 答案之前结束，多出来的这一行基本就是解析走错了地方，且这时通常没有
    # 「解析：」行。只在判断题上做这个纠正——选择题有多行选项，分不清。
    if qtype == "judge" and not explanation and "\n" in stem:
        first, _, extra = stem.partition("\n")
        if extra.strip():
            question.stem = first.strip()
            question.explanation = extra.strip()
            notes.append(
                f"「{_short(question.stem)}」答案上面多了一行文字（{extra.strip()[:20]}…），"
                "已当成解析放进去，请扫一眼对不对。"
            )

    return question, notes


def _short(text: str, limit: int = 24) -> str:
    one_line = text.replace("\n", " ")
    return one_line if len(one_line) <= limit else one_line[:limit] + "…"


# ---------- 导入一批文件 ----------


@dataclass
class ImportBundle:
    """一次导入的结果：题 + 每个文件的解析情况 + 打不开的文件。"""

    questions: list[TempQuestion] = field(default_factory=list)
    outcomes: list[ParseOutcome] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        if not self.outcomes:
            return "没有可导入的文件"
        return "；".join(f"{item.source} → {item.summary_line()}" for item in self.outcomes)


def collect_files(paths: list[Path], on_progress=None) -> ImportBundle:
    """把用户选的文件/文件夹读成题目。**只解析、不写库**——先让用户看一眼再决定入库。

    每个文件单独 try：一个读不了不该让整批白干。
    `on_progress` 是给后台线程回传进度用的（`TaskWorker` 会传进来）。
    """
    bundle = ImportBundle()
    files: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            files.extend(
                item for item in sorted(path.rglob("*")) if item.suffix.lower() in SUPPORTED
            )
        elif path.is_file():
            files.append(path)
        else:
            bundle.problems.append(f"找不到：{path}")

    if not files:
        bundle.problems.append("这些位置里没有能读的文件（支持 PDF / Word / txt）。")
        return bundle

    # .doc 要先用 LibreOffice 转一手，得有个临时工作目录。
    # 用系统临时目录，不往用户的题库文件夹里写东西。
    workdir = Path(tempfile.mkdtemp(prefix="cstudy-temp-bank-"))
    try:
        for index, path in enumerate(files, start=1):
            if on_progress is not None:
                on_progress(f"正在读第 {index}/{len(files)} 个文件：{path.name}")
            try:
                text = extract(path, workdir)
            except Exception as exc:  # 一个文件读不了，接着读下一个
                log.exception("读文件失败：%s", path)
                bundle.problems.append(f"{path.name} 读不了：{exc}")
                continue
            outcome = parse_bank_text(text, path.name)
            bundle.outcomes.append(outcome)
            bundle.problems.extend(f"{path.name}：{note}" for note in outcome.problems)
            bundle.questions.extend(outcome.questions)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if on_progress is not None:
        on_progress(f"读完 {len(files)} 个文件，解析出 {len(bundle.questions)} 道题。")
    return bundle


# ---------- 独立的库 ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS temp_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    qtype       TEXT    NOT NULL,
    stem        TEXT    NOT NULL,
    stem_key    TEXT    NOT NULL,
    options     TEXT    NOT NULL DEFAULT '[]',
    answer      TEXT    NOT NULL DEFAULT '',
    explanation TEXT    NOT NULL DEFAULT '',
    knowledge   TEXT    NOT NULL DEFAULT '',
    source      TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_temp_questions_stem ON temp_questions(stem_key);

CREATE TABLE IF NOT EXISTS temp_answers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id INTEGER NOT NULL,
    user_answer TEXT    NOT NULL DEFAULT '',
    is_correct  INTEGER NOT NULL,
    answered_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS temp_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_INSERT = """
INSERT OR IGNORE INTO temp_questions
    (qtype, stem, stem_key, options, answer, explanation, knowledge, source, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_COLUMNS = "id, qtype, stem, options, answer, explanation, knowledge, source"


def _filter_sql(
    qtypes: list[str] | None, after_id: int | None = None, upto_id: int | None = None
) -> tuple[str, list[object]]:
    """拼 WHERE 子句。题型先过一遍白名单，别把界面传进来的怪值塞进 SQL。"""
    wheres: list[str] = []
    params: list[object] = []
    wanted = [key for key in (qtypes or []) if key in TYPE_LABELS]
    if wanted:
        wheres.append(f"qtype IN ({','.join('?' * len(wanted))})")
        params.extend(wanted)
    if after_id is not None:
        wheres.append("id > ?")
        params.append(int(after_id))
    if upto_id is not None:
        wheres.append("id <= ?")
        params.append(int(upto_id))
    return (" WHERE " + " AND ".join(wheres)) if wheres else "", params


class TempBank:
    """临时题库的读写。

    **每次操作现开一个连接**：导入是在后台线程里跑的，而 SQLite 的连接
    默认不能跨线程用。开连接的代价在这个数据量（几百道题）下可以忽略，
    换来的是"哪个线程调都不会出错"。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---------- 建库 ----------

    def init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------- 写入 ----------

    def add(self, questions: list[TempQuestion]) -> tuple[int, int]:
        """入库。题干完全一样的直接跳过（两个文件里有大量轮换重复的题）。

        返回 (入库数, 重复跳过数)。
        """
        if not questions:
            return 0, 0
        self.init_schema()
        now = datetime.now().isoformat(timespec="seconds")
        rows = [
            (
                question.qtype,
                question.stem,
                question.stem_key,
                json.dumps(question.options, ensure_ascii=False),
                question.answer,
                question.explanation,
                question.knowledge,
                question.source,
                now,
            )
            for question in questions
        ]
        with closing(self._connect()) as conn:
            before = conn.total_changes
            conn.executemany(_INSERT, rows)
            inserted = conn.total_changes - before
            conn.commit()
        return inserted, len(rows) - inserted

    def clear(self) -> None:
        """清空这一栏（只动临时库这个文件）。"""
        if not self.path.exists():
            return
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM temp_questions")
            conn.execute("DELETE FROM temp_answers")
            # 「上次刷到哪」也一起清掉：题都没了，那个位置就没意义了
            conn.execute("DELETE FROM temp_meta")
            conn.commit()

    def record_answer(self, question_id: int, user_answer: str, is_correct: bool) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO temp_answers (question_id, user_answer, is_correct, answered_at)"
                " VALUES (?, ?, ?, ?)",
                (question_id, user_answer, 1 if is_correct else 0, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()

    # ---------- 读取 ----------

    def questions(
        self,
        qtypes: list[str] | None = None,
        shuffle: bool = False,
        limit: int | None = None,
        after_id: int | None = None,
    ) -> list[TempQuestion]:
        """按入库顺序取题（`ORDER BY id`，id 就是入库的先后）。

        `after_id` 专给「接着上次刷」用：只要 id 比它大的题。
        """
        if not self.path.exists():
            return []
        where, params = _filter_sql(qtypes, after_id=after_id)
        sql = f"SELECT {_COLUMNS} FROM temp_questions{where} ORDER BY id"
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [_row_to_question(row) for row in rows]
        if shuffle:
            random.shuffle(items)
        if limit is not None:
            items = items[:limit]
        return items

    def questions_by_ids(self, ids: list[int]) -> list[TempQuestion]:
        """按给定的 id 取题，顺序照传进来的来。

        「重刷这批」要用同一批、同一个顺序，所以不能又按 id 重排一遍。
        """
        clean = [int(i) for i in ids]
        if not clean or not self.path.exists():
            return []
        marks = ",".join("?" for _ in clean)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM temp_questions WHERE id IN ({marks})", clean
            ).fetchall()
        by_id = {int(row["id"]): _row_to_question(row) for row in rows}
        return [by_id[i] for i in clean if i in by_id]

    def count_questions(
        self, qtypes: list[str] | None = None, upto_id: int | None = None
    ) -> int:
        """数题。`upto_id` 是"id 不超过它的有几道"，用来算"整份第几道"。"""
        if not self.path.exists():
            return 0
        where, params = _filter_sql(qtypes, upto_id=upto_id)
        with closing(self._connect()) as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n FROM temp_questions{where}", params
            ).fetchone()
        return int(row["n"])

    def count_by_type(self) -> dict[str, int]:
        counts = {key: 0 for key in TYPE_ORDER}
        if not self.path.exists():
            return counts
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT qtype, COUNT(*) AS n FROM temp_questions GROUP BY qtype")
            for row in rows:
                counts[row["qtype"]] = int(row["n"])
        return counts

    # ---------- 记「上次刷到哪」 ----------
    #
    # 用户的原话是"一次性刷的题太多了，我记不过来"，所以这一栏按"每轮 N 道"来刷，
    # 位置（最后一道**答过的**题的 id）存在 temp_meta 里，关掉软件再打开也还在。

    def meta_get(self, key: str) -> str | None:
        if not self.path.exists():
            return None
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM temp_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, key: str, value: str) -> None:
        self.init_schema()
        with closing(self._connect()) as conn:
            conn.execute(
                "INSERT INTO temp_meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            conn.commit()

    def meta_delete(self, key: str) -> None:
        if not self.path.exists():
            return
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM temp_meta WHERE key = ?", (key,))
            conn.commit()

    def answer_stats(self) -> tuple[int, int]:
        """(做过几道, 对了几道)。同一道题做过多次按每次都算。"""
        if not self.path.exists():
            return 0, 0
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(is_correct), 0) AS right FROM temp_answers"
            ).fetchone()
        return int(row["n"]), int(row["right"])

    # ---------- 错题集 ----------
    #
    # "多次做错的题"不另存一张表：`temp_answers` 里每一次作答都在，
    # 错了几次、现在连着对了几次，用 SQL 现算就行。多存一份状态迟早跟作答记录对不上
    # （主题库的错题本也是这么算的，见 `wrong_book.py` 顶上那段）。

    def _wrong_rows(self) -> list[sqlite3.Row]:
        """做错过的题：错了几次、最近一次什么时候错的。"""
        if not self.path.exists():
            return []
        with closing(self._connect()) as conn:
            return conn.execute(
                "SELECT q.id, q.qtype, q.stem, q.options, q.answer, q.explanation,"
                "       q.knowledge, q.source,"
                "       COUNT(a.id) AS wrong_count, MAX(a.answered_at) AS last_wrong_at"
                "  FROM temp_answers a JOIN temp_questions q ON q.id = a.question_id"
                " WHERE a.is_correct = 0"
                " GROUP BY q.id"
            ).fetchall()

    def _consecutive_correct(self) -> dict[int, int]:
        """每道题**最近连着**答对了几次：按题倒着数，碰到第一个答错的就停。"""
        if not self.path.exists():
            return {}
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT question_id, is_correct FROM temp_answers ORDER BY question_id, id DESC"
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

    def wrong_items(
        self,
        min_wrong: int = WRONG_MIN_TIMES,
        master_after: int = WRONG_MASTER_AFTER,
        qtypes: list[str] | None = None,
    ) -> list[WrongItem]:
        """错题集：做错 >= `min_wrong` 次、且还没"连着答对 `master_after` 次"的题。

        排序是**错得多的在前**——考前最该先看的正是那些反复错的；
        错得一样多的，最近错的在前。
        """
        wanted = [key for key in (qtypes or []) if key in TYPE_LABELS]
        streaks = self._consecutive_correct()
        items: list[WrongItem] = []
        for row in self._wrong_rows():
            wrong_count = int(row["wrong_count"] or 1)
            if wrong_count < max(1, int(min_wrong)):
                continue
            question = _row_to_question(row)
            if wanted and question.qtype not in wanted:
                continue
            streak = streaks.get(int(row["id"]), 0)
            if streak >= max(1, int(master_after)):
                continue          # 连着答对够了 = 掌握了，不再占着错题集
            items.append(
                WrongItem(
                    question=question,
                    wrong_count=wrong_count,
                    consecutive_correct=streak,
                    last_wrong_at=row["last_wrong_at"],
                )
            )
        items.sort(key=lambda item: item.last_wrong_at or "", reverse=True)   # 先按时间新→旧
        items.sort(key=lambda item: item.wrong_count, reverse=True)          # 再按错得多的排前面
        return items

    def wrong_count(
        self,
        min_wrong: int = WRONG_MIN_TIMES,
        master_after: int = WRONG_MASTER_AFTER,
        qtypes: list[str] | None = None,
    ) -> int:
        """错题集里有几道（按钮上显示的数字）。"""
        return len(self.wrong_items(min_wrong, master_after, qtypes))


def _row_to_question(row: sqlite3.Row) -> TempQuestion:
    try:
        options = json.loads(row["options"] or "[]")
    except (TypeError, ValueError):
        options = []
    if not isinstance(options, list):
        options = []
    return TempQuestion(
        id=int(row["id"]),
        qtype=row["qtype"],
        stem=row["stem"],
        options=[str(item) for item in options],
        answer=row["answer"],
        explanation=row["explanation"],
        knowledge=row["knowledge"],
        source=row["source"],
    )
