# Cstudy — C语言备考助手

项目说明见 [README.md](README.md)，设计决策见 [docs/decisions.md](docs/decisions.md)。

---

## 会话开始时的固定动作

本项目已安装插件 **`claude-code-setup`**（来源：`anthropic-plugin-directory`，Anthropic 官方目录）。

**每个新会话开始处理本项目时，第一件事是调用该插件的 `claude-automation-recommender` skill**，
让它分析当前代码库，给出适合本项目的 Claude Code 自动化建议（hooks / skills / subagents / MCP servers）。

- 调用方式：Skill 工具，skill 名 `claude-code-setup:claude-automation-recommender`
- 如果该 skill 不在可用列表里，说明插件没生效——先运行 `claude plugin list` 确认状态，
  必要时 `claude plugin enable claude-code-setup`，然后提示用户重启会话。

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
