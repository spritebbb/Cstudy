"""主窗口。

三个主按钮对应三步流程，每步都能单独重跑：
    导入链接 → 生成笔记 → 开始刷题

耗时操作全部丢给 TaskWorker 在后台跑，界面不会被卡住。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..db import Database
from ..exam import build_exam_paper
from ..logging_setup import get_logger
from ..models import Question, ReviewReport
from ..pipeline import LoadedVideo, NoteResult, QuizResult, StudyPipeline
from .filter_dialog import FilterDialog
from .import_dialog import ImportDialog
from .login_dialog import LoginDialog
from .notes_panel import NotesPanel
from .quiz_panel import QuizPanel
from .stats_panel import StatsPanel
from .syllabus_panel import SyllabusPanel
from .workers import TaskWorker, friendly_error
from .wrong_book_panel import WrongBookPanel

log = get_logger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, db: Database) -> None:
        super().__init__()
        self.config = config
        self.db = db
        self.pipeline = StudyPipeline(config, db)

        self._video: LoadedVideo | None = None
        self._note: NoteResult | None = None
        self._worker: TaskWorker | None = None

        self.setWindowTitle(f"{config.get('app.name', 'C语言备考助手')} · 专升本")
        self.resize(980, 720)

        self._build_ui()
        self._refresh_buttons()
        self._update_stats()

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- 链接输入 ---
        link_row = QHBoxLayout()
        link_row.addWidget(QLabel("视频链接："))
        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText(
            "粘贴 B 站链接或 BV 号，例如 https://www.bilibili.com/video/BV1xx411c7mD?p=1"
        )
        self.link_input.returnPressed.connect(self.on_import)
        link_row.addWidget(self.link_input, stretch=1)

        self.import_button = QPushButton("导入链接")
        self.import_button.setMinimumWidth(100)
        self.import_button.clicked.connect(self.on_import)
        link_row.addWidget(self.import_button)
        root.addLayout(link_row)

        # --- 操作按钮 ---
        action_row = QHBoxLayout()
        self.note_button = QPushButton("生成笔记")
        self.note_button.clicked.connect(self.on_make_notes)
        action_row.addWidget(self.note_button)

        self.quiz_button = QPushButton("开始刷题")
        self.quiz_button.clicked.connect(self.on_make_quiz)
        action_row.addWidget(self.quiz_button)

        self.exam_button = QPushButton("组一套真题")
        self.exam_button.clicked.connect(self.on_build_exam)
        action_row.addWidget(self.exam_button)

        self.login_button = QPushButton("B 站扫码登录")
        self.login_button.clicked.connect(self.on_login)
        action_row.addWidget(self.login_button)

        self.import_bank_button = QPushButton("导入题库")
        self.import_bank_button.clicked.connect(self.on_import_bank)
        action_row.addWidget(self.import_bank_button)

        action_row.addStretch(1)

        self.open_notes_button = QPushButton("打开笔记文件夹")
        self.open_notes_button.clicked.connect(self.on_open_notes)
        action_row.addWidget(self.open_notes_button)
        root.addLayout(action_row)

        # --- 内容区 ---
        self.tabs = QTabWidget()

        self.progress_view = QPlainTextEdit()
        self.progress_view.setReadOnly(True)
        self.tabs.addTab(self.progress_view, "进度")

        self.notes_panel = NotesPanel(self.db)
        self.tabs.addTab(self.notes_panel, "笔记")

        # 答题区。判分和展示都在 QuizPanel 里，主窗口只负责把题喂给它。
        self.quiz_panel = QuizPanel(self.db)
        self.quiz_panel.status_changed.connect(self._set_status)
        self.quiz_panel.load_all_requested.connect(self.on_load_all_questions)
        self.tabs.addTab(self.quiz_panel, "答题")

        # 考纲覆盖度：哪些知识点还没题。导入新题后要 refresh 一次。
        self.syllabus_panel = SyllabusPanel(self.db)
        self.tabs.addTab(self.syllabus_panel, "考纲")

        # 错题本：错过的题 + 今天该复习哪几道。
        self.wrong_book_panel = WrongBookPanel(self.db, self.config)
        self.wrong_book_panel.review_requested.connect(self._on_review_wrong)
        self.tabs.addTab(self.wrong_book_panel, "错题本")

        # 学情：这几周练得怎么样。备考六个月，看趋势比看单次分数重要。
        self.stats_panel = StatsPanel(self.db)
        self.tabs.addTab(self.stats_panel, "学情")

        # 切到某个 tab 时顺手刷新它——省得用户看到过期数据
        self.tabs.currentChanged.connect(self._on_tab_changed)

        root.addWidget(self.tabs, stretch=1)

        # --- 状态栏 ---
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, stretch=1)
        self.stats_label = QLabel("")
        self.statusBar().addPermanentWidget(self.stats_label)

    # ---------- 状态与按钮 ----------

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _refresh_buttons(self) -> None:
        busy = self._busy()
        self.import_button.setEnabled(not busy)
        self.note_button.setEnabled(not busy and self._video is not None)
        self.quiz_button.setEnabled(not busy and self._note is not None)
        self.exam_button.setEnabled(not busy)
        self.login_button.setEnabled(not busy)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self._log(text)

    def _log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.progress_view.appendPlainText(f"[{stamp}] {text}")

    def _update_stats(self) -> None:
        try:
            stats = self.db.stats()
        except Exception:
            log.debug("统计数据库失败", exc_info=True)
            self.stats_label.setText("")
            return
        summary = "  ".join(f"{k} {v}" for k, v in stats.items())
        self.stats_label.setText(summary)
        # 题目数变了，考纲覆盖度也跟着变。放这儿是为了让"哪次导入/出题之后忘了刷新"
        # 这种问题不可能发生——凡是要更新统计的地方，覆盖度一定一起更新。
        self.syllabus_panel.refresh()

    # ---------- 后台任务通用入口 ----------

    def _start_task(self, func, on_done, *args) -> None:
        if self._busy():
            QMessageBox.information(self, "正在忙", "上一个任务还没跑完，请稍等一下。")
            return

        self.tabs.setCurrentWidget(self.progress_view)
        self._worker = TaskWorker(func, *args)
        self._worker.progress.connect(self._log)
        self._worker.finished_ok.connect(on_done)
        self._worker.failed.connect(self._on_task_failed)
        self._worker.finished.connect(self._on_task_finished)
        self._refresh_buttons()
        self._worker.start()

    def _on_task_finished(self) -> None:
        self._refresh_buttons()
        self._update_stats()

    def _on_task_failed(self, message: str) -> None:
        self._log("失败：" + message.replace("\n", " "))
        self._set_status("任务失败")
        QMessageBox.warning(self, "出错了", message)

    # ---------- 三个主流程 ----------

    def on_import(self) -> None:
        link = self.link_input.text().strip()
        if not link:
            QMessageBox.information(self, "提示", "请先粘贴 B 站视频链接或 BV 号。")
            return
        self._video = None
        self._note = None
        self._set_status("正在导入…")
        self._start_task(self.pipeline.load, self._on_import_done, link)

    def _on_import_done(self, video: LoadedVideo) -> None:
        self._video = video
        self._set_status(f"已导入：{video.title}（字幕 {len(video.segments)} 条，{video.subtitle_label}）")
        self._refresh_buttons()

    def on_make_notes(self) -> None:
        if not self._video:
            return
        self._set_status("正在生成笔记…")
        self._start_task(self.pipeline.make_notes, self._on_notes_done, self._video)

    def _on_notes_done(self, note: NoteResult) -> None:
        self._note = note
        self.notes_panel.show_note(note.body)
        self.tabs.setCurrentWidget(self.notes_panel)
        self._set_status(f"笔记已生成：{note.path.name}")
        self._log_review(note.review)
        self._refresh_buttons()

    def _log_review(self, report: ReviewReport) -> None:
        """把复查报告写进进度区。

        用户要看得见 AI 背着他改了什么——不然复查就成了个黑箱。
        """
        text = report.describe()
        if text:
            self._log(text)

    def on_make_quiz(self) -> None:
        if not self._note or not self._video:
            return
        source = f"video:{self._video.bvid}#p{self._video.page}"
        self._set_status("正在出题…")
        self._start_task(
            self.pipeline.make_quiz,
            self._on_quiz_done,
            self._note.note_id,
            self._note.body,
            source,
        )

    def _on_quiz_done(self, result: QuizResult) -> None:
        self.quiz_panel.load(result.questions, result.question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(f"已生成 {result.summary}")
        self._log_review(result.review)
        self._refresh_buttons()

    def on_load_all_questions(self) -> None:
        """从题库里挑题来刷——先弹筛选框选条件，选完再加载。

        题库一千多道，全塞进一个队列没法用，所以这里默认也要过一遍筛选。
        对话框里默认全选，直接回车就是"全刷"。
        """
        dialog = FilterDialog(self.db, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            rows = self.db.query_questions(**dialog.criteria())
        except Exception:
            log.exception("按条件读题目失败")
            QMessageBox.warning(
                self, "读不出来", "读题目时出了点问题，详细原因在 logs/cstudy.log。"
            )
            return

        questions, question_ids = self._to_questions(rows)
        if not questions:
            QMessageBox.information(
                self, "没有符合条件的题", "条件太窄了，放宽一点再试一次。"
            )
            return

        self.quiz_panel.load(questions, question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(f"挑了 {len(questions)} 道题来刷")

    def on_build_exam(self) -> None:
        """按考试题型分值从题库里组一套卷子，直接进刷题界面。

        不调 AI——只是从已有的题里按比例随机抽。
        """
        try:
            paper = build_exam_paper(self.db, self.config)
        except Exception as exc:
            log.exception("组卷失败")
            QMessageBox.warning(self, "组卷失败", f"组卷时出了点问题：{friendly_error(exc)}")
            return

        if not paper.questions:
            QMessageBox.information(
                self,
                "题库里还没有题",
                "题库是空的。先「导入题库」，或者生成一批题，再来组卷。",
            )
            return

        self.quiz_panel.load(paper.questions, paper.question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(paper.summary)

    def _to_questions(self, rows) -> tuple[list[Question], list[int]]:
        """把数据库的行转成 Question 列表。读不出来的那条跳过，不连累其他题。"""
        questions: list[Question] = []
        question_ids: list[int] = []
        for row in rows:
            try:
                questions.append(Question.from_row(row))
                question_ids.append(int(row["id"]))
            except Exception:
                log.warning("跳过一条读不出来的题目：id=%s", row["id"], exc_info=True)
        return questions, question_ids

    def _on_review_wrong(self, questions: list[Question], question_ids: list[int]) -> None:
        """错题本里点「复习今天该复习的」——直接进答题区。"""
        self.quiz_panel.load(questions, question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(f"从错题本拿了 {len(questions)} 道题来复习")

    def _on_tab_changed(self, index: int) -> None:
        """切到某个 tab 时刷新它——做题、导入都会让这些页的数据变化。"""
        widget = self.tabs.widget(index)
        if widget is self.wrong_book_panel:
            self.wrong_book_panel.refresh()
        elif widget is self.syllabus_panel:
            self.syllabus_panel.refresh()
        elif widget is self.stats_panel:
            self.stats_panel.refresh()
        elif widget is self.notes_panel:
            self.notes_panel.refresh()

    # ---------- 辅助操作 ----------

    def on_login(self) -> None:
        dialog = LoginDialog(self.config, self)
        dialog.exec()
        if self._note is None:
            self._set_status("登录窗口已关闭")

    def on_import_bank(self) -> None:
        """导入现成题库：粘贴一段题目文字，AI 整理后入库。"""
        dialog = ImportDialog(self.config, self.db, self.pipeline.client, self)
        dialog.saved.connect(self._on_bank_imported)
        dialog.exec()

    def _on_bank_imported(self, count: int) -> None:
        self._set_status(f"题库新增 {count} 道题")
        self._update_stats()
        # 题库变了，考纲覆盖度跟着变。这里**不**顺手加载进答题区——
        # 那会弹一个筛选框，用户刚导完题就被打断，很烦。
        self.syllabus_panel.refresh()

    def on_open_notes(self) -> None:
        notes_dir = self.config.path("notes")
        notes_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(notes_dir)  # Windows 专有，用资源管理器打开
        except OSError as exc:
            QMessageBox.warning(self, "打不开", f"无法打开文件夹：{exc}")

    def closeEvent(self, event) -> None:
        if self._busy():
            answer = QMessageBox.question(
                self,
                "还有任务在跑",
                "后台还有任务正在执行，确定要退出吗？未完成的任务会中断。",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # 网络请求可能卡住不返回，等一小会儿，还不停就强制结束
            if not self._worker.wait(5000):
                log.warning("后台任务未在 5 秒内退出，强制结束")
                self._worker.terminate()
                self._worker.wait(2000)
        self.db.close()
        super().closeEvent(event)
