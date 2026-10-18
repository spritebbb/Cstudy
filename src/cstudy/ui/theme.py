"""界面主题：配色、字号层级、圆角与全部控件样式。

**为什么用样式表而不是逐个控件设颜色**：12 个界面文件、两千多行，
一个个设颜色既改不动也容易漏。QSS 一处生效、改主题只改这一个文件。

**深色不难，难在"好读"**：终端风那种近黑 + 密排的界面看久了很累，
而这个软件是拿来读代码和长文字的。所以分三层明度：
    底（bg） < 面板（surface） < 输入/卡片面（field）
行高放宽，边框细但可见，强调色只用在"当前在哪""这里能点"。

**颜色是派生的，不是手抄的**：`accent_soft`、`surface_alt` 这些都由
基础色按比例混出来。用户在 config.yaml 里只改一个 `accent`，
选中底、hover 边、图标色会**一起**跟着变——不然改完主色，
界面上还留着一圈旧蓝的底，比不改还乱。
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger

log = get_logger(__name__)


# ---------- 颜色工具 ----------


def _rgb(color: str) -> tuple[int, int, int] | None:
    """`#252526` → (37, 37, 38)。认不出来就返回 None，别抛。"""
    text = color.lstrip("#")
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _rgba(color: str, alpha: float) -> str:
    """`#252526` + 0.9 → `rgba(37, 37, 38, 0.90)`。

    背景图那套要靠半透明：面板全不透明的话，图就被切一块一块的；
    全透明的话字又看不清。半透明是唯一能让两者共存的解法。
    """
    parsed = _rgb(color)
    if parsed is None:
        return color
    red, green, blue = parsed
    return f"rgba({red}, {green}, {blue}, {alpha:.2f})"


def _mix(front: str, back: str, amount: float) -> str:
    """把 front 按 amount 的比例混进 back。用来派生同色系的深浅档。"""
    first, second = _rgb(front), _rgb(back)
    if first is None or second is None:
        return back
    ratio = min(1.0, max(0.0, amount))
    mixed = tuple(
        round(second[i] + (first[i] - second[i]) * ratio) for i in range(3)
    )
    return "#" + "".join(f"{value:02x}" for value in mixed)


def _luminance(color: str) -> float:
    """WCAG 相对亮度（0 黑、1 白）。用来判断"深色底还是浅色底"。"""
    parsed = _rgb(color)
    if parsed is None:
        return 0.0
    channels = []
    for value in parsed:
        channel = value / 255
        channels.append(channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(one: str, other: str) -> float:
    """WCAG 对比度（1:1 到 21:1）。正文要 ≥ 7，辅助文字要 ≥ 4.5。

    这个函数是给测试和"挑配色"用的门——**感觉"够清楚"不算数**，
    算出来的数字才算。深色主题里最容易翻车的是次要文字和强调色当文字用。
    """
    first, second = _luminance(one), _luminance(other)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _is_dark(color: str) -> bool:
    return _luminance(color) < 0.35


# 勾选框的勾、单选的点、半选的横杠——QSS 覆盖 indicator 之后 Qt 不再画这些，
# 不自己给图的话，选中和没选中只差一个底色，多选题根本看不出勾了几个。
_ICON_SVGS = {
    "check": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    '<path d="M3.5 8.4 L6.4 11.2 L12.5 5.0" fill="none" stroke="{color}" '
    'stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    "dot": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    '<circle cx="8" cy="8" r="3.4" fill="{color}"/></svg>',
    "dash": '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">'
    '<rect x="3.6" y="7.2" width="8.8" height="2" rx="1" fill="{color}"/></svg>',
}


def _icon_assets(text_color: str) -> dict[str, str]:
    """把三个小图写到临时目录，返回 QSS 能用的正斜杠路径。

    为什么不塞进项目目录：这是纯运行时图形，跟着打包程序还得额外配
    `datas`；用系统临时目录谁都不欠谁。写失败就退回没有图形（只是少了勾，
    界面不会坏）。
    """
    folder = Path(tempfile.gettempdir()) / "cstudy-ui"
    paths: dict[str, str] = {}
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, template in _ICON_SVGS.items():
            target = folder / f"{name}.svg"
            body = template.format(color=text_color)
            # 颜色变了要重写（换主题时勾的颜色得跟着走）
            if not target.exists() or target.read_text(encoding="utf-8") != body:
                target.write_text(body, encoding="utf-8")
            paths[name] = target.as_posix()
    except OSError:
        log.warning("勾选图标写不出去，退回纯色指示器", exc_info=True)
    return paths


# ---------- 一套配色 ----------


@dataclass(frozen=True)
class Theme:
    """一套配色。基础颜色都是 `#rrggbb`，其余由它们派生。"""

    name: str
    bg: str           # 最底层（窗口背景、没放背景图时的底色）
    surface: str      # 面板、页签底
    field: str        # 输入框、代码框——比面板再亮一档，让人看出"这里能输入"
    border: str       # 分隔线和边框
    text: str         # 正文
    muted: str        # 次要文字（提示、计数）
    accent: str       # 强调色：选中的页签、主要按钮
    ok: str           # 答对
    danger: str       # 答错、该复习了
    # 背景图存在时，面板用半透明，好让图透出来又不影响读字
    surface_alpha: float = 1.0
    # 输入框、代码区用得更实——**图再好看也不能盖过代码的可读性**
    field_alpha: float = 1.0

    # ----- 派生色：不写进配置，改基础色会自动跟着变 -----

    @property
    def is_dark(self) -> bool:
        return _is_dark(self.bg)

    @property
    def surface_alt(self) -> str:
        """卡片里再抬一层的底：表头、hover、代码块内衬。"""
        return _mix(self.text, self.surface, 0.06 if self.is_dark else 0.03)

    @property
    def border_strong(self) -> str:
        return _mix(self.text, self.border, 0.22)

    @property
    def faint(self) -> str:
        """比 muted 还弱一档：时间戳、占位符、禁用文字。"""
        return _mix(self.muted, self.bg, 0.42)

    @property
    def accent_hover(self) -> str:
        return _mix(self.text, self.accent, 0.10 if self.is_dark else 0.14)

    @property
    def accent_pressed(self) -> str:
        return _mix(self.bg, self.accent, 0.18 if self.is_dark else 0.10)

    @property
    def accent_soft(self) -> str:
        """强调色的淡化底：选中页签、选中列表项、徽章。"""
        return _rgba(self.accent, 0.16 if self.is_dark else 0.12)

    @property
    def accent_edge(self) -> str:
        return _rgba(self.accent, 0.55 if self.is_dark else 0.45)

    @property
    def accent_text(self) -> str:
        """压在强调色底上的字：黑字还是白字，**按对比度算，不按"看着亮不亮"猜**。

        第一版用的是"亮度 > 0.5 就配深字"，结果 #4c9aff 这类中等亮度蓝
        被判成"亮"，配了白字，实测只有 2.85:1 —— 按钮上的字发虚。
        """
        dark, light = "#0b1116", "#ffffff"
        return dark if contrast(dark, self.accent) >= contrast(light, self.accent) else light

    @property
    def accent_on_soft(self) -> str:
        """淡化底（选中页签）上的强调色文字。底变浅了，字得单独算对比度。"""
        base = _mix(self.accent, self.surface, 0.16 if self.is_dark else 0.12)
        return (
            self.accent
            if contrast(self.accent, base) >= 4.5
            else _mix(self.text, self.accent, 0.35)
        )

    @property
    def ok_soft(self) -> str:
        return _rgba(self.ok, 0.16)

    @property
    def danger_soft(self) -> str:
        return _rgba(self.danger, 0.18)

    @property
    def warn(self) -> str:
        return "#e2b04a" if self.is_dark else "#b45309"

    @property
    def scroll(self) -> str:
        return _rgba(self.text, 0.16)

    @property
    def scroll_hover(self) -> str:
        return _rgba(self.text, 0.30)

    @property
    def radius(self) -> int:
        return 10

    @property
    def radius_small(self) -> int:
        return 7

    def _palette(self) -> dict[str, str]:
        icons = _icon_assets(self.accent_text)
        colors = {
            "bg": self.bg,
            "surface": self.surface,
            "field": self.field,
            "border": self.border,
            "border_strong": self.border_strong,
            "text": self.text,
            "muted": self.muted,
            "faint": self.faint,
            "accent": self.accent,
            "accent_hover": self.accent_hover,
            "accent_pressed": self.accent_pressed,
            "accent_soft": self.accent_soft,
            "accent_edge": self.accent_edge,
            "accent_on_soft": self.accent_on_soft,
            "accent_text": self.accent_text,
            "ok": self.ok,
            "ok_soft": self.ok_soft,
            "danger": self.danger,
            "danger_soft": self.danger_soft,
            "danger_edge": _rgba(self.danger, 0.5),
            "warn": self.warn,
            "scroll": self.scroll,
            "scroll_hover": self.scroll_hover,
            "panel": _rgba(self.surface, self.surface_alpha),
            "panel_field": _rgba(self.field, self.field_alpha),
            "panel_alt": _rgba(self.surface_alt, self.surface_alpha),
            "radius": str(self.radius),
            "radius_small": str(self.radius_small),
            "check_icon": icons.get("check", ""),
            "dot_icon": icons.get("dot", ""),
            "dash_icon": icons.get("dash", ""),
        }
        return colors

    def stylesheet(self) -> str:
        # 用 % 格式化而不是 f-string：QSS 里全是 {}，f-string 得每个都双写
        return _QSS % self._palette()


# ---------- 样式表 ----------
#
# 几处"看着能写其实不行"的地方，改之前先看清楚：
# 1. QSS **没有 transition / box-shadow**。想要"渐变高亮"只能用 QPropertyAnimation
#    写代码，想要投影只能用 QGraphicsDropShadowEffect（很吃性能）。
#    这里的层次全靠**明度分层 + 1px 边框**做，不靠阴影。
# 2. `QWidget { background: transparent }` 是全局的，所以每个要底色的容器
#    （QTabWidget::pane、卡片 QFrame#card、输入框）都必须**自己**再设一次底色。
# 3. QComboBox 的默认下拉箭头一旦被 QSS 接管就可能消失。这里只设 drop-down
#    的宽度、不设箭头图，让 Qt 自己画——实测这样箭头还在。
# 4. 知识图谱的 QGraphicsView **只设边框圆角，不设底色**：底色由面板自己
#    `setBackgroundBrush` 设，两边都设会叠出近乎全不透明的一块（实测算过 0.88 叠两层 ≈ 98.6%）。

_QSS = """
/* ---------- 全局 ---------- */
QWidget {
    background-color: transparent;
    color: %(text)s;
}
QMainWindow, QDialog {
    background-color: %(bg)s;
}
QToolTip {
    background-color: %(panel_alt)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 6px;
    padding: 6px 9px;
}

