"""用假字幕验证 AI 链路，不需要 B 站视频。

会真实调用 DeepSeek（几厘钱），验证：
    分段总结 → 合并 → 出题 → JSON 解析

    python scripts/test_ai.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.ai import DeepSeekClient, NoteGenerator, QuizGenerator  # noqa: E402
from cstudy.config import get_config  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402
from cstudy.subtitle import Segment  # noqa: E402

# 一段模拟的 C 语言课程字幕，覆盖指针的几个易错点
FAKE_LINES = [
    "这节课我们讲指针。指针是什么？指针就是地址。",
    "我们先看 int a = 10; 然后 int *p = &a; 这里的 &a 是取地址。",
    "注意 p 的类型是 int *，它存的是 a 的地址，不是 a 的值。",
    "那么 *p 是什么呢？*p 叫解引用，就是取出 p 指向的那个变量的值。",
    "所以 *p 等于 10。这时候如果你写 *p = 20; 那么 a 也变成 20 了。",
    "初学者最容易搞混的是：p 和 *p 完全是两回事。",
    "p 是地址，*p 是值。这一点一定要分清楚。",
    "再看一个常见的坑：int *p; 这样定义之后 p 是野指针，它指向哪里不知道。",
    "野指针你直接 *p = 5; 可能会让程序崩溃。所以定义指针时最好先初始化为 NULL。",
    "还有指针和数组的关系。int arr[5]; 数组名 arr 在大多数情况下会退化成指向首元素的指针。",
    "所以 arr 等价于 &arr[0]，arr + 1 指向 arr[1]。",
    "注意 arr + 1 不是地址加 1 个字节，而是加一个 int 的大小，通常是 4 个字节。",
    "这个叫指针的算术运算，偏移量是按它指向的类型的大小来算的。",
    "最后讲一下指针作为函数参数。因为 C 语言是值传递。",
    "如果你想在函数里修改外面的变量，就必须传指针进去。",
    "比如 void swap(int *x, int *y) 这样，调用的时候写 swap(&a, &b)。",
    "这节课的重点就是：指针存地址，解引用取值，传指针能改实参。",
]


def make_segments() -> list[Segment]:
    return [Segment(start=i * 12.0, text=line) for i, line in enumerate(FAKE_LINES)]


def main() -> int:
    config = get_config()
    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))

    client = DeepSeekClient(config)
    print(f"\n模型：{config.ai_model}")
    print(f"字幕：{len(FAKE_LINES)} 条\n")

    def progress(text: str) -> None:
        print(f"  · {text}")

    # ---- 1. 生成笔记 ----
    print("[1/3] 生成笔记")
    note = NoteGenerator(client, config).generate(make_segments(), "指针入门", progress)
    print(f"      笔记 {len(note)} 字\n")

    # ---- 2. 出题 ----
    print("[2/3] 出题")
    questions = QuizGenerator(client, config).generate(note, progress)
    print(f"      共 {len(questions)} 道\n")

    # ---- 3. 检查 ----
    print("[3/3] 检查结果")
    by_type: dict[str, int] = {}
    for q in questions:
        by_type[q.type] = by_type.get(q.type, 0) + 1
    for kind, count in by_type.items():
        print(f"      {kind}: {count} 道")

    problems = [q for q in questions if not q.answer.strip()]
    if problems:
        print(f"\n有 {len(problems)} 道题没有答案！")
        return 1

    print("\n----- 笔记预览（前 400 字）-----")
    print(note[:400])
    print("\n----- 第一道题 -----")
    first = questions[0]
    print(f"[{first.type_label} / {first.difficulty}] {first.question}")
    for option in first.options:
        print(f"  {option}")
    print(f"答案：{first.answer}")
    print(f"解析：{first.explanation[:200]}")

    print("\nAI 链路正常。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
