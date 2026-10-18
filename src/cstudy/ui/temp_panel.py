"""「考前突击」这一栏。

临时加的：考前这几天把另一门考试的题库（单选 / 多选 / 判断 / 问答）导进来刷。

**这一栏和原来的题库完全不混**：
- 题目存在独立的库文件里（`data/temp_bank/bank.db`，见 `cstudy.temp_bank`）；
- 这里做错的题**不进「错题本」那一页**，这里的作答**不进学情统计**；
- 反过来，原来那些 C 语言的题也不会出现在这一栏的筛选里。

这一栏**自己**记错题：做错的题收进错题集（默认错 1 次就收，见 `temp_bank.wrong_min_times`），
点「刷错题集」就只刷它们，连着答对 2 次又移出去（判据都是从 `temp_answers` 现算的，不另存状态）。

考完想清干净：删掉 `data/temp_bank/` 这个文件夹、再把 `main_window.py` 里
加 tab 的那三行删掉就完了，主题库一个字都没动过。

界面上任何一处出错都不能弹 traceback 给用户看——他不会 Python。
"""

from __future__ import annotations

import random
from html import escape
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import Config, ConfigError
from ..logging_setup import get_logger
from ..temp_bank import (
    TYPE_LABELS,
    TYPE_ORDER,
    WRONG_MASTER_AFTER,
    WRONG_MIN_TIMES,
    TempBank,
    TempQuestion,
    collect_files,
    shuffle_options,
)
from .theme import DARK, Theme, set_variant
from .workers import TaskWorker

log = get_logger(__name__)

FILE_FILTER = "题库文件 (*.pdf *.docx *.doc *.txt *.md);;所有文件 (*)"
# 预览框里最多列这么多条问题，免得一个坏文件刷出几百行
MAX_PROBLEM_LINES = 20
JUDGE_OPTIONS = (("正确", "正确"), ("错误", "错误"))


def bank_path(config: Config) -> Path:
    """这一栏的库文件在哪。配置里没写就退回默认位置。

    `paths.temp_bank` 是**可选**配置：这一栏是临时加的，
    不该因为配置文件里少写一项就让整个软件打不开。
    """
    try:
        return config.path("temp_bank")
    except ConfigError:
        return config.root / "data" / "temp_bank" / "bank.db"


def _hint(text: str) -> QLabel:
    """工具条上那种"题型 / 次序 / 每轮"的小标签：灰一点、小一点，别跟选项抢眼。"""
    label = QLabel(text)
    label.setObjectName("pageHint")
    return label


def html_text(text: str) -> str:
    """要塞进富文本框的普通文字。

    换行转 `<br>`；连续空格换成不换行空格——HTML 会把「（    ）」里那四个空格
    压成一个，题干上的填空看起来就变窄了；问答题的参考答案也是按空格对齐的。
    """
    escaped = escape(text or "").replace("  ", "\u00a0\u00a0")
    return escaped.replace("\n", "<br>")


