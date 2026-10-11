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
from cstudy.ui import rendering  # noqa: E402
from cstudy.ui.filter_dialog import FilterDialog  # noqa: E402
from cstudy.ui.notes_panel import NotesPanel  # noqa: E402
from cstudy.ui.stats_panel import StatsPanel  # noqa: E402
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


class TestFonts:
    """字体：默认那套 `<pre>` 落到 Courier New，又小又细，看久了伤眼。

    这里验证「写进去的字体 Qt 真的认」，光看 HTML 字符串不算数。
    """

    def _parsed_format(self, html: str):
        from PyQt6.QtGui import QTextDocument

        doc = QTextDocument()
        doc.setHtml(html)
        iterator = doc.begin().begin()
        while not iterator.atEnd():
            fragment = iterator.fragment()
            if fragment.isValid():
                return fragment.charFormat()
            iterator += 1
        raise AssertionError("没解析出任何文本")

    def test_style_attribute_is_not_broken_by_nested_quotes(self):
        """踩过：`style='...'Consolas'...'`——单引号套单引号，属性当场截断，样式全废。"""
        html = material_html("int main()")

        assert html.startswith('<pre style="')
        assert html.count('"') == 2

    def test_qt_actually_applies_the_configured_font(self):
        rendering.configure("Consolas", 13)

        fmt = self._parsed_format(material_html("int main()"))

        families = list(fmt.fontFamilies())
        assert families[0] == "Consolas"
        assert "Microsoft YaHei" in families, "中文没兜底字体，会变方框"
        assert fmt.fontPointSize() == 13.0

    def test_chinese_falls_back_to_a_cjk_font(self):
        """配置里填的通常是纯英文等宽字体，中文得自己补一个，不然是方框。"""
        rendering.configure("Fira Code")

        families = list(self._parsed_format(material_html("指针")).fontFamilies())

        assert families[0] == "Fira Code"
        assert any("YaHei" in name or "Noto" in name for name in families)

    def test_broken_config_does_not_break_the_font(self):
        rendering.configure("", None)          # 配置留空
        rendering.configure("Consolas", "十二")  # 字号写成中文

        fmt = self._parsed_format(material_html("x"))

        assert list(fmt.fontFamilies())[0] == "Consolas"
        assert fmt.fontPointSize() > 0

    def test_default_is_bigger_than_the_bare_pre(self):
        """裸 `<pre>` 的字号是 0（用默认那套小的），我们至少得比它大。"""
        rendering.configure("Consolas", 12)

        assert self._parsed_format(material_html("x")).fontPointSize() >= 12

    def teardown_method(self, _method):
        rendering.configure("Consolas", 12)   # 别把状态漏给别的测试


class TestRunBox:
    """编程题的「跑一下」——只在程序设计题上出现。

    这 31 分是软件原来完全帮不上忙的地方：以前只能"写完对照参考答案"，
    可 C 语言恰恰是看着对、编译就报错的科目。
    """

    def panel_with(self, db: Database, qtype: str) -> QuizPanel:
        """装一道题进去，返回面板。"""
        stem = (
            "编写一个程序，输出 Hello World。"
            if qtype == "coding"
            else "表达式 1+1 的值为 ______。"
        )
        question_id = db.save_questions([Question(type=qtype, question=stem, answer="1")])[0]
        question = Question.from_row(db.query_questions()[0])
        panel = QuizPanel(db)
        panel.load([question], [question_id])
        return panel

    def test_hidden_for_a_plain_fill(self, qapp, db: Database):
        panel = self.panel_with(db, "fill")

        # 用 isHidden 而不是 isVisible：面板本身没 show() 过，
        # isVisible() 会因为"父窗口没显示"而一律返回 False，测不出东西
        assert panel.run_box.isHidden()

    def test_shown_for_a_coding_question(self, qapp, db: Database):
        panel = self.panel_with(db, "coding")

        assert not panel.run_box.isHidden()
        assert panel.run_button.isEnabled()

    def test_clicking_with_empty_code_only_warns(self, qapp, db: Database, monkeypatch):
        """没写代码就点「跑一下」：提醒一句就行，别真起线程去编译空文件。

        注意弹窗必须 mock 掉——离屏环境下 QMessageBox 会一直等用户点击，测试会挂死。
        """
        from cstudy.ui import quiz_panel

        warned: list[str] = []
        monkeypatch.setattr(
            quiz_panel.QMessageBox,
            "information",
            lambda *args, **kwargs: warned.append(str(args[-1])),
        )
        panel = self.panel_with(db, "coding")
        panel.answer_edit.setPlainText("   ")

        panel._run_code()

        assert panel._run_worker is None, "不该起线程"
        assert warned and "代码" in warned[0]


class TestNotesPanel:
    """笔记页：能搜、能翻。

    原来只能显示"刚生成的那一篇"，关掉软件就只能去 notes/ 文件夹翻 .md 了。
    """

    def seed(self, db: Database, title: str, content: str) -> None:
        video_id = db.upsert_video(f"BV{title}", 1, title, "url", 100)
        db.save_note(video_id=video_id, title=title, content=content)

    def test_empty_state_is_friendly(self, qapp, db: Database):
        panel = NotesPanel(db)

        assert "还没有笔记" in panel.count_label.text()

    def test_lists_the_notes_it_finds(self, qapp, db: Database):
        self.seed(db, "第七集", "讲位运算")
        self.seed(db, "第八集", "讲文件")

        panel = NotesPanel(db)

        assert panel.note_list.count() == 2
        assert "找到 2 篇" in panel.count_label.text()

    def test_search_filters_the_list(self, qapp, db: Database):
        self.seed(db, "第七集", "这节课讲位运算")
        self.seed(db, "第八集", "这节课讲文件")

        panel = NotesPanel(db)
        panel.search_edit.setText("位运算")

        assert panel.note_list.count() == 1

    def test_search_with_no_hits_says_so(self, qapp, db: Database):
        self.seed(db, "第七集", "讲位运算")

        panel = NotesPanel(db)
        panel.search_edit.setText("搜不到的词")

        assert "没搜到" in panel.count_label.text()

    def test_picking_a_note_shows_its_content(self, qapp, db: Database):
        self.seed(db, "第七集", "# 这一集讲位运算")

        panel = NotesPanel(db)

        assert "位运算" in panel.content_view.toPlainText()

    def test_show_note_displays_a_freshly_generated_one(self, qapp, db: Database):
        panel = NotesPanel(db)

        panel.show_note("# 刚生成的\n讲 for 循环")

        assert "for 循环" in panel.content_view.toPlainText()


class TestStatsPanel:
    def test_empty_state_points_at_where_to_start(self, qapp, db: Database):
        panel = StatsPanel(db)

        assert "还没做过题" in panel.summary_label.text()
        assert "从题库挑题" in panel.browser.toPlainText()

    def test_shows_the_running_total(self, qapp, db: Database):
        question_id = db.save_questions([Question(type="fill", question="甲", answer="1")])[0]
        db.record_answer(question_id, "1", is_correct=True)

        panel = StatsPanel(db)

        assert "累计做过 1 道" in panel.summary_label.text()
        assert "正确率 100%" in panel.summary_label.text()

    def test_breaks_down_by_exam_type(self, qapp, db: Database):
        """分题型看比只看总分有用——总分 70% 可能掩盖「程序填空只有 30%」。"""
        question_id = db.save_questions([Question(type="fill", question="甲", answer="1")])[0]
        db.record_answer(question_id, "1", is_correct=True)

        panel = StatsPanel(db)

        assert "填空题" in panel.browser.toPlainText()
