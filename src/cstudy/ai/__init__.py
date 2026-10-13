"""AI 相关：客户端、提示词、笔记生成、出题、复查、题库导入、找知识点关系。"""

from .client import AIError, DeepSeekClient
from .gap import GapFiller
from .importer import ImportOutcome, QuestionImporter
from .notes import NoteGenerator, render_note_file
from .quiz import QuizGenerator, QuizPlan
from .relations import ProposedRelation, RelationFinder, RelationProposal
from .review import Reviewer
from .slides import SlideReader

__all__ = [
    "AIError",
    "DeepSeekClient",
    "GapFiller",
    "ImportOutcome",
    "NoteGenerator",
    "ProposedRelation",
    "QuestionImporter",
    "QuizGenerator",
    "QuizPlan",
    "RelationFinder",
    "RelationProposal",
    "Reviewer",
    "SlideReader",
    "render_note_file",
]
