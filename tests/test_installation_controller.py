import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication
from rigol_control.app import create_application
from rigol_control.repository import DeviceRepository
from rigol_control.ui.main_window import DeviceCenterWindow


class InstallationControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["install-controller-tests"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = DeviceCenterWindow(DeviceRepository(Path(self.temporary.name)))
        self.controller = self.home.installation_controller
        self.request = (Path(self.temporary.name) / "verified-setup.exe", Mock())

    def tearDown(self):
        self.controller.preparing = False
        self.home.open_windows.clear()
        self.home.close()
        self.home.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def test_automatic_install_waits_for_existing_instrument_lease_without_closing(self):
        self.home.update_panel._install_request_automatic = True
        self.home.update_panel.automatic_install.setChecked(True)
        with patch.object(self.home.connection_manager, "active_leases", return_value=("busy",)), \
                patch.object(self.home, "close") as close, patch.object(self.controller.pool, "start") as start:
            self.controller.request(self.request)
            self.assertEqual(self.controller.pending, self.request)
            close.assert_not_called()
            start.assert_not_called()
            self.assertTrue(self.controller.timer.isActive())

    def test_disabling_automatic_install_cancels_busy_queue(self):
        self.controller.pending = self.request
        self.controller.automatic = True
        self.home.update_panel.automatic_install.setChecked(False)
        with patch.object(self.controller.pool, "start") as start:
            self.controller._try_start()
        self.assertIsNone(self.controller.pending)
        start.assert_not_called()

    def test_manual_busy_request_requires_explicit_retry(self):
        with patch.object(self.home.connection_manager, "active_transitions", return_value=("busy",)), \
                patch.object(self.controller.pool, "start") as start:
            self.controller.request(self.request)
        self.assertIsNone(self.controller.pending)
        start.assert_not_called()

    def test_hardware_became_busy_after_preparation_cancels_helper_without_install_commit(self):
        self.controller.preparing = True
        with patch.object(self.home.connection_manager, "active_leases", return_value=("busy",)), \
                patch("rigol_control.ui.installation_controller.cancel_handoff") as cancel, \
                patch("rigol_control.ui.installation_controller.commit_handoff") as commit:
            token = Mock()
            self.controller._prepared(token)
        cancel.assert_called_once_with(token)
        commit.assert_not_called()

    def test_refused_window_close_does_not_authorize_installation(self):
        with patch.object(self.home, "close", return_value=False), \
                patch("rigol_control.ui.installation_controller.cancel_handoff") as cancel, \
                patch("rigol_control.ui.installation_controller.commit_handoff") as commit:
            token = Mock()
            self.controller._prepared(token)
        cancel.assert_called_once_with(token)
        commit.assert_not_called()

    def test_clean_accepted_close_commits_then_quits(self):
        with patch.object(self.home, "close", return_value=True), \
                patch("rigol_control.ui.installation_controller.commit_handoff") as commit, \
                patch.object(self.app, "quit") as quit:
            token = Mock()
            self.controller._prepared(token)
        commit.assert_called_once_with(token)
        quit.assert_called_once()

    def test_parameter_save_failure_cancels_installer_without_closing_devices(self):
        with patch.object(self.home, "_flush_device_parameters", side_effect=OSError("disk full")), \
                patch.object(self.home, "close") as close, \
                patch("rigol_control.ui.installation_controller.cancel_handoff") as cancel, \
                patch("rigol_control.ui.installation_controller.commit_handoff") as commit:
            token = Mock()
            self.controller._prepared(token)
        cancel.assert_called_once_with(token)
        commit.assert_not_called()
        close.assert_not_called()

    def test_preparation_dispatch_failure_keeps_application_open_and_retryable(self):
        with patch.object(self.controller.pool, "start", side_effect=RuntimeError("dispatch")):
            self.controller.request(self.request)
        self.assertFalse(self.controller.preparing)
        self.assertIsNone(self.controller.task)
        self.assertFalse(self.controller.timer.isActive())

    def test_ordinary_close_cancels_unstarted_install_queue(self):
        self.controller.pending = self.request
        self.controller.on_window_closed()
        self.assertIsNone(self.controller.pending)
        self.assertFalse(self.controller.timer.isActive())
