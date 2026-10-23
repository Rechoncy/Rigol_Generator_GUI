import os
import json
import tempfile
import time
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton
from rigol_control.app import create_application
from rigol_control.config_archive import ConfigArchiveService
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.siglent_ssg import SsgPoint, VIRTUAL_SSG_RESOURCE, capabilities
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
        self.assertTrue(self.wait(lambda: not w._busy and not w._call_tasks and not w._pending_safety))

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
        self.assertEqual(self.window.pages.count(), 5)
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

    def test_initial_connection_uses_its_native_communication_lease_owner(self):
        calls = []
        original = self.manager.connect

        def connect(record, *, owner=None):
            calls.append((record.id, owner, self.manager.lease_owner(record.id)))
            return original(record, owner=owner)

        with patch.object(self.manager, "connect", side_effect=connect):
            self.connect()
        self.assertEqual(calls, [(self.record.id, self.window.owner, self.window.owner)])
        self.assertTrue(self.window.session.is_connected)
        self.assertFalse(self.manager.active_leases())

    def test_stop_reconnect_uses_owner_and_retains_lease_until_confirmed_off(self):
        self.arm_external_scan()
        self.window.session.disconnect()
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        calls = []
        original = self.manager.connect

        def connect(record, *, owner=None):
            calls.append((record.id, owner, self.manager.lease_owner(record.id)))
            return original(record, owner=owner)

        with patch.object(self.manager, "connect", side_effect=connect):
            self.window._safety(self.window.driver.stop)
            self.idle()
        self.assertEqual(calls, [(self.record.id, self.window.owner, self.window.owner)])
        self.assertTrue(self.window.session.is_connected)
        self.assertEqual(self.window.driver.state, "off")
        self.assertFalse(self.manager.active_leases())

    def test_power_unit_switch_preserves_prepared_step_configuration(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        before = self.window.driver.prepared
        self.window.controls["sweep.start_dbm"].set_unit("mV")
        self.window.controls["sweep.stop_dbm"].set_unit("mVpp")
        self.assertEqual(self.window._sweep_config(), before)
        self.assertEqual(self.window.controls["sweep.start_dbm"].value(), -20)
        self.window._select_page(2)
        self.window.apply_sweep()
        self.idle()
        before = self.window.driver.prepared
        for unit in ("mV", "mVpp", "dBm"):
            self.window.list_power_unit.setCurrentIndex(self.window.list_power_unit.findData(unit))
            self.assertEqual(self.window._sweep_config(), before)

    def test_scan_pages_share_bottom_monitor_without_commands_or_parameter_crosstalk(self):
        w = self.window
        self.assertEqual([b.text() for b in w.nav_buttons][1:3], ["触发步进扫描", "列表扫描"])
        w.controls["sweep.mode"].setCurrentIndex(1)
        self.assertEqual(w.controls["list_sweep.mode"].currentData(), "CONTINUE")
        with patch.object(w.session, "query") as query, patch.object(w.session, "write") as write:
            for index, kind in ((2, "LIST"), (1, "STEP"), (2, "LIST")):
                w._select_page(index)
                self.assertEqual(w._sweep_config().kind, kind)
                layout = w._scan_layouts[kind]
                self.assertIs(layout.itemAt(layout.count() - 1).widget(), w.scan_monitor_widget)
            query.assert_not_called()
            write.assert_not_called()
        self.assertTrue(w.pages.widget(2).isAncestorOf(w.table))
        self.assertFalse(w.pages.widget(1).isAncestorOf(w.table))

    def test_list_and_step_start_require_the_matching_applied_configuration(self):
        self.connect()
        w = self.window
        w.apply_sweep("LIST")
        self.idle()
        self.assertEqual(w.driver.prepared.kind, "LIST")
        with patch.object(w.session, "write", wraps=w.session.write) as write:
            w.start_sweep("STEP")
            w.start_sweep("STEP", single=True)
            write.assert_not_called()
        self.assertIn("请先应用配置", w.log.toPlainText())
        w.start_sweep("LIST")
        self.idle()
        self.assertEqual(w.session.query(":SWEep:TYPE?"), "LIST")
        self.assertEqual(w.driver.state, "enabled")
        w._safety(w.driver.stop)
        self.idle()

    def test_legacy_scan_parameters_migrate_then_persist_independently(self):
        w = self.window
        w.controls["sweep.point_trigger"].setCurrentIndex(2)
        w.controls["sweep.mode"].setCurrentIndex(1)
        w.table.item(0, 0).setText("1200000000")
        saved = w._device_parameter_snapshot()
        saved["controls"] = {k:v for k,v in saved["controls"].items() if not k.startswith("named/ssg.list_sweep.")}
        saved["controls"]["named/ssg.sweep.kind"] = {"type": "combo", "data": "LIST"}
        self.parameters.save(self.record.id, kind=self.record.kind.value, parameters=saved)
        restored = self.open_window()
        self.assertFalse(restored.session.is_connected)
        self.assertEqual(restored.controls["list_sweep.point_trigger"].currentData(), "BUS")
        self.assertEqual(restored.controls["list_sweep.mode"].currentData(), "SINGLE")
        self.assertEqual(restored.table.item(0, 0).text(), "1200000000")
        restored.controls["list_sweep.point_trigger"].setCurrentIndex(3)
        restored._save_device_parameters(force=True)
        again = self.open_window()
        self.assertEqual(again.controls["sweep.point_trigger"].currentData(), "BUS")
        self.assertEqual(again.controls["list_sweep.point_trigger"].currentData(), "EXT")

    def test_invalid_list_draft_does_not_block_step_configuration(self):
        self.connect()
        self.window.table.item(0, 0).setText("invalid list draft")
        self.window.apply_sweep("STEP")
        self.idle()
        self.assertEqual(self.window.driver.prepared.kind, "STEP")
        self.assertEqual(self.window.session.query(":SWEep:STATe?"), "OFF")

    def test_voltage_input_is_applied_as_dbm(self):
        self.connect()
        self.window.power.set_unit("mVpp")
        self.window.power.spin.setValue(200)
        self.window.apply_power()
        self.idle()
        self.assertAlmostEqual(float(self.window.session.query(":POWer?")), -10)
        self.assertEqual(self.window.session.query(":OUTPut?"), "0")
        self.assertIn("200 mVpp", self.window.rf_status.text())

    def test_list_voltage_upload_and_readback_preserve_selected_unit(self):
        self.connect()
        w = self.window
        w.list_power_unit.setCurrentIndex(w.list_power_unit.findData("mVpp"))
        w.table.item(0, 1).setText("200")
        w.table.item(1, 1).setText("20")
        w._select_page(2)
        w.apply_sweep()
        self.idle()
        self.assertEqual([round(p.power_dbm) for p in w.driver.read_list()], [-10, -30])
        w.read_list()
        self.idle()
        self.assertEqual(w.list_power_unit.currentData(), "mVpp")
        self.assertAlmostEqual(float(w.table.item(0, 1).text()), 200)
        self.assertAlmostEqual(float(w.table.item(1, 1).text()), 20)

    def test_voltage_unit_and_list_values_are_restored_without_writes(self):
        w = self.window
        w.power.set_unit("mV")
        w.power.spin.setValue(100)
        w.controls["sweep.start_dbm"].set_unit("mVpp")
        w.list_power_unit.setCurrentIndex(w.list_power_unit.findData("mVpp"))
        w.table.item(0, 1).setText("200")
        w.add_row(SsgPoint(1.2e9, -110))
        w.add_row(SsgPoint(1.3e9, 20))
        w._save_device_parameters(force=True)
        restored = self.open_window()
        self.assertFalse(restored.session.is_connected)
        self.assertEqual(restored.power.current_unit(), "mV")
        self.assertAlmostEqual(restored.power.display_value(), 100)
        self.assertEqual(restored.controls["sweep.start_dbm"].current_unit(), "mVpp")
        self.assertEqual(restored.list_power_unit.currentData(), "mVpp")
        self.assertEqual(restored.table.item(0, 1).text(), "200")
        self.assertAlmostEqual(restored._table_points()[0].power_dbm, -10)
        self.assertEqual(restored._table_points()[2].power_dbm, -110)
        self.assertEqual(restored._table_points()[3].power_dbm, 20)

    def test_old_dbm_snapshot_restores_into_unit_editors(self):
        self.parameters.save(self.record.id, kind=self.record.kind.value, parameters={
            "binding_version": 2, "controls": {"named/ssg.rf.power": {"type": "double", "value": -33.5},
                "named/ssg.sweep.start_dbm": {"type": "double", "value": -40}},
            "custom": {"list": [["1000000000", "-35", "0.03"]]}})
        restored = self.open_window()
        self.assertEqual(restored.power.current_unit(), "dBm")
        self.assertEqual(restored.power.value(), -33.5)
        self.assertEqual(restored.controls["sweep.start_dbm"].value(), -40)
        self.assertEqual(restored.list_power_unit.currentData(), "dBm")
        self.assertEqual(restored._table_points()[0].power_dbm, -35)

    def test_invalid_list_voltage_blocks_unit_change_without_partial_conversion(self):
        w = self.window
        w.table.item(1, 1).setText("bad")
        first = w.table.item(0, 1).text()
        w.list_power_unit.setCurrentIndex(w.list_power_unit.findData("mV"))
        self.assertEqual(w.list_power_unit.currentData(), "dBm")
        self.assertEqual(w.table.item(0, 1).text(), first)
        self.assertEqual(w.table.item(1, 1).text(), "bad")
        self.assertIn("单位未切换", w.log.toPlainText())

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

    def arm_external_scan(self):
        self.connect()
        self.window.poll_timer.stop()
        control = self.window.controls["sweep.point_trigger"]
        control.setCurrentIndex(control.findData("EXT"))
        self.window.apply_sweep()
        self.idle()
        self.window.start_sweep()
        self.idle()
        self.assertEqual(self.window.driver.state, "enabled", self.window.log.toPlainText())

    def test_scan_monitor_reads_current_values_without_inventing_trigger_counts(self):
        self.arm_external_scan()
        original = self.window.session.query
        calls = []
        def response(command, **kwargs):
            calls.append(command)
            if command == ":SWEep:CURRent:DATA?":
                raise OSError("COMBINED_QUERY_NOT_SUPPORTED")
            values = {":SWEep:CURRent:FREQuency?": "1234000000", ":SWEep:CURRent:LEVel?": "-31"}
            return values[command] if command in values else original(command, **kwargs)
        self.window.session.query = response
        with patch.object(self.window.session, "write") as writes:
            self.window.refresh_scan_preview()
            self.idle()
            writes.assert_not_called()
        current = self.window._current_scan_preview
        self.assertEqual(current["frequency_hz"], 1234000000)
        self.assertEqual(current["power_dbm"], -31)
        self.assertIn("最近回读", self.window.scan_preview_status.text())
        self.assertEqual(self.window.scan_metrics["received"].text(), "未知")
        self.assertEqual(self.window.scan_metrics["backlog"].text(), "未知")
        self.assertNotIn(":SWEep:CURRent:DATA?", calls)
        report = json.loads(self.window._write_diagnostics().read_text(encoding="utf-8"))
        self.assertIsNone(report["scan_monitor"]["trigger_received"])
        self.assertIsNone(report["scan_monitor"]["trigger_backlog"])
        self.assertEqual(report["scan_monitor"]["current_values"], current)
        calls.clear()
        self.window.refresh()
        self.idle()
        self.assertFalse(any(c.startswith(":SWEep:CURRent:") for c in calls))
        self.assertEqual(self.window._current_scan_preview, current)
        self.window.session.query = original

    def test_auto_scan_preview_is_opt_in_and_stop_clears_preview(self):
        self.arm_external_scan()
        self.assertFalse(self.window.scan_preview_auto.isChecked())
        with patch.object(self.window.session, "query", wraps=self.window.session.query) as query:
            self.window._poll()
            self.idle()
            self.assertFalse(any(c.args[0].startswith(":SWEep:CURRent:") for c in query.call_args_list))
            self.window.scan_preview_auto.setChecked(True)
            self.window._poll()
            self.idle()
            self.assertIsNotNone(self.window._current_scan_preview)
            self.assertTrue(any(c.args[0] == ":SWEep:CURRent:FREQuency?" for c in query.call_args_list))
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.window.scan_preview_auto.isChecked())
        self.assertIsNone(self.window._current_scan_preview)
        self.assertEqual(self.window.scan_metrics["current"].text(), "未知")

    def test_slow_preview_keeps_home_status_and_stop_button_responsive(self):
        from PyQt6.QtCore import QTimer
        from rigol_control.ui.main_window import DeviceCenterWindow
        self.arm_external_scan()
        repository = DeviceRepository(self.tmp.name)
        repository.save(self.record)
        home = DeviceCenterWindow(repository, self.manager)
        home.connection_timer.stop()
        home.open_windows[self.record.id] = self.window
        backend = self.window.session._backend
        original = backend.query
        entered, release = threading.Event(), threading.Event()
        def slow(command):
            if command == ":SWEep:CURRent:FREQuency?":
                entered.set()
                release.wait(3)
                raise OSError("SLOW_PREVIEW_TIMEOUT")
            return original(command)
        backend.query = slow
        watchdog = threading.Timer(3, release.set)
        watchdog.start()
        try:
            self.window.refresh_scan_preview()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            home._sync_connection_states()
            self.assertLess(time.monotonic() - started, .3)
            next(b for b in self.window.findChildren(QPushButton) if b.text() == "停止扫描").click()
            self.assertTrue(self.window._pending_safety)
            self.assertFalse(release.is_set())
            # Only the GUI event loop can fire this timer and unblock the query.
            QTimer.singleShot(20, release.set)
            self.idle()
            self.assertTrue(release.is_set())
            self.assertEqual(self.window.driver.state, "off")
            self.assertFalse(self.manager.active_leases())
            self.assertIn("SLOW_PREVIEW_TIMEOUT", self.window.log.toPlainText())
        finally:
            release.set()
            watchdog.cancel()
            backend.query = original
            self.idle()
            home.open_windows.clear()
            home.close()

    def test_bad_scan_preview_reply_disables_auto_and_invalidates_last_value(self):
        self.arm_external_scan()
        self.window.refresh_scan_preview()
        self.idle()
        self.assertIsNotNone(self.window._current_scan_preview)
        original = self.window.session.query
        def response(command, **kwargs):
            return "nan" if command == ":SWEep:CURRent:LEVel?" else original(command, **kwargs)
        self.window.session.query = response
        self.window.scan_preview_auto.setChecked(True)
        self.window._poll()
        self.idle()
        self.assertFalse(self.window.scan_preview_auto.isChecked())
        self.assertIsNone(self.window._current_scan_preview)
        self.assertTrue(self.window.session.is_connected)
        self.assertIn("当前扫描值无法解析", self.window.log.toPlainText())
        self.window.session.query = original

    def test_auto_preview_and_values_are_not_restored_as_device_settings(self):
        self.window.scan_preview_auto.setChecked(True)
        self.window._save_device_parameters(force=True)
        restored = self.open_window()
        self.assertFalse(restored.scan_preview_auto.isChecked())
        self.assertIsNone(restored._current_scan_preview)
        self.assertEqual(restored.scan_metrics["received"].text(), "未知")

    def test_current_point_timeout_is_opt_in_and_stop_reconnects_without_full_refresh(self):
        original = self.window.session.query
        calls = []
        def query(command, **kwargs):
            calls.append(command)
            if command == ":SWEep:CURRent:FREQuency?":
                raise OSError("CURRENT_POINT_TIMEOUT")
            return original(command, **kwargs)
        self.window.session.query = query
        self.arm_external_scan()
        self.window._poll()
        self.idle()
        self.assertNotIn(":SWEep:CURRent:DATA?", calls)
        self.assertNotIn(":SWEep:CURRent:FREQuency?", calls)
        next(b for b in self.window.findChildren(QPushButton) if b.text() == "刷新当前值预览").click()
        self.idle()
        self.assertFalse(self.window.session.is_connected)
        self.assertTrue(self.manager.active_leases())
        self.assertIn("CURRENT_POINT_TIMEOUT", self.window.log.toPlainText())
        calls.clear()
        original_write = self.window.session.write
        writes = []
        def write(command, **kwargs):
            writes.append(command)
            return original_write(command, **kwargs)
        self.window.session.write = write
        next(b for b in self.window.findChildren(QPushButton) if b.text() == "停止扫描").click()
        self.idle()
        self.assertTrue(self.window.session.is_connected)
        self.assertEqual(writes, [":SWEep:STATe OFF"])
        self.assertEqual(calls, [":SWEep:STATe?"])
        self.assertEqual(self.window.driver.state, "off")
        self.assertFalse(self.manager.active_leases())
        self.assertIn("扫描已停止", self.window.sweep_status.text())
        self.assertIsNone(self.window.driver.prepared)
        self.window.session.query = original

    def test_stop_with_enabled_readback_keeps_running_lease(self):
        self.arm_external_scan()
        original = self.window.session.write
        self.window.session.write = lambda command, **kwargs: None if command == ":SWEep:STATe OFF" else original(command, **kwargs)
        try:
            self.window._safety(self.window.driver.stop)
            self.idle()
            self.assertEqual(self.window.driver.state, "enabled")
            self.assertTrue(self.window.driver.scan_may_be_active)
            self.assertTrue(self.manager.active_leases())
            self.assertIn("停止未确认", self.window.log.toPlainText())
        finally:
            self.window.session.write = original
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())

    def test_stop_queued_during_failed_poll_recovers_and_does_not_query_power(self):
        self.arm_external_scan()
        original = self.window.session.query
        def fail(command, **kwargs):
            if command == ":POWer?":
                raise OSError("POLL_POWER_TIMEOUT")
            return original(command, **kwargs)
        self.window.session.query = fail
        self.window._poll()
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertIn("POLL_POWER_TIMEOUT", self.window.log.toPlainText())
        self.assertTrue(self.window.session.is_connected)
        self.assertEqual(self.window.driver.state, "off")
        self.assertFalse(self.manager.active_leases())
        self.assertIn("扫描已停止", self.window.statusBar().currentMessage())
        self.window.session.query = original

    def test_stop_readback_failure_keeps_lease_without_replaying_stop(self):
        self.arm_external_scan()
        original_query, original_write = self.window.session.query, self.window.session.write
        writes = []
        def write(command, **kwargs):
            writes.append(command)
            return original_write(command, **kwargs)
        def query(command, **kwargs):
            if command == ":SWEep:STATe?":
                raise OSError("STOP_CONFIRMATION_TIMEOUT")
            return original_query(command, **kwargs)
        self.window.session.write, self.window.session.query = write, query
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertEqual(writes, [":SWEep:STATe OFF"])
        self.assertTrue(self.window.driver.scan_may_be_active)
        self.assertTrue(self.manager.active_leases())
        self.assertFalse(self.window.session.is_connected)
        self.window.session.query = original_query
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertEqual(writes, [":SWEep:STATe OFF", ":SWEep:STATe OFF"])
        self.assertFalse(self.manager.active_leases())

    def test_reconnected_stop_rejects_changed_identity_without_writing(self):
        self.arm_external_scan()
        backend = self.window.session._backend
        identity = backend.identity
        self.window.session.disconnect()
        backend.identity = "SIGLENT,SSG3021X,DIFFERENT_INSTRUMENT,1"
        before = len(backend.commands)
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertEqual(backend.commands[before:], ["*IDN?"])
        self.assertIn("重连身份不一致", self.window.log.toPlainText())
        self.assertTrue(self.manager.active_leases())
        backend.identity = identity
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())

    def test_rf_off_reconnects_without_releasing_unconfirmed_scan(self):
        self.arm_external_scan()
        self.window.session.disconnect()
        self.window.driver.state = "unknown"
        self.window._safety(lambda: self.window.driver.set_output(False))
        self.idle()
        self.assertTrue(self.window.session.is_connected)
        self.assertEqual(self.window.session.query(":OUTPut?"), "0")
        self.assertTrue(self.window.driver.scan_may_be_active)
        self.assertTrue(self.manager.active_leases())
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())

    def test_invalid_table_error_stays_local(self):
        self.connect()
        self.window._select_page(2)
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

    def test_initial_readback_timeout_releases_lease_and_allows_close(self):
        original = self.window.session.query
        def fail(command, **kwargs):
            if command == ":SWEep:STATe?":
                raise OSError("USB timeout reading scan status")
            return original(command, **kwargs)
        self.window.session.query = fail
        self.window.connect_device()
        self.idle()
        self.assertTrue(self.window.driver.caps.known)
        self.assertFalse(self.window.driver.scan_may_be_active)
        self.assertFalse(self.manager.active_leases())
        self.assertIn("状态读取失败", self.window.log.toPlainText())
        reports = list((Path(self.tmp.name) / "diagnostics").glob("siglent-*.json"))
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["resource"], self.record.resource)
        self.assertEqual(report["model"], "SSG6082A-V")
        self.assertIn("USB timeout reading scan status", report["log"])
        self.assertTrue(self.window.close())
        self.manager.disconnect(self.record)

    def test_connect_button_bad_power_reply_is_contained_and_recorded(self):
        original = self.window.session.query
        button = next(b for b in self.window.findChildren(QPushButton) if b.text() == "连接 / 读取身份")
        try:
            for reply in ("INVALID_POWER", "", "nan", "inf", "1e309", "-1000000"):
                def response(command, **kwargs):
                    if command == "*IDN?":
                        return "Siglent Technologies,SSG3021X,SSG3_TEST,3.1.21"
                    return reply if command == ":POWer?" else original(command, **kwargs)
                self.window.session.query = response
                with self.subTest(reply=reply), patch("sys.excepthook") as uncaught:
                    button.click()
                    self.idle()
                    uncaught.assert_not_called()
                    self.assertTrue(self.window.session.is_connected)
                    self.assertFalse(self.manager.active_leases())
                    self.assertIn("[:POWer?]", self.window.log.toPlainText())
                    report = json.loads(next((Path(self.tmp.name) / "diagnostics").glob("siglent-*.json")).read_text(encoding="utf-8"))
                    self.assertEqual(report["model"], "SSG3021X")
                    self.assertEqual(json.loads(report["readback"])[":POWer"], reply)
                    self.assertIn("Traceback", report["log"])
        finally:
            self.window.session.query = original
        self.window.refresh()
        self.idle()
        self.assertIn("dBm", self.window.rf_status.text())
        self.assertTrue(self.window.close())

    def test_result_callback_failure_releases_idle_lease_and_runs_queued_safety(self):
        self.connect()
        self.window._run(lambda: self.window.driver.set_output(True))
        self.idle()
        def callback(_result):
            self.window._safety(lambda: self.window.driver.set_output(False))
            raise ValueError("callback failure")
        with patch("sys.excepthook") as uncaught, patch.object(self.window.session, "write", wraps=self.window.session.write) as writes:
            self.window._run(lambda: None, callback=callback)
            self.idle()
            uncaught.assert_not_called()
            self.assertEqual([call.args[0] for call in writes.call_args_list], [":OUTPut OFF"])
        self.assertEqual(self.window.session.query(":OUTPut?"), "0")
        self.assertFalse(self.manager.active_leases())
        self.assertIn("callback failure", self.window.log.toPlainText())

    def test_display_failure_keeps_running_scan_lease_until_confirmed_stop(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        self.window._run(self.window.driver.start)
        self.idle()
        original = self.window.session.query
        self.window.session.query = lambda command, **kwargs: "INVALID_POWER" if command == ":POWer?" else original(command, **kwargs)
        try:
            with patch("sys.excepthook") as uncaught:
                self.window.refresh()
                self.idle()
                uncaught.assert_not_called()
            self.assertTrue(self.window.driver.scan_may_be_active)
            self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
            self.assertFalse(self.window.close())
            self.window._safety(self.window.driver.stop)
            self.idle()
            self.assertFalse(self.window.driver.scan_may_be_active)
            self.assertFalse(self.manager.active_leases())
        finally:
            self.window.session.query = original
        self.assertTrue(self.window.close())

    def test_readback_failure_after_confirmed_off_does_not_lock_window(self):
        self.connect()
        original = self.window.session.query
        def fail(command, **kwargs):
            if command == ":SWEep:MODE?":
                raise OSError("optional status query failed")
            return original(command, **kwargs)
        self.window.session.query = fail
        self.window.refresh()
        self.idle()
        self.assertFalse(self.manager.active_leases())
        self.assertIn("optional status query failed", self.window.log.toPlainText())
        self.assertIn(self.window.driver.caps.model, self.window.identity_label.text())
        self.assertTrue(self.window.close())

    def test_usb_modulation_timeouts_do_not_block_connect_rf_or_scan_poll(self):
        original = self.window.session.query
        calls = []
        blocked = (":MODULATION", ":OUTPUT:MODULATION", ":AM:", ":FM:", ":PM:", ":PULM:")
        def response(command, **kwargs):
            calls.append(command)
            if command == "*IDN?":
                return "Siglent Technologies,SSG3021X,USB_TEST,3.1.21"
            if command.upper().startswith(blocked):
                raise OSError("VI_ERROR_TMO (-1073807339): Timeout expired before operation completed")
            return original(command, **kwargs)
        self.window.session.query = response
        try:
            button = next(b for b in self.window.findChildren(QPushButton) if b.text() == "连接 / 读取身份")
            button.click()
            self.idle()
            self.assertTrue(self.window.session.is_connected)
            self.assertIn("USB_TEST", self.window.identity_label.text())
            self.assertIn("尚未读取", self.window.mod_status.text())
            self.window.power.setValue(-30)
            self.window.apply_power()
            self.idle()
            self.assertIn("-30 dBm", self.window.rf_status.text())
            self.window.apply_sweep()
            self.idle()
            self.window.start_sweep()
            self.idle()
            self.window._poll()
            self.idle()
            self.window._safety(self.window.driver.stop)
            self.idle()
            self.assertTrue(self.window.session.is_connected)
            self.assertFalse(self.manager.active_leases())
            self.assertFalse(any(c.upper().startswith(blocked) for c in calls))
            self.assertNotIn("错误", self.window.log.toPlainText())
        finally:
            self.window.session.query = original

    def test_read_modulation_button_uses_primary_command_without_applying_edits(self):
        original = self.window.session.query
        calls = []
        def response(command, **kwargs):
            calls.append(command)
            if command == "*IDN?":
                return "Siglent Technologies,SSG3021X,USB_TEST,3.1.21"
            if command.upper() == ":OUTPUT:MODULATION?":
                raise OSError("VI_ERROR_TMO: equivalent form did not reply")
            return original(command, **kwargs)
        self.window.session.query = response
        try:
            self.connect()
            self.window.controls["mod.am"].setChecked(True)
            self.window.controls["mod.master"].setChecked(True)
            calls.clear()
            with patch.object(self.window.session, "write", wraps=self.window.session.write) as writes:
                button = next(b for b in self.window.findChildren(QPushButton) if b.text() == "读取调制状态")
                button.click()
                self.idle()
                writes.assert_not_called()
            self.assertIn(":MODulation?", calls)
            self.assertNotIn(":OUTPut:MODulation?", calls)
            self.assertIn("调制总开关=0", self.window.mod_status.text())
            self.assertTrue(self.window.controls["mod.master"].isChecked())
            self.window.apply_modulation()
            self.idle()
            self.assertIn("调制总开关=1", self.window.mod_status.text())
            previous = self.window.mod_status.text()
            calls.clear()
            self.window.refresh()
            self.idle()
            self.assertEqual(self.window.mod_status.text(), previous)
            self.assertNotIn(":MODulation?", calls)
            self.assertEqual(original(":OUTPut?"), "0")
        finally:
            self.window.session.query = original

    def test_explicit_modulation_timeout_is_reported_and_basic_reconnect_still_works(self):
        original = self.window.session.query
        failed = []
        def response(command, **kwargs):
            if command == "*IDN?":
                return "Siglent Technologies,SSG3021X,USB_TEST,3.1.21"
            if command == ":MODulation?":
                failed.append(command)
                raise OSError("VI_ERROR_TMO: primary modulation query timed out")
            return original(command, **kwargs)
        self.window.session.query = response
        try:
            self.connect()
            self.window.refresh_modulation()
            self.idle()
            self.assertEqual(failed, [":MODulation?"])
            self.assertIn("VI_ERROR_TMO", self.window.log.toPlainText())
            self.assertIn("USB_TEST", self.window.identity_label.text())
            self.assertFalse(self.window.session.is_connected)
            self.assertFalse(self.manager.active_leases())
            self.assertIn("尚未读取", self.window.mod_status.text())
            self.window.connect_device()
            self.idle()
            self.assertTrue(self.window.session.is_connected)
            self.assertEqual(failed, [":MODulation?"])
        finally:
            self.window.session.query = original

    def test_instrument_rejection_keeps_connection_without_scan_lease(self):
        self.connect()
        original = self.window.session.query
        def stale_error(command, **kwargs):
            return "16" if command == "*ESR?" else original(command, **kwargs)
        self.window.session.query = stale_error
        self.window.apply_power()
        self.idle()
        self.assertIn("历史 SCPI 错误", self.window.log.toPlainText())
        self.assertTrue(self.window.session.is_connected)
        self.assertFalse(self.manager.active_leases())
        self.window.session.query = original

    def test_failed_scan_start_keeps_lease_until_off_is_confirmed(self):
        self.connect()
        self.window.apply_sweep()
        self.idle()
        original = self.window.session.write
        def fail(command, **kwargs):
            original(command, **kwargs)
            if command == ":SWEep:STATe FREQUENCY":
                raise OSError("USB lost after scan enable")
        self.window.session.write = fail
        self.window._run(self.window.driver.start)
        self.idle()
        self.assertTrue(self.window.driver.scan_may_be_active)
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        self.assertFalse(self.window.close())
        self.window.session.write = original
        self.window.connect_device()
        self.idle()
        self.window._safety(self.window.driver.stop)
        self.idle()
        self.assertFalse(self.manager.active_leases())

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
