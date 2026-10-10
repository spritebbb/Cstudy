"""B 站扫码登录（命令行版）。

会把二维码存成 PNG 并自动用看图软件打开，用 B 站手机 App 扫它。
登录成功后登录态存到 data/cookies/bilibili.json，之后不用再登。

    python scripts/login_bilibili.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.bilibili.auth import BilibiliAuthError, login_and_save  # noqa: E402
from cstudy.config import get_config  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    config = get_config()
    setup_logging(config.root / "logs/cstudy.log", level="INFO")

    qr_path = config.path("cookies").parent / "qrcode.png"

    def show_qrcode(png: bytes) -> None:
        qr_path.parent.mkdir(parents=True, exist_ok=True)
        qr_path.write_bytes(png)
        print(f"二维码已保存到：{qr_path}")
        try:
            os.startfile(qr_path)  # 自动打开看图软件
        except OSError:
            print("（自动打开失败，请手动打开上面这个文件）")

    try:
        login_and_save(
            config,
            on_qrcode=show_qrcode,
            on_state=lambda text: print(f"  {text}"),
        )
    except BilibiliAuthError as exc:
        print(f"\n登录失败：{exc}", file=sys.stderr)
        return 1

    print("\n登录成功！登录态已保存，之后导入视频会自动使用。")
    print("（二维码图片可以删掉了）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
