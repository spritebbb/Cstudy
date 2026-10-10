"""建数据库表。

可以重复运行，不会丢数据。
    python scripts/init_db.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    config = get_config()
    setup_logging(config.root / "logs/cstudy.log", level="INFO")

    db = Database(config.path("database"))
    db.init_schema()

    print(f"数据库已就绪：{config.path('database')}")
    for name, count in db.stats().items():
        print(f"  {name}：{count}")
    print()
    print("下一步：python scripts/seed_syllabus.py  （灌入考纲知识点）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
