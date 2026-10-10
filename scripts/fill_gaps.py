"""给考纲上题量不足的知识点补题。

覆盖度一查就知道哪些知识点没题或题太少。这个脚本对着那些知识点出题，
出完过一遍 AI 复查，再入库。

    # 先看哪些知识点题少（不调 AI、不花钱）
    .venv\\Scripts\\python.exe scripts\\fill_gaps.py --dry-run

    # 真补：每个知识点补 gap.count 道（见 config.yaml）
    .venv\\Scripts\\python.exe scripts\\fill_gaps.py

    # 只补指定的知识点
    .venv\\Scripts\\python.exe scripts\\fill_gaps.py --point "指针的概念、类型与分类"

**每道题都必须有答案**：`Question` 在构造时就拒绝空答案，这里入库前再查一遍，
最后还会统计一次库里有没有没答案的题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import DeepSeekClient, GapFiller, Reviewer  # noqa: E402
from cstudy.config import get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402
from cstudy.models import Question  # noqa: E402

# 题数少于这个数的知识点就算"薄"，要补
DEFAULT_BELOW = 5


def thin_points(db: Database, below: int) -> list:
    """题量不足的知识点，题最少的排前面。"""
    rows = [
        row for row in db.coverage() if int(row["question_count"]) < below
    ]
    return sorted(rows, key=lambda row: int(row["question_count"]))


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="给题少的知识点补题")
    parser.add_argument("--dry-run", action="store_true", help="只列出要补哪些，不调 AI")
    parser.add_argument("--below", type=int, default=DEFAULT_BELOW, help="少于几道算题少")
    parser.add_argument("--point", default="", help="只补这一个知识点（按名字精确匹配）")
    parser.add_argument("--yes", action="store_true", help="不确认，直接开补")
    args = parser.parse_args()

    config = get_config()
    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))
    db = Database(config.path("database"))

    if not db.list_syllabus_points():
        print("库里还没有考纲。先跑 scripts/seed_syllabus.py。")
        return 1

    targets = thin_points(db, args.below)
    if args.point:
        targets = [row for row in targets if str(row["name"]) == args.point]
        if not targets:
            print(f"没找到题少的「{args.point}」——要么名字不对，要么它题已经够多了。")
            return 1

    if not targets:
        print(f"没有题数少于 {args.below} 道的知识点，不用补。")
        return 0

    per_point = int(config.get("gap.count", 6) or 6)
    print(f"题数少于 {args.below} 道的知识点共 {len(targets)} 个，"
          f"每个补 {per_point} 道，预计调用 AI {len(targets) * 2} 次左右。\n")
    for row in targets:
        print(f"  {int(row['question_count']):>3} 道  {row['chapter']} / {row['name']}")

    if args.dry_run:
        print("\n（干跑，没调 AI。去掉 --dry-run 再跑一遍就真补。）")
        return 0

    if not args.yes:
        answer = input("\n继续吗？输入 y 回车：").strip().lower()
        if answer != "y":
            print("已取消。")
            return 0

    client = DeepSeekClient(config)
    filler = GapFiller(client, config)
    reviewer = Reviewer(client, config)

    added_total = 0
    failed: list[tuple[str, str]] = []

    for index, row in enumerate(targets, start=1):
        name = str(row["name"])
        chapter = str(row["chapter"] or "")
        print(f"\n[{index}/{len(targets)}] {name}（现在 {row['question_count']} 道）")
        try:
            questions = filler.generate(name, chapter)
        except Exception as exc:
            print(f"    出题失败，跳过：{exc}")
            failed.append((name, str(exc)))
            continue

        if not questions:
            print("    这个知识点没出出来题（多半是不适合出题，比如纯概念）")
            continue

        # 复查：答案错的改掉、废题删掉。复查的"依据"就是知识点本身。
        basis = f"知识点：{name}\n所属章节：{chapter}\n这是针对该知识点出的练习题。"
        questions, report = reviewer.review_questions(questions, basis)
        if not report.ok:
            print("    复查没成功，这批题没经过二次把关")

        # **每道题都要有答案**：复查也可能改出空答案，入库前再挡一次
        ready = [q for q in questions if q.answer.strip()]
        dropped = len(questions) - len(ready)
        if dropped:
            print(f"    有 {dropped} 道没答案，丢掉了")

        if not ready:
            print("    复查之后一道都不剩，跳过")
            continue

        # 补题的题本来就属于这个知识点，直接连上考纲
        for question in ready:
            question.point_id = int(row["id"])

        ids = db.save_questions(ready, note_id=None, source=f"考纲补题：{name}")
        added_total += len(ids)
        print(f"    入库 {len(ids)} 道")

    print("\n" + "=" * 66)
    print(f"一共补了 {added_total} 道题。")
    if failed:
        print(f"有 {len(failed)} 个知识点出题失败：")
        for name, reason in failed:
            print(f"  · {name}：{reason[:60]}")

    # 最后把"有没有没答案的题"查一遍——用户明确要求过
    missing = db.conn.execute(
        "SELECT COUNT(*) FROM questions WHERE answer IS NULL OR trim(answer) = ''"
    ).fetchone()[0]
    print(f"\n全库题目 {db.count('questions')} 道，其中没答案的 {missing} 道。")
    if missing:
        print("有题没答案，这不正常，把日志发过来看看。")
        return 1
    print("每道题都有答案。")
    print("打开软件点「考纲」标签页，就能看到刚才补的知识点涨上来了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
