"""PreToolUse 守卫：挡住会泄漏密钥 / 把运行时数据带进仓库的操作。

由 .claude/settings.json 调用，从 stdin 读取 Claude Code 传来的工具调用 JSON。
命中就输出 deny，Claude 会被拦下并看到原因；没命中就静默放行。
"""

import json
import os
import re
import sys

# 中文 Windows 上管道默认走 GBK，会让提示语变成乱码，这里强制 UTF-8
sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8")

# 仓库里绝对不能由 Claude 写入的文件（.env.example 除外，那是给人看的模板）
SENSITIVE_FILES = (".env",)

# 只截取 `git add` 自己的参数（到行尾或 shell 分隔符为止），
# 不能拿整条命令去搜——否则提交信息、heredoc 里出现 ".env" 这种字样会误伤。
GIT_ADD = re.compile(r"git\s+add\b([^\n;&|]*)")

# 参数里出现这些路径段就拦下（前面必须是行首/空格/引号，避免误伤
# 像 src/cstudy/data_loader.py 这种恰好含 "data" 的普通文件名）
GIT_ADD_GUARD = re.compile(r"(^|[\s'\"])(\.env(\b|$)|(\./)?(data|logs)/)")


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )
    sys.exit(0)


def is_secret_file(path: str) -> bool:
    name = os.path.basename(path.replace("\\", "/"))
    if name in SENSITIVE_FILES:
        return True
    # .env.local / .env.production 也算，但 .env.example 不算
    return name.startswith(".env.") and not name.endswith(".example")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # 读不懂就放行，不能因为守卫自己出错卡住正常工作

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool in ("Edit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path") or ""
        if is_secret_file(path):
            deny(
                f"不能改 {path}：这个文件里放的是 API 密钥，改坏了软件就连不上 AI 了。"
                "如果确实要改配置项，请改 .env.example，或者直接告诉用户手动改。"
            )

    elif tool == "Bash":
        command = tool_input.get("command") or ""
        if any(GIT_ADD_GUARD.search(args) for args in GIT_ADD.findall(command)):
            deny(
                "这条 git add 会把 .env 或 data/、logs/ 里的内容加进仓库："
                "里面有 API 密钥和你的登录态、答题记录，一旦提交就很难彻底删掉。"
                "请只 add 具体要提交的文件，例如 git add src/cstudy/pipeline.py。"
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
