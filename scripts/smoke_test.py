"""环境自检。哪里出问题，这个脚本会直接告诉你。

    python scripts/smoke_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.config import ConfigError, get_config  # noqa: E402
from cstudy.db import Database  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402
from cstudy.tools import find_ffmpeg, find_yt_dlp  # noqa: E402

OK = "  [OK]   "
FAIL = "  [失败] "


def check(name: str, func) -> bool:
    try:
        detail = func()
    except Exception as exc:
        print(f"{FAIL}{name}")
        print(f"         {type(exc).__name__}: {exc}")
        return False
    print(f"{OK}{name}" + (f"  {detail}" if detail else ""))
    return True


def main() -> int:
    print("\n===== C语言备考助手 环境自检 =====\n")
    results: list[bool] = []

    # 1. 配置文件
    config_holder: dict = {}

    def load():
        cfg = get_config()
        config_holder["config"] = cfg
        return f"项目目录 {cfg.root}"

    results.append(check("读取 config.yaml", load))
    if "config" not in config_holder:
        print("\n配置文件读不了，后面的检查没法继续。")
        return 1
    config = config_holder["config"]

    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))

    # 2. 数据库
    def check_db():
        db = Database(config.path("database"))
        db.init_schema()
        stats = db.stats()
        return "  ".join(f"{k} {v}" for k, v in stats.items())

    results.append(check("打开数据库", check_db))

    # 3. 考纲数据
    def check_syllabus():
        db = Database(config.path("database"))
        count = db.count("syllabus_points")
        if count == 0:
            raise RuntimeError("考纲是空的，请运行 python scripts/seed_syllabus.py")
        return f"{count} 个知识点"

    results.append(check("考纲知识点", check_syllabus))

    # 4. 密钥
    def check_key():
        key = config.api_key
        return f"{key[:6]}…（共 {len(key)} 字符）"

    results.append(check("DeepSeek 密钥已配置", check_key))

    # 5. DeepSeek 连通性
    def check_ai():
        from cstudy.ai import DeepSeekClient

        client = DeepSeekClient(config)
        reply = client.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            temperature=0,
        )
        return f"模型 {config.ai_model} 回复「{reply.strip()[:20]}」"

    results.append(check("连接 DeepSeek", check_ai))

    # 6. 外部工具
    results.append(check("找到 ffmpeg", lambda: find_ffmpeg() or _missing("winget install Gyan.FFmpeg")))
    results.append(check("找到 yt-dlp", lambda: find_yt_dlp() or _missing("winget install yt-dlp.yt-dlp")))

    # 7. whisper
    def check_whisper():
        import ctranslate2

        from faster_whisper import WhisperModel  # noqa: F401

        gpus = ctranslate2.get_cuda_device_count()
        return f"ctranslate2 {ctranslate2.__version__}，检测到 {gpus} 块可用显卡"

    results.append(check("本地转写组件", check_whisper))

    # 8. B 站登录态
    def check_login():
        from cstudy.bilibili import credential_path, load_credential

        credential = load_credential(credential_path(config))
        if not credential:
            raise RuntimeError("未登录。运行 python scripts/login_bilibili.py 扫码登录")
        return "已登录"

    results.append(check("B 站登录态", check_login))

    # 9. 界面
    def check_ui():
        from PyQt6.QtWidgets import QApplication

        from cstudy.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow(config, Database(config.path("database")))
        window.close()
        del app
        return "主窗口可以创建"

    results.append(check("PyQt6 界面", check_ui))

    passed = sum(results)
    total = len(results)
    print(f"\n===== {passed}/{total} 项通过 =====\n")

    if passed < total:
        print("带 [失败] 的项就是问题所在。修好后再跑一次这个脚本。\n")
        return 1

    print("环境没问题，可以运行 python run.py 启动软件了。\n")
    return 0


def _missing(hint: str) -> str:
    raise RuntimeError(f"没找到。安装命令：{hint}")


if __name__ == "__main__":
    sys.exit(main())
