"""B 站扫码登录对话框。

二维码有效期约 3 分钟，过期后可以点「刷新二维码」重新生成。
登录成功后登录态会存到 data/cookies/bilibili.json，之后不用再登。
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from ..bilibili.auth import (
    STATE_DONE,
    STATE_SCANNED,
    STATE_TIMEOUT,
    STATE_WAITING,
    QrCodeSession,
    credential_path,
    save_credential,
)
from ..config import Config
from ..logging_setup import get_logger
from .workers import friendly_error

log = get_logger(__name__)

STATE_TEXT = {
    STATE_WAITING: "请用 B 站手机 App 扫描二维码",
    STATE_SCANNED: "已扫描，请在手机上点「确认登录」",
    STATE_TIMEOUT: "二维码已过期，请点「刷新二维码」",
}


class LoginWorker(QThread):
    qrcode_ready = pyqtSignal(bytes)
    state_changed = pyqtSignal(str)
    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, config: Config, timeout_seconds: int = 180) -> None:
        super().__init__()
        self._config = config
        self._timeout = timeout_seconds
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        session = QrCodeSession()
        try:
            self.qrcode_ready.emit(session.start())
            deadline = time.time() + self._timeout
            last_state = ""
            while time.time() < deadline and not self._cancelled:
                state = session.poll()
                if state != last_state:
                    self.state_changed.emit(state)
                    last_state = state
                if state == STATE_DONE:
                    save_credential(session.credential(), credential_path(self._config))
                    self.succeeded.emit()
                    return
                if state == STATE_TIMEOUT:
                    self.failed.emit("二维码已过期，请点「刷新二维码」重新获取。")
                    return
                self.msleep(2000)
            if not self._cancelled:
                self.failed.emit("等待扫码超时，请重新打开登录窗口。")
        except Exception as exc:
            log.error("扫码登录失败：%s", exc)
            self.failed.emit(friendly_error(exc))
        finally:
            session.close()


class LoginDialog(QDialog):
    def __init__(self, config: Config, parent=None) -> None:
        super().__init__(parent)
        self._config = config
        self._worker: LoginWorker | None = None

        self.setWindowTitle("B 站扫码登录")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)

        self.image_label = QLabel("正在获取二维码…")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumSize(260, 260)
        layout.addWidget(self.image_label)

        self.state_label = QLabel("")
        self.state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.refresh_button = QPushButton("刷新二维码")
        self.refresh_button.clicked.connect(self.start_login)
        layout.addWidget(self.refresh_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.start_login()

    def start_login(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)

        self.image_label.setText("正在获取二维码…")
        self.state_label.setText("")
        self.refresh_button.setEnabled(False)

        self._worker = LoginWorker(self._config)
        self._worker.qrcode_ready.connect(self._on_qrcode)
        self._worker.state_changed.connect(self._on_state)
        self._worker.succeeded.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.start()

    def _on_qrcode(self, png: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(png):
            self.state_label.setText("二维码图片解码失败，请点「刷新二维码」重试。")
            self.refresh_button.setEnabled(True)
            return
        self.image_label.setPixmap(
            pixmap.scaled(
                260, 260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.refresh_button.setEnabled(True)

    def _on_state(self, state: str) -> None:
        self.state_label.setText(STATE_TEXT.get(state, state))

    def _on_success(self) -> None:
        self.state_label.setText("登录成功！")
        self.refresh_button.setEnabled(False)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self.state_label.setText(message)
        self.refresh_button.setEnabled(True)

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        super().closeEvent(event)
