"""扫码登录的重试逻辑测试。

B 站偶尔会在 TLS 握手阶段直接掐断连接（实测约 1/5 的概率），
报错是 curl 的 "SSL_connect: Connection closed abruptly"。
一次失败就放弃太脆，所以 auth 层要自己重试。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cstudy.bilibili import auth as auth_module  # noqa: E402
from cstudy.bilibili.auth import BilibiliAuthError, QrCodeSession  # noqa: E402

FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes"


class FakePicture:
    def __init__(self, content: bytes) -> None:
        self.content = content


class FakeQrLogin:
    """可配置成"前 N 次失败，之后成功"的假登录会话。"""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.generate_calls = 0
        self.poll_calls = 0

    async def generate_qrcode(self):
        self.generate_calls += 1
        if self.generate_calls <= self.fail_times:
            raise RuntimeError(
                "Failed to perform, curl: (35) BoringSSL SSL_connect: "
                "Connection closed abruptly (SSL_ERROR_SYSCALL; error queue empty)"
            )

    async def check_state(self):
        self.poll_calls += 1
        if self.poll_calls <= self.fail_times:
            raise RuntimeError("SSL_ERROR_SYSCALL")
        return auth_module.login_v2.QrCodeLoginEvents.SCAN

    def get_qrcode_picture(self):
        return FakePicture(FAKE_PNG)

    def get_credential(self):
        return None


def make_session(monkeypatch, fail_times: int) -> tuple[QrCodeSession, FakeQrLogin]:
    fake = FakeQrLogin(fail_times=fail_times)
    monkeypatch.setattr(auth_module.login_v2, "QrCodeLogin", lambda **kwargs: fake)
    monkeypatch.setattr(auth_module.time, "sleep", lambda _: None)  # 别真等
    return QrCodeSession(), fake


class TestQrCodeRetry:
    def test_first_try_success(self, monkeypatch):
        session, fake = make_session(monkeypatch, fail_times=0)
        assert session.start() == FAKE_PNG
        assert fake.generate_calls == 1  # 一次就成，不该有多余请求

    def test_retries_through_transient_failures(self, monkeypatch):
        # 前两次 TLS 被掐断，第三次成功——这正是实测到的抖动
        session, fake = make_session(monkeypatch, fail_times=2)
        assert session.start() == FAKE_PNG
        assert fake.generate_calls == 3

    def test_gives_up_with_human_readable_message(self, monkeypatch):
        session, fake = make_session(monkeypatch, fail_times=99)
        with pytest.raises(BilibiliAuthError) as exc:
            session.start()

        assert fake.generate_calls == auth_module.RETRY_ATTEMPTS
        message = str(exc.value)
        assert "连着试了 3 次" in message
        assert "不是你的账号问题" in message
        assert "扫码登录.bat" in message  # 给出下一步该点哪里

    def test_poll_also_retries(self, monkeypatch):
        session, fake = make_session(monkeypatch, fail_times=0)
        session.start()
        fake.fail_times = 2  # 让接下来的轮询抖两下
        fake.poll_calls = 0
        assert session.poll() == auth_module.STATE_WAITING
        assert fake.poll_calls == 3

    def test_poll_before_start_is_rejected(self, monkeypatch):
        session, _ = make_session(monkeypatch, fail_times=0)
        with pytest.raises(BilibiliAuthError) as exc:
            session.poll()
        assert "先调用 start()" in str(exc.value)
