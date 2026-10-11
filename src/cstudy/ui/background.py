"""窗口背景层：背景图 + 压暗蒙版。

**为什么不直接用 QSS 给 QMainWindow 设 `background-image`**：子控件
（标签、文本框、页签、滚动区）各自带着自己的底色，图会被切成一块一块的补丁。

正确做法是三层：
1. 最底下这个 widget 画背景（纯色，或者背景图）
2. 图上压一层半透明黑，把图压暗，字才看得清
3. 上面的控件在 QSS 里设成**半透明**（`Theme.surface_alpha`），图透得出来，字也不糊

`background_dim` 调的就是第 2 层的深浅。
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget

from ..logging_setup import get_logger
from .theme import Appearance

log = get_logger(__name__)


class BackgroundWidget(QWidget):
    """垫在最底下的背景层。窗口的内容都放在它上面。"""

    def __init__(self, appearance: Appearance, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._appearance = appearance
        self._pixmap: QPixmap | None = None
        self._scaled: QPixmap | None = None
        self._scaled_for = (0, 0)

        if appearance.has_image:
            pixmap = QPixmap(str(appearance.background_image))
            if pixmap.isNull():
                log.warning("背景图读不出来（%s），改用纯色", appearance.background_image)
            else:
                self._pixmap = pixmap

    def paintEvent(self, event) -> None:  # noqa: N802  (Qt 的命名)
        painter = QPainter(self)
        theme = self._appearance.theme

        # 先铺底色：图没铺满的地方（比例不匹配时）会是这个颜色，不是花的
        painter.fillRect(self.rect(), QColor(theme.bg))

        pixmap = self._scaled_pixmap()
        if pixmap is not None:
            # 居中 + 裁切（等比放大到铺满，多出来的切掉），不变形也不留白边
            x = (pixmap.width() - self.width()) // 2
            y = (pixmap.height() - self.height()) // 2
            painter.drawPixmap(-x, -y, pixmap)

            dim = self._appearance.dim
            if dim > 0:
                painter.fillRect(self.rect(), QColor(0, 0, 0, int(dim * 255)))

    def _scaled_pixmap(self) -> QPixmap | None:
        """按窗口大小缩放背景图。**缓存住**——paintEvent 会被频繁调用，
        每次都缩一遍大图会明显卡顿。"""
        if self._pixmap is None:
            return None
        size = (self.width(), self.height())
        if size == self._scaled_for and self._scaled is not None:
            return self._scaled
        if size[0] <= 0 or size[1] <= 0:
            return None

        self._scaled = self._pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._scaled_for = size
        return self._scaled
