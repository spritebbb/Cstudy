"""把题库文件夹里的文件导进题库。

界面上的"导入题库"只能粘贴文字，几百个文件不可能手动复制，所以走脚本。

    # 先干跑：只抽文字、数一数，不调 AI、不入库（不花钱）
    .venv\\Scripts\\python.exe scripts\\import_bank.py "D:\\题库文件夹" --dry-run

    # 确认抽出来的文字没问题，再真导
    .venv\\Scripts\\python.exe scripts\\import_bank.py "D:\\题库文件夹"

    # 只导一个文件、或只导前 3 个先试水
    .venv\\Scripts\\python.exe scripts\\import_bank.py "D:\\某套真题.pdf"
    .venv\\Scripts\\python.exe scripts\\import_bank.py "D:\\题库" --limit 3

**会花 AI 的钱**：每 8000 字一段、一段一次调用，脚本开跑前会把预计段数报出来。
用 `--dry-run` 先看不花钱的结果。

已经导过的文件不会重复导（按 `questions.source` 记，见 `--force`）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import DeepSeekClient, QuestionImporter  # noqa: E402
from cstudy.ai.importer import DEFAULT_MAX_CHARS, split_by_questions  # noqa: E402
from cstudy.config import get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402
from cstudy.models import DataError  # noqa: E402

# LibreOffice 在标准安装位置。.doc（老式二进制 Word）只有它能读。
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")

SUPPORTED = {".pdf", ".docx", ".doc", ".txt", ".md"}
SKIP_SUFFIXES = {".xls", ".xlsx", ".ppt", ".pptx", ".jpg", ".png", ".zip", ".rar"}

# 抽出来不到这么多字，基本就是扫描件或空文件——OCR 是另一个量级的事，先跳过。
MIN_CHARS = 200

QUESTION_START = re.compile(r"^\s*\d{1,3}\s*[.、,，)）]", re.MULTILINE)


# ---------- 各格式抽文字 ----------


def pdf_text(path: Path) -> str:
    """抽 PDF 文字。

    **优先 pypdfium2**：同一份 PDF，pypdf 会把每个词甚至每个字符拆成一行
    （"专升本\\nC\\n语言"），AI 得多花力气去拼；pypdfium2 抽出来是正常成行的。
    字数是差不多的，差别全在可读性上。
    """
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        return "\n".join(page.get_textpage().get_text_range() for page in doc)
    except ImportError:
        pass
    except Exception:
        pass  # 少数加密 PDF 它读不了，退回 pypdf

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        # 题库 PDF 大多只设了"禁止编辑"的空密码，空串就能解开
        reader.decrypt("")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def docx_text(path: Path) -> str:
    """docx 本质是个 zip，直接读里面的 document.xml，不用装 python-docx。"""
    with zipfile.ZipFile(path) as archive:
        xml = archive.read("word/document.xml").decode("utf-8", "ignore")
    xml = re.sub(r"<w:p\b[^>]*>", "\n", xml)
    xml = re.sub(r"<w:br\b[^>]*/?>", "\n", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return unescape(xml)


def doc_text(path: Path, workdir: Path) -> str:
    """老式 .doc 交给 LibreOffice 转成 txt。"""
    if not SOFFICE.exists():
        raise RuntimeError(f"没找到 LibreOffice（{SOFFICE}），.doc 读不了")
    # 单独给一个用户配置目录，免得跟用户开着的 LibreOffice 抢
    profile = workdir / "lo-profile"
    result = subprocess.run(
        [
            str(SOFFICE),
            f"-env:UserInstallation=file:///{profile.as_posix()}",
            "--headless",
            "--convert-to",
            "txt:Text (encoded):UTF8",
            "--outdir",
            str(workdir),
            str(path),
        ],
        capture_output=True,
        timeout=180,
    )
    converted = workdir / (path.stem + ".txt")
    if not converted.exists():
        detail = (result.stderr or b"").decode("utf-8", "ignore").strip()
        raise RuntimeError(f"LibreOffice 转换失败：{detail[:200] or '没有输出文件'}")
    text = converted.read_text("utf-8", "ignore")
    converted.unlink()
    return text


def extract(path: Path, workdir: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_text(path)
    if suffix == ".docx":
        return docx_text(path)
    if suffix == ".doc":
        return doc_text(path, workdir)
    if suffix in {".txt", ".md"}:
        return path.read_text("utf-8", "ignore")
    raise RuntimeError(f"这种格式还没支持：{suffix}")


# ---------- 收集文件 ----------


def collect(paths: list[str]) -> list[Path]:
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*") if p.is_file()))
        elif path.is_file():
            found.append(path)
        else:
            print(f"路径不存在，跳过：{raw}")
    return found


def rough_question_count(text: str) -> int:
    return len(QUESTION_START.findall(text))


# ---------- 主流程 ----------


def main() -> int:
    # 中文 Windows 上 stdout 默认是 GBK，中文会变乱码
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="把题库文件导进题库")
    parser.add_argument("paths", nargs="+", help="文件或文件夹，可以给多个")
    parser.add_argument("--dry-run", action="store_true", help="只抽文字数一数，不调 AI 不入库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几个文件（试水用）")
    parser.add_argument("--force", action="store_true", help="已经导过的文件也重新导一遍")
    parser.add_argument("--yes", action="store_true", help="不确认，直接开导")
    args = parser.parse_args()

    config = get_config()
    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))
    db = Database(config.path("database"))

    files = collect(args.paths)
    if not files:
        print("没找到任何文件。")
        return 1

    # 已经导过的文件（按 source 记），默认不重复导
    done: set[str] = set()
    if not args.force:
        done = {
            str(row["source"])
            for row in db.conn.execute(
                "SELECT DISTINCT source FROM questions WHERE source != ''"
            )
        }

    print(f"共 {len(files)} 个文件，开始抽文字…\n")

    extracted: list[tuple[Path, str]] = []
    skipped: list[tuple[Path, str]] = []

    with tempfile.TemporaryDirectory(prefix="cstudy_bank_") as tmp:
        workdir = Path(tmp)
        for path in files:
            # --limit 限的是"能用的文件"，所以放在抽文字成功之后再数
            if args.limit and len(extracted) >= args.limit:
                break
            if path.suffix.lower() in SKIP_SUFFIXES:
                skipped.append((path, f"{path.suffix} 格式还没支持"))
                continue
            if path.suffix.lower() not in SUPPORTED:
                continue
            try:
                text = extract(path, workdir)
            except Exception as exc:
                skipped.append((path, f"抽文字失败：{exc}"))
                continue

            if len(text.strip()) < MIN_CHARS:
                scanned = path.suffix.lower() in {".pdf", ".doc", ".docx"}
                reason = "大概是扫描件，要 OCR 才读得出来" if scanned else "内容太短"
                skipped.append((path, f"只抽出 {len(text.strip())} 字，{reason}"))
                continue
            extracted.append((path, text))

    # ---- 干跑：只报数 ----
    if args.dry_run:
        total_chars = sum(len(text) for _, text in extracted)
        total_questions = sum(rough_question_count(text) for _, text in extracted)
        print(f"{'文件':<52}{'字数':>8}{'粗数题目':>9}")
        print("-" * 72)
        for path, text in extracted:
            print(f"{path.name[:50]:<52}{len(text):>8}{rough_question_count(text):>9}")
        print("-" * 72)
        print(f"可导入 {len(extracted)} 个文件，共 {total_chars} 字，粗数 {total_questions} 道")

        if skipped:
            print(f"\n跳过 {len(skipped)} 个：")
            for path, reason in skipped:
                print(f"  · {path.name[:44]:<46}{reason}")

        max_chars = int(config.get("import.max_chars_per_call", DEFAULT_MAX_CHARS) or DEFAULT_MAX_CHARS)
        chunks = sum(len(split_by_questions(text, max_chars)) for _, text in extracted)
        print(f"\n真导的话大约要调 AI {chunks} 次（每段一次）。")
        print("确认没问题就去掉 --dry-run 再跑一遍。")
        return 0

    # ---- 真导 ----
    pending = [(p, t) for p, t in extracted if str(p) not in done]
    already = len(extracted) - len(pending)
    if already:
        print(f"有 {already} 个文件之前导过了，跳过（要重导加 --force）。")

    if not pending:
        print("没有需要导的文件。")
        return 0

    total_chars = sum(len(t) for _, t in pending)
    print(f"待导 {len(pending)} 个文件、{total_chars} 字，会调用 AI 若干次并写入题库。")
    if not args.yes:
        answer = input("继续吗？输入 y 回车：").strip().lower()
        if answer != "y":
            print("已取消。")
            return 0

    client = DeepSeekClient(config)
    importer = QuestionImporter(client, config)

    existing = {
        " ".join(str(row["question"]).split()) for row in db.list_questions()
    }

    grand_parsed = grand_saved = grand_skipped = grand_dup = 0
    failures: list[tuple[Path, str]] = []
    empty: list[tuple[Path, str]] = []

    for index, (path, text) in enumerate(pending, start=1):
        print(f"\n[{index}/{len(pending)}] {path.name}")
        try:
            outcome = importer.parse(text, on_progress=lambda s: print(f"    · {s}"))
        except DataError as exc:
            # 这个文件里本来就没题（参考答案、代码片段），或者全是选择题被滤光了。
            # **不是出错**——所以单独归一类，别吓用户。
            print(f"    跳过：{exc}")
            empty.append((path, str(exc)))
            continue
        except Exception as exc:
            print(f"    解析失败：{exc}")
            failures.append((path, str(exc)))
            continue

        # 跟库里已有的比一遍（忽略空白差异），跨文件重复的题不再入库
        fresh = [q for q in outcome.questions if " ".join(q.question.split()) not in existing]
        cross_dup = len(outcome.questions) - len(fresh)

        print(f"    {outcome.summary()}")
        if cross_dup:
            print(f"    其中 {cross_dup} 道跟库里已有的重复，跳过")

        if fresh:
            db.save_questions(fresh, note_id=None, source=str(path))
            existing.update(" ".join(q.question.split()) for q in fresh)

        grand_parsed += outcome.parsed
        grand_saved += len(fresh)
        grand_skipped += sum(outcome.skipped.values())
        grand_dup += outcome.duplicates + cross_dup

    print("\n" + "=" * 72)
    print(f"解析出 {grand_parsed} 道，过滤掉 {grand_skipped} 道，去重 {grand_dup} 道，"
          f"入库 {grand_saved} 道")
    if empty:
        # 参考答案、代码片段这类文件里本来就没题，列出来让用户知道少了什么就行
        print(f"\n{len(empty)} 个文件里没有你要的题型，跳过了（不是出错）：")
        for path, reason in empty[:12]:
            print(f"  · {path.name[:44]:<46}{reason[:38]}")
        if len(empty) > 12:
            print(f"  · …还有 {len(empty) - 12} 个，完整的在 logs/cstudy.log")
    if failures:
        print(f"\n有 {len(failures)} 个文件**真出错了**（这些要看看）：")
        for path, reason in failures:
            print(f"  · {path.name[:44]:<46}{reason[:60]}")
    print(f"\n库里现在共 {db.stats().get('题目', '?')} 道题。")
    print("回到主窗口点「开始刷题」就能刷了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
