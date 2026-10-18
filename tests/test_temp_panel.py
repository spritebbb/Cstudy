"""「考前突击」栏的刷题节奏：每轮只给几十道、能接着上次往下刷。

为什么专门测这个：用户的原话是"一次性刷的题太多了，我记不过来"。
一轮把 279 道全排进去，等于没有节奏；而这个功能的坑几乎都在"位置"上——
从哪接着、换题型会不会串、刷到头会不会绕回第一题。这些跑得出来，
所以写测试盯住。

没有显示器也要能跑：强制离屏后端，且必须在 import PyQt6 之前设。
项目里没有 pytest-qt，所以这里不模拟鼠标，直接调面板的方法——
但调的是真控件、真库文件（离屏建 QApplication，和 test_ui_smoke 一个套路）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtTest import QTest  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from cstudy.config import Config  # noqa: E402
from cstudy.temp_bank import TempQuestion  # noqa: E402
from cstudy.ui import temp_panel as temp_panel_module  # noqa: E402
from cstudy.ui.temp_panel import TempBankPanel  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    # 整个测试会话只建一个 QApplication，多建会崩
    yield QApplication.instance() or QApplication([])


class NoPopups:
    """测试里不许弹窗——弹出来会一直等人点，整条测试就卡死。

    只实现被测代码真的会用到的那两个（`_clear_bank` 里的 question 返回"是"）。
    """

    @staticmethod
    def information(*args, **kwargs):
        return None

    @staticmethod
    def warning(*args, **kwargs):
        return None


@pytest.fixture
def no_popups(monkeypatch):
    monkeypatch.setattr(temp_panel_module, "QMessageBox", NoPopups)


def make_question(index: int, qtype: str = "single") -> TempQuestion:
    options = ["甲", "乙"] if qtype in {"single", "multi"} else []
    answer = "正确" if qtype == "judge" else "A"
    return TempQuestion(qtype=qtype, stem=f"第 {index} 题", options=options, answer=answer)


@pytest.fixture
def panel(qapp, tmp_path):
    """一个装好单选题的临时栏（库文件在 tmp_path 里，不碰真库）。

    这一栏默认**会把选项顺序打乱**，但这里的测试大多在验"位置 / 节奏 / 键盘"，
    所以夹具里关掉打乱（正确答案稳定在第一个，`answer_current` 才点得准）。
    打乱本身另有 `TestShuffledOptions` 专门盯着。
    """
    config = Config(
        {"paths": {"temp_bank": str(tmp_path / "bank.db")}, "temp_bank": {"shuffle_options": False}},
        Path("."),
    )
    widget = TempBankPanel(config)
    widget.bank.add([make_question(i) for i in range(1, 41)])
    widget.refresh_counts()
    widget._update_buttons()
    yield widget
    widget.deleteLater()


def answer_current(widget: TempBankPanel) -> None:
    """把当前这道题选上第一个选项并提交（模拟真人点一下）。"""
    widget._option_widgets[0].setChecked(True)
    widget._on_submit()


def set_round(widget: TempBankPanel, size: int) -> None:
    widget.round_combo.setCurrentIndex(widget.round_combo.findData(size))


def select_type(widget: TempBankPanel, label: str) -> None:
    widget.type_combo.setCurrentIndex(widget.type_combo.findText(label))


class TestRoundSize:
    def test_default_round_is_20(self, panel):
        """配置里没写 round_size 时，一轮 20 道。"""
        assert panel._round_size() == 20

    def test_start_only_loads_one_round(self, panel):
        panel.start()
        assert len(panel._items) == 20
        assert panel._items[0].stem == "第 1 题"
        assert panel.progress_label.text() == "第 1/20 道"

    def test_round_size_can_be_all(self, panel):
        set_round(panel, 0)
        panel.start()
        assert len(panel._items) == 40
        assert panel.progress_label.text() == "第 1/40 道"


class TestStartAndRestart:
    """「开始刷题」= 接着往下刷；「重刷这批」= 把刚做完的那一批再刷一遍。

    用户原话（2026-10-15，第一次）："我点开始刷题后他又从第一轮给我开始刷了"——
    原来的设计是「开始刷题」= 从头、「接着上次刷」= 接续，跟直觉正好相反，
    而且点「开始刷题」还会把位置清掉、顺手把另一个按钮弄灰。

    用户原话（同一天，第二次）："我要重头再刷的意思是我要重新刷我刚刚刷完的
    二十道题，而不是从第一道题开始刷"——所以「重刷这批」重装的是**当前这一批**，
    位置、题型、顺序都不动。
    """

    def _finish_round(self, panel) -> None:
        """把这一轮的 20 道全答完（每题都对）。"""
        for _ in range(len(panel._items)):
            answer_current(panel)
            panel._next()

    def test_restart_is_disabled_before_any_batch(self, panel):
        """一次都没点过「开始刷题」时，「重刷这批」没有东西可重刷。"""
        assert not panel.restart_button.isEnabled()
        assert panel._batch == []

    def test_start_keeps_the_old_position(self, panel):
        """再点一次「开始刷题」是往下接，不能把位置清掉（用户报的就是这个）。"""
        select_type(panel, "单选题")
        panel.start()
        answer_current(panel)
        assert panel.bank.meta_get("pos:single") == "1"
        panel.start()
        assert panel.bank.meta_get("pos:single") == "1"

    def test_start_continues_after_last_answered(self, panel, no_popups):
        panel.start()
        self._finish_round(panel)
        assert panel.restart_button.isEnabled()

        panel.start()          # 主按钮就是"接着上次"
        assert len(panel._items) == 20
        assert panel._items[0].stem == "第 21 题"
        # 一轮只有 20 道，光看"第 1/20 道"不知道整份刷到哪了
        assert panel.progress_label.text() == "第 1/20 道（整份第 21–40 道，共 40 道）"

    def test_start_at_the_end_says_so_instead_of_looping(self, panel, no_popups):
        set_round(panel, 0)          # 一轮全取
        panel.start()
        self._finish_round(panel)
        panel.start()                # 已经刷到头了
        assert panel._items == []
        assert "刷到头" in panel.feedback.toPlainText()
        assert "重刷这批" in panel.feedback.toPlainText()

    def test_restart_repeats_the_same_batch(self, panel, no_popups):
        """「重刷这批」＝ 再刷一遍刚做完的这 20 道，不是回到整份的第 1 道。

        用户原话："我要重新刷我刚刚刷完的二十道题，而不是从第一道题开始刷"。
        """
        set_round(panel, 10)
        panel.start()
        first_batch = [q.stem for q in panel._items]
        for _ in range(10):                  # 把这批做完
            answer_current(panel)
            panel._next()
        assert panel.bank.meta_get("pos:all") == "10"

        panel.restart()
        assert [q.stem for q in panel._items] == first_batch      # 还是这 10 道
        assert panel._index == 0
        assert (panel._right, panel._wrong) == (0, 0)             # 本轮成绩清零
        assert panel.bank.meta_get("pos:all") == "10"             # 位置一点没动

    def test_restart_does_not_jump_to_the_very_first_question(self, panel, no_popups):
        """第二批（第 21–40 道）重刷的还是第 21 道起——用户报的就是这个。"""
        set_round(panel, 0)
        panel.start()
        for _ in range(20):                  # 第一批做完
            answer_current(panel)
            panel._next()
        panel.start()                        # 第二批：第 21–40 道
        assert panel._items[0].stem == "第 21 题"

        panel.restart()
        assert panel._items[0].stem == "第 21 题"      # 不是「第 1 题」
        assert len(panel._items) == 20

    def test_restart_survives_closing_the_app(self, qapp, tmp_path, no_popups):
        """「上一批」记在库里：关了软件重开，这个按钮还认得它。"""
        config = Config({"paths": {"temp_bank": str(tmp_path / "bank.db")}}, Path("."))
        first = TempBankPanel(config)
        first.bank.add([make_question(i) for i in range(1, 41)])
        first.refresh_counts()
        set_round(first, 10)
        first.start()
        batch = [q.stem for q in first._items]

        again = TempBankPanel(config)        # 相当于关掉软件重新打开
        assert again.restart_button.isEnabled()
        again.restart()
        assert [q.stem for q in again._items] == batch
        again.deleteLater()
        first.deleteLater()

    def test_start_in_random_mode_does_not_touch_position(self, panel, no_popups):
        """随机模式：点「开始刷题」翻一批新的，但不该把记着的位置弄丢。"""
        select_type(panel, "单选题")
        panel.start()
        answer_current(panel)
        assert panel.bank.meta_get("pos:single") == "1"
        panel.order_combo.setCurrentIndex(panel.order_combo.findData("random"))
        panel.start()
        assert len(panel._items) == 20
        assert panel.bank.meta_get("pos:single") == "1"

    def test_position_is_kept_per_type(self, panel, no_popups):
        """换题型不该套用另一个题型的位置。"""
        select_type(panel, "单选题")
        panel.start()
        answer_current(panel)
        assert panel.bank.meta_get("pos:single")
        assert panel.bank.meta_get("pos:judge") is None

        select_type(panel, "判断题")
        panel.bank.add([make_question(100, "judge")])
        set_round(panel, 10)
        panel.start()
        answer_current(panel)
        assert panel.bank.meta_get("pos:judge")

        select_type(panel, "多选题")                        # 多选一道没答过
        panel.start()
        assert panel._items == []

    def test_finished_round_points_to_the_buttons(self, panel):
        set_round(panel, 10)
        panel.start()
        for _ in range(10):
            answer_current(panel)
            panel._next()
        text = panel.feedback.toPlainText()
        assert "做完了" in text and "开始刷题" in text and "重刷这批" in text


def answer_current_wrong(widget: TempBankPanel) -> None:
    """故意选错：选第 2 个选项（夹具里的题正确答案都是 A）。"""
    widget._option_widgets[1].setChecked(True)
    widget._on_submit()


def wrong_the_first_question(widget: TempBankPanel, times: int = 2) -> None:
    """把第一道题做错 `times` 次（默认门槛是 1 次，所以错一次就够收进来）。

    第一次先装一批，之后用「重刷这批」回到同一批的第 1 道（要是用「开始刷题」，
    第二次就跑到第 2 道去了，错两次的会是两道不同的题）。
    """
    widget.start()
    answer_current_wrong(widget)
    for _ in range(times - 1):
        widget.restart()
        answer_current_wrong(widget)


class TestWrongCollection:
    """错题集：做错的题收进来（默认错 1 次就收）、连对 2 次移出去，点「刷错题集」只刷它们。

    用户原话："添加错题集功能，多次做错的题进行搜集"，2026-10-15 又要求
    "把错题判断改成1"。
    """

    def test_one_wrong_collects_it_by_default(self, panel):
        panel.start()
        answer_current_wrong(panel)            # 错一道就收（门槛是 1）
        assert panel.wrong_button.isEnabled()
        assert panel.wrong_button.text() == "刷错题集（1）"

    def test_the_threshold_can_still_be_two(self, qapp, tmp_path, no_popups):
        """`temp_bank.wrong_min_times` 写 2 就回到"错两次才算数"。"""
        config = Config(
            {
                "paths": {"temp_bank": str(tmp_path / "bank.db")},
                "temp_bank": {"shuffle_options": False, "wrong_min_times": 2},
            },
            Path("."),
        )
        widget = TempBankPanel(config)
        widget.bank.add([make_question(i) for i in range(1, 4)])
        widget.refresh_counts()
        widget.start()
        answer_current_wrong(widget)           # 只错一次：门槛 2 时不收
        assert not widget.wrong_button.isEnabled()
        widget.restart()                       # 「重刷这批」回到同一道题
        answer_current_wrong(widget)           # 同一道错第二次
        assert widget.wrong_button.text() == "刷错题集（1）"
        widget.deleteLater()

    def test_two_wrongs_enable_the_button_and_show_a_count(self, panel):
        wrong_the_first_question(panel, 2)
        assert panel.wrong_button.isEnabled()
        assert panel.wrong_button.text() == "刷错题集（1）"
        assert "错题" in panel.wrong_button.toolTip()

    def test_start_wrong_only_loads_wrong_questions(self, panel):
        wrong_the_first_question(panel, 2)
        panel.start_wrong()
        assert panel._wrong_mode
        assert [q.stem for q in panel._items] == ["第 1 题"]
        assert panel.progress_label.text() == "错题第 1/1 道"
        # 做题时能看见这道题错了几次
        assert "错过 2 次" in panel.meta_label.text()

    def test_repeated_correct_answers_remove_it(self, panel):
        wrong_the_first_question(panel, 2)
        panel.start_wrong()
        answer_current(panel)                  # 对第 1 次：还在错题集里
        assert panel.wrong_button.text() == "刷错题集（1）"
        panel.start_wrong()
        answer_current(panel)                  # 连对 2 次：移出
        assert not panel.wrong_button.isEnabled()
        assert panel.wrong_button.text() == "刷错题集"

    def test_wrong_round_respects_the_round_size(self, panel):
        """错题集也按「每轮」限量，不然一次又堆几百道。"""
        questions = panel.bank.questions()
        for question in questions[:15]:
            panel.bank.record_answer(question.id, "B", False)
            panel.bank.record_answer(question.id, "B", False)
        panel.refresh_counts()
        assert panel.wrong_button.text() == "刷错题集（15）"
        set_round(panel, 10)
        panel.start_wrong()
        assert len(panel._items) == 10

    def test_wrong_mode_does_not_touch_the_resume_position(self, panel):
        """刷错题集不能把「开始刷题」记的位置带偏——两边的顺序不一样。"""
        panel.start()
        answer_current(panel)                  # 正常那份：位置记在第 1 题
        saved = panel.bank.meta_get("pos:all")
        assert saved

        questions = panel.bank.questions()
        fifth = questions[4]
        panel.bank.record_answer(fifth.id, "B", False)      # 第 5 题错两次
        panel.bank.record_answer(fifth.id, "B", False)
        panel.refresh_counts()

        panel.start_wrong()
        assert panel._items[0].id == fifth.id
        answer_current_wrong(panel)            # 在错题集里又错一次
        assert panel.bank.meta_get("pos:all") == saved      # 位置没被动过

    def test_finished_wrong_round_says_where_to_go_next(self, panel):
        wrong_the_first_question(panel, 2)
        panel.start_wrong()
        answer_current(panel)
        panel._next()
        text = panel.feedback.toPlainText()
        assert "错题" in text and "刷错题集" in text

    def test_wrong_set_can_be_filtered_by_type(self, panel):
        judge = make_question(100, "judge")
        panel.bank.add([judge])
        stored = [q for q in panel.bank.questions() if q.stem == "第 100 题"][0]
        panel.bank.record_answer(stored.id, "错误", False)
        panel.bank.record_answer(stored.id, "错误", False)
        panel.refresh_counts()

        select_type(panel, "判断题")
        panel.start_wrong()
        assert [q.stem for q in panel._items] == ["第 100 题"]
        panel.start_wrong()                    # 「全部」题型下只有这一道是错题
        select_type(panel, "全部")
        panel.start_wrong()
        assert [q.stem for q in panel._items] == ["第 100 题"]


class TestKeyboard:
    """键盘刷题：数字键 1–4 选选项、Enter 提交。

    用户原话："这样刷有点慢了" → "你把ABCD四个选项绑定到键盘按键ABCD，enter快捷提交"
    → 又改成"你改绑成1234吧"（所以绑的是数字键，不是字母键）。
    """

    def test_number_key_picks_the_matching_option(self, panel):
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_1)
        assert panel._option_widgets[0].isChecked()
        QTest.keyClick(panel, Qt.Key.Key_2)
        assert panel._option_widgets[1].isChecked()
        assert not panel._option_widgets[0].isChecked()      # 单选钮互斥

    def test_number_beyond_the_options_does_nothing(self, panel):
        """夹具里的题只有两个选项，按 4 不该有反应（更不能报错）。"""
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_4)
        assert not any(widget.isChecked() for widget in panel._option_widgets)

    def test_letter_keys_are_not_bound_anymore(self, panel):
        """用户要求改绑数字键：按 A 不该动选项。"""
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_A)
        assert not any(widget.isChecked() for widget in panel._option_widgets)

    def test_enter_submits(self, panel, no_popups):
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_1)
        QTest.keyClick(panel, Qt.Key.Key_Return)
        assert panel._answered
        assert "答对了" in panel.feedback.toPlainText()      # 夹具里正确答案是第一个

    def test_enter_goes_to_the_next_question_after_submitting(self, panel, no_popups):
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_1)
        QTest.keyClick(panel, Qt.Key.Key_Return)
        QTest.keyClick(panel, Qt.Key.Key_Return)             # 已作答：Enter 就是下一题
        assert panel._index == 1
        assert panel.progress_label.text() == "第 2/20 道"

    def test_enter_with_nothing_selected_does_not_answer(self, panel, no_popups):
        """一个字都没选就按 Enter：只提示，不能算作答。"""
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_Return)
        assert not panel._answered
        assert panel.submit_button.isEnabled()

    def test_multi_choice_toggles(self, panel):
        panel.bank.add([make_question(100, "multi")])
        select_type(panel, "多选题")
        set_round(panel, 10)
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_1)
        QTest.keyClick(panel, Qt.Key.Key_2)
        assert [widget.isChecked() for widget in panel._option_widgets] == [True, True]
        QTest.keyClick(panel, Qt.Key.Key_2)                  # 再按一次取消
        assert [widget.isChecked() for widget in panel._option_widgets] == [True, False]

    def test_judge_question_uses_position(self, panel):
        """判断题画的是"正确/错误"两个钮：1＝正确、2＝错误。"""
        panel.bank.add([make_question(100, "judge")])
        select_type(panel, "判断题")
        set_round(panel, 10)
        panel.start()
        QTest.keyClick(panel, Qt.Key.Key_1)
        assert panel._option_widgets[0].isChecked()
        QTest.keyClick(panel, Qt.Key.Key_2)
        assert panel._option_widgets[1].isChecked()

    def test_typing_in_the_answer_box_is_not_hijacked(self, qapp, tmp_path, no_popups):
        """问答题在框里写字时，数字键和 Enter 归输入框，快捷键不许抢。"""
        config = Config({"paths": {"temp_bank": str(tmp_path / "bank.db")}}, Path("."))
        widget = TempBankPanel(config)
        widget.bank.add([make_question(1, "short")])
        widget.refresh_counts()
        widget.start()
        widget.answer_edit.setFocus()
        QTest.keyClicks(widget.answer_edit, "123")
        QTest.keyClick(widget.answer_edit, Qt.Key.Key_Return)
        assert widget.answer_edit.toPlainText() == "123\n"
        assert not widget._answered                          # 没把"看参考答案"当成提交
        widget.deleteLater()

    def test_key_press_is_ignored_when_there_is_no_question(self, panel):
        """没开始刷题时按键不该炸。"""
        QTest.keyClick(panel, Qt.Key.Key_1)
        QTest.keyClick(panel, Qt.Key.Key_Return)
        assert not panel._answered

    def test_keys_work_when_focus_is_on_the_question_text(self, panel):
        """用户常先点一下题干（想划重点），那时按键得照样有用。

        题干是只读的 QTextBrowser：它不处理数字键，事件会冒泡到面板。
        """
        panel.start()
        panel.question_view.setFocus()
        QTest.keyClick(panel.question_view, Qt.Key.Key_2)
        assert panel._option_widgets[1].isChecked()
        QTest.keyClick(panel.question_view, Qt.Key.Key_Return)
        assert panel._answered


class TestShuffledOptions:
    """打乱选项。用户原话："每次重新刷题后，把选项打乱"。

    夹具 `panel` 里打乱是**关**的（那些测试在验位置/节奏/键盘），这里自己开一个。
    重点是两件事：顺序真的换了；**判分还是对的**（答案字母得跟着重算）。
    """

    @staticmethod
    def _panel(tmp_path, shuffle: bool = True, questions=None):
        config = Config(
            {
                "paths": {"temp_bank": str(tmp_path / "bank.db")},
                "temp_bank": {"shuffle_options": shuffle},
            },
            Path("."),
        )
        widget = TempBankPanel(config)
        widget.bank.add(
            questions if questions is not None else [make_question(i) for i in range(1, 41)]
        )
        widget.refresh_counts()
        widget._update_buttons()
        return widget

    def test_shuffle_is_on_by_default(self, qapp, tmp_path):
        """配置里没写这一项时默认开着——这是用户要的行为。"""
        config = Config({"paths": {"temp_bank": str(tmp_path / "bank.db")}}, Path("."))
        widget = TempBankPanel(config)
        assert widget._shuffle_options is True
        widget.deleteLater()

    def test_options_are_shuffled_and_keep_the_same_set(self, qapp, tmp_path):
        widget = self._panel(tmp_path)
        stored = {q.id: list(q.options) for q in widget.bank.questions()}
        widget.start()
        assert widget._items
        for item in widget._items:
            assert item.options != stored[item.id]                  # 顺序真的换了
            assert sorted(item.options) == sorted(stored[item.id])   # 只是换了顺序
        widget.deleteLater()

    def test_can_be_switched_off(self, qapp, tmp_path):
        widget = self._panel(tmp_path, shuffle=False)
        widget.start()
        assert all(item.options == ["甲", "乙"] for item in widget._items)
        widget.deleteLater()

    def test_grading_follows_the_shuffled_positions(self, qapp, tmp_path):
        """打乱之后，正确答案在屏幕上是别的位置——按内容找到它再点，必须判对。"""
        widget = self._panel(tmp_path)
        widget.start()
        question = widget._items[0]
        index = question.options.index("甲")        # 库里"甲"是对的（answer="A"）
        widget._option_widgets[index].setChecked(True)
        widget._on_submit()
        assert "答对了" in widget.feedback.toPlainText()
        widget.deleteLater()

    def test_restart_keeps_the_question_order(self, qapp, tmp_path):
        widget = self._panel(tmp_path)
        widget.start()
        ids = [q.id for q in widget._items]
        widget.restart()
        assert [q.id for q in widget._items] == ids
        widget.deleteLater()

    def test_options_do_not_change_within_a_round(self, qapp, tmp_path):
        """翻回上一题时看到的是同一套顺序，不会晃。"""
        widget = self._panel(tmp_path)
        widget.start()
        first = list(widget._items[0].options)
        widget._option_widgets[0].setChecked(True)
        widget._on_submit()
        widget._next()
        assert widget._items[0].options == first
        widget.deleteLater()

    def test_judge_is_not_shuffled(self, qapp, tmp_path):
        """判断题就是"正确/错误"两个钮，没有可换的。"""
        widget = self._panel(tmp_path, questions=[make_question(1, "judge")])
        select_type(widget, "判断题")
        widget.start()
        assert widget._items[0].options == []                   # 库里的判断题本来就没选项
        assert [w.text() for w in widget._option_widgets] == ["正确", "错误"]
        widget.deleteLater()


class TestIdleHint:
    """打开软件、库里已经有题时，中间那块不能还叫用户去导入。

    真机踩过（打好的包第一次打开）：头上写着"这栏共 279 道"，
    中间却写着"先点上面的「导入文件」…"，看着像题库丢了。
    """

    def test_empty_bank_asks_to_import(self, qapp, tmp_path):
        widget = TempBankPanel(
            Config({"paths": {"temp_bank": str(tmp_path / "bank.db")}}, Path("."))
        )
        assert "导入文件" in widget.feedback.toPlainText()
        widget.deleteLater()

    def test_bank_with_questions_asks_to_start(self, panel):
        """等于关掉软件重开：库里已经有题，提示该是"点开始刷题"。"""
        again = TempBankPanel(panel.config)
        hint = again.feedback.toPlainText()
        assert "开始刷题" in hint
        assert "导入文件" not in hint
        again.deleteLater()
