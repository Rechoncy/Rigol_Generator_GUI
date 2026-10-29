"""Installer requests retain verified metadata and never execute from the panel."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.ui.update_panel import UpdatePanel, preferred_update_package
from rigol_control.updates import ReleaseAsset, ReleaseInfo
from rigol_control.update_settings import UpdatePreferences, load_update_preferences

from test_update_panel import FakeChecker, FakeDownloader


def setup_asset(version="99.0.0", *, digest="0" * 64):
    name = f"Lab-Control-Center-{version}-windows-x64-setup.exe"
    return ReleaseAsset(name,
                        f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v{version}/{name}",
                        100, digest)


class InstallerUpdatePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["installer-panel-tests"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checker, self.downloader = FakeChecker(), FakeDownloader()
        self.installed = patch("rigol_control.ui.update_panel.is_installed", return_value=True)
        self.installed.start()
        self.panel = UpdatePanel(self.root, checker=self.checker, downloader=self.downloader)
        self.panel.show()
        self.asset = setup_asset()
        self.release = ReleaseInfo("99.0.0", "v99.0.0",
                                   "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v99.0.0",
                                   (self.asset,))
        self.requests = []
        self.panel.install_requested.connect(self.requests.append)

    def tearDown(self):
        self.panel.stop()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.installed.stop()
        self.temporary.cleanup()

    def ready(self):
        self.checker.update_available.emit(self.release)
        path = self.root / "updates/v99.0.0" / self.asset.name
        self.downloader.finish(self.downloader.finished, path)
        return path

    def test_installed_mode_selects_setup_and_preserves_other_formats(self):
        self.assertEqual(preferred_update_package(), "setup")
        self.assertTrue(self.panel.automatic_install.isChecked())
        self.assertTrue(self.panel.automatic_install.isVisible())
        zip_asset = ReleaseAsset("Lab-Control-Center-99.0.0-windows-x64-portable.zip", "zip", 100, "1" * 64)
        exe_asset = ReleaseAsset("Lab-Control-Center-99.0.0-windows-x64.exe", "exe", 100, "2" * 64)
        mixed = ReleaseInfo(self.release.version, self.release.tag, self.release.url,
                            (zip_asset, exe_asset, self.asset))
        self.checker.update_available.emit(mixed)
        self.assertEqual(self.downloader.calls[0][0], self.asset)
        self.assertFalse(self.requests, "Download start must never be treated as install-ready")

    def test_repeated_result_keeps_verified_setup_and_single_pending_install(self):
        path = self.ready()
        self.assertEqual(len(self.requests), 1)
        self.checker.update_available.emit(self.release)
        self.checker.update_available.emit(self.release)
        self.assertEqual(len(self.downloader.calls), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.panel._downloaded_path, path)
        self.assertEqual(self.panel._downloaded_asset, self.asset)
        self.assertTrue(self.panel._install_request_pending)

    def test_cached_recheck_does_not_replay_failed_install_without_explicit_click(self):
        path = self.ready()
        self.panel.set_install_status("测试安装准备失败", retry=True)
        self.checker.update_available.emit(self.release)
        self.assertEqual(len(self.downloader.calls), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.panel._downloaded_path, path)
        self.assertFalse(self.panel._install_request_pending)
        self.panel.install_button.click()
        self.assertEqual(len(self.requests), 2)

    def test_only_verified_completion_emits_once_and_never_opens_or_launches(self):
        with patch("rigol_control.ui.update_panel.QDesktopServices.openUrl") as browser:
            path = self.ready()
            self.assertEqual(self.requests, [(path, self.asset)])
            self.assertTrue(self.panel.install_request_automatic)
            self.assertTrue(self.panel.install_button.isVisible())
            self.assertFalse(self.panel.install_button.isEnabled())
            self.panel.request_install()
            self.downloader.finished.emit(path)
            self.assertEqual(len(self.requests), 1)
            browser.assert_not_called()

    def test_automatic_install_off_keeps_ready_file_for_explicit_install(self):
        self.panel.automatic_install.setChecked(False)
        path = self.ready()
        self.assertFalse(self.requests)
        self.assertTrue(self.panel.install_button.isEnabled())
        self.panel.install_button.click()
        self.assertEqual(self.requests, [(path, self.asset)])
        self.assertFalse(self.panel.install_request_automatic)
        preferences, _ = load_update_preferences(self.root)
        self.assertEqual(preferences, UpdatePreferences(True, True, False))

    def test_enabling_automatic_install_after_download_requests_existing_verified_file(self):
        self.panel.automatic_install.setChecked(False)
        path = self.ready()
        self.panel.automatic_install.setChecked(True)
        self.assertEqual(self.requests, [(path, self.asset)])
        self.assertTrue(self.panel.install_request_automatic)

    def test_busy_status_retains_verified_pair_and_manual_retry(self):
        path = self.ready()
        self.panel.set_install_status("设备任务尚未停止，请等待后重试", retry=True)
        self.assertIn("任务尚未停止", self.panel.download_status.text())
        self.assertTrue(self.panel.install_button.isEnabled())
        self.panel.install_button.click()
        self.assertEqual(self.requests, [(path, self.asset), (path, self.asset)])
        self.panel.set_install_status("正在备份用户数据", retry=False)
        self.assertFalse(self.panel.install_button.isEnabled())
        self.panel.request_install()
        self.assertEqual(len(self.requests), 2)

    def test_changed_release_cannot_substitute_an_inflight_asset_digest(self):
        self.checker.update_available.emit(self.release)
        different = setup_asset(digest="a" * 64)
        self.checker.update_available.emit(ReleaseInfo(self.release.version, self.release.tag,
                                                      self.release.url, (different,)))
        path = self.root / "updates/v99.0.0" / self.asset.name
        self.downloader.finish(self.downloader.finished, path)
        self.assertEqual(self.requests, [(path, self.asset)])
        self.assertNotEqual(self.requests[0][1].sha256, different.sha256)

    def test_wrong_destination_or_invalid_completion_cannot_request_install(self):
        for path in (self.root / self.asset.name, None):
            with self.subTest(path=path):
                self.checker.update_available.emit(self.release)
                self.downloader.finish(self.downloader.finished, path)
                self.assertFalse(self.requests)
                self.assertIsNone(self.panel._downloaded_asset)
                self.assertTrue(self.panel.install_button.isHidden())
                self.assertIn("路径", self.panel.download_status.text())

    def test_download_failure_or_cancellation_never_requests_install(self):
        self.checker.update_available.emit(self.release)
        self.downloader.finish(self.downloader.failed, "SHA-256 不匹配")
        self.assertFalse(self.requests)
        self.panel.request_install()
        self.assertFalse(self.requests)
        self.checker.update_available.emit(self.release)
        path = self.root / "updates/v99.0.0" / self.asset.name
        self.panel.download_button.click()
        self.downloader.finished.emit(path)
        self.assertFalse(self.requests)

    def test_close_prevents_late_install_request_and_late_status_changes(self):
        self.checker.update_available.emit(self.release)
        self.panel.stop()
        previous = self.panel.download_status.text()
        self.downloader.finished.emit(self.root / "updates/v99.0.0" / self.asset.name)
        self.panel.request_install()
        self.panel.set_install_status("late status")
        self.assertFalse(self.requests)
        self.assertEqual(self.panel.download_status.text(), previous)

    def test_standalone_exe_selection_cannot_accidentally_choose_setup(self):
        self.panel.package_format = "exe"
        executable = ReleaseAsset("Lab-Control-Center-99.0.0-windows-x64.exe", "exe", 100, "1" * 64)
        self.checker.update_available.emit(ReleaseInfo(self.release.version, self.release.tag,
                                                      self.release.url, (self.asset, executable)))
        self.assertEqual(self.downloader.calls[-1][0], executable)
        self.downloader.finish(self.downloader.finished,
                               self.root / "updates/v99.0.0" / executable.name)
        self.assertFalse(self.requests)
        self.assertTrue(self.panel.install_button.isHidden())
