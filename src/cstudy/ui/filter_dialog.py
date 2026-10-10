"""刷题前挑题：按题型 / 章节知识点 / 来源 / 数量筛。

题库一千多道，全塞进一个队列没法用。默认全部选中，直接回车就是"全刷"。
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
)

from ..db import Database
from ..logging_setup import get_logger
from ..models import EXAM_TYPE_LABELS

log = get_logger(__name__)

# 按**考试的四类**排。程序填空单独一项——考试里它是独立的大题（36 分），
# 跟普通填空（48 分）考的能力也不一样，混在一起就没法专练。
EXAM_TYPE_ORDER = ("fill", "code_fill", "reading", "coding")

# "数量"下拉框的选项：(显示文字, 抽几道；None = 全部)
COUNT_CHOICES = (("全部", None), ("随机抽 10 道", 10), ("随机抽 20 道", 20), ("随机抽 50 道", 50))

ALL_CHAPTERS = "全部章节"
ALL_POINTS = "全部知识点"
ALL_SOURCES = "全部来源"


class FilterDialog(QDialog):
    """挑一套要刷的题。关掉对话框（取消）就什么都不做。"""

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self._db = db
        self._chapters = db.points_by_chapter()

        self.setWindowTitle("挑题刷")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        hint = QLabel("想刷哪些题？不确定就全部留着，直接点「开始刷题」。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 实时题数。控件都在下面建，信号统一在最后接——见文件末尾的说明。
        self.count_label = QLabel("")
        self.count_label.setWordWrap(True)

        form = QFormLayout()
        layout.addLayout(form)

        # ---- 题型（按考试的四类）----
        self._type_boxes: dict[str, QCheckBox] = {}
        present = {str(row["exam_type"]) for row in db.query_questions()}
        type_row = QHBoxLayout()
        for kind in EXAM_TYPE_ORDER:
            if kind not in present:
                continue
            box = QCheckBox(EXAM_TYPE_LABELS.get(kind, kind))
            box.setChecked(True)
            self._type_boxes[kind] = box
            type_row.addWidget(box)
        type_row.addStretch(1)
        form.addRow("题型", type_row)

        # ---- 章节 / 知识点 ----
        self.chapter_combo = QComboBox()
        self.chapter_combo.addItem(ALL_CHAPTERS, None)
        for chapter in self._chapters:
            self.chapter_combo.addItem(chapter, chapter)
        form.addRow("章节", self.chapter_combo)

        self.point_combo = QComboBox()
        form.addRow("知识点", self.point_combo)

        # ---- 来源 ----
        self.source_combo = QComboBox()
        self.source_combo.addItem(ALL_SOURCES, None)
        for source in db.list_sources():
            # 存全路径，只显示文件名——全路径太长，下拉框里看不完
            self.source_combo.addItem(Path(source).name, source)
        form.addRow("来源", self.source_combo)

        # ---- 数量 ----
        self.count_combo = QComboBox()
        for label, value in COUNT_CHOICES:
            self.count_combo.addItem(label, value)
        form.addRow("数量", self.count_combo)

        layout.addWidget(self.count_label)

        buttons = QDialogButtonBox()
        start_button = buttons.addButton("开始刷题", QDialogButtonBox.ButtonRole.AcceptRole)
        start_button.clicked.connect(self.accept)
        close_button = buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        close_button.clicked.connect(self.reject)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # **所有控件都建好了再连信号**：一接上就会触发一次 _refresh_count，
        # 那时它要读 source_combo 之类的控件，早一步就会算出个假的"算不出来"。
        for box in self._type_boxes.values():
            box.stateChanged.connect(self._refresh_count)
        self.chapter_combo.currentIndexChanged.connect(self._on_chapter_changed)
        self.point_combo.currentIndexChanged.connect(self._refresh_count)
        self.source_combo.currentIndexChanged.connect(self._refresh_count)
        self.count_combo.currentIndexChanged.connect(self._refresh_count)

        # 填知识点下拉框，顺带算第一次题数
        self._on_chapter_changed()

    # ---------- 联动 ----------

    def _on_chapter_changed(self) -> None:
        """章节变了，知识点下拉框跟着换。"""
        chapter = self.chapter_combo.currentData()
        self.point_combo.blockSignals(True)
        self.point_combo.clear()
        self.point_combo.addItem(ALL_POINTS, None)
        for row in self._chapters.get(chapter, []) if chapter else []:
            self.point_combo.addItem(str(row["name"]), int(row["id"]))
        self.point_combo.blockSignals(False)
        self._refresh_count()

    def criteria(self) -> dict:
        """当前选中的条件，直接喂给 `Database.query_questions()`。"""
        exam_types = [kind for kind, box in self._type_boxes.items() if box.isChecked()]
        point_id = self.point_combo.currentData()
        source = self.source_combo.currentData()
        limit = self.count_combo.currentData()

        return {
            "exam_types": exam_types,
            "point_ids": [point_id] if point_id else None,
            "sources": [source] if source else None,
            "limit": limit,
            "shuffle": limit is not None,   # 抽 N 道才有随机可言；全刷按题目顺序
        }

    def _refresh_count(self) -> None:
        try:
            total = len(self._db.query_questions(**self.criteria()))
        except Exception:
            log.warning("算符合条件的题数失败", exc_info=True)
            self.count_label.setText("符合条件的题：算不出来，详见 logs/cstudy.log")
            return

        if total == 0:
            self.count_label.setText("符合条件的题：0 道——条件太窄了，放宽一点。")
        elif self.count_combo.currentData() is not None:
            self.count_label.setText(f"符合条件的题：{total} 道（会从中随机抽）")
        else:
            self.count_label.setText(f"符合条件的题：{total} 道")
