"""知识图谱面板：把考纲的 64 个知识点画成一张有关系的网。

三件事挤在一张图上（这是用户要的）：

1. **考点地图**——圆圈大小是题量，一眼看出考试的重头在哪块
2. **我的薄弱区**——圆圈颜色是你的正确率，红的就该去补
3. **知识点之间的关系**——连线是"先学这个""容易混"，AI 提、用户确认过才画

**画布坐标是虚拟的**（1000×1000 个"单位"），由 `QGraphicsView` 自己缩放到窗口大小。
不能直接用像素：圆圈大小和点之间的间距得是同一个单位，不然窗口一拉宽，
圆圈和间距的比例就变了，本来不叠的会叠上（算法那边见 `graph.py` 的注释）。

几个反复踩到的点，都写在对应位置：标签会盖住别的标签、点会盖住别的点、
面板里的 HTML 不能用写死的颜色（深色底上看不见）。
"""

from __future__ import annotations

import math
from html import escape

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QPolygonF,
)
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..ai import DeepSeekClient, RelationFinder, RelationProposal
from ..ai.relations import ProposedRelation
from ..config import Config
from ..db import Database
from ..graph import (
    LAYOUT_SEED,
    MASTERY_LABELS,
    RELATION_KINDS,
    GraphData,
    graph_signature,
    is_thin_sample,
    layout,
    load_graph,
)
from ..logging_setup import get_logger
from ..models import EXAM_TYPE_LABELS
from .relation_dialog import RelationDialog
from .theme import DARK, Theme
from .workers import TaskWorker, friendly_error

log = get_logger(__name__)

# 画布的虚拟边长。所有坐标、半径、字号都是"这个的几分之几"，见 graph.py。
SCENE_SIZE = 1000.0

# 知识点名字的标注字号（虚拟单位）。太大就会互相压住，太小看不清。
LABEL_FONT_SIZE = 13
# 标注最多显示几个字。考纲里有"预处理命令（#define 宏定义、#include 文件包含、条件编译）"
# 这种超长名字，整条画出来得占满半个屏幕。
LABEL_MAX_CHARS = 9

# 连线在虚拟单位下的粗细
EDGE_WIDTH = 2.4

# 章名标注的字号，以及最多显示几个字。
# 考纲的章名有"第3章 最简单的C程序设计——顺序程序设计"这么长，
# 整条写出来能横跨半个屏幕，把一串知识点名字压掉。
CHAPTER_FONT_SIZE = 17
CHAPTER_MAX_CHARS = 12


def mastery_color(theme: Theme, band: str) -> str:
    """掌握度档位 → 颜色。**颜色只从 theme 里取**，不写死——
    写死的深红在深色底上几乎看不见（踩过）。"""
    return {
        "unseen": theme.muted,
        "weak": theme.danger,
        "mid": theme.accent,
        "strong": theme.ok,
    }.get(band, theme.muted)


def edge_pen(theme: Theme, kind: str, highlighted: bool = True) -> QPen:
    """不同关系画成不同样子：**光靠颜色分不开**（色弱的人、投影仪上都白搭）。"""
    color = edge_color(theme, kind)
    pen = QPen(QColor(color))
    pen.setWidthF(EDGE_WIDTH)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    if kind == "confuse":
        pen.setStyle(Qt.PenStyle.DashLine)
    elif kind == "related":
        pen.setStyle(Qt.PenStyle.DotLine)
    if not highlighted:
        dimmed = QColor(color)
        dimmed.setAlpha(45)      # 选中某个点时，不相干的线淡下去
        pen.setColor(dimmed)
    return pen


def edge_color(theme: Theme, kind: str) -> str:
    return {"prereq": theme.accent, "confuse": theme.danger}.get(kind, theme.border)


def edge_mark(kind: str) -> str:
    """图例里那条线画成什么样（画不了真线，用字符示意）。"""
    return {"prereq": "──▶", "confuse": "╌╌╌", "related": "···"}.get(kind, "──")


class _NodeItem(QGraphicsEllipseItem):
    """一个知识点。点一下选中它。

    用 `QGraphicsEllipseItem` 而不是 `QGraphicsObject`：这里只需要"被点了通知一声"，
    为这个引入 QObject 那套信号反而绕。
    """

    def __init__(self, node, radius: float, on_click) -> None:
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node = node
        self.radius = radius
        self._on_click = on_click
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    def mousePressEvent(self, event) -> None:
        # **必须调 super()**：不然选中状态和场景的事件链都断了
        super().mousePressEvent(event)
        self._on_click(self.node.id)