/* ---------- 页签：选中态是"胶囊"，不是一条下划线 ---------- */
QTabWidget::pane {
    background-color: %(panel)s;
    border: 1px solid %(border)s;
    border-radius: %(radius)spx;
    top: -1px;
}
QTabWidget::tab-bar {
    left: 4px;
}
QTabBar::tab {
    background: transparent;
    color: %(muted)s;
    padding: 7px 14px;
    margin: 4px 4px 0 0;
    border: 1px solid transparent;
    border-radius: %(radius_small)spx;
    font-size: 10.5pt;
}
QTabBar::tab:hover {
    color: %(text)s;
    background-color: %(field)s;
}
QTabBar::tab:selected {
    color: %(accent_on_soft)s;
    background-color: %(accent_soft)s;
    border-color: %(accent_edge)s;
    font-weight: 600;
}
QTabBar::tab:disabled {
    color: %(faint)s;
}

/* ---------- 按钮：默认次级、主要动作用 variant="primary" ---------- */
QPushButton {
    background-color: %(field)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
    padding: 7px 16px;
    min-height: 18px;
}
QPushButton:hover {
    background-color: %(panel_alt)s;
    border-color: %(accent_edge)s;
}
QPushButton:pressed {
    background-color: %(panel)s;
}
QPushButton:focus {
    border-color: %(accent)s;
}
QPushButton:disabled {
    background-color: %(panel)s;
    color: %(faint)s;
    border-color: %(border)s;
}
QPushButton:default {
    border-color: %(accent_edge)s;
}
QPushButton[variant="primary"] {
    background-color: %(accent)s;
    color: %(accent_text)s;
    border: 1px solid %(accent)s;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background-color: %(accent_hover)s;
    border-color: %(accent_hover)s;
}
QPushButton[variant="primary"]:pressed {
    background-color: %(accent_pressed)s;
    border-color: %(accent_pressed)s;
}
QPushButton[variant="primary"]:focus {
    border-color: %(accent_text)s;
}
QPushButton[variant="primary"]:disabled {
    background-color: %(panel_alt)s;
    color: %(faint)s;
    border-color: %(border)s;
}
QPushButton[variant="ghost"] {
    background-color: transparent;
    color: %(muted)s;
    border-color: transparent;
    padding: 6px 10px;
}
QPushButton[variant="ghost"]:hover {
    background-color: %(field)s;
    color: %(text)s;
}
QPushButton[variant="danger"] {
    background-color: transparent;
    color: %(danger)s;
    border-color: %(danger_edge)s;
}
QPushButton[variant="danger"]:hover {
    background-color: %(danger_soft)s;
}

