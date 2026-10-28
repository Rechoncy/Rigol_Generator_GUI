"""Non-modal software update notices, independent of instrument workers."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .. import __version__
from ..update_settings import load_update_preference, save_update_preference
from ..updates import ReleaseInfo, UpdateChecker


class UpdatePanel(QFrame):
    STARTUP_DELAY_MS = 3000
    CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000

    def __init__(self, data_dir: Path, parent=None, *, checker=None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.data_dir = data_dir
        self.checker = checker if checker is not None else UpdateChecker(self)
        self._automatic_started = False
        self._closed = False
        self._automatic_request = False
        self._available: ReleaseInfo | None = None
        self._settings_error = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel(f"软件更新 · 当前 v{__version__}"))
        row.addStretch()
        self.automatic = QCheckBox("启动后自动检查更新")
        enabled, error = load_update_preference(data_dir)
        self.automatic.setChecked(enabled)
        self.automatic.setToolTip("启动 3 秒后及运行期间每 6 小时检查 GitHub 正式版；可随时关闭")
        row.addWidget(self.automatic)
        self.check_button = QPushButton("检查更新")
        self.check_button.clicked.connect(lambda: self.check())
        row.addWidget(self.check_button)
        layout.addLayout(row)
        result = QHBoxLayout()
        self.status = QLabel(error or "从 GitHub 检查新版本，发现更新后在此提示。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setObjectName("Muted")
        result.addWidget(self.status, 1)
        self.release_button = QPushButton("打开发布页")
        self.release_button.setObjectName("PrimaryButton")
        self.release_button.setToolTip("在浏览器查看更新说明并选择下载便携 ZIP 或 EXE")
        self.release_button.hide()
        self.release_button.clicked.connect(self.open_release)
        result.addWidget(self.release_button)
        layout.addLayout(result)

        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.setInterval(self.STARTUP_DELAY_MS)
        self.startup_timer.timeout.connect(self._automatic_check)
        self.periodic_timer = QTimer(self)
        self.periodic_timer.setInterval(self.CHECK_INTERVAL_MS)
        self.periodic_timer.timeout.connect(self._automatic_check)
        self.automatic.toggled.connect(self._preference_changed)
        self.checker.checking_changed.connect(self._checking_changed)
        self.checker.update_available.connect(self._update_available)
        self.checker.up_to_date.connect(self._up_to_date)
        self.checker.failed.connect(self._failed)

    def start_automatic_checks(self) -> None:
        """Called only by normal startup, never by construction or self-tests."""
        if self._closed or self._automatic_started:
            return
        self._automatic_started = True
        if self.automatic.isChecked():
            self.startup_timer.start()
            self.periodic_timer.start()

    def check(self, *, automatic: bool = False) -> None:
        if self._closed or self.checker.checking:
            return
        # A manual check also satisfies the pending startup check.
        self.startup_timer.stop()
        self._automatic_request = automatic
        self.checker.check()

    def _automatic_check(self) -> None:
        if self._automatic_started and self.automatic.isChecked():
            self.check(automatic=True)

    def _preference_changed(self, enabled: bool) -> None:
        self._settings_error = ""
        try:
            save_update_preference(self.data_dir, enabled)
        except OSError as exc:
            self._settings_error = f"更新设置保存失败，本次选择仅在当前运行有效：{exc}"
        if enabled and self._automatic_started and not self._closed:
            self.startup_timer.start()
            self.periodic_timer.start()
        else:
            self.startup_timer.stop()
            self.periodic_timer.stop()
            if self._automatic_request and self.checker.checking:
                self.checker.cancel()
        self._set_status("自动检查已启用。" if enabled else "自动检查已关闭，仍可手动检查。")

    def _set_status(self, text: str) -> None:
        if self._available is not None:
            text = f"发现新版本 v{self._available.version}，当前 v{__version__}。{text}"
        if self._settings_error:
            text += f" {self._settings_error}"
        self.status.setText(text)

    def _checking_changed(self, checking: bool) -> None:
        self.check_button.setEnabled(not checking)
        if checking:
            self._set_status("正在检查 GitHub…")
        else:
            self._automatic_request = False

    def _update_available(self, release: ReleaseInfo) -> None:
        if self._closed:
            return
        self._available = release
        self.release_button.show()
        self._set_status("可打开发布页查看更新说明和下载。")

    def _up_to_date(self, latest: str) -> None:
        if self._closed:
            return
        self._available = None
        self.release_button.hide()
        self._set_status(f"检查完成：GitHub 正式版 v{latest}，当前版本无需更新。")

    def _failed(self, message: str) -> None:
        if not self._closed:
            self._set_status(f"检查未完成：{message}。可稍后重试，设备功能仍可使用。")

    def open_release(self) -> None:
        if self._available is not None and not self._closed:
            if not QDesktopServices.openUrl(QUrl(self._available.url)):
                self._set_status(f"无法打开浏览器，请访问 {self._available.url}")

    def stop(self) -> None:
        self._closed = True
        self.startup_timer.stop()
        self.periodic_timer.stop()
        self.checker.close()
