"""「考前突击」那一栏：解析、隔离、判分的测试。

这一栏最要紧的只有一件事：**不能碰主题库**。所以除了常规的解析测试，
`TestIsolation` 会直接去看两个库文件里各有什么表——结构上的隔离，
比"某条 SQL 写对了"可靠。
"""

from __future__ import annotations

import random
import sqlite3
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from cstudy.db import Database  # noqa: E402
from cstudy.temp_bank import (  # noqa: E402
    TempBank,
    TempQuestion,
    collect_files,
    label_to_type,
    normalize_letters,
    normalize_judge,
    parse_bank_text,
    shuffle_options,
    uses_option_letters,
)


def make_docx(path: Path, paragraphs: list[str]) -> None:
    """造一个最小可用的 .docx——我们只读 word/document.xml。"""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def bank_text(*lines: str) -> str:
    return "\n".join(lines)


def one(text: str):
    outcome = parse_bank_text(text, "测试.docx")
    assert len(outcome.questions) == 1, outcome.problems
    return outcome.questions[0]


# ---------- 解析 ----------


class TestParse:
    def test_four_types_split_by_marker(self):
        """按 `[题型]` 切题，题型各归各位。"""
        outcome = parse_bank_text(
            bank_text(
                "[单选题]端口安全属于哪一层？",
                "A.物理层",
                "B.数据链路层",
                "答案：B",
                "[多选题]WAF 基础防护策略包含（    ）。",
                "A.IP 黑白名单",
                "B.URL 访问控制",
                "C.端口扫描",
                "答案：AB",
                "[判断题]双绞线中的绞合可以减少串扰。",
                "答案：正确",
                "[问答题]请简述完全备份与增量备份的区别。",
                "答案：完全备份每次都全量；增量备份只备份变化的部分。",
            )
        )
        assert outcome.total_marks == 4
        assert len(outcome.questions) == 4
        assert [q.qtype for q in outcome.questions] == ["single", "multi", "judge", "short"]
        assert outcome.by_type == {"single": 1, "multi": 1, "judge": 1, "short": 1}

    def test_options_answer_and_explanation(self):
        question = one(
            bank_text(
                "[单选题]交换机安全配置中，端口隔离的作用是（    ）。",
                "A.限制端口之间的二层互通",
                "B.提高端口带宽",
                "C.加密端口数据",
                "知识点：交换机安全配置",
                "答案：A",
                "解析：端口隔离把同一 VLAN 内的端口互相隔开。",
            )
        )
        assert question.options == [
            "限制端口之间的二层互通",
            "提高端口带宽",
            "加密端口数据",
        ]
        assert question.answer == "A"
        assert question.knowledge == "交换机安全配置"
        assert question.explanation.startswith("端口隔离把同一 VLAN")
        assert question.answer_display().startswith("A. 限制端口")

    def test_multiline_option_continuation(self):
        """选项折行要接回上一项，不能变成新的题干。"""
        question = one(
            bank_text(
                "[单选题]下面哪个说法对？",
                "A.第一个说法",
                "接着第一项的后半句",
                "B.第二个说法",
                "答案：B",
            )
        )
        assert question.options == ["第一个说法 接着第一项的后半句", "第二个说法"]

    def test_explanation_may_be_empty(self):
        question = one(
            bank_text(
                "[单选题]题干。",
                "A.甲",
                "B.乙",
                "答案：B",
                "解析：",
            )
        )
        assert question.answer == "B"
        assert question.explanation == ""

    def test_multi_letters_up_to_e(self):
        question = one(
            bank_text(
                "[多选题]全都选。",
                "A.一",
                "B.二",
                "C.三",
                "D.四",
                "E.五",
                "答案：ABCDE",
            )
        )
        assert question.answer == "ABCDE"
        assert question.check("edcba") is True

    def test_missing_answer_is_skipped_not_guessed(self):
        """没写答案就跳过并报出来——猜一个答案塞进去是会害人的。"""
        outcome = parse_bank_text(
            bank_text("[单选题]这题没给答案。", "A.甲", "B.乙", "C.丙")
        )
        assert outcome.questions == []
        assert outcome.total_marks == 1
        assert any("没写答案" in note for note in outcome.problems)

    def test_answer_letter_out_of_range_is_skipped(self):
        outcome = parse_bank_text(
            bank_text("[单选题]答案对不上选项。", "A.甲", "B.乙", "答案：D")
        )
        assert outcome.questions == []
        assert any("超出现有选项" in note for note in outcome.problems)

    def test_no_options_at_all_is_skipped(self):
        outcome = parse_bank_text(bank_text("[单选题]只有题干和答案。", "答案：A"))
        assert outcome.questions == []
        assert any("没解析出选项" in note for note in outcome.problems)

    def test_single_marked_but_multi_letters_becomes_multi(self):
        """卷面标错题型时按答案走，免得把对的判成错的，但要说出来。"""
        outcome = parse_bank_text(
            bank_text("[单选题]标错了。", "A.甲", "B.乙", "C.丙", "答案：AB")
        )
        assert len(outcome.questions) == 1
        assert outcome.questions[0].qtype == "multi"
        assert any("按多选题处理" in note for note in outcome.problems)

    def test_judge_answer_must_be_true_or_false(self):
        outcome = parse_bank_text(
            bank_text("[判断题]说不清的答案。", "答案：不清楚")
        )
        assert outcome.questions == []
        assert any("不是「正确/错误」" in note for note in outcome.problems)

    def test_judge_stray_line_above_answer_moves_to_explanation(self):
        """真实脏数据：解析被写在「答案」上面那一行。"""
        outcome = parse_bank_text(
            bank_text(
                "[判断题]应用安全测试仅需在开发完成后执行一次，无需重复进行。",
                "需持续测试",
                "答案：错误",
            )
        )
        assert len(outcome.questions) == 1
        question = outcome.questions[0]
        assert question.stem == "应用安全测试仅需在开发完成后执行一次，无需重复进行。"
        assert question.explanation == "需持续测试"
        assert any("多了一行文字" in note for note in outcome.problems)

    def test_unknown_label_is_reported(self):
        outcome = parse_bank_text(bank_text("[填空题]这个题型这栏不认。", "答案：甲"))
        assert outcome.questions == []
        assert any("不认识的题型" in note for note in outcome.problems)

    def test_square_brackets_in_body_are_not_question_starts(self):
        """只有"方括号里以题结尾"的才算标记，正文里的 `[1]` 不算。"""
        question = one(
            bank_text(
                "[单选题]参考[1]里的说法，哪个对？",
                "A.甲",
                "B.乙",
                "答案：A",
                "解析：见[1]。",
            )
        )
        assert question.stem.endswith("哪个对？")
        assert question.explanation == "见[1]。"

    def test_no_marker_says_so_instead_of_returning_zero_quietly(self):
        outcome = parse_bank_text("1. 第一题\nA.甲\nB.乙\n答案：A")
        assert outcome.questions == []
        assert outcome.total_marks == 0
        assert any("没有找到" in note for note in outcome.problems)

    def test_full_width_brackets_and_spaceless_answer(self):
        """`【多选题】`、`答案:B` 这种写法也得认。"""
        question = one(bank_text("【多选题】全角标记。", "A.甲", "B.乙", "答案:AB"))
        assert question.qtype == "multi"
        assert question.answer == "AB"


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [("b", "B"), ("a、c", "AC"), ("c b a", "ABC"), ("B,D,A", "ABD"), ("", ""), ("甲乙", "")],
    )
    def test_normalize_letters(self, raw, want):
        assert normalize_letters(raw) == want

    @pytest.mark.parametrize(
        ("raw", "want"),
        [("正确", True), ("对", True), ("√", True), ("错误", False), ("×", False), ("说不清", None)],
    )
    def test_normalize_judge(self, raw, want):
        assert normalize_judge(raw) is want

    @pytest.mark.parametrize(
        ("label", "want"),
        [
            ("单选题", "single"),
            ("单项选择题", "single"),
            ("选择题", "single"),
            ("多项选择题", "multi"),
            ("不定项选择题", "multi"),
            ("判断题", "judge"),
            ("简答题", "short"),
            ("名词解释题", "short"),
            ("填空题", None),
        ],
    )
    def test_label_to_type(self, label, want):
        assert label_to_type(label) == want


