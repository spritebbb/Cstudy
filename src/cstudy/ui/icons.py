"""页签图标：内联 SVG 转成 QIcon，颜色跟着主题走。

**为什么不到处放 png**：图标要跟主题换色（深色/亮色/墨青/靛紫），
位图得备四套；SVG 只写一次，按当前主题的色当场渲染。
这里只用线性描边图标（不填充），跟界面"细边框、少装饰"的路子一致。

颜色的三种状态（页签会用到这些）：
    普通 → muted，选中 → accent，禁用 → faint
选中态是 Qt 的 `QIcon.Mode.Selected`，不是我们自己切的。
"""

from __future__ import annotations

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

from .theme import Theme

# 24×24 视框、1.8 描边、圆头圆角——跟界面里的圆角保持一致
_SVG = {
    # 进度：清单板
    "progress": (
        '<path d="M9 4H6a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2h-3"/>'
        '<rect x="9" y="2" width="6" height="4" rx="1.6"/>'
        '<path d="M8 11.5h8M8 15.5h5"/>'
    ),
    # 笔记：摊开的书
    "notes": (
        '<path d="M2.5 6.5A2.5 2.5 0 0 1 5 4h4.5a3 3 0 0 1 2 1v14a2.5 2.5 0 0 0-2-1H4.5"/>'
        '<path d="M21.5 6.5A2.5 2.5 0 0 0 19 4h-4.5a3 3 0 0 0-2 1v14a2.5 2.5 0 0 1 2-1h5"/>'
    ),
    # 答题：方框打勾
    "quiz": (
        '<path d="M9.5 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3.5"/>'
        '<path d="M7.5 11.5 10.5 14.5 21 4.5"/>'
    ),
    # 考纲：四宫格
    "syllabus": (
        '<rect x="3.5" y="3.5" width="7" height="7" rx="1.8"/>'
        '<rect x="13.5" y="3.5" width="7" height="7" rx="1.8"/>'
        '<rect x="3.5" y="13.5" width="7" height="7" rx="1.8"/>'
        '<rect x="13.5" y="13.5" width="7" height="7" rx="1.8"/>'
    ),
    # 知识图谱：三个节点连成网
    "graph": (
        '<circle cx="12" cy="5" r="2.6"/><circle cx="5" cy="18.5" r="2.6"/>'
        '<circle cx="19" cy="18.5" r="2.6"/>'
        '<path d="M10.8 7.4 6.2 16M13.2 7.4 17.8 16M7.6 18.5h8.8"/>'
    ),
    # 错题本：带感叹的三角
    "wrong": (
        '<path d="M10.4 3.6 2.2 17.8A2 2 0 0 0 3.9 20.8h16.2a2 2 0 0 0 1.7-3L13.6 3.6a2 2 0 0 0-3.2 0z"/>'
        '<path d="M12 9.2v4.2M12 17.1h.01"/>'
    ),
    # 学情：柱状图
    "stats": (
        '<path d="M3.5 20.5h17"/>'
        '<rect x="5" y="11" width="4" height="9.5" rx="1.4"/>'
        '<rect x="13" y="4.5" width="4" height="16" rx="1.4"/>'
    ),
    # 考前突击：闪电
    "sprint": '<path d="M13.2 2.5 4.5 14.2h5.8l-1 7.3 8.7-11.7h-5.8z"/>',
}

_TEMPLATE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
    'stroke-linejoin="round">{body}</svg>'
)


def _pixmap(body: str, color: str, size: int, ratio: float) -> QPixmap:
    """把一段 SVG 画成位图。

    按 `ratio` 超采样再标回逻辑尺寸——不然高分屏上图标边缘是毛的，
    而这种 1px 描边的线性图标一糊就废了。
    """
    renderer = QSvgRenderer(QByteArray(_TEMPLATE.format(color=color, body=body).encode("utf-8")))
    pixel = int(round(size * ratio))
    pixmap = QPixmap(pixel, pixel)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(ratio)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


def themed_icon(name: str, theme: Theme, size: int = 17, color: str | None = None) -> QIcon:
    """按主题配色渲染一个图标，带普通 / 选中 / 禁用三种状态。

    `color` 给了就用这一个色（三态相同）——页签选中态在某些系统样式下
    不会走 `QIcon.Mode.Selected`，那时得由调用方显式画一个强调色版本。
    """
    body = _SVG.get(name)
    if body is None:
        return QIcon()

    ratio = 2.0
    icon = QIcon()
    if color is not None:
        states = {mode: color for mode in (
            QIcon.Mode.Normal,
            QIcon.Mode.Selected,
            QIcon.Mode.Active,
            QIcon.Mode.Disabled,
        )}
    else:
        states = {
            QIcon.Mode.Normal: theme.muted,
            QIcon.Mode.Selected: theme.accent,
            QIcon.Mode.Active: theme.muted,
            QIcon.Mode.Disabled: theme.faint,
        }
    for mode, value in states.items():
        icon.addPixmap(_pixmap(body, value, size, ratio), mode, QIcon.State.Off)
    return icon


def icon_names() -> list[str]:
    return sorted(_SVG)
