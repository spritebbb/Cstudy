"""批量导入题库文件时"抽文字"那一段的测试。

抽文字出问题会**静默丢题**——AI 拿到半截文字也照样返回结果，只是少几道。
所以这里盯的是"能不能把文件里的字完整、成行地抽出来"。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest  # noqa: E402

from import_bank import collect, docx_text, extract, rough_question_count  # noqa: E402


def make_docx(path: Path, paragraphs: list[str]) -> None:
    """造一个最小可用的 .docx——我们只读 word/document.xml，别的部件不需要。"""
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


class TestDocxText:
    def test_each_paragraph_becomes_a_line(self, tmp_path):
        """每段一行——全挤成一行的话，题号和题干就分不开了。"""
        path = tmp_path / "a.docx"
        make_docx(path, ["1. 第一题", "A. 甲", "2. 第二题"])

        lines = [line for line in docx_text(path).splitlines() if line]
        assert lines == ["1. 第一题", "A. 甲", "2. 第二题"]

    def test_unescapes_html_entities(self, tmp_path):
        """docx 里 `&` 是 `&amp;`、`<` 是 `&lt;`，不解开的话题目会变成乱码。"""
        path = tmp_path / "b.docx"
        make_docx(path, ["a &amp;&amp; b", "if (a &lt; b)"])

        text = docx_text(path)
        assert "a && b" in text
        assert "if (a < b)" in text

    def test_does_not_leak_xml_attributes(self, tmp_path):
        """以前踩过的坑：标签没整个吃掉，`w:rsidR="..."` 会漏进正文。"""
        path = tmp_path / "c.docx"
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?><w:document><w:body>'
            '<w:p w:rsidR="00D51B85" w:rsidRDefault="00D51B85">'
            "<w:r><w:t>1. 题目</w:t></w:r></w:p></w:body></w:document>"
        )
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)

        text = docx_text(path)
        assert text.strip() == "1. 题目"
        assert "rsid" not in text


class TestExtract:
    def test_unsupported_suffix_says_so(self, tmp_path):
        path = tmp_path / "a.xlsx"
        path.write_bytes(b"whatever")

        with pytest.raises(RuntimeError, match="还没支持"):
            extract(path, tmp_path)

    def test_txt_is_read_as_utf8(self, tmp_path):
        path = tmp_path / "a.txt"
        path.write_text("1. 甲\n2. 乙\n", encoding="utf-8")

        assert "1. 甲" in extract(path, tmp_path)


class TestCollect:
    def test_walks_subfolders(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.pdf").write_bytes(b"x")
        (tmp_path / "sub" / "b.doc").write_bytes(b"x")

        found = {p.name for p in collect([str(tmp_path)])}

        assert found == {"a.pdf", "b.doc"}

    def test_accepts_a_single_file(self, tmp_path):
        path = tmp_path / "a.pdf"
        path.write_bytes(b"x")

        assert collect([str(path)]) == [path]

    def test_missing_path_is_ignored(self, tmp_path, capsys):
        assert collect([str(tmp_path / "nope")]) == []
        assert "路径不存在" in capsys.readouterr().out


class TestRoughQuestionCount:
    def test_counts_various_numbering_styles(self):
        text = "1. 甲\n2、乙\n3) 丙\n4）丁\n不是题号的一行\n"
        assert rough_question_count(text) == 4

    def test_does_not_count_numbers_mid_line(self):
        assert rough_question_count("答案：1. 甲 2. 乙") == 0
