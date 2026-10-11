"""启动入口。

直接运行：python run.py
也可以双击项目根目录的「启动.bat」。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
# 让 cstudy 包可以被导入
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from cstudy.config import ConfigError, get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import get_logger, setup_logging  # noqa: E402
from cstudy.tools import describe_missing_tools  # noqa: E402


def _fatal(title: str, message: str) -> None:
    """启动阶段的致命错误：先打印，再尽量弹个窗。"""
    print(f"\n{title}\n{message}\n", file=sys.stderr)
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, title, message)
    except Exception:
        pass  # 弹窗失败就算了，控制台已经打印了


def main() -> int:
    try:
        config = get_config()
    except ConfigError as exc:
        _fatal("配置有问题", str(exc))
        return 1

    setup_logging(
        log_file=config.root / str(config.get("logging.file", "logs/cstudy.log")),
        level=str(config.get("logging.level", "INFO")),
        max_bytes=int(config.get("logging.max_bytes", 5 * 1024 * 1024)),
        backup_count=int(config.get("logging.backup_count", 3)),
    )
    log = get_logger("run")
    log.info("=" * 60)
    log.info("启动 C语言备考助手")
    log.info("项目目录：%s", config.root)

    db = Database(config.path("database"))
    try:
        db.init_schema()
    except Exception as exc:
        _fatal("数据库初始化失败", f"{exc}\n\n数据库路径：{config.path('database')}")
        return 1

    # 顺手备份一份。库里唯一不可再生的是用户的作答记录和错题本，
    # 题目丢了能重导，学习轨迹丢了就真没了。一天备一次，失败也不拦着用。
    try:
        db.backup()
    except Exception:
        log.warning("数据库备份出意外，跳过", exc_info=True)

    missing = describe_missing_tools()
    if missing:
        log.warning("缺少外部工具：%s（只有需要语音转写时才会用到）", "、".join(missing))

    # 界面相关的 import 放在后面，这样配置出错时不用先加载 Qt
    from PyQt6.QtGui import QFont
    from PyQt6.QtWidgets import QApplication

    from cstudy.ui import rendering
    from cstudy.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(str(config.get("app.name", "C语言备考助手")))
    _apply_fonts(app, config, QFont, rendering)

    window = MainWindow(config, db)
    window.show()

    log.info("界面已启动")
    return app.exec()


def _apply_fonts(app, config, QFont, rendering) -> None:
    """按 config.yaml 的 `ui:` 段设置字体。

    **不设的话 Qt 会给一套很小很淡的默认字体**，中文还发虚，看久了伤眼。
    配置写坏了就退回默认值——字体不该是跑不起来的原因。
    """
    family = str(config.get("ui.font_family", "") or "Microsoft YaHei")
    try:
        size = max(8, int(config.get("ui.font_size", 11)))
    except (TypeError, ValueError):
        size = 11
    app.setFont(QFont(family, size))

    rendering.configure(
        mono_family=str(config.get("ui.mono_family", "") or "Consolas"),
        mono_size=config.get("ui.mono_size", 12),
    )


if __name__ == "__main__":
    sys.exit(main())
