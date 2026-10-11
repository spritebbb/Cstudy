"""答题面板。

一题一题做：选择题自动判分，填空/阅读/编程题展示参考答案和解析，由用户自己对照。

判分逻辑全在 `cstudy.grading` 里（不依赖 Qt，好单测），这里只做搬运和展示。
面板里任何一处出问题都不能弹 traceback 给用户——他基本不会 Python。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from html import escape
from itertools import zip_longest
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..db import Database
from ..grading import describe_expected, grade_choice, normalize_choice_answer
from ..logging_setup import get_logger
from ..models import Question
from ..runner import RunResult, run_c_code
from .rendering import material_html
from .theme import DARK, Theme

log = get_logger(__name__)

# 不同题型给不同高度的输入框：填空题一行就够，编程题得给足地方
INPUT_HEIGHT = {"fill": 60, "reading": 140, "coding": 220}


class RunCodeWorker(QThread):
    """编译并运行用户写的代码。

    **必须放后台**：编译要一两秒，而代码里要是写了 `while(1)`，
    要等超时（好几秒）才结束——主线程跑的话界面会整个卡住。
    """

    done = pyqtSignal(object)   # RunResult

    def __init__(self, source: str, stdin_text: str) -> None:
        super().__init__()
        self._source = source
        self._stdin = stdin_text

    def run(self) -> None:
        try:
            result = run_c_code(self._source, self._stdin)
        except Exception as exc:
            log.exception("编译运行代码时出了意外")
            result = RunResult(output=f"跑的时候出了点意外：{exc}")
        self.done.emit(result)


@dataclass
class QuizItem:
    """一道题的作答状态。"""

    question: Question
    question_id: int | None = None
    user_answer: str = ""
    is_correct: bool | None = None   # None 表示还没作答
    started_at: float = 0.0


class QuizPanel(QWidget):
    """答题面板。外部只需要调 load() 和接 status_changed 信号。"""

    status_changed = pyqtSignal(str)
    load_all_requested = pyqtSignal()

    def __init__(
        self, db: Database, parent: QWidget | None = None, theme: Theme = DARK
    ) -> None:
        super().__init__(parent)
        self.db = db
        self._theme = theme
        self._items: list[QuizItem] = []
        self._index = 0
        self._answered = False   # 当前这题是否已经提交过
        self._run_worker: RunCodeWorker | None = None
        self._build_ui()
        self._show_empty("还没有题目。先导入一个视频、生成笔记，再点「开始刷题」。")

    # ---------- 搭界面 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.progress_label = QLabel("")
        self.score_label = QLabel("")
        top.addWidget(self.progress_label)
        top.addWidget(self.score_label)
        top.addStretch(1)
        self.load_all_button = QPushButton("从题库挑题")
        self.load_all_button.clicked.connect(self.load_all_requested.emit)
        self.restart_button = QPushButton("重做这批")
        self.restart_button.clicked.connect(self._restart)
        self.retry_wrong_button = QPushButton("只做刚才做错的")
        self.retry_wrong_button.clicked.connect(self._retry_wrong)
        for button in (self.load_all_button, self.restart_button, self.retry_wrong_button):
            top.addWidget(button)
        root.addLayout(top)

        # 题目区放在滚动区域里：编程题的题干加参考答案可能很长
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)

        self.meta_label = QLabel("")
        self.meta_label.setWordWrap(True)
        body_layout.addWidget(self.meta_label)

        self.question_view = QTextBrowser()
        self.question_view.setOpenExternalLinks(False)
        self.question_view.setMinimumHeight(80)
        body_layout.addWidget(self.question_view)

        # 选择题：一堆单选按钮
        self.choice_box = QWidget()
        self.choice_layout = QVBoxLayout(self.choice_box)
        self.choice_layout.setContentsMargins(0, 0, 0, 0)
        self.choice_group = QButtonGroup(self)
        body_layout.addWidget(self.choice_box)

        # 非选择题：一个文本框
        self.answer_edit = QPlainTextEdit()
        self.answer_edit.setPlaceholderText("在这里写你的答案，写完点「提交」")
        body_layout.addWidget(self.answer_edit)

        self.submit_button = QPushButton("提交")
        self.submit_button.clicked.connect(self._submit)
        body_layout.addWidget(self.submit_button)

        body_layout.addWidget(self._build_run_box())

        self.feedback_view = QTextBrowser()
        self.feedback_view.setOpenExternalLinks(False)
        self.feedback_view.hide()
        body_layout.addWidget(self.feedback_view)

        # 非选择题没法自动判分，让用户自己点
        self.self_grade_row = QWidget()
        row = QHBoxLayout(self.self_grade_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("自己对照一下："))
        self.right_button = QPushButton("答对了")
        self.right_button.clicked.connect(lambda: self._self_grade(True))
        self.wrong_button = QPushButton("答错了")
        self.wrong_button.clicked.connect(lambda: self._self_grade(False))
        row.addWidget(self.right_button)
        row.addWidget(self.wrong_button)
        row.addStretch(1)
        self.self_grade_row.hide()
        body_layout.addWidget(self.self_grade_row)

        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.body)
        root.addWidget(scroll, 1)

        bottom = QHBoxLayout()
        self.prev_button = QPushButton("上一题")
        self.prev_button.clicked.connect(lambda: self._go(-1))
        self.next_button = QPushButton("下一题")
        self.next_button.clicked.connect(lambda: self._go(1))
        self.finish_button = QPushButton("看成绩")
        self.finish_button.clicked.connect(self._show_score)
        bottom.addWidget(self.prev_button)
        bottom.addWidget(self.next_button)
        bottom.addStretch(1)
        bottom.addWidget(self.finish_button)
        root.addLayout(bottom)

    # ---------- 编程题：跑一下 ----------

    def _build_run_box(self) -> QWidget:
        """编程题专用的「跑一下」。别的题型下面会把它藏起来。

        为什么要有这个：光"写完对照参考答案"练不出程序设计能力——C 语言恰恰是
        **看着对、编译就报错**的科目（漏分号、少 `&`、数组越界），
        得让编译器来告诉你。这 31 分是软件原来完全帮不上忙的地方。
        """
        self.run_box = QWidget()
        layout = QVBoxLayout(self.run_box)
        layout.setContentsMargins(0, 0, 0, 0)

        hint = QLabel("写完可以先跑一下，编译错误会告诉你在第几行、错在哪。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("程序需要的输入（程序里 scanf 读的那些，没有就留空）："))
        self.stdin_edit = QPlainTextEdit()
        self.stdin_edit.setPlaceholderText('每行一个。比如程序要 scanf("%d",&n); 这里就填 5')
        self.stdin_edit.setMaximumHeight(56)
        layout.addWidget(self.stdin_edit)

        self.run_button = QPushButton("跑一下（编译并运行）")
        self.run_button.clicked.connect(self._run_code)
        layout.addWidget(self.run_button)

        self.run_output = QTextBrowser()
        self.run_output.setOpenExternalLinks(False)
        self.run_output.hide()
        layout.addWidget(self.run_output)

        return self.run_box

    def _run_code(self) -> None:
        source = self.answer_edit.toPlainText().strip()
        if not source:
            QMessageBox.information(self, "还没写代码", "先在上面写点代码，再点「跑一下」。")
            return
        if self._run_worker is not None and self._run_worker.isRunning():
            return

        self.run_button.setEnabled(False)
        self.run_button.setText("正在编译运行…")
        self.run_output.setHtml(material_html("正在编译…"))
        self.run_output.show()

        self._run_worker = RunCodeWorker(source, self.stdin_edit.toPlainText())
        self._run_worker.done.connect(self._on_run_done)
        self._run_worker.start()

    def _on_run_done(self, result: RunResult) -> None:
        self.run_button.setEnabled(True)
        self.run_button.setText("跑一下（编译并运行）")
        self.run_output.setHtml(material_html(result.output))
        self.run_output.show()

    # ---------- 装载题目 ----------

    def load(self, questions: list[Question], question_ids: list[int] | None = None) -> None:
        """装载一批题。questions 和 question_ids 应该一一对应。"""
        ids = list(question_ids or [])
        # 长度对不上时按短的补 None，绝不按下标硬取
        if len(ids) != len(questions):
            log.warning("题目数(%d)和 id 数(%d)对不上，按缺的补空", len(questions), len(ids))
        items = [
            QuizItem(question=question, question_id=qid)
            for question, qid in zip_longest(questions, ids, fillvalue=None)
        ]

        if not items:
            self._show_empty(
                "这次没有题目。多半是开场或导学课，没有可考的 C 语言内容，跳过即可。"
            )
            return

        self._items = items
        self._index = 0
        self._answered = False
        log.info("装载 %d 道题", len(items))
        self._show_current()

    def _show_empty(self, message: str) -> None:
        self._items = []
        self.meta_label.setText("")
        self.question_view.setMarkdown(message)
        self._set_input_visible(choice=False, text=False)
        self._hide_feedback()
        self.submit_button.setEnabled(False)
        self.prev_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.finish_button.setEnabled(False)
        self.restart_button.setEnabled(False)
        self.retry_wrong_button.setEnabled(False)
        self.progress_label.setText("")
        self.score_label.setText("")

    # ---------- 显示当前题 ----------

    @property
    def _current(self) -> QuizItem | None:
        if 0 <= self._index < len(self._items):
            return self._items[self._index]
        return None

    def _show_current(self) -> None:
        item = self._current
        if item is None:
            return

        question = item.question
        self.meta_label.setText(
            f"第 {self._index + 1}/{len(self._items)} 题　·　"
            f"{question.exam_type_label}　·　难度 {question.difficulty}"
            + (f"　·　知识点：{question.point}" if question.point else "")
        )
        self.question_view.setHtml(material_html(question.question))

        self._fill_inputs(question, item)
        self._hide_feedback()
        self.submit_button.setEnabled(True)
        self._update_nav()
        self._update_score_label()
        item.started_at = time.monotonic()

    def _fill_inputs(self, question: Question, item: QuizItem) -> None:
        """按题型摆好作答控件，并把之前答过的内容填回去。"""
        is_choice = question.type == "choice" and len(question.options) >= 2

        if is_choice:
            self._rebuild_choices(question)
            for index, button in enumerate(self.choice_group.buttons()):
                if chr(ord("A") + index) == item.user_answer:
                    button.setChecked(True)
        else:
            self.answer_edit.setPlainText(item.user_answer)
            self.answer_edit.setFixedHeight(INPUT_HEIGHT.get(question.type, 100))

        self._set_input_visible(choice=is_choice, text=not is_choice)
        # 只有程序设计题能跑——程序填空的代码本来就缺一块，跑了必然报错
        self.run_box.setVisible(question.exam_type == "coding")
        self.run_output.hide()

    def _rebuild_choices(self, question: Question) -> None:
        """每题重建单选按钮——选项是随题目变的，不能复用。"""
        for button in self.choice_group.buttons():
            self.choice_group.removeButton(button)
        while self.choice_layout.count():
            widget = self.choice_layout.takeAt(0).widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for option in question.options:
            button = QRadioButton(option, self.choice_box)
            self.choice_group.addButton(button)
            self.choice_layout.addWidget(button)

    def _set_input_visible(self, choice: bool, text: bool) -> None:
        self.choice_box.setVisible(choice)
        self.answer_edit.setVisible(text)
        # 跑代码那块只在程序设计题上显示（由 _show_current 决定）。
        # 这里先把"没答案框"的情况（空状态、成绩页）一并藏掉。
        if not text:
            self.run_box.hide()

    def _hide_feedback(self) -> None:
        self.feedback_view.hide()
        self.feedback_view.setMarkdown("")
        self.self_grade_row.hide()

    # ---------- 提交与判分 ----------

    def _collect_answer(self) -> str:
        item = self._current
        if item is None:
            return ""
        if item.question.type == "choice" and len(item.question.options) >= 2:
            for index, button in enumerate(self.choice_group.buttons()):
                if button.isChecked():
                    return chr(ord("A") + index)
            return ""
        return self.answer_edit.toPlainText().strip()

    def _submit(self) -> None:
        item = self._current
        if item is None or self._answered:
            return

        try:
            answer = self._collect_answer()
        except Exception:
            log.exception("读取作答内容失败")
            self._warn("读取你的答案时出了点问题，请重新填一次。")
            return

        if not answer:
            self._warn("还没作答呢，先选一个选项或者写点东西再提交。")
            return

        item.user_answer = answer
        question = item.question

        if question.type == "choice" and len(question.options) >= 2:
            self._grade_choice_item(item)
        else:
            self._show_reference_answer(item, auto=None)

    def _grade_choice_item(self, item: QuizItem) -> None:
        """选择题：自动判分。判不了就退化成"自己对照"。"""
        options = item.question.options
        index = ord(item.user_answer) - ord("A") if item.user_answer else -1
        result = grade_choice(index, options, item.question.answer)

        if not result.can_auto_grade:
            self._show_reference_answer(item, auto=None, extra=result.note)
            return

        item.is_correct = bool(result.correct)
        self._record(item)
        self._answered = True
        self._show_reference_answer(item, auto=result.correct)

    @staticmethod
    def _reference_text(question: Question) -> str:
        """把参考答案整理成给用户看的样子。

        选择题尽量还原成"B. 乙"——光给个字母，用户还得自己回去数第几个。
        """
        if question.type == "choice" and question.options:
            letter = normalize_choice_answer(question.answer, len(question.options))
            if letter:
                return describe_expected(question.options, letter)
        return question.answer

    def _show_reference_answer(
        self, item: QuizItem, auto: bool | None, extra: str = ""
    ) -> None:
        """展示参考答案和解析。auto 为 None 时让用户自己判对错。"""
        question = item.question
        expected = self._reference_text(question)

        parts: list[str] = []
        if auto is True:
            parts.append(
                f"<h3 style='color:{self._theme.ok}; margin:4px 0;'>✅ 答对了</h3>"
            )
        elif auto is False:
            parts.append(
                f"<h3 style='color:{self._theme.danger}; margin:4px 0;'>❌ 答错了</h3>"
            )
        if extra:
            parts.append(f"<p>{escape(extra)}</p>")

        parts.append("<p style='margin:8px 0 2px 0;'><b>参考答案</b></p>")
        parts.append(material_html(expected))
        if question.explanation:
            parts.append("<p style='margin:8px 0 2px 0;'><b>解析</b></p>")
            parts.append(material_html(question.explanation))

        self.feedback_view.setHtml("".join(parts))
        self.feedback_view.show()

        if auto is None:
            self.self_grade_row.show()
            self.submit_button.setEnabled(False)
        else:
            self.self_grade_row.hide()
            self.submit_button.setEnabled(False)
            self._update_score_label()
            self._update_nav()

    def _self_grade(self, correct: bool) -> None:
        item = self._current
        if item is None:
            return
        item.is_correct = correct
        self._record(item)
        self._answered = True
        self.self_grade_row.hide()
        self._show_reference_answer(item, auto=correct)

    def _record(self, item: QuizItem) -> None:
        """把这次作答记进库。写库失败不能让答题流程断掉。"""
        if item.question_id is None:
            log.warning("这道题没有 id（可能没入库），本次作答只当练习")
            return
        elapsed = int(time.monotonic() - item.started_at) if item.started_at else None
        try:
            self.db.record_answer(
                item.question_id, item.user_answer, bool(item.is_correct), elapsed
            )
        except Exception:
            log.exception("作答记录写库失败")
            self.status_changed.emit("这次作答没能记进数据库，但不影响你继续做题。")

    # ---------- 导航与成绩 ----------

    def _go(self, delta: int) -> None:
        target = self._index + delta
        if not 0 <= target < len(self._items):
            return
        self._index = target
        self._answered = self._items[target].is_correct is not None
        self._show_current()

    def _update_nav(self) -> None:
        self.prev_button.setEnabled(self._index > 0)
        self.next_button.setEnabled(self._index < len(self._items) - 1)
        answered = sum(1 for it in self._items if it.is_correct is not None)
        self.finish_button.setEnabled(bool(self._items) and answered == len(self._items))

    def _update_score_label(self) -> None:
        done = [it for it in self._items if it.is_correct is not None]
        if not done:
            self.score_label.setText("")
            return
        right = sum(1 for it in done if it.is_correct)
        self.score_label.setText(f"已答 {len(done)}，答对 {right}")

    def _show_score(self) -> None:
        total = len(self._items)
        right = sum(1 for it in self._items if it.is_correct is True)
        wrong = sum(1 for it in self._items if it.is_correct is False)
        pending = total - right - wrong

        lines = [f"# 本批 {total} 题", "", f"- 答对：**{right}** 道", f"- 答错：**{wrong}** 道"]
        if pending:
            lines.append(f"- 还没做：{pending} 道")
        if wrong:
            lines.append("")
            lines.append("答错的题已经记进错题本了。可以点上面的「只做刚才做错的」再来一遍。")
        else:
            lines.append("")
            lines.append("全对，厉害。")

        self.question_view.setMarkdown("\n".join(lines))
        self.meta_label.setText("成绩")
        self._set_input_visible(choice=False, text=False)
        self._hide_feedback()
        self.submit_button.setEnabled(False)
        self.status_changed.emit(f"本批 {total} 题，答对 {right}，答错 {wrong}")

    def _restart(self) -> None:
        if not self._items:
            return
        self.load([it.question for it in self._items], [it.question_id for it in self._items])

    def _retry_wrong(self) -> None:
        wrong = [it for it in self._items if it.is_correct is False]
        if not wrong:
            self._warn("这批题里没有做错的，不用重做。")
            return
        self.load([it.question for it in wrong], [it.question_id for it in wrong])

    # ---------- 小工具 ----------

    def _warn(self, message: str) -> None:
        QMessageBox.information(self, "提示", message)