/* ---------- 输入 ---------- */
QLineEdit, QPlainTextEdit, QTextBrowser, QTextEdit, QSpinBox, QDoubleSpinBox {
    background-color: %(panel_field)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
    padding: 7px 10px;
    selection-background-color: %(accent)s;
    selection-color: %(accent_text)s;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QTextBrowser:focus {
    border-color: %(accent)s;
}
QLineEdit:disabled, QPlainTextEdit:disabled {
    color: %(faint)s;
}

/* ---------- 知识图谱的画布（只给边框，底色由面板自己设）---------- */
QGraphicsView {
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
}

/* ---------- 下拉框、列表 ---------- */
QComboBox {
    background-color: %(field)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
    padding: 6px 10px;
    min-height: 18px;
}
QComboBox:hover {
    border-color: %(accent_edge)s;
}
QComboBox:focus {
    border-color: %(accent)s;
}
QComboBox QAbstractItemView {
    background-color: %(panel_alt)s;
    color: %(text)s;
    border: 1px solid %(border_strong)s;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: %(accent_soft)s;
    selection-color: %(text)s;
    outline: none;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QListWidget, QListView, QTreeWidget, QTableWidget {
    background-color: %(panel_field)s;
    alternate-background-color: %(panel_alt)s;
    color: %(text)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
    padding: 4px;
    outline: none;
}
QListWidget::item, QTreeWidget::item {
    padding: 6px 8px;
    margin: 1px 2px;
    border-radius: 6px;
}
QListWidget::item:hover, QTreeWidget::item:hover {
    background-color: %(panel_alt)s;
}
QListWidget::item:selected, QTreeWidget::item:selected {
    background-color: %(accent_soft)s;
    color: %(text)s;
}

/* ---- 左侧导航栏（主窗口自己用，其余列表不受影响） ---- */
QListWidget#navList {
    background-color: %(panel)s;
    border: none;
    border-right: 1px solid %(border)s;
    border-radius: 0px;
    padding: 12px 8px;
    outline: none;
}
QListWidget#navList::item {
    padding: 0px 10px;
    margin: 2px 0px;
    border-radius: %(radius_small)spx;
    color: %(muted)s;
}
QListWidget#navList::item:hover {
    background-color: %(panel_alt)s;
    color: %(text)s;
}
QListWidget#navList::item:selected {
    background-color: %(accent_soft)s;
    color: %(accent_on_soft)s;
    font-weight: 600;
    border-left: 3px solid %(accent)s;
    padding-left: 7px;
}
QHeaderView::section {
    background-color: %(panel_alt)s;
    color: %(muted)s;
    border: none;
    border-bottom: 1px solid %(border)s;
    padding: 6px 8px;
}

