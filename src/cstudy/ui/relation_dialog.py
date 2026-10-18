"""关系确认对话框：AI 提的关系、库里已有的关系，都在这里过一遍。

**这个对话框是"AI 说的话能不能进库"的唯一关口**，所以按这个思路设计：

- **勾上的才算数。** 一句话讲清楚："勾上的就是你要保留的关系"，新建的默认勾上，
  已有的也默认勾上（取消勾选＝删掉）。一个规则管两头，不用记两套。
- **理由必须摆在眼前。** 光看到"A 和 B 有关系"没法判断对不对；
  看到"都是跳出循环用的，考试常放在一起考"才判断得了。
- **不止能砍，还能自己加。** AI 提漏了的关系（比如老师上课专门强调的），
  用户得能自己补上——否则这张图永远长不全。

**这个对话框不写库。** 它只把"要加哪些、要删哪些"算出来交给调用方，
写库由面板统一做。这样点取消就真的什么都没发生，测试里也不用真弹窗。
"""

from __future__ import annotations

from html import escape

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..ai.relations import KIND_PRIORITY, ProposedRelation
from ..db import Database
from ..graph import RELATION_KINDS
from ..logging_setup import get_logger
from .theme import DARK, Theme

log = get_logger(__name__)

# 新建的关系存在这个 role 里；已经是库里那条的，存它的 id
ROLE_RELATION = Qt.ItemDataRole.UserRole
ROLE_EXISTING_ID = Qt.ItemDataRole.UserRole + 1


