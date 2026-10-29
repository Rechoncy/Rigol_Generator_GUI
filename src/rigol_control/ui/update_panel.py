"""Non-modal software update notices, independent of instrument workers."""
from __future__ import annotations

from pathlib import Path
import sys

from PyQt6.QtCore import QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .. import __version__
from ..runtime import is_installed
from ..update_settings import UpdatePreferences, load_update_preferences, save_update_preferences
from ..updates import REPOSITORY_URL, ReleaseAsset, ReleaseInfo, UpdateChecker
from ..updates_download import UpdateDownloader


def preferred_update_package() -> str:
    if is_installed():
        return "setup"
    if not getattr(sys, "frozen", False):
        return "zip"
    executable_dir = Path(sys.executable).resolve().parent
    bundle_dir = Path(getattr(sys, "_MEIPASS", executable_dir)).resolve()
    return "zip" if bundle_dir in (executable_dir, executable_dir / "_internal") else "exe"


class UpdatePanel(QFrame):
    install_requested = pyqtSignal(object)
    STARTUP_DELAY_MS = 3000
    CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000

    def __init__(self, data_dir: Path, parent=None, *, checker=None, downloader=None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        self.data_dir = data_dir
        self.checker = checker if checker is not None else UpdateChecker(self)
        self.downloader = downloader if downloader is not None else UpdateDownloader(self)
        self.package_format = preferred_update_package()
        self._downloaded_path: Path | None = None
        self._downloaded_asset: ReleaseAsset | None = None
        self._inflight_asset: ReleaseAsset | None = None
        self._inflight_destination: Path | None = None
        self._install_request_pending = False
        self._install_request_automatic = False
        self._automatic_download_request = False
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
        preferences, error = load_update_preferences(data_dir)
        self.automatic.setChecked(preferences.automatic_check)
        self.automatic.setToolTip("启动 3 秒后及运行期间每 6 小时检查 GitHub 正式版；可随时关闭")
        row.addWidget(self.automatic)
        self.check_button = QPushButton("检查更新")
        self.check_button.clicked.connect(lambda: self.check())
        row.addWidget(self.check_button)
        layout.addLayout(row)
        download_preferences = QHBoxLayout()
        self.automatic_download = QCheckBox("发现更新后自动下载")
        self.automatic_download.setChecked(preferences.automatic_download)
        self.automatic_download.setToolTip("后台下载并核对 SHA-256；安装版可在设备空闲后自动升级")
        download_preferences.addWidget(self.automatic_download)
        self.automatic_install = QCheckBox("下载后自动安装（空闲时）")
        self.automatic_install.setChecked(preferences.automatic_install)
        self.automatic_install.setToolTip("先保留并备份用户数据，确认设备任务停止并退出旧程序后安装")
        self.automatic_install.setVisible(self.package_format == "setup")
        download_preferences.addWidget(self.automatic_install)
        package_label = QLabel("更新包：" + {"zip": "便携 ZIP", "exe": "独立 EXE", "setup": "安装程序 EXE"}[self.package_format])
        package_label.setObjectName("Muted")
        download_preferences.addStretch()
        download_preferences.addWidget(package_label)
        layout.addLayout(download_preferences)
        result = QHBoxLayout()
        self.status = QLabel(error or "从 GitHub 检查新版本，发现更新后在此提示。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status.setObjectName("Muted")
        result.addWidget(self.status, 1)
        self.release_button = QPushButton("打开发布页")
        self.release_button.setObjectName("PrimaryButton")
        self.release_button.setToolTip("在浏览器查看正式发布页，选择安装程序、便携 ZIP 或独立 EXE")
        self.release_button.hide()
        self.release_button.clicked.connect(self.open_release)
        result.addWidget(self.release_button)
        layout.addLayout(result)

        self.download_controls = QWidget()
        download_row = QHBoxLayout(self.download_controls)
        download_row.setContentsMargins(0, 0, 0, 0)
        self.download_status = QLabel()
        self.download_status.setWordWrap(True)
        self.download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.download_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        download_row.addWidget(self.download_status, 1)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 1000)
        self.download_progress.setFormat("%p%")
        self.download_progress.setMaximumWidth(130)
        self.download_progress.hide()
        download_row.addWidget(self.download_progress)
        self.download_button = QPushButton("下载更新")
        self.download_button.clicked.connect(lambda: self.download_update())
        download_row.addWidget(self.download_button)
        self.install_button = QPushButton("安装更新")
        self.install_button.clicked.connect(lambda: self.request_install())
        self.install_button.hide()
        download_row.addWidget(self.install_button)
        self.open_folder_button = QPushButton("打开下载文件夹")
        self.open_folder_button.clicked.connect(self.open_download_folder)
        self.open_folder_button.hide()
        download_row.addWidget(self.open_folder_button)
        self.download_controls.hide()
        layout.addWidget(self.download_controls)

        self.startup_timer = QTimer(self)
        self.startup_timer.setSingleShot(True)
        self.startup_timer.setInterval(self.STARTUP_DELAY_MS)
        self.startup_timer.timeout.connect(self._automatic_check)
        self.periodic_timer = QTimer(self)
        self.periodic_timer.setInterval(self.CHECK_INTERVAL_MS)
        self.periodic_timer.timeout.connect(self._automatic_check)
        self.automatic.toggled.connect(self._preference_changed)
        self.automatic_download.toggled.connect(self._download_preference_changed)
        self.automatic_install.toggled.connect(self._install_preference_changed)
        self.checker.checking_changed.connect(self._checking_changed)
        self.checker.update_available.connect(self._update_available)
        self.checker.up_to_date.connect(self._up_to_date)
        self.checker.failed.connect(self._failed)
        self.downloader.downloading_changed.connect(self._downloading_changed)
        self.downloader.progress.connect(self._download_progress)
        self.downloader.finished.connect(self._download_finished)
        self.downloader.failed.connect(self._download_failed)

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
        self._save_preferences()
        if enabled and self._automatic_started and not self._closed:
            self.startup_timer.start()
            self.periodic_timer.start()
        else:
            self.startup_timer.stop()
            self.periodic_timer.stop()
            if self._automatic_request and self.checker.checking:
                self.checker.cancel()
        self._set_status("自动检查已启用。" if enabled else "自动检查已关闭，仍可手动检查。")

    def _save_preferences(self) -> None:
        self._settings_error = ""
        try:
            save_update_preferences(self.data_dir, UpdatePreferences(
                self.automatic.isChecked(), self.automatic_download.isChecked(),
                self.automatic_install.isChecked()))
        except OSError as exc:
            self._settings_error = f"更新设置保存失败，本次选择仅在当前运行有效：{exc}"

    def _download_preference_changed(self, enabled: bool) -> None:
        self._save_preferences()
        self._set_status("自动下载已启用。" if enabled else "自动下载已关闭，仍可手动下载。")
        if not enabled and self._automatic_download_request and self.downloader.downloading:
            self.downloader.cancel()
            self._inflight_asset = None
            self._inflight_destination = None
            self.download_status.setText("自动下载已关闭，本次下载已取消。")
        elif enabled and self._available is not None and not self.downloader.downloading:
            self.download_update(automatic=True)

    def _install_preference_changed(self, enabled: bool) -> None:
        self._save_preferences()
        self._set_status("自动安装已启用，将在设备空闲后升级。" if enabled else "自动安装已关闭，可点击“安装更新”。")
        if enabled and self._downloaded_path is not None and not self._install_request_pending:
            self.request_install(automatic=True)

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
        self.download_controls.show()
        if not self.downloader.downloading:
            asset = self._download_asset()
            if self._downloaded_path is not None and (asset is None or self._downloaded_path.name != asset.name):
                self._downloaded_path = None
                self._downloaded_asset = None
                self._install_request_pending = False
                self.install_button.hide()
                self.open_folder_button.hide()
            self.download_button.setEnabled(asset is not None)
            if asset is None:
                self.download_status.setText("该版本未提供匹配的更新包或 SHA-256 校验信息，请从发布页下载。")
            elif self.automatic_download.isChecked():
                self.download_update(automatic=True)
            else:
                self.download_status.setText("自动下载已关闭，可点击“下载更新”。")

    def _up_to_date(self, latest: str) -> None:
        if self._closed:
            return
        self._available = None
        self.release_button.hide()
        self._set_status(f"检查完成：GitHub 正式版 v{latest}，当前版本无需更新。")
        if not self.downloader.downloading and self._downloaded_path is None:
            self.download_controls.hide()

    def _failed(self, message: str) -> None:
        if not self._closed:
            self.release_button.show()
            self._set_status(f"检查未完成：{message}。可稍后重试，设备功能仍可使用。")

    def open_release(self) -> None:
        if not self._closed:
            url = self._available.url if self._available is not None else f"{REPOSITORY_URL}/releases/latest"
            if not QDesktopServices.openUrl(QUrl(url)):
                self._set_status(f"无法打开浏览器，请访问 {url}")

    def stop(self) -> None:
        self._closed = True
        self.startup_timer.stop()
        self.periodic_timer.stop()
        self.checker.close()
        self.downloader.close()

    def _download_asset(self):
        if self._available is None:
            return None
        suffix = {"zip": "-portable.zip", "exe": "-windows-x64.exe", "setup": "-setup.exe"}[self.package_format]
        return next((asset for asset in self._available.assets if asset.name.endswith(suffix)), None)

    def download_update(self, *, automatic: bool = False) -> None:
        if self._closed:
            return
        if self.downloader.downloading:
            if not automatic:
                self.downloader.cancel()
                self._inflight_asset = None
                self._inflight_destination = None
                self.download_status.setText("下载已取消，可稍后重新下载。")
            return
        asset = self._download_asset()
        if asset is None:
            return
        if automatic and self._downloaded_path is not None and self._downloaded_asset == asset:
            # A cached/repeated version notice must keep a verified package and
            # any pending install decision. An installation error still needs
            # the explicit Install action; checking again must not replay it.
            return
        self._automatic_download_request = automatic
        self._downloaded_path = None
        self._downloaded_asset = None
        self._inflight_asset = asset
        self._inflight_destination = self.data_dir / "updates" / f"v{self._available.version}"
        self._install_request_pending = False
        self.install_button.hide()
        self.open_folder_button.hide()
        self.download_controls.show()
        self.download_status.setText("正在准备下载和校验…")
        if not self.downloader.start(asset, self._inflight_destination) and self._inflight_asset is asset:
            self._download_failed("无法启动下载，请稍后重试")

    def _downloading_changed(self, downloading: bool) -> None:
        self.download_button.setText("取消下载" if downloading else "下载更新")
        self.download_button.setEnabled(downloading or self._download_asset() is not None)
        self.download_progress.setVisible(downloading)
        if downloading:
            self.download_progress.setValue(0)
        else:
            self._automatic_download_request = False

    def _download_progress(self, received: int, total: int) -> None:
        if not self._closed:
            self.download_progress.setValue(int(received * 1000 / max(1, total)))
            self.download_status.setText(f"正在下载 / 校验：{received / 1048576:.1f} / {total / 1048576:.1f} MB")

    def _download_finished(self, path) -> None:
        if self._closed:
            return
        asset, destination = self._inflight_asset, self._inflight_destination
        if asset is None or destination is None:
            return  # A duplicate/late signal cannot replace a ready installer.
        try:
            path = Path(path)
            resolved_path = path.resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            self._download_failed("下载完成路径无效，请重新下载")
            return
        if resolved_path != (destination / asset.name).resolve():
            self._download_failed("下载完成路径与已校验的附件不一致，请重新下载")
            return
        self._inflight_asset = None
        self._inflight_destination = None
        self._downloaded_path = path
        self._downloaded_asset = asset
        self.download_status.setText(f"更新已下载并通过校验：{path.name}。停止实验、退出旧版后再升级。")
        self.open_folder_button.show()
        self.install_button.setVisible(self.package_format == "setup" and asset.name.endswith("-setup.exe"))
        self.install_button.setEnabled(True)
        if self.package_format == "setup" and self.automatic_install.isChecked():
            self.request_install(automatic=True)

    def _download_failed(self, message: str) -> None:
        if not self._closed:
            self._inflight_asset = None
            self._inflight_destination = None
            self._downloaded_asset = None
            self._install_request_pending = False
            self.install_button.hide()
            self.download_status.setText(f"下载未完成：{message}。可稍后重试或打开发布页。")

    @property
    def install_request_automatic(self) -> bool:
        return self._install_request_automatic

    def request_install(self, *, automatic: bool = False) -> None:
        if self._closed or self.package_format != "setup" or self._install_request_pending:
            return
        path, asset = self._downloaded_path, self._downloaded_asset
        if path is None or asset is None or not asset.name.endswith("-setup.exe") or path.name != asset.name:
            return
        self._install_request_pending = True
        self._install_request_automatic = automatic
        self.install_button.setEnabled(False)
        self.download_status.setText("更新已校验，正在准备备份用户数据并等待设备空闲…")
        # The main window owns the shutdown/data-backup/handoff decision. This
        # panel never launches an EXE or closes an instrument window by itself.
        self.install_requested.emit((path, asset))

    def set_install_status(self, message: str, *, retry: bool = True) -> None:
        if self._closed:
            return
        self.download_controls.show()
        self.download_status.setText(message)
        self._install_request_pending = not retry
        self.install_button.setVisible(self.package_format == "setup" and self._downloaded_asset is not None)
        self.install_button.setEnabled(retry)

    def open_download_folder(self) -> None:
        if self._downloaded_path is not None and not self._closed:
            path = self._downloaded_path.parent
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
                self.download_status.setText(f"无法打开文件夹，请访问：{path}")
