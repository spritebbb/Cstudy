"""学情统计：最近几周练得怎么样。

`answers` 表一直在记，但界面上只有"本批答对 X"，关掉就没了。备考六个月，
**看趋势比看单次分数重要**——这页就是给这个用的。

排版为什么不用字符画了：原来每周一条 `████░░░░`，靠等宽字体对齐，
一换字体、一换主题就错位，而且颜色是死的（跟不住主题）。
现在用 rich text 的表格画条——宽度按比例算，颜色取主题色，对齐由 Qt 负责。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from ..db import Database
from ..logging_setup import get_logger
from ..stats import by_exam_type, by_week, overview
from .rendering import render_html
from .theme import DARK, Theme, set_variant

log = get_logger(__name__)

# 正确率低于这个数就标红——提醒这是短板
WEAK_ACCURACY = 60


class StatsPanel(QWidget):
    """做题趋势和分题型正确率。"""

    def __init__(self, db: Database, parent: QWidget | None = None, theme: Theme = DARK) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("pageTitle")
        self.summary_label.setWordWrap(True)
        top.addWidget(self.summary_label, stretch=1)
        refresh = QPushButton("刷新")
        set_variant(refresh, "ghost")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        layout.addWidget(self.browser, 1)

        self.refresh()

    def refresh(self) -> None:
        try:
            data = overview(self._db)
            weeks = by_week(self._db)
            kinds = by_exam_type(self._db)
        except Exception:
            log.exception("读学情统计失败")
            self.summary_label.setText("读统计数据出了点问题，详见 logs/cstudy.log。")
            render_html(self.browser, "")
            return

        if not data.all_time.total:
            self.summary_label.setText("还没做过题")
            render_html(
                self.browser,
                f'<p style="color:{self._theme.muted};">'
                "去「答题」页点「从题库挑题」，做几道这儿就有数据了。</p>",
            )
            return

        self.summary_label.setText(
            f"累计做过 {data.all_time.total} 道，正确率 {data.all_time.accuracy}%"
        )
        render_html(self.browser, self._render(data, weeks, kinds))

    # ---------- 排版 ----------

    def _render(self, data, weeks, kinds) -> str:
        theme = self._theme
        parts: list[str] = []

        parts.append(self._section("总览"))
        rows = [
            ("全部", data.all_time.describe()),
            ("最近 30 天", data.last_30_days.describe()),
            ("最近 7 天", data.last_7_days.describe()),
        ]
        parts.append(self._table(rows))

        if any(bar.tally.total for bar in weeks):
            peak = max(bar.tally.total for bar in weeks)
            parts.append(self._section("每周做题量"))
            bar_rows: list[str] = []
            for bar in weeks:
                if not bar.tally.total:
                    bar_rows.append(
                        self._bar_row(bar.label, "这周没做题", 0, theme.muted)
                    )
                    continue
                ratio = bar.tally.total / peak if peak else 0
                color = theme.ok if bar.tally.accuracy >= WEAK_ACCURACY else theme.danger
                bar_rows.append(
                    self._bar_row(
                        bar.label,
                        f"{bar.tally.total} 道 · {bar.tally.accuracy}%",
                        ratio,
                        color,
                    )
                )
            parts.append(
                '<table width="640" cellspacing="0" cellpadding="0">'
                + "".join(bar_rows)
                + "</table>"
            )

        if kinds:
            parts.append(self._section("按题型看强弱"))
            rows: list[str] = []
            for name, tally in kinds:
                if tally.total < 5:
                    note = f'<span style="color:{theme.muted}; font-size:9.5pt;">题太少，看不出强弱</span>'
                elif tally.accuracy < WEAK_ACCURACY:
                    note = f'<span style="color:{theme.danger}; font-size:9.5pt;">这是短板，多练这块</span>'
                else:
                    note = ""
                rows.append(
                    "<tr>"
                    f'<td style="padding:1px 16px 1px 0; white-space:nowrap;">'
                    f"{_escape(name)}</td>"
                    f'<td style="padding:1px 16px 1px 0; color:{theme.muted};'
                    f' font-size:9.5pt; white-space:nowrap;">{_escape(tally.describe())}</td>'
                    f'<td style="padding:1px 0;">{note}</td>'
                    "</tr>"
                )
            # 用表格不用 `<ul>`：Qt 给列表项的行距调不动，三条就撑出半屏空白
            parts.append(
                '<table width="640" cellspacing="0" cellpadding="0">'
                + "".join(rows)
                + "</table>"
            )
            parts.append(
                f'<p style="color:{theme.muted}; font-size:9.5pt; margin:12px 0 0 0;">'
                "分题型看比只看总分有用：总分 70% 可能掩盖着"
                "「程序填空只有 30%」这种事，那才是丢分的地方。</p>"
            )

        return "".join(parts)

    def _section(self, title: str) -> str:
        return (
            f'<p style="font-size:11pt; font-weight:600; color:{self._theme.text};'
            ' margin:14px 0 6px 0;">'
            f"{_escape(title)}</p>"
        )

    def _table(self, rows: list[tuple[str, str]]) -> str:
        cells = "".join(
            "<tr>"
            f'<td style="color:{self._theme.muted}; padding:3px 18px 3px 0;">{_escape(label)}</td>'
            f'<td style="padding:3px 0;">{_escape(value)}</td>'
            "</tr>"
            for label, value in rows
        )
        return f'<table cellspacing="0" cellpadding="0">{cells}</table>'

    def _bar_row(self, label: str, text: str, ratio: float, color: str) -> str:
        """一行：日期 · 按比例上色的条 · 数字。

        三个坑，都是渲染出来才发现的：
        1. **条的高度只能靠小字号撑**：Qt 的表格忽略 `height:8px`，写上去塌成一条线；
           而直接用 `&nbsp;` 撑又会用默认字号，行高胖到 50px。所以把 `font-size`
           压到 3pt，条就细了。
        2. **没做过题的那几周不画条**：画一条满宽的灰轨等于每周都占一排视觉重量，
           十来行下来页面全是灰块，真正的数据反而看不见。
        3. 宽度用百分比，换窗口大小自动跟着变，不用算像素。
        """
        ratio = max(0.0, min(1.0, ratio))
        bar = ""
        if ratio > 0:
            filled = max(2, round(ratio * 100))
            bar = (
                '<table width="440" cellspacing="0" cellpadding="0"'
                ' style="margin:0; border:0;">'
                "<tr>"
                f'<td width="{filled}%" style="background-color:{color}; padding:0;">'
                '<span style="font-size:3pt; line-height:3pt;">&nbsp;</span></td>'
                f'<td style="background-color:{self._theme.surface_alt}; padding:0;">'
                '<span style="font-size:3pt; line-height:3pt;">&nbsp;</span></td>'
                "</tr></table>"
            )
        return (
            "<tr>"
            f'<td style="color:{self._theme.muted}; font-size:9.5pt;'
            f' padding:1px 14px 1px 0; white-space:nowrap;">{_escape(label)}</td>'
            f'<td style="padding:1px 14px 1px 0;">{bar}</td>'
            f'<td style="font-size:9.5pt; white-space:nowrap;">{_escape(text)}</td>'
            "</tr>"
        )


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
