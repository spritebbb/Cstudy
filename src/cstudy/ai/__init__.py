"""AI 相关：客户端、提示词、笔记生成、出题。"""

from .client import AIError, DeepSeekClient
from .notes import NoteGenerator, render_note_file
from .quiz import QuizGenerator

__all__ = ["AIError", "DeepSeekClient", "NoteGenerator", "QuizGenerator", "render_note_file"]