/* ---------- 复选框、单选框 ---------- */
QCheckBox, QRadioButton {
    color: %(text)s;
    spacing: 8px;
    padding: 2px 0;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 15px;
    height: 15px;
    border: 1px solid %(border_strong)s;
    background-color: %(panel_field)s;
}
QCheckBox::indicator {
    border-radius: 4px;
}
QRadioButton::indicator {
    border-radius: 8px;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: %(accent)s;
}
QCheckBox::indicator:checked {
    background-color: %(accent)s;
    border-color: %(accent)s;
    image: url("%(check_icon)s");
}
QCheckBox::indicator:indeterminate {
    background-color: %(accent)s;
    border-color: %(accent)s;
    image: url("%(dash_icon)s");
}
QRadioButton::indicator:checked {
    background-color: %(accent)s;
    border-color: %(accent)s;
    image: url("%(dot_icon)s");
}
QCheckBox:disabled, QRadioButton:disabled {
    color: %(faint)s;
}

/* ---------- 滚动条：细一点，别抢戏 ---------- */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 0;
}
QScrollBar::handle:vertical {
    background: %(scroll)s;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: %(scroll_hover)s;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0 2px 2px 2px;
}
QScrollBar::handle:horizontal {
    background: %(scroll)s;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover {
    background: %(scroll_hover)s;
}
QScrollBar::add-line, QScrollBar::sub-line {
    background: none;
    border: none;
    height: 0;
    width: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* ---------- 状态栏 ---------- */
QStatusBar {
    background-color: %(panel)s;
    color: %(muted)s;
    border-top: 1px solid %(border)s;
}
QStatusBar::item {
    border: none;
}
QStatusBar QLabel {
    color: %(muted)s;
    font-size: 9.5pt;
    padding: 0 8px;
}

/* ---------- 菜单 ---------- */
QMenu {
    background-color: %(panel_alt)s;
    border: 1px solid %(border_strong)s;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item {
    padding: 6px 20px 6px 14px;
    border-radius: 5px;
    color: %(text)s;
}
QMenu::item:selected {
    background-color: %(accent_soft)s;
    color: %(text)s;
}
QMenu::separator {
    height: 1px;
    background-color: %(border)s;
    margin: 6px 8px;
}

/* ---------- 分隔条 ---------- */
QSplitter::handle {
    background-color: %(border)s;
}
QSplitter::handle:hover {
    background-color: %(accent_edge)s;
}
QSplitter::handle:horizontal {
    width: 3px;
}
QSplitter::handle:vertical {
    height: 3px;
}

/* ---------- 容器：卡片、分隔线 ---------- */
QFrame#card {
    background-color: %(panel)s;
    border: 1px solid %(border)s;
    border-radius: %(radius)spx;
}
QFrame#cardRaised {
    background-color: %(panel_alt)s;
    border: 1px solid %(border)s;
    border-radius: %(radius)spx;
}
QFrame#divider {
    background-color: %(border)s;
    border: none;
    max-height: 1px;
}
QFrame#vline {
    background-color: %(border)s;
    border: none;
    min-width: 1px;
    max-width: 1px;
}