# ---------- 判分 ----------


class TestCheck:
    def test_single_and_multi(self):
        assert TempQuestion("single", "题干", ["甲", "乙"], "B").check("b") is True
        assert TempQuestion("single", "题干", ["甲", "乙"], "B").check("A") is False
        assert TempQuestion("multi", "题干", ["甲", "乙", "丙"], "ABC").check("cba") is True
        assert TempQuestion("multi", "题干", ["甲", "乙", "丙"], "ABC").check("AB") is False

    def test_judge(self):
        assert TempQuestion("judge", "题干", [], "正确").check("对") is True
        assert TempQuestion("judge", "题干", [], "正确").check("错误") is False

    def test_short_is_never_auto_graded(self):
        assert TempQuestion("short", "题干", [], "参考答案").check("随便写") is None

    def test_unparsable_user_answer_gives_none(self):
        assert TempQuestion("single", "题干", ["甲", "乙"], "B").check("") is None


# ---------- 临时库 ----------


def make_question(stem: str, qtype: str = "single", answer: str = "A") -> TempQuestion:
    options = ["甲", "乙"] if qtype in {"single", "multi"} else []
    return TempQuestion(qtype=qtype, stem=stem, options=options, answer=answer)


class TestTempBank:
    def test_add_then_count(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        assert bank.add([make_question("第一题"), make_question("第二题")]) == (2, 0)
        assert bank.count_by_type() == {"single": 2, "multi": 0, "judge": 0, "short": 0}

    def test_duplicate_stem_is_skipped_ignoring_whitespace(self, tmp_path):
        """两个文件是同一批题的轮换版，题干（抹掉空白后）一样就算重复。"""
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("交换机端口隔离的作用是（    ）。")])
        inserted, skipped = bank.add([make_question("交换机端口隔离的作用是（ ）。")])
        assert (inserted, skipped) == (0, 1)
        assert bank.count_by_type()["single"] == 1

    def test_duplicate_within_one_batch(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        assert bank.add([make_question("同一题"), make_question("同一题")]) == (1, 1)

    def test_filter_shuffle_limit(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add(
            [
                make_question("单选一"),
                make_question("多选一", "multi", "AB"),
                make_question("判断一", "judge", "正确"),
            ]
        )
        assert [q.stem for q in bank.questions(["multi"])] == ["多选一"]
        assert len(bank.questions()) == 3
        assert len(bank.questions(limit=2)) == 2
        assert len(bank.questions(shuffle=True)) == 3
        assert bank.questions(["不存在的题型"]) and True  # 未知题型不过滤掉全部

    def test_record_answer_and_stats(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("第一题"), make_question("第二题")])
        items = bank.questions()
        bank.record_answer(items[0].id, "A", True)
        bank.record_answer(items[1].id, "B", False)
        assert bank.answer_stats() == (2, 1)

    def test_clear_only_empties_this_bank(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("第一题")])
        bank.clear()
        assert bank.count_by_type() == {"single": 0, "multi": 0, "judge": 0, "short": 0}
        assert bank.answer_stats() == (0, 0)

    def test_empty_bank_is_not_an_error(self, tmp_path):
        bank = TempBank(tmp_path / "还没建库" / "bank.db")
        assert bank.questions() == []
        assert bank.answer_stats() == (0, 0)
        assert bank.add([]) == (0, 0)

    def test_options_survive_a_round_trip(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add(
            [
                TempQuestion(
                    qtype="multi",
                    stem="题干（    ）。",
                    options=["甲", "乙", "丙"],
                    answer="AC",
                    explanation="解析文字",
                    knowledge="知识点",
                    source="某文件.docx",
                )
            ]
        )
        question = bank.questions()[0]
        assert question.options == ["甲", "乙", "丙"]
        assert question.answer == "AC"
        assert question.explanation == "解析文字"
        assert question.knowledge == "知识点"
        assert question.source == "某文件.docx"
        assert question.type_label == "多选题"


class TestRoundPosition:
    """「每轮 N 道 + 接着上次刷」的底子：位置存在 temp_meta 里。

    用户的原话是"一次性刷的题太多了，我记不过来"，所以一轮只给他几十道，
    位置（最后一道**答过的**题的 id）得能跨次、跨重启留着。
    """

    def test_meta_round_trip_and_overwrite(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        assert bank.meta_get("pos:all") is None
        bank.meta_set("pos:all", "42")
        assert bank.meta_get("pos:all") == "42"
        bank.meta_set("pos:all", "43")      # 同一个键只留最新的一条
        assert bank.meta_get("pos:all") == "43"
        bank.meta_delete("pos:all")
        assert bank.meta_get("pos:all") is None

    def test_meta_on_missing_file_is_none(self, tmp_path):
        """库还没建出来的时候问一句，不能炸。"""
        bank = TempBank(tmp_path / "还没建库" / "bank.db")
        assert bank.meta_get("pos:all") is None

    def test_after_id_continues_where_left_off(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question(f"第{i}题") for i in range(1, 8)])
        first = bank.questions(limit=3)
        assert [q.stem for q in first] == ["第1题", "第2题", "第3题"]
        second = bank.questions(limit=3, after_id=first[-1].id)
        assert [q.stem for q in second] == ["第4题", "第5题", "第6题"]
        assert [q.stem for q in bank.questions(limit=3, after_id=second[-1].id)] == ["第7题"]
        # 刷到头了就是空，不能绕回第一题
        assert bank.questions(after_id=bank.questions()[-1].id) == []

    def test_after_id_works_with_type_filter(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add(
            [
                make_question("单选一"),
                make_question("多选一", "multi", "AB"),
                make_question("单选二"),
                make_question("多选二", "multi", "AB"),
            ]
        )
        pick = bank.questions(["multi"], limit=1)
        assert [q.stem for q in pick] == ["多选一"]
        rest = bank.questions(["multi"], after_id=pick[0].id)
        assert [q.stem for q in rest] == ["多选二"]

    def test_count_questions_with_upto_id(self, tmp_path):
        """「整份第几道」是这么算出来的。"""
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question(f"第{i}题") for i in range(1, 6)])
        items = bank.questions()
        assert bank.count_questions() == 5
        assert bank.count_questions(upto_id=items[1].id) == 2
        assert bank.count_questions(["multi"], upto_id=items[1].id) == 0

    def test_count_questions_on_missing_file(self, tmp_path):
        bank = TempBank(tmp_path / "还没建库" / "bank.db")
        assert bank.count_questions() == 0

    def test_clear_also_forgets_position(self, tmp_path):
        """清空这一栏之后，"上次刷到哪"就没意义了，得一起清掉。"""
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("第一题")])
        bank.record_answer(bank.questions()[0].id, "A", True)
        bank.meta_set("pos:all", "1")
        bank.clear()
        assert bank.meta_get("pos:all") is None


