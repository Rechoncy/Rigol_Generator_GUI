"""Update notifications must remain independent of instruments and the UI loop."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication

from rigol_control import app as app_module
from rigol_control.app import create_application
from rigol_control.ui.update_panel import UpdatePanel, preferred_update_package
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.repository import DeviceRepository
from rigol_control.update_settings import (UpdatePreferences, load_update_preference, save_update_preference,
                                          load_update_preferences, save_update_preferences)
from rigol_control.updates import ReleaseAsset, ReleaseInfo


class FakeChecker(QObject):
    checking_changed = pyqtSignal(bool)
    update_available = pyqtSignal(object)
    up_to_date = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.checking = False
        self.calls = 0
        self.closed = False
        self.cancels = 0

    def check(self):
        self.calls += 1
        self.checking = True
        self.checking_changed.emit(True)
        return True

    def cancel(self):
        self.cancels += 1
        self.checking = False
        self.checking_changed.emit(False)

    def close(self):
        self.closed = True
        self.cancel()

    def finish(self, signal, result):
        self.checking = False
        self.checking_changed.emit(False)
        signal.emit(result)


class FakeDownloader(QObject):
    downloading_changed = pyqtSignal(bool)
    progress = pyqtSignal(object, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.downloading = False
        self.calls = []
        self.cancels = 0
        self.closed = False

    def start(self, asset, directory):
        self.calls.append((asset, directory))
        self.downloading = True
        self.downloading_changed.emit(True)
        return True

    def cancel(self):
        self.cancels += 1
        self.downloading = False
        self.downloading_changed.emit(False)

    def close(self):
        self.closed = True
        self.cancel()

    def finish(self, signal, value):
        self.downloading = False
        self.downloading_changed.emit(False)
        signal.emit(value)


class UpdatePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_install_default_and_migration_of_setting(self):
        self.assertEqual(load_update_preference(self.root), (True, ""))
        save_update_preference(self.root, False)
        self.assertEqual(load_update_preference(self.root), (False, ""))
        save_update_preference(self.root, True)
        self.assertEqual(load_update_preference(self.root), (True, ""))
        self.assertEqual([p.name for p in self.root.iterdir()], ["update_settings.json"])

    def test_corrupt_or_unsupported_setting_does_not_reenable_network(self):
        for content in ('[]', '{}', '{broken', '{"schema_version":1,"automatic_check":"false"}',
                        '{"schema_version":2,"automatic_check":true}', 'x' * 4097):
            with self.subTest(content=content[:80]):
                (self.root / "update_settings.json").write_text(content, encoding="utf-8")
                enabled, error = load_update_preference(self.root)
                self.assertFalse(enabled)
                self.assertIn("自动检查已暂停", error)

    def test_failed_atomic_replace_preserves_previous_setting(self):
        save_update_preference(self.root, False)
        with patch.object(Path, "replace", side_effect=PermissionError("read only")):
            with self.assertRaises(PermissionError):
                save_update_preference(self.root, True)
        self.assertEqual(load_update_preference(self.root), (False, ""))
        self.assertEqual([p.name for p in self.root.iterdir()], ["update_settings.json"])

    def test_old_setting_adds_download_default_and_preserves_separate_choices(self):
        (self.root / "update_settings.json").write_text(
            '{"schema_version":1,"automatic_check":false}', encoding="utf-8")
        self.assertEqual(load_update_preferences(self.root), (UpdatePreferences(False, True), ""))
        save_update_preferences(self.root, UpdatePreferences(True, False))
        save_update_preference(self.root, False)
        self.assertEqual(load_update_preferences(self.root), (UpdatePreferences(False, False), ""))

    def test_malformed_download_setting_disables_both_automatic_options(self):
        (self.root / "update_settings.json").write_text(
            '{"schema_version":1,"automatic_check":true,"automatic_download":"yes"}', encoding="utf-8")
        preferences, error = load_update_preferences(self.root)
        self.assertEqual(preferences, UpdatePreferences(False, False, False))
        self.assertIn("读取失败", error)

    def test_deeply_nested_settings_cannot_abort_application_startup(self):
        (self.root / "update_settings.json").write_text("[" * 1500 + "]" * 1500, encoding="utf-8")
        preferences, error = load_update_preferences(self.root)
        self.assertEqual(preferences, UpdatePreferences(False, False, False))
        self.assertIn("读取失败", error)


class UpdatePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["update-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checker = FakeChecker()
        self.panel = UpdatePanel(self.root, checker=self.checker)
        self.panel.show()
        self.app.processEvents()

    def tearDown(self):
        self.panel.stop()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_construction_has_no_network_or_automatic_timer(self):
        self.assertEqual(self.checker.calls, 0)
        self.assertFalse(self.panel.startup_timer.isActive())
        self.assertFalse(self.panel.periodic_timer.isActive())

    def test_auto_delay_and_disable_cancel_pending_auto_request(self):
        self.panel.start_automatic_checks()
        self.assertEqual(self.checker.calls, 0)
        self.assertEqual(self.panel.startup_timer.interval(), 3000)
        self.assertTrue(self.panel.startup_timer.isActive())
        # Deliver the scheduled timeout deterministically; prior GUI tests may
        # have queued quit events, so wall-clock waits are unsuitable here.
        self.panel.startup_timer.timeout.emit()
        self.assertEqual(self.checker.calls, 1)
        self.panel.automatic.setChecked(False)
        self.assertFalse(self.checker.checking)
        self.assertEqual(self.checker.cancels, 1)
        self.assertTrue(self.panel.check_button.isEnabled())
        self.assertFalse(self.panel.periodic_timer.isActive())
        self.assertEqual(load_update_preference(self.root), (False, ""))

    def test_manual_check_coalesces_startup_and_disabling_does_not_abort_manual(self):
        self.panel.start_automatic_checks()
        self.panel.check_button.click()
        self.assertFalse(self.panel.startup_timer.isActive())
        self.assertEqual(self.checker.calls, 1)
        self.assertFalse(self.panel.check_button.isEnabled())
        self.panel.check()
        self.panel.automatic.setChecked(False)
        self.assertEqual(self.checker.calls, 1)
        self.assertTrue(self.checker.checking)
        self.assertEqual(self.checker.cancels, 0)

    def test_nonmodal_new_release_and_explicit_browser_click(self):
        info = ReleaseInfo("99.0.0", "v99.0.0",
                           "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v99.0.0")
        with patch("rigol_control.ui.update_panel.QDesktopServices.openUrl", return_value=True) as browser:
            self.panel.check()
            self.checker.finish(self.checker.update_available, info)
            self.assertIn("发现新版本 v99.0.0", self.panel.status.text())
            self.assertTrue(self.panel.release_button.isVisible())
            browser.assert_not_called()
            self.assertTrue(self.panel.automatic.isEnabled())
            self.panel.release_button.click()
            self.assertEqual(browser.call_args.args[0].toString(), info.url)

    def test_network_failure_preserves_known_notice_and_supports_manual_retry(self):
        info = ReleaseInfo("99.0.0", "v99.0.0",
                           "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v99.0.0")
        self.checker.update_available.emit(info)
        self.panel.check()
        self.checker.finish(self.checker.failed, "网络超时")
        self.assertIn("网络超时", self.panel.status.text())
        self.assertIn("v99.0.0", self.panel.status.text())
        self.assertTrue(self.panel.release_button.isVisible())
        self.panel.check_button.click()
        self.assertEqual(self.checker.calls, 2)
        self.checker.finish(self.checker.up_to_date, "0.7.20")
        self.assertIn("无需更新", self.panel.status.text())
        self.assertFalse(self.panel.release_button.isVisible())

    def test_first_check_failure_offers_official_latest_page_without_auto_browser(self):
        with patch("rigol_control.ui.update_panel.QDesktopServices.openUrl", return_value=True) as browser:
            self.panel.check()
            self.checker.finish(self.checker.failed, "GitHub API 请求额度暂时用完")
            self.assertTrue(self.panel.release_button.isVisible())
            self.assertIsNone(self.panel._available)
            self.assertFalse(self.panel.downloader.downloading)
            self.assertNotIn("无需更新", self.panel.status.text())
            browser.assert_not_called()
            self.panel.release_button.click()
            self.assertEqual(browser.call_args.args[0].toString(),
                             "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/latest")
            self.panel.stop()
            self.panel.open_release()
            self.assertEqual(browser.call_count, 1)

    def test_failed_browser_open_before_version_known_keeps_fixed_release_link_visible(self):
        self.checker.finish(self.checker.failed, "offline")
        with patch("rigol_control.ui.update_panel.QDesktopServices.openUrl", return_value=False):
            self.panel.open_release()
        self.assertIn("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/latest", self.panel.status.text())
        self.assertTrue(self.panel.release_button.isVisible())

    def test_save_failure_visible_and_close_cancels_timers(self):
        self.panel.start_automatic_checks()
        with patch("rigol_control.ui.update_panel.save_update_preferences", side_effect=OSError("denied")):
            self.panel.automatic.setChecked(False)
        self.assertIn("保存失败", self.panel.status.text())
        self.panel.check()
        self.checker.finish(self.checker.failed, "offline")
        self.assertIn("保存失败", self.panel.status.text())
        self.panel.stop()
        self.panel.check()
        self.panel.start_automatic_checks()
        self.assertEqual(self.checker.calls, 1)
        self.assertTrue(self.checker.closed)
        self.assertFalse(self.panel.startup_timer.isActive())
        self.assertFalse(self.panel.periodic_timer.isActive())

    def test_disabled_setting_honored_and_manual_check_remains_available(self):
        save_update_preference(self.root, False)
        other = UpdatePanel(self.root, checker=self.checker)
        try:
            other.start_automatic_checks()
            self.assertFalse(other.startup_timer.isActive())
            self.assertFalse(other.periodic_timer.isActive())
            other.check_button.click()
            self.assertEqual(self.checker.calls, 1)
        finally:
            other.stop()
            other.deleteLater()

    def test_main_close_cancels_check_without_waiting_for_network(self):
        home = DeviceCenterWindow(DeviceRepository(self.root))
        home.show()
        try:
            with patch.object(home.update_panel.checker, "close") as cancel:
                self.assertTrue(home.close())
                cancel.assert_called_once()
                self.assertTrue(home.update_panel._closed)
        finally:
            home.deleteLater()

    def test_rejected_device_close_keeps_update_panel_alive(self):
        home = DeviceCenterWindow(DeviceRepository(self.root))
        device_window = Mock()
        device_window.close.return_value = False
        home.open_windows["busy"] = device_window
        home.show()
        try:
            with patch.object(home.update_panel.checker, "close") as cancel:
                self.assertFalse(home.close())
                cancel.assert_not_called()
                self.assertFalse(home.update_panel._closed)
        finally:
            home.open_windows.clear()
            home.close()
            home.deleteLater()


class AppUpdateStartupTests(unittest.TestCase):
    def test_only_ordinary_startup_schedules_network(self):
        with tempfile.TemporaryDirectory() as directory:
            for args in ([], ["--smoke-test"]):
                with self.subTest(args=args), patch.object(app_module, "prepare_data_dir", return_value=Path(directory)), \
                        patch.object(app_module, "create_application") as application, \
                        patch.object(app_module, "DeviceCenterWindow") as window, \
                        patch.object(app_module.QTimer, "singleShot"), patch.dict(os.environ):
                    application.return_value.exec.return_value = 0
                    self.assertEqual(app_module.main(args), 0)
                    if args:
                        window.return_value.update_panel.start_automatic_checks.assert_not_called()
                    else:
                        window.return_value.update_panel.start_automatic_checks.assert_called_once()


class UpdateDownloadPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["download-panel-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checker, self.downloader = FakeChecker(), FakeDownloader()
        self.panel = UpdatePanel(self.root, checker=self.checker, downloader=self.downloader)
        self.asset = ReleaseAsset("Lab-Control-Center-99.0.0-windows-x64-portable.zip",
                                 "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v99.0.0/"
                                 "Lab-Control-Center-99.0.0-windows-x64-portable.zip", 100, "0" * 64)
        self.release = ReleaseInfo("99.0.0", "v99.0.0",
                                  "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v99.0.0",
                                  (self.asset,))
        self.panel.show()

    def tearDown(self):
        self.panel.stop()
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_automatic_download_starts_with_compatible_asset_and_separate_folder(self):
        self.checker.update_available.emit(self.release)
        self.assertEqual(self.downloader.calls, [(self.asset, self.root / "updates/v99.0.0")])
        self.assertEqual(self.panel.download_button.text(), "取消下载")
        self.downloader.progress.emit(50, 100)
        self.assertEqual(self.panel.download_progress.value(), 500)
        self.assertIn("正在下载", self.panel.download_status.text())
        self.assertTrue(self.panel.check_button.isEnabled())

    def test_verified_completion_only_opens_folder_on_explicit_click(self):
        self.checker.update_available.emit(self.release)
        path = self.root / "updates/v99.0.0" / self.asset.name
        with patch("rigol_control.ui.update_panel.QDesktopServices.openUrl", return_value=True) as browser:
            self.downloader.finish(self.downloader.finished, path)
            self.assertIn("通过校验", self.panel.download_status.text())
            browser.assert_not_called()
            self.panel.open_folder_button.click()
            self.assertEqual(Path(browser.call_args.args[0].toLocalFile()), path.parent)
            self.assertNotEqual(Path(browser.call_args.args[0].toLocalFile()), path)

    def test_cancel_keeps_new_version_notice_and_allows_manual_retry(self):
        self.checker.update_available.emit(self.release)
        self.panel.download_button.click()
        self.assertEqual(self.downloader.cancels, 1)
        self.assertIn("取消", self.panel.download_status.text())
        self.assertIn("发现新版本", self.panel.status.text())
        self.panel.download_button.click()
        self.assertEqual(len(self.downloader.calls), 2)
        self.downloader.finish(self.downloader.failed, "SHA-256 不匹配")
        self.assertIn("SHA-256", self.panel.download_status.text())
        self.assertTrue(self.panel.open_folder_button.isHidden())
        self.assertTrue(self.panel.download_button.isEnabled())

    def test_automatic_download_preference_is_independent_and_manual_retry_works(self):
        self.panel.automatic_download.setChecked(False)
        self.checker.update_available.emit(self.release)
        self.assertFalse(self.downloader.calls)
        preferences, _ = load_update_preferences(self.root)
        self.assertEqual(preferences, UpdatePreferences(True, False))
        self.panel.download_button.click()
        self.assertEqual(len(self.downloader.calls), 1)
        self.panel.automatic.setChecked(False)
        self.assertTrue(self.downloader.downloading)

    def test_disabling_automatic_download_cancels_automatic_request(self):
        self.checker.update_available.emit(self.release)
        self.panel.automatic_download.setChecked(False)
        self.assertEqual(self.downloader.cancels, 1)
        self.assertFalse(self.downloader.downloading)

    def test_missing_trusted_asset_offers_release_page_without_download(self):
        self.checker.update_available.emit(ReleaseInfo("99.0.0", "v99.0.0", self.release.url))
        self.assertFalse(self.downloader.calls)
        self.assertFalse(self.panel.download_button.isEnabled())
        self.assertIn("SHA-256", self.panel.download_status.text())
        self.assertTrue(self.panel.release_button.isVisible())

    def test_close_cancels_download_and_ignores_late_completion(self):
        self.checker.update_available.emit(self.release)
        self.panel.stop()
        self.assertTrue(self.downloader.closed)
        self.downloader.finished.emit(self.root / self.asset.name)
        self.assertIsNone(self.panel._downloaded_path)

    def test_running_distribution_selects_same_update_package_format(self):
        with patch("rigol_control.ui.update_panel.sys.frozen", True, create=True), \
                patch("rigol_control.ui.update_panel.sys.executable", str(self.root / "app.exe")):
            with patch("rigol_control.ui.update_panel.sys._MEIPASS", str(self.root / "_internal"), create=True):
                self.assertEqual(preferred_update_package(), "zip")
            with patch("rigol_control.ui.update_panel.sys._MEIPASS", str(self.root / "temp/extraction"), create=True):
                self.assertEqual(preferred_update_package(), "exe")
