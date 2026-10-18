"""主窗口。

三个主按钮对应三步流程，每步都能单独重跑：
    导入链接 → 生成笔记 → 开始刷题

耗时操作全部丢给 TaskWorker 在后台跑，界面不会被卡住。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
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
from .background import BackgroundWidget
from .filter_dialog import FilterDialog
from .graph_panel import KnowledgeGraphPanel
from .icons import themed_icon
from .import_dialog import ImportDialog
from .login_dialog import LoginDialog
from .notes_panel import NotesPanel
from .quiz_panel import QuizPanel
from .stats_panel import StatsPanel
from .syllabus_panel import SyllabusPanel
from .temp_panel import TempBankPanel
from .theme import Appearance, load_appearance, set_variant
from .workers import TaskWorker, friendly_error
from .wrong_book_panel import WrongBookPanel

log = get_logger(__name__)

# 页签图标，顺序跟 addTab 一致。图标本身在 ui/icons.py 里（内联 SVG，跟着主题换色）。
_TAB_ICONS = ("progress", "notes", "quiz", "syllabus", "graph", "wrong", "stats", "sprint")


class MainWindow(QMainWindow):
    def __init__(
        self, config: Config, db: Database, appearance: Appearance | None = None
    ) -> None:
        super().__init__()
        self.config = config
        self.db = db
        self.pipeline = StudyPipeline(config, db)
        # 不传就自己从 config 读——测试里可以塞一个指定的外观进来
        self.appearance = appearance or load_appearance(config)

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
        # 背景层垫在最底下：纯色，或者一张压暗的背景图（见 ui/background.py）
        central = BackgroundWidget(self.appearance)
        self.setCentralWidget(central)
        # 外面一层横排：左边导航栏，右边内容区。
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        right = QWidget()
        root = QVBoxLayout(right)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(10)
        outer.addWidget(right, stretch=1)

        # --- 顶部工具条 ---
        # 第一版是七个按钮平铺两行，没有任何主次，用户得先读完才知道从哪下手。
        # 现在收进一张卡片：第一行只说一件事（粘链接 → 导入），
        # 第二行是其余操作并分成"内容 / 账号与题库"两组，
        # 主流程按钮会按"现在轮到哪一步"换样式（见 _refresh_buttons）。
        header = QFrame()
        header.setObjectName("card")
        head = QVBoxLayout(header)
        head.setContentsMargins(12, 10, 12, 10)
        head.setSpacing(9)

        # --- 链接输入 ---
        link_row = QHBoxLayout()
        link_row.setSpacing(8)
        link_caption = QLabel("视频链接：")
        link_caption.setObjectName("pageHint")
        link_row.addWidget(link_caption)

        self.link_input = QLineEdit()
        self.link_input.setPlaceholderText(
            "粘贴 B 站链接或 BV 号，例如 https://www.bilibili.com/video/BV1xx411c7mD?p=1"
        )
        self.link_input.returnPressed.connect(self.on_import)
        link_row.addWidget(self.link_input, stretch=1)

        self.import_button = QPushButton("导入链接")
        self.import_button.setMinimumWidth(104)
        set_variant(self.import_button, "primary")
        self.import_button.clicked.connect(self.on_import)
        link_row.addWidget(self.import_button)
        head.addLayout(link_row)

        # --- 其他操作 ---
        action_row = QHBoxLayout()
        action_row.setSpacing(6)

        self.note_button = QPushButton("生成笔记")
        self.note_button.clicked.connect(self.on_make_notes)
        action_row.addWidget(self.note_button)

        self.quiz_button = QPushButton("开始刷题")
        self.quiz_button.clicked.connect(self.on_make_quiz)
        action_row.addWidget(self.quiz_button)

        self.exam_button = QPushButton("组一套真题")
        self.exam_button.clicked.connect(self.on_build_exam)
        action_row.addWidget(self.exam_button)

        # 两组之间一条细竖线：一边是"出内容"，一边是"账号和题库"。
        # 光靠间距分不开组，加条线一眼就看明白了。
        action_row.addWidget(self._rule())
        action_row.addSpacing(2)

        self.login_button = QPushButton("B 站扫码登录")
        self.login_button.clicked.connect(self.on_login)
        action_row.addWidget(self.login_button)

        self.import_bank_button = QPushButton("导入题库")
        self.import_bank_button.clicked.connect(self.on_import_bank)
        action_row.addWidget(self.import_bank_button)

        action_row.addStretch(1)

        # 打开文件夹是个"顺手的辅助动作"，做成无边框样式，别跟主操作抢注意力
        self.open_notes_button = QPushButton("打开笔记文件夹")
        self.open_notes_button.clicked.connect(self.on_open_notes)
        set_variant(self.open_notes_button, "ghost")
        action_row.addWidget(self.open_notes_button)
        head.addLayout(action_row)
        root.addWidget(header)

        # --- 内容区 ---
        self.tabs = QTabWidget()
        self.tabs.setIconSize(QSize(17, 17))
        self.tabs.setDocumentMode(False)

        self.progress_view = QPlainTextEdit()
        self.progress_view.setReadOnly(True)
        # 这一页平时是空的（只有导入/生成时才写日志），空白的整页看着像坏了。
        # 占位提示只在没内容时显示，有日志了自己会消失。
        self.progress_view.setPlaceholderText(
            "这里显示导入、抓字幕、生成笔记、出题的进展。\n"
            "把视频链接贴到上面，点「导入链接」开始。"
        )
        self.tabs.addTab(self.progress_view, "进度")

        self.notes_panel = NotesPanel(self.db)
        self.tabs.addTab(self.notes_panel, "笔记")

        # 答题区。判分和展示都在 QuizPanel 里，主窗口只负责把题喂给它。
        self.quiz_panel = QuizPanel(self.db, theme=self.appearance.theme)
        self.quiz_panel.status_changed.connect(self._set_status)
        self.quiz_panel.load_all_requested.connect(self.on_load_all_questions)
        self.tabs.addTab(self.quiz_panel, "答题")

        # 考纲覆盖度：哪些知识点还没题。导入新题后要 refresh 一次。
        self.syllabus_panel = SyllabusPanel(self.db, theme=self.appearance.theme)
        self.tabs.addTab(self.syllabus_panel, "考纲")

        # 知识图谱：把考纲的知识点画成一张网——题量、我的正确率、知识点之间的关系。
        # 它调 AI 找关系，所以要把 client 传进去（跟导入题库一个路子）。
        self.graph_panel = KnowledgeGraphPanel(
            self.db, self.config, self.pipeline.client, theme=self.appearance.theme
        )
        self.graph_panel.practice_requested.connect(self._on_practice_point)
        self.graph_panel.status_changed.connect(self._set_status)
        self.tabs.addTab(self.graph_panel, "知识图谱")

        # 错题本：错过的题 + 今天该复习哪几道。
        self.wrong_book_panel = WrongBookPanel(self.db, self.config, theme=self.appearance.theme)
        self.wrong_book_panel.review_requested.connect(self._on_review_wrong)
        self.tabs.addTab(self.wrong_book_panel, "错题本")

        # 学情：这几周练得怎么样。备考六个月，看趋势比看单次分数重要。
        self.stats_panel = StatsPanel(self.db, theme=self.appearance.theme)
        self.tabs.addTab(self.stats_panel, "学情")

        # 考前突击：临时加的一栏，题存在**另一个库文件**里
        # （config 的 paths.temp_bank），跟上面这些互不影响。
        # 它不参与 _update_stats()，也不进错题本和学情。
        # 考完想清干净：删掉 data/temp_bank/ 这个文件夹，再删掉下面这两行。
        self.temp_panel = TempBankPanel(self.config, theme=self.appearance.theme)
        self.tabs.addTab(self.temp_panel, self.config.get("temp_bank.tab_name", "考前突击"))

        # 切到某个 tab 时顺手刷新它——省得用户看到过期数据
        self.tabs.currentChanged.connect(self._on_tab_changed)

        root.addWidget(self.tabs, stretch=1)

        # --- 左侧导航栏 ---
        # 八个页签横排在顶上要占一整行高度，窗口一窄还会挤成两行；竖起来之后
        # 宽度固定、高度跟着页数走，长文页面能多出 40px。
        # 做法是**把真正的 tabBar 藏起来**，另用一份列表当导航：切换逻辑、以及
        # 测试里对 self.tabs 的各种用法（setCurrentWidget / widget(i)）全都不变。
        # 试过直接 setTabPosition(West)：Qt 会把页签文字竖排，中文挤成一列，难认。
        self.tabs.tabBar().hide()
        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(158)
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        for index in range(self.tabs.count()):
            item = QListWidgetItem(self.tabs.tabText(index))
            item.setSizeHint(QSize(140, 42))
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        outer.insertWidget(0, self.nav)

        self._sync_tab_icons()

        # --- 状态栏 ---
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, stretch=1)
        self.stats_label = QLabel("")
        self.statusBar().addPermanentWidget(self.stats_label)

    def _rule(self) -> QFrame:
        """两个按钮组之间的细分隔线。"""
        line = QFrame()
        line.setObjectName("vline")
        line.setFixedWidth(1)
        return line

    def _on_nav_changed(self, row: int) -> None:
        """点左边导航栏 → 切页。"""
        if 0 <= row < self.tabs.count() and row != self.tabs.currentIndex():
            self.tabs.setCurrentIndex(row)

    def _sync_tab_icons(self) -> None:
        """给导航栏每项配图标，当前那页用强调色。

        QIcon 本身有 `Selected` 模式，但不同系统样式下不一定会用到它，
        所以选中的图标干脆**直接画成强调色**再装上去——这样任何样式下都对。
        代价是每次切页要重画 8 个小图标，这点开销可以忽略。
        """
        theme = self.appearance.theme
        current = self.tabs.currentIndex()
        count = min(self.tabs.count(), len(_TAB_ICONS))
        for index in range(count):
            color = theme.accent if index == current else None
            self.tabs.setTabIcon(
                index, themed_icon(_TAB_ICONS[index], theme, size=17, color=color)
            )
            if index < self.nav.count():
                self.nav.item(index).setIcon(
                    themed_icon(_TAB_ICONS[index], theme, size=18, color=color)
                )
        if self.nav.currentRow() != current:
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(current)
            self.nav.blockSignals(False)

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

        # "下一步该点哪个"直接长在按钮上：能点的下一步是实心强调色，别的都是次级。
        # 比在状态栏写一句"请先导入视频"更省事——用户眼睛扫一眼就知道吃哪颗糖。
        if self._note is not None:
            step = self.quiz_button
        elif self._video is not None:
            step = self.note_button
        else:
            step = None
        for button in (self.note_button, self.quiz_button):
            active = button is step and button.isEnabled()
            set_variant(button, "primary" if active else "")

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
        # 用 · 分隔：原来是两个空格，一长串数字糊在一起，看不出哪段是哪项
        summary = "  ·  ".join(f"{k} {v}" for k, v in stats.items())
        self.stats_label.setText(summary)
        # 题目数变了，考纲覆盖度也跟着变。放这儿是为了让"哪次导入/出题之后忘了刷新"
        # 这种问题不可能发生——凡是要更新统计的地方，覆盖度一定一起更新。
        self.syllabus_panel.refresh()
        # 知识图谱也一样：题目数和答题正确率都会进这张图。
        # 图谱自己会判断"结构变了才重排"，所以这里调它不会让图跳位置。
        self.graph_panel.refresh()

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

    def _on_practice_point(self, point_id: int) -> None:
        """知识图谱里点「只练这个知识点」——把这个知识点的题捞出来进答题区。

        用的就是筛选刷题那套（`query_questions(point_ids=...)`），
        只是条件由图上点哪个决定，用户不用再去弹窗里选一遍。
        """
        try:
            rows = self.db.query_questions(point_ids=[point_id])
        except Exception:
            log.exception("按知识点读题目失败")
            QMessageBox.warning(
                self, "读不出来", "读题目时出了点问题，详细原因在 logs/cstudy.log。"
            )
            return

        questions, question_ids = self._to_questions(rows)
        if not questions:
            QMessageBox.information(
                self, "这个知识点还没题", "它现在还一道题都没有，先用补题脚本补一批。"
            )
            return

        self.quiz_panel.load(questions, question_ids)
        self.tabs.setCurrentWidget(self.quiz_panel)
        self._set_status(f"从知识图谱拿了 {len(questions)} 道题来练")

    def _on_tab_changed(self, index: int) -> None:
        """切到某个 tab 时刷新它——做题、导入都会让这些页的数据变化。"""
        self._sync_tab_icons()
        widget = self.tabs.widget(index)
        if widget is self.wrong_book_panel:
            self.wrong_book_panel.refresh()
        elif widget is self.syllabus_panel:
            self.syllabus_panel.refresh()
        elif widget is self.graph_panel:
            self.graph_panel.refresh()
        elif widget is self.stats_panel:
            self.stats_panel.refresh()
        elif widget is self.notes_panel:
            self.notes_panel.refresh()
        elif widget is self.temp_panel:
            # 这一栏要用键盘刷题（数字键选选项、Enter 提交）：
            # 切过来就把焦点给它，不然按了键没人理。
            self.temp_panel.setFocus()

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
        self.temp_panel.shutdown()   # 临时栏也可能正踩着后台线程在读题库文件
        self.db.close()
        super().closeEvent(event)