class _EdgeItem(QGraphicsLineItem):
    """一条关系。点一下就能删（会先问一遍）。

    **为什么要专门弄个类**：`QGraphicsLineItem` 默认**不接受鼠标按下**——
    也就是说线画在那里，用户点它什么都不会发生。第一版就是这样，
    而图例上还写着"点它可删"：界面明说了一件做不到的事。
    用户会反复点几下，然后认为"删不掉"或者"软件坏了"。

    另外线很细，按实际的笔画宽度做命中判定太难点中，所以 `shape()`
    用一条加粗的笔画来判定——**看得见的是一条细线，点得中的是一条宽带子**。
    """

    # 命中判定用的宽度（场景单位）。比画出来的线粗得多，手指/鼠标才好点。
    HIT_WIDTH = 16.0

    def __init__(self, edge, start: QPointF, end: QPointF, on_click) -> None:
        super().__init__(start.x(), start.y(), end.x(), end.y())
        self.edge = edge
        self._on_click = on_click
        self.setAcceptHoverEvents(True)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        path.moveTo(self.line().p1())
        path.lineTo(self.line().p2())
        stroker = QPainterPathStroker()
        stroker.setWidth(self.HIT_WIDTH)
        return stroker.createStroke(path)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self._on_click(self.edge)

    def hoverEnterEvent(self, event) -> None:
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.unsetCursor()
        super().hoverLeaveEvent(event)


class _GraphView(QGraphicsView):
    """能滚轮缩放、能拖着走的画布。"""

    def __init__(self, scene: QGraphicsScene, parent=None) -> None:
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        # 缩放时"鼠标底下那个点不动"，不然一放大图就跑到画面外
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._zoomed = False

    def fit(self) -> None:
        """把整张图铺进窗口，并忘掉用户之前的缩放。"""
        self.resetTransform()
        self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._zoomed = False

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)
        self._zoomed = True

    def resizeEvent(self, event) -> None:
        """窗口变大变小时跟着缩放——**但用户手动缩放过就别动**，
        不然他刚放大看清某个点，窗口一变化又缩回去了。"""
        super().resizeEvent(event)
        if not self._zoomed:
            self.fit()


