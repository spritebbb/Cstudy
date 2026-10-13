"""让 AI 找出知识点之间的关系（知识图谱的连线）。

**这个模块只提建议，不写库。** 提出来的一律要在界面上让用户逐条过目，
确认过的才入库——关系是"判断"不是"事实"，AI 说错了用户照着复习是实打实的损失。

所以这个模块有一半的代码是在**挑刺**：模型会写出考纲里没有的名字、
把一条关系写两遍、写反方向、拿"相关"凑数。这些全都得在进用户眼前之前挡掉，
而且**要如实报出挡掉了什么**——静默丢弃会让用户以为 AI 就提了这么多。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable

from ..config import Config
from ..graph import RELATION_KINDS
from ..logging_setup import get_logger
from ..models import DataError, parse_json_object
from . import prompts
from .client import DeepSeekClient

log = get_logger(__name__)

ProgressCallback = Callable[[str], None]

DEFAULT_MAX_RELATIONS = 60

# 关系类型的优先级：条数超上限时先砍"相关"这种最弱的一档。
# 顺序就是 prompt 里要模型的顺序，也是界面上分组显示的顺序。
KIND_PRIORITY = ("prereq", "confuse", "related")

# 无方向的关系。判定重复时要把 A→B 和 B→A 当成同一条。
# **`prereq` 不在此列**——方向写反是另一条关系，不能当重复吞掉。
UNDIRECTED = ("confuse", "related")

# ---------- 认名字的阈值 ----------
#
# 模型抄知识点名字会抄岔：实测跑两次，有一次把 64 个名字里的 3 个写错了
# （多打了个空格、少了个"的"、尾巴被截断）。名字对不上就整条丢掉，
# 等于白花了那次调用，所以做一层"认名字"。
#
# **下面两个数是量出来的，不是拍的**（拿真考纲的 64 个名字算过）：
#
#   · 考纲里真实存在的一对近义知识点——"一维数组的定义、初始化与元素引用"
#     和"二维数组的定义、初始化与元素引用"——相似度 **0.938**。
#     这两个是**不同的知识点，绝不能配到一起**，所以这就是下限。
#   · 模型真写岔的那三个名字，跟正确目标的相似度是 **0.941 / 0.971 / 1.000**，
#     而跟"第二名"的差距是 **0.72 ~ 0.79**。
#
# 只看相似度是分不开的：0.941 和 0.938 只差 0.003。
# **必须同时要求"明显甩开第二名"**，才既认得出错别字、又不会把
# "一维数组"和"二维数组"配到一起（那种错看着跟对的一样）。
#
# 实测这条规则的边界（都验过）：
#   "维数组的定义…"（丢了开头的"一/二"）→ 两名并列，差 0.000 → 不认
#   "二维数组的定义、初始化与元素引"（截断）→ 差 0.065 → 不认
#   "结构体的含意与类型声明"（错别字 义→意）→ 差 0.182 → 认
FUZZY_MIN_RATIO = 0.90
FUZZY_MIN_MARGIN = 0.12


@dataclass(frozen=True)
class ProposedRelation:
    """一条待确认的关系。

    `origin` 记下它是 AI 提的还是用户自己敲的——**用户得知道自己该信几分**，
    界面上要显示出来。AI 提的和手写的在库里是分开记的（`point_edges.origin`）。
    """

    from_name: str
    to_name: str
    kind: str
    reason: str
    origin: str = "ai"      # ai / manual

    @property
    def kind_label(self) -> str:
        return RELATION_KINDS.get(self.kind, self.kind)

    def signature(self) -> tuple[str, str, str]:
        """判重用。无方向的关系把两端排序，这样 A↔B 和 B↔A 算同一条。"""
        if self.kind in UNDIRECTED:
            left, right = sorted((self.from_name, self.to_name))
            return (left, right, self.kind)
        return (self.from_name, self.to_name, self.kind)


@dataclass
class RelationProposal:
    """一次"找关系"的结果。

    跟 `ReviewReport` 一样用 ok/error 表达，**不抛异常**：
    找关系失败不该把界面搞崩，用户重试一次就行。
    """

    relations: list[ProposedRelation] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)   # 挡掉的原因，逐条给人看
    corrected: list[str] = field(default_factory=list) # AI 把名字写岔、被认回来的
    duplicated: int = 0                                # 库里已经有了的，条数
    truncated: int = 0                                 # 超出上限被砍掉的，条数
    ok: bool = True
    error: str = ""
    # 出错时的原始异常。**界面要靠它决定怎么报**：`AIError` / `DataError` 这些
    # 消息本来就是写给人看的中文，直接显示；别的异常得包成"出了预期之外的错误
    # + 日志在哪"，不能把英文技术信息摆到用户脸上。只留字符串的话这个区别就没了。
    error_exc: BaseException | None = None

    @property
    def dropped_count(self) -> int:
        return len(self.dropped)

    def describe(self) -> str:
        """写成给用户看的一段话。纯文本——它直接进进度区，用 Markdown 记号会露出来。"""
        if not self.ok:
            return f"找关系没成功（{self.error}）。"
        if not self.relations and not self.dropped:
            return "AI 说这些知识点之间找不出把握得住的关系。"
        lines = [f"AI 找出 {len(self.relations)} 条关系，等你确认后才会存进库。"]
        if self.duplicated:
            lines.append(f"  · 其中 {self.duplicated} 条库里已经有了，没再重复列出")
        if self.truncated:
            lines.append(f"  · 超出上限的 {self.truncated} 条被砍掉了，可以调大上限再找一次")
        if self.corrected:
            lines.append(f"  · {len(self.corrected)} 处知识点名字 AI 写岔了，按最接近的那个认的：")
            lines.extend(f"      {note}" for note in self.corrected[:5])
        if self.dropped:
            lines.append(f"  · 丢掉 {len(self.dropped)} 条不合格的：")
            lines.extend(f"      {why}" for why in self.dropped[:10])
            if len(self.dropped) > 10:
                lines.append(f"      …还有 {len(self.dropped) - 10} 条")
        return "\n".join(lines)


# ---------- 认名字 ----------
#
# 模型是"抄"考纲里的名字，不是自己想一个。抄岔的情况实测有（见上面阈值那段注释）。
# 名字对不上就整条丢掉太亏，所以在"一字不差"和"丢掉"之间加一层有限的宽容。


def _normalize_name(text: str) -> str:
    """比较名字之前先归一：**去掉所有空白**，全角括号和逗号换成半角。

    模型很爱在中文里多插一个空格（"什么 是算法和算法的特性"），
    这是它抄岔里最常见的一种，归一之后就一模一样了。
    """
    return (
        re.sub(r"\s+", "", str(text or ""))
        .replace("（", "(")
        .replace("）", ")")
        .replace("，", ",")
    )


def resolve_name(raw: str, valid_names) -> tuple[str | None, str]:
    """模型写的知识点名字 → 考纲里真实存在的名字。

    返回 `(认出来的名字 或 None, 说明)`。说明是空的表示一字不差，
    非空表示做了宽容处理——**这一步得让用户看见**，因为"我替你认了个名字"
    本身是个判断，用户有权知道并推翻（确认框里显示的就是认出来的全名）。

    三级，从严到宽：

    1. **一字不差**
    2. **归一之后一字不差**（多打了空格、括号全半角不同）
    3. **模糊匹配**，要求相似度够高**而且明显甩开第二名**——两个条件缺一不可，
       理由见上面阈值那段注释（"一维数组"和"二维数组"只差 0.003）

    认不出来就返回 None。**宁可丢掉也不硬配**——这条线是这个项目的底线。
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""

    names = list(valid_names)
    if text in names:
        return text, ""

    normalized = _normalize_name(text)
    same = [name for name in names if _normalize_name(name) == normalized]
    if len(same) == 1:
        return same[0], f"AI 把「{text}」写成了带空格的写法，按考纲里的「{same[0]}」认了"
    if len(same) > 1:
        # 归一之后撞上两个，说不清是哪个，不认
        return None, ""

    scored = sorted(
        (
            (difflib.SequenceMatcher(None, normalized, _normalize_name(name)).ratio(), name)
            for name in names
        ),
        reverse=True,
    )
    if not scored:
        return None, ""
    best_ratio, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_ratio >= FUZZY_MIN_RATIO and best_ratio - runner_up >= FUZZY_MIN_MARGIN:
        return best, f"AI 把「{_short(text)}」写岔了，按考纲里的「{best}」认了"
    return None, ""


