import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from rigol_control.app import create_application
from rigol_control.config_archive import ConfigArchiveService
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.newport_smc100 import VIRTUAL_SMC100_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE


class ConfigArchiveUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["config-ui-test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = DeviceRepository(self.root / "source")
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="PR50CC", resource=VIRTUAL_SMC100_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        self.repo.save(self.stage)
        self.repo.save(self.ni)
        self.manager = DeviceConnectionManager()
        self.manager.connect(self.stage)
        self.manager.connect(self.ni)
        self.home = DeviceCenterWindow(self.repo, self.manager)
        self.home.show()
        self.homes = [self.home]
        self.assertTrue(self.wait(lambda: not self.home.probe_tasks))

    def tearDown(self):
        for home in self.homes:
            self.wait(lambda: home._transfer_task is None and not home.connection_tasks)
            home.close()
        self.app.processEvents()
        self.tmp.cleanup()

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        return predicate()

    def export_from_live_window(self):
        self.home.open_device(self.stage.id)
        stage_window = self.home.open_windows[self.stage.id]
        stage_window.step.setValue(0.37)
        stage_window.count.setValue(17)
        target = self.root / "portable.json"
        # Click before the autosave timer fires; export must flush current controls.
        with patch("rigol_control.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(target), "")):
            QTest.mouseClick(self.home.export_config_button, Qt.MouseButton.LeftButton)
        self.assertTrue(target.exists())
        return target, stage_window

    def test_buttons_export_and_import_reconnect_without_motion(self):
        target, _ = self.export_from_live_window()
        destination = DeviceRepository(self.root / "destination")
        old = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="old", resource="SIM::NI::old")
        destination.save(old)
        manager = DeviceConnectionManager()
        manager.connect(old)
        home = DeviceCenterWindow(destination, manager)
        self.homes.append(home)
        home.show()
        self.assertTrue(self.wait(lambda: not home.probe_tasks))
        with patch("rigol_control.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(target), "")):
            def accept_preview():
                dialog = QApplication.activeModalWidget()
                if isinstance(dialog, QMessageBox):
                    self.assertTrue(dialog.checkBox().isChecked())
                    dialog.button(QMessageBox.StandardButton.Ok).click()
            QTimer.singleShot(30, accept_preview)
            QTest.mouseClick(home.import_config_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: home._transfer_task is None and not home.connection_tasks))
        self.assertEqual({r.id for r in destination.list()}, {self.stage.id, self.ni.id})
        self.assertTrue(manager.is_connected(self.stage))
        self.assertTrue(manager.is_connected(self.ni))
        self.assertFalse(manager.is_connected(old))
        self.assertEqual(manager.newport_device_for(self.stage).move_history, [])
        home.open_device(self.stage.id)
        restored = home.open_windows[self.stage.id]
        self.assertEqual(restored.step.value(), 0.37)
        self.assertEqual(restored.count.value(), 17)
        self.assertEqual(restored.ni_combo.currentData(), self.ni.id)
        self.assertIsNone(restored.controller)
        backups = list((destination.data_dir / "config_backups").glob("*/configuration.json"))
        self.assertEqual(len(backups), 1)
        backup = ConfigArchiveService(destination.data_dir).read_file(backups[0])
        self.assertEqual(backup.files["devices.json"]["devices"][0]["id"], old.id)

    def test_cancel_import_leaves_files_windows_and_connections(self):
        target, stage_window = self.export_from_live_window()
        old = self.repo.path.read_bytes()
        with patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Cancel):
            self.home.import_configuration(target)
        self.assertEqual(self.repo.path.read_bytes(), old)
        self.assertIs(self.home.open_windows[self.stage.id], stage_window)
        self.assertTrue(self.manager.is_connected(self.stage))
        self.assertFalse((self.repo.data_dir / "config_backups").exists())

    def test_import_running_scan_is_rejected_without_stop(self):
        target, w = self.export_from_live_window()
        w.directory.setText(str(self.root / "runs"))
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller is not None and w.controller.snapshot().phase == "holding"))
        with patch.object(QMessageBox, "warning") as warning:
            self.home.import_configuration(target)
        self.assertTrue(warning.called)
        self.assertTrue(w.controller.is_running)
        self.assertFalse((self.repo.data_dir / "config_backups").exists())
        w.controller.stop()

    def test_same_home_restoration_closes_old_windows_and_preserves_imported_values(self):
        target, old = self.export_from_live_window()
        bundle = ConfigArchiveService(self.repo.data_dir).read_file(target)
        old.step.setValue(-0.42)
        self.home._restore_configuration(bundle, reconnect=False)
        self.assertTrue(self.wait(lambda: self.home._transfer_task is None))
        self.assertFalse(self.manager.is_connected(self.stage))
        self.home.open_device(self.stage.id)
        restored = self.home.open_windows[self.stage.id]
        self.app.processEvents()
        self.assertIs(self.home.open_windows[self.stage.id], restored)
        self.assertEqual(restored.step.value(), 0.37)
        self.assertIsNone(restored.controller)
        backups = list((self.repo.data_dir / "config_backups").glob("*/configuration.json"))
        old_bundle = ConfigArchiveService(self.repo.data_dir).read_file(backups[0])
        saved = old_bundle.files["device_parameters.json"]["devices"][self.stage.id]["parameters"]
        self.assertEqual(saved["controls"]["named/newport.step"]["value"], -0.42)


if __name__ == "__main__":
    unittest.main()
