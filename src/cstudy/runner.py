"""编译并运行用户写的 C 代码。

答题页的编程题用得上：光"对照参考答案"练不出程序设计能力——C 语言恰恰是
**看着对、编译就报错**的科目（漏分号、少 `&`、数组越界），得让编译器来告诉你。

用 gcc（MinGW）。编译和运行都带超时——`while(1)` 这种死循环必须能停下来。
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .logging_setup import get_logger
from .tools import find_gcc

log = get_logger(__name__)

# 编译最多等这么久。正常也就一两秒，卡住多半是代码里有奇怪的东西。
COMPILE_TIMEOUT = 60.0

# 程序跑最多等这么久。练题的程序不该跑很久，超时基本就是死循环。
RUN_TIMEOUT = 10.0

# gcc 的编译参数：
#   -std=gnu11  —— 教材普遍按 C99/C11 写，gnu11 最宽容
#   -Wall       —— 警告要看着，漏 `&`、少分号这类问题全在警告里
#   -lm         —— 数学函数（sqrt 等）在 Linux 上要显式链接，Windows 上无害
COMPILE_FLAGS = ("-std=gnu11", "-Wall", "-lm")


@dataclass
class RunResult:
    """一次"编译并运行"的结果。"""

    compiled: bool = False          # 编译过了没有
    ran: bool = False               # 真的跑起来了没有
    output: str = ""                # 给用户看的内容（说人话）
    timed_out: bool = False         # 是不是跑太久被掐了

    @property
    def ok(self) -> bool:
        return self.compiled and self.ran


# 微软那套 `_s` 安全函数。**注意**：MinGW 的 UCRT 版本其实自带它们
# （实测 `scanf_s` 能编过），所以这里只在**真的编不过**时才拿来做提示。
# 换个不支持它的编译器，或者哪天换了工具链，这段就用上了。
MSVC_ONLY = {
    "scanf_s": "scanf",
    "printf_s": "printf",
    "gets_s": "fgets",
    "strcpy_s": "strcpy",
    "strcat_s": "strcat",
    "sprintf_s": "sprintf",
}


def _explain_msvc_only(source: str, errors: str) -> str:
    """代码里有微软专有函数、又编不过时，给一句人话解释。

    这种情况最容易卡住人：同一份代码在 Visual Studio 里好好的、换编译器就一片红，
    而报错完全看不懂。提示里直接把对应关系摆出来。
    """
    hit = [name for name in MSVC_ONLY if name in source]
    if not hit:
        return ""
    lines = [f"代码里用了 {name}——它是微软专有的写法，这个编译器不认" for name in hit]
    lines.append("")
    lines.append("考试和大多数编译器用的是标准写法，把它换掉：")
    for name in hit:
        lines.append(f"  {name}  →  {MSVC_ONLY[name]}")
    if "scanf_s" in hit:
        lines.append("")
        lines.append('注意 scanf 的第二个参数要加 &：scanf("%d", &a);')
    return "\n".join(lines)


def run_c_code(
    source: str,
    stdin_text: str = "",
    timeout: float = RUN_TIMEOUT,
) -> RunResult:
    """编译并运行一段 C 代码。

    没装 gcc、编译不过、跑超时——每种情况都返回一段**说人话**的说明，
    绝不把裸的编译输出丢给用户（他基本不会 Python，更看不懂 gcc 的黑话）。
    """
    source = (source or "").strip()
    if not source:
        return RunResult(output="代码是空的，先写点东西再跑。")

    gcc = find_gcc()
    if not gcc:
        return RunResult(
            output=(
                "找不到 gcc 编译器，没法编译。\n"
                "在终端运行这条命令装上：\n"
                "    winget install BrechtSanders.WinLibs.POSIX.UCRT\n"
                "装完**重启这个软件**（PATH 要重新读一次）。"
            )
        )

    with tempfile.TemporaryDirectory(prefix="cstudy_run_") as tmp:
        workdir = Path(tmp)
        source_file = workdir / "main.c"
        exe_file = workdir / "main.exe"
        source_file.write_text(source, encoding="utf-8")

        compile_result = subprocess.run(
            [gcc, *COMPILE_FLAGS, "-o", str(exe_file), str(source_file)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMPILE_TIMEOUT,
            cwd=workdir,
        )

        if compile_result.returncode != 0 or not exe_file.exists():
            errors = (compile_result.stderr or compile_result.stdout or "").strip()
            hint = _explain_msvc_only(source, errors)
            body = _clean_compiler_output(errors, workdir)
            text = f"❌ 编译没通过：\n\n{body}"
            if hint:
                text += f"\n\n{hint}"
            return RunResult(compiled=False, output=text)

        warnings = (compile_result.stderr or "").strip()
        try:
            run_result = subprocess.run(
                [str(exe_file)],
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=workdir,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                compiled=True,
                ran=False,
                timed_out=True,
                output=(
                    f"⚠️ 编译通过了，但程序跑了 {timeout:.0f} 秒还没结束，已经被强制停下。\n\n"
                    "多半是死循环——检查一下循环条件（比如 `while(i <= 10)` 里 i 有没有变）。"
                ),
            )

        parts = ["✅ 编译通过。", ""]
        out = (run_result.stdout or "").rstrip()
        err = (run_result.stderr or "").rstrip()
        parts.append("程序输出：")
        parts.append(out if out else "（没有输出）")
        if err:
            parts.append("")
            parts.append(f"运行时的报错（stderr）：\n{err}")
        if warnings:
            parts.append("")
            parts.append(
                f"编译警告（能跑，但这些是隐患）：\n{_clean_compiler_output(warnings, workdir)}"
            )

        return RunResult(compiled=True, ran=True, output="\n".join(parts))


def _clean_compiler_output(text: str, workdir: Path | None = None) -> str:
    """把 gcc 的输出收拾成人能看的。

    做三件事：
    1. **去掉临时目录的完整路径**——`C:\\Users\\...\\cstudy_run_xxx\\main.c:5:3`
       又长又没意义，看着还以为是自己的路径出了问题
    2. `main.c:5:3:` → `第 5 行：`，一眼知道去哪找
    3. **补一句中文**——gcc 的报错是英文的，初学者看不懂（见 `_explain_errors`）
    """
    if workdir is not None:
        for prefix in (str(workdir) + "\\", str(workdir) + "/", str(workdir)):
            text = text.replace(prefix, "")

    lines: list[str] = []
    for line in text.splitlines():
        cleaned = line
        marker = "main.c:"
        if marker in cleaned:
            head, _, rest = cleaned.partition(marker)
            number, _, tail = rest.partition(":")
            if number.strip().isdigit():
                rest_tail = tail.split(":", 1)[-1].lstrip(": ") if ":" in tail else tail
                cleaned = f"第 {number.strip()} 行：{rest_tail.strip()}"
                if head.strip():
                    cleaned = f"{head.strip()} {cleaned}"
        lines.append(cleaned)
    body = "\n".join(lines).strip()

    hint = _explain_errors(text)
    return f"{body}\n\n{hint}" if hint else body


# gcc 的报错是英文的，初学者看不懂。这里把最常见的几条翻成人话。
# 每条给「怎么改」，不是光翻译——翻译一遍他还是不知道怎么办。
#
# **用正则不用字面串**：gcc 的说法会变（漏分号它报的是
# `expected ',' or ';' before 'printf'`，不是 `expected ';'`），
# 钉死字符串就会像第一次那样匹配不上。
ERROR_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"expected .*';'"),
        "**多半是上一行末尾漏了分号**。C 语言每条语句都要以分号结尾，"
        "gcc 是读到下一行才发现不对的，所以它报的行号常常比真正出错的行**晚一行**。",
    ),
    (
        re.compile(r"expected declaration or statement at end of input"),
        "**多半是少了右花括号 `}`**（`{` 和 `}` 数量对不上）。"
        "把代码往下翻，看看哪个函数或者循环没收尾。",
    ),
    (
        re.compile(r"undeclared"),
        "**用了一个没声明过的名字**。检查是不是拼错了，或者忘了 `int x;` 这样的声明。",
    ),
    (
        re.compile(r"implicit declaration of function"),
        "**用了函数但没引入它的头文件**。比如用 `printf` / `scanf` 要在最上面写 "
        "`#include <stdio.h>`，用 `sqrt` 要写 `#include <math.h>`。",
    ),
    (
        re.compile(r"expected '\)'|expected '\}'|expected '\]'"),
        "**括号对不上**。`{` `}`、`(` `)`、`[` `]` 都必须成对出现。",
    ),
    (
        re.compile(r"format '%"),
        "**printf/scanf 的格式符和参数类型对不上**。`%d` 对应 int、`%f` 对应 "
        "float/double、`%c` 对应 char、`%s` 对应字符串。",
    ),
    (
        re.compile(r"control reaches end of non-void function"),
        "**函数声明了要返回一个值，但没写 `return`**。",
    ),
    (
        re.compile(r"unused variable"),
        "这个变量定义了但没用上。不是错误，但说明你可能忘了用它。",
    ),
)


def _explain_errors(errors: str) -> str:
    """认出常见的编译错误，补一句**中文的、说怎么办**的提示。

    **优先只看 error 行**：一条错误常会连带出一串警告（漏个分号会顺带报
    "变量没用上"），全解释一遍反而把真正的原因淹了。
    只有警告、没有错误时才去解释警告。
    """
    error_lines = "\n".join(line for line in errors.splitlines() if "error:" in line)
    scope = error_lines or errors

    hits: list[str] = []
    for pattern, hint in ERROR_HINTS:
        if pattern.search(scope) and hint not in hits:
            hits.append(hint)
    if not hits:
        return ""
    return "可能的原因：\n" + "\n".join(f"  · {hint}" for hint in hits)