class RelationFinder:
    """把考纲递给 AI，让它提关系。"""

    def __init__(self, client: DeepSeekClient, config: Config) -> None:
        self.client = client
        raw = config.get("graph.max_relations", DEFAULT_MAX_RELATIONS)
        try:
            self.limit = max(1, int(raw))
        except (TypeError, ValueError):
            log.warning("graph.max_relations 配置看不懂（%r），用默认值 %d", raw, DEFAULT_MAX_RELATIONS)
            self.limit = DEFAULT_MAX_RELATIONS

    def propose(
        self,
        grouped: dict[str, list[str]],
        existing: set[tuple[str, str, str]] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> RelationProposal:
        """`grouped` 是 {章名: [知识点名字]}，`existing` 是库里已经确认过的（判重用）。

        任何一步出问题都返回 `ok=False`，不抛异常。
        """
        total = sum(len(names) for names in grouped.values())
        if total < 2:
            return RelationProposal(
                ok=False, error="考纲里还不到两个知识点，连不出关系"
            )

        if on_progress:
            on_progress(f"正在让 AI 找 {total} 个知识点之间的关系…")

        try:
            raw = self.client.chat_json(
                [
                    {"role": "system", "content": prompts.POINT_RELATION_SYSTEM},
                    {
                        "role": "user",
                        "content": prompts.point_relation_user_prompt(grouped, self.limit),
                    },
                ]
            )
        except Exception as exc:
            log.warning("找关系失败：%s", exc)
            return RelationProposal(
                ok=False, error=str(exc) or type(exc).__name__, error_exc=exc
            )

        try:
            items = self._extract_items(raw)
        except DataError as exc:
            log.warning("找关系的返回解析不了：%s", exc)
            return RelationProposal(ok=False, error=str(exc), error_exc=exc)

        return self._screen(items, grouped, existing or set())

    # ---------- 挑刺 ----------

    @staticmethod
    def _extract_items(raw) -> list[dict]:
        data = parse_json_object(raw)
        items = data.get("relations")
        if items is None:
            # 模型偶尔写成单数，不算错，认了
            items = data.get("relation")
        if items is None:
            raise DataError("AI 没返回 relations 列表")
        if not isinstance(items, list):
            raise DataError("AI 返回的 relations 不是一个列表")
        return [item for item in items if isinstance(item, dict)]

    def _screen(
        self,
        items: list[dict],
        grouped: dict[str, list[str]],
        existing: set[tuple[str, str, str]],
    ) -> RelationProposal:
        """把模型给的东西筛成能看的。**每挡掉一条都记下为什么。**"""
        valid_names = {name for names in grouped.values() for name in names}

        result = RelationProposal()
        seen: set[tuple[str, str, str]] = set()

        for item in items:
            raw_from = str(item.get("from") or "").strip()
            raw_to = str(item.get("to") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
            reason = " ".join(str(item.get("reason") or "").split())

            from_name, from_note = resolve_name(raw_from, valid_names)
            if from_name is None:
                result.dropped.append(
                    f"「{_short(raw_from)}」在考纲里找不到（名字对不上就不敢猜是哪一条），整条丢掉"
                )
                continue
            to_name, to_note = resolve_name(raw_to, valid_names)
            if to_name is None:
                result.dropped.append(
                    f"「{_short(raw_to)}」在考纲里找不到（名字对不上就不敢猜是哪一条），整条丢掉"
                )
                continue
            # 认回来的名字要记账——"我替你认了个名字"是个判断，用户有权知道
            for note in (from_note, to_note):
                if note and note not in result.corrected:
                    result.corrected.append(note)

            if from_name == to_name:
                result.dropped.append(f"「{_short(from_name)}」跟自己连，丢掉")
                continue
            if kind not in RELATION_KINDS:
                result.dropped.append(
                    f"「{_short(from_name)}」和「{_short(to_name)}」的关系类型是"
                    f"「{_short(kind)}」，不认识，丢掉"
                )
                continue

            # **故意不按"同章就丢掉"来筛。** 一开始加过这条规则，是错的：
            # C 语言里最值钱的关系恰恰多半在同章内——`break` 和 `continue`
            # 都在第 5 章，`i++` 和 `++i` 都在第 3 章。同章只说明"位置挨着"，
            # 说不了"谁先谁后""哪儿容易混"。真正挡住凑数的是下面这两条：
            # 必须写得出理由，以及最后那道人工确认。
            if not reason:
                result.dropped.append(
                    f"「{_short(from_name)}」和「{_short(to_name)}」这条没写理由，丢掉"
                )
                continue

            relation = ProposedRelation(
                from_name=from_name, to_name=to_name, kind=kind, reason=reason
            )
            signature = relation.signature()

            if signature in seen:
                result.dropped.append(
                    f"「{_short(from_name)}」和「{_short(to_name)}」这条 AI 说了两遍，只留一条"
                )
                continue
            if signature in existing:
                result.duplicated += 1
                continue

            seen.add(signature)
            result.relations.append(relation)

        self._cap(result)
        log.info(
            "找关系：留下 %d 条，认回 %d 处名字，挡掉 %d 条，库里已有的 %d 条",
            len(result.relations), len(result.corrected), len(result.dropped),
            result.duplicated,
        )
        return result

    def _cap(self, result: RelationProposal) -> None:
        """超上限就砍，**先砍最弱的"相关"**。

        模型的输出顺序是它自己的，不能指望它把最重要的放前面，
        所以这里先按关系类型的优先级排一遍再截断（顺带界面上也是这个顺序）。
        """
        result.relations.sort(
            key=lambda rel: KIND_PRIORITY.index(rel.kind)
            if rel.kind in KIND_PRIORITY else len(KIND_PRIORITY)
        )
        if len(result.relations) > self.limit:
            result.truncated = len(result.relations) - self.limit
            result.relations = result.relations[: self.limit]


def _short(text: str, limit: int = 24) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"
