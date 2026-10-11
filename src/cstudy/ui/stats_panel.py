"""学情统计：最近几周练得怎么样。

`answers` 表一直在记，但界面上只有"本批答对 X"，关掉就没了。备考六个月，
**看趋势比看单次分数重要**——这页就是给这个用的。
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QWidget

from ..db import Database
from ..logging_setup import get_logger
from ..stats import by_exam_type, by_week, overview
from .theme import DARK, Theme

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

        top = QHBoxLayout()
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        top.addWidget(self.summary_label, stretch=1)
        refresh = QPushButton("刷新")
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
            self.browser.setHtml("")
            return

        if not data.all_time.total:
            self.summary_label.setText("还没做过题。")
            self.browser.setHtml(
                "<p>去「答题」页点「从题库挑题」，做几道这儿就有数据了。</p>"
            )
            return

        self.summary_label.setText(
            f"累计做过 {data.all_time.total} 道，正确率 {data.all_time.accuracy}%"
        )
        self.browser.setHtml(self._render(data, weeks, kinds))

    def _render(self, data, weeks, kinds) -> str:
        parts: list[str] = []

        parts.append("<h3>总览</h3><ul>")
        parts.append(f"<li>全部：{data.all_time.describe()}</li>")
        parts.append(f"<li>最近 30 天：{data.last_30_days.describe()}</li>")
        parts.append(f"<li>最近 7 天：{data.last_7_days.describe()}</li>")
        parts.append("</ul>")

        if any(bar.tally.total for bar in weeks):
            peak = max(bar.tally.total for bar in weeks)
            parts.append("<h3>每周做题量</h3>")
            lines: list[str] = []
            for bar in weeks:
                if bar.tally.total:
                    lines.append(
                        f"{bar.label:>6}  {bar.bar(peak):<26} "
                        f"{bar.tally.total:>4} 道  正确率 {bar.tally.accuracy}%"
                    )
                else:
                    lines.append(f"{bar.label:>6}  （这周没做题）")
            # **行之间必须用 \n 连**：整段是 join 出来的，忘了换行会全挤成一行
            parts.append(
                "<pre style='white-space:pre; margin:0; font-size:10.5pt;'>"
                + "\n".join(_escape(line) for line in lines)
                + "</pre>"
            )

        if kinds:
            parts.append("<h3>按题型看强弱</h3><ul>")
            for name, tally in kinds:
                if tally.total < 5:
                    parts.append(
                        f"<li>{_escape(name)}：{tally.describe()}"
                        "<small>（题太少，看不出强弱）</small></li>"
                    )
                elif tally.accuracy < WEAK_ACCURACY:
                    parts.append(
                        f'<li><span style="color:{self._theme.danger}">{_escape(name)}：'
                        f"{tally.describe()} ← 这是短板，多练这块</span></li>"
                    )
                else:
                    parts.append(f"<li>{_escape(name)}：{tally.describe()}</li>")
            parts.append("</ul>")
            parts.append(
                "<p><small>分题型看比只看总分有用：总分 70% 可能掩盖着"
                "「程序填空只有 30%」这种事，那才是丢分的地方。</small></p>"
            )

        return "".join(parts)


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
