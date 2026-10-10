"""AI 二次校准的测试。

重点在失败路径：复查出问题**不能**把整个导入搞挂——原来的内容照常保留，
只是这一轮没人把关而已。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai.review import Reviewer  # noqa: E402
from cstudy.config import Config  # noqa: E402
from cstudy.models import (  # noqa: E402
    Question,
    ReviewNote,
    ReviewReport,
    parse_review_report,
)
from cstudy.subtitle import Segment  # noqa: E402


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


def make_config(review: dict | None = None) -> Config:
    return Config({"review": review or {}, "paths": {}}, Path("."))


def make_questions(count: int = 3) -> list[Question]:
    return [
        Question(
            type="choice",
            question=f"第 {i} 题？",
            answer="A",
            options=["A. 甲", "B. 乙"],
            explanation="因为甲",
            point="测试点",
        )
        for i in range(1, count + 1)
    ]


SEGMENTS = [Segment(0.0, "字符 '0' 的 ASCII 值是 48"), Segment(5.0, "点赞收藏三连")]


def dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


class TestReviewNote:
    def test_returns_revised_note_and_report(self):
        payload = dump(
            {
                "revised_note": "# 修正后的笔记\n内容",
                "removed": [{"subject": "点赞三连引导", "reason": "不是技术内容"}],
                "fixed": [],
            }
        )
        body, report = Reviewer(FakeClient(payload), make_config()).review_note("原笔记", SEGMENTS)

        assert body.startswith("# 修正后的笔记")
        assert report.ok
        assert report.removed[0].reason == "不是技术内容"

    def test_source_subtitles_are_sent_along(self):
        """必须带字幕——不带就查不出"笔记编造了字幕里没有的内容"。"""
        client = FakeClient(dump({"revised_note": "笔记", "removed": [], "fixed": []}))
        Reviewer(client, make_config()).review_note("原笔记", SEGMENTS)

        sent = client.calls[0][1]["content"]
        assert "48" in sent  # 字幕内容确实送过去了
        assert "原笔记" in sent

    def test_empty_revised_note_falls_back(self):
        payload = dump({"revised_note": "   ", "removed": [], "fixed": []})
        body, report = Reviewer(FakeClient(payload), make_config()).review_note("原笔记", SEGMENTS)

        assert body == "原笔记"
        assert not report.ok

    def test_client_failure_keeps_original_and_never_raises(self):
        reviewer = Reviewer(FakeClient(raises=RuntimeError("网络断了")), make_config())
        body, report = reviewer.review_note("原笔记", SEGMENTS)

        assert body == "原笔记"
        assert not report.ok
        assert "网络断了" in report.error

    def test_disabled_skips_the_call_entirely(self):
        client = FakeClient("不该被调用")
        reviewer = Reviewer(client, make_config({"enabled": False}))
        body, report = reviewer.review_note("原笔记", SEGMENTS)

        assert body == "原笔记"
        assert client.calls == []
        assert report.ok


class TestReviewQuestions:
    @staticmethod
    def _payload(questions, removed=None, fixed=None) -> str:
        return dump({"questions": questions, "removed": removed or [], "fixed": fixed or []})

    def test_drops_removed_questions(self):
        kept = [
            {
                "type": "choice",
                "question": "保留的题",
                "answer": "A",
                "options": ["A. 甲", "B. 乙"],
                "explanation": "x",
            }
        ]
        payload = self._payload(
            kept, removed=[{"index": 3, "subject": "广告题", "reason": "问怎么领资料"}]
        )
        questions, report = Reviewer(FakeClient(payload), make_config()).review_questions(
            make_questions(3), "笔记"
        )

        assert [q.question for q in questions] == ["保留的题"]
        assert report.removed[0].reason == "问怎么领资料"

    def test_missing_subject_is_filled_from_index(self):
        payload = self._payload(
            [{"type": "choice", "question": "留", "answer": "A", "options": ["A. 甲", "B. 乙"]}],
            removed=[{"index": 2, "reason": "编造的"}],
        )
        _, report = Reviewer(FakeClient(payload), make_config()).review_questions(
            make_questions(3), "笔记"
        )

        assert report.removed[0].subject == "第 2 题？"  # 光看序号认不出是哪道，补上题干

    def test_corrected_answer_is_applied(self):
        payload = self._payload(
            [
                {
                    "type": "choice",
                    "question": "第 1 题？",
                    "answer": "C",
                    "options": ["A. 甲", "B. 乙", "C. 丙"],
                }
            ],
            fixed=[{"index": 1, "subject": "第 1 题？", "reason": "答案 A 改成 C"}],
        )
        questions, report = Reviewer(FakeClient(payload), make_config()).review_questions(
            make_questions(1), "笔记"
        )

        assert questions[0].answer == "C"
        assert report.fixed[0].reason == "答案 A 改成 C"

    def test_empty_result_without_reasons_falls_back(self):
        """题全没了却一条删除理由都没给——多半是返回格式坏了，退回原题更安全。"""
        payload = self._payload([], removed=[])
        original = make_questions(3)
        questions, report = Reviewer(FakeClient(payload), make_config()).review_questions(
            original, "笔记"
        )

        assert questions == original
        assert not report.ok

    def test_empty_result_with_reasons_is_respected(self):
        """真的全是废题时，删光是对的——这正是用户踩过的那个坑。"""
        payload = self._payload(
            [],
            removed=[
                {"index": i, "subject": f"第 {i} 题？", "reason": "广告题"}
                for i in (1, 2, 3)
            ],
        )
        questions, report = Reviewer(FakeClient(payload), make_config()).review_questions(
            make_questions(3), "笔记"
        )

        assert questions == []
        assert report.ok
        assert len(report.removed) == 3

    def test_client_failure_keeps_original(self):
        original = make_questions(3)
        questions, report = Reviewer(
            FakeClient(raises=RuntimeError("超时")), make_config()
        ).review_questions(original, "笔记")

        assert questions == original
        assert not report.ok

    def test_no_questions_skips_the_call(self):
        client = FakeClient("不该被调用")
        questions, report = Reviewer(client, make_config()).review_questions([], "笔记")

        assert questions == []
        assert client.calls == []


class TestParseReviewReport:
    def test_tolerates_missing_and_wrong_types(self):
        report = parse_review_report({"removed": "不是列表", "fixed": None})
        assert report.removed == []
        assert report.fixed == []

    def test_tolerates_bad_index(self):
        report = parse_review_report(
            {"removed": [{"index": "abc", "subject": "x", "reason": "y"}]}
        )
        assert report.removed[0].index == 0

    def test_non_dict_payload_is_not_ok(self):
        assert not parse_review_report("不是对象").ok


class TestReviewReportDescribe:
    def test_empty_when_nothing_changed(self):
        assert ReviewReport().describe() == ""

    def test_lists_what_was_removed_and_fixed(self):
        report = ReviewReport(
            removed=[ReviewNote(subject="广告题", reason="问怎么领资料", index=3)],
            fixed=[ReviewNote(subject="答案错了", reason="B 改成 C", index=1)],
        )
        text = report.describe()
        assert "第 3 题" in text and "问怎么领资料" in text
        assert "第 1 题" in text and "B 改成 C" in text

    def test_failure_is_stated_plainly(self):
        assert "没成功" in ReviewReport(ok=False, error="网络断了").describe()
