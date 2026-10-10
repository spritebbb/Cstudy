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
import base64
import io
import re
import subprocess
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import DeepSeekClient, QuestionImporter  # noqa: E402
from cstudy.ai import prompts  # noqa: E402
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


# ---------- 扫描件 OCR ----------

# 渲染倍数：2.0 ≈ 144dpi。够看清试卷上的小字，图片又不会大到浪费 token。
OCR_SCALE = 2.0


def ocr_pdf(path: Path, client: DeepSeekClient, on_page=None) -> str:
    """把扫描版 PDF 逐页渲染成图，交给 AI 抄成文字。

    用 AI 而不是装 Tesseract/PaddleOCR：**不用新装任何东西**（pypdfium2 已经在用），
    而且它对代码和公式的识别比传统 OCR 好得多——试卷里全是代码。
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(path))
    total = len(document)
    pages: list[str] = []
    for index, page in enumerate(document, start=1):
        if on_page:
            on_page(index, total)
        image = page.render(scale=OCR_SCALE).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode()

        text = client.chat(
            [
                {"role": "system", "content": prompts.OCR_SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompts.ocr_user_prompt(index, total)},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
            temperature=0,
        )
        pages.append(text)
    return "\n\n".join(pages)


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


def is_answer_key(path: Path) -> bool:
    """这个文件是不是"另一个文件的答案"？是的话就不必再当题目导一遍。

    省一次白跑的 AI 调用——答案文件里本来就没有题干，导出来必然是 0 道。
    """
    if not ANSWER_HINT.search(path.stem):
        return False
    mine = _normalize_stem(path.stem)
    if not mine:
        return False
    return any(
        other.is_file()
        and other != path
        and other.suffix.lower() in SUPPORTED
        and _normalize_stem(other.stem) == mine
        for other in path.parent.iterdir()
    )


def rough_question_count(text: str) -> int:
    return len(QUESTION_START.findall(text))


# ---------- 配套答案文件 ----------

# 文件名里带这些词的，多半是答案文件
ANSWER_HINT = re.compile(r"答案|参考")
ANSWER_SUFFIX = re.compile(r"(参考答案|答案与解析|参考答案与解析|试题答案|答案|参考)")


def _normalize_stem(stem: str) -> str:
    """去掉"参考答案"这类词，好让 `模拟试卷11` 和 `模拟试卷11参考答案` 对上。"""
    return ANSWER_SUFFIX.sub("", stem).strip(" -_()（）")


def find_answer_file(path: Path) -> Path | None:
    """找这个文件配套的答案文件——题库常见排版是题目一个文件、答案另一个文件。

    只按**文件名**配对：`模拟试卷11.doc` ↔ `模拟试卷11参考答案.doc`。

    **认不出就返回 None，宁可让 AI 推断也不要配错**——配错的答案看着像真的，
    比标着"推断"的更坑。曾经想"文件夹里只有两个文件就配一对"，
    但 `谭浩强课后答案.pdf` 和 `小绿书.pdf` 正好同处一个文件夹，会把答案配错。
    这种拿不准的，用 `--answer-key` 手动指定。
    """
    if not path.parent.is_dir():
        return None

    mine = _normalize_stem(path.stem)
    if not mine:
        return None
    for candidate in path.parent.iterdir():
        if (
            candidate.is_file()
            and candidate != path
            and candidate.suffix.lower() in SUPPORTED
            and ANSWER_HINT.search(candidate.stem)
            and _normalize_stem(candidate.stem) == mine
        ):
            return candidate
    return None


# ---------- 主流程 ----------


def main() -> int:
    # 中文 Windows 上 stdout 默认是 GBK，中文会变乱码
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="把题库文件导进题库")
    parser.add_argument("paths", nargs="+", help="文件或文件夹，可以给多个")
    parser.add_argument("--dry-run", action="store_true", help="只抽文字数一数，不调 AI 不入库")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几个文件（试水用）")
    parser.add_argument("--force", action="store_true", help="已经导过的文件也重新导一遍")
    parser.add_argument(
        "--ocr",
        action="store_true",
        help="对抽不出文字的扫描件逐页调 AI 识别（慢、花钱，但能读扫描版）",
    )
    parser.add_argument(
        "--answer-key",
        default="",
        help="强制用这个文件当本次所有文件的配套答案（文件名配不上时手动指定）",
    )
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

    # OCR 要调 AI，所以只在开了 --ocr 时才提前建客户端
    client = DeepSeekClient(config) if args.ocr else None

    extracted: list[tuple[Path, str]] = []
    skipped: list[tuple[Path, str]] = []
    ocr_used: list[Path] = []

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
            if is_answer_key(path):
                skipped.append((path, "是另一个文件的答案，不单独导"))
                continue
            can_ocr = args.ocr and client is not None and path.suffix.lower() == ".pdf"

            try:
                text = extract(path, workdir)
            except Exception as exc:
                if not can_ocr:
                    skipped.append((path, f"抽文字失败：{exc}"))
                    continue
                # 有些 PDF 连解析库都读不了（文件损坏），但渲染成图往往还能看，
                # 交给 OCR 再试一次
                print(f"  {path.name}：直接读失败（{exc}），改用 OCR…")
                text = ""

            if len(text.strip()) < MIN_CHARS:
                # 扫描件：文字抽不出来，开了 --ocr 就逐页渲染成图让 AI 认
                if can_ocr:
                    print(f"  {path.name}：文字太少，改用 OCR 逐页识别…")
                    try:
                        text = ocr_pdf(
                            path, client, on_page=lambda i, n: print(f"    OCR {i}/{n} 页")
                        )
                    except Exception as exc:
                        skipped.append((path, f"OCR 失败：{exc}"))
                        continue
                    if len(text.strip()) < MIN_CHARS:
                        skipped.append((path, f"OCR 后也只有 {len(text.strip())} 字"))
                        continue
                    ocr_used.append(path)
                    extracted.append((path, text))
                    continue

                scanned = path.suffix.lower() in {".pdf", ".doc", ".docx"}
                reason = "大概是扫描件，要 OCR 才读得出来" if scanned else "内容太短"
                skipped.append((path, f"只抽出 {len(text.strip())} 字，{reason}"))
                continue
            extracted.append((path, text))

        # 题目和答案常常分成两个文件（`模拟试卷11.doc` + `模拟试卷11参考答案.doc`）。
        # 把配套答案抽出来，导入时附给每一段，免得 AI 只能猜答案。
        references: dict[Path, str] = {}
        answer_cache: dict[Path, str] = {}
        forced_key = Path(args.answer_key) if args.answer_key else None
        for path, _ in extracted:
            answer_file = forced_key or find_answer_file(path)
            if answer_file is None:
                continue
            if answer_file not in answer_cache:
                text = ""
                try:
                    text = extract(answer_file, workdir)
                except Exception:
                    text = ""
                if (
                    len(text.strip()) < MIN_CHARS
                    and args.ocr
                    and client is not None
                    and answer_file.suffix.lower() == ".pdf"
                ):
                    print(f"  配套答案《{answer_file.name}》是扫描件，OCR…")
                    try:
                        text = ocr_pdf(answer_file, client)
                    except Exception as exc:
                        print(f"    OCR 失败：{exc}")
                        text = ""
                answer_cache[answer_file] = text
            if answer_cache[answer_file].strip():
                references[path] = f"来自《{answer_file.name}》：\n{answer_cache[answer_file]}"

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
        if ocr_used:
            print(f"其中 {len(ocr_used)} 个是 OCR 出来的："
                  + "、".join(p.name[:20] for p in ocr_used[:5]))
        if references:
            print(f"其中 {len(references)} 个文件找到了配套答案，导入时会附给 AI 对答案")

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

    # --ocr 时上面已经建过了，复用同一个连接
    client = client or DeepSeekClient(config)
    importer = QuestionImporter(client, config)

    existing = {
        " ".join(str(row["question"]).split()) for row in db.list_questions()
    }

    grand_parsed = grand_saved = grand_skipped = grand_dup = 0
    failures: list[tuple[Path, str]] = []
    empty: list[tuple[Path, str]] = []

    for index, (path, text) in enumerate(pending, start=1):
        print(f"\n[{index}/{len(pending)}] {path.name}")
        reference = references.get(path, "")
        if reference:
            print(f"    附上配套答案（{len(reference)} 字）")
        try:
            outcome = importer.parse(
                text, on_progress=lambda s: print(f"    · {s}"), reference=reference
            )
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
