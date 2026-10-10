# 决策记录

记录**为什么**这么选，不只是选了什么。改代码之前先看这里，避免重复讨论已经定过的事。

---

## 2026-10-10 项目立项

### 目录名从 `D:\Cstudy！` 改成 `D:\Cstudy`

**原因**：原目录名结尾是全角「！」。FFmpeg、gcc 这类命令行工具在处理非 ASCII / 全角路径时
会出各种难定位的编码错误。趁项目还没有文件时改掉，成本最低。

### Python 用 3.12，不用机器上已有的 3.14

**原因**：机器上只有 Python 3.14。`faster-whisper` 依赖的 `ctranslate2`、以及 PyTorch，
在 3.14 上轮子可能还没发布，装到一半失败会非常难排查。单独装一个 3.12 并建独立虚拟环境，
不动用户现有的 3.14 和 LibreOffice 自带的 Python。

### AI 服务用 DeepSeek，模型用 `deepseek-flash`

**原因**：用户原本以为自己的 Claude Code 接的是 DeepSeek，实际查 `~/.claude/settings.json`
发现接的是**阶跃星辰 StepFun**（`api.stepfun.com/step_plan`，模型 `step-5-preview`）。
两者是不同的服务，Key 不通用。用户选择自行申请 DeepSeek Key。

**模型名**：用户原提示词里写的 `deepseek-v4-flash` 和 `deepseek-v4-flash-vision-exp`
**已经退役**（官方文档说明这两个名字仍兼容但会转到 V4.1-Flash）。
实测 `GET https://api.deepseek.com/models` 当前只有两个模型：

- `deepseek-flash` —— 100 万上下文，`input_modalities: ["text","image"]`，**支持图片输入**
- `deepseek-v4-pro` —— 100 万上下文，纯文本

**关键决策**：用 `deepseek-flash` **一个模型同时处理文字和图片**。
原方案要配两个模型（文本一个、视觉一个），现在配置项和出错点都少一半。

### 语音转写用本地 faster-whisper

**原因**：用户机器有 RTX 4060 Ti 8GB，本地转写免费且离线。
代价是 PyTorch + CUDA 体积大、安装慢，且必须配 Python 3.12。

**保留 CPU 回退**：`transcribe.device: auto` 会优先尝试 CUDA，失败自动退回 CPU（int8）。
用户不会 Python，不能因为 CUDA 环境没配好就整个功能不可用。

### B 站字幕走扫码登录

**原因**：B 站字幕接口需要登录态。写一个扫码登录窗口，登录一次把 cookies 缓存到
`data/cookies/bilibili.json`（已 gitignore），之后不用重复登录。

### 笔记导出到项目内 `notes/` 文件夹

**原因**：用户暂时不需要 Obsidian 集成。同时写两份：数据库一份（供出题和复习用），
文件一份（用户能直接打开看）。导出目录做成配置项，以后想接 Obsidian 只改 `config.yaml`。

### 第一版范围：只做端到端主链路

**原因**：遵循"先做最小闭环"。第一版只做 导入链接 → 抓字幕 → 生成笔记 → 出题 → 入库 + 一个能点的窗口。
错题本、刷题界面、抽帧看图全部推迟。

---

## 待办 / 已知风险

- **图片识别未经实测**：`deepseek-flash` 支持图片输入是从 `/models` 接口的
  `input_modalities` 字段读出来的，没有实际传图验证过。第二版做关键帧识别时要先小样本验证。
- **faster-whisper 的 CUDA 依赖**：Windows 上跑 CUDA 需要 cuDNN 和 cuBLAS 的 DLL，
  这是常见坑点。安装脚本里会处理，装不上就退回 CPU。
- **B 站接口不稳定**：字幕接口依赖 `bilibili-api-python`，B 站改接口会导致失效。
  所有 B 站相关调用都包了重试和明确的失败提示。
