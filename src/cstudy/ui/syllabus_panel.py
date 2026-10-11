"""考纲覆盖度：每个知识点已经有多少道题，哪些还一道都没有。

用户复习时最想知道的就是"我哪里还没练到"。所以**题数为 0 的知识点必须显眼**，
不能只是静静地列在那里。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QTextBrowser, QVBoxLayout, QWidget

from ..db import Database
from ..logging_setup import get_logger
from .theme import DARK, Theme

log = get_logger(__name__)


class SyllabusPanel(QWidget):
    """按考纲章节列出每个知识点有多少道题。"""

    def __init__(self, db: Database, parent: QWidget | None = None, theme: Theme = DARK) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme

        layout = QVBoxLayout(self)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        layout.addWidget(self.browser, 1)

        self.refresh()

    def refresh(self) -> None:
        """重新算一遍覆盖度。导入了新题之后要再调一次。"""
        try:
            rows = self._db.coverage()
        except Exception:
            log.exception("读考纲覆盖度失败")
            self.summary_label.setText("读考纲数据出了点问题，详见 logs/cstudy.log。")
            self.browser.setHtml("")
            return

        if not rows:
            self.summary_label.setText("还没有考纲数据。")
            self.browser.setHtml(
                "<p>先跑一次 <code>scripts/seed_syllabus.py</code> 把考纲灌进来。</p>"
            )
            return

        empty = sum(1 for row in rows if row["question_count"] == 0)
        self.summary_label.setText(
            f"考纲共 {len(rows)} 个知识点，其中 {empty} 个还没有题。"
            if empty
            else f"考纲共 {len(rows)} 个知识点，每个都有题了。"
        )
        self.browser.setHtml(self._render(rows))

    def _render(self, rows) -> str:
        parts: list[str] = []
        chapter = None
        for row in rows:
            if row["chapter"] != chapter:
                if chapter is not None:
                    parts.append("</ul>")
                chapter = row["chapter"]
                parts.append(f"<h3>{_escape(chapter)}</h3><ul>")
            count = int(row["question_count"])
            if count:
                parts.append(f"<li>{_escape(row['name'])} — <b>{count}</b> 道</li>")
            else:
                parts.append(
                    f'<li><span style="color:{self._theme.danger}">'
                    f"{_escape(row['name'])} — 还没有题</span></li>"
                )
        if chapter is not None:
            parts.append("</ul>")
        return "".join(parts)


def _escape(text) -> str:
    """考纲里有 `#include`、`<` 这类字符，直接塞进 HTML 会被当成标签。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
