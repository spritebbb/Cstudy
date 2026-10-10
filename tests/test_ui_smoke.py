"""界面冒烟测试：窗口和对话框**建得出来、不报错**。

项目里没有 pytest-qt，所以这里**只验证构造**，不模拟点击——真实交互得靠人点。
但"构造顺序错了"这类问题是一建就炸的，这个测试正好挡得住
（真踩过：`_refresh_count` 跑在 `source_combo` 建出来之前）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 没有显示器也要能跑，所以强制用离屏后端。必须在 import PyQt6 之前设。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.models import Question  # noqa: E402
from cstudy.ui.filter_dialog import FilterDialog  # noqa: E402
from cstudy.ui.quiz_panel import QuizPanel  # noqa: E402
from cstudy.ui.rendering import material_html  # noqa: E402
from cstudy.ui.syllabus_panel import SyllabusPanel  # noqa: E402
from cstudy.ui.wrong_book_panel import WrongBookPanel  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    # 整个测试会话只建一个 QApplication，多建会崩
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


@pytest.fixture
def stocked(db: Database) -> Database:
    db.upsert_syllabus_point("指针", "第8章", 1)
    db.upsert_syllabus_point("预处理命令", "第1章", 2)
    db.save_questions(
        [Question(type="fill", question=f"填空{i}", answer="1") for i in range(5)],
        source="卷A",
    )
    db.save_questions(
        [Question(type="coding", question=f"编程{i}", answer="1") for i in range(3)],
        source="卷B",
    )
    return db


class TestFilterDialog:
    def test_constructs_with_a_stocked_bank(self, qapp, stocked: Database):
        dialog = FilterDialog(stocked)

        assert dialog.count_label.text()
        assert set(dialog._type_boxes) == {"fill", "coding"}

    def test_constructs_on_an_empty_bank(self, qapp, db: Database):
        """空题库也要能建出来——不该因为没题就崩。"""
        dialog = FilterDialog(db)

        assert "0 道" in dialog.count_label.text()

    def test_default_criteria_means_everything(self, qapp, stocked: Database):
        criteria = FilterDialog(stocked).criteria()

        assert criteria["exam_types"] == ["fill", "coding"]
        assert criteria["point_ids"] is None
        assert criteria["sources"] is None
        assert criteria["limit"] is None

    def test_criteria_can_be_fed_straight_to_the_query(self, qapp, stocked: Database):
        """criteria() 的返回值必须能直接喂给 query_questions——两边对不上就白筛。"""
        dialog = FilterDialog(stocked)

        assert len(stocked.query_questions(**dialog.criteria())) == 8

    def test_unchecking_a_type_narrows_the_count(self, qapp, stocked: Database):
        dialog = FilterDialog(stocked)

        dialog._type_boxes["coding"].setChecked(False)

        assert len(stocked.query_questions(**dialog.criteria())) == 5

    def test_choosing_a_chapter_fills_the_point_combo(self, qapp, stocked: Database):
        dialog = FilterDialog(stocked)

        dialog.chapter_combo.setCurrentIndex(1)

        assert dialog.point_combo.count() == 2   # "全部知识点" + 该章的一个知识点

    def test_choosing_a_source_narrows_the_count(self, qapp, stocked: Database):
        dialog = FilterDialog(stocked)
        index = dialog.source_combo.findData("卷B")

        dialog.source_combo.setCurrentIndex(index)

        assert len(stocked.query_questions(**dialog.criteria())) == 3

    def test_picking_a_count_turns_on_shuffle(self, qapp, stocked: Database):
        dialog = FilterDialog(stocked)
        index = dialog.count_combo.findData(10)

        dialog.count_combo.setCurrentIndex(index)

        assert dialog.criteria()["shuffle"] is True
        assert dialog.criteria()["limit"] == 10


class TestSyllabusPanel:
    def test_lists_every_point_including_empty_ones(self, qapp, stocked: Database):
        panel = SyllabusPanel(stocked)

        html = panel.browser.toHtml()
        assert "指针" in html
        assert "预处理命令" in html
        # 两个知识点都还没有题，都要显示出来
        assert "2 个还没有题" in panel.summary_label.text()

    def test_reports_when_there_is_no_syllabus(self, qapp, db: Database):
        panel = SyllabusPanel(db)

        assert "还没有考纲数据" in panel.summary_label.text()

    def test_refresh_picks_up_newly_linked_questions(self, qapp, stocked: Database):
        panel = SyllabusPanel(stocked)
        point_id = stocked.upsert_syllabus_point("指针", "第8章", 1)
        stocked.assign_points({"": point_id})

        panel.refresh()

        assert "1 个还没有题" in panel.summary_label.text()


class TestWrongBookPanel:
    def test_constructs_with_an_empty_book(self, qapp, db: Database):
        panel = WrongBookPanel(db, Config({"wrong_book": {}, "paths": {}}, Path(".")))

        assert "空的" in panel.summary_label.text()
        assert not panel.review_button.isEnabled()

    def test_lists_a_question_that_was_answered_wrong(self, qapp, db: Database):
        question_id = db.save_questions(
            [Question(type="fill", question="错过的题", answer="1")]
        )[0]
        db.record_answer(question_id, "0", is_correct=False)

        panel = WrongBookPanel(db, Config({"wrong_book": {}, "paths": {}}, Path(".")))

        assert "1 道" in panel.summary_label.text()
        assert "错过的题" in panel.browser.toPlainText()

    def test_review_button_only_enables_when_something_is_due(self, qapp, db: Database):
        """刚错完不该能点"复习"——今天还没到复习时间。"""
        question_id = db.save_questions([Question(type="fill", question="甲", answer="1")])[0]
        db.record_answer(question_id, "0", is_correct=False)

        panel = WrongBookPanel(db, Config({"wrong_book": {}, "paths": {}}, Path(".")))

        assert not panel.review_button.isEnabled()

    def test_question_text_with_html_is_escaped(self, qapp, db: Database):
        """题干里有 `#include <stdio.h>`，不转义会被当成 HTML 标签吃掉。"""
        question_id = db.save_questions(
            [Question(type="fill", question="#include <stdio.h> 输出什么", answer="1")]
        )[0]
        db.record_answer(question_id, "0", is_correct=False)

        panel = WrongBookPanel(db, Config({"wrong_book": {}, "paths": {}}, Path(".")))

        assert "<stdio.h>" in panel.browser.toPlainText()

    def test_removing_a_question_takes_it_out_of_the_list(self, qapp, db: Database):
        question_id = db.save_questions([Question(type="fill", question="甲", answer="1")])[0]
        db.record_answer(question_id, "0", is_correct=False)
        panel = WrongBookPanel(db, Config({"wrong_book": {}, "paths": {}}, Path(".")))

        panel._on_anchor(QUrl("remove:{}".format(question_id)))

        # "还欠着"没了，说明不再当欠账算——但题目还留在"已掌握"里归档
        assert "还欠着" not in panel.summary_label.text()
        assert "清空了" in panel.summary_label.text()
        assert "甲" in panel.browser.toPlainText()


class TestMaterialHtml:
    """题干/答案/解析不能当 Markdown 渲染——它们里面是代码。

    真踩过：`#include` 被当成 Markdown 标题渲染成巨大的 "include"，
    而且 Markdown 里单个换行等于空格，代码的每一行被拼成一长条。
    """

    def test_hash_include_is_not_a_heading(self):
        html = material_html("#include <stdio.h>")

        assert "<h1" not in html and "<h2" not in html
        assert "#include" in html          # 原文一个字没少

    def test_newlines_survive(self):
        html = material_html("int main()\n{\n    return 0;\n}")

        assert html.count("\n") == 3

    def test_angle_brackets_are_escaped(self):
        """不转义的话 `<stdio.h>` 会被 HTML 当标签吃掉。"""
        html = material_html("#include <stdio.h>")

        assert "&lt;stdio.h&gt;" in html
        assert "<stdio.h>" not in html

    def test_ampersand_is_escaped(self):
        assert "&amp;&amp;" in material_html("if (a && b)")

    def test_empty_text_gives_nothing(self):
        assert material_html("") == ""
        assert material_html("   \n  ") == ""


class TestQuizPanelRendering:
    def test_code_in_the_stem_keeps_its_line_breaks(self, qapp, db: Database):
        """这是那两个排版 bug 的回归测试。"""
        stem = "#include <stdio.h>\nint main()\n{\n    return 0;\n}"
        question_id = db.save_questions([Question(type="fill", question=stem, answer="1")])[0]
        panel = QuizPanel(db)

        panel.load([Question.from_row(db.query_questions()[0])], [question_id])

        shown = panel.question_view.toPlainText()
        assert "#include <stdio.h>" in shown, "`#` 被 Markdown 吃掉了"
        assert "\n" in shown, "换行被 Markdown 吞了，代码会挤成一长条"
        assert shown.count("\n") >= 4

    def test_multiline_answer_keeps_its_line_breaks(self, qapp, db: Database):
        """参考答案也可能是多行的（程序运行结果就分好几行）。"""
        answer = "12345\n234\n3"
        db.save_questions(
            [
                Question(
                    type="reading",
                    question="阅读程序，写出输出。\nint main() { return 0; }",
                    answer=answer,
                )
            ]
        )
        panel = QuizPanel(db)
        panel.load([Question.from_row(db.query_questions()[0])], [1])

        panel.answer_edit.setPlainText("随便写点")
        panel._submit()

        shown = panel.feedback_view.toPlainText()
        assert "12345" in shown and "234" in shown
        assert shown.count("\n") >= 2
