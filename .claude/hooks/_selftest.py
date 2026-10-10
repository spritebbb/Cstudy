"""临时自测：用真实形状的 JSON 喂两个 hook 脚本，检查拦截/放行是否符合预期。"""

import json
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
BS = chr(92)


def run(script, tool, payload):
    proc = subprocess.run(
        [PY, str(ROOT / ".claude" / "hooks" / script)],
        input=json.dumps({"tool_name": tool, "tool_input": payload}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    return proc.stdout.strip()


CASES = [
    ("guard_sensitive.py", "Edit", {"file_path": f"D:{BS}Cstudy{BS}.env"}, "拦"),
    ("guard_sensitive.py", "Write", {"file_path": "D:/Cstudy/.env.local"}, "拦"),
    ("guard_sensitive.py", "Write", {"file_path": "D:/Cstudy/.env.example"}, "放"),
    ("guard_sensitive.py", "Edit", {"file_path": f"D:{BS}Cstudy{BS}src{BS}cstudy{BS}pipeline.py"}, "放"),
    ("guard_sensitive.py", "Bash", {"command": "git add .env"}, "拦"),
    ("guard_sensitive.py", "Bash", {"command": "git add -f data/cstudy.db"}, "拦"),
    ("guard_sensitive.py", "Bash", {"command": "git add logs/"}, "拦"),
    ("guard_sensitive.py", "Bash", {"command": "git add src/cstudy/pipeline.py"}, "放"),
    ("guard_sensitive.py", "Bash", {"command": "git add src/cstudy/data_loader.py"}, "放"),
    ("guard_sensitive.py", "Bash", {"command": "git status"}, "放"),
    # 回归：提交信息里出现 ".env" 不该被误判（曾经误拦过一次）
    ("guard_sensitive.py", "Bash", {"command": "git add .gitignore"}, "放"),
    (
        "guard_sensitive.py",
        "Bash",
        {"command": "git commit -F - <<'EOF'\n第一版：密钥放 .env，运行时数据不进仓库\nEOF\n"},
        "放",
    ),
    ("guard_sensitive.py", "Bash", {"command": "echo '.env' && git add src/cstudy/x.py"}, "放"),
    ("guard_sensitive.py", "Bash", {"command": "git add -f data/cstudy.db && git commit -m x"}, "拦"),
    ("guard_sensitive.py", "Bash", {"command": "git add logs/cstudy.log"}, "拦"),
    ("run_tests.py", "Edit", {"file_path": "D:/Cstudy/README.md"}, "放"),
    ("run_tests.py", "Bash", {"command": "git status"}, "放"),
]

failed = 0
for script, tool, payload, expect in CASES:
    out = run(script, tool, payload)
    blocked = "deny" in out
    ran_tests = "自动测试" in out

    if expect == "拦":
        ok = blocked
    elif expect == "跑":
        ok = ran_tests
    else:
        ok = not blocked and not ran_tests

    failed += not ok
    mark = "OK  " if ok else "FAIL"
    detail = payload.get("file_path") or payload.get("command")
    detail = " ".join(str(detail).split())[:70]
    print(f"{mark} [{expect}] {script} <- {tool} {detail}")
    if out:
        for line in out.splitlines():
            print(f"        | {line}")

print()
print(f"失败 {failed} 项 / 共 {len(CASES)} 项")

# 单独验证自动测试脚本：塞一个必定失败的探针测试，看它能不能跑起来并报出来
probe = ROOT / "tests" / "test_zz_probe_tmp.py"
probe.write_text(
    "def test_probe_should_fail():\n    assert False, '探针：这是自测用的假失败'\n",
    encoding="utf-8",
)
try:
    out = run("run_tests.py", "Edit", {"file_path": f"D:{BS}Cstudy{BS}src{BS}cstudy{BS}pipeline.py"})
    if "自动测试" in out and "probe_should_fail" in out:
        print("OK   [报错] 改 src 下的文件 -> 跑起 pytest 并报出了失败")
    else:
        failed += 1
        print("FAIL [报错] 没跑测试或没报出失败，实际输出：")
        for line in out.splitlines():
            print(f"        | {line}")
finally:
    probe.unlink(missing_ok=True)

print()
print("全部通过" if not failed else f"失败 {failed} 项")
sys.exit(1 if failed else 0)
