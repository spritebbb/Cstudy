"""把 src/cstudy/syllabus.yaml 里的考纲知识点灌进数据库。

可以重复运行：已存在的知识点只更新章节，不会重复插入，
也不会删掉你自己手动加的内容。
    python scripts/seed_syllabus.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402

SYLLABUS_FILE = Path(__file__).resolve().parents[1] / "src" / "cstudy" / "syllabus.yaml"


def main() -> int:
    config = get_config()
    setup_logging(config.root / "logs/cstudy.log", level="INFO")

    if not SYLLABUS_FILE.exists():
        print(f"找不到考纲文件：{SYLLABUS_FILE}", file=sys.stderr)
        return 1

    chapters = yaml.safe_load(SYLLABUS_FILE.read_text(encoding="utf-8")) or []
    if not isinstance(chapters, list):
        print("考纲文件格式不对，根节点应该是一个列表。", file=sys.stderr)
        return 1

    db = Database(config.path("database"))
    db.init_schema()

    added = 0
    order = 0
    for entry in chapters:
        if not isinstance(entry, dict):
            continue
        chapter = str(entry.get("chapter", "")).strip()
        for point in entry.get("points") or []:
            name = str(point).strip()
            if not name:
                continue
            order += 1
            db.upsert_syllabus_point(name=name, chapter=chapter, sort_order=order)
            added += 1

    print(f"考纲已灌入：{len(chapters)} 个章节，{added} 个知识点")
    print(f"数据库现在共有 {db.count('syllabus_points')} 个知识点")
    return 0


if __name__ == "__main__":
    sys.exit(main())
