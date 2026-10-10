"""B 站扫码登录。

登录态（cookies）缓存在 data/cookies/bilibili.json，已 gitignore，不会进仓库。

注意：QrCodeSession 内部持有一个 asyncio 事件循环，
必须在**同一个线程**里创建和使用。UI 里要放到后台线程跑。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

from bilibili_api import Credential, login_v2

from ..config import Config
from ..logging_setup import get_logger

log = get_logger(__name__)

# B 站偶尔会在 TLS 握手阶段直接把连接掐掉（实测约 1/5 的概率），
# 报错长这样：curl: (35) BoringSSL SSL_connect: Connection closed abruptly。
# 这不是账号或配置问题，重试一下通常就好，所以所有 B 站调用都过这层重试。
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0

# 需要保存的登录凭据字段
CREDENTIAL_FIELDS = (
    "sessdata",
    "bili_jct",
    "buvid3",
    "buvid4",
    "dedeuserid",
    "ac_time_value",
)

# 轮询状态
STATE_WAITING = "waiting"    # 还没扫
STATE_SCANNED = "scanned"    # 已扫，待确认
STATE_DONE = "done"          # 确认完成
STATE_TIMEOUT = "timeout"    # 二维码过期


class BilibiliAuthError(RuntimeError):
    """登录相关失败。消息显示给用户。"""


def credential_path(config: Config) -> Path:
    return config.path("cookies")


def save_credential(credential: Credential, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {name: (getattr(credential, name, "") or "") for name in CREDENTIAL_FIELDS}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("登录态已保存到 %s", path)


def load_credential(path: Path) -> Credential | None:
    """读取缓存的登录态。没有或失效返回 None。"""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("登录态文件读不出来（%s），当作未登录处理", exc)
        return None

    credential = Credential(**{name: data.get(name, "") for name in CREDENTIAL_FIELDS})
    if not credential.has_sessdata():
        log.info("缓存的登录态已失效")
        return None
    log.info("已加载缓存登录态")
    return credential


class QrCodeSession:
    """一次扫码登录会话。同一个线程里创建、使用、关闭。"""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        self._started = False

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _run_retrying(self, coro_factory, what: str):
        """跑一个协程，失败自动重试。

        coro_factory 是「调用后返回新协程」的可调用对象——每次重试都要新的协程，
        不能复用同一个。
        """
        last: Exception | None = None
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return self._run(coro_factory())
            except Exception as exc:
                last = exc
                if attempt < RETRY_ATTEMPTS:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    log.warning(
                        "%s 第 %d 次失败（%s），%.0f 秒后重试",
                        what, attempt, type(exc).__name__, wait,
                    )
                    time.sleep(wait)

        raise BilibiliAuthError(
            f"{what}失败，连着试了 {RETRY_ATTEMPTS} 次都没成功。\n"
            "这多半是网络不稳（B 站偶尔会直接掐断连接），不是你的账号问题。\n"
            "检查一下网络，然后重新双击 扫码登录.bat 就行。\n"
            f"（技术细节，看不懂可以发给我：{last}）"
        ) from last

    def start(self) -> bytes:
        """生成二维码，返回 PNG 图片字节。"""
        self._run_retrying(self._qr.generate_qrcode, "获取登录二维码")
        self._started = True
        return self._qr_image()

    def _qr_image(self) -> bytes:
        picture = self._qr.get_qrcode_picture()
        content = getattr(picture, "content", picture)
        if isinstance(content, str):
            # 某些版本返回 base64 字符串
            if "," in content and content.strip().startswith("data:"):
                content = content.split(",", 1)[1]
            try:
                return base64.b64decode(content)
            except (ValueError, TypeError) as exc:
                raise BilibiliAuthError(f"二维码图片解码失败：{exc}") from exc
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        raise BilibiliAuthError(f"看不懂的二维码格式：{type(content)}")

    def poll(self) -> str:
        """查询扫码状态。"""
        if not self._started:
            raise BilibiliAuthError("还没生成二维码，先调用 start()")
        events = login_v2.QrCodeLoginEvents
        state = self._run_retrying(self._qr.check_state, "查询扫码状态")

        if state == events.DONE:
            return STATE_DONE
        if state == events.CONF:
            return STATE_SCANNED
        if state == events.TIMEOUT:
            return STATE_TIMEOUT
        return STATE_WAITING

    def credential(self) -> Credential:
        return self._qr.get_credential()

    def close(self) -> None:
        try:
            self._loop.close()
        except Exception:  # 关闭失败不影响主流程
            log.debug("事件循环关闭时出错", exc_info=True)


def login_and_save(
    config: Config,
    on_state=None,
    on_qrcode=None,
    timeout_seconds: int = 180,
) -> Credential:
    """命令行用的扫码登录：生成二维码、轮询到成功为止，成功后保存登录态。

    on_qrcode 收到 PNG 字节，由调用方决定怎么展示（存文件、打印路径等）。
    on_state 收到状态文字，用于终端提示。
    """
    session = QrCodeSession()
    try:
        png = session.start()
        if on_qrcode:
            on_qrcode(png)
        if on_state:
            on_state("请用 B 站 App 扫描二维码")
        deadline = time.time() + timeout_seconds
        last = ""
        while time.time() < deadline:
            state = session.poll()
            if state != last and on_state:
                on_state(
                    {"scanned": "已扫描，请在手机上确认", "waiting": "等待扫描…"}.get(state, state)
                )
                last = state
            if state == STATE_DONE:
                credential = session.credential()
                save_credential(credential, credential_path(config))
                return credential
            if state == STATE_TIMEOUT:
                raise BilibiliAuthError("二维码已过期，请重新运行登录脚本。")
            time.sleep(2)
        raise BilibiliAuthError("等待扫码超时，请重新运行登录脚本。")
    finally:
        session.close()
