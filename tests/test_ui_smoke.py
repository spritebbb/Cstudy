"""界面冒烟测试：窗口和对话框**建得出来、不报错**。

项目里没有 pytest-qt，所以这里**只验证构造**，不模拟点击——真实交互得靠人点。
但"构造顺序错了"这类问题是一建就炸的，这个测试正好挡得住
（真踩过：`_refresh_count` 跑在 `source_combo` 建出来之前）。
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

# 没有显示器也要能跑，所以强制用离屏后端。必须在 import PyQt6 之前设。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import Qt, QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from cstudy.ai.relations import RelationProposal  # noqa: E402
from cstudy.config import Config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.graph import MIN_GAP  # noqa: E402
from cstudy.models import Question  # noqa: E402
from cstudy.ui import rendering  # noqa: E402
from cstudy.ui.filter_dialog import FilterDialog  # noqa: E402
from cstudy.ui.graph_panel import KnowledgeGraphPanel  # noqa: E402
from cstudy.ui.notes_panel import NotesPanel  # noqa: E402
from cstudy.ui.relation_dialog import RelationDialog  # noqa: E402
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


# ---------- 知识图谱 ----------


@pytest.fixture
def graphed(db: Database) -> Database:
    """两个章、三个知识点、几道题、一条确认过的关系——够画一张小图。"""
    pointer = db.upsert_syllabus_point("指针变量", "第8章", 1)
    array = db.upsert_syllabus_point("用指针处理数组", "第8章", 2)
    loop = db.upsert_syllabus_point("while 循环", "第5章", 3)
    ids = db.save_questions(
        [Question(type="fill", question=f"题{i}", answer="1", point_id=pointer) for i in range(4)]
        + [Question(type="coding", question="写个程序", answer="1", point_id=array)]
    )
    db.save_relations(
        [{"from_id": pointer, "to_id": array, "kind": "prereq",
          "reason": "得先懂指针才能用指针处理数组", "origin": "ai"}]
    )
    # 两道错的、一道对的——做满 3 道才会被算成"偏弱"，见 graph.MIN_SAMPLE
    db.record_answer(ids[0], "0", is_correct=False)
    db.record_answer(ids[1], "0", is_correct=False)
    db.record_answer(ids[2], "1", is_correct=True)
    return db


def graph_config() -> Config:
    return Config({"graph": {}, "paths": {}}, Path("."))


class TestKnowledgeGraphPanel:
    def test_constructs_without_a_syllabus(self, qapp, db: Database):
        panel = KnowledgeGraphPanel(db, graph_config())

        assert "还没有考纲" in panel.summary_label.text()
        assert "seed_syllabus" in panel.detail.toPlainText()

    def test_draws_a_node_for_every_point(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())

        assert len(panel._nodes) == 3
        assert "3 个知识点" in panel.summary_label.text()

    def test_the_summary_says_the_colours_are_all_grey_before_any_practice(self, qapp, db: Database):
        """现在库里只有 1 条作答记录，整张图是灰的。**必须说出来**，
        不然用户会以为"薄弱区"这个功能坏了。"""
        db.upsert_syllabus_point("指针", "第8章", 1)

        panel = KnowledgeGraphPanel(db, graph_config())

        assert "颜色还都是灰的" in panel.summary_label.text()

    def test_the_summary_reports_questions_that_are_not_linked(self, qapp, graphed: Database):
        """图里画不出来的题必须报数，否则用户以为这张图就是全部题库。"""
        graphed.save_questions([Question(type="fill", question="没挂的", answer="1")])

        panel = KnowledgeGraphPanel(graphed, graph_config())

        assert "1 道题没挂到知识点上" in panel.summary_label.text()

    def test_clicking_a_node_fills_the_detail_pane(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())

        panel._on_node_clicked(panel._data.nodes[0].id)

        text = panel.detail.toPlainText()
        assert "指针变量" in text
        assert "共 4 道" in text
        assert "偏弱" in text               # 3 道里对 1 道 = 33%
        assert "先学这个" in text

    def test_the_detail_pane_offers_to_practise_just_that_point(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())
        panel._on_node_clicked(panel._data.nodes[0].id)

        assert "practice:" in panel.detail.toHtml()

    def test_the_detail_pane_offers_to_delete_each_relation(self, qapp, graphed: Database):
        """AI 提的关系难免有不准的，用户得能自己在图上纠掉。"""
        panel = KnowledgeGraphPanel(graphed, graph_config())
        panel._on_node_clicked(panel._data.nodes[0].id)

        assert "del:" in panel.detail.toHtml()

    def test_deleting_a_relation_from_the_pane_takes_it_out_of_the_database(
        self, qapp, graphed: Database
    ):
        panel = KnowledgeGraphPanel(graphed, graph_config())
        edge_id = graphed.list_relations()[0]["id"]

        panel._on_anchor(QUrl(f"del:{edge_id}"))

        assert graphed.list_relations() == []
        assert panel.summary_label.text().count("还没有任何关系") == 1

    def test_practising_emits_the_point_id(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())
        seen: list[int] = []
        panel.practice_requested.connect(seen.append)
        point_id = panel._data.nodes[0].id

        panel._on_anchor(QUrl(f"practice:{point_id}"))

        assert seen == [point_id]

    def test_a_broken_link_does_not_blow_up(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())

        panel._on_anchor(QUrl("del:不是数字"))
        panel._on_anchor(QUrl("practice:"))
        panel._on_anchor(QUrl("别的什么"))

        assert graphed.list_relations() != []      # 什么都没动

    def test_the_legend_uses_the_theme_not_hardcoded_colours(self, qapp, graphed: Database):
        """面板里的 HTML 颜色**不受 QSS 管**——写死的深红在深色底上几乎看不见。
        （错题本、考纲、学情三处都踩过这个坑。）"""
        from cstudy.ui.theme import DARK

        panel = KnowledgeGraphPanel(graphed, graph_config(), theme=DARK)

        html = panel._legend_html()
        assert DARK.danger in html
        assert DARK.ok in html
        assert "先学这个" in html

    def test_refreshing_after_a_change_keeps_the_node_positions(self, qapp, graphed: Database):
        """题量变了不该让整张图跳位置——用户正对着看呢。"""
        panel = KnowledgeGraphPanel(graphed, graph_config())
        before = dict(panel._positions)

        graphed.save_questions([Question(type="fill", question="新题", answer="1")])
        panel.refresh()

        assert panel._positions == before

    def test_relayout_actually_moves_things(self, qapp, graphed: Database):
        """**「重新排布」不能是个空按钮。**

        布局是确定性的（同一份数据永远算出一样的位置，这是"位置稳定"那条设计），
        所以按原来的种子重排等于什么都没做——用户点了没反应会以为按钮坏了。
        换个种子才真的动。
        """
        panel = KnowledgeGraphPanel(graphed, graph_config())
        before = dict(panel._positions)

        panel._on_relayout()

        assert panel._positions != before
        # 换完还是同一批点、还是不许叠在一起
        assert set(panel._positions) == set(before)
        ids = list(before)
        closest = min(
            math.dist(panel._positions[a], panel._positions[b])
            for index, a in enumerate(ids)
            for b in ids[index + 1:]
        )
        assert closest >= MIN_GAP - 1e-9

    def test_every_node_and_name_actually_lands_in_the_scene(self, qapp, graphed: Database):
        """**这条盯的是一个真踩过的坑**：名字算了一堆位置、忘了 `scene.addItem`，
        测试全绿、界面上一个字都没有。所以得数一数场景里真有东西。"""
        from PyQt6.QtWidgets import QGraphicsEllipseItem, QGraphicsSimpleTextItem

        panel = KnowledgeGraphPanel(graphed, graph_config())

        ellipses = [i for i in panel.scene.items() if isinstance(i, QGraphicsEllipseItem)]
        texts = [i for i in panel.scene.items() if isinstance(i, QGraphicsSimpleTextItem)]

        assert len(ellipses) == len(panel._data.nodes)
        # 每个知识点一个名字，外加每个章一个章名
        assert len(texts) == len(panel._data.nodes) + len(panel._data.by_chapter())

    def test_the_edges_and_their_arrows_land_in_the_scene(self, qapp, graphed: Database):
        panel = KnowledgeGraphPanel(graphed, graph_config())

        assert len(panel._edge_items) == len(panel._data.edges)
        # 夹具里唯一那条关系是「先学这个」，所以应该正好一个箭头
        assert len(panel._arrow_items) == 1

    def test_clicking_a_line_asks_first_and_then_deletes_it(
        self, qapp, graphed: Database, monkeypatch
    ):
        """图例上写了"点连线可以删掉它"，那就**必须真的能删**——
        第一版图例这么写，实际点上去什么都不会发生。"""
        from cstudy.ui import graph_panel as module

        panel = KnowledgeGraphPanel(graphed, graph_config())
        edge = panel._data.edges[0]
        asked: list[str] = []
        monkeypatch.setattr(
            module.QMessageBox,
            "question",
            lambda *args, **kwargs: (
                asked.append(str(args[2])),
                module.QMessageBox.StandardButton.Yes,
            )[1],
        )

        panel._on_edge_clicked(edge)

        assert asked and "删掉" in asked[0]
        assert graphed.list_relations() == []

    def test_saying_no_to_the_question_keeps_the_relation(
        self, qapp, graphed: Database, monkeypatch
    ):
        """线细又挨着别的东西，点错很正常——所以必须问一遍。"""
        from cstudy.ui import graph_panel as module

        panel = KnowledgeGraphPanel(graphed, graph_config())
        monkeypatch.setattr(
            module.QMessageBox,
            "question",
            lambda *args, **kwargs: module.QMessageBox.StandardButton.No,
        )

        panel._on_edge_clicked(panel._data.edges[0])

        assert len(graphed.list_relations()) == 1

    def test_finding_relations_without_a_client_warns_instead_of_crashing(
        self, qapp, graphed: Database, monkeypatch
    ):
        from cstudy.ui import graph_panel

        warned: list[str] = []
        monkeypatch.setattr(
            graph_panel.QMessageBox,
            "warning",
            lambda *args, **kwargs: warned.append(str(args[-1])),
        )
        panel = KnowledgeGraphPanel(graphed, graph_config(), client=None)

        panel._on_find_relations()

        assert warned and "AI" in warned[0]

    def test_an_empty_graph_is_not_drawn(self, qapp, db: Database):
        panel = KnowledgeGraphPanel(db, graph_config())

        assert panel._nodes == {}

    def test_a_selection_that_disappeared_does_not_dim_the_whole_graph(
        self, qapp, graphed: Database
    ):
        """选中的知识点没了（软件开着的时候重灌了考纲）时，
        **不能把整张图淡成一片黑**——"高亮的那个"找不到的话，
        所有点都会被算成"跟它无关"。"""
        panel = KnowledgeGraphPanel(graphed, graph_config())
        point_id = panel._data.nodes[0].id
        panel._on_node_clicked(point_id)
        graphed.conn.execute("DELETE FROM syllabus_points WHERE id = ?", (point_id,))
        graphed.conn.commit()

        panel.refresh()

        assert panel._selected is None
        assert all(item.brush().color().alpha() == 255 for item in panel._nodes.values())


class TestRelationDialog:
    """**勾上的才入库**——这是 AI 说的话能不能进库的唯一关口。"""

    def _dialog(self, db: Database, proposals=None) -> RelationDialog:
        db.upsert_syllabus_point("指针变量", "第8章", 1)
        db.upsert_syllabus_point("用指针处理数组", "第8章", 2)
        return RelationDialog(db, proposals=proposals or [], parent=None)

    def _proposal(self, from_name: str = "指针变量", to_name: str = "用指针处理数组"):
        from cstudy.ai.relations import ProposedRelation

        return ProposedRelation(
            from_name=from_name, to_name=to_name, kind="prereq", reason="有先后"
        )

    def test_constructs_with_nothing_to_show(self, qapp, db: Database):
        dialog = self._dialog(db)

        assert "还没有任何关系" in dialog.list.item(0).text()

    def test_a_proposal_is_checked_and_approved_by_default(self, qapp, db: Database):
        dialog = self._dialog(db, [self._proposal()])

        assert [r.from_name for r in dialog.approved()] == ["指针变量"]

    def test_unchecking_a_proposal_drops_it(self, qapp, db: Database):
        dialog = self._dialog(db, [self._proposal()])

        dialog.list.item(1).setCheckState(Qt.CheckState.Unchecked)   # 第 0 项是分组标题

        assert dialog.approved() == []

    def test_the_reason_is_shown_next_to_each_row(self, qapp, db: Database):
        """光看到"A 和 B 有关系"判断不了对不对。"""
        dialog = self._dialog(db, [self._proposal()])

        assert "理由：有先后" in dialog.list.item(1).text()

    def test_an_existing_relation_is_checked_and_unchecking_means_delete(
        self, qapp, db: Database
    ):
        left = db.upsert_syllabus_point("甲", "第1章", 1)
        right = db.upsert_syllabus_point("乙", "第2章", 2)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "related", "reason": "有关"}]
        )
        dialog = RelationDialog(db, proposals=[], parent=None)
        edge_id = int(db.list_relations()[0]["id"])

        assert dialog.removed_ids() == []           # 默认是保留

        dialog.list.item(1).setCheckState(Qt.CheckState.Unchecked)

        assert dialog.removed_ids() == [edge_id]
        assert dialog.approved() == []              # 已有的不算"新增"

    def test_adding_one_by_hand_needs_a_reason(self, qapp, db: Database):
        dialog = self._dialog(db)
        dialog.to_combo.setCurrentIndex(1)      # 选一个不一样的，别撞上"自己连自己"

        dialog._on_add()

        assert "理由" in dialog.summary_label.text()
        assert dialog.approved() == []

    def test_adding_one_by_hand_refuses_a_self_loop(self, qapp, db: Database):
        dialog = self._dialog(db)
        dialog.reason_edit.setText("随便写个理由")

        dialog._on_add()          # 两个下拉框默认都停在第一个知识点上

        assert "同一个" in dialog.summary_label.text()
        assert dialog.approved() == []

    def test_adding_one_by_hand_marks_it_as_manual(self, qapp, db: Database):
        dialog = self._dialog(db)
        dialog.to_combo.setCurrentIndex(1)
        dialog.reason_edit.setText("老师的讲义里放在一起讲的")

        dialog._on_add()

        relation = dialog.approved()[0]
        assert relation.origin == "manual"
        assert "自己加的" in dialog.list.item(1).text()

    def test_the_summary_counts_what_will_happen(self, qapp, db: Database):
        dialog = self._dialog(db, [self._proposal(), self._proposal("甲", "乙")])

        assert "新增 2 条" in dialog.summary_label.text()

    def test_the_dialog_writes_nothing_by_itself(self, qapp, db: Database):
        """**点确定之前库里一条都不能多**——这个对话框只负责算，
        写库由面板统一做。这样点取消就真的什么都没发生。"""
        self._dialog(db, [self._proposal()])

        assert db.list_relations() == []

    def test_adding_one_by_hand_refuses_a_duplicate(self, qapp, db: Database):
        """库里已有 A↔B 时，手加 B↔A 不能存成第二条。
        「容易混」和「相关」是不分方向的，数据库的 UNIQUE 只看方向，挡不住。"""
        left = db.upsert_syllabus_point("甲", "第1章", 1)
        right = db.upsert_syllabus_point("乙", "第2章", 2)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "confuse", "reason": "容易混"}]
        )
        dialog = RelationDialog(db, proposals=[], parent=None)
        # 类型选「容易混」——不分方向的那种才存在"反着写"的问题
        dialog.kind_combo.setCurrentIndex(
            [dialog.kind_combo.itemData(i) for i in range(dialog.kind_combo.count())].index("confuse")
        )
        dialog.reason_edit.setText("反着再说一遍")
        # 两个下拉框按名字顺序排：把 from/to 反过来选
        dialog.from_combo.setCurrentIndex(1)
        dialog.to_combo.setCurrentIndex(0)

        dialog._on_add()

        assert "已经有了" in dialog.summary_label.text()
        assert dialog.approved() == []

    def test_adding_one_by_hand_refuses_the_same_thing_twice_in_a_row(
        self, qapp, db: Database
    ):
        dialog = self._dialog(db)
        dialog.to_combo.setCurrentIndex(1)
        dialog.reason_edit.setText("老师的讲义里放在一起讲的")
        dialog._on_add()

        dialog.reason_edit.setText("手滑又加了一遍")
        dialog._on_add()

        assert len(dialog.approved()) == 1
        assert "已经有了" in dialog.summary_label.text()

    def test_the_dropped_and_the_corrected_are_shown_under_separate_headings(
        self, qapp, db: Database
    ):
        """认回名字的那几条**是列进来了的**，跟"没列进来"混在一个标题底下，
        用户会以为它没被采纳，然后在列表里白找。"""
        db.upsert_syllabus_point("甲", "第1章", 1)
        db.upsert_syllabus_point("乙", "第2章", 2)

        dialog = RelationDialog(
            db,
            proposals=[self._proposal("甲", "乙")],
            dropped=["「丙丁」在考纲里找不到，整条丢掉"],
            corrected=["AI 把「甲 」写岔了，按考纲里的「甲」认了"],
            parent=None,
        )

        texts = [label.text() for label in dialog.findChildren(QLabel)]
        joined = "\n".join(texts)
        assert "按考纲里最接近的那个认了" in joined
        assert "没有列进来" in joined
        # 两块提示是分开的两个控件，不是一个
        assert sum(1 for t in texts if "丙丁" in t or "写岔了" in t) == 2

    def test_no_notice_when_there_is_nothing_to_report(self, qapp, db: Database):
        db.upsert_syllabus_point("甲", "第1章", 1)
        db.upsert_syllabus_point("乙", "第2章", 2)

        dialog = RelationDialog(db, proposals=[self._proposal("甲", "乙")], parent=None)

        joined = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        assert "没有列进来" not in joined


class TestApplyingRelationChanges:
    """**勾上的才入库**这条规则的落地处。

    对话框只负责算，写库在面板里做（`_apply_and_report`）。这一段最容易
    出错的地方是"一条写不进去就把整批都丢了"——那用户得在几十条里重勾一遍。

    （注意 `graphed` 夹具里本来就有一条关系，所以断言都看"变了多少条"，
    不是看总数。）
    """

    def _panel(self, db: Database) -> KnowledgeGraphPanel:
        return KnowledgeGraphPanel(db, graph_config())

    def _proposal(self, *pairs):
        from cstudy.ai.relations import ProposedRelation

        return RelationProposal(
            relations=[
                ProposedRelation(a, b, kind, f"{a} 和 {b} 的关系")
                for a, b, kind in pairs
            ]
        )

    # 夹具里已确认的是「指针变量 →用指针处理数组」，这两对都不会跟它撞
    def _fresh_pairs(self):
        return (
            ("指针变量", "while 循环", "related"),
            ("while 循环", "用指针处理数组", "confuse"),
        )

    def _pairs_in(self, db: Database) -> set[tuple[str, str, str]]:
        return {
            (str(row["from_name"]), str(row["to_name"]), str(row["kind"]))
            for row in db.list_relations()
        }

    def test_only_checked_proposals_get_saved(self, qapp, graphed: Database, monkeypatch):
        from cstudy.ui import graph_panel as module

        panel = self._panel(graphed)
        before = self._pairs_in(graphed)

        def accept(dialog):
            dialog.list.item(2).setCheckState(Qt.CheckState.Unchecked)  # 第 0 项是分组标题
            return 1

        monkeypatch.setattr(module.RelationDialog, "exec", accept)

        panel._on_proposal(self._proposal(*self._fresh_pairs()))

        added = self._pairs_in(graphed) - before
        assert added == {("指针变量", "while 循环", "related")}

    def test_saved_relations_are_marked_as_coming_from_ai(
        self, qapp, graphed: Database, monkeypatch
    ):
        """是 AI 提的还是自己敲的，得分开记——用户要知道哪条该多留个心眼。"""
        from cstudy.ui import graph_panel as module

        panel = self._panel(graphed)
        monkeypatch.setattr(module.RelationDialog, "exec", lambda dialog: 1)

        panel._on_proposal(self._proposal(("指针变量", "while 循环", "related")))

        row = next(
            row for row in graphed.list_relations() if row["to_name"] == "while 循环"
        )
        assert row["origin"] == "ai"

    def test_unchecking_existing_relations_deletes_them(
        self, qapp, graphed: Database, monkeypatch
    ):
        from cstudy.ui import graph_panel as module
        from cstudy.ui.relation_dialog import ROLE_EXISTING_ID

        panel = self._panel(graphed)
        assert graphed.list_relations()          # 夹具里有一条，下面要验证它被删掉

        def uncheck_everything(dialog):
            for index in range(dialog.list.count()):
                item = dialog.list.item(index)
                if item.data(ROLE_EXISTING_ID) is not None:
                    item.setCheckState(Qt.CheckState.Unchecked)
            return 1

        monkeypatch.setattr(module.RelationDialog, "exec", uncheck_everything)

        panel._on_manage_relations()

        assert graphed.list_relations() == []

    def test_one_relation_failing_does_not_lose_the_others(
        self, qapp, graphed: Database, monkeypatch
    ):
        """写一条失败就把整批丢掉的话，用户得在几十条里重勾一遍——
        所以每条单独 try，并且如实报出有几条没写进去。"""
        from cstudy.ui import graph_panel as module

        panel = self._panel(graphed)
        before = self._pairs_in(graphed)
        monkeypatch.setattr(module.RelationDialog, "exec", lambda dialog: 1)
        status: list[str] = []
        panel.status_changed.connect(status.append)

        real_save = graphed.save_relations
        calls = {"n": 0}

        def flaky(payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("模拟写库失败")
            return real_save(payload)

        monkeypatch.setattr(graphed, "save_relations", flaky)

        panel._on_proposal(self._proposal(*self._fresh_pairs()))

        assert len(self._pairs_in(graphed) - before) == 1     # 第二条还是写进去了
        assert status and "没写进去" in status[-1]

    def test_a_relation_naming_an_unknown_point_is_not_silently_saved(
        self, qapp, graphed: Database, monkeypatch
    ):
        """名字对不上就抛，让上层记日志——不能硬塞一个错的 id 进去，
        那会连出一条谁也说不清的关系。"""
        from cstudy.ui import graph_panel as module

        panel = self._panel(graphed)
        before = self._pairs_in(graphed)
        monkeypatch.setattr(module.RelationDialog, "exec", lambda dialog: 1)

        panel._on_proposal(
            self._proposal(("考纲里没有的知识点", "指针变量", "related"))
        )

        assert self._pairs_in(graphed) == before


class TestMainWindowGraphWiring:
    """主窗口把知识图谱接进来了没有。

    **这类"接错了"的问题构造时不报错**——tab 忘了加、信号忘了连，
    都可能一路跑到用户手上才发现。而签收标准恰恰是"点一下能不能用"，
    所以这几条得测。
    """

    def _window(self, db: Database):
        from cstudy.ui.main_window import MainWindow
        from cstudy.ui.theme import load_appearance

        config = Config({"ui": {}, "quiz": {}, "paths": {}}, Path("."))
        return MainWindow(config, db, load_appearance(config))

    def _point_with_questions(self, db: Database) -> int:
        row = db.conn.execute(
            "SELECT point_id FROM questions WHERE point_id IS NOT NULL LIMIT 1"
        ).fetchone()
        return int(row["point_id"])

    def test_the_graph_has_its_own_tab(self, qapp, graphed: Database):
        window = self._window(graphed)

        names = [window.tabs.tabText(i) for i in range(window.tabs.count())]

        assert "知识图谱" in names
        assert window.tabs.currentWidget() is not None

    def test_practising_from_the_graph_lands_in_the_quiz_page(self, qapp, graphed: Database):
        window = self._window(graphed)

        window._on_practice_point(self._point_with_questions(graphed))

        assert window.tabs.currentWidget() is window.quiz_panel
        assert "知识图谱" in window.status_label.text()

    def test_the_quiz_page_actually_received_questions(self, qapp, graphed: Database):
        """状态栏说"拿了 N 道"不算数——题得**真的出现在答题页上**。"""
        window = self._window(graphed)

        window._on_practice_point(self._point_with_questions(graphed))

        assert window.quiz_panel.question_view.toPlainText().strip()

    def test_a_point_without_questions_says_so_instead_of_opening_an_empty_page(
        self, qapp, graphed: Database, monkeypatch
    ):
        """考纲里有知识点一道题都没有（补题之前就是这样），
        点了不该打开一个空白的答题页让用户对着发呆。"""
        from cstudy.ui import main_window as module

        told: list[str] = []
        monkeypatch.setattr(
            module.QMessageBox,
            "information",
            lambda *args, **kwargs: told.append(str(args[-1])),
        )
        window = self._window(graphed)
        empty = graphed.upsert_syllabus_point("还没出题的知识点", "第1章", 99)
        window.graph_panel.refresh()

        window._on_practice_point(empty)

        assert told and "一道题都没有" in told[0]
        assert window.tabs.currentWidget() is not window.quiz_panel

    def test_switching_to_the_graph_tab_refreshes_it(self, qapp, graphed: Database):
        """切过去要重算——刚做完题，圆圈的颜色和大小得跟着变。"""
        window = self._window(graphed)
        point_id = self._point_with_questions(graphed)
        before = next(
            node for node in window.graph_panel._data.nodes if node.id == point_id
        ).question_count
        graphed.save_questions(
            [Question(type="fill", question="新加的题", answer="1", point_id=point_id)]
        )

        window.tabs.setCurrentWidget(window.graph_panel)

        after = next(
            node for node in window.graph_panel._data.nodes if node.id == point_id
        ).question_count
        assert after == before + 1
