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
| 打包成免安装版 | 双击 `打包软件.bat`（成品在 `dist\C语言备考助手\`，目标机不用装 Python） |
| 单独验证 AI 链路 | `.venv\Scripts\python.exe scripts\test_ai.py` |
| 单独验证 B 站链路 | `.venv\Scripts\python.exe scripts\test_bilibili.py "BV号"` |
| 批量导入题库文件 | `.venv\Scripts\python.exe scripts\import_bank.py "文件夹" --dry-run` |
| 扫描件 OCR 导入 | `.venv\Scripts\python.exe scripts\import_bank.py "文件.pdf" --ocr` |
| 知识点对齐考纲 | `.venv\Scripts\python.exe scripts\map_syllabus.py --dry-run` |
| 给题少的知识点补题 | `.venv\Scripts\python.exe scripts\fill_gaps.py --dry-run` |
| 装 C 编译器（gcc） | `winget install BrechtSanders.WinLibs.POSIX.UCRT` |

---

## 这个项目的情况

**使用者只有一个人**，用来做 C 语言课程学习：把 B 站课程视频转成学习笔记和练习题。

**用户基本不会 Python**。所以写代码时：

- 报错信息必须说人话，并且给出下一步怎么做（不要抛裸 traceback 给用户看）
- 关键步骤都要写日志到 `logs/cstudy.log`，用户会把日志发过来求助
- 优先选"防呆"的设计，宁可多一层兜底（比如 GPU 用不了就自动退回 CPU）
- 注释和文档写中文

**改设计之前先读 `docs/decisions.md`**，那里记录了每个选择的理由，避免重复讨论已经定过的事。

**密钥**在 `.env` 里（已 gitignore），配置项在 `config.yaml` 里。不要把密钥写进代码或文档。

---

## 当前进度

第一版已完成并**端到端验证**：导入链接 → 抓字幕 → 生成笔记 → 出题 → 入库 + PyQt 界面。

**B 站字幕抓取已于 2026-10-10 实测通过**（BV1cp421X755，404 条 CC 字幕）。
它此前一直是"唯一未验证的环节"，第一次真跑就暴露出四个连着的 bug（见下）。

**还没做的**（第二、三版）：**规划里的都做完了。**
（关键帧抽帧、AI 看图识别 PPT、考纲覆盖度统计、真题组卷、错题本与遗忘曲线、
编程题 gcc 编译验证，全部已完成。）

**界面外观全在 `config.yaml` 的 `ui:` 段**（字体、主题、配色、背景图）：

- 字体：界面微软雅黑 11pt / 代码 Consolas 12pt。改完看 HTML 里写进去的
  `font-family` 不算数，要用 `QTextDocument.setHtml()` 读 `charFormat().fontFamilies()`
  确认 Qt 真的认（踩过 `style='...'Consolas'...'` 单引号套单引号、属性被截断）。
- 主题：深色（默认）/ 亮色，配色可逐个覆盖（`ui.theme.accent` 等），
  支持背景图（`ui.theme.background_image` + `background_dim` 控制压暗）。
  样式表在 `ui/theme.py` 一处生成，不在各控件里设颜色。
- **面板里的 HTML 用色不能写死**：`<span style="color:...">` 不受 QSS 管，
  写死的深红在深色底上几乎看不见。面板都接一个 `theme` 参数，
  用 `theme.danger` / `theme.accent`。

**界面上的八个 tab**：进度 / 笔记 / 答题 / 考纲 / 知识图谱 / 错题本 / 学情 / **考前突击**（临时）。
答题页的「从题库挑题」（按题型/章节/来源筛，或随机抽 N 道）、
「组一套真题」（按考试分值比例从题库抽，不调 AI）、
**程序设计题的「跑一下」**（编译并运行用户写的代码，见下）；
「笔记」页能搜（标题和正文都搜）；「考纲」页看哪些知识点还没题；
「知识图谱」页把考纲画成一张网（见下）；
「错题本」页按遗忘曲线安排复习；「学情」页看每周做题量和分题型正确率。

**「考前突击」是 2026-10-15 临时加的一栏**（另一门考试：《网络安全管理员》），
跟上面七个不是一回事，改动前先看 `docs/decisions.md` 里那一天：

- 它的题存在**另一个库文件** `data/temp_bank/bank.db`（`paths.temp_bank`），
  跟 `cstudy.db` 结构上分开：两边看不见彼此的题，这栏做错的题也不进错题本和学情；
- 它**不吃** `import.skip_types`、**不走** `exam_type_for()`，题型只有
  单选/多选/判断/问答四类（`src/cstudy/temp_bank.py`）；
- 导入是**规则解析**（认行首 `[单选题]` 这类标记），不调 AI；换排版就得改代码；
- **一轮默认 20 道**（`temp_bank.round_size`），刷完**再点一次「开始刷题」**往下接——
  用户第一次试用反馈"一次性刷的题太多了，我记不过来"。位置存在临时库的
  `temp_meta` 里（`pos:<题型>` → 最后一道答过的题的 id），按题型分开记；
  想重刷刚做完的那一批用「重刷这批」（**它不会清进度、也不会跳回整份第 1 道**；
   一批都没刷过时它是灰的）。**别把「开始刷题」改回"从头开始"**：用户第二次反馈"我点开始刷题后他又从第一轮
  给我开始刷了"——主按钮就是"继续/开始"，"重来"必须是单独一个按钮，而且要重来的是
  **刚做完的那一批**（用户第三次反馈："我要重头再刷的意思是我要重新刷我刚刚刷完的
  二十道题而不是从第一道题开始刷"）。批次记在 `temp_meta` 的 `batch:ids` 里，关掉软件
  重开也认得；
  随机模式下点「开始刷题」翻一批新题，但不动已经记着的位置；
- 这一栏**自己有一个错题集**（`temp_bank.wrong_min_times` 默认 **1**、`wrong_master_after` 默认 2）：
  **做错一道就收进来**（2026-10-15 用户要求"把错题判断改成1"；想回到"错两次才算"就把
  `wrong_min_times` 写成 2）、连着答对 2 次移出去，工具栏的「刷错题集（N）」只刷它们，
  错得最多的排前面。**错了几次是现算的**（从 `temp_answers`），不加字段、不做迁移；
  刷错题集不会动「开始刷题」记的位置（两边顺序不同）；
- **选项顺序每次重新刷都会打乱**（`temp_bank.shuffle_options`，默认开；用户原话
  "每次重新刷题后，把选项打乱"）。**答案字母是按选项位置记的**（`answer="C"` 指第 3 个
  选项），所以打乱之后必须重算，否则判分会反过来——`temp_bank.shuffle_options()` 干这个，
  有测试盯着"答案指的还是同一个选项"。判断题没有可换的选项；
  **解析里明确写了"选项A是…"的题不打乱**（`uses_option_letters()`）：真实题库 279 道里
  命中 2 道，打乱会把解析指到别的选项上。别想着连解析里的字母一起换——`ARP`/`IP`/`DNS`
  这类术语满地都是，按单个字母替换会把解析改烂；
- **键盘刷题**：数字键 `1`–`4` 选选项（`1`＝第 1 个，判断题 `1`＝正确、`2`＝错误；
  多选可连按切换，真实题库有 A–E 所以 `5` 也认）、`Enter` 提交，**答完再按 `Enter`
  就是下一题**。按键处理在面板自己的 `keyPressEvent`，**故意不用窗口级 `QShortcut`**
  ——那样切到别的页签打字也会被吃掉。难点是**焦点**：切页签时
  `main_window._on_tab_changed` 里要给这一栏 `setFocus()`，不然用户点完页签按键没人理；
  问答题在输入框里打字时按键归输入框，不许抢（有测试盯着）；
- 考完删 `data/temp_bank/` 文件夹，再删 `ui/main_window.py` 里加 tab 的那三行。

### 知识图谱（第七个 tab）

一张图同时表达三件事：**圆圈大小 = 题量**、**颜色 = 自己的正确率**、
**连线 = 知识点之间的关系**。点某个圆圈，右边显示它考过什么题型、
你做过几道对几道，还能「只练这个知识点」（直接进答题页）。

- 算的部分全在 `graph.py`，**故意不 import Qt**（跟 `grading.py` 一个道理），
  布局和分档才好单测。界面在 `ui/graph_panel.py`，关系确认框在 `ui/relation_dialog.py`。
- **连线是 AI 提的，但必须用户在弹窗里逐条勾选确认后才入库**（`point_edges` 表里
  只有确认过的）。原因写在 `docs/decisions.md`：关系是判断不是事实，
  一条假关系看着跟真的一样。
- **点图上的连线可以直接删掉它**（`_EdgeItem`）。`QGraphicsLineItem` 默认
  不接受鼠标按下——不写这个子类，线画在那儿点上去什么都不会发生，
  而图例上还写着"点它可删"（第一版就是这样）。命中判定用加粗笔画，
  看得见的是细线、点得中的是宽条子。
- **AI 抄知识点名字会抄岔**（实测 64 个名字里有 3 个：多空格、少个"的"、被截断），
  所以 `ai/relations.resolve_name()` 有一层有限的宽容。**阈值是量出来的**，
  而且必须同时满足"相似度够高"**和**"明显甩开第二名"——考纲里"一维数组"
  和"二维数组"的相似度就有 0.938，只看相似度会把这两个不同的知识点配到一起。
  改阈值之前先看 `relations.py` 顶上那段，再跑一遍压测。
- **掌握度样本不够就按"没练到"算**（`graph.MIN_SAMPLE = 3`）：做 1 道错 1 道标成
  大红，用户会去补一个其实不弱的地方。判据是**练过几道不同的题**，
  不是"做过多少次"——这软件自己就在引导重刷。
- **布局是确定性的**，而且只有图的结构变了才重排——位置一跳，用户记的
  "我的薄弱区在右上角那团"就废了。
- **圆圈大小和点间距用同一个单位**（都是画布边长的几分之几），
  混用像素会在窗口拉宽时开始叠。`MIN_GAP > 2 * MAX_RADIUS` 有单测盯着。
- **每一章的"范围圈"画不出来**，是算下来不让画：10 个章的范围加起来比整块画布还大。
  靠"位置聚拢 + 团上方的章名"表示归属。三种失败的画法都记在 `decisions.md` 里。
- 改完界面**渲染成 PNG 看一眼**：这个功能踩过的坑（画布白板、名字忘了 `addItem`、
  标注互相盖住、章名太长）**跑测试一个都发现不了**。

**数据库每天启动时自动备份**到 `data/backups/cstudy-日期.db`（留最近 14 份）。
用 SQLite 自己的 backup API，不是复制文件——直接复制可能拿到写了一半的库。
**库里唯一不可再生的是用户的作答记录和错题本**，题目丢了能用脚本重导。

**「跑一下」用的编译器**：MinGW-w64（WinLibs，winget 装的，
在 `%LOCALAPPDATA%\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.POSIX.UCRT_*\mingw64\bin\gcc.exe`）。
查找逻辑在 `tools.py` 的 `find_gcc()`——**故意不缓存**，因为用户很可能在软件开着时
才去装编译器，装完点「跑一下」就该能用。编译和运行在后台线程里跑
（代码里写 `while(1)` 会卡住主线程），错误会翻译成中文（见 `runner.py` 的 `ERROR_HINTS`）。

**题型有两套口径，别搞混**：库里的 `type` 是三类（fill/reading/coding），
而**考试是四类**——"填空"和"程序填空"是两道不同的大题（48 分 vs 36 分）。
所以 `questions.exam_type` 这一列按考试口径算（多一个 `code_fill`），
**筛选、组卷、界面显示都用 `exam_type`**，判据在 `models.exam_type_for()`。
它还会顺手纠正一批历史误标——当初有一批**程序阅读题**因为题干里有 `____`
被标成了 `fill`。

**题库已导入**：闲鱼买的题库（127 个文件、119MB）用 `scripts/import_bank.py` 批量导完。
导入时按 `import.skip_types` 滤掉选择题——四所目标校都不考。**扫描件用 `--ocr`**
（pypdfium2 渲染成图 → 发给 DeepSeek 认，不用装 OCR 库）。
**导完要跑 `scripts/map_syllabus.py`** 把知识点对齐到考纲，否则「考纲」标签页
和按知识点筛选都是空的。

### B 站接口的坑（改动前务必先看）

1. **库的接口是 async 的**：`Video.get_info()` / `get_subtitle()` 都要 await。
   同步调用会拿到协程对象，报 `'coroutine' object has no attribute 'get'`。
   用 `bilibili/video.py` 里的 `_run_async()` 桥接。
2. **curl_cffi 的异常不是 `requests` 的子类**（它挂在 `OSError` 下面）。
   `_retry` 里必须单独捕一次，否则 TLS 抖动会绕过重试直接冒到界面。
3. **登录 cookie 走 `Set-Cookie`，不在 URL 里了**：crossDomain 那个 URL 现在只剩
   `ticket` / `gourl` / `first_domain`。bilibili-api 仍按老格式从 URL 抠 cookie，
   抠出六个空字符串**而且不报错**——脚本照样打印"登录成功"，直到导入视频才发现。
   所以要从 HTTP 会话的 cookie jar 读（见 `auth.py` 的 `credential()`）。
4. **TLS 握手偶发被掐断**（实测约 1/5 概率，`SSL_ERROR_SYSCALL`）。
   所有 B 站调用都必须带重试。
5. **必须装 `curl_cffi`**：bilibili-api 把 HTTP 后端列为**可选依赖**，不装的话
   调用任何接口都直接报"尚未安装第三方请求库或未注册自定义第三方请求库"。

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