/* ---------- 答题页：题干纸、判分纸、选项行 ----------
   题干和判分块本质是 QTextBrowser，但这里是"纸张"不是"输入框"：
   底色用面板色、内边距更大，跟下面真正的输入框区分开。
   QSS 不支持"父控件按子控件状态改样式"，所以选项行的选中态
   靠 ChoiceRow 里的 setProperty("checked") + 重新 polish 来切。 */
QTextBrowser#questionCard {
    background-color: %(panel)s;
    border: 1px solid %(border)s;
    border-radius: %(radius)spx;
    padding: 14px 16px;
}
QTextBrowser#feedbackCard {
    background-color: %(panel_alt)s;
    border: 1px solid %(border)s;
    border-radius: %(radius)spx;
    padding: 12px 16px;
}
QFrame#choiceRow {
    background-color: %(panel_field)s;
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
}
QFrame#choiceRow:hover {
    border-color: %(accent_edge)s;
}
QFrame#choiceRow[checked="true"] {
    background-color: %(accent_soft)s;
    border-color: %(accent)s;
}
QLabel#choiceText {
    color: %(text)s;
}

/* ---------- 文字层级：靠 objectName 分，别在面板里写死字号 ---------- */
QLabel#pageTitle {
    font-size: 15pt;
    font-weight: 600;
    color: %(text)s;
}
QLabel#pageHint {
    color: %(muted)s;
    font-size: 9.5pt;
}
QLabel#statValue {
    font-size: 18pt;
    font-weight: 600;
    color: %(accent)s;
}
QLabel#chip {
    background-color: %(accent_soft)s;
    color: %(accent)s;
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 9pt;
}
QRadioButton#choiceLetter {
    font-weight: 600;
    min-width: 14px;
}
QRadioButton#choiceLetter:checked {
    color: %(accent)s;
}

