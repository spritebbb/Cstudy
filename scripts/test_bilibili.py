"""单独验证 B 站链路：登录态 → 视频信息 → 字幕。

不调用 AI，也不下载音频，所以跑得很快、不花钱。
用来把"B 站这一环"和"AI 那一环"分开排查。

    python scripts/test_bilibili.py
    python scripts/test_bilibili.py "https://www.bilibili.com/video/BVxxxx?p=1"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.bilibili import (  # noqa: E402
    BilibiliError,
    credential_path,
    fetch_subtitle,
    fetch_video_info,
    load_credential,
    parse_video_ref,
)
from cstudy.config import get_config  # noqa: E402
from cstudy.logging_setup import setup_logging  # noqa: E402


def main() -> int:
    config = get_config()
    setup_logging(config.root / str(config.get("logging.file", "logs/cstudy.log")))

    print("\n===== B 站链路自检 =====\n")

    # ---- 1. 登录态 ----
    path = credential_path(config)
    credential = load_credential(path)
    if credential:
        print(f"  [OK]   已登录（登录态文件：{path}）")
    else:
        print("  [未登录] 没有可用的登录态。")
        print("          双击 扫码登录.bat 扫一次码，然后再跑这个脚本。")
        print("          没有登录态时，只有公开视频能拿到信息，CC 字幕基本拿不到。\n")

    # ---- 2. 要测哪个视频 ----
    link = " ".join(sys.argv[1:]).strip()
    if not link:
        try:
            link = input("  粘贴一个 B 站视频链接或 BV 号，回车确认：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  没输入链接，退出。")
            return 1
    if not link:
        print("\n  没输入链接，退出。")
        return 1

    try:
        bvid, page = parse_video_ref(link)
    except BilibiliError as exc:
        print(f"\n  链接解析失败：{exc}\n")
        return 1
    print(f"  [OK]   解析出 {bvid} 第 {page} P\n")

    # ---- 3. 视频信息 ----
    try:
        info = fetch_video_info(bvid, page, credential=credential, config=config)
    except BilibiliError as exc:
        print(f"  [失败] 拿视频信息：{exc}\n")
        return 1
    print(f"  [OK]   视频信息：《{info.display_title}》")
    print(f"         时长 {info.duration // 60} 分 {info.duration % 60} 秒，cid={info.cid}\n")

    # ---- 4. 字幕 ----
    if not credential:
        print("  [跳过] 没登录，不去试 CC 字幕（试了也基本是失败）\n")
        print("B 站这一环到此为止：能拿到公开视频的信息，但拿不到字幕。\n")
        return 0

    print("  正在查 CC 字幕…")
    try:
        segments = fetch_subtitle(info, credential=credential, config=config)
    except BilibiliError as exc:
        print(f"  [失败] 查字幕：{exc}\n")
        print("这一环有问题，把上面这句和 logs/cstudy.log 的最后 50 行发出来。\n")
        return 1

    if not segments:
        print("  [无字幕] 这个视频本身没有 CC 字幕（不是报错）。")
        print("          导入时会自动改用本地语音转写，这属于正常情况。\n")
        return 0

    total_chars = sum(len(s.text) for s in segments)
    print(f"  [OK]   拿到 {len(segments)} 条字幕，共 {total_chars} 字")
    print(f"         时间跨度 {segments[-1].timestamp}\n")

    print("----- 前 8 条预览 -----")
    for seg in segments[:8]:
        print(f"  [{seg.timestamp}] {seg.text}")
    print("\n----- 最后 3 条预览 -----")
    for seg in segments[-3:]:
        print(f"  [{seg.timestamp}] {seg.text}")

    print("\nB 站链路正常：登录态、视频信息、CC 字幕都能拿到。\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(130)
