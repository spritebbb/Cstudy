"""PostToolUse：改完 src/cstudy 下的代码自动跑一遍测试。

测试全过就什么都不输出（免得刷屏）；有失败才把关键报错打出来，
这样 Claude 能立刻看到自己刚改坏了什么，用户不用等双击启动软件才发现。
"""

import json
import subprocess
import sys
from pathlib import Path

# 中文 Windows 上管道默认走 GBK，会让提示语变成乱码，这里强制 UTF-8
sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WATCHED = "src/cstudy/"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    if tool not in ("Edit", "Write"):
        sys.exit(0)

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    normalized = path.replace("\\", "/")
    if WATCHED not in normalized:
        sys.exit(0)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
        )
    except subprocess.TimeoutExpired:
        print("[自动测试] 跑了 4 分钟还没结束，已中断。可能是某条测试卡住了，需要人工看一眼。")
        sys.exit(0)
    except Exception as exc:
        print(f"[自动测试] 没能启动 pytest：{exc}")
        sys.exit(0)

    if result.returncode != 0:
        output = (result.stdout or "") + (result.stderr or "")
        tail = "\n".join(output.strip().splitlines()[-40:])
        print(
            f"[自动测试] 刚改的 {normalized} 让测试挂了。下面是要修的地方：\n"
            f"{tail}\n"
            "[自动测试] 请先修好这些失败，再继续下一步。"
        )

    sys.exit(0)


if __name__ == "__main__":
    main()
