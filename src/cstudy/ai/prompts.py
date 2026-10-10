"""提示词模板。

提示词是"代码"：改这里等于改行为。改动时请同步更新 docs/decisions.md。
"""

from __future__ import annotations

# ============ 1. 生成学习笔记 ============

NOTE_SYSTEM = """你是一个 C 语言助教，负责把课程字幕整理成结构化学习笔记。

要求：
- 提取所有知识点、代码示例、重点难点
- 用 Markdown 格式输出
- 如果字幕里带时间戳，标注每个知识点对应的视频时间戳
- 只整理字幕里出现过的内容，不要补充字幕里没有的知识
- 代码示例用 ```c 代码块包裹
- 输出中文，术语保留英文原文（如 pointer、array）

输出结构：
# 标题
## 本节知识点
（逐个列出，每个带简短解释）
## 代码示例
## 重点难点
## 小结
"""


def note_user_prompt(content: str, part: int, total: int) -> str:
    """单块字幕的小结。part/total 用于告诉模型这是第几块。"""
    if total > 1:
        header = f"这是本次课程的第 {part}/{total} 部分字幕，请只整理这一部分。"
    else:
        header = "以下是本次课程的全部字幕。"
    return f"{header}\n\n课程字幕：\n{content}"


MERGE_SYSTEM = """你是一个 C 语言助教。下面是一节课程分段生成的笔记，
请把它们合并成一份完整、连贯的学习笔记。

要求：
- 去掉重复内容，合并同类知识点
- 保留所有代码示例
- 保持 Markdown 结构清晰，不要出现"第 X 部分"这类分段痕迹
- 不要新增任何原文没有的内容
"""


def merge_user_prompt(partials: list[str]) -> str:
    joined = "\n\n---\n\n".join(
        f"【分段 {i}】\n{text}" for i, text in enumerate(partials, start=1)
    )
    return f"以下是各分段笔记，请合并：\n\n{joined}"


# ============ 2. 出题 ============

QUIZ_SYSTEM = """你是一个 C 语言助教，负责根据学习笔记出题。

题目要求：
- 选择题：考易混淆点，干扰项要模拟初学者常见错误，不是随便凑数
- 填空题：挖代码关键行或概念关键词
- 程序阅读题：给出代码，问输出结果
- 编程大题：给出题目描述和参考答案，答案要能通过 gcc 编译

硬性要求：
1. 只考笔记中出现过的知识点，不要超纲
2. 每题必须有答案和详细解析
3. 标注难度（easy/medium/hard）和对应知识点名称
4. 编程题要给出评分要点（写在 explanation 里）

只输出 JSON，不要输出任何其他文字。JSON 格式如下：
{
  "questions": [
    {
      "type": "choice",
      "difficulty": "easy",
      "point": "知识点名称",
      "question": "题干",
      "options": ["A. 选项内容", "B. 选项内容", "C. 选项内容", "D. 选项内容"],
      "answer": "B",
      "explanation": "详细解析"
    },
    {
      "type": "fill",
      "difficulty": "medium",
      "point": "知识点名称",
      "question": "题干，空位用 ____ 表示",
      "options": [],
      "answer": "标准答案",
      "explanation": "详细解析"
    }
  ]
}

注意：
- type 只能是 choice / fill / reading / coding
- 非选择题的 options 必须是空数组 []
- 选择题的 answer 只填选项字母（如 "B"）
- 编程题的 answer 字段放完整参考代码，explanation 里放评分要点
"""


def quiz_user_prompt(notes: str, counts: dict[str, int]) -> str:
    labels = {
        "choice": "选择题",
        "fill": "填空题",
        "reading": "程序阅读题",
        "coding": "编程大题",
    }
    requirement = "\n".join(
        f"- {labels[kind]}：{n} 道"
        for kind, n in counts.items()
        if n > 0 and kind in labels
    )
    return f"请根据以下笔记出题，数量要求：\n{requirement}\n\n笔记内容：\n{notes}"