class RelationDialog(QDialog):
    """过目 / 增删知识点关系。`exec()` 返回 Accepted 表示用户按了确定。"""

    def __init__(
        self,
        db: Database,
        theme: Theme = DARK,
        proposals: list[ProposedRelation] | None = None,
        dropped: list[str] | None = None,
        corrected: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._theme = theme
        self._points = db.list_syllabus_points()
        self._names = {int(row["id"]): str(row["name"]) for row in self._points}
        self._existing = db.list_relations()
        self._existing_signatures = {
            ProposedRelation(
                from_name=str(row["from_name"]),
                to_name=str(row["to_name"]),
                kind=str(row["kind"]),
                reason="",
            ).signature()
            for row in self._existing
        }

        self.setWindowTitle("知识点关系")
        self.resize(760, 620)

        self._build_ui(proposals or [], dropped or [], corrected or [])
        self._fill_list()

    # ---------- 搭建 ----------

    def _build_ui(
        self,
        proposals: list[ProposedRelation],
        dropped: list[str],
        corrected: list[str],
    ) -> None:
        root = QVBoxLayout(self)

        intro = QLabel(
            "<b>勾上的就是你要保留的关系。</b>"
            "AI 新提的默认勾上——觉得不对就把勾去掉；"
            "下面标着「已有」的是库里已经存了的，取消勾选就是删掉它。"
            "理由看不明白的，宁可先不要。"
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        # **没列进来的东西也要摆出来**：只写日志的话用户会以为 AI 就给这么多，
        # 然后照着一条不完整的图安排复习。
        # 「认了名字」和「没敢配」**分成两块各配各的标题**——认回名字的那几条
        # 是列进来了的，混在"没有列进来"底下会让人以为它没被采纳。
        if corrected:
            root.addWidget(self._notice(
                "AI 把这几处知识点名字抄岔了，<b>按考纲里最接近的那个认了</b>"
                "（下面列表里显示的是认出来的名字，觉得不对就把勾去掉）",
                corrected,
            ))
        if dropped:
            root.addWidget(self._notice(
                "还有这些<b>没有列进来</b>——名字对不上考纲里的任何一个，"
                "没敢替你猜是哪一条",
                dropped,
            ))

        self.list = QListWidget()
        # 勾选状态变了，"确定后：新增 N 条"要跟着变——不然用户勾了半天
        # 不知道最后会存进去几条
        self.list.itemChanged.connect(lambda _item: self._update_summary())
        root.addWidget(self.list, stretch=1)

        self.summary_label = QLabel("")
        root.addWidget(self.summary_label)

        root.addWidget(self._add_row())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._proposals = proposals

    def _notice(self, title: str, lines: list[str]) -> QWidget:
        """对话框顶上那块"另外还有这些"的提示。最多列 8 条，剩下的指向日志。"""
        label = QLabel(
            f"{title}：<br>"
            + "<br>".join(escape(line) for line in lines[:8])
            + (f"<br>…另外还有 {len(lines) - 8} 条，详见 logs/cstudy.log"
               if len(lines) > 8 else "")
        )
        label.setWordWrap(True)
        # 用全局 QSS 的次要文字样式，不在这里写死颜色——
        # 控件级 setStyleSheet 优先级更高，换主题时会跟不住。
        label.setObjectName("pageHint")
        return label

    def _add_row(self) -> QWidget:
        """手动加一条关系。AI 提漏的、老师上课强调的，得能自己补上。"""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)

        row.addWidget(QLabel("自己加一条："))
        self.from_combo = QComboBox()
        self.to_combo = QComboBox()
        for point_id, name in self._names.items():
            self.from_combo.addItem(name, point_id)
            self.to_combo.addItem(name, point_id)
        row.addWidget(self.from_combo, stretch=1)

        self.kind_combo = QComboBox()
        for kind in KIND_PRIORITY:
            self.kind_combo.addItem(RELATION_KINDS[kind], kind)
        row.addWidget(self.kind_combo)
        row.addWidget(QLabel("→"))
        row.addWidget(self.to_combo, stretch=1)

        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("为什么（必填，将来你自己还得靠它回忆）")
        row.addWidget(self.reason_edit, stretch=2)

        add = QPushButton("添加")
        add.clicked.connect(self._on_add)
        row.addWidget(add)
        return box

    # ---------- 列表 ----------

    def _fill_list(self) -> None:
        self.list.clear()

        if self._proposals:
            self._section(f"AI 新提的（{len(self._proposals)} 条，勾上才存）")
            for relation in self._proposals:
                self._add_item(relation, checked=True, existing_id=None)
            if self._existing:
                self._section(f"库里已有的（{len(self._existing)} 条，取消勾选就是删掉）")

        if self._existing:
            if not self._proposals:
                self._section(f"库里已有的关系（{len(self._existing)} 条，取消勾选就是删掉）")
            for row in self._existing:
                relation = ProposedRelation(
                    from_name=str(row["from_name"]),
                    to_name=str(row["to_name"]),
                    kind=str(row["kind"]),
                    reason=str(row["reason"] or ""),
                    origin=str(row["origin"] or "ai"),
                )
                self._add_item(relation, checked=True, existing_id=int(row["id"]))

        if not self.list.count():
            item = QListWidgetItem("还没有任何关系。点「让 AI 找关系」可以让它提一批。")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)

        self._update_summary()

    def _section(self, text: str) -> None:
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list.addItem(item)

    def _add_item(
        self, relation: ProposedRelation, checked: bool, existing_id: int | None
    ) -> None:
        # 每个来源标清楚：AI 提的、自己加的、库里已有的。
        # 用户得知道哪条该多留个心眼。
        source = relation.origin
        if existing_id is not None:
            source = f"已有，{source}"
        elif relation.origin == "manual":
            source = "自己加的"
        arrow = "→" if relation.kind == "prereq" else "↔"
        item = QListWidgetItem(
            f"[{relation.kind_label}]　{relation.from_name} {arrow} {relation.to_name}"
            f"　（{source}）\n　　理由：{relation.reason}"
        )
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        )
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        item.setData(ROLE_RELATION, relation)
        item.setData(ROLE_EXISTING_ID, existing_id)
        self.list.addItem(item)

    def _known_signatures(self) -> set[tuple[str, str, str]]:
        """已经有的关系：**库里的 + 这次列表里已经列出来的**。

        「容易混」和「相关」这两种是**没有方向的**，所以 `signature()` 会把两端
        排序——不然库里已有 A↔B 时，手加 B↔A 会存成第二条（数据库的 UNIQUE
        只看方向），图上出现两条一模一样的线、详情里也列两遍。
        """
        signatures = set(self._existing_signatures)
        for index in range(self.list.count()):
            relation = self.list.item(index).data(ROLE_RELATION)
            if relation is not None:
                signatures.add(relation.signature())
        return signatures

    def _on_add(self) -> None:
        from_id = self.from_combo.currentData()
        to_id = self.to_combo.currentData()
        reason = self.reason_edit.text().strip()
        if from_id == to_id:
            # 自己连自己没有意义，而且图上画出来就是个点
            self.summary_label.setText("两个知识点选成同一个了，换一个。")
            return
        if not reason:
            self.summary_label.setText("理由得填——将来你自己还要靠它回忆为什么连这条线。")
            return

        relation = ProposedRelation(
            from_name=self._names[int(from_id)],
            to_name=self._names[int(to_id)],
            kind=str(self.kind_combo.currentData()),
            reason=reason,
            origin="manual",
        )
        if relation.signature() in self._known_signatures():
            self.summary_label.setText("这条关系已经有了（「容易混」和「相关」不分方向，反着写也算同一条）。")
            return

        self._add_item(relation, checked=True, existing_id=None)
        self.reason_edit.clear()
        self._update_summary()

    # ---------- 结果 ----------

    def _update_summary(self) -> None:
        added = len(self.approved())
        removed = len(self.removed_ids())
        text = f"确定后：新增 {added} 条"
        if removed:
            text += f"，删掉 {removed} 条"
        self.summary_label.setText(text)

    def approved(self) -> list[ProposedRelation]:
        """勾上的**新**关系（已有的不算，那些本来就在库里）。"""
        result: list[ProposedRelation] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            existing_id = item.data(ROLE_EXISTING_ID)
            if existing_id is not None or item.checkState() != Qt.CheckState.Checked:
                continue
            relation = item.data(ROLE_RELATION)
            if relation is not None:
                result.append(relation)
        return result

    def removed_ids(self) -> list[int]:
        """已经在库里、但被取消勾选的——这些要删掉。"""
        result: list[int] = []
        for index in range(self.list.count()):
            item = self.list.item(index)
            existing_id = item.data(ROLE_EXISTING_ID)
            if existing_id is None or item.checkState() == Qt.CheckState.Checked:
                continue
            result.append(int(existing_id))
        return result
