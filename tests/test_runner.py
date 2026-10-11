"""编译并运行用户写的 C 代码。

真正调 gcc 的那几条会在没装编译器时自动跳过；其余的都是纯逻辑，
不需要编译器也能测。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy import runner  # noqa: E402
from cstudy.runner import RunResult, _clean_compiler_output, _explain_msvc_only, run_c_code  # noqa: E402

needs_gcc = pytest.mark.skipif(
    runner.find_gcc() is None, reason="这台机器上没装 gcc"
)


class TestCleanCompilerOutput:
    """gcc 会把临时目录的完整路径写进每一行，又长又没法看。"""

    def test_turns_the_path_into_a_line_number(self, tmp_path):
        workdir = tmp_path / "cstudy_run_abc"
        raw = f"{workdir / 'main.c'}:5:3: error: 'x' undeclared"

        cleaned = _clean_compiler_output(raw, workdir)

        assert "第 5 行" in cleaned
        assert "cstudy_run_abc" not in cleaned, "临时目录的完整路径不该给用户看"
        assert "error" in cleaned

    def test_keeps_lines_without_a_file_marker(self):
        assert "收藏" in _clean_compiler_output("收藏：有的编译器会多打几行")

    def test_handles_empty_input(self):
        assert _clean_compiler_output("") == ""


class TestMsvcOnlyHint:
    """用户的课用的是 Visual Studio，老师的代码里很可能有 MSVC 专有函数。

    同一份代码在 VS 里好好的、换 gcc 就一片红——不解释一句人会懵。
    """

    def test_detects_scanf_s(self):
        hint = _explain_msvc_only("scanf_s(\"%d\", &n);", "error: implicit declaration")

        assert "scanf_s" in hint
        assert "scanf" in hint

    def test_mentions_the_missing_ampersand(self):
        """scanf 少 `&` 是最常见的坑，顺手提醒一下。"""
        assert "&" in _explain_msvc_only("scanf_s(\"%d\", n);", "")

    def test_silent_when_there_is_nothing_to_say(self):
        assert _explain_msvc_only("int main() { return 0; }", "") == ""


class TestRunCodeWithoutCompiler:
    def test_empty_source_is_rejected(self):
        result = run_c_code("   ")

        assert not result.ok
        assert "空" in result.output

    def test_missing_compiler_says_how_to_install(self, monkeypatch):
        monkeypatch.setattr(runner, "find_gcc", lambda: None)

        result = run_c_code("int main() { return 0; }")

        assert not result.compiled
        assert "winget install" in result.output, "得告诉用户怎么装"
        assert "重启" in result.output, "装完要重启软件才读得到 PATH"


@needs_gcc
class TestRunCodeWithCompiler:
    def test_successful_program(self):
        result = run_c_code('#include <stdio.h>\nint main() { printf("hi\\n"); return 0; }')

        assert result.ok
        assert "hi" in result.output

    def test_compile_error_is_explained(self):
        """漏个分号——最典型的错，用户必须看得懂。"""
        result = run_c_code("int main() { int a = 1 return 0; }")

        assert not result.compiled
        assert "编译没通过" in result.output

    def test_reads_stdin(self):
        """很多练习题要 scanf 读输入，得能把输入喂进去。"""
        source = '#include <stdio.h>\nint main() { int n; scanf("%d", &n); printf("%d\\n", n * 2); return 0; }'

        result = run_c_code(source, stdin_text="21\n")

        assert result.ok
        assert "42" in result.output

    def test_infinite_loop_is_stopped(self):
        """`while(1)` 必须能被掐掉，不然软件就卡死了。"""
        result = run_c_code("int main() { while (1) { } return 0; }", timeout=1.0)

        assert result.compiled and not result.ran
        assert result.timed_out
        assert "死循环" in result.output

    def test_msvc_only_functions_compile_or_get_a_hint(self):
        """`scanf_s` 这类微软函数：**能编过最好，编不过就必须给提示**。

        实测 MinGW 的 UCRT 版本自带 `scanf_s`，所以它能编过反而是正常的。
        换个不支持它的编译器就会走提示那条路——两种结果都不算错，
        但**绝不能只丢一堆看不懂的报错**。
        """
        source = '#include <stdio.h>\nint main() { int n; scanf_s("%d", &n); return 0; }'

        result = run_c_code(source)

        assert result.compiled or "scanf_s" in result.output


class TestErrorHints:
    """gcc 的报错是英文的，初学者看不懂——这里把常见的翻成"怎么办"。"""

    def test_missing_semicolon(self):
        errors = "main.c:4:5: error: expected ',' or ';' before 'printf'"

        assert "分号" in runner._explain_errors(errors)

    def test_missing_brace(self):
        errors = "main.c:7:1: error: expected declaration or statement at end of input"

        assert "花括号" in runner._explain_errors(errors)

    def test_missing_include(self):
        errors = "main.c:2:5: error: implicit declaration of function 'printf'"

        assert "头文件" in runner._explain_errors(errors)

    def test_errors_win_over_the_warnings_they_drag_along(self):
        """漏个分号会连带报"变量没用上"——只解释警告会把真正的原因淹了。"""
        errors = (
            "main.c:4:5: error: expected ',' or ';' before 'printf'\n"
            "main.c:3:9: warning: unused variable 'a'"
        )

        hint = runner._explain_errors(errors)

        assert "分号" in hint
        assert "没用上" not in hint

    def test_only_warnings_still_get_explained(self):
        errors = "main.c:3:9: warning: unused variable 'a'"

        assert "没用上" in runner._explain_errors(errors)

    def test_unrecognised_errors_say_nothing(self):
        """认不出来就别瞎猜，把原文给用户看就行。"""
        assert runner._explain_errors("error: 谁也没见过的错") == ""