class KnowledgeGraphPanel(QWidget):
    """知识图谱。外部只要接 `practice_requested` 就能把某个知识点的题拿去刷。"""

    practice_requested = pyqtSignal(int)     # 知识点 id
    status_changed = pyqtSignal(str)

    def __init__(
        self,
        db: Database,
        config: Config,
        client: DeepSeekClient | None = None,
        parent: QWidget | None = None,
        theme: Theme = DARK,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._config = config
        self._client = client
        self._theme = theme

        self._data = GraphData(nodes=[], edges=[])
        self._positions: dict[int, tuple[float, float]] = {}
        self._signature: tuple | None = None
        self._selected: int | None = None
        self._nodes: dict[int, _NodeItem] = {}
        self._edge_items: list[_EdgeItem] = []
        self._arrow_items: list[tuple[int, object]] = []
        self._chapter_anchors: list[tuple[str, QPointF]] = []
        self._relayouts = 0
        self._worker: TaskWorker | None = None

        self._build_ui()
        self.refresh()

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top = QHBoxLayout()
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        top.addWidget(self.summary_label, stretch=1)

        self.find_button = QPushButton("让 AI 找关系")
        self.find_button.clicked.connect(self._on_find_relations)
        top.addWidget(self.find_button)

        self.manage_button = QPushButton("手动加 / 删关系")
        self.manage_button.clicked.connect(self._on_manage_relations)
        top.addWidget(self.manage_button)

        self.relayout_button = QPushButton("重新排布")
        self.relayout_button.clicked.connect(self._on_relayout)
        top.addWidget(self.relayout_button)

        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        top.addWidget(self.refresh_button)
        root.addLayout(top)

        root.addWidget(self._legend())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.scene = QGraphicsScene()
        self.scene.setSceneRect(0, 0, SCENE_SIZE, SCENE_SIZE)
        self.view = _GraphView(self.scene)
        # 画布底色跟着主题走。QSS 那条管界面边框，**场景自己的底色得在这儿设**——
        # 只靠 QSS 的话场景会画成默认的白色，深色界面上就是一块刺眼的白板。
        self.view.setBackgroundBrush(self._canvas_brush())
        splitter.addWidget(self.view)

        self.detail = QTextBrowser()
        self.detail.setOpenLinks(False)
        self.detail.anchorClicked.connect(self._on_anchor)
        splitter.addWidget(self.detail)
        # 图占大头。详情区给一个够读的宽度就行，不跟图抢地方。
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([600, 400])
        root.addWidget(splitter, stretch=1)

    def _canvas_brush(self) -> QBrush:
        """画布底色：跟别的面板同一个色和同一个透明度。

        **透明度也得跟上**：有背景图时别的面板是半透明的，画布要是实心的，
        那块图就会被切掉一个方块。
        """
        color = QColor(self._theme.surface)
        color.setAlphaF(min(1.0, max(0.0, self._theme.surface_alpha)))
        return QBrush(color)

    def _legend(self) -> QWidget:
        """图例。**颜色必须在图例里再说一遍**——光看图没人知道蓝的绿的是什么意思。"""
        label = QLabel(self._legend_html())
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        return label

    def _legend_html(self) -> str:
        theme = self._theme

        def dot(color: str, text: str) -> str:
            return (
                f'<span style="color:{color}">●</span> {escape(text)}'
            )

        circles = "　".join(
            dot(mastery_color(theme, band), MASTERY_LABELS[band])
            for band in ("unseen", "weak", "mid", "strong")
        )
        lines = "　".join(
            f'<span style="color:{edge_color(theme, kind)}">'
            f"{edge_mark(kind)}</span> {escape(name)}"
            for kind, name in RELATION_KINDS.items()
        )
        return (
            f"<small>圆圈大小 = 题量；颜色 = 你的正确率：{circles}<br>"
            f"连线 = 知识点关系：{lines}<br>"
            "点圆圈看这个知识点的情况；<b>点连线可以删掉它</b>（会先问一遍）</small>"
        )

    # ---------- 刷新 ----------

    def refresh(self) -> None:
        """重新读库、必要时重排、重画。**读不出来也要说人话**，不抛 traceback。"""
        try:
            data = load_graph(self._db)
        except Exception:
            log.exception("读知识图谱数据失败")
            self.summary_label.setText("读知识图谱的数据出了点问题，详见 logs/cstudy.log。")
            self.scene.clear()
            return

        self._data = data
        if not data.nodes:
            self.summary_label.setText("还没有考纲知识点。")
            self.detail.setHtml(
                "<p>库里还没有考纲。先跑一次 "
                "<code>scripts/seed_syllabus.py</code> 把考纲灌进来。</p>"
            )
            self.scene.clear()
            self._selected = None
            return

        # 选中的那个知识点可能已经没了（比如软件开着的时候重灌了考纲）。
        # **不清掉的话整张图会被淡成一片黑**——因为"高亮的那个"找不到，
        # 所有点都算作"跟它无关"。宁可回到"什么都没选"的状态。
        if self._selected is not None and data.node(self._selected) is None:
            log.info("选中的知识点（id=%s）已经不在考纲里了，取消选中", self._selected)
            self._selected = None

        signature = graph_signature(data)
        reshaped = signature != self._signature
        if reshaped or not self._positions:
            # **只有结构变了才重排。** 只是题量、正确率变了就沿用旧坐标——
            # 用户正对着图看呢，位置跳一下他就找不着刚才那块了。
            self._positions = layout(data)
            self._signature = signature

        self._redraw(refit=reshaped)
        self.summary_label.setText(self._summary())
        self._show_detail(self._selected)

    def _summary(self) -> str:
        data = self._data
        parts = [f"考纲 {len(data.nodes)} 个知识点，分 {len(data.by_chapter())} 章"]

        if data.edges:
            parts.append(f"已确认 {len(data.edges)} 条关系")
        else:
            parts.append("还没有任何关系——点「让 AI 找关系」让它提一批，你确认后才入库")

        practised = [node for node in data.nodes if node.answered]
        # 判据是"练过几道**不同的**题"，不是"做过多少次"——
        # 同一道题刷三遍不叫练到了，而这软件自己就在引导重刷
        thin = [node for node in data.nodes if is_thin_sample(node.distinct)]
        weak = [node for node in data.nodes if node.mastery == "weak"]
        if not practised:
            parts.append(
                "目前一个知识点的题都没做过，所以颜色还都是灰的——"
                "去「答题」页做几道，颜色就出来了"
            )
        else:
            parts.append(f"做过题的知识点 {len(practised)} 个")
            if weak:
                parts.append(f"其中偏弱 {len(weak)} 个（红的那些）")
            if thin:
                parts.append(f"另有 {len(thin)} 个只做过 1~2 道，样本太少，先按灰的算")

        if data.loose_questions:
            # 这 231 道题在图上画不出来。**必须说**，不然用户以为图上就是全部题库。
            parts.append(
                f"题库里还有 {data.loose_questions} 道题没挂到知识点上，画不进这张图"
            )
        return "　·　".join(parts)

    # ---------- 画 ----------

    def _redraw(self, refit: bool = True) -> None:
        self.scene.clear()
        self._nodes = {}
        self._edge_items = []
        self._arrow_items = []
        data = self._data
        peak = data.peak_questions

        positions = {
            node_id: QPointF(x * SCENE_SIZE, y * SCENE_SIZE)
            for node_id, (x, y) in self._positions.items()
        }

        self._draw_chapters(positions, peak)
        self._draw_edges(positions)
        self._draw_nodes(positions, peak)
        self._place_labels(positions, peak)
        self._highlight(self._selected)

        # **重排了才重新铺满窗口。** 只是数据刷新就别动用户的缩放——
        # 他刚放大看清某个知识点，一刷新又缩回去，很烦。
        if refit:
            self.view.fit()

    def _draw_chapters(self, positions: dict[int, QPointF], peak: int) -> None:
        """每章在它那一团上方写一个章名。**不画范围的圈。**

        试过三种画圈的写法，都不行，记在这儿免得又走回去：

        1. **包围盒里画椭圆**：盒子对角线留白太多，圈比实际大一圈，相邻两章压叠。
        2. **把每个圆圈并起来当边界**（`QPainterPath.simplified()`）：同章的点实际
           隔得挺开（约 195 个场景单位），圆圈半径才几十，怎么外扩都连不到一起，
           画出来是"一个点一个圈"。
        3. **按形心算最小包住半径**：每个圈大得离谱，十来个章互相压成一团。

        **根本原因是画不下**：算下来 10 个章各自的范围加起来有 0.9~1.1 个画布那么大，
        而章和章之间还得留空。所以边界这件事在地图上做不到。

        改成靠**位置聚拢 + 醒目的章名**表示归属：同章的点本来就挨在一起，
        眼睛自己会分组，章名一标就够清楚了。章名的位置**优先于知识点名**
        （见 `_place_labels`），因为它更少、也更要紧。
        """
        self._chapter_anchors = []
        for chapter, nodes in self._data.by_chapter().items():
            points = [positions[node.id] for node in nodes if node.id in positions]
            if not points:
                continue
            # 锚点取"这一团的顶边中点"，章名默认就写在这上面
            self._chapter_anchors.append(
                (
                    chapter,
                    QPointF(
                        (min(p.x() for p in points) + max(p.x() for p in points)) / 2,
                        min(p.y() for p in points),
                    ),
                )
            )

    def _place_labels(self, positions: dict[int, QPointF], peak: int) -> None:
        """把所有标注摆开。**摆不下会互相盖住，所以要挑位置。**

        第一版直接把名字写在圆圈正下方，渲染出来一堆名字糊成一团——
        下面的被上面的盖掉一半，等于没标。所以给每个名字试几个位置
        （下、上、右、左、更远的下…），挑第一个不跟已放好的打架的。

        **顺序有讲究**：先章名（少、表示的是大块结构），再知识点名。
        知识点名**按题量从大到小**摆——先安排重要的，重要的名字一定放得下。
        """
        # 圆圈本身也算"占位"，不然名字会压在别的圆圈上
        placed: list[QRectF] = [
            QRectF(
                point.x() - node.radius(peak) * SCENE_SIZE,
                point.y() - node.radius(peak) * SCENE_SIZE,
                node.radius(peak) * SCENE_SIZE * 2,
                node.radius(peak) * SCENE_SIZE * 2,
            )
            for node in self._data.nodes
            if (point := positions.get(node.id)) is not None
        ]

        chapter_font = QFont(QApplication.font())
        chapter_font.setPixelSize(CHAPTER_FONT_SIZE)
        chapter_font.setBold(True)
        for chapter, anchor in self._chapter_anchors:
            self._place_text(
                _shorten(chapter, CHAPTER_MAX_CHARS),
                chapter_font,
                _with_alpha(self._theme.accent, 215),
                anchor,
                0.0,
                placed,
                z=3,
                upward_only=True,
            )

        label_font = QFont(QApplication.font())
        label_font.setPixelSize(LABEL_FONT_SIZE)
        ordered = sorted(
            self._data.nodes, key=lambda node: node.question_count, reverse=True
        )
        for node in ordered:
            point = positions.get(node.id)
            if point is None:
                continue
            self._place_text(
                _shorten(node.name, LABEL_MAX_CHARS),
                label_font,
                self._theme.text,
                point,
                node.radius(peak) * SCENE_SIZE,
                placed,
                z=2,
            )

    def _place_text(
        self,
        text: str,
        font: QFont,
        color: str,
        point: QPointF,
        radius: float,
        placed: list[QRectF],
        z: float,
        upward_only: bool = False,
    ) -> None:
        """放一段文字，**并且真的加进场景**。

        （旧的写法就是漏了最后这一步，名字算了一堆位置却没画出来——
        测试全绿、界面上一个字都没有。所以这里写完位置立刻 addItem。）
        """
        item = QGraphicsSimpleTextItem(text)
        item.setFont(font)
        item.setBrush(QBrush(QColor(color)))
        rect = self._best_label_rect(
            point,
            radius,
            item.boundingRect().width(),
            item.boundingRect().height(),
            placed,
            upward_only=upward_only,
        )
        placed.append(rect)
        item.setPos(rect.left(), rect.top())
        item.setZValue(z)
        self.scene.addItem(item)

    def _draw_edges(self, positions: dict[int, QPointF]) -> None:
        """连线画在圆圈**下面**（zValue 负的），不然线会横穿圆圈盖住颜色。"""
        for edge in self._data.edges:
            start = positions.get(edge.from_id)
            end = positions.get(edge.to_id)
            if start is None or end is None:
                continue
            item = _EdgeItem(edge, start, end, self._on_edge_clicked)
            item.setPen(edge_pen(self._theme, edge.kind))
            item.setZValue(-1)
            item.setToolTip(
                f"{edge.kind_label}：{edge.reason}\n（点这条线可以删掉它）"
            )
            self.scene.addItem(item)
            self._edge_items.append(item)

            if edge.kind == "prereq":
                self._arrow_items.append((edge.id, self._draw_arrowhead(start, end)))

    def _draw_arrowhead(self, start: QPointF, end: QPointF):
        """前置关系带箭头——**没有箭头就看不出谁先谁后**，
        而"先学哪个"恰恰是这张图最要紧的信息。

        返回值要给调用方收着：选中某个点时，不相干的箭头也得跟着淡下去，
        不然图上会剩下一堆孤零零的蓝三角，看着像没画完。
        """
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        size = 16.0
        # 箭头画在靠近终点的地方，但不顶到圆心
        tip = QPointF(end.x() - math.cos(angle) * 26, end.y() - math.sin(angle) * 26)
        left = QPointF(
            tip.x() - math.cos(angle - 0.4) * size,
            tip.y() - math.sin(angle - 0.4) * size,
        )
        right = QPointF(
            tip.x() - math.cos(angle + 0.4) * size,
            tip.y() - math.sin(angle + 0.4) * size,
        )
        item = self.scene.addPolygon(
            QPolygonF([tip, left, right]),
            QPen(Qt.PenStyle.NoPen),
            QBrush(QColor(self._theme.accent)),
        )
        item.setZValue(-1)
        return item

    def _draw_nodes(self, positions: dict[int, QPointF], peak: int) -> None:
        for node in self._data.nodes:
            point = positions.get(node.id)
            if point is None:
                continue
            radius = node.radius(peak) * SCENE_SIZE
            item = _NodeItem(node, radius, self._on_node_clicked)
            item.setPos(point)
            item.setPen(QPen(QColor(self._theme.border), 1.5))
            item.setBrush(QBrush(QColor(mastery_color(self._theme, node.mastery))))
            item.setToolTip(
                f"{node.name}\n{node.chapter}\n题库 {node.question_count} 道\n{node.describe()}"
            )
            self.scene.addItem(item)
            self._nodes[node.id] = item

    @staticmethod
    def _best_label_rect(
        point: QPointF,
        radius: float,
        width: float,
        height: float,
        placed: list[QRectF],
        upward_only: bool = False,
    ) -> QRectF:
        """试几个位置，挑第一个不打架的。都不行就用第一个（宁可压着也不能不显示）。

        `upward_only` 是给章名用的：**章名只往上方找位置**。
        一开始章名也走那套"下/上/右/左"，结果是一旦上面和下面都被占，
        它就掉到右边去，正好压在一排知识点上（渲染出来才看见）。
        章名只往上一层层让，让不出去顶多是离自己那团远了点，不会盖住别人。
        """
        gap = 4.0
        if upward_only:
            # 章名只往上方找位置，而且**一层层往上让 + 左右错开**。
            # 只往上让不让左右挪的话，上面正好被别的标注占满时就只能压在别人身上
            # （实测真实考纲就剩那么一处）。
            candidates = [
                (point.x() - width / 2 + offset, point.y() - radius - gap - height * (1 + 0.75 * step))
                for step in range(6)
                for offset in (0.0, -width * 0.4, width * 0.4)
            ]
        else:
            # **一圈一圈往外试**：先贴着自己试八个方向，都不行再往外挪一圈。
            # 只用"下/上/右/左 + 更下/更上"六个位置时，真实考纲（64 个知识点）
            # 实测有 13 对名字互相压住——正好都在题最多的第 3/4/5/6/8 章，
            # 而那些地方恰恰最该看清。八个方向 × 三圈基本能全部找着地方，
            # 而且顺序是"近的优先"，名字不会被扔到离自己很远的地方。
            candidates = []
            for ring in (1, 2, 3, 4):
                reach = (radius + gap) * ring
                candidates.extend(
                    [
                        (point.x() - width / 2, point.y() + reach),                 # 下
                        (point.x() - width / 2, point.y() - reach - height),        # 上
                        (point.x() + reach, point.y() - height / 2),                # 右
                        (point.x() - reach - width, point.y() - height / 2),        # 左
                        (point.x() + reach * 0.8, point.y() + reach * 0.8),         # 右下
                        (point.x() - reach * 0.8 - width, point.y() + reach * 0.8), # 左下
                        (point.x() + reach * 0.8, point.y() - reach * 0.8 - height),# 右上
                        (point.x() - reach * 0.8 - width, point.y() - reach * 0.8 - height),
                    ]
                )
        for left, top in candidates:
            # 夹进画布：跑出边界的字会被切掉，而画布的边界就是能滚到的边界，
            # 用户再怎么拖也看不到剩下的那半个字。
            left = min(max(4.0, left), SCENE_SIZE - width - 4.0)
            top = min(max(4.0, top), SCENE_SIZE - height - 4.0)
            rect = QRectF(left, top, width, height)
            if not any(rect.intersects(other) for other in placed):
                return rect
        return QRectF(
            min(max(4.0, candidates[0][0]), SCENE_SIZE - width - 4.0),
            min(max(4.0, candidates[0][1]), SCENE_SIZE - height - 4.0),
            width,
            height,
        )

    # ---------- 交互 ----------

    def _on_node_clicked(self, node_id: int) -> None:
        self._selected = node_id
        self._highlight(node_id)
        self._show_detail(node_id)

    def _on_edge_clicked(self, edge) -> None:
        """点图上的连线就能删掉这条关系。

        **要问一遍**：线细、又跟别的东西挨着，点错的概率不低，
        而删掉的关系不会自己回来（得重跑 AI 或者自己再加一遍）。
        """
        answer = QMessageBox.question(
            self,
            "删掉这条关系？",
            f"「{self._node_name(edge.from_id)}」"
            f"{' → ' if edge.kind == 'prereq' else ' ↔ '}"
            f"「{self._node_name(edge.to_id)}」（{edge.kind_label}）\n\n"
            f"理由：{edge.reason}\n\n"
            "删掉之后这条线就从图上没了。要加回来得重新让 AI 找，或者自己加。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._delete_relation_by_id(edge.id)

    def _node_name(self, node_id: int) -> str:
        node = self._data.node(node_id)
        return node.name if node else f"（对不上的知识点 {node_id}）"

    def _highlight(self, node_id: int | None) -> None:
        """选中一个点时，**跟它没关系的线和点淡下去**——
        一个点连着哪些别的点，这是"关系图"最该一眼看出来的东西。"""
        if node_id is None:
            neighbours = set()
        else:
            neighbours = self._data.neighbours(node_id) | {node_id}

        for item in self._edge_items:
            edge = item.edge
            related = node_id is None or node_id in (edge.from_id, edge.to_id)
            item.setPen(edge_pen(self._theme, edge.kind, highlighted=related))

        for edge_id, arrow in self._arrow_items:
            edge = next((e for e in self._data.edges if e.id == edge_id), None)
            related = (
                node_id is None
                or edge is None
                or node_id in (edge.from_id, edge.to_id)
            )
            color = QColor(self._theme.accent)
            if not related:
                color.setAlpha(45)
            arrow.setBrush(QBrush(color))

        for other_id, item in self._nodes.items():
            node = item.node
            active = node_id is None or other_id in neighbours
            color = QColor(mastery_color(self._theme, node.mastery))
            if not active:
                # 淡下去，但**别淡到看不见**：一次点击就发生在打开这一页之后不久，
                # 全淡成黑点的话用户就丢掉了整张图的印象。0.37 是"看得出还有别的点、
                # 但眼睛先落在高亮那几个上"的位置。
                color.setAlpha(95)
            item.setBrush(QBrush(color))
            item.setPen(
                QPen(
                    QColor(self._theme.accent if other_id == node_id else self._theme.border),
                    3.0 if other_id == node_id else 1.5,
                )
            )

    def _show_detail(self, node_id: int | None) -> None:
        if node_id is None:
            self.detail.setHtml(
                "<p>点左边任何一个圆圈，这里会显示这个知识点的情况："
                "考过多少题、考什么题型、你做过几道对几道，以及它牵扯到的关系。</p>"
            )
            return
        node = self._data.node(node_id)
        if node is None:
            self.detail.setHtml("")
            return

        theme = self._theme
        parts = [f"<h3>{escape(node.name)}</h3>"]
        parts.append(f"<p>{escape(node.chapter)}</p>")
        parts.append("<h4>题库</h4><ul>")
        parts.append(f"<li>共 <b>{node.question_count}</b> 道</li>")

        types = self._db.exam_types_of_point(node_id)
        if types:
            detail = "、".join(
                f"{escape(EXAM_TYPE_LABELS.get(kind, kind))} {count} 道"
                for kind, count in types.items()
            )
            parts.append(f"<li>题型分布：{detail}</li>")
        parts.append("</ul>")

        parts.append("<h4>你的情况</h4>")
        if node.answered:
            band = node.mastery
            parts.append(
                f'<p><span style="color:{mastery_color(theme, band)}">'
                f"{escape(MASTERY_LABELS[band])}</span>：{escape(node.describe())}</p>"
            )
        else:
            parts.append(
                f'<p><span style="color:{theme.muted}">还没练到</span>：'
                "这个知识点的题你一道都没做过。</p>"
            )

        if node.question_count:
            parts.append(
                f'<p><a href="practice:{node.id}" style="color:{theme.accent}">'
                f"只练这个知识点（{node.question_count} 道）</a></p>"
            )

        parts.append("<h4>牵涉的关系</h4>")
        relations = self._data.relations_of(node_id)
        if not relations:
            parts.append(
                "<p>还没有跟别的知识点连上线。点「让 AI 找关系」可以让它提一批。</p>"
            )
        else:
            parts.append("<ul>")
            for edge in relations:
                other_id = edge.to_id if edge.from_id == node_id else edge.from_id
                other = self._data.node(other_id)
                other_name = other.name if other else "（对不上）"
                arrow = "→" if edge.from_id == node_id else "←"
                mark = "　（先学这个）" if edge.kind == "prereq" else f"　（{edge.kind_label}）"
                parts.append(
                    f"<li>{arrow} {escape(other_name)}{escape(mark)}"
                    f'<br><small>{escape(edge.reason)}'
                    f'　<a href="del:{edge.id}" style="color:{theme.muted}">删掉不练</a>'
                    "</small></li>"
                )
            parts.append("</ul>")
            parts.append(
                "<p><small>「先学这个」是方向：「→」表示要先学当前这个，"
                "才学得懂箭头指的那个。</small></p>"
            )

        self.detail.setHtml("".join(parts))

    def _on_anchor(self, url) -> None:
        href = url.toString()
        if href.startswith("del:"):
            self._delete_relation(href)
        elif href.startswith("practice:"):
            self._practice(href)

    def _delete_relation(self, href: str) -> None:
        try:
            edge_id = int(href.split(":", 1)[1])
        except (ValueError, IndexError):
            log.warning("图谱里点了一个看不懂的链接：%s", href)
            return
        self._delete_relation_by_id(edge_id)

    def _delete_relation_by_id(self, edge_id: int) -> None:
        try:
            self._db.delete_relation(edge_id)
        except Exception as exc:
            log.exception("删关系失败")
            QMessageBox.warning(self, "没删掉", f"删关系时出了点问题：{friendly_error(exc)}")
            return
        # 删线要重排（连线影响布局），所以把指纹清掉
        self._signature = None
        self.refresh()
        self.status_changed.emit("已删掉一条关系")

    def _practice(self, href: str) -> None:
        try:
            point_id = int(href.split(":", 1)[1])
        except (ValueError, IndexError):
            log.warning("图谱里点了一个看不懂的链接：%s", href)
            return
        self.practice_requested.emit(point_id)

    def _on_relayout(self) -> None:
        """换个排法。

        **必须换种子，不然这就是个空按钮**：布局是确定性的（同一份数据永远
        算出一样的位置，这是"位置稳定"那条设计），所以按原来的种子重排等于
        什么都没做——用户点了没反应，会以为按钮坏了。换个种子才真的动，
        代价是他记住的方位会失效，所以状态栏里说一句。
        """
        self._relayouts += 1
        self._positions = layout(self._data, seed=LAYOUT_SEED + self._relayouts)
        self._signature = graph_signature(self._data)
        self._redraw()
        self.status_changed.emit("换了个排法——位置跟刚才不一样了，你记的方位会失效")

    # ---------- 关系管理 ----------

    def _existing_signatures(self) -> set[tuple[str, str, str]]:
        """库里已确认的关系，转成跟 AI 提案同一套判重标记。

        判重**必须走同一个 `signature()`**：`propose()` 拿它认"这条已经有了"，
        界面拿它列"已有"的行。两边各写一套，迟早会对不上——
        表现就是 AI 提了一条明明已经有的关系，用户还得再确认一遍。
        """
        return {
            ProposedRelation(
                from_name=str(row["from_name"]),
                to_name=str(row["to_name"]),
                kind=str(row["kind"]),
                reason="",
            ).signature()
            for row in self._db.list_relations()
        }

    def _on_find_relations(self) -> None:
        """让 AI 提一批关系，然后**弹窗让用户逐条过目**。"""
        if self._busy():
            QMessageBox.information(self, "正在忙", "上一次找关系还没跑完，稍等一下。")
            return
        if self._client is None:
            QMessageBox.warning(
                self, "用不了", "这个功能要调 AI，但没拿到 AI 客户端，请重启一下软件。"
            )
            return

        grouped: dict[str, list[str]] = {}
        for node in self._data.nodes:
            grouped.setdefault(node.chapter, []).append(node.name)

        finder = RelationFinder(self._client, self._config)
        self.find_button.setEnabled(False)
        self.status_changed.emit("正在让 AI 找知识点之间的关系…")

        self._worker = TaskWorker(
            finder.propose, grouped, existing=self._existing_signatures()
        )
        self._worker.progress.connect(self.status_changed.emit)
        self._worker.finished_ok.connect(self._on_proposal)
        self._worker.failed.connect(self._on_find_failed)
        self._worker.finished.connect(lambda: self.find_button.setEnabled(True))
        self._worker.start()

    def _on_proposal(self, proposal: RelationProposal) -> None:
        if not proposal.ok:
            self.status_changed.emit("找关系失败")
            # 走 friendly_error：AIError / DataError 这些消息本来就是写给人看的中文，
            # 原样显示；别的异常会被包成"出了预期之外的错误 + 日志在哪儿"，
            # 不会把英文技术信息直接摆到用户面前
            message = (
                friendly_error(proposal.error_exc)
                if proposal.error_exc is not None
                else (proposal.error or "AI 那边没给结果。")
            )
            QMessageBox.warning(self, "没找成", f"让 AI 找关系时出了点问题：\n\n{message}")
            return

        # **提了多少、丢了什么，进日志。** 用户看到"留下 12 条"时要能核对
        # AI 到底提了多少、被挡掉的是什么——静默丢弃会让他以为就这么几条。
        log.info("找关系结果：%s", proposal.describe().replace("\n", " "))
        self.status_changed.emit(
            f"AI 提了 {len(proposal.relations)} 条关系，等你在弹窗里确认"
            + (f"（另有 {proposal.dropped_count} 条没敢配）" if proposal.dropped else "")
        )

        # 挡掉的和认回名字的都要**摆到弹窗里**，不能只写日志——
        # 用户得知道"AI 其实还提了这些，是对不上名字才没列出来"。
        # 两者分开传：认回名字的那几条是**列进来了的**，跟"没列进来"混在一起
        # 会让人以为它没被采纳。
        self._review_proposals(
            proposal.relations,
            dropped=proposal.dropped,
            corrected=proposal.corrected,
        )

    def _on_find_failed(self, message: str) -> None:
        self.status_changed.emit("找关系失败")
        QMessageBox.warning(self, "没找成", f"让 AI 找关系时出了点问题：\n\n{message}")

    def _review_proposals(
        self,
        proposals: list[ProposedRelation],
        dropped: list[str] | None = None,
        corrected: list[str] | None = None,
    ) -> None:
        dialog = RelationDialog(
            self._db,
            theme=self._theme,
            proposals=proposals,
            dropped=dropped or [],
            corrected=corrected or [],
            parent=self,
        )
        if not dialog.exec():
            self.status_changed.emit("这次提的关系没有入库（你取消了）")
            return
        self._apply_and_report(dialog)

    def _on_manage_relations(self) -> None:
        """不跑 AI，直接开那个对话框看 / 自己加 / 删。

        **找关系还在跑的时候不给开**：那会变成两个模态框叠在一起、
        同时改同一批关系，谁先确认谁后确认结果还不一样。
        """
        if self._busy():
            QMessageBox.information(
                self, "正在忙", "AI 正在找关系，等它出结果了再改关系。"
            )
            return
        dialog = RelationDialog(self._db, theme=self._theme, proposals=[], parent=self)
        if not dialog.exec():
            return
        self._apply_and_report(dialog)

    def _apply_and_report(self, dialog: RelationDialog) -> None:
        """把用户在对话框里勾的结果写进库，并如实报出加了几条、删了几条。

        每一行都单独 try：**一条写不进去不该让整批都白勾**——
        那用户得重新在几十条里再勾一遍。
        """
        added = 0
        failed = 0
        for relation in dialog.approved():
            try:
                added += self._db.save_relations(
                    [
                        {
                            "from_id": self._name_to_id(relation.from_name),
                            "to_id": self._name_to_id(relation.to_name),
                            "kind": relation.kind,
                            "reason": relation.reason,
                            # 是 AI 提的还是自己敲的，分开记——
                            # 以后看这张图时要能知道哪条该多留个心眼
                            "origin": relation.origin,
                        }
                    ]
                )
            except Exception:
                failed += 1
                log.exception("存一条关系失败：%s", relation.signature())

        removed = 0
        for edge_id in dialog.removed_ids():
            try:
                removed += int(self._db.delete_relation(edge_id))
            except Exception:
                failed += 1
                log.exception("删一条关系失败：edge_id=%s", edge_id)

        text = f"关系已更新：新增 {added} 条"
        if removed:
            text += f"，删掉 {removed} 条"
        if failed:
            text += f"（有 {failed} 条没写进去，详见 logs/cstudy.log）"
        self.status_changed.emit(text)

        # 连线变了，布局也要跟着重排（连线本来就影响摆放）
        self._signature = None
        self.refresh()

    def _name_to_id(self, name: str) -> int:
        """知识点名字 → id。名字是弹窗里选的，理论上一定找得到；
        找不到就抛，让上层记日志，不要硬塞一个错的 id 进去。"""
        for node in self._data.nodes:
            if node.name == name:
                return node.id
        raise ValueError(f"考纲里找不到知识点：{name}")

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()


def _shorten(text: str, limit: int = 0) -> str:
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        return text[:limit] + "…"
    return text


def _with_alpha(color: str, alpha: int) -> str:
    """`#rrggbb` + 透明度 → `#aarrggbb`。Qt 的样式表颜色支持这种八位写法。"""
    text = str(color).lstrip("#")
    if len(text) != 6:
        return color
    return f"#{alpha:02x}{text}"
