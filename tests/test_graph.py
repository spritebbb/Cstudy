"""知识图谱：节点数据、掌握度分档、布局。

布局这块**光跑测试看不出好不好看**（那是渲染出来看的事），
但下面这些是能测、也必须测的：

- 个数算错（重复 JOIN 把行数乘起来）——这类错误很隐蔽，数字看着还挺合理
- 挨得太近的两个点会叠在一起，等于图上少了两个知识点
- 同一张图两次画出来位置不一样，用户就记不住"我的薄弱区在哪块"
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy import graph  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.models import Question  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.db")
    database.init_schema()
    return database


def make_node(node_id: int, chapter: str, count: int = 10, answered: int = 0,
              correct: int = 0, distinct: int = 0, name: str = "") -> graph.PointNode:
    return graph.PointNode(
        id=node_id,
        name=name or f"知识点{node_id}",
        chapter=chapter,
        question_count=count,
        answered=answered,
        correct=correct,
        distinct=distinct,
    )


def make_data(chapters: dict[str, int], edges: list[tuple[int, int, str]] | None = None):
    """按 {章名: 几个知识点} 造一张图，id 顺着往下排。"""
    nodes: list[graph.PointNode] = []
    next_id = 1
    for chapter, count in chapters.items():
        for _ in range(count):
            nodes.append(make_node(next_id, chapter))
            next_id += 1
    edge_objects = [
        graph.PointEdge(id=i + 1, from_id=a, to_id=b, kind=kind, reason="测试", origin="ai")
        for i, (a, b, kind) in enumerate(edges or [])
    ]
    return graph.GraphData(nodes=nodes, edges=edge_objects)


# ---------- 掌握度分档 ----------


class TestMasteryBand:
    """**样本不够就说不知道，不硬给结论**——做 1 道错 1 道就标成大红色，
    用户会去补一个其实不弱的地方，而这样的假红点一多整张图就没法看了。"""

    @pytest.mark.parametrize(
        "answered, correct, expected",
        [
            (0, 0, "unseen"),
            (1, 0, "unseen"),      # 只做 1 道，样本太少
            (2, 2, "unseen"),      # 只做 2 道，全对也先不下结论
            (3, 0, "weak"),        # 够 3 道了，0% 就是偏弱
            (3, 2, "mid"),         # 67%
            (5, 4, "strong"),      # 80%
        ],
    )
    def test_boundaries(self, answered, correct, expected):
        assert graph.mastery_band(answered, correct) == expected

    def test_fifty_percent_is_weak(self):
        assert graph.mastery_band(4, 2) == "weak"

    def test_eighty_percent_is_strong(self):
        assert graph.mastery_band(5, 4) == "strong"

    def test_just_below_eighty_is_mid(self):
        assert graph.mastery_band(10, 7) == "mid"      # 70%

    def test_thin_sample_flag(self):
        assert graph.is_thin_sample(0) is False        # 一道没做，不是"样本少"
        assert graph.is_thin_sample(1) is True
        assert graph.is_thin_sample(graph.MIN_SAMPLE) is False


class TestPointNode:
    def test_accuracy_is_none_when_never_practiced(self):
        """**没做过返回 None 而不是 0**——"没练过"和"练了全错"不是一回事。"""
        assert make_node(1, "第1章").accuracy is None

    def test_describe_says_so_when_sample_is_thin(self):
        """判据是"练过几道**不同的**题"，不是"做过多少次"——
        同一道题刷三遍不叫练到了，而这软件自己就在引导重刷。"""
        node = make_node(1, "第1章", answered=2, correct=2, distinct=2)

        assert "看不出强弱" in node.describe()

    def test_answering_the_same_question_many_times_is_not_a_thick_sample(self):
        node = make_node(1, "第1章", answered=9, correct=9, distinct=1)

        text = node.describe()

        assert "覆盖 1 道题" in text
        assert "看不出强弱" in text

    def test_describe_reports_attempts_and_coverage_separately(self):
        """三个数都要说出来，别让用户自己对着"共 4 道"和"做过 8 道"发懵。"""
        node = make_node(1, "第1章", answered=8, correct=6, distinct=4)

        text = node.describe()

        assert "做过 8 次" in text
        assert "覆盖 4 道题" in text
        assert "对 6 次（75%）" in text

    def test_radius_shrinks_with_the_question_count(self):
        peak = 100
        big = make_node(1, "第1章", count=100)
        small = make_node(2, "第1章", count=1)

        assert big.radius(peak) > small.radius(peak)
        assert big.radius(peak) == pytest.approx(graph.MAX_RADIUS)
        assert small.radius(peak) >= graph.MIN_RADIUS

    def test_radius_survives_an_empty_bank(self):
        assert make_node(1, "第1章", count=0).radius(0) == graph.MIN_RADIUS


# ---------- 从库里读 ----------


class TestLoadGraph:
    def test_counts_answers_without_multiplying_rows(self, db: Database):
        """**这是最要命的一类错**：题量和作答数用 JOIN 算会把行数乘起来——
        一个知识点挂 3 道题、其中 2 道做过，JOIN 之后答案行数会变成 2×3。
        数字看着还挺合理，所以必须用测试钉住。"""
        point_id = db.upsert_syllabus_point("指针", "第8章", 1)
        ids = db.save_questions(
            [
                Question(type="fill", question=f"题{i}", answer="1", point_id=point_id)
                for i in range(3)
            ]
        )
        db.record_answer(ids[0], "1", is_correct=True)
        db.record_answer(ids[1], "0", is_correct=False)
        db.record_answer(ids[1], "1", is_correct=True)

        node = graph.load_graph(db).nodes[0]

        assert node.question_count == 3
        assert node.answered == 3
        assert node.correct == 2

    def test_points_with_no_questions_still_appear(self, db: Database):
        db.upsert_syllabus_point("文件定位", "第10章", 1)

        node = graph.load_graph(db).nodes[0]

        assert node.question_count == 0
        assert node.answered == 0
        assert node.mastery == "unseen"

    def test_reports_questions_that_are_not_linked_to_any_point(self, db: Database):
        """没挂到知识点上的题画不出来，但**必须报出来**，
        不然用户会以为这张图覆盖了全部题库。"""
        db.upsert_syllabus_point("指针", "第8章", 1)
        db.save_questions([Question(type="fill", question="没挂的题", answer="1")])

        assert graph.load_graph(db).loose_questions == 1

    def test_reads_saved_relations(self, db: Database):
        left = db.upsert_syllabus_point("break 与 continue", "第5章", 1)
        right = db.upsert_syllabus_point("switch 语句", "第4章", 2)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "confuse",
              "reason": "都是跳出循环/分支用的，容易混", "origin": "ai"}]
        )

        data = graph.load_graph(db)

        assert len(data.edges) == 1
        assert data.edges[0].kind_label == "容易混"
        assert data.nodes[0].id == left

    def test_a_relation_whose_point_disappeared_is_left_out(self, db: Database):
        """知识点没了、连线成了孤儿——图上和状态栏都**不能**算它。

        正常造不出这种行：外键是 ON DELETE CASCADE，删知识点会把连线一起带走
        （所以下面要先把外键关掉才删得掉）。留着这条是为了钉住"两个数走同一套
        过滤"这件事——不然会出现"状态栏说 54 条、图上只画了 52 条"这种情况，
        而用户没有任何办法查清楚差在哪。"""
        left = db.upsert_syllabus_point("甲", "第1章", 1)
        right = db.upsert_syllabus_point("乙", "第2章", 2)
        db.save_relations(
            [{"from_id": left, "to_id": right, "kind": "related",
              "reason": "有点关系", "origin": "ai"}]
        )
        db.conn.execute("PRAGMA foreign_keys = OFF")
        db.conn.execute("DELETE FROM syllabus_points WHERE id = ?", (right,))
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys = ON")

        data = graph.load_graph(db)

        assert data.nodes and not data.edges
        assert db.stats()["知识关系"] == 0

    def test_relations_of_and_neighbours(self, db: Database):
        a = db.upsert_syllabus_point("甲", "第1章", 1)
        b = db.upsert_syllabus_point("乙", "第2章", 2)
        c = db.upsert_syllabus_point("丙", "第3章", 3)
        db.save_relations(
            [
                {"from_id": a, "to_id": b, "kind": "prereq", "reason": "先甲后乙", "origin": "ai"},
                {"from_id": c, "to_id": a, "kind": "confuse", "reason": "容易混", "origin": "ai"},
            ]
        )

        data = graph.load_graph(db)

        assert data.neighbours(a) == {b, c}
        assert len(data.relations_of(a)) == 2
        # 乙只连着甲这一条（甲→乙是前置）
        assert [(edge.from_id, edge.to_id) for edge in data.relations_of(b)] == [(a, b)]


class TestChapters:
    def test_points_without_a_chapter_get_their_own_group(self):
        """考纲里每个知识点都写了章，但这是"系统边界之外的输入"——
        空章名不该让分组崩掉，单独归一组，界面上也看得出来它没写章。"""
        data = graph.GraphData(
            nodes=[make_node(1, ""), make_node(2, "第1章")], edges=[]
        )

        grouped = data.by_chapter()

        assert list(grouped) == [graph.CHAPTER_FALLBACK, "第1章"]

    def test_chapters_keep_the_syllabus_order(self):
        data = make_data({"第1章": 2, "第2章": 1, "第3章": 3})

        assert list(data.by_chapter()) == ["第1章", "第2章", "第3章"]


class TestGraphSignature:
    def test_same_shape_gives_the_same_signature(self):
        assert graph.graph_signature(make_data({"第1章": 2})) == graph.graph_signature(
            make_data({"第1章": 2})
        )

    def test_new_relation_changes_the_signature(self):
        plain = graph.graph_signature(make_data({"第1章": 2}))
        linked = graph.graph_signature(
            make_data({"第1章": 2}, edges=[(1, 2, "related")])
        )

        assert plain != linked


# ---------- 布局 ----------


class TestLayout:
    def test_is_deterministic(self):
        """用户记的是"我的薄弱区在右上角那团"。位置每次都变，这个记忆就废了。"""
        data = make_data({"第1章": 5, "第2章": 4, "第3章": 6})

        assert graph.layout(data) == graph.layout(data)

    def test_stays_inside_the_canvas(self):
        data = make_data({f"第{i}章": 5 for i in range(1, 11)})

        positions = graph.layout(data)

        assert len(positions) == 50
        for x, y in positions.values():
            assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0

    def test_no_two_nodes_are_closer_than_the_minimum_gap(self):
        """叠在一起的两个点，底下那个的名字和颜色就被盖住了——
        等于图上少了两个知识点。

        **按真实考纲的形状来测**：10 章、最大的那章 10 个点。
        以前这里用"9 章 × 每章 6 个"，比现实松，照真实形状改也不会挂。
        """
        data = make_data(
            {f"第{i}章": (10 if i == 3 else 5) for i in range(1, 11)}
        )

        positions = graph.layout(data)
        ids = [node.id for node in data.nodes]
        closest = min(
            math.dist(positions[a], positions[b])
            for index, a in enumerate(ids)
            for b in ids[index + 1:]
        )

        assert closest >= graph.MIN_GAP - 1e-9

    def test_a_chapter_too_crowded_to_fit_is_logged_not_swallowed(self, caplog):
        """一章里的点多到它那块地盘装不下时，最后会有两个点叠在一起推不开
        （被画布边界夹住了）。那种形状现在的考纲到不了——真实是 10 章、
        最大的那章 10 个点——但**不能悄悄这样**，得留一条日志，
        至少"图上少了东西"这件事不是错觉。

        门槛特意留得松（差 20% 以上才报）：贴着边界的点收敛后总会差一丁点
        （真实考纲上是 0.0480 差到 0.04795），为那点毛毛雨报警，
        只会教会用户忽略这条警告。
        """
        data = make_data({"第1章": 120})

        graph.layout(data)

        assert "叠在一起" in caplog.text

    def test_a_merely_tight_chapter_does_not_cry_wolf(self, caplog):
        """40 个点挤在一章里已经很不正常了，但还摆得下——**这种情况不该报警**。"""
        graph.layout(make_data({"第1章": 40}))

        assert "叠在一起" not in caplog.text

    def test_minimum_gap_leaves_room_for_the_biggest_circle(self):
        """两条常数是互相牵连的，改一条忘了另一条就会开始叠。"""
        assert graph.MIN_GAP > 2 * graph.MAX_RADIUS

    def test_chapters_cluster_together(self):
        """同章的点应该明显比跨章的点挨得近——不然"按章抱团"就没做到。"""
        data = make_data({f"第{i}章": 6 for i in range(1, 11)})

        positions = graph.layout(data)
        inside: list[float] = []
        across: list[float] = []
        for index, left in enumerate(data.nodes):
            for right in data.nodes[index + 1:]:
                gap = math.dist(positions[left.id], positions[right.id])
                (inside if left.chapter == right.chapter else across).append(gap)

        assert sum(inside) / len(inside) < sum(across) / len(across) / 2

    def test_edge_happens_inside_a_chapter_without_crashing(self):
        data = make_data({"第1章": 4}, edges=[(1, 2, "prereq"), (2, 3, "confuse")])

        positions = graph.layout(data)

        assert len(positions) == 4

    def test_no_points_at_all(self):
        assert graph.layout(graph.GraphData(nodes=[], edges=[])) == {}

    def test_a_single_point_sits_in_the_middle(self):
        data = make_data({"第1章": 1})

        assert graph.layout(data) == {1: (0.5, 0.5)}

    def test_one_chapter_with_one_point_does_not_divide_by_zero(self):
        data = make_data({"第1章": 1, "第2章": 1})

        positions = graph.layout(data)

        assert len(positions) == 2
        assert math.dist(positions[1], positions[2]) > 0

    def test_points_in_the_same_chapter_do_not_stay_on_top_of_each_other(self):
        """一章里一条关系都没有时，力导向只剩斥力——也得把它们摊开。"""
        data = make_data({"第1章": 6})

        positions = graph.layout(data)
        ids = [node.id for node in data.nodes]
        closest = min(
            math.dist(positions[a], positions[b])
            for index, a in enumerate(ids)
            for b in ids[index + 1:]
        )

        assert closest >= graph.MIN_GAP - 1e-9