class TestQuestionsByIds:
    """「重刷这批」按 id 取题：顺序要照传进来的来，不能又按 id 重排。

    用户原话："我要重头再刷的意思是我要重新刷我刚刚刷完的二十道题"——
    那一批可能是随机抽的，顺序也在"这批"的含义里。
    """

    def test_keeps_the_given_order(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question(f"第 {i} 题") for i in range(1, 4)])
        ids = [q.id for q in bank.questions()]
        got = bank.questions_by_ids([ids[2], ids[0], ids[1]])
        assert [q.stem for q in got] == ["第 3 题", "第 1 题", "第 2 题"]

    def test_missing_ids_are_skipped(self, tmp_path):
        """题被删掉过（比如清空重导）时，剩下的照给，别整批丢掉。"""
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("第一题")])
        ids = [q.id for q in bank.questions()]
        assert [q.stem for q in bank.questions_by_ids([ids[0], 99999])] == ["第一题"]

    def test_empty_list_or_missing_file(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        assert bank.questions_by_ids([]) == []
        assert bank.questions_by_ids([1, 2]) == []      # 库文件都还没建


class TestIsolation:
    """这一栏和主题库必须**结构上**分开，不能只靠查询条件绕。"""

    def test_two_databases_do_not_share_tables(self, tmp_path):
        main = Database(tmp_path / "cstudy.db")
        main.init_schema()
        bank = TempBank(tmp_path / "temp_bank" / "bank.db")
        bank.init_schema()

        def tables(path: Path) -> set[str]:
            with sqlite3.connect(path) as conn:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                return {row[0] for row in rows}

        main_tables = tables(main.path)
        temp_tables = tables(bank.path)
        assert {"questions", "answers", "wrong_questions"} <= main_tables
        assert "temp_questions" not in main_tables
        assert "questions" not in temp_tables
        assert {"temp_questions", "temp_answers"} <= temp_tables

    def test_importing_into_temp_bank_leaves_main_db_untouched(self, tmp_path):
        main = Database(tmp_path / "cstudy.db")
        main.init_schema()
        before = main.count_questions() if hasattr(main, "count_questions") else None

        bank = TempBank(tmp_path / "temp_bank" / "bank.db")
        bank.add([make_question("临时栏的题，不该出现在主题库")])

        with sqlite3.connect(main.path) as conn:
            found = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE question LIKE '%临时栏的题%'"
            ).fetchone()[0]
        assert found == 0
        if before is not None:
            assert main.count_questions() == before


