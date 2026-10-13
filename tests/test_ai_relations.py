"""让 AI 找知识点关系的测试。

**重点全在"挑刺"上**：模型会写出考纲里没有的名字、把一条关系说两遍、
方向写反、拿"相关"凑数、编不出理由。这些必须在进用户眼前之前挡掉，
而且**要如实报出挡掉了什么**——静默丢弃会让用户以为 AI 就提了这么多，
于是他照着一条不完整的图去安排复习，还以为是全部。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import prompts  # noqa: E402
from cstudy.ai.relations import RelationFinder, resolve_name  # noqa: E402
from cstudy.config import Config  # noqa: E402


class FakeClient:
    """按调用顺序返回预设回复；raises 不为空时每次都抛。"""

    def __init__(self, replies=None, raises=None) -> None:
        if replies is None:
            self.replies: list = []
        elif isinstance(replies, list):
            self.replies = replies
        else:
            self.replies = [replies]
        self.raises = raises
        self.calls: list = []

    def chat(self, messages, temperature=None, json_mode=False):
        self.calls.append(messages)
        if self.raises is not None:
            raise self.raises
        return self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]

    def chat_json(self, messages, temperature=None):
        return self.chat(messages, json_mode=True)


GROUPED = {
    "第5章 循环结构程序设计": ["while 循环", "for 循环", "break 语句", "continue 语句"],
    "第8章 善于利用指针": ["指针变量", "用指针处理数组"],
}
VALID = {name for names in GROUPED.values() for name in names}


def make_finder(payload, limit: int = 60) -> RelationFinder:
    return RelationFinder(FakeClient(json.dumps(payload, ensure_ascii=False)), make_config(limit))


def make_config(limit: int = 60) -> Config:
    return Config({"graph": {"max_relations": limit}, "paths": {}}, Path("."))


def reply(relations: list[dict]) -> dict:
    return {"relations": relations}


def rel(from_name: str, to_name: str, kind: str = "prereq", reason: str = "有先后顺序") -> dict:
    return {"from": from_name, "to": to_name, "kind": kind, "reason": reason}


class TestScreening:
    def test_keeps_a_good_relation(self):
        result = make_finder(reply([rel("指针变量", "用指针处理数组")])).propose(GROUPED)

        assert result.ok
        assert [(r.from_name, r.to_name, r.kind) for r in result.relations] == [
            ("指针变量", "用指针处理数组", "prereq")
        ]
        assert not result.dropped

    def test_a_relation_inside_one_chapter_is_kept(self):
        """`break` 和 `continue` 都在第 5 章里——**同章完全可以是好关系**。
        一开始我加过"同章就丢掉"的规则，是错的，这条测试盯着它别回来。"""
        result = make_finder(reply([rel("break 语句", "continue 语句", "confuse", "都跳出循环，容易混")])).propose(GROUPED)

        assert len(result.relations) == 1
        assert result.relations[0].kind == "confuse"

    def test_drops_a_name_that_is_not_in_the_syllabus(self):
        """模型会自己造名字。清单里没有的一律丢掉，**而且要说出来**。"""
        result = make_finder(reply([rel("指针变量", "内存管理")])).propose(GROUPED)

        assert not result.relations
        assert result.dropped_count == 1
        assert "内存管理" in result.dropped[0]

    def test_drops_a_self_loop(self):
        result = make_finder(reply([rel("for 循环", "for 循环")])).propose(GROUPED)

        assert not result.relations
        assert "自己跟自己" in result.dropped[0] or "跟自己连" in result.dropped[0]

    def test_drops_an_unknown_kind(self):
        result = make_finder(reply([rel("指针变量", "用指针处理数组", "depends_on")])).propose(GROUPED)

        assert not result.relations
        assert "depends_on" in result.dropped[0]

    def test_drops_a_relation_without_a_reason(self):
        """写不出理由，说明这条关系其实没想清楚。"""
        result = make_finder(reply([rel("指针变量", "用指针处理数组", "related", "   ")])).propose(GROUPED)

        assert not result.relations
        assert "理由" in result.dropped[0]

    def test_undirected_duplicates_are_collapsed_whichever_way_round(self):
        result = make_finder(
            reply(
                [
                    rel("break 语句", "continue 语句", "confuse", "容易混"),
                    rel("continue 语句", "break 语句", "confuse", "容易混，反着写一遍"),
                ]
            )
        ).propose(GROUPED)

        assert len(result.relations) == 1
        assert "说了两遍" in result.dropped[0]

    def test_reversed_prereq_is_not_a_duplicate(self):
        """前置关系有方向，写反了是**另一条关系**——顺序错了会把人带沟里，
        不能当重复吞掉。"""
        result = make_finder(
            reply([rel("指针变量", "用指针处理数组"), rel("用指针处理数组", "指针变量")])
        ).propose(GROUPED)

        assert len(result.relations) == 2
        assert not result.dropped

    def test_relations_already_in_the_database_are_not_proposed_again(self):
        existing = {("指针变量", "用指针处理数组", "prereq")}

        result = make_finder(reply([rel("指针变量", "用指针处理数组")])).propose(
            GROUPED, existing=existing
        )

        assert not result.relations
        assert result.duplicated == 1
        # 已经有的不算"被丢掉"，它是正常的
        assert not result.dropped

    def test_a_lowercase_kind_is_accepted(self):
        result = make_finder(reply([rel("指针变量", "用指针处理数组", "PRE-REQ")])).propose(GROUPED)

        assert not result.relations      # 归一化后仍不认识


class TestResolveName:
    """模型抄知识点名字会抄岔，这里认名字。

    **这一段是安全关键**：认错了就会把两个不同的知识点连起来，
    而"配错的看起来跟配对的一样"。所以下面既测"该认回来的"，
    也测"必须拒绝的"——后者更要紧。

    用的名字是**真考纲里的原话**（含那对危险的一维/二维）。
    """

    NAMES = [
        "一维数组的定义、初始化与元素引用",
        "二维数组的定义、初始化与元素引用",
        "用指针处理数组",
        "用指针处理函数",
        "结构体的含义与类型声明",
        "什么是算法和算法的特性",
        "while 循环的格式与循环条件",
        "do-while 循环的格式与循环条件",
        "条件的表达方式（算术表达式、关系表达式、逻辑表达式）",
        "常用的输入输出函数与基本输入输出格式",
    ]

    def test_an_exact_name_needs_no_correction(self):
        name, note = resolve_name("用指针处理数组", self.NAMES)

        assert name == "用指针处理数组"
        assert note == ""

    def test_a_stray_space_is_forgiven(self):
        """模型很爱在中文里多插一个空格——这是它抄岔里最常见的一种。"""
        name, note = resolve_name("什么 是算法和算法的特性", self.NAMES)

        assert name == "什么是算法和算法的特性"
        assert note                      # 做了宽容处理就得说出来

    def test_a_full_width_bracket_is_forgiven(self):
        name, _ = resolve_name("条件的表达方式(算术表达式、关系表达式、逻辑表达式)", self.NAMES)

        assert name == "条件的表达方式（算术表达式、关系表达式、逻辑表达式）"

    def test_a_missing_character_is_forgiven_when_it_is_unambiguous(self):
        """实测真发生过：模型把"常用的输入输出函数…"写成了"常用输入输出函数…"。"""
        name, note = resolve_name("常用的输入输出函数与基本输入输出格式".replace("的", "", 1), self.NAMES)

        assert name == "常用的输入输出函数与基本输入输出格式"
        assert note

    def test_a_typo_is_forgiven_when_it_is_unambiguous(self):
        name, _ = resolve_name("结构体的含意与类型声明", self.NAMES)

        assert name == "结构体的含义与类型声明"

    def test_the_one_two_array_pair_is_never_confused(self):
        """**这条最要紧。** "一维数组"和"二维数组"是两个不同的知识点，
        名字像到只差一个字。模型要是把开头的"一/二"丢了，
        任何一个都说得通——这时候**必须拒绝**，不能猜。
        猜错的后果是用户照着一条错误的顺序去复习，而且看不出来。"""
        name, _ = resolve_name("维数组的定义、初始化与元素引用", self.NAMES)

        assert name is None

    def test_a_truncated_one_two_array_name_is_refused(self):
        name, _ = resolve_name("二维数组的定义、初始化与元素引", self.NAMES)

        assert name is None

    def test_an_ambiguous_typo_is_refused(self):
        """"用指针处理数列"能对到数组也能对到函数，分不清就不能猜。"""
        name, _ = resolve_name("用指针处理数列", self.NAMES)

        assert name is None

    def test_a_totally_unrelated_name_is_refused(self):
        assert resolve_name("内存管理", self.NAMES)[0] is None

    def test_an_empty_name_is_refused(self):
        assert resolve_name("", self.NAMES)[0] is None
        assert resolve_name("   ", self.NAMES)[0] is None

    def test_nothing_to_choose_from(self):
        assert resolve_name("随便什么", [])[0] is None

    def test_a_broken_name_never_lands_on_a_different_point(self):
        """**这是"认名字"最要紧的不变量。**

        拿这份清单里每个名字，逐字删掉一个字，看会不会认到**别的**知识点上。
        允许的结果只有两种：认回它自己，或者拒绝（返回 None）。
        **认到别人身上是绝对不允许的**——那会连出一条看起来很像真的、实际不存在的关系。

        实测过真考纲的 64 个名字、752 个残缺写法：认回 660 个，认错 **0** 个。
        这里用一份浓缩的清单把同一条不变量钉住（都是考纲里最像的那几对）。
        """
        wrong: list[tuple[str, str, str]] = []
        for name in self.NAMES:
            for index in range(1, len(name) - 1):
                broken = name[:index] + name[index + 1:]
                if len(broken) < 4:
                    continue
                got, _ = resolve_name(broken, self.NAMES)
                if got is not None and got != name:
                    wrong.append((name, broken, got))

        assert wrong == [], f"残缺名字认到别的知识点上了：{wrong}"

    def test_an_extra_space_never_lands_on_a_different_point(self):
        """模型最常犯的是多打空格。这条同样一个都不许认错。"""
        wrong: list[tuple[str, str, str]] = []
        for name in self.NAMES:
            for index in range(1, len(name)):
                broken = name[:index] + " " + name[index:]
                got, _ = resolve_name(broken, self.NAMES)
                if got is not None and got != name:
                    wrong.append((name, broken, got))

        assert wrong == []


class TestNameCorrectionsReachTheUser:
    """认回名字这件事**得让用户看见**——"我替你认了个名字"是个判断，
    用户有权知道并推翻。"""

    def test_a_written_wrong_name_is_accepted_and_recorded(self):
        grouped = {
            "第5章 循环结构程序设计": ["while 循环的格式与循环条件"],
            "第8章 善于利用指针": ["用指针处理数组"],
        }
        payload = reply(
            [rel("while 循环的格式与循环条", "用指针处理数组", "related", "都跟循环有关")]
        )

        result = make_finder(payload).propose(grouped)

        assert [(r.from_name, r.to_name) for r in result.relations] == [
            ("while 循环的格式与循环条件", "用指针处理数组")
        ]
        assert result.corrected
        assert "写岔了" in result.corrected[0]
        assert "认了" in result.describe()

    def test_the_dropped_line_no_longer_promises_an_exact_match(self):
        grouped = {"第1章": ["甲"], "第2章": ["乙"]}
        payload = reply([rel("甲", "丙丁", "related", "有点关系")])

        result = make_finder(payload).propose(grouped)

        assert not result.relations
        assert "不敢猜" in result.dropped[0]


class TestCapping:
    def test_weak_relations_are_cut_first(self):
        """模型给的顺序是它自己的，不能指望它把最重要的放前面——
        超上限时必须先砍最弱的"相关"。"""
        payload = reply(
            [
                rel("break 语句", "for 循环", "related", "都跟循环有关"),
                rel("指针变量", "用指针处理数组", "prereq", "得先懂指针才能用指针处理数组"),
                rel("while 循环", "for 循环", "related", "都是循环"),
            ]
        )
        result = make_finder(payload, limit=2).propose(GROUPED)

        assert [r.kind for r in result.relations] == ["prereq", "related"]
        assert result.truncated == 1

    def test_the_cut_count_is_reported(self):
        loops = ["while 循环", "for 循环", "break 语句", "continue 语句"]
        pairs = [
            (loops[i], loops[j]) for i in range(len(loops)) for j in range(i + 1, len(loops))
        ]
        payload = reply([rel(a, b, "related", f"{a}和{b}都是循环里的东西") for a, b in pairs])
        result = make_finder(payload, limit=2).propose(GROUPED)

        assert len(result.relations) == 2
        assert result.truncated == len(pairs) - 2
        assert "砍掉" in result.describe()


class TestFailurePaths:
    def test_broken_json_does_not_raise(self):
        result = RelationFinder(FakeClient("这不是 JSON"), make_config()).propose(GROUPED)

        assert not result.ok
        assert result.error

    def test_missing_relations_key_does_not_raise(self):
        result = make_finder({"relations_typo": []}).propose(GROUPED)

        assert not result.ok
        assert "relations" in result.error

    def test_a_call_that_blows_up_does_not_raise(self):
        finder = RelationFinder(FakeClient(raises=RuntimeError("网络断了")), make_config())

        result = finder.propose(GROUPED)

        assert not result.ok
        assert "网络断了" in result.error

    def test_non_dict_items_are_ignored(self):
        result = make_finder(reply(["这行不是对象", rel("指针变量", "用指针处理数组")])).propose(GROUPED)

        assert len(result.relations) == 1

    def test_a_syllabus_with_one_point_says_so(self):
        result = make_finder(reply([])).propose({"第1章": ["只有一个知识点"]})

        assert not result.ok
        assert "两个" in result.error

    def test_describe_is_plain_text(self):
        """它直接进界面的进度区（纯文本控件），用 Markdown 记号会露出来。"""
        result = make_finder(reply([rel("指针变量", "用指针处理数组")])).propose(GROUPED)

        text = result.describe()

        assert "**" not in text and "#" not in text
        assert "1 条" in text


class TestPrompt:
    def test_json_is_demanded_by_name(self):
        """接口要求提示词里出现 "JSON" 字样，少一次调用就报错。"""
        assert "JSON" in prompts.POINT_RELATION_SYSTEM

    def test_the_three_kinds_are_spelled_out(self):
        for kind in ("prereq", "confuse", "related"):
            assert kind in prompts.POINT_RELATION_SYSTEM

    def test_it_warns_against_linking_everything_in_a_chapter(self):
        assert "同一章" in prompts.POINT_RELATION_SYSTEM

    def test_the_user_prompt_lists_every_point_and_the_limit(self):
        text = prompts.point_relation_user_prompt(GROUPED, 25)

        for name in VALID:
            assert name in text
        assert "25" in text
