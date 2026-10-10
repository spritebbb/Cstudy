"""AI 相关：客户端、提示词、笔记生成、出题、复查、题库导入。"""

from .client import AIError, DeepSeekClient
from .gap import GapFiller
from .importer import ImportOutcome, QuestionImporter
from .notes import NoteGenerator, render_note_file
from .quiz import QuizGenerator, QuizPlan
from .review import Reviewer
from .slides import SlideReader

__all__ = [
    "AIError",
    "DeepSeekClient",
    "GapFiller",
    "ImportOutcome",
    "NoteGenerator",
    "QuestionImporter",
    "QuizGenerator",
    "QuizPlan",
    "Reviewer",
    "SlideReader",
    "render_note_file",
]
