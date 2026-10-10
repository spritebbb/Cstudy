"""把题目的知识点对齐到考纲。

题库里的 `point` 是 AI 自由写的文本（"递归函数"、"带参宏定义"），跟考纲的 64 个
知识点（"函数的递归调用"）对不上，所以没法统计"哪个知识点还没练到"。
这个脚本让 AI 把每个不同的标签映射到考纲知识点，写进 `questions.point_id`。

**可重复跑**：只处理 `point_id` 还是 NULL 的题，已经映射过的一律不动。
所以导入了新题之后再跑一遍就行。

    # 先干跑：只打印映射表，不写库、不花钱（不调 AI 的部分）
    .venv\\Scripts\\python.exe scripts\\map_syllabus.py --dry-run
    # 确认映射合理再真写
    .venv\\Scripts\\python.exe scripts\\map_syllabus.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import DeepSeekClient, prompts  # noqa: E402
from cstudy.config import get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402
from cstudy.models import DataError, parse_json_object  # noqa: E402

# 一批送多少标签。太多容易让模型漏项，太少又费调用。
BATCH_SIZE = 60


def unmapped_tags(db: Database) -> list[str]:
    """还没映射过的标签，按出现次数从多到少。"""
    rows = db.conn.execute(
        """
        SELECT point, COUNT(*) AS n FROM questions
        WHERE point_id IS NULL AND trim(coalesce(point, '')) != ''
        GROUP BY point
        ORDER BY n DESC, point
        """
    ).fetchall()
    return [str(row["point"]) for row in rows]


def map_batch(
    client: DeepSeekClient, syllabus_names: list[str], tags: list[str]
) -> dict[str, str | None]:
    """让 AI 把一批标签映射到考纲知识点。返回 {标签: 知识点名字 或 None}。"""
    raw = client.chat_json(
        [
            {"role": "system", "content": prompts.POINT_MAP_SYSTEM},
            {
                "role": "user",
                "content": prompts.point_map_user_prompt(syllabus_names, tags),
            },
        ]
    )
    data = parse_json_object(raw)
    items = data.get("mapping")
    if not isinstance(items, list):
        raise DataError("AI 没返回 mapping 列表")

    valid = set(syllabus_names)
    mapping: dict[str, str | None] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        point = item.get("point")
        point = str(point).strip() if point else ""
        # 模型偶尔会自己造名字，清单里没有的一律当作没配上
        mapping[tag] = point if point in valid else None
    return mapping


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="把题目知识点对齐到考纲")
    parser.add_argument("--dry-run", action="store_true", help="只打印映射表，不写库")
    parser.add_argument("--yes", action="store_true", help="不确认，直接写")
    args = parser.parse_args()

    config = get_config()
    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))
    db = Database(config.path("database"))

    points = db.list_syllabus_points()
    if not points:
        print("库里还没有考纲。先跑 scripts/seed_syllabus.py。")
        return 1

    names = [str(row["name"]) for row in points]
    by_name = {str(row["name"]): int(row["id"]) for row in points}

    tags = unmapped_tags(db)
    if not tags:
        print("没有需要映射的题——都已经对齐过了。")
        return 0

    total_questions = db.conn.execute(
        "SELECT COUNT(*) FROM questions WHERE point_id IS NULL "
        "AND trim(coalesce(point, '')) != ''"
    ).fetchone()[0]
    batches = (len(tags) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"考纲 {len(names)} 个知识点，待映射 {len(tags)} 个不同标签"
          f"（涉及 {total_questions} 道题），分 {batches} 批。\n")

    if not args.dry_run and not args.yes:
        answer = input(f"会调用 AI {batches} 次。继续吗？输入 y 回车：").strip().lower()
        if answer != "y":
            print("已取消。")
            return 0

    client = DeepSeekClient(config)
    result: dict[str, str | None] = {}
    for index in range(batches):
        batch = tags[index * BATCH_SIZE : (index + 1) * BATCH_SIZE]
        print(f"[{index + 1}/{batches}] {len(batch)} 个标签…")
        try:
            result.update(map_batch(client, names, batch))
        except Exception as exc:
            print(f"    这批失败，跳过：{exc}")
            continue

    matched = {tag: name for tag, name in result.items() if name}
    unmatched = [tag for tag in tags if not result.get(tag)]

    print(f"\n对齐上 {len(matched)} 个标签，{len(unmatched)} 个对不上（保持未映射）。")

    if matched:
        print("\n映射表（抽查 25 条）：")
        for tag, name in list(matched.items())[:25]:
            print(f"  {tag[:24]:<26} → {name[:34]}")
    if unmatched:
        print("\n对不上的标签（抽查 15 条）：")
        for tag in unmatched[:15]:
            print(f"  {tag[:50]}")

    if args.dry_run:
        print("\n（干跑，没写库。去掉 --dry-run 再跑一遍就写进去。）")
        return 0

    changed = db.assign_points({tag: by_name[name] for tag, name in matched.items()})
    print(f"\n已写入：{changed} 道题对齐到了考纲。")

    remaining = db.conn.execute(
        "SELECT COUNT(*) FROM questions WHERE point_id IS NULL"
    ).fetchone()[0]
    print(f"还有 {remaining} 道没对齐（标签对不上考纲的题）。")
    print("打开软件点「考纲」标签页就能看到覆盖度了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
