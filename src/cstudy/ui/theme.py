"""界面主题：深色、好读、颜色和背景都能改。

**为什么用样式表而不是逐个控件设颜色**：12 个界面文件、两千多行，
一个个设颜色既改不动也容易漏。QSS 一处生效、改主题只改这一个文件。

**深色不难，难在"好读"**：终端风那种近黑 + 密排的界面看久了很累，
而这个软件是拿来读代码和长文字的。所以：面板比最底层亮一档，
输入框再亮一档，行高放宽，边框细但可见。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..config import Config
from ..logging_setup import get_logger

log = get_logger(__name__)


def _rgba(color: str, alpha: float) -> str:
    """`#252526` + 0.9 → `rgba(37, 37, 38, 0.9)`。

    背景图那套要靠半透明：面板全不透明的话，图就被切一块一块的；
    全透明的话字又看不清。半透明是唯一能让两者共存的解法。
    """
    text = color.lstrip("#")
    if len(text) != 6:
        return color
    try:
        red, green, blue = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return color
    return f"rgba({red}, {green}, {blue}, {alpha:.2f})"


@dataclass(frozen=True)
class Theme:
    """一套配色。颜色都是 `#rrggbb`。"""

    name: str
    bg: str           # 最底层（窗口背景、没放背景图时的底色）
    surface: str      # 面板、标签页底
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

    def stylesheet(self) -> str:
        panel = _rgba(self.surface, self.surface_alpha)
        field = _rgba(self.field, self.field_alpha)

        return f"""
/* ---------- 全局 ---------- */
QWidget {{
    background-color: transparent;
    color: {self.text};
}}
QMainWindow, QDialog {{
    background-color: {self.bg};
}}
QToolTip {{
    background-color: {self.surface};
    color: {self.text};
    border: 1px solid {self.border};
    padding: 4px;
}}

/* ---------- 页签 ---------- */
QTabWidget::pane {{
    background-color: {panel};
    border: 1px solid {self.border};
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {self.muted};
    padding: 8px 18px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
    font-size: 11pt;
}}
QTabBar::tab:hover {{
    color: {self.text};
}}
QTabBar::tab:selected {{
    color: {self.accent};
    border-bottom: 2px solid {self.accent};
}}

/* ---------- 按钮 ---------- */
QPushButton {{
    background-color: {field};
    color: {self.text};
    border: 1px solid {self.border};
    border-radius: 5px;
    padding: 6px 16px;
    min-height: 20px;
}}
QPushButton:hover {{
    border-color: {self.accent};
}}
QPushButton:pressed {{
    background-color: {panel};
}}
QPushButton:disabled {{
    color: {self.muted};
    border-color: {self.border};
}}
QPushButton:default {{
    border-color: {self.accent};
}}

/* ---------- 输入 ---------- */
QLineEdit, QPlainTextEdit, QTextBrowser, QTextEdit {{
    background-color: {field};
    color: {self.text};
    border: 1px solid {self.border};
    border-radius: 5px;
    padding: 6px 8px;
    selection-background-color: {self.accent};
    selection-color: {self.bg};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {self.accent};
}}

/* ---------- 下拉框、列表（筛题对话框用）---------- */
QComboBox {{
    background-color: {field};
    color: {self.text};
    border: 1px solid {self.border};
    border-radius: 5px;
    padding: 5px 10px;
    min-height: 20px;
}}
QComboBox:hover {{
    border-color: {self.accent};
}}
QComboBox QAbstractItemView {{
    background-color: {self.surface};
    color: {self.text};
    border: 1px solid {self.border};
    selection-background-color: {self.accent};
    selection-color: {self.bg};
    outline: none;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QListWidget {{
    background-color: {field};
    color: {self.text};
    border: 1px solid {self.border};
    border-radius: 5px;
    outline: none;
}}
QListWidget::item {{
    padding: 5px 8px;
    border-radius: 4px;
}}
QListWidget::item:hover {{
    background-color: {panel};
}}
QListWidget::item:selected {{
    background-color: {self.accent};
    color: {self.bg};
}}

/* ---------- 复选框、单选框 ---------- */
QCheckBox, QRadioButton {{
    color: {self.text};
    spacing: 7px;
    padding: 2px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {self.border};
    background-color: {field};
}}
QCheckBox::indicator {{
    border-radius: 3px;
}}
QRadioButton::indicator {{
    border-radius: 8px;
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {self.accent};
    border-color: {self.accent};
}}

/* ---------- 滚动条：细一点，别抢戏 ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {self.border};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {self.muted};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
}}
QScrollBar::handle:horizontal {{
    background: {self.border};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
    border: none;
    height: 0;
    width: 0;
}}

/* ---------- 状态栏 ---------- */
QStatusBar {{
    background-color: {self.surface};
    color: {self.muted};
    border-top: 1px solid {self.border};
}}
QStatusBar QLabel {{
    color: {self.muted};
    padding: 0 6px;
}}

/* ---------- 分隔线 ---------- */
QSplitter::handle {{
    background-color: {self.border};
}}
"""


DARK = Theme(
    name="dark",
    bg="#1e1e1e",
    surface="#252526",
    field="#2d2d30",
    border="#3c3c3c",
    text="#d4d4d4",
    muted="#9d9d9d",
    accent="#4a9eff",
    ok="#3fb950",
    danger="#f14c4c",
)

LIGHT = Theme(
    name="light",
    bg="#f3f3f3",
    surface="#ffffff",
    field="#ffffff",
    border="#d0d0d0",
    text="#1f1f1f",
    muted="#616161",
    accent="#0067c0",
    ok="#1a7f37",
    danger="#c0392b",
)

THEMES = {"dark": DARK, "light": LIGHT}

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
        log.warning("ui.theme.mode 只认 dark / light（读到 %r），用深色", mode)
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
        ok=base.ok,
        danger=base.danger,
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
