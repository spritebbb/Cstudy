"""考纲覆盖度：每个知识点已经有多少道题，哪些还一道都没有。

用户复习时最想知道的就是"我哪里还没练到"。所以**题数为 0 的知识点必须显眼**，
不能只是静静地列在那里。

版式上做了两件事：
1. 章节标题做成带底色的条，让 10 个章之间有明确的段落感 —— 原来全是
   一样的 `<h3>`，从头翻到尾像一张没有页眉的长表。
2. 每个知识点变成「名字 …… 题量」的两列表格，题量靠右对齐。这样扫一眼
   就能比大小；用圆点列表时数字夹在文字后面，64 行看下来眼睛是花的。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QLabel, QTextBrowser, QVBoxLayout, QWidget

from ..db import Database
from ..logging_setup import get_logger
from .rendering import render_html, style_document
from .theme import DARK, Theme

log = get_logger(__name__)

# 题量低于这个数的知识点标黄：「有题」但只有一两道，练不到手
THIN_COVERAGE = 3


class SyllabusPanel(QWidget):
    """按考纲章节列出每个知识点有多少道题。"""

    def __init__(self, db: Database, parent: QWidget | None = None, theme: Theme = DARK) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("pageTitle")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        style_document(self.browser, margin=16)
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
            render_html(
                self.browser,
                "<p>先跑一次 <code>scripts/seed_syllabus.py</code> 把考纲灌进来。</p>",
            )
            return

        empty = sum(1 for row in rows if row["question_count"] == 0)
        self.summary_label.setText(
            f"考纲共 {len(rows)} 个知识点，其中 {empty} 个还没有题。"
            if empty
            else f"考纲共 {len(rows)} 个知识点，每个都有题了。"
        )
        render_html(self.browser, self._render(rows))

    def _render(self, rows) -> str:
        theme = self._theme
        parts: list[str] = []
        chapter = None
        cells: list[str] = []

        def flush() -> None:
            if cells:
                parts.append(
                    '<table width="100%" cellspacing="0" cellpadding="0">'
                    + "".join(cells)
                    + "</table>"
                )
                cells.clear()

        for row in rows:
            if row["chapter"] != chapter:
                flush()
                chapter = row["chapter"]
                parts.append(
                    '<p style="background-color:%s; font-size:11.5pt; font-weight:600;'
                    ' margin:18px 0 4px 0;">&nbsp;%s</p>'
                    % (theme.accent_soft, _escape(chapter))
                )
            count = int(row["question_count"])
            if count == 0:
                # 一道都没有：整行标红，这是「该去补题」的信号
                badge = f'<span style="color:{theme.danger}; font-weight:600;">还没有题</span>'
            elif count < THIN_COVERAGE:
                badge = f'<span style="color:{theme.warn};">{count} 道（偏少）</span>'
            else:
                badge = f'<span style="color:{theme.muted};">{count} 道</span>'
            name_color = theme.text if count else theme.danger
            cells.append(
                "<tr>"
                f'<td style="padding:1px 16px 1px 0;">'
                f'<span style="color:{name_color};">{_escape(row["name"])}</span></td>'
                f'<td align="right" style="padding:1px 0; font-size:9.5pt;">{badge}</td>'
                "</tr>"
            )
        flush()
        return "".join(parts)


def _escape(text) -> str:
    """考纲里有 `#include`、`<` 这类字符，直接塞进 HTML 会被当成标签。"""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
