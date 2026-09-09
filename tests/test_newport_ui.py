import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.newport_smc100 import Smc100Device, VIRTUAL_SMC100_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.ui.device_windows import create_device_window
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.ui.newport_window import NewportWindow
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE


class NewportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["newport-test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = DeviceConnectionManager()
        self.params = DeviceParameterRepository(self.tmp.name)
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="PR50CC", resource=VIRTUAL_SMC100_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        self.records = [self.stage, self.ni]
        self.manager.connect(self.stage)
        self.manager.connect(self.ni)
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.app.processEvents()
        self.manager.close_all()
        self.tmp.cleanup()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        return predicate()

    def window(self):
        w = create_device_window(self.stage, self.records, self.manager,
                                 device_provider=lambda: self.records,
                                 parameter_repository=self.params)
        self.assertIsInstance(w, NewportWindow)
        self.windows.append(w)
        w.directory.setText(str(Path(self.tmp.name) / "runs"))
        w.show()
        self.app.processEvents()
        return w

    def test_real_buttons_cycle_acquire_and_restore(self):
        w = self.window()
        QTest.mouseClick(w.refresh_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        w.step.setValue(0.1)
        w.count.setValue(3)
        w.ai_enabled.setChecked(True)
        w._select_page(1)
        QTest.mouseClick(w.start_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: w.controller is not None and w.controller.snapshot().phase == "holding"), w.scan_status.text())
        device = self.manager.newport_device_for(self.stage)
        self.assertEqual(device.move_history, [])
        self.assertFalse(w.move_button.isEnabled())
        for k in range(1, 5):
            QTest.mouseClick(w.inject_button, Qt.MouseButton.LeftButton)
            self.assertTrue(self.wait(lambda: w.controller.snapshot().completed == k), w.controller.snapshot())
        self.assertEqual(device.move_history, [0.1, 0.2, 0.0, 0.1])
        QTest.mouseClick(w.stop_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: not w.controller.is_running))
        self.assertIsNone(self.manager.lease_owner(self.ni.id))
        run = w.controller.run_directory
        self.assertTrue((run / "analog.csv").exists())
        self.assertTrue((run / "rotation.csv").exists())
        w._save_device_parameters(force=True)
        restored = self.window()
        self.assertEqual(restored.step.value(), 0.1)
        self.assertEqual(restored.count.value(), 3)
        self.assertEqual(restored.ni_combo.currentData(), self.ni.id)
        self.assertIsNone(restored.controller)
        self.assertEqual(len(device.move_history), 4)

    def test_field_stage_identifier_connects_and_refreshes_without_motion(self):
        identifier = "PR50CC_PN:B2210116_UD:01/10/2021"
        revision = "SMC_CC - Controller-driver version 3.1.2"

        class FieldSerial:
            def __init__(self):
                self.is_open = True
                self.writes = []
                self.replies = {
                    "VE": revision, "ID?": identifier, "TS": "000033",
                    "TP": "-5.2", "SL?": "-360", "SR?": "360", "VA?": "20",
                }

            def reset_input_buffer(self):
                pass

            def write(self, payload):
                self.writes.append(payload)
                return len(payload)

            def read_until(self, terminator, size):
                command = self.writes[-1].decode("ascii").strip()[1:]
                return f"1{command[:2]}{self.replies[command]}\r\n".encode("ascii")

            def close(self):
                self.is_open = False

        self.manager.disconnect(self.stage)
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100,
                                        name="Field PR50CC", resource="COM3::1")
        self.records = [self.stage, self.ni]
        serial = FieldSerial()
        device = Smc100Device(self.stage.resource, serial_factory=lambda **kw: serial)
        with patch("rigol_control.connections.Smc100Device", return_value=device):
            info = self.manager.connect(self.stage)
        self.assertTrue(info.connected)
        self.assertEqual(info.identity, f"{revision} / {identifier}")

        w = self.window()
        self.assertIs(w.device, device)
        self.assertEqual(w.connection_label.text(), info.identity)
        QTest.mouseClick(w.refresh_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.operation_label.text(), "操作完成")
        self.assertIn("-5.200000°", w.position_label.text())
        self.assertIn("状态 0x33", w.position_label.text())
        self.assertIn("速度 20°/s", w.position_label.text())
        self.assertTrue(w.move_button.isEnabled())
        self.assertTrue(self.manager.is_connected(self.stage))
        self.assertEqual(serial.writes,
                         [b"1VE\r\n", b"1ID?\r\n", b"1TP\r\n", b"1TS\r\n", b"1VA?\r\n"])

    def test_deleted_ni_is_not_silently_rebound(self):
        w = self.window()
        w._save_device_parameters(force=True)
        self.records = [self.stage]
        w.update_ni_devices(self.records)
        self.assertEqual(w.ni_combo.currentData(), self.ni.id)
        replacement = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="Other", resource="Dev9")
        self.records.append(replacement)
        w.update_ni_devices(self.records)
        w._start_scan()
        self.assertIsNone(w.controller)
        self.assertIn("有效的 NI", w.scan_status.text())
        restored = self.window()
        self.assertEqual(restored.ni_combo.currentData(), self.ni.id)

    def test_close_during_motion_stops_and_keeps_shared_connection(self):
        w = self.window()
        device = self.manager.newport_device_for(self.stage)
        device.set_velocity(0.1)
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._inject()
        self.assertTrue(self.wait(lambda: device.status().moving))
        self.assertTrue(w.close())
        self.windows.remove(w)
        self.assertFalse(device.status().moving)
        self.assertTrue(device.is_connected)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_home_factory_includes_virtual_newport(self):
        repo = DeviceRepository(Path(self.tmp.name) / "home")
        home = DeviceCenterWindow(repo, self.manager)
        self.windows.append(home)
        home.create_virtual_lab()
        self.assertTrue(self.wait(lambda: len(repo.list()) == 5 and all(self.manager.is_connected(r) for r in repo.list())))
        stage = next(r for r in repo.list() if r.kind is DeviceKind.NEWPORT_SMC100)
        home.open_device(stage.id)
        self.assertIsInstance(home.open_windows[stage.id], NewportWindow)


if __name__ == "__main__":
    unittest.main()
