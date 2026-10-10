"""扫码登录的重试逻辑测试。

B 站偶尔会在 TLS 握手阶段直接掐断连接（实测约 1/5 的概率），
报错是 curl 的 "SSL_connect: Connection closed abruptly"。
一次失败就放弃太脆，所以 auth 层要自己重试。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
        self.credential = None

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
        return self.credential


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


class TestCredentialFromSessionCookies:
    """B 站改用 Set-Cookie 下发登录 cookie，库从 URL 里抠的那套已经失效。

    实测：crossDomain 的 URL 现在只剩 ticket / gourl / first_domain，
    库抠出来六个空字符串，而且不报错——脚本照样打印"登录成功"，
    直到导入视频时才发现登录态是空的。
    """

    def _session(self, monkeypatch, jar, library_credential):
        session, fake = make_session(monkeypatch, fail_times=0)
        fake.credential = library_credential
        monkeypatch.setattr(
            auth_module, "get_session", lambda: SimpleNamespace(cookies=jar)
        )
        return session

    def test_reads_cookies_from_session_jar(self, monkeypatch):
        jar = {"SESSDATA": "s" * 222, "bili_jct": "j" * 32, "DedeUserID": "12345678"}
        library = SimpleNamespace(sessdata="", ac_time_value="t" * 32)
        session = self._session(monkeypatch, jar, library)

        credential = session.credential()
        assert credential.sessdata == "s" * 222
        assert credential.bili_jct == "j" * 32
        assert credential.dedeuserid == "12345678"
        # ac_time_value 来自响应里的 refresh_token，库那边是好的
        assert credential.ac_time_value == "t" * 32

    def test_falls_back_to_library_if_jar_empty(self, monkeypatch):
        """万一 B 站哪天又改回在 URL 里带 cookie。"""
        library = SimpleNamespace(sessdata="from-url", ac_time_value="")
        session = self._session(monkeypatch, {}, library)

        assert session.credential().sessdata == "from-url"

    def test_raises_clearly_when_nothing_available(self, monkeypatch):
        library = SimpleNamespace(sessdata="", ac_time_value="")
        session = self._session(monkeypatch, {}, library)

        with pytest.raises(BilibiliAuthError) as exc:
            session.credential()
        assert "没拿到登录凭据" in str(exc.value)
        assert "cstudy.log" in str(exc.value)  # 告诉用户该发什么给我

    def test_missing_ac_time_value_does_not_break_login(self, monkeypatch):
        """拿不到 ac_time_value 只影响自动续期，不该让登录失败。"""
        jar = {"SESSDATA": "s" * 222}

        def boom():
            raise RuntimeError("库内部状态没设置")

        session, fake = make_session(monkeypatch, fail_times=0)
        fake.get_credential = boom
        monkeypatch.setattr(
            auth_module, "get_session", lambda: SimpleNamespace(cookies=jar)
        )

        credential = session.credential()
        assert credential.sessdata == "s" * 222
        assert credential.ac_time_value in (None, "")


class TestYtDlpCookieFile:
    """把登录态转成 yt-dlp 认识的 cookie 文件。

    yt-dlp 用的是 Netscape 格式，而项目里的登录态是 bilibili-api 的 JSON，
    两边格式不一样，得转换——**而且 cookie 名的大小写必须对**
    （B 站认 `SESSDATA`，不认 `sessdata`）。
    """

    @pytest.fixture
    def config(self, tmp_path: Path):
        from cstudy.config import Config

        return Config({"paths": {"cookies": str(tmp_path / "cookies" / "bilibili.json")}}, tmp_path)

    def write_login(self, config, **overrides):
        import json

        path = auth_module.credential_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "sessdata": "假的-sessdata-值",
            "bili_jct": "假的-jct",
            "dedeuserid": "12345",
            "buvid3": "",
            "buvid4": "",
            "ac_time_value": "",
        }
        data.update(overrides)
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_returns_none_when_not_logged_in(self, config):
        assert auth_module.yt_dlp_cookie_file(config) is None

    def test_writes_a_netscape_cookie_file(self, config):
        self.write_login(config)

        path = auth_module.yt_dlp_cookie_file(config)

        assert path is not None and path.exists()
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        # Netscape 格式每行必须是 7 个 tab 分隔的字段
        assert all(len(line.split("\t")) == 7 for line in lines)
        names = [line.split("\t")[5] for line in lines]
        assert "SESSDATA" in names, "大小写不对 B 站不认"
        assert "bili_jct" in names
        assert "DedeUserID" in names

    def test_empty_fields_are_skipped(self, config):
        self.write_login(config)

        path = auth_module.yt_dlp_cookie_file(config)

        names = [l.split("\t")[5] for l in path.read_text(encoding="utf-8").splitlines() if not l.startswith("#")]
        assert "buvid3" not in names, "空值的 cookie 不该写进去"

    def test_returns_none_when_credentials_are_unusable(self, config):
        self.write_login(config, sessdata="")

        assert auth_module.yt_dlp_cookie_file(config) is None