/* ---------- 进度条、分组框、工具按钮 ---------- */
QProgressBar {
    background-color: %(field)s;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: %(text)s;
}
QProgressBar::chunk {
    background-color: %(accent)s;
    border-radius: 4px;
}
QGroupBox {
    border: 1px solid %(border)s;
    border-radius: %(radius_small)spx;
    margin-top: 12px;
    padding-top: 8px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: %(muted)s;
}
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 4px 8px;
    color: %(muted)s;
}
QToolButton:hover {
    background-color: %(field)s;
    color: %(text)s;
}
"""


# ---------- 现成配色 ----------
#
# 挑配色的硬指标（tests/test_theme.py 里盯着）：
#   正文 vs 面板 ≥ 7:1、次要文字 vs 面板 ≥ 4.5:1、强调色 vs 面板 ≥ 4.5:1。

DARK = Theme(
    name="dark",
    bg="#0d1117",
    surface="#161b22",
    field="#1c232c",
    border="#2a313c",
    text="#dde4ec",
    muted="#93a1b1",
    accent="#4c9aff",
    ok="#3fb950",
    danger="#f06a6a",
)

# 墨青：长时间读长文最省眼的一套，蓝光最少。
TEAL = Theme(
    name="teal",
    bg="#0b1514",
    surface="#12201f",
    field="#192a28",
    border="#26403c",
    text="#d9e6e3",
    muted="#8fa8a3",
    accent="#2fbfa4",
    ok="#4ade80",
    danger="#f07171",
)

# 靛紫 + 暖橙：层次最分明，不像原生 Qt。
VIOLET = Theme(
    name="violet",
    bg="#0f0e17",
    surface="#181726",
    field="#201f31",
    border="#2f2d45",
    text="#e2e0ef",
    muted="#9d99b8",
    accent="#9b87f5",
    ok="#3ddc97",
    danger="#fb7185",
)

LIGHT = Theme(
    name="light",
    bg="#f2f4f7",
    surface="#ffffff",
    field="#ffffff",
    border="#dde2e8",
    text="#1b2430",
    muted="#5f6b7a",
    accent="#2563eb",
    ok="#15803d",
    danger="#c92a2a",
)

# ---------- 护眼两套 ----------
#
# **"护眼"不等于"绿色"**。真正能减负的四件事：
#   1. 蓝光占比低（暖色底、暖白字，屏幕色温往 3500K 那边靠）；
#   2. 背景不用纯黑也不用纯白——纯黑配白字对比过强，眼睛在明暗之间来回对焦更累；
#   3. 对比度够但不过分：正文落在 8~13:1，而不是拉到 21:1；
#   4. 大面积的色块饱和度低，只让强调色亮一点，眼睛才有地方落脚。
#
# 民间常说的"豆沙绿护眼"（#C7EDCC 那种亮绿）在**亮色**下反而刺眼：那个绿太亮，
# 白字放不上去、黑字对比又只有 4 左右。所以 sage 把它压暗、降饱和，做成能久看的版本。

# 暖灰：深色里蓝光最少的一套。晚上开着不刺眼，读代码也够清楚。
WARM = Theme(
    name="warm",
    bg="#16140f",
    surface="#1f1c15",
    field="#262219",
    border="#37312a",
    text="#e8dfd2",
    muted="#a79c8a",
    accent="#d9a95c",
    ok="#8cbf6a",
    danger="#e08a76",
)

# 豆沙绿：亮色里最适合长时间看的一套。底色是压暗、降饱和的浅豆绿，
# 不用纯白 —— 白底在大太阳底下或者晚上开灯时都晃眼。
SAGE = Theme(
    name="sage",
    bg="#cdd9cd",
    surface="#dfe9df",
    field="#f2f7f2",
    border="#b5c5b5",
    text="#26302a",
    muted="#4f5f52",
    accent="#2f6b45",
    ok="#2e7d4f",
    danger="#a8443a",
)

THEMES = {
    "dark": DARK,
    "light": LIGHT,
    "teal": TEAL,
    "violet": VIOLET,
    "warm": WARM,
    "sage": SAGE,
}

DEFAULT_MODE = "dark"
# 背景图上面压一层黑，让字看得清。
# 0.7 是"图看得出是什么、面板上的字也读得清"的位置——第一版设 0.85，
# 渲染出来发现图被压得基本看不见了，白配一张图。
DEFAULT_DIM = 0.7

# 有背景图时面板的透明度。面板透一点，图才露得出来。
SURFACE_ALPHA_WITH_IMAGE = 0.88
# 但**输入框和代码区要更实**——图再好看也不能盖过代码的可读性。
FIELD_ALPHA_WITH_IMAGE = 0.96


@dataclass(frozen=True)
class Appearance:
    """最终生效的外观：主题 + 背景图。"""

    theme: Theme
    background_image: Path | None = None
    dim: float = DEFAULT_DIM

    @property
    def has_image(self) -> bool:
        return self.background_image is not None


def _color(config: Config, key: str, fallback: str) -> str:
    raw = str(config.get(f"ui.theme.{key}", "") or "").strip()
    if not raw:
        return fallback
    if not raw.startswith("#"):
        log.warning("ui.theme.%s 不是 #rrggbb 颜色（%r），用默认值", key, raw)
        return fallback

    body = raw[1:]
    if len(body) == 3:
        # CSS 的 `#abc` 简写——展开成 `#aabbcc` 再往下传。
        # QSS 对简写的支持看版本，展开了一劳永逸。
        return "#" + "".join(char * 2 for char in body)
    if len(body) != 6:
        log.warning("ui.theme.%s 得是 #rrggbb（读到 %r），用默认值", key, raw)
        return fallback
    try:
        int(body, 16)
    except ValueError:
        log.warning("ui.theme.%s 里有非十六进制字符（%r），用默认值", key, raw)
        return fallback
    return raw.lower()


def load_appearance(config: Config) -> Appearance:
    """从 config.yaml 读外观设置。配置写坏了就退回默认深色，不让它挡住启动。"""
    mode = str(config.get("ui.theme.mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    base = THEMES.get(mode)
    if base is None:
        log.warning(
            "ui.theme.mode 只认 %s（读到 %r），用深色",
            " / ".join(THEMES),
            mode,
        )
        base = DARK

    theme = Theme(
        name=base.name,
        bg=_color(config, "bg", base.bg),
        surface=_color(config, "surface", base.surface),
        field=_color(config, "field", base.field),
        border=_color(config, "border", base.border),
        text=_color(config, "text", base.text),
        muted=_color(config, "muted", base.muted),
        accent=_color(config, "accent", base.accent),
        # ok / danger 也必须能覆盖：用户想换个更醒目的红绿时，
        # 从前这两个写死成 base 的值，config 里改了没反应。
        ok=_color(config, "ok", base.ok),
        danger=_color(config, "danger", base.danger),
    )

    image = None
    raw_path = str(config.get("ui.theme.background_image", "") or "").strip()
    if raw_path:
        candidate = Path(raw_path).expanduser()
        if candidate.is_file():
            image = candidate
        else:
            log.warning("背景图找不到（%s），这次用纯色背景", raw_path)

    try:
        dim = float(config.get("ui.theme.background_dim", DEFAULT_DIM))
    except (TypeError, ValueError):
        log.warning("ui.theme.background_dim 不是数字，用默认值")
        dim = DEFAULT_DIM
    dim = min(1.0, max(0.0, dim))

    # 有背景图时面板得半透明，否则图被切成一块一块的。
    # 但**代码区留得更实**：面板透一点图才露得出来，代码透太多就读不清了。
    if image is not None:
        theme = replace(
            theme,
            surface_alpha=SURFACE_ALPHA_WITH_IMAGE,
            field_alpha=FIELD_ALPHA_WITH_IMAGE,
        )

    return Appearance(theme=theme, background_image=image, dim=dim)


def set_variant(widget, variant: str) -> None:
    """给控件打上 QSS 的 variant 标记，并立刻重绘。

    **光 `setProperty` 界面不会变**：QSS 的属性选择器
    （`QPushButton[variant="primary"]`）只在控件重新 polish 时才重新求值。
    少写那两句 unpolish/polish，是这类"改了样式没反应"最常见的原因。
    """
    if widget.property("variant") == variant:
        return
    widget.setProperty("variant", variant)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def apply_palette(app, theme: Theme) -> None:
    """把主题同步给 QPalette。

    **QSS 管不到的东西得靠它**：输入框的占位符颜色（`PlaceholderText`）、
    链接色、文字选中色在 Qt 里走调色板，样式表里写 `::placeholder` 无效。
    不设的话，占位符是系统默认的灰，深色底上几乎看不见。
    """
    from PyQt6.QtGui import QColor, QPalette

    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.bg,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.field,
        QPalette.ColorRole.AlternateBase: theme.surface_alt,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.field,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.accent_text,
        QPalette.ColorRole.ToolTipBase: theme.surface_alt,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Link: theme.accent,
        QPalette.ColorRole.LinkVisited: theme.accent,
        QPalette.ColorRole.PlaceholderText: theme.faint,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
