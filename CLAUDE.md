# Cstudy — C语言备考助手

项目说明见 [README.md](README.md)，设计决策见 [docs/decisions.md](docs/decisions.md)。

---

## 会话开始时的固定动作

**不需要每次调用 recommender skill 了。** 本项目的自动化已经装完并实测过，
清单见下面「已装好的 Claude Code 自动化」一节，不要重复安装。

只有这两种情况才重新跑一次：

- 换了机器，或怀疑 `.claude/` 里的配置缺失
- Claude Code 出了新能力，想看看还有什么值得装

跑法：插件 `claude-code-setup`（来源 `anthropic-plugin-directory`）的
`claude-automation-recommender` skill。如果该 skill 不在可用列表里，说明插件没生效——
先 `claude plugin list` 确认状态，必要时 `claude plugin enable claude-code-setup`，
然后提示用户重启会话。

---

## 环境速查

| 用途 | 命令 |
|---|---|
| 虚拟环境 | `.venv\Scripts\python.exe`（**3.12，项目专用**，不要用系统里的 3.14） |
| 启动软件 | 双击 `启动软件.bat` |
| 环境自检 | 双击 `环境自检.bat` |
| 扫码登录 B 站 | 双击 `扫码登录.bat` |
| 跑测试 | `.venv\Scripts\python.exe -m pytest` |
| 单独验证 AI 链路 | `.venv\Scripts\python.exe scripts\test_ai.py` |

---

## 这个项目的情况

**使用者只有一个人**，用来做专升本 C 语言备考：把 B 站课程视频转成学习笔记和练习题。

**用户基本不会 Python**。所以写代码时：

- 报错信息必须说人话，并且给出下一步怎么做（不要抛裸 traceback 给用户看）
- 关键步骤都要写日志到 `logs/cstudy.log`，用户会把日志发过来求助
- 优先选"防呆"的设计，宁可多一层兜底（比如 GPU 用不了就自动退回 CPU）
- 注释和文档写中文

**改设计之前先读 `docs/decisions.md`**，那里记录了每个选择的理由，避免重复讨论已经定过的事。

**密钥**在 `.env` 里（已 gitignore），配置项在 `config.yaml` 里。不要把密钥写进代码或文档。

---

## 当前进度

第一版已完成并验证：导入链接 → 抓字幕 → 生成笔记 → 出题 → 入库 + PyQt 界面。

**还没做的**（第二、三版）：关键帧抽帧、AI 看图识别 PPT、错题本与遗忘曲线、
考纲覆盖度统计、刷题界面、真题组卷、编程题 gcc 编译验证。

**唯一未验证的环节**：B 站字幕抓取需要用户本人扫码登录，尚未实测过。

---

## 已完成：git 首次提交（2026-10-10）

- 身份用 `git config --local` **只写进本仓库**，全局 git config 未被改动：
  - `user.name  = spritebbb`
  - `user.email = 91140834+spritebbb@users.noreply.github.com`
    （GitHub 隐私邮箱格式；公开主页上既没有显示名也没有公开邮箱，所以用 `<用户ID>+<用户名>` 推导）
- 提交 `de8d1a1` 第一版代码；提交 `9d0e83e` Claude Code 自动化配置。
- `.claude/settings.local.json` 已加入 `.gitignore`（本机个人设置，不进仓库）。
- 首次提交前已复查：`.env`、`data/`、`logs/` 均未纳入。

---

## 已装好的 Claude Code 自动化

下面的东西**已经落地并实测过**，新会话不要重复安装：

| 类型 | 位置 | 作用 |
|---|---|---|
| hook | `.claude/hooks/guard_sensitive.py` | 写 `.env`、或 `git add` 到 `.env`/`data/`/`logs/` 时拦下 |
| hook | `.claude/hooks/run_tests.py` | 改 `src/cstudy/` 后自动跑 pytest，只有失败才输出 |
| hook | `.claude/hooks/_selftest.py` | 守卫脚本的自测，改过 hook 后跑一遍 |
| skill | `/排错` | 读日志说人话，定位卡在链路的哪一环 |
| skill | `/自检` | 跑环境体检并逐项解释 |
| subagent | `code-reviewer` | 按本项目约束复查改动 |
| MCP | `context7` | 查 PyQt6 / openai / faster-whisper 等库的最新文档 |

**写 hook 时的两个坑**（都踩过）：
1. 本机**没有装 `jq`**——用项目自带 `.venv\Scripts\python.exe` 解析 stdin，别用 `jq`。
2. 中文 Windows 上 Python 的 stdout 默认是 GBK，会输出乱码。脚本里必须
   `sys.stdout.reconfigure(encoding="utf-8")`。

