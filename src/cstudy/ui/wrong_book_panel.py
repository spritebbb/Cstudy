"""错题本：错过哪些题、错了几次、今天该复习哪几道。

数据一直在记（`record_answer()` 早就往 `wrong_questions` 写了），这个面板把它显示出来。
复习安排按遗忘曲线算——见 `cstudy/wrong_book.py`。
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import Config
from ..db import Database
from ..logging_setup import get_logger
from ..models import Question
from ..wrong_book import due_items, intervals_from_config, list_wrong, master_after_from_config

log = get_logger(__name__)

DUE_COLOR = "#c0392b"      # 该复习了，醒目一点
DONE_COLOR = "#7f8c8d"     # 已掌握，退到背景里


class WrongBookPanel(QWidget):
    """错题本。外部只要接 `review_requested` 就能把要复习的题拿去刷。"""

    review_requested = pyqtSignal(list, list)   # list[Question], list[int]

    def __init__(self, db: Database, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._items = []

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        top.addWidget(self.summary_label, stretch=1)

        self.review_button = QPushButton("复习今天该复习的")
        self.review_button.clicked.connect(self._on_review)
        top.addWidget(self.review_button)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)

        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)   # 链接自己处理，别去开浏览器
        self.browser.anchorClicked.connect(self._on_anchor)
        layout.addWidget(self.browser, 1)

        self.refresh()

    # ---------- 数据 ----------

    def refresh(self) -> None:
        try:
            self._items = list_wrong(
                self._db,
                intervals_from_config(self._config),
                master_after_from_config(self._config),
            )
        except Exception:
            log.exception("读错题本失败")
            self.summary_label.setText("读错题本出了点问题，详见 logs/cstudy.log。")
            self.browser.setHtml("")
            self.review_button.setEnabled(False)
            return

        due = due_items(self._items)
        active = [item for item in self._items if not item.mastered]
        self.summary_label.setText(self._summary(len(active), len(due), len(self._items)))
        self.review_button.setEnabled(bool(due))
        self.browser.setHtml(self._render(due))

    @staticmethod
    def _summary(active: int, due: int, total: int) -> str:
        """`active` 是**还欠着的**（没掌握的），已掌握的不算欠。"""
        if not total:
            return "错题本是空的——做错的题会自动记到这里。"
        if not active:
            return f"错题本清空了，掌握的 {total} 道已经归档。"
        if not due:
            return f"还欠着 {active} 道，今天没有该复习的。"
        return f"还欠着 {active} 道，今天该复习 {due} 道。"

    def _render(self, due) -> str:
        if not self._items:
            return (
                "<p>还没有错题。做题时答错的会自动记进来，"
                "隔一段时间提醒你再做一遍。</p>"
            )

        parts: list[str] = []
        if due:
            parts.append(f"<h3>今天该复习（{len(due)} 道）</h3><ul>")
            parts.extend(self._row(item, DUE_COLOR) for item in due)
            parts.append("</ul>")

        waiting = [item for item in self._items if not item.due and not item.mastered]
        if waiting:
            parts.append(f"<h3>还没到复习时间（{len(waiting)} 道）</h3><ul>")
            parts.extend(self._row(item) for item in waiting)
            parts.append("</ul>")

        mastered = [item for item in self._items if item.mastered]
        if mastered:
            parts.append(f"<h3>已掌握（{len(mastered)} 道）</h3><ul>")
            parts.extend(self._row(item, DONE_COLOR) for item in mastered)
            parts.append("</ul>")

        return "".join(parts)

    @staticmethod
    def _row(item, color: str = "") -> str:
        """一道题一行。带个"这个我会了"的链接，点了就移出错题本。"""
        style = f' style="color:{color}"' if color else ""
        stem = _escape(_shorten(item.question.question, 60))
        return (
            f"<li{style}>{stem}<br>"
            f'<small>{_escape(item.describe())}　'
            f'<a href="remove:{item.question_id}">这个我会了</a></small></li>'
        )

    # ---------- 交互 ----------

    def _on_review(self) -> None:
        due = due_items(self._items)
        questions: list[Question] = []
        ids: list[int] = []
        for item in due:
            questions.append(item.question)
            ids.append(item.question_id)
        if questions:
            self.review_requested.emit(questions, ids)

    def _on_anchor(self, url) -> None:
        href = url.toString()
        if not href.startswith("remove:"):
            return
        try:
            question_id = int(href.split(":", 1)[1])
        except ValueError:
            log.warning("错题本里点了一个看不懂的链接：%s", href)
            return
        try:
            self._db.set_mastered(question_id)
        except Exception as exc:
            log.exception("移出错题本失败")
            QMessageBox.warning(self, "没移出去", f"移出错题本时出了点问题：{exc}")
            return
        self.refresh()


def _shorten(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _escape(text: str) -> str:
    """题干里有 `#include <stdio.h>` 这类内容，直接塞进 HTML 会被当成标签。"""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
