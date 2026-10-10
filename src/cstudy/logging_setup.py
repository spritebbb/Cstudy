"""日志：同时写文件和控制台。文件会滚动，不会无限增长。"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_configured = False

FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def fix_windows_console() -> None:
    """让 Windows 控制台能正常显示中文。

    默认代码页是 GBK，打印中文会变成乱码。
    这里把标准输出切成 UTF-8，并把控制台代码页设成 65001。
    改不了也无所谓，不影响功能。重复调用是安全的。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


# 任何入口脚本都会 import 本模块，所以在导入时就修好，
# 免得脚本里第一句 print 还是乱码。
fix_windows_console()


def setup_logging(
    log_file: Path,
    level: str = "INFO",
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """初始化根 logger。重复调用只生效一次。"""
    global _configured
    if _configured:
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(FORMAT, datefmt=DATE_FORMAT)

    # 文件：详细，含 DEBUG
    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 控制台：只显示配置的级别
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(formatter)
    root.addHandler(console)

    # 第三方库的噪音压掉
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
