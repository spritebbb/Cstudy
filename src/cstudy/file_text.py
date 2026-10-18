"""从题库文件里抽文字：PDF / Word / txt。

原先这几个函数长在 `scripts/import_bank.py` 里。现在「考前突击」那栏也要
按同样的方式读文件，所以搬到包里来（一处维护），脚本那边改成从这里 import，
对外名字不变，老调用方不受影响。

**这里只负责"把文件变成一段文字"，不认识题目。** 解析成题目在那之后做。
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from html import unescape
from pathlib import Path

# LibreOffice 在标准安装位置。.doc（老式二进制 Word）只有它能读。
SOFFICE = Path(r"C:\Program Files\LibreOffice\program\soffice.exe")

SUPPORTED = {".pdf", ".docx", ".doc", ".txt", ".md"}
SKIP_SUFFIXES = {".xls", ".xlsx", ".ppt", ".pptx", ".jpg", ".png", ".zip", ".rar"}

# 抽出来不到这么多字，基本就是扫描件或空文件——OCR 是另一个量级的事，先跳过。
MIN_CHARS = 200


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
    """按后缀把文件抽成纯文字。抽不了就抛中文异常（用户看得懂的那种）。"""
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
