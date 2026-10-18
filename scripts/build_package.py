"""把「C语言备考助手」打包成一个**免安装**的文件夹（双击 exe 就能用）。

用户的原话是"帮我打包一份"，并选了两件事：
- 形式＝**免安装文件夹**（PyInstaller onedir，目标电脑不用装 Python）
- 数据＝**只带题库，不带作答记录**

用法::

    .venv\\Scripts\\python.exe scripts\\build_package.py          只做文件夹
    .venv\\Scripts\\python.exe scripts\\build_package.py --zip    做完再压一个 zip

或者双击项目根目录的「打包软件.bat」。

产出在 ``dist\\C语言备考助手\\``（整份拷到别的电脑就能跑）。
打完会**自动启动一次成品**验证它真能打开（窗口会闪一下，几秒后自动关掉），
然后把这次验证产生的日志、空主库、备份删干净——交付出去的包是干净的。

**故意不放进包里的东西**（会写进包内的「使用说明.txt」）：

- ``.env``：里面是 DeepSeek 密钥，不能跟着包到处跑；要 AI 功能自己拷过去。
- ``logs/``、``notes/``、``data/cstudy.db``、``data/backups/``：都是自己的学习记录。
- ``data/temp_bank/bank.db`` 会带，但**先删掉作答记录和进度**（用户要的是"只带题库"）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# PyInstaller 的中间名字用 ASCII，最后再把文件夹和 exe 改成中文名，
# 免得构建过程中因为非 ASCII 名字出幺蛾子。
APP_NAME = "Cstudy"
PACKAGE_NAME = "C语言备考助手"

# 这些包要么带二进制（pdfium / cacert / DLL），要么靠动态 import 找子模块，
# PyInstaller 的自动分析盯不住——干脆整包收进来。宁可大一点，也别运行时报缺模块。
COLLECT_ALL = (
    "pypdfium2",
    "curl_cffi",
    "bilibili_api",
    "faster_whisper",
    "ctranslate2",
    "av",
    "onnxruntime",
    "tokenizers",
    "huggingface_hub",
    "qrcode",
    "yt_dlp",
)


def _human(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def build() -> Path:
    """跑 PyInstaller，返回 PyInstaller 自己的输出目录。"""
    try:
        from PyInstaller.__main__ import run as pyinstaller_run
    except ImportError:
        print("没装 PyInstaller。先跑：.venv\\Scripts\\python.exe -m pip install pyinstaller")
        raise SystemExit(1)

    for stale in (DIST / APP_NAME, BUILD / APP_NAME):
        if stale.exists():
            shutil.rmtree(stale, ignore_errors=True)

    args = [
        str(ROOT / "run.py"),
        "--name",
        APP_NAME,
        "--noconfirm",
        "--clean",
        "--windowed",                       # 不弹黑窗口（用户看的是图形界面）
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        # src 必须显式告诉 PyInstaller：run.py 是在**运行时**才
        # sys.path.insert(0, PROJECT_ROOT/"src") 的，PyInstaller 只做静态分析，
        # 看不到那一行。不加这个参数，打出来的包双击没反应——日志里是
        # ModuleNotFoundError: No module named 'cstudy.config'（实测踩过）。
        "--paths",
        str(SRC),
        # schema.sql 是运行时用 Path(__file__).parent / "schema.sql" 读的，
        # 不放进来的话建库那一步就炸。
        "--add-data",
        f"{SRC / 'cstudy' / 'schema.sql'}{os.pathsep}cstudy",
    ]
    for package in COLLECT_ALL:
        args += ["--collect-all", package]

    print("开始打包（这一步慢，几分钟很正常）……")
    pyinstaller_run(args)
    return DIST / APP_NAME


def make_bank(source: Path, target: Path) -> tuple[int, str]:
    """把题库复制过去，并**删掉作答记录和进度**（只留题目）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    conn = sqlite3.connect(target)
    try:
        conn.execute("DELETE FROM temp_answers")
        conn.execute("DELETE FROM temp_meta")
        conn.commit()
        conn.execute("VACUUM")
        rows = conn.execute(
            "SELECT qtype, COUNT(*) FROM temp_questions GROUP BY qtype"
        ).fetchall()
    finally:
        conn.close()
    labels = {"single": "单选题", "multi": "多选题", "judge": "判断题", "short": "问答题"}
    detail = "、".join(f"{labels.get(t, t)} {n}" for t, n in rows)
    return sum(n for _, n in rows), detail


README = """C语言备考助手 —— 免安装版
================================

怎么用
------
1. 把**整个**「{pkg}」文件夹拷到目标电脑（放桌面也行）——
   别只拷 exe，程序还需要旁边的 _internal 文件夹。
2. 双击「{pkg}.exe」。
3. 第一次打开会自己建 data、logs 这些文件夹，慢几秒是正常的。

这一份里有什么
--------------
- 完整的软件，**目标电脑不需要装 Python**。
- 「考前突击」那一栏的题库：{bank_detail}，共 {bank_total} 道，
  放在 data\\temp_bank\\bank.db。（按你的选择：**只带题目，没带做题记录和进度**）
- config.yaml：所有配置都在这里，记事本能改（字体、主题、每轮几道、
  错题门槛、选项是否打乱等）。

没有放进来的东西
----------------
- `.env`（DeepSeek 密钥）：生成笔记 / 出题 / 导入题库这些 AI 功能要用它。
  想用就把原来项目根目录的 .env 拷进这个文件夹。
- 原来的 C 语言主题库（data\\cstudy.db）、笔记、日志、备份——
  那些是你的学习记录，没跟着包走。
- 考纲、知识图谱、错题本里的内容。这一份主要是给你考前刷「考前突击」用的。

出问题怎么办
------------
- 双击没反应：打开这个文件夹里的 logs\\cstudy.log，看最后几行写到哪里停的。
- 想清空「考前突击」的题库：关掉软件，删 data\\temp_bank\\bank.db 就行
  （下次打开是空的，重新点「导入文件」再导一遍）。
- 这个文件夹可以整个删掉，不会影响原项目。
"""