# ---------- 从文件读到入库（整条链路） ----------


class TestCollectFiles:
    def test_docx_round_trip(self, tmp_path):
        path = tmp_path / "模拟题.docx"
        make_docx(
            path,
            [
                "考试名称：某考试模拟题",
                "[单选题]端口隔离的作用是（    ）。",
                "A.隔开端口",
                "B.加密数据",
                "答案：A",
                "解析：端口隔离在二层隔开。",
                "[判断题]双绞线绞合可以减少串扰。",
                "答案：正确",
            ],
        )
        bundle = collect_files([tmp_path])
        assert [q.qtype for q in bundle.questions] == ["single", "judge"]
        assert bundle.questions[0].source == "模拟题.docx"
        assert bundle.problems == []

        bank = TempBank(tmp_path / "bank.db")
        assert bank.add(bundle.questions) == (2, 0)

    def test_broken_file_does_not_kill_the_batch(self, tmp_path):
        """一个文件读不了，别的文件照样导——不能让整批白干。"""
        (tmp_path / "坏文件.docx").write_text("这不是一个真的 docx", encoding="utf-8")
        make_docx(
            tmp_path / "好文件.docx",
            ["[单选题]好文件里的题。", "A.甲", "B.乙", "答案：A"],
        )
        bundle = collect_files([tmp_path])
        assert len(bundle.questions) == 1
        assert any("坏文件.docx" in problem for problem in bundle.problems)

    def test_unsupported_and_missing_paths(self, tmp_path):
        (tmp_path / "图片.png").write_bytes(b"\x89PNG")
        bundle = collect_files([tmp_path])
        assert bundle.questions == []
        assert any("没有能读的文件" in problem for problem in bundle.problems)

        missing = collect_files([tmp_path / "不存在"])
        assert any("找不到" in problem for problem in missing.problems)

    def test_progress_callback_gets_called(self, tmp_path):
        make_docx(tmp_path / "a.docx", ["[单选题]题。", "A.甲", "B.乙", "答案：A"])
        seen: list[str] = []
        collect_files([tmp_path], on_progress=seen.append)
        assert seen and "解析出 1 道题" in seen[-1]


