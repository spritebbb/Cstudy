"""笔记页：能搜、能翻。

原来这里只能显示"刚生成的那一篇"，关掉软件就找不回来了（只能去 notes/ 文件夹翻
一堆 .md）。视频一百多集的时候，"哪一集讲过位运算"是个天天要问的问题。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..db import Database, parse_db_time
from ..logging_setup import get_logger
from .rendering import render_markdown, style_document

log = get_logger(__name__)

LIST_HEIGHT = 150


class NotesPanel(QWidget):
    """左边搜、上面列、下面看。"""

    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._db = db

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜笔记：标题和正文都搜，比如「位运算」")
        self.search_edit.textChanged.connect(self.refresh)
        layout.addWidget(self.search_edit)

        self.count_label = QLabel("")
        self.count_label.setObjectName("pageHint")
        layout.addWidget(self.count_label)

        self.note_list = QListWidget()
        self.note_list.setMaximumHeight(LIST_HEIGHT)
        self.note_list.currentItemChanged.connect(self._on_picked)
        layout.addWidget(self.note_list)

        self.content_view = QTextBrowser()
        self.content_view.setOpenExternalLinks(True)
        # 笔记是要拿来读长文的：留白给足、行距放松、标题分层次。
        # 这几项 QSS 管不到（文档内部的排版是 QTextDocument 的事）。
        style_document(self.content_view, margin=24)
        layout.addWidget(self.content_view, 1)

        self.refresh()

    # ---------- 列表 ----------

    def refresh(self) -> None:
        """按搜索框里的词重新列一遍。"""
        try:
            rows = self._db.search_notes(self.search_edit.text())
        except Exception:
            log.exception("搜笔记失败")
            self.count_label.setText("读笔记出了点问题，详见 logs/cstudy.log。")
            return

        self.note_list.blockSignals(True)
        self.note_list.clear()
        for row in rows:
            when = parse_db_time(row["created_at"])
            stamp = when.strftime("%m-%d") if when else ""
            title = str(row["title"] or "（没标题）")
            item = QListWidgetItem(f"{stamp}  {title[:60]}")
            item.setData(Qt.ItemDataRole.UserRole, int(row["id"]))
            self.note_list.addItem(item)
        self.note_list.blockSignals(False)

        text = self.search_edit.text().strip()
        if not rows:
            self.count_label.setText(
                f"没搜到含「{text}」的笔记。" if text else "还没有笔记。"
            )
        else:
            self.count_label.setText(
                f"找到 {len(rows)} 篇" + (f"（含「{text}」）" if text else "")
            )

        # 搜完自动选中第一篇，省得还要点一下
        if rows:
            self.note_list.setCurrentRow(0)
        else:
            self.content_view.setHtml("")

    def _on_picked(self, current, _previous=None) -> None:
        if current is None:
            return
        note_id = current.data(Qt.ItemDataRole.UserRole)
        try:
            row = self._db.get_note(int(note_id))
        except Exception:
            log.exception("读笔记内容失败")
            return
        if row is None:
            self.content_view.setHtml("<p>这篇笔记找不到了。</p>")
            return
        render_markdown(self.content_view, str(row["content"] or ""))

    # ---------- 刚生成的笔记 ----------

    def show_note(self, markdown: str, title: str = "") -> None:
        """直接显示一篇笔记（刚生成完那条路走这儿）。"""
        self.refresh()
        if markdown:
            render_markdown(self.content_view, markdown)
