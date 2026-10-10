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
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..db import Database
from ..logging_setup import get_logger
from ..models import Question, ReviewReport
from ..pipeline import LoadedVideo, NoteResult, QuizResult, StudyPipeline
from .import_dialog import ImportDialog
from .login_dialog import LoginDialog
from .quiz_panel import QuizPanel
from .workers import TaskWorker

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

        self.note_view = QTextBrowser()
        self.note_view.setOpenExternalLinks(True)
        self.tabs.addTab(self.note_view, "笔记")

        # 答题区。判分和展示都在 QuizPanel 里，主窗口只负责把题喂给它。
        self.quiz_panel = QuizPanel(self.db)
        self.quiz_panel.status_changed.connect(self._set_status)
        self.quiz_panel.load_all_requested.connect(self.on_load_all_questions)
        self.tabs.addTab(self.quiz_panel, "答题")

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
        self.note_view.setMarkdown(note.body)
        self.tabs.setCurrentWidget(self.note_view)
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
        """从题库里把所有题目捞出来刷——关掉软件重开之后也能接着刷。"""
        try:
            rows = self.db.list_questions()
        except Exception:
            log.exception("从题库读题目失败")
            QMessageBox.warning(
                self, "读不出来", "从题库读题目时出了点问题，详细原因在 logs/cstudy.log。"
            )
            return

        questions: list[Question] = []
        question_ids: list[int] = []
        for row in rows:
            try:
                questions.append(Question.from_row(row))
                question_ids.append(int(row["id"]))
            except Exception:
                # 一条坏数据不该让整批题都加载不了
                log.warning("跳过一条读不出来的题目：id=%s", row["id"], exc_info=True)

        if not questions:
            self._set_status("题库里还没有题目，先导入视频生成一批吧。")
            return

        self.quiz_panel.load(questions, question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(f"从题库加载了 {len(questions)} 道题")

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
        # 顺手加载进答题区，省得用户再点一次
        self.on_load_all_questions()

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