class TestWrongSet:
    """错题集：做错的题收进来（默认错 1 次就收），连着答对 2 次移出去。

    用户最初的原话是"添加错题集功能，多次做错的题进行搜集"（当时门槛定的 2 次），
    2026-10-15 又要求"把错题判断改成1"。判据不另存状态——
    错了几次、连着对了几次，都是从 `temp_answers` 现算的（跟主题库错题本一个道理）。
    """

    def _bank_with_one(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question("第一题")])
        return bank, bank.questions()[0].id

    def test_one_wrong_is_enough_by_default(self, tmp_path):
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        items = bank.wrong_items()
        assert len(items) == 1
        assert items[0].wrong_count == 1
        assert bank.wrong_count() == 1

    def test_two_wrongs_are_collected(self, tmp_path):
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        bank.record_answer(qid, "B", False)
        items = bank.wrong_items()
        assert len(items) == 1
        assert items[0].question.id == qid
        assert items[0].wrong_count == 2
        assert items[0].consecutive_correct == 0
        assert not items[0].mastered

    def test_two_correct_in_a_row_removes_it(self, tmp_path):
        """连着答对 2 次 = 掌握了，不再占着错题集。"""
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        bank.record_answer(qid, "B", False)
        bank.record_answer(qid, "A", True)
        assert bank.wrong_count() == 1          # 只对了一次，还算错题
        bank.record_answer(qid, "A", True)
        assert bank.wrong_count() == 0          # 连对两次，移出

    def test_correct_between_wrongs_resets_the_streak(self, tmp_path):
        """错—对—错：留错题集是肯定的，但连着对的次数必须归零。"""
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        bank.record_answer(qid, "A", True)
        bank.record_answer(qid, "B", False)
        items = bank.wrong_items()
        assert items and items[0].consecutive_correct == 0
        assert items[0].wrong_count == 2

    def test_order_is_most_wrong_first(self, tmp_path):
        """错得多的排前面——考前最该先看的正是反复错的那几道。"""
        bank = TempBank(tmp_path / "bank.db")
        bank.add([make_question(f"第 {i} 题") for i in range(1, 4)])
        ids = [q.id for q in bank.questions()]
        for _ in range(3):
            bank.record_answer(ids[1], "B", False)      # 第二题错 3 次
        for _ in range(2):
            bank.record_answer(ids[2], "B", False)      # 第三题错 2 次
        bank.record_answer(ids[0], "B", False)          # 第一题只错 1 次，门槛 2 时不收
        assert [
            item.question.id for item in bank.wrong_items(min_wrong=2)
        ] == [ids[1], ids[2]]
        # 门槛是 1（现在的默认）时，三道全在，顺序仍是错得多的在前
        assert [item.question.id for item in bank.wrong_items()] == [ids[1], ids[2], ids[0]]

    def test_type_filter(self, tmp_path):
        bank = TempBank(tmp_path / "bank.db")
        bank.add(
            [
                make_question("单选题", "single"),
                make_question("多选题", "multi", "AB"),
            ]
        )
        for question in bank.questions():
            bank.record_answer(question.id, "X", False)
            bank.record_answer(question.id, "X", False)
        assert bank.wrong_count() == 2
        assert [item.question.stem for item in bank.wrong_items(qtypes=["multi"])] == ["多选题"]

    def test_threshold_and_master_after_are_adjustable(self, tmp_path):
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        assert bank.wrong_count() == 1                 # 默认门槛就是 1
        assert bank.wrong_count(min_wrong=2) == 0      # 想回到"错两次才算"也行
        bank.record_answer(qid, "B", False)
        assert bank.wrong_count(min_wrong=2) == 1
        bank.record_answer(qid, "A", True)
        assert bank.wrong_count(min_wrong=1, master_after=1) == 0

    def test_clear_also_empties_the_wrong_set(self, tmp_path):
        bank, qid = self._bank_with_one(tmp_path)
        bank.record_answer(qid, "B", False)
        bank.record_answer(qid, "B", False)
        assert bank.wrong_count() == 1
        bank.clear()
        assert bank.wrong_count() == 0

    def test_empty_bank_has_no_wrong_items(self, tmp_path):
        bank = TempBank(tmp_path / "还没建库" / "bank.db")
        assert bank.wrong_items() == []
        assert bank.wrong_count() == 0


