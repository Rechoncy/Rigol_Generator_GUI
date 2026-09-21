import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton
from rigol_control.app import create_application
from rigol_control.config_archive import ConfigArchiveService
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.siglent_ssg import VIRTUAL_SSG_RESOURCE, capabilities
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.ui.device_windows import create_device_window
from rigol_control.ui.siglent_window import SiglentWindow


class SiglentUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["ssg-test"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG", resource=VIRTUAL_SSG_RESOURCE)
        self.manager = DeviceConnectionManager()
        self.parameters = DeviceParameterRepository(self.tmp.name)
        self.windows = []
        self.window = self.open_window()

    def open_window(self):
        window = create_device_window(self.record, [], self.manager, parameter_repository=self.parameters)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def wait(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(.003)
        return predicate()

    def idle(self, window=None):
        w = window or self.window
        self.assertTrue(self.wait(lambda: not w._busy and not w._call_tasks))

    def connect(self):
        self.window.connect_device()
        self.idle()
        self.assertTrue(self.window.driver.caps.known, self.window.log.toPlainText())

    def tearDown(self):
        self.idle()
        # Test cleanup only: stop through the same driver and release confirmed leases.
        if self.window.session.is_connected and self.window.driver.caps.known:
            self.window.driver.stop()
        self.manager.release_devices(self.window.owner)
        for window in self.windows:
            try:
                window.close()
            except RuntimeError:
                pass
        self.app.processEvents()
        self.manager.close_all()
        self.tmp.cleanup()

    def test_window_starts_disconnected_and_restore_never_writes(self):
        self.assertIsInstance(self.window, SiglentWindow)
        self.assertFalse(self.window.session.is_connected)
        self.assertEqual(self.window.pages.count(), 4)
        self.window.frequency.setValue(2.4e9)
        self.window.controls["mod.master"].setChecked(True)
        self.window.controls["sweep.point_trigger"].setCurrentIndex(2)
        self.window.table.item(0, 1).setText("-33.5")
        self.window._save_device_parameters(force=True)
        restored = self.open_window()
        self.assertFalse(restored.session.is_connected)
        self.assertEqual(restored.frequency.value(), 2.4e9)
        self.assertTrue(restored.controls["mod.master"].isChecked())
        self.assertEqual(restored.controls["sweep.point_trigger"].currentData(), "BUS")
        self.assertEqual(restored.table.item(0, 1).text(), "-33.5")

    def test_apply_and_start_lease_then_stop_releases(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        self.assertEqual(self.window.driver.state, "off", self.window.log.toPlainText())
        self.assertFalse(self.manager.active_leases())
        self.window._run(self.window.driver.start)
        self.idle()
        self.assertEqual(self.window.driver.state, "enabled", self.window.log.toPlainText())
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        self.assertFalse(self.window.close())
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())
        self.assertEqual(self.window.session.query(":OUTPut?"), "0")

    def test_invalid_table_error_stays_local(self):
        self.connect()
        self.window.controls["sweep.kind"].setCurrentIndex(1)
        self.window.table.item(0, 0).setText("bad frequency")
        self.window.apply_sweep()
        self.assertIn("无效数字", self.window.log.toPlainText())
        self.assertEqual(self.window.session.query(":SWEep:STATe?"), "OFF")

    def test_start_rejects_edits_after_apply(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        self.window.controls["sweep.points"].setValue(17)
        self.window.start_sweep()
        self.assertIn("已有编辑", self.window.log.toPlainText())
        self.assertEqual(self.window.session.query(":SWEep:STATe?"), "OFF")

    def test_3021_pulse_modes_gate_controls_without_changing_saved_selection(self):
        w = self.window
        w.driver.caps = capabilities("SIGLENT,SSG3021X,TEST,1")
        w.controls["mod.pulse"].setChecked(True)
        trigger = w.controls["mod.pulse_trigger"]
        trigger.setCurrentIndex(trigger.findData("EXT"))
        w._update_buttons()
        self.assertTrue(w.controls["mod.pulse_delay"].isEnabled())
        self.assertFalse(w.controls["mod.gate_polarity"].isEnabled())
        self.assertTrue(trigger.model().item(trigger.findData("BUS")).isEnabled())
        trigger.setCurrentIndex(trigger.findData("EGATE"))
        self.assertFalse(w.controls["mod.pulse_delay"].isEnabled())
        self.assertTrue(w.controls["mod.gate_polarity"].isEnabled())
        w.controls["mod.pulse_source"].setCurrentIndex(1)
        self.assertFalse(trigger.isEnabled())
        self.assertFalse(w.controls["mod.pulse_width"].isEnabled())
        self.assertEqual(trigger.currentData(), "EGATE")

    def test_3021_scan_bus_remains_selectable(self):
        self.window.driver.caps = capabilities("SIGLENT,SSG3021X,TEST,1")
        self.window._update_buttons()
        for key in ("sweep.sweep_trigger", "sweep.point_trigger"):
            combo = self.window.controls[key]
            self.assertTrue(combo.model().item(combo.findData("BUS")).isEnabled())

    def test_3021_pulse_bus_button_and_persistence(self):
        self.connect()
        # The virtual transport is shared; switch the driver's capability branch to 3021X.
        w = self.window
        w.driver.caps = capabilities("SIGLENT,SSG3021X,TEST,1")
        w._update_buttons()
        w.controls["mod.pulse"].setChecked(True)
        w.controls["mod.master"].setChecked(True)
        trigger = w.controls["mod.pulse_trigger"]
        trigger.setCurrentIndex(trigger.findData("BUS"))
        w.apply_modulation()
        self.idle()
        self.assertIn("脉冲触发=BUS", w.mod_status.text())
        button = next(b for b in w.findChildren(QPushButton) if b.text() == "触发脉冲（全局 *TRG）")
        button.click()
        self.idle()
        self.assertEqual(w.session.query(":OUTPut?"), "0")
        self.assertNotIn("错误", w.log.toPlainText())
        w._save_device_parameters(force=True)
        saved = self.parameters.load(self.record.id, kind=self.record.kind.value)
        self.assertEqual(saved["controls"]["named/ssg.mod.pulse_trigger"]["data"], "BUS")

    def test_archive_contains_kind_and_custom_table(self):
        repository = DeviceRepository(self.tmp.name)
        repository.save(self.record)
        self.window.table.item(0, 0).setText("1234567890")
        self.window._save_device_parameters(force=True)
        path = Path(self.tmp.name) / "ssg-export.json"
        ConfigArchiveService(self.tmp.name).export_file(path)
        bundle = ConfigArchiveService(self.tmp.name).read_file(path)
        self.assertIn("siglent_ssg", str(bundle.payload))
        self.assertIn("1234567890", str(bundle.payload))

    def test_connection_and_readback_do_not_apply_restored_modulation(self):
        self.window.controls["mod.am"].setChecked(True)
        self.window.controls["mod.master"].setChecked(True)
        self.connect()
        self.assertEqual(self.window.session.query(":AM:STATe?"), "0")
        self.assertEqual(self.window.session.query(":OUTPut:MODulation?"), "0")
        self.window.apply_modulation()
        self.idle()
        self.assertEqual(self.window.session.query(":AM:STATe?"), "1", self.window.log.toPlainText())
        self.assertEqual(self.window.session.query(":OUTPut?"), "0")

    def test_transport_failure_keeps_unknown_lease_and_reconnect_does_not_restart(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        self.window._run(self.window.driver.start)
        self.idle()
        original = self.window.session.write
        calls = []
        def fail(command, **kwargs):
            calls.append(command)
            raise OSError("lost")
        self.window.session.write = fail
        self.window._run(self.window.driver.bus_trigger)
        self.idle()
        self.assertEqual(calls, ["*TRG"])
        self.assertEqual(self.window.driver.state, "unknown")
        self.assertTrue(self.manager.active_leases())
        self.window.session.write = original
        self.window.connect_device()
        self.idle()
        self.assertEqual(self.window.driver.state, "enabled")
        self.assertIsNone(self.window.driver.prepared)
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())
