# C语言备考助手

## 这是什么 / 给谁用

一个只在本机运行的 Windows 桌面软件，把 B 站 C 语言课程视频自动变成**学习笔记**和**练习题**，
用于专升本 C 语言备考。**使用者只有我一个人。**

## 解决什么问题

每天听几节 B 站 C 语言课，看完就忘、没有笔记、不知道自己哪里不会。
手动整理笔记和找题太费时间，所以让软件自动做：

```
粘贴 B 站视频链接
   ↓  抓字幕（没有 CC 字幕就本地语音转写）
   ↓  按 10 分钟切块，逐块让 AI 总结，再合并
生成 Markdown 学习笔记  →  存到 notes/ 文件夹
   ↓  基于笔记出题
生成 3 选择 + 2 填空 + 1 阅读 + 1 编程（含答案和解析）
   ↓
存入 SQLite，供后续刷题和复习
```

## 验收标准（怎么算做完了）

第一版跑通的标准，拿一个**真实的 C 语言课程 BV 号**验证：

| # | 检查项 | 通过标准 |
|---|---|---|
| 1 | 抓字幕 | 能拿到该视频的分 P 列表和字幕文本；无字幕时能走转写或明确提示 |
| 2 | 生成笔记 | `notes/` 下生成一份 Markdown，正文 ≥ 500 字，含 ≥ 3 个知识点 |
| 3 | 出题 | 共 7 题（3 选择 / 2 填空 / 1 阅读 / 1 编程），**每题答案和解析都非空** |
| 4 | 数据落库 | `data/cstudy.db` 里 notes / questions 表能看到对应记录 |
| 5 | 不崩 | 全流程无未捕获异常；窗口不卡死（耗时任务在后台线程） |
| 6 | 出错可查 | 任何失败都能在界面上看到一句人话解释，且 `logs/cstudy.log` 有详细记录 |

## 第一版不做什么

关键帧抽帧、AI 看图识别 PPT、错题本与遗忘曲线、考纲覆盖度统计、刷题界面、
真题组卷、编程题 gcc 编译验证 —— 全部留到第二、三版。

## 环境要求

- Windows 10/11
- Python 3.12（**不要用 3.13/3.14**，whisper 依赖的轮子可能还没出）
- FFmpeg（抽音频用）
- 显卡可选：有 NVIDIA 显卡会快很多，没有会自动退回 CPU

## 从零跑起来

```bash
# 1. 建虚拟环境
py -3.12 -m venv .venv

# 2. 激活（Git Bash）
source .venv/Scripts/activate

# 3. 装依赖
pip install -r requirements.txt

# 4. 配置密钥
cp .env.example .env    # 然后把 DeepSeek Key 填进去

# 5. 建数据库 + 灌考纲
python scripts/init_db.py
python scripts/seed_syllabus.py

# 6. 启动
python run.py
```

## 目录说明

```
Cstudy/
├── run.py                  启动入口
├── config.yaml             非敏感配置（模型、路径、分块大小…）
├── .env                    密钥（不进仓库）
├── requirements.txt        锁死版本的依赖清单
├── data/                   数据库、登录态、临时音频
├── notes/                  导出的 Markdown 笔记
├── logs/                   日志
├── docs/decisions.md       为什么这么设计（改代码前先看这个）
├── scripts/                建库、灌考纲、扫码登录等一次性脚本
├── src/cstudy/             主代码
│   ├── config.py           读 config.yaml + .env
│   ├── db.py               SQLite 封装
│   ├── ai/                 DeepSeek 调用、提示词、笔记生成、出题
│   ├── bilibili/           视频信息、字幕、扫码登录
│   ├── transcribe/         本地 whisper 转写
│   └── ui/                 PyQt6 界面
└── tests/                  核心逻辑的测试
```

## 出问题了怎么办

1. 界面上的红色提示会告诉你大致原因
2. 详细错误在 `logs/cstudy.log` 里，把最后 50 行发给我
3. 密钥相关的报错检查 `.env` 里的 `DEEPSEEK_API_KEY` 有没有填对