class TestShuffleOptions:
    """打乱选项：用户要的是"每次重新刷题后，把选项打乱"。

    这里最要紧的一条是**答案要跟着走**——答案字母是按位置记的（`answer="C"` 指第 3 个
    选项），打乱以后要是忘了重算，就会把对的判成错的，用户只会觉得"这软件判分坏了"。
    所以除了"顺序真的变了"，还要盯"答案指的还是那个选项"。
    """

    @staticmethod
    def _question(qtype: str = "single", answer: str = "C") -> TempQuestion:
        return TempQuestion(
            qtype=qtype,
            stem="题干",
            options=["甲", "乙", "丙", "丁"],
            answer=answer,
            explanation="没提选项字母",
            id=7,
        )

    def test_answer_still_points_at_the_same_option(self):
        question = self._question()
        for seed in range(40):
            shuffled = shuffle_options(question, random.Random(seed))
            expected = question.answer_display().split(". ", 1)[1]
            assert shuffled.answer_display().split(". ", 1)[1] == expected

    def test_order_actually_changes(self):
        question = self._question()
        for seed in range(40):
            shuffled = shuffle_options(question, random.Random(seed))
            assert shuffled.options != question.options

    def test_options_are_only_reordered(self):
        question = self._question()
        for seed in range(40):
            shuffled = shuffle_options(question, random.Random(seed))
            assert sorted(shuffled.options) == sorted(question.options)

    def test_multi_letters_are_remapped(self):
        question = TempQuestion(
            qtype="multi", stem="多选", options=["甲", "乙", "丙", "丁"], answer="ABD"
        )
        for seed in range(40):
            shuffled = shuffle_options(question, random.Random(seed))
            letters = normalize_letters(shuffled.answer)
            assert len(letters) == 3
            chosen = [shuffled.options[ord(ch) - ord("A")] for ch in letters]
            # 答案 "ABD" 指的是甲、乙、丁三个选项：内容没变，只是位置换了
            assert sorted(chosen) == sorted(["甲", "乙", "丁"])

    def test_grading_agrees_after_shuffle(self):
        question = self._question()          # 正确答案是"丙"
        for seed in range(40):
            shuffled = shuffle_options(question, random.Random(seed))
            right = [ch for ch, text in zip("ABCD", shuffled.options) if text == "丙"][0]
            assert shuffled.check(right) is True
            wrong = [ch for ch, text in zip("ABCD", shuffled.options) if text == "甲"][0]
            assert shuffled.check(wrong) is False

    def test_judge_is_left_alone(self):
        question = TempQuestion(
            qtype="judge", stem="判断题", options=["正确", "错误"], answer="正确"
        )
        assert shuffle_options(question, random.Random(1)) is question

    def test_short_is_left_alone(self):
        question = TempQuestion(qtype="short", stem="简答", answer="参考答案原文")
        assert shuffle_options(question, random.Random(1)) is question

    def test_single_option_is_left_alone(self):
        question = TempQuestion(qtype="single", stem="只有一个选项", options=["甲"], answer="A")
        assert shuffle_options(question, random.Random(1)) is question

    def test_explanation_that_names_letters_is_left_alone(self):
        """解析里写"选项A是中继器功能"的题不能打乱——打乱之后解析就指错了。"""
        question = TempQuestion(
            qtype="single",
            stem="路由器的主要功能",
            options=["甲", "乙", "丙", "丁"],
            answer="C",
            explanation="选项A是中继器功能；选项C是ARP协议功能。",
        )
        assert shuffle_options(question, random.Random(1)) is question

    def test_english_terms_do_not_count_as_option_letters(self):
        """`ARP` / `IP` 这种术语不能被当成"解析提到了选项字母"。"""
        question = TempQuestion(
            qtype="single",
            stem="ARP",
            options=["甲", "乙", "丙", "丁"],
            answer="C",
            explanation="ARP协议根据IP地址查MAC地址，和DNS不一样。",
        )
        assert uses_option_letters(question) is False
        assert shuffle_options(question, random.Random(1)).options != question.options

    def test_letter_reference_patterns(self):
        def check(text: str) -> bool:
            return uses_option_letters(
                TempQuestion(qtype="single", stem="s", options=["甲", "乙"], answer="A", explanation=text)
            )

        assert check("选项A是中继器功能") is True
        assert check("A、B、C正确。") is True
        assert check("A. 甲是对的") is True
        assert check("ARP协议和IP地址") is False
        assert check("HTTP 默认端口是 80") is False
        assert check("") is False

    def test_other_fields_are_kept(self):
        question = self._question()
        shuffled = shuffle_options(question, random.Random(3))
        assert shuffled.id == 7
        assert shuffled.stem == question.stem
        assert shuffled.qtype == question.qtype
        assert shuffled.explanation == question.explanation