def assemble(app_dir: Path) -> Path:
    """把 exe 目录改成交付样子：中文名 + 配置 + 题库 + 使用说明。"""
    final = DIST / PACKAGE_NAME
    if final.exists():
        shutil.rmtree(final)
    app_dir.rename(final)

    exe_from = final / f"{APP_NAME}.exe"
    exe_to = final / f"{PACKAGE_NAME}.exe"
    if exe_from.exists() and not exe_to.exists():
        exe_from.rename(exe_to)

    shutil.copy2(ROOT / "config.yaml", final / "config.yaml")
    if (ROOT / ".env.example").exists():
        shutil.copy2(ROOT / ".env.example", final / ".env.example")

    bank_source = ROOT / "data" / "temp_bank" / "bank.db"
    if bank_source.exists():
        total, detail = make_bank(bank_source, final / "data" / "temp_bank" / "bank.db")
    else:
        total, detail = 0, "（原项目里还没有这栏题库）"
        print("提醒：原项目里没有 data/temp_bank/bank.db，包里不带题库。")

    (final / "使用说明.txt").write_text(
        README.format(pkg=PACKAGE_NAME, bank_total=total, bank_detail=detail),
        encoding="utf-8",
    )
    return final


VERIFY_MARK = "界面已启动"
VERIFY_TIMEOUT = 120


def verify(package_dir: Path, timeout: float = VERIFY_TIMEOUT) -> tuple[bool, str]:
    """把成品启动一次，确认它真能把界面拉起来。

    **为什么非要真启动一次**：`--windowed` 的 exe 不带控制台，启动阶段
    挂了就是"双击没反应"，什么提示都没有（实测踩过：漏了 `--paths src`，
    日志里才写着 ModuleNotFoundError）。所以这里等包内日志出现
    "界面已启动"；等不到就把日志尾巴交出来，别让用户对着一个哑包猜。
    """
    exe = package_dir / f"{PACKAGE_NAME}.exe"
    log_file = package_dir / "logs" / "cstudy.log"
    if log_file.exists():
        log_file.unlink()

    proc = subprocess.Popen([str(exe)], cwd=str(package_dir))
    started = False
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if log_file.exists():
                text = log_file.read_text(encoding="utf-8", errors="replace")
                if VERIFY_MARK in text:
                    started = True
                    break
            if proc.poll() is not None:      # 进程自己退了：不用再等
                break
            time.sleep(1)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:  # 弹了错误框、点了不理它
                proc.kill()

    if started:
        return True, f"界面起来了（日志里出现「{VERIFY_MARK}」）"

    tail = ""
    if log_file.exists():
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-15:])
    return False, tail or "日志都没建起来，说明启动阶段就挂了"


def clean_after_verify(package_dir: Path) -> list[str]:
    """删掉验证时产生的痕迹——交付出去的包要干净。

    只删验证会生成的那几样（logs / notes / 空主库 / 备份）。
    **题库 data/temp_bank 绝不动**；每个路径都先确认在包目录里面再删。
    """
    removed: list[str] = []
    keep = package_dir.resolve()
    for rel in ("logs", "notes", "data" + os.sep + "cstudy.db", "data" + os.sep + "backups"):
        target = (package_dir / rel).resolve()
        if target == keep or keep not in target.parents:   # 保险：不出包目录
            continue
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(rel)
        elif target.exists():
            target.unlink()
            removed.append(rel)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="打包成免安装文件夹")
    parser.add_argument("--zip", action="store_true", help="最后再压一个 zip")
    parser.add_argument(
        "--no-verify", action="store_true", help="不启动成品验证（默认会启动一次）"
    )
    opts = parser.parse_args()

    app_dir = build()
    final = assemble(app_dir)

    if not opts.no_verify:
        print("启动一次成品，确认它真能打开……")
        ok, message = verify(final)
        if ok:
            print(f"验证通过：{message}")
        else:
            print()
            print("!" * 56)
            print("验证没过：这个包现在双击是打不开的。")
            print(message)
            print("!" * 56)
        clean_after_verify(final)

    size = _human(_dir_size(final))
    print()
    print("=" * 56)
    print(f"打包完成：{final}")
    print(f"体积：{size}（整个文件夹拷到别的电脑就能用）")
    print(f"主程序：{final / (PACKAGE_NAME + '.exe')}")
    print("=" * 56)

    if opts.zip:
        print("正在压缩……（这一份不小，要等一会儿）")
        archive = shutil.make_archive(
            str(DIST / PACKAGE_NAME), "zip", root_dir=DIST, base_dir=PACKAGE_NAME
        )
        print(f"压缩包：{archive}（{_human(Path(archive).stat().st_size)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
