"""从《高等职业教育专科英语课程标准》PDF 里抽出官方词汇表。

湖北专升本《大学英语》考纲写着"以《高等职业教育专科英语课程标准（2021年版）》
为主要依据"，而那份标准的**附录就是一份 3000 词的词汇表**——
这就是官方指定的考试词汇范围，比自己找的词库靠谱。

词表里带分级标记：
    无标记 = 入学时应掌握（2100 个）
    *      = 基础模块词汇（500 个）
    **     = 拓展模块词汇（400 个）

用法：
    .venv\\Scripts\\python.exe scripts\\extract_syllabus_vocab.py "标准.pdf"

输出到 notes/ 下（已 gitignore，不会进仓库）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "notes"

# 词表从这一页开始。前面是正文，页数变了就调这个。
VOCAB_START_PAGE = 20

# 合法词条：字母开头，允许撇号、连字符、斜杠（a/an、actor/actress）、点号
WORD = re.compile(r"[A-Za-z][A-Za-z'’./-]*$")

CORE, BASE, EXT = "核心", "基础", "拓展"


def extract(pdf: Path) -> list[tuple[str, str]]:
    """返回 [(词条, 分级)]。"""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    chunks = [
        page.extract_text() or ""
        for index, page in enumerate(reader.pages, start=1)
        if index >= VOCAB_START_PAGE
    ]
    tokens = "\n".join(chunks).split()

    entries: list[tuple[str, str]] = []
    for token in tokens:
        if token in ("*", "**"):
            # 标记是跟在词后面的，改前一个词的分级
            if entries:
                word, _ = entries[-1]
                entries[-1] = (word, BASE if token == "*" else EXT)
            continue
        if not WORD.match(token):
            continue
        entries.append((token, CORE))
    return entries


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    if len(sys.argv) < 2:
        print("用法：python scripts/extract_syllabus_vocab.py <课程标准.pdf>")
        return 1

    pdf = Path(sys.argv[1])
    if not pdf.is_file():
        print(f"找不到文件：{pdf}")
        return 1

    entries = extract(pdf)
    if len(entries) < 1000:
        print(f"只抽到 {len(entries)} 条，看着不对——词表起始页可能要调（现在设的是 {VOCAB_START_PAGE}）")
        return 1

    counts = {kind: sum(1 for _, level in entries if level == kind) for kind in (CORE, BASE, EXT)}
    print(f"共抽出 {len(entries)} 条")
    for kind, number in counts.items():
        print(f"  {kind}  {number:>5}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # 全部词条，一行一个——背单词 App 基本都是这个格式
    everything = OUT_DIR / "专升本英语词汇-全部.txt"
    everything.write_text(
        "\n".join(word for word, _ in entries) + "\n", encoding="utf-8"
    )
    print(f"\n已写出：{everything}")

    # 去掉拓展模块那 400 个剩下的——考试"够用为度"，优先背这些
    core_only = [word for word, level in entries if level != EXT]
    core_file = OUT_DIR / "专升本英语词汇-去掉拓展.txt"
    core_file.write_text("\n".join(core_only) + "\n", encoding="utf-8")
    print(f"已写出：{core_file}（{len(core_only)} 条，去掉了拓展模块的 {counts[EXT]} 条）")

    print("\n导入背单词 App 时，一行一个词就能认。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
