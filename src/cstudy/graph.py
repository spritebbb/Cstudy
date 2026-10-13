"""知识图谱：节点、连线，以及把它们摆到画面上的算法。

**这个模块刻意不 import Qt**——跟 `grading.py` 一样，算得对不对是最好测、
也最必须测的那部分（摆歪了、颜色判错了，测试里全抓得住），
跟界面解耦才测得动。界面只负责把这里算出来的坐标和档位画出来。

三条设计上的取舍，都写在对应函数的注释里：

1. **"掌握度"要样本够才敢下判断**（`mastery_band`）——做过 1 道题就标红，
   用户会去补一个其实不弱的地方，白费功夫。
2. **布局是确定性的**（`layout`）——同一张图每次画出来位置一样。
   每次打开都换一套位置的图，用户记不住"我的薄弱区在右上角"。
3. **连线有方向、有强弱**（`RELATION_KINDS` / `EDGE_PULL`）——"先学 A 才学得懂 B"
   和"这俩容易混"不是一回事，画成一个样子等于没说。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .logging_setup import get_logger

log = get_logger(__name__)

# ---------- 关系类型 ----------

# 连线只有这三种。**不做"自定义类型"**——类型一多，画出来就分不清，
# 而用户要的是"扫一眼看出结构"，不是一条条读。
RELATION_KINDS: dict[str, str] = {
    "prereq": "先学这个",     # 有方向：from 是前置，to 是后续
    "confuse": "容易混",      # 无方向
    "related": "相关",        # 无方向，最弱
}

# 画线时线被拉得有多紧。前置关系应该挨得近，笼统的"相关"松一些——
# 全都一样紧的话，一眼看不出哪条是真结构、哪条只是沾边。
EDGE_PULL: dict[str, float] = {"prereq": 1.0, "confuse": 0.6, "related": 0.35}


def describe_relation(kind: str, from_name: str, to_name: str) -> str:
    """一条关系写成给人看的一句话。"""
    if kind == "prereq":
        return f"{from_name} → {to_name}（先学这个，再学那个）"
    return f"{from_name} ↔ {to_name}（{RELATION_KINDS.get(kind, kind)}）"


# ---------- 掌握度 ----------

# 做过几道题才敢说"这个知识点你弱"。**低于这个数一律按"还没练到"算**：
# 做 1 道错 1 道就是 0%，标成大红色，用户会跑去补一个其实不弱的地方。
# 考纲上 64 个知识点，这种假红点一多，整张图就没法看了。
MIN_SAMPLE = 3

MASTERY_BANDS = ("unseen", "weak", "mid", "strong")

MASTERY_LABELS: dict[str, str] = {
    "unseen": "还没练到",
    "weak": "偏弱",
    "mid": "一般",
    "strong": "稳",
}

WEAK_ACCURACY = 60      # 低于这个算偏弱（跟学情页同一个线）
STRONG_ACCURACY = 80    # 到这儿算稳


def mastery_band(answered: int, correct: int) -> str:
    """由作答情况定一个档位。**样本不够就说"不知道"**，不硬给结论。"""
    if answered < MIN_SAMPLE:
        return "unseen"
    accuracy = correct * 100 / answered
    if accuracy < WEAK_ACCURACY:
        return "weak"
    if accuracy < STRONG_ACCURACY:
        return "mid"
    return "strong"


def is_thin_sample(distinct_questions: int) -> bool:
    """练过，但**练的题太少**，不够下结论。

    注意判据是"练过几道**不同的**题"，不是"做过多少次"。
    同一道题反复做三遍不算练到了这个知识点——而这软件自己就在引导重刷
    （「只练这个知识点」、错题本复习），所以这个区别真会碰上。

    这种点在图上跟"完全没练过"一样是灰的，但**总结里要单独报出来**——
    否则用户会以为这些知识点一道题都没有，其实只是还没刷到。
    """
    return 0 < distinct_questions < MIN_SAMPLE


# 章名缺失时的兜底。考纲里每个知识点都写了章，但这是"系统边界之外的输入"，
# 空章名不该让分组和画图崩掉——单独归成一组，界面上也看得出来它没写章。
CHAPTER_FALLBACK = "（没写章节）"

# ---------- 节点与连线 ----------
#
# **下面这些长度单位和坐标是同一套**：都是"画布边长的几分之几"。
# 界面拿到坐标后乘上画布边长，就成了像素。
#
# 这么定是有原因的：圆圈大小和点之间的间距**必须用同一个单位**，
# 不然窗口一大一小就会一会儿挤成一坨一会儿稀疏得找不到线。
# 早先这两套是分开的（坐标归一化、半径写死像素），结果窗口一拉宽
# 圆圈就叠在一起了。

# 圆圈大小：题量最多的知识点画多大，以及最小能小到多少。
# 用面积（开方）而不是直径直接成比例——题量差 10 倍时，直径差 10 倍会把图撑爆。
MAX_RADIUS = 0.020
MIN_RADIUS = 0.010

# 两个圆心至少离这么远。**必须大于 2 倍的 MAX_RADIUS**（0.040），
# 否则两个大题量的知识点会叠在一起。有单测盯着这条。
#
# 这个数不是随便定的：64 个知识点要按章分成 10 团、摆进一个方框里，
# 团和团之间也要留出这个间距。算下来 0.048 是"塞得下"和"圆圈大小看得清"
# 之间的位置——再大就摆不开（相邻两章会叠），再小圆圈就小到看不清。
# 改了它就得**重新渲染看一眼**，光算不出来的。
MIN_GAP = 0.048


@dataclass(frozen=True)
class PointNode:
    """图上的一个点 = 一个考纲知识点。

    做题情况有**三个数**，不能混：

    - `answered` / `correct`：作答**次数**和其中对了几次（跟学情页同一个口径）
    - `distinct`：练过几道**不同的**题

    为什么要分开：同一道题反复做三遍是一道题，不是三道。「练过几道题」
    才是"样本够不够"的判据，而正确率按次数算才跟学情页对得上。
    混成一个数的话，节点详情会同时显示"共 4 道"和"做过 8 道"，看着自相矛盾。
    """

    id: int
    name: str
    chapter: str
    question_count: int
    answered: int
    correct: int
    distinct: int = 0

    @property
    def accuracy(self) -> int | None:
        """正确率百分比。**没做过返回 None，不返回 0**——
        "没练过"和"练了全错"是两回事，混在一起显示会误导。"""
        if not self.answered:
            return None
        return round(self.correct * 100 / self.answered)

    @property
    def mastery(self) -> str:
        return mastery_band(self.answered, self.correct)

    def describe(self) -> str:
        """做题情况写成一句话。**三个数都说出来**，别让用户自己去对。"""
        if not self.answered:
            return "还没做过这里的题"
        text = f"做过 {self.answered} 次"
        if self.distinct:
            text += f"（覆盖 {self.distinct} 道题）"
        text += f"，对 {self.correct} 次（{self.accuracy}%）"
        if is_thin_sample(self.distinct):
            text += f"——只练过 {self.distinct} 道题，还看不出强弱"
        return text

    def radius(self, peak: int) -> float:
        """按题量算圆圈半径。题量最多的画 MAX_RADIUS，其余按面积缩。"""
        if peak <= 0:
            return MIN_RADIUS
        share = math.sqrt(max(0, self.question_count) / peak)
        return MIN_RADIUS + (MAX_RADIUS - MIN_RADIUS) * share


@dataclass(frozen=True)
class PointEdge:
    """图上的一条线 = 一条用户确认过的知识点关系。"""

    id: int
    from_id: int
    to_id: int
    kind: str
    reason: str
    origin: str

    @property
    def kind_label(self) -> str:
        return RELATION_KINDS.get(self.kind, self.kind)

    @property
    def pull(self) -> float:
        return EDGE_PULL.get(self.kind, 0.3)


@dataclass
class GraphData:
    """一张完整的图。"""

    nodes: list[PointNode]
    edges: list[PointEdge]
    # 没挂到任何知识点上的题——图上画不出来，但**必须报出来**，
    # 不然用户会以为那张图覆盖了全部题库
    loose_questions: int = 0

    @property
    def peak_questions(self) -> int:
        return max((node.question_count for node in self.nodes), default=0)

    def node(self, node_id: int) -> PointNode | None:
        for item in self.nodes:
            if item.id == node_id:
                return item
        return None

    def neighbours(self, node_id: int) -> set[int]:
        """跟这个点直接相连的所有点（不管方向）。"""
        linked: set[int] = set()
        for edge in self.edges:
            if edge.from_id == node_id:
                linked.add(edge.to_id)
            elif edge.to_id == node_id:
                linked.add(edge.from_id)
        return linked

    def relations_of(self, node_id: int) -> list[PointEdge]:
        return [
            edge for edge in self.edges
            if edge.from_id == node_id or edge.to_id == node_id
        ]

    def by_chapter(self) -> dict[str, list[PointNode]]:
        """按章分组，**保持考纲顺序**（dict 从 3.7 起保序）。"""
        grouped: dict[str, list[PointNode]] = {}
        for node in self.nodes:
            grouped.setdefault(node.chapter or CHAPTER_FALLBACK, []).append(node)
        return grouped


# ---------- 读取 ----------


def load_graph(db) -> GraphData:
    """从库里读出整张图。读不出来就抛——调用方负责说人话。"""
    nodes = [
        PointNode(
            id=int(row["id"]),
            name=str(row["name"]),
            chapter=str(row["chapter"] or "") or CHAPTER_FALLBACK,
            question_count=int(row["question_count"] or 0),
            answered=int(row["answered"] or 0),
            correct=int(row["correct"] or 0),
            distinct=int(row["distinct_questions"] or 0),
        )
        for row in db.point_stats()
    ]

    # `list_relations()` 用的是 INNER JOIN，两端对不上（知识点被删了）的行
    # 在 SQL 那层就查不出来——**所以这里不需要再筛一遍**。
    # 那种行正常也不该存在：外键是 ON DELETE CASCADE，删知识点会带走连线。
    edges = [
        PointEdge(
            id=int(row["id"]),
            from_id=int(row["from_id"]),
            to_id=int(row["to_id"]),
            kind=str(row["kind"]),
            reason=str(row["reason"] or ""),
            origin=str(row["origin"] or "ai"),
        )
        for row in db.list_relations()
    ]

    loose = int(
        db.conn.execute(
            "SELECT COUNT(*) FROM questions WHERE point_id IS NULL"
        ).fetchone()[0]
    )
    return GraphData(nodes=nodes, edges=edges, loose_questions=loose)


def graph_signature(data: GraphData) -> tuple:
    """图结构的指纹。用来判断"要不要重排"。

    只跟**有哪些点、每个点属于哪一章、有哪些线**有关，跟题量、正确率无关——
    那些变了不该让整张图跳一下位置，用户正对着看呢。

    **章必须算进来**：换考纲重灌之后知识点 id 通常没变、只是章的归属变了，
    指纹不变的话图就不会重排，结果是知识点画在旧章的地盘上、
    跟旁边的章名对不上——用户会得出"这一章的点散在别处"的错误印象。
    """
    return (
        tuple((node.id, node.chapter) for node in data.nodes),
        tuple(sorted((edge.from_id, edge.to_id, edge.kind) for edge in data.edges)),
    )


# ---------- 布局 ----------
#
# **不跑全图力导向。** 试过的做法是把 64 个点丢进一个力导向场里，靠"往本章中心
# 拽"的引力让同章抱团——问题是这个引力跟点与点之间的斥力哪个大，很难调：
# 调小了各章混成一团毛线，调大了所有点被钉死在章中心变成十个圆点。
# 而且收敛结果跟初始状态纠缠，同一张图稍微改一点就整张重排。
#
# 改成**分两层的确定性做法**：
#   1. 各章的中心摆在一个椭圆上，按考纲顺序绕一圈（位置固定，算出来的）
#   2. 每章内部**单独**跑一次小规模力导向，只看章内的关系
#
# 好处是每章的形状只由它自己的知识点和关系决定，改一个章不影响别的章，
# 而且每个章的人数最多 10 个，几十次迭代就收敛，结果稳定。

# 固定的随机种子。同一张图每次画成一个样——用户记的是"我的薄弱区在右上角那团"，
# 位置一跳这个记忆就废了。
LAYOUT_SEED = 20261013

# 章中心那个椭圆。宽一点（窗口是横的）。
RING_WIDTH = 0.86
RING_HEIGHT = 0.80

# 一章占多大的地盘（半径）。人多的章占大一点，但**不给超过这个上限**——
# 十来个章挤在一个椭圆上，谁的地盘大了就会盖住邻居。
MIN_SPREAD = 0.050
MAX_SPREAD = 0.105

SMALL = 1e-6


def _chapter_centers(chapters: list[str]) -> dict[str, tuple[float, float]]:
    """把各章的中心摆在一个椭圆上，**按考纲顺序**绕一圈。

    摆成一圈而不是随便散开：教材是线性的，走完一圈看到的就是
    "第 1 章 → 第 10 章"，顺序本身也有信息量（前面的不懂，后面全塌）。
    从正上方开始顺时针排，这样第 1 章永远在顶上——位置可预测。
    """
    count = max(1, len(chapters))
    centers: dict[str, tuple[float, float]] = {}
    for index, chapter in enumerate(chapters):
        angle = -math.pi / 2 + 2 * math.pi * index / count
        centers[chapter] = (
            0.5 + RING_WIDTH / 2 * math.cos(angle),
            0.5 + RING_HEIGHT / 2 * math.sin(angle),
        )
    return centers


def _spread(count: int, peak: int) -> float:
    """这一章的地盘半径。按人数开方缩放——线性的会让 10 人的章盖住 5 人的章。"""
    if peak <= 0:
        return MIN_SPREAD
    share = math.sqrt(count / peak)
    return MIN_SPREAD + (MAX_SPREAD - MIN_SPREAD) * share


def _layout_locally(count: int, links: list[tuple[int, int, float]], seed: int):
    """一章之内，按章内的关系把点摆开。返回 (count, 2) 的数组，坐标在 0~1。

    就是一个小号的力导向：点与点相斥、有关系的相吸。
    **章内一条关系都没有时也不能摆成一坨**，所以斥力是常开的。
    """
    rng = np.random.default_rng(seed)
    pos = rng.random((count, 2)) * 0.6 + 0.2

    if count == 1:
        return np.array([[0.5, 0.5]])

    ideal = math.sqrt(1.0 / count)      # 理想间距
    iterations = 140

    for step in range(iterations):
        temperature = 0.09 * (1 - step / iterations) + 0.002

        delta = pos[:, None, :] - pos[None, :, :]
        dist = np.linalg.norm(delta, axis=2)
        np.fill_diagonal(dist, 1.0)
        dist = np.maximum(dist, SMALL)
        push = (ideal * ideal / dist)[:, :, None] * (delta / dist[:, :, None])
        np.fill_diagonal(push[:, :, 0], 0.0)
        np.fill_diagonal(push[:, :, 1], 0.0)
        disp = push.sum(axis=1)

        for left, right, pull in links:
            delta = pos[left] - pos[right]
            gap = max(float(np.linalg.norm(delta)), SMALL)
            force = (gap * gap / ideal) * pull * 0.6
            direction = delta / gap
            disp[left] -= force * direction
            disp[right] += force * direction

        length = np.maximum(np.linalg.norm(disp, axis=1)[:, None], SMALL)
        pos += disp / length * np.minimum(length, temperature)
        pos = np.clip(pos, 0.0, 1.0)

    # 铺满这一章自己的小方块，好让所有章都利用上自己的地盘
    low, high = pos.min(axis=0), pos.max(axis=0)
    span = np.maximum(high - low, SMALL)
    return (pos - low) / span


def layout(data: GraphData, seed: int = LAYOUT_SEED) -> dict[int, tuple[float, float]]:
    """算每个点摆在哪儿。返回 {知识点 id: (x, y)}，坐标归一化到 0~1。

    边界情况都得能跑：0 个点、1 个点、某章只有 1 个点、一条关系都没有。
    """
    if not data.nodes:
        return {}
    if len(data.nodes) == 1:
        return {data.nodes[0].id: (0.5, 0.5)}

    grouped = data.by_chapter()
    chapters = list(grouped.keys())
    centers = _chapter_centers(chapters)
    peak = max(len(items) for items in grouped.values())

    # 两端都在同一章里的关系，才参与章内布局；跨章的靠画长线表示。
    positions: dict[int, list[float]] = {}
    for chapter_index, (chapter, nodes) in enumerate(grouped.items()):
        inside = {node.id: index for index, node in enumerate(nodes)}
        links: list[tuple[int, int, float]] = []
        for edge in data.edges:
            left, right = inside.get(edge.from_id), inside.get(edge.to_id)
            if left is None or right is None or left == right:
                continue
            links.append((left, right, edge.pull))

        # 每章一个独立的种子，这样**加一个知识点不会把别的章重排**
        local = _layout_locally(len(nodes), links, seed + chapter_index * 977)
        radius = _spread(len(nodes), peak)
        cx, cy = centers[chapter]

        for index, node in enumerate(nodes):
            # 章内坐标 0~1 → 以章中心为圆心摊开；只有一个点时正好落在中心
            offset_x = (local[index, 0] - 0.5) * 2 * radius
            offset_y = (local[index, 1] - 0.5) * 2 * radius
            positions[node.id] = [
                min(0.985, max(0.015, cx + offset_x)),
                min(0.985, max(0.015, cy + offset_y)),
            ]

    closest = _relax_overlaps(positions, data.nodes)
    # 门槛留得松一点：贴着画布边界的点推不动，收敛后总差那么一丁点
    # （真实考纲上是 0.0480 差到 0.0479，差 0.2%），那种毛毛雨不值得报警——
    # 报了反而教会用户忽略这条警告。真正要报的是**看得出来叠在一起**的那种。
    if closest < MIN_GAP * 0.8:
        log.warning(
            "知识图谱里有知识点叠在一起推不开（最近 %.4f，期望 %.3f）："
            "多半是某一章的知识点太多，装不进它在图上那块地盘",
            closest, MIN_GAP,
        )
    return {node_id: (point[0], point[1]) for node_id, point in positions.items()}


def _relax_overlaps(positions: dict[int, list[float]], nodes: list[PointNode]) -> float:
    """把挨得太近的两个点推开。**就地改 positions**，返回推完之后的最近间距。

    章内布局只管"这一章摆得开不开"，管不了跨章——相邻两章的圆圈在边界上
    照样会叠（尤其是人多的第 3 章和第 6 章，地盘大）。叠着的两个点里，
    底下那个的名字和颜色就全被盖住了，等于图上少了两个知识点。

    做法很土但够用：反复扫所有点对，离得比 MIN_GAP 近就各推开一半。
    几十轮之后稳定下来。**只动位置，不动大小**——圆圈大小是有含义的（题量）。

    **返回最近间距是有用的**：一章里点太多（多到地盘塞不下）时，点会被挤到
    画布边界上再也推不开，最后会有两个点重合。调用方拿到这个数就能报一声，
    而不是让图上悄悄少一个点。
    """
    ids = [node.id for node in nodes]
    for _ in range(60):
        moved = False
        for left_index, left_id in enumerate(ids):
            for right_id in ids[left_index + 1 :]:
                left, right = positions[left_id], positions[right_id]
                dx, dy = right[0] - left[0], right[1] - left[1]
                gap = math.hypot(dx, dy)
                if gap >= MIN_GAP:
                    continue
                # 完全重合时分不出方向，随便挑一个方向推开，免得除零
                if gap < 1e-9:
                    dx, dy, gap = 1.0, 0.0, 1.0
                push = (MIN_GAP - gap) / 2
                ux, uy = dx / gap, dy / gap
                left[0] = min(0.99, max(0.01, left[0] - ux * push))
                left[1] = min(0.99, max(0.01, left[1] - uy * push))
                right[0] = min(0.99, max(0.01, right[0] + ux * push))
                right[1] = min(0.99, max(0.01, right[1] + uy * push))
                moved = True
        if not moved:
            break

    return min(
        (
            math.hypot(
                positions[a][0] - positions[b][0], positions[a][1] - positions[b][1]
            )
            for index, a in enumerate(ids)
            for b in ids[index + 1 :]
        ),
        default=MIN_GAP,
    )
