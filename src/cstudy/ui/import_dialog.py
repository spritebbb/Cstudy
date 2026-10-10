"""导入现成题库对话框。

用户从网页、PDF 阅读器、Word 里复制题目粘进来，AI 整理成结构化题目，
**先给用户看一遍预览**，确认了才入库。

这里刻意不做 AI 复查：原文（尤其是真题）是权威的，让 AI 去"纠正"答案
反而可能把对的改错。人来把关更靠谱。
"""

from __future__ import annotations

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..ai import QuestionImporter
from ..config import Config
from ..db import Database
from ..logging_setup import get_logger
from ..models import Question
from .workers import friendly_error

log = get_logger(__name__)

PLACEHOLDER = (
    "把题目粘贴到这里。\n\n"
    "来源不限：网页、PDF 阅读器、Word、聊天记录都行，格式乱一点没关系，\n"
    "AI 会尽量整理。答案跟在题目后面或者统一放在文末都可以。"
)


class ImportWorker(QThread):
    """调 AI 解析题库文字。慢，必须放后台，否则窗口会假死。"""

    progress = pyqtSignal(str)
    succeeded = pyqtSignal(object)   # list[Question]
    failed = pyqtSignal(str)

    def __init__(self, client, config: Config, text: str) -> None:
        super().__init__()
        self._client = client
        self._config = config
        self._text = text

    def run(self) -> None:
        try:
            questions = QuestionImporter(self._client, self._config).parse(
                self._text, self.progress.emit
            )
        except Exception as exc:
            log.error("导入题库失败：%s", exc)
            self.failed.emit(friendly_error(exc))
        else:
            self.succeeded.emit(questions)


class ImportDialog(QDialog):
    saved = pyqtSignal(int)   # 入库了几道题

    def __init__(self, config: Config, db: Database, client, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._db = db
        self._client = client
        self._worker: ImportWorker | None = None
        self._questions: list[Question] = []

        self.setWindowTitle("导入题库")
        self.resize(760, 620)

        layout = QVBoxLayout(self)

        hint = QLabel(
            "从网页、PDF 或 Word 里复制题目，粘贴到下面，点「开始解析」。\n"
            "解析完会先给你看一遍，确认没问题再入库。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlaceholderText(PLACEHOLDER)
        self.text_edit.setMinimumHeight(180)
        layout.addWidget(self.text_edit)

        row = QHBoxLayout()
        self.parse_button = QPushButton("开始解析")
        self.parse_button.clicked.connect(self.start_parse)
        row.addWidget(self.parse_button)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, 1)
        layout.addLayout(row)

        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("解析结果会显示在这里。")
        layout.addWidget(self.preview, 1)

        buttons = QDialogButtonBox()
        self.save_button = buttons.addButton("保存到题库", QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        close_button = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        close_button.clicked.connect(self.reject)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ---------- 解析 ----------

    def start_parse(self) -> None:
        text = self.text_edit.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "还没内容", "先把题目粘贴到上面的框里。")
            return
        if self._worker and self._worker.isRunning():
            return

        self._questions = []
        self.preview.setMarkdown("")
        self.save_button.setEnabled(False)
        self.parse_button.setEnabled(False)
        self.status_label.setText("正在解析…")

        self._worker = ImportWorker(self._client, self._config, text)
        self._worker.progress.connect(self.status_label.setText)
        self._worker.succeeded.connect(self._on_parsed)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(lambda: self.parse_button.setEnabled(True))
        self._worker.start()

    def _on_parsed(self, questions: list[Question]) -> None:
        self._questions = list(questions)
        self.preview.setMarkdown(render_questions_markdown(self._questions))
        self.save_button.setEnabled(bool(self._questions))
        self.status_label.setText(
            f"解析出 {len(self._questions)} 道题。确认没问题就点「保存到题库」。"
        )

    def _on_failed(self, message: str) -> None:
        self.status_label.setText(message)
        QMessageBox.warning(self, "解析失败", message)

    # ---------- 入库 ----------

    def _save(self) -> None:
        if not self._questions:
            return

        try:
            existing = {
                " ".join(str(row["question"]).split()) for row in self._db.list_questions()
            }
        except Exception:
            log.warning("读已有题目失败，这次不做去重", exc_info=True)
            existing = set()

        fresh = [q for q in self._questions if " ".join(q.question.split()) not in existing]
        skipped = len(self._questions) - len(fresh)

        if not fresh:
            QMessageBox.information(
                self, "都是重复的", f"这 {len(self._questions)} 道题题库里都已经有了，没有新增。"
            )
            return

        try:
            self._db.save_questions(fresh, note_id=None, source="题库导入")
        except Exception as exc:
            log.exception("题库入库失败")
            QMessageBox.warning(
                self, "保存失败", f"入库时出了点问题：{friendly_error(exc)}"
            )
            return

        message = f"已保存 {len(fresh)} 道题。"
        if skipped:
            message += f"（有 {skipped} 道跟题库里已有的重复，跳过了）"
        log.info("题库导入完成：新增 %d 道，跳过重复 %d 道", len(fresh), skipped)

        self.saved.emit(len(fresh))
        QMessageBox.information(self, "导入完成", message + "\n\n回到主窗口点「开始刷题」就能刷了。")
        self.accept()

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.wait(3000)
        super().closeEvent(event)


def render_questions_markdown(questions: list[Question]) -> str:
    """把题目渲染成预览用的 Markdown。"""
    if not questions:
        return "_没有解析出题目_"

    parts: list[str] = [f"# 解析出 {len(questions)} 道题", ""]
    for index, question in enumerate(questions, start=1):
        parts.append(f"## {index}. {question.type_label}（{question.difficulty}）")
        if question.point:
            parts.append(f"**知识点**：{question.point}")
        parts.append("")
        parts.append(question.question)
        parts.append("")
        for option in question.options:
            parts.append(f"- {option}")
        if question.options:
            parts.append("")
        parts.append(f"**答案**：{question.answer}")
        if question.explanation:
            parts.append("")
            parts.append(f"**解析**：{question.explanation}")
        parts.append("")
        parts.append("---")
        parts.append("")
    return "\n".join(parts)