class TempBankPanel(QWidget):
    def __init__(
        self, config: Config, parent: QWidget | None = None, theme: Theme = DARK
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._theme = theme
        self.bank = TempBank(bank_path(config))
        # 上一次装的那一批：「重刷这批」要的是同一批题（选项每次重新打乱，见 _prepare）
        self._batch: list[int] = []
        # 「每次重新刷题把选项打乱」：用户要求，默认开；不想要就把 config.yaml 里
        # temp_bank.shuffle_options 写成 false
        raw_shuffle = config.get("temp_bank.shuffle_options", True)
        self._shuffle_options = str(raw_shuffle).strip().lower() not in {
            "false", "0", "no", "off",
        }
        self._rng = random.Random()

        self._items: list[TempQuestion] = []
        self._index = 0
        self._offset = 0      # 这一轮的第一道，在这个题型里排第几（做进度用）
        self._grand = 0       # 这个题型一共多少道
        self._answered = False
        self._right = 0
        self._wrong = 0
        # 现在这一轮刷的是"错题集"还是正常那份：两者的顺序、进度文案都不一样
        self._wrong_mode = False
        self._wrong_counts: dict[int, int] = {}   # 题 id → 错过几次（错题模式下显示用）
        self._worker: TaskWorker | None = None
        # 键盘刷题（用户："这样刷有点慢了"→"你改绑成1234吧"）：1–4 选选项、Enter 提交。
        # 要能收到按键，面板自己得先能拿焦点（见 showEvent）。
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._build_ui()
        self._batch = self._batch_ids()      # 上次那批记在库里，关了软件重开也认得
        self.refresh_counts()
        self.type_combo.currentIndexChanged.connect(lambda *_: self._update_buttons())
        self.order_combo.currentIndexChanged.connect(lambda *_: self._update_buttons())
        self._update_buttons()
        self._show_placeholder(self._idle_placeholder())

    # ---------- 搭界面 ----------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.bank_label = QLabel("")
        top.addWidget(self.bank_label)
        top.addStretch(1)
        self.import_file_button = QPushButton("导入文件")
        self.import_file_button.setToolTip("选一个或多个文件（PDF / Word / txt）")
        self.import_file_button.clicked.connect(self._import_files)
        self.import_folder_button = QPushButton("导入文件夹")
        self.import_folder_button.setToolTip("把一个文件夹里的题库文件一次全导进来")
        self.import_folder_button.clicked.connect(self._import_folder)
        self.clear_button = QPushButton("清空这一栏")
        self.clear_button.setToolTip("只清这一栏，原来的题库一个字的都不动")
        self.clear_button.clicked.connect(self._clear_bank)
        for button in (self.import_file_button, self.import_folder_button, self.clear_button):
            set_variant(button, "ghost")
            top.addWidget(button)
        root.addLayout(top)

        # 原来是一整段连着的灰字，横跨整屏、换行处没有停顿，读起来很累。
        # 拆成两行：第一行讲"这栏是独立的"，第二行讲"怎么刷"。
        note = QLabel(
            "这一栏的题存在单独的库里，不会和你原来的题库混在一起；"
            "这里做错的题也不会进「错题本」那一页、不算进学情"
            "——这一栏自己记错题，点「刷错题集」就能只刷它们。<br>"
            "每次重新刷题选项顺序都会换，别记「答案是 C」这种位置。"
            "键盘也能刷：数字键 1–4 选选项（多选可连按），Enter 提交，"
            "答完再按 Enter 就是下一题。"
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setObjectName("pageHint")
        root.addWidget(note)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(_hint("题型"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部", "")
        for key in TYPE_ORDER:
            self.type_combo.addItem(TYPE_LABELS[key], key)
        bar.addWidget(self.type_combo)

        bar.addWidget(_hint("次序"))
        self.order_combo = QComboBox()
        self.order_combo.addItem("按顺序", "order")
        self.order_combo.addItem("随机", "random")
        bar.addWidget(self.order_combo)

        bar.addWidget(_hint("每轮"))
        self.round_combo = QComboBox()
        for size in (10, 20, 50):
            self.round_combo.addItem(f"{size} 道", size)
        self.round_combo.addItem("全部", 0)
        # 默认每轮几道写在 config.yaml 的 temp_bank.round_size 里（没写就 20 道）
        self._select_round_size(self.config.get("temp_bank.round_size", 20))
        self.round_combo.setToolTip(
            "一轮做几道。做完再点一次「开始刷题」往下接，不用一次把几百道做完。"
        )
        bar.addWidget(self.round_combo)

        # 主按钮就是"继续/开始"：点一下给一批，再点一下给下一批。
        # "从第 1 道重刷"是少数情况，单独一个按钮（点它才会清进度）。
        self.start_button = QPushButton("开始刷题")
        self.start_button.setToolTip("从第 1 道开始，一轮只取上面「每轮」那么多道")
        self.start_button.clicked.connect(self.start)
        set_variant(self.start_button, "primary")
        self.restart_button = QPushButton("重刷这批")
        self.restart_button.setToolTip("把刚才那批再刷一遍（不回到整份的第 1 道）")
        self.restart_button.clicked.connect(self.restart)
        set_variant(self.restart_button, "ghost")
        self.wrong_button = QPushButton("刷错题集")
        self.wrong_button.clicked.connect(self.start_wrong)
        set_variant(self.wrong_button, "ghost")
        bar.addWidget(self.start_button)
        bar.addWidget(self.restart_button)
        bar.addWidget(self.wrong_button)

        bar.addStretch(1)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("pageHint")
        self.score_label = QLabel("")
        self.score_label.setObjectName("pageHint")
        root.addLayout(bar)

        # 进度和本轮成绩单独一行：挤在工具栏右边时，窗口一窄它们会被顶出可视区，
        # 屏幕上就看不到"刷到第几道"了（渲染成 PNG 才看出来的）。
        info = QHBoxLayout()
        info.addStretch(1)
        info.addWidget(self.progress_label)
        info.addWidget(self.score_label)
        root.addLayout(info)

        # 题目区放在滚动区域里：问答题的题干加参考答案可能很长
        self.body = QWidget()
        body_layout = QVBoxLayout(self.body)

        self.meta_label = QLabel("")
        self.meta_label.setObjectName("pageHint")
        self.meta_label.setWordWrap(True)
        body_layout.addWidget(self.meta_label)

        self.question_view = QTextBrowser()
        self.question_view.setOpenExternalLinks(False)
        self.question_view.setMinimumHeight(90)
        self.question_view.setMaximumHeight(360)
        # 高度跟着内容走。Qt 的默认策略是"有多少地方占多少"，
        # 那样还没作答时，下面那块空的反馈框会占掉半个屏幕。
        self.question_view.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        body_layout.addWidget(self.question_view)

        # 选择题的选项；判断题也画在这里
        self.options_box = QWidget()
        self.options_layout = QVBoxLayout(self.options_box)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self._group = QButtonGroup(self)
        self._option_widgets: list[QWidget] = []
        body_layout.addWidget(self.options_box)

        # 问答题：先自己写一遍再看答案（写不写都行，判分不看你写的）
        self.answer_edit = QPlainTextEdit()
        self.answer_edit.setPlaceholderText("可以先在这里写一遍，再点「看参考答案」（写不写都行）")
        self.answer_edit.setMaximumHeight(120)
        body_layout.addWidget(self.answer_edit)

        self.feedback = QTextBrowser()
        self.feedback.setOpenExternalLinks(False)
        self.feedback.setMinimumHeight(60)
        self.feedback.setMaximumHeight(360)
        self.feedback.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        body_layout.addWidget(self.feedback)
        # 还没作答时这块是空的，先藏起来——留一大片空白很难看
        self.feedback.setVisible(False)

        body_layout.addStretch(1)   # 多出来的高度给这条弹簧，别让两个文本框撑满

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.body)
        root.addWidget(scroll, stretch=1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.submit_button = QPushButton("提交")
        self.submit_button.setToolTip("也可以直接按 Enter")
        self.submit_button.clicked.connect(self._on_submit)
        set_variant(self.submit_button, "primary")
        self.next_button = QPushButton("下一题")
        self.next_button.setToolTip("答完之后按 Enter 也一样")
        self.next_button.clicked.connect(self._next)
        set_variant(self.next_button, "ghost")
        self.known_button = QPushButton("这题我会了")
        self.known_button.clicked.connect(lambda: self._self_grade(True))
        set_variant(self.known_button, "primary")
        self.unknown_button = QPushButton("这题没记住")
        self.unknown_button.clicked.connect(lambda: self._self_grade(False))
        set_variant(self.unknown_button, "danger")
        for button in (
            self.submit_button,
            self.next_button,
            self.known_button,
            self.unknown_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.answer_edit.setVisible(False)
        self.known_button.setVisible(False)
        self.unknown_button.setVisible(False)

    # ---------- 导入 ----------

    def _import_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择题库文件", "", FILE_FILTER)
        if files:
            self._start_import([Path(item) for item in files])

    def _import_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择题库文件夹", str(Path.home()))
        if folder:
            self._start_import([Path(folder)])

    def _start_import(self, paths: list[Path]) -> None:
        if self._worker is not None:
            QMessageBox.information(self, "稍等一下", "上一次导入还在读文件，读完再试。")
            return
        # 解析可能要十几秒（PDF、扫描件更久），丢后台线程，界面不能卡
        self._set_status(f"正在读 {len(paths)} 个位置…")
        worker = TaskWorker(collect_files, paths)
        worker.progress.connect(self._set_status)
        worker.finished_ok.connect(self._after_parse)
        worker.failed.connect(self._import_failed)
        self._worker = worker
        worker.start()

    def _import_failed(self, message: str) -> None:
        self._worker = None
        self._set_status("导入失败")
        QMessageBox.warning(self, "没读成", f"读文件时出了点问题：\n\n{message}")

    def _after_parse(self, bundle) -> None:
        self._worker = None
        self._set_status("导入完成" if bundle.questions else "没解析出题目")

        if not bundle.questions:
            detail = "\n".join(bundle.problems[:MAX_PROBLEM_LINES]) or "文件里没有认出题目。"
            QMessageBox.information(
                self,
                "没解析出题目",
                "这些文件里没有认出题目，一道都没入库。\n\n"
                "这一栏是按「[单选题] / [多选题] / [判断题] / [问答题]」这种"
                "题型标记来拆题的，卷面上得有这样的标记才行。\n\n"
                f"具体情况：\n{detail}",
            )
            return

        counts = {}
        for question in bundle.questions:
            counts[question.qtype] = counts.get(question.qtype, 0) + 1
        detail = "、".join(
            f"{TYPE_LABELS[key]} {counts[key]}" for key in TYPE_ORDER if counts.get(key)
        )
        box = QMessageBox(self)
        box.setWindowTitle("导入预览")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"解析出 {len(bundle.questions)} 道题（{detail}）\n\n要存进这一栏吗？")
        if bundle.problems:
            box.setInformativeText(
                f"有 {len(bundle.problems)} 处需要留意，点「显示详情」看看。"
            )
            box.setDetailedText("\n".join(bundle.problems[:MAX_PROBLEM_LINES]))
        ok = box.addButton("存进这一栏", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("先不存", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not ok:
            self._set_status("这次导入取消了，什么都没入库")
            return

        inserted, skipped = self.bank.add(bundle.questions)
        self.refresh_counts()
        self._update_buttons()
        if not self._items:      # 导完还没开始刷：中间的提示要跟着库里的情况变
            self._show_placeholder(self._idle_placeholder())
        message = f"存进去 {inserted} 道题。"
        if skipped:
            message += f"\n有 {skipped} 道题在库里已经有一样的题干，跳过了。"
        self._set_status(message.replace("\n", " "))
        QMessageBox.information(self, "导入完成", message)

    def _clear_bank(self) -> None:
        total = sum(self.bank.count_by_type().values())
        if not total:
            QMessageBox.information(self, "本来就是空的", "这一栏里还没有题。")
            return
        answer = QMessageBox.question(
            self,
            "确定要清空吗",
            f"把这一栏里的 {total} 道题和作答记录都删掉？\n\n"
            "只清这一栏，原来的题库和错题本不受影响。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.bank.clear()
        self._items = []
        self._batch = []          # 题都清掉了，"上一批"也就没了
        self._wrong_mode = False
        self.refresh_counts()
        self._update_buttons()
        self._show_placeholder("这一栏已经清空了。")
        self._set_status("这一栏已清空")

    # ---------- 刷题 ----------

    def refresh_counts(self) -> None:
        counts = self.bank.count_by_type()
        total = sum(counts.values())
        if total:
            detail = "、".join(
                f"{TYPE_LABELS[key]} {counts[key]}" for key in TYPE_ORDER if counts.get(key)
            )
            self.bank_label.setText(f"这栏共 {total} 道：{detail}")
        else:
            self.bank_label.setText("这栏还没有题")
        self._update_wrong_button()

    def _update_wrong_button(self) -> None:
        """「刷错题集」按钮上带个数字：一眼看出攒了多少道，没有就点不动。"""
        count = self.bank.wrong_count(self._wrong_min_times(), self._wrong_master_after())
        self.wrong_button.setText(f"刷错题集（{count}）" if count else "刷错题集")
        self.wrong_button.setEnabled(bool(count))
        if count:
            self.wrong_button.setToolTip(
                f"只刷这 {count} 道错题，错得多的排前面；"
                f"连着答对 {self._wrong_master_after()} 次就移出错题集"
            )
        else:
            min_times = self._wrong_min_times()
            self.wrong_button.setToolTip(
                "还没有做错过的题。做错一道就会收进来"
                if min_times <= 1
                else f"还没有错 {min_times} 次的题。"
                f"同一道题错到 {min_times} 次会自动收进来"
            )

    def start(self) -> None:
        """开始刷题：接着上次刷到的地方往下刷；还没刷过就从第 1 道开始。

        用户第一次用的时候点它却被送回第 1 道（原话："我点开始刷题后他又从第一轮
        给我开始刷了"）。当时的设计是「开始刷题」= 从头、「接着上次刷」= 接续，
        跟直觉正好相反；而且点「开始刷题」还会把位置清掉、顺手把「接着上次刷」
        也弄灰，等于回不到原来那道题。现在反过来：主按钮就是"继续/开始"，
        想把刚做完的那一批再刷一遍，用「重刷这批」——它不碰"刷到哪了"。
        """
        if self.order_combo.currentData() != "order":
            # 随机模式没有"上次刷到哪"这回事：直接翻一批新的，但不动存着的位置
            self._load(after_id=None)
            return
        raw = self.bank.meta_get(self._position_key())
        try:
            after_id = int(raw) if raw else None
        except ValueError:
            after_id = None      # 存进去的东西坏了，就当没存过
        self._load(after_id=after_id)

    def restart(self) -> None:
        """重刷这批：把刚才那一批再刷一遍（同一批题、题目顺序照旧），**不回到第 1 道**。

        用户原话："我要重头再刷的意思是我要重新刷我刚刚刷完的二十道题，
        而不是从第一道题开始刷"。所以这里不碰"刷到哪了"，只把这一批重装一遍：
        一批之内从第 1 道开始，本轮对错计数清零，题型和顺序照旧。
        **选项会重新打乱**——用户要的就是"重新刷的时候换个顺序"。
        """
        questions = self.bank.questions_by_ids(self._batch)
        if not questions:
            return
        self._wrong_mode = self.bank.meta_get("batch:mode") == "wrong"
        self._wrong_counts = {}
        if self._wrong_mode:
            self._wrong_counts = {
                item.question.id: item.wrong_count
                for item in self.bank.wrong_items(
                    self._wrong_min_times(), self._wrong_master_after()
                )
                if item.question.id is not None
            }
        self._items = self._prepare(questions)
        self._index = 0
        self._right = 0
        self._wrong = 0
        self._offset = self._int_meta("batch:offset")
        self._grand = self._int_meta("batch:grand") or len(questions)
        self._update_buttons()
        self._update_wrong_button()
        self._show_question()

    def start_wrong(self) -> None:
        """只刷错题集：做错过的题（默认错 1 次就收）、还没连着答对 2 次的题。"""
        self._load(after_id=None, wrong=True)

    def _select_round_size(self, size: object) -> None:
        """把配置里的每轮题量选中；配置里写了个怪值就退回 20 道。"""
        try:
            wanted = int(size)
        except (TypeError, ValueError):
            wanted = 20
        index = self.round_combo.findData(wanted)
        if index < 0:
            index = self.round_combo.findData(20)
        self.round_combo.setCurrentIndex(index)

    def _int_config(self, key: str, default: int) -> int:
        """从 config.yaml 读一个正整数；写错了就用默认值，不让它把功能搞挂。"""
        raw = self.config.get(key, default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            log.warning("%s 配置看不懂（%r），用默认值 %s", key, raw, default)
            return default
        return value if value > 0 else default

    def _wrong_min_times(self) -> int:
        """错几次才收进错题集。"""
        return self._int_config("temp_bank.wrong_min_times", WRONG_MIN_TIMES)

    def _wrong_master_after(self) -> int:
        """连着答对几次移出错题集。"""
        return self._int_config("temp_bank.wrong_master_after", WRONG_MASTER_AFTER)

    def _round_size(self) -> int | None:
        """这一轮取几道；「全部」返回 None（不限量）。"""
        try:
            size = int(self.round_combo.currentData())
        except (TypeError, ValueError):
            size = 0
        return size or None

    def _batch_ids(self) -> list[int]:
        """从临时库读回"上一次那批"的题号。"""
        raw = self.bank.meta_get("batch:ids") or ""
        ids: list[int] = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids

    def _int_meta(self, key: str) -> int:
        """读一个存成字符串的整数；没存过或者存坏了就当 0。"""
        raw = self.bank.meta_get(key)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    def _prepare(self, items: list[TempQuestion]) -> list[TempQuestion]:
        """把一批题装进这一轮之前的处理：**选项打乱**。

        用户原话："每次重新刷题后，把选项打乱"——为的是记住"答案是 C"这种事，
        而不是真会了。打乱的是**显示用的那一份**（题目对象是新建的，库里不动），
        答案字母跟着重算；判断题和问答题不涉及（见 `temp_bank.shuffle_options`）。
        一轮之内只打乱一次：翻回上一题时看到的是同一套顺序，不会晃。
        """
        if not self._shuffle_options:
            return items
        return [shuffle_options(question, self._rng) for question in items]

    def _remember_batch(self) -> None:
        """把刚装的这一批记进临时库——「重刷这批」要的是同一批题。

        记进库里而不是只放内存，是为了关了软件重开之后这个按钮还有用。
        记的是**题号顺序**（不做选项顺序：选项每次重新刷都会换）。
        """
        ids = [q.id for q in self._items if q.id is not None]
        if not ids:
            return
        self._batch = ids
        self.bank.meta_set("batch:ids", ",".join(str(i) for i in ids))
        self.bank.meta_set("batch:mode", "wrong" if self._wrong_mode else "normal")
        self.bank.meta_set("batch:offset", str(self._offset))
        self.bank.meta_set("batch:grand", str(self._grand))

    def _position_key(self) -> str:
        """「上次刷到哪」按题型分开记——换题型不该套用另一个题型的位置。"""
        wanted = self.type_combo.currentData() or "all"
        return f"pos:{wanted}"

    def _load(self, after_id: int | None, wrong: bool = False) -> None:
        self._wrong_mode = wrong
        wanted = self.type_combo.currentData() or ""
        qtypes = [wanted] if wanted else None
        shuffle = self.order_combo.currentData() == "random"
        size = self._round_size()
        self._wrong_counts = {}
        if wrong:
            items = self.bank.wrong_items(
                self._wrong_min_times(), self._wrong_master_after(), qtypes
            )
            if shuffle:
                random.shuffle(items)
            batch = items if size is None else items[:size]
            self._items = self._prepare([item.question for item in batch])
            self._wrong_counts = {
                item.question.id: item.wrong_count
                for item in items
                if item.question.id is not None
            }
            self._offset = 0
            self._grand = len(items)
        else:
            self._items = self._prepare(
                self.bank.questions(qtypes, shuffle=shuffle, limit=size, after_id=after_id)
            )
            self._offset = (
                self.bank.count_questions(qtypes, upto_id=after_id)
                if after_id is not None
                else 0
            )
            self._grand = self.bank.count_questions(qtypes)
        self._index = 0
        self._right = 0
        self._wrong = 0
        self._remember_batch()
        self._update_buttons()
        self._update_wrong_button()

        if not self._items:
            counts = self.bank.count_by_type()
            if wrong:
                min_times = self._wrong_min_times()
                how = "做错一道" if min_times <= 1 else f"同一道题错到 {min_times} 次"
                self._show_placeholder(
                    f"错题集里还没有这个题型的题（{self.type_combo.currentText()}）。"
                    f" {how}就会自动收进来，"
                    f"连着答对 {self._wrong_master_after()} 次又会移出去。"
                )
            elif sum(counts.values()) == 0:
                self._show_placeholder("这一栏还没有题。先点上面的「导入文件」。")
            elif after_id is not None:
                self._show_placeholder(
                    f"这个题型已经刷到头了（{self.type_combo.currentText()}）。"
                    " 想把刚才那一批再刷一遍点「重刷这批」，"
                    "想翻新题就把题型换成别的、或者点「清空这一栏」重导。"
                )
            else:
                self._show_placeholder(f"这个题型这里还没有题（{self.type_combo.currentText()}）。")
            return
        self._show_question()

    def _update_buttons(self) -> None:
        """刷新两个按钮的文字说明和能不能点。"""
        ordered = self.order_combo.currentData() == "order"
        raw = self.bank.meta_get(self._position_key())
        try:
            has_position = bool(raw) and int(raw) > 0
        except ValueError:
            has_position = False

        # 「重刷这批」：要有"上一批"才点得动（关了软件重开也还认得）
        self.restart_button.setEnabled(bool(self._batch))
        if self._batch:
            self.restart_button.setToolTip(
                f"把刚才那批 {len(self._batch)} 道再刷一遍（从这批的第 1 道开始，"
                "不会回到整份题库的第 1 道）"
            )
        else:
            self.restart_button.setToolTip("还没刷过哪一批，先点「开始刷题」")

        if not ordered:
            self.start_button.setToolTip("随机翻一批，一轮只取上面「每轮」那么多道")
        elif has_position:
            self.start_button.setToolTip("接着上次答过的那道题往下刷")
        else:
            self.start_button.setToolTip("从第 1 道开始，一轮只取上面「每轮」那么多道")

    def _show_question(self) -> None:
        question = self._items[self._index]
        self._answered = False
        self.question_view.setVisible(True)   # 空态时被收起来了，这里放出来
        self.feedback.clear()
        self.feedback.setVisible(False)   # 作答之前不占地方
        self._clear_options()
        self.answer_edit.clear()
        is_short = question.qtype == "short"
        self.answer_edit.setVisible(is_short)
        self.known_button.setVisible(False)
        self.unknown_button.setVisible(False)
        self.next_button.setEnabled(False)
        self.submit_button.setVisible(True)
        self.submit_button.setEnabled(True)
        self.submit_button.setText("看参考答案" if is_short else "提交")

        meta = [question.type_label]
        if self._wrong_mode and question.id in self._wrong_counts:
            meta.append(f"错过 {self._wrong_counts[question.id]} 次")
        if question.knowledge:
            meta.append(f"知识点：{question.knowledge}")
        if question.source:
            meta.append(f"来源：{question.source}")
        self.meta_label.setText("　".join(meta))

        stem = html_text(question.stem)
        self.question_view.setHtml(f"<b>{self._index + 1}.</b>&nbsp;{stem}")
        self._build_options(question)
        self._update_progress()

    def _build_options(self, question: TempQuestion) -> None:
        """单选画单选钮、多选画复选框、判断画"正确/错误"两个钮、问答没有选项。"""
        if question.qtype == "single":
            for index, text in enumerate(question.options):
                letter = chr(ord("A") + index)
                button = QRadioButton(f"{letter}. {text}")
                button.setProperty("value", letter)
                self._group.addButton(button)
                self.options_layout.addWidget(button)
                self._option_widgets.append(button)
        elif question.qtype == "multi":
            for index, text in enumerate(question.options):
                letter = chr(ord("A") + index)
                box = QCheckBox(f"{letter}. {text}")
                box.setProperty("value", letter)
                self.options_layout.addWidget(box)
                self._option_widgets.append(box)
        elif question.qtype == "judge":
            for text, value in JUDGE_OPTIONS:
                button = QRadioButton(text)
                button.setProperty("value", value)
                self._group.addButton(button)
                self.options_layout.addWidget(button)
                self._option_widgets.append(button)

    def _clear_options(self) -> None:
        for button in self._group.buttons():
            self._group.removeButton(button)
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._option_widgets = []

    def _collect_answer(self, question: TempQuestion) -> str | None:
        """用户选了什么。一个都没选返回 None。"""
        if question.qtype == "multi":
            letters = sorted(
                widget.property("value")
                for widget in self._option_widgets
                if isinstance(widget, QCheckBox) and widget.isChecked()
            )
            return "".join(letters) if letters else None
        for widget in self._option_widgets:
            if isinstance(widget, QRadioButton) and widget.isChecked():
                return str(widget.property("value"))
        return None

    # ---------- 键盘刷题 ----------

    def showEvent(self, event) -> None:      # noqa: N802 —— Qt 的写法，下同
        """切到这一栏时把焦点拿过来，不然按了键没人理。"""
        super().showEvent(event)
        self.setFocus()

    def keyPressEvent(self, event) -> None:      # noqa: N802
        if self._handle_shortcut(event):
            return
        super().keyPressEvent(event)

    def _handle_shortcut(self, event) -> bool:
        """数字键 1–4 选选项、Enter 提交（已作答时 Enter 就是下一题）。True＝这个键我处理了。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # 用 _answered 判断，不用 isVisible()：控件没显示时 isVisible() 是 False，
            # 那样连测试里都按不动（真实窗口里这一栏显示着，两者等价）。
            if not self._answered and self.submit_button.isEnabled():
                self._on_submit()
                return True
            if self.next_button.isEnabled():
                self._next()
                return True
            # 问答题看完参考答案还没自评：下一题本来就点不动，别装作处理了
            return False
        # 用户要求："你改绑成1234吧"。题干里的选项最多到 E，所以 5 也认；
        # 题里没那么多选项时多按一下什么也不会发生（下面按位置判断）。
        digit = (event.text() or "").strip()
        if digit in ("1", "2", "3", "4", "5"):
            return self._press_option(int(digit) - 1)
        return False

    def _press_option(self, index: int) -> bool:
        """按数字键 ＝ 点第 index 个选项（判断题 1＝正确、2＝错误）；多选是切换。"""
        if index < 0 or index >= len(self._option_widgets):
            return False
        widget = self._option_widgets[index]
        if isinstance(widget, QCheckBox):
            widget.setChecked(not widget.isChecked())
        elif isinstance(widget, QRadioButton):
            widget.setChecked(True)
        else:
            return False
        return True

    def _on_submit(self) -> None:
        if not self._items or self._answered:
            return
        question = self._items[self._index]

        if question.qtype == "short":
            self._reveal_reference(question)
            return

        user_answer = self._collect_answer(question)
        if user_answer is None:
            QMessageBox.information(self, "还没作答", "先选一个答案再提交。")
            return

        is_correct = bool(question.check(user_answer))
        self._record(question, user_answer, is_correct)
        self._set_feedback(question, user_answer, is_correct)

    def _reveal_reference(self, question: TempQuestion) -> None:
        """问答题没法自动判分：把参考答案摆出来，让用户自己评。"""
        self._answered = True
        self.submit_button.setVisible(False)
        self.known_button.setVisible(True)
        self.unknown_button.setVisible(True)
        parts = [
            "<div><b>参考答案：</b></div>",
            f"<div>{html_text(question.answer)}</div>",
        ]
        if question.explanation:
            parts.append(
                f"<div style='color:{self._theme.muted};'><b>解析：</b>"
                f"{html_text(question.explanation)}</div>"
            )
        parts.append("<div style='margin-top:6px;'>对着答一遍，再选下面「我会了 / 没记住」。</div>")
        self.feedback.setVisible(True)
        self.feedback.setHtml("".join(parts))

    def _self_grade(self, known: bool) -> None:
        if not self._items or not self._answered:
            return
        question = self._items[self._index]
        self._record(question, self.answer_edit.toPlainText().strip(), known)
        self.known_button.setVisible(False)
        self.unknown_button.setVisible(False)
        color = self._theme.ok if known else self._theme.danger
        word = "会了" if known else "没记住"
        self.feedback.setVisible(True)
        self.feedback.setHtml(
            f"<div style='color:{color};font-weight:bold;'>记下了：这题你{word}。</div>"
        )

    def _record(self, question: TempQuestion, user_answer: str, is_correct: bool) -> None:
        if question.id is not None:
            self.bank.record_answer(question.id, user_answer, is_correct)
            if not self._wrong_mode:
                # 记下"刷到哪了"，下次点「开始刷题」从这道题后面接。
                # 刷错题集时不写：错题集的顺序跟正常那份不一样，写了位置就串了。
                self.bank.meta_set(self._position_key(), str(question.id))
        if is_correct:
            self._right += 1
        else:
            self._wrong += 1
        self._answered = True
        self.submit_button.setEnabled(False)
        self.next_button.setEnabled(True)
        self._update_progress()
        self._update_buttons()
        # 连着答对够了就会移出错题集，按钮上的数字要跟着变
        self._update_wrong_button()

    def _set_feedback(self, question: TempQuestion, user_answer: str, is_correct: bool) -> None:
        color = self._theme.ok if is_correct else self._theme.danger
        head = "答对了" if is_correct else "答错了"
        parts = [f"<div style='color:{color};font-weight:bold;'>{head}</div>"]
        if not is_correct:
            parts.append(f"<div>你选的是：{html_text(user_answer)}</div>")
        parts.append(f"<div>正确答案：<b>{html_text(question.answer_display())}</b></div>")
        if question.explanation:
            parts.append(
                f"<div style='color:{self._theme.muted};'>解析：{html_text(question.explanation)}</div>"
            )
        if question.knowledge:
            parts.append(
                f"<div style='color:{self._theme.muted};'>知识点：{html_text(question.knowledge)}</div>"
            )
        self.feedback.setVisible(True)
        self.feedback.setHtml("".join(parts))

    def _next(self) -> None:
        if self._index + 1 >= len(self._items):
            total = len(self._items)
            if self._wrong_mode:
                self._show_placeholder(
                    f"这批错题 {total} 道做完了：对 {self._right} 道、错 {self._wrong} 道。"
                    f" 连着答对 {self._wrong_master_after()} 次的题已经移出错题集；"
                    " 想再来一批点「刷错题集」，想刷正常那份点「开始刷题」。"
                )
            else:
                self._show_placeholder(
                    f"这一批 {total} 道做完了：对 {self._right} 道、错 {self._wrong} 道。"
                    " 觉得没记牢，点「重刷这批」把这批再刷一遍；"
                    "想接着往下刷，再点一次「开始刷题」。"
                )
            self._set_status(f"这批做完了：对 {self._right} 错 {self._wrong}")
            self._update_wrong_button()
            return
        self._index += 1
        self._show_question()

    def _update_progress(self) -> None:
        total = len(self._items)
        if not total:
            self.progress_label.setText("")
            self.score_label.setText("")
            return
        if self._wrong_mode:
            # 错题集是单独一份，没有"整份第几道"这回事
            text = f"错题第 {self._index + 1}/{total} 道"
        else:
            text = f"第 {self._index + 1}/{total} 道"
            if self._offset:
                # 一轮只有 20 道，光看"第 3/20 道"不知道整份刷到哪了
                text += (
                    f"（整份第 {self._offset + 1}–{self._offset + total} 道，"
                    f"共 {self._grand} 道）"
                )
        self.progress_label.setText(text)
        self.score_label.setText(f"本轮 对 {self._right} 错 {self._wrong}")

    def _idle_placeholder(self) -> str:
        """还没开始刷题时，中间那块该说什么。

        **库里有题却让人"先导入"会把人吓一跳**（打好的包里真机截图踩到过：
        头上写着"这栏共 279 道"，中间却叫他去导题库）。所以按库里有没有题分两种说法。
        """
        if sum(self.bank.count_by_type().values()):
            return "点上面的「开始刷题」就能开始：一轮 20 道，做完再点一次接着往下刷。"
        return "先点上面的「导入文件」或「导入文件夹」，把这门考试的题库放进来。"

    def _show_placeholder(self, text: str) -> None:
        self._clear_options()
        self.meta_label.clear()
        self.feedback.setVisible(True)
        self.feedback.setHtml(f"<div style='color:{self._theme.muted};'>{escape(text)}</div>")
        # 没有题的时候把题干框收起来：一个 90px 高的空框看着像没加载出来，
        # 而真正该看的是下面这句提示。
        self.question_view.clear()
        self.question_view.setVisible(False)
        self.answer_edit.clear()
        self.answer_edit.setVisible(False)
        self.submit_button.setVisible(False)
        self.known_button.setVisible(False)
        self.unknown_button.setVisible(False)
        self.next_button.setEnabled(False)
        self.progress_label.setText("")
        self.score_label.setText("")

    def _set_status(self, text: str) -> None:
        window = self.window()
        if hasattr(window, "status_label"):
            window.status_label.setText(text)

    # ---------- 关窗口 ----------

    def shutdown(self) -> None:
        """窗口关掉时别让后台线程还在跑——QThread 边跑边被销毁会直接崩。"""
        worker = self._worker
        if worker is not None and worker.isRunning():
            if not worker.wait(5000):
                log.warning("临时栏的导入线程没在 5 秒内退出")
                worker.terminate()
                worker.wait(2000)
