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
from rigol_control.drivers.newport_smc100 import MotionStatus, Smc100Device, VIRTUAL_SMC100_RESOURCE
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
        w._tick()
        self.assertEqual(w.scan_actual_value.text(), "0.000000°")
        self.assertEqual(w.scan_target_value.text(), "0.000000°")
        self.assertEqual(w.scan_point_value.text(), "P0")
        self.assertEqual(w.scan_table_model.rowCount(), 3)
        self.assertFalse(w.scan_refresh_button.isEnabled())
        for k in range(1, 5):
            QTest.mouseClick(w.inject_button, Qt.MouseButton.LeftButton)
            self.assertTrue(self.wait(lambda: w.controller.snapshot().completed == k), w.controller.snapshot())
            w._tick()
            self.assertEqual(w.scan_point_value.text(), f"P{k % 3}")
            self.assertEqual(w.scan_actual_value.text(), f"{(k % 3) * 0.1:.6f}°")
            self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "0.000000")
            self.assertIn(f"完成回跳 {k // 3}", w.scan_counts.text())
        self.assertEqual(device.move_history, [0.1, 0.2, 0.0, 0.1])
        QTest.mouseClick(w.stop_button, Qt.MouseButton.LeftButton)
        self.assertTrue(self.wait(lambda: not w.controller.is_running))
        self.assertIsNone(self.manager.lease_owner(self.ni.id))
        run = w.controller.run_directory
        self.assertTrue((run / "analog.csv").exists())
        self.assertTrue((run / "rotation.csv").exists())
        w.counter_name.setText("ctr1")
        w.terminal.setText("PFI3")
        w.edge.setCurrentIndex(1)
        w.tolerance.setValue(0.0123)
        w.settle.setValue(0.0456)
        w.timeout.setValue(12.3)
        w._save_device_parameters(force=True)
        restored = self.window()
        self.assertEqual(restored.step.value(), 0.1)
        self.assertEqual(restored.count.value(), 3)
        self.assertEqual(restored.ni_combo.currentData(), self.ni.id)
        self.assertEqual(restored.counter_name.text(), "ctr1")
        self.assertEqual(restored.terminal.text(), "PFI3")
        self.assertFalse(restored.edge.currentData())
        self.assertEqual(restored.tolerance.value(), 0.0123)
        self.assertEqual(restored.settle.value(), 0.0456)
        self.assertEqual(restored.timeout.value(), 12.3)
        self.assertIsNone(restored.controller)
        self.assertEqual(len(device.move_history), 4)

    def test_scan_preview_reads_negative_position_without_polling_or_motion(self):
        w = self.window()
        w._select_page(1)
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "—")
        device = self.manager.newport_device_for(self.stage)
        with patch.object(device, "position", return_value=-5.2):
            QTest.mouseClick(w.scan_refresh_button, Qt.MouseButton.LeftButton)
            self.assertTrue(self.wait(lambda: not w._manual_busy))
        w.step.setValue(-0.1)
        w.count.setValue(3)
        self.assertEqual(w.scan_actual_value.text(), "-5.200000°")
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(2, 1)), "-5.400000")
        self.assertIn("预览", w.table_summary.text())
        with patch.object(device, "position", side_effect=AssertionError("UI tick must not query hardware")):
            for _ in range(5):
                w._tick()
        self.assertEqual(device.move_history, [])
        # Starting must capture the real current position, not the cached preview origin.
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._tick()
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "0.000000")
        self.assertEqual(w.scan_actual_value.text(), "0.000000°")

    def test_specified_initial_position_previews_offline_and_restores_without_motion(self):
        self.manager.disconnect(self.stage)
        w = self.window()
        self.assertEqual(w.initial_mode.currentData(), "current")
        w.initial_mode.setCurrentIndex(w.initial_mode.findData("specified"))
        w.initial_angle.setValue(-5.2)
        w.step.setValue(0.1)
        w.count.setValue(3)
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertEqual(w.scan_target_value.text(), "-5.200000°")
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(2, 1)), "-5.000000")
        self.assertFalse(w.move_initial_button.isEnabled())
        self.assertFalse(w.use_current_button.isEnabled())
        w._save_device_parameters(force=True)
        restored = self.window()
        self.assertEqual(restored.initial_mode.currentData(), "specified")
        self.assertEqual(restored.initial_angle.value(), -5.2)
        self.assertEqual(restored.scan_table_model.data(restored.scan_table_model.index(0, 1)), "-5.200000")
        self.assertIsNone(restored.controller)
        self.assertEqual(w.device.move_history, [])
        # A failed readiness check must retain the known P0 preview as well.
        self.manager.connect(self.stage)
        with patch.object(w.device, "status", return_value=MotionStatus(0, 0x0A)):
            w._start_scan()
            self.assertTrue(self.wait(lambda: w.controller is not None and not w.controller.is_running))
        w._tick()
        self.assertIn("READY", w.scan_status.text())
        self.assertIn("起点预览", w.table_summary.text())
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "-5.200000")
        self.assertEqual(w.scan_target_value.text(), "-5.200000°")
        self.assertEqual(w.device.move_history, [])

    def test_use_current_reads_fresh_position_and_only_sets_initial_parameter(self):
        w = self.window()
        w._last_position = 9.0
        with patch.object(w.device, "position", return_value=-1.234567):
            w.use_current_button.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.initial_mode.currentData(), "specified")
        self.assertEqual(w.initial_angle.value(), -1.234567)
        self.assertEqual(w.scan_actual_value.text(), "-1.234567°")
        self.assertEqual(w.scan_target_value.text(), "-1.234567°")
        self.assertEqual(w.device.move_history, [])

    def test_move_to_specified_initial_then_trigger_cycle(self):
        w = self.window()
        w.initial_mode.setCurrentIndex(w.initial_mode.findData("specified"))
        w.initial_angle.setValue(-0.2)
        w.step.setValue(0.1)
        w.count.setValue(3)
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller is not None and not w.controller.is_running))
        w._tick()
        self.assertIn("初始位置", w.scan_status.text())
        self.assertEqual(w.device.move_history, [])
        self.assertIsNone(self.manager.lease_owner(self.ni.id))
        w.move_initial_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.scan_actual_value.text(), "-0.200000°")
        self.assertEqual(w.device.move_history, [-0.2])
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._tick()
        self.assertEqual(w.device.move_history, [-0.2])
        self.assertFalse(w.initial_angle.isEnabled())
        self.assertFalse(w.initial_mode.isEnabled())
        self.assertFalse(w.move_initial_button.isEnabled())
        self.assertFalse(w.use_current_button.isEnabled())
        for k, target in enumerate((-0.1, 0.0, -0.2), 1):
            w._inject()
            self.assertTrue(self.wait(lambda: w.controller.snapshot().completed == k))
            w._tick()
            self.assertAlmostEqual(w.device.position(), target)
            self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "-0.200000")

    def test_stopped_plan_is_retained_until_explicit_refresh_or_edit(self):
        w = self.window()
        w.step.setValue(0.1)
        w.count.setValue(3)
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._inject()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().completed == 1))
        w._stop()
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
        w._tick()
        self.assertIn("上次运行", w.table_summary.text())
        self.assertEqual(w.scan_actual_value.text(), "0.100000°")
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "0.000000")
        w.scan_refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertIn("预览", w.table_summary.text())
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "0.100000")
        w.step.setValue(-0.2)
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(2, 1)), "-0.300000")
        self.assertEqual(w.scan_point_value.text(), "—")
        self.assertEqual(self.manager.newport_device_for(self.stage).move_history, [0.1])

    def test_disconnect_and_read_failure_do_not_display_stale_position(self):
        w = self.window()
        w.scan_refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.scan_actual_value.text(), "0.000000°")
        self.manager.disconnect(self.stage)
        w._tick()
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "—")
        self.manager.connect(self.stage)
        w._tick()
        self.assertEqual(w.scan_actual_value.text(), "—")
        with patch.object(w.device, "position", side_effect=RuntimeError("read failed")):
            w.scan_refresh_button.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertIn("read failed", w.scan_status.text())

    def test_scan_disconnect_preserves_fault_but_reconnect_does_not_reuse_position(self):
        w = self.window()
        w.step.setValue(0.1)
        w.count.setValue(3)
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._inject()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().completed == 1))
        w.device.disconnect()
        self.assertTrue(self.wait(lambda: not w.controller.is_worker_running))
        w._tick()
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertIn("错误停止", w.scan_status.text())
        self.assertIn("未连接", w.scan_status.text())
        self.assertIn("完成步进 1", w.scan_counts.text())
        self.assertEqual(w.scan_table_model.data(w.scan_table_model.index(0, 1)), "0.000000")
        self.assertTrue(w.controller.cleanup_pending)
        self.assertEqual(self.manager.lease_owner(self.stage.id), w.controller.owner)
        self.assertFalse(w.connect_button.isEnabled())
        self.assertIn("重连并停止", w.stop_button.text())
        move_history = list(w.device.move_history)
        w.stop_button.click()
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
        self.assertEqual(w.device.move_history, move_history)
        self.assertTrue(self.manager.is_connected(self.stage))
        self.assertIsNone(self.manager.lease_owner(self.stage.id))
        w._tick()
        self.assertEqual(w.scan_actual_value.text(), "—")
        self.assertIn("错误停止", w.scan_status.text())
        w.scan_refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy))
        self.assertEqual(w.scan_actual_value.text(), "0.100000°")
        self.assertIn("预览", w.table_summary.text())

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

    def test_manual_dispatch_rejection_restores_completion_and_allows_close(self):
        w = self.window()
        with patch.object(w.thread_pool, "start", side_effect=RuntimeError("REPRO_POOL_REJECTED")):
            w.refresh_button.click()
        self.assertFalse(w._manual_busy)
        self.assertTrue(w._manual_done.is_set())
        self.assertFalse(w._call_tasks)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))
        self.assertTrue(w.close())

    def test_disconnect_dispatch_rejection_does_not_leave_a_phantom_operation(self):
        w = self.window()
        with patch.object(w.thread_pool, "start", side_effect=RuntimeError("REPRO_DISCONNECT_REJECTED")):
            w.disconnect_button.click()
        self.assertFalse(w._manual_busy)
        self.assertTrue(w._manual_done.is_set())
        self.assertTrue(self.manager.is_connected(self.stage))
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_window_connect_button_can_connect_an_initially_disconnected_stage(self):
        self.manager.disconnect(self.stage)
        w = self.window()
        w.connect_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertTrue(self.manager.is_connected(self.stage), w.operation_label.text())
        self.assertEqual(w.device.move_history, [])
        self.assertEqual(w.device.stop_count, 0)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_ready_home_rejection_keeps_live_position_without_sending_stop(self):
        w = self.window()
        w.refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        w._manual(w.device.home, wait_motion=True)
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertEqual(w.device.stop_count, 0)
        self.assertEqual(w.device.move_history, [])
        self.assertEqual(w._last_position, 0)
        self.assertNotIn("读取失败", w.position_label.text())
        self.assertIn("0x33", w.status_label.text())
        self.assertTrue(w.move_button.isEnabled())
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_disable_recovery_is_explicit_and_does_not_home_or_move(self):
        w = self.window()
        w.device.set_virtual_state(0x3C)
        w.refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertIn("0x3C", w.status_label.text())
        self.assertFalse(w.move_button.isEnabled())
        self.assertTrue(w.enable_button.isEnabled())
        self.assertEqual(w.device.enable_count, 0)
        w.enable_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertTrue(w.device.status().ready)
        self.assertEqual(w.device.enable_count, 1)
        self.assertEqual(w.device.home_count, 0)
        self.assertEqual(w.device.move_history, [])
        self.assertTrue(w.move_button.isEnabled())
        self.assertFalse(w.enable_button.isEnabled())

    def test_uncertain_manual_stop_retains_lease_until_explicit_retry(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        with patch.object(w.device, "move_absolute", side_effect=MotionError("REPRO_UNCERTAIN_PA")) as move:
            with patch.object(w.device, "stop", side_effect=MotionError("REPRO_STOP_FAILED")):
                w._move(.1)
                self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
            self.assertTrue(w._manual_cleanup_pending)
            self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
            self.assertFalse(w.move_button.isEnabled())
            self.assertFalse(w.disconnect_button.isEnabled())
            self.assertFalse(w.close())
            self.assertEqual(move.call_count, 1)
            w.sidebar_stop.click()
            self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
            self.assertFalse(w._manual_cleanup_pending)
            self.assertIsNone(self.manager.lease_owner(self.stage.id))
            self.assertEqual(move.call_count, 1)

    def test_disconnected_manual_stop_retry_reconnects_only_for_cleanup(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        def uncertain_stop():
            w.device.disconnect()
            raise MotionError("REPRO_USB_STOP_UNKNOWN")
        with patch.object(w.device, "move_absolute", side_effect=MotionError("REPRO_PA_UNKNOWN")) as move:
            with patch.object(w.device, "stop", side_effect=uncertain_stop):
                w._move(.1)
                self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
            self.assertFalse(w.device.is_connected)
            self.assertTrue(w._manual_cleanup_pending)
            self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
            self.assertFalse(w.connect_button.isEnabled())
            self.assertTrue(w.sidebar_stop.isEnabled())
            w.sidebar_stop.click()
            self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
            self.assertTrue(w.device.is_connected)
            self.assertIsNone(self.manager.lease_owner(self.stage.id))
            self.assertEqual(move.call_count, 1)
            self.assertEqual(w.device.home_count, 0)

    def test_rejected_cleanup_dispatch_preserves_pending_lease_and_completion(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        with patch.object(w.device, "move_absolute", side_effect=MotionError("REPRO_PA_UNKNOWN")), \
                patch.object(w.device, "stop", side_effect=MotionError("REPRO_STOP_UNKNOWN")):
            w._move(.1)
            self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        with patch.object(w.thread_pool, "start", side_effect=RuntimeError("REPRO_CLEANUP_POOL_REJECTED")):
            w.sidebar_stop.click()
        self.assertTrue(w._manual_done.is_set())
        self.assertTrue(w._manual_cleanup_pending)
        self.assertFalse(w._manual_busy)
        self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
        w.sidebar_stop.click()
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))

    def test_cancelled_manual_motion_attempts_stop_once_then_waits_for_explicit_retry(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        def cancel_after_command(_angle):
            w._cancel_manual.set()
        with patch.object(w.device, "move_absolute", side_effect=cancel_after_command) as move:
            with patch.object(w.device, "stop", side_effect=MotionError("REPRO_CANCEL_STOP_UNKNOWN")) as stop:
                w._move(.1)
                self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
                self.assertEqual(stop.call_count, 1)
            self.assertTrue(w._manual_cleanup_pending)
            self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
            w.sidebar_stop.click()
            self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
            self.assertEqual(move.call_count, 1)

    def test_unknown_state_after_stop_does_not_release_the_manual_lease(self):
        w = self.window()
        with patch.object(w.device, "status", return_value=MotionStatus(0, 0x99)):
            w.manual_stop.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertTrue(w._manual_cleanup_pending)
        self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
        self.assertIn("0x99", w.operation_label.text())
        self.assertFalse(w.move_button.isEnabled())
        w.sidebar_stop.click()
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))

    def _stationary_stop_without_illegal_st(self, state):
        class RefusingStopSerial:
            def __init__(self):
                self.is_open = True
                self.writes = []
                self.state = state
                self.error = "@"
                self.refuse_stop = True

            def reset_input_buffer(self):
                pass

            def write(self, payload):
                self.writes.append(payload)
                command = payload.decode("ascii").strip()[1:]
                if command == "ST":
                    self.error = "H" if self.refuse_stop else "@"
                return len(payload)

            def read_until(self, terminator, size):
                command = self.writes[-1].decode("ascii").strip()[1:]
                replies = {"VE": "SMC100CC V3.0", "ID?": "PR50CC", "TP": "0", "VA?": "20",
                           "TS": f"0000{self.state:02X}", "TE": self.error}
                return f"1{command[:2]}{replies[command]}\r\n".encode("ascii")

            def close(self):
                self.is_open = False

        self.manager.disconnect(self.stage)
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="Stationary PR50CC", resource="COM19::1")
        self.records = [self.stage, self.ni]
        serial = RefusingStopSerial()
        device = Smc100Device(self.stage.resource, serial_factory=lambda **kw: serial)
        with patch("rigol_control.connections.Smc100Device", return_value=device):
            self.manager.connect(self.stage)
        w = self.window()
        w.refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        serial.writes.clear()
        try:
            w.manual_stop.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
            self.assertFalse(w._manual_cleanup_pending, w.operation_label.text())
            self.assertIsNone(self.manager.lease_owner(self.stage.id))
            self.assertEqual(serial.writes, [b"1TS\r\n"])
            self.assertEqual(w.home_button.isEnabled(), state == 0x0A)
        finally:
            # Let a failing implementation clean this disposable fake fixture.
            if w._manual_cleanup_pending:
                serial.state, serial.refuse_stop, serial.error = 0x33, False, "@"
                w.sidebar_stop.click()
                self.wait(lambda: not w._busy() and not w._call_tasks)

    def test_stop_in_not_referenced_state_never_sends_illegal_st_or_blocks_homing(self):
        self._stationary_stop_without_illegal_st(0x0A)

    def test_stop_in_configuration_state_never_sends_illegal_st_or_retains_a_lease(self):
        self._stationary_stop_without_illegal_st(0x14)

    def test_fault_in_first_stop_status_still_attempts_one_stop_and_is_not_consumed(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        with patch.object(w.device, "status", side_effect=[MotionError("REPRO_FIRST_TS_HARDWARE_FAULT"), MotionStatus(0, 0x33)]):
            w.manual_stop.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertEqual(w.device.stop_count, 1)
        self.assertTrue(w._manual_cleanup_pending)
        self.assertEqual(self.manager.lease_owner(self.stage.id), w._manual_owner)
        self.assertIn("REPRO_FIRST_TS_HARDWARE_FAULT", w.operation_label.text())
        w.sidebar_stop.click()
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
        self.assertEqual(w.device.stop_count, 2)
        self.assertIn("REPRO_FIRST_TS_HARDWARE_FAULT", w.operation_label.text())

    def test_successful_status_refresh_keeps_the_consumed_hardware_error_visible(self):
        from rigol_control.drivers.newport_smc100 import MotionError
        w = self.window()
        with patch.object(w.device, "status", side_effect=MotionError("SMC100 运动错误 0x0040；状态 0x0B")):
            w.refresh_button.click()
            self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        w.refresh_button.click()
        self.assertTrue(self.wait(lambda: not w._manual_busy and not w._call_tasks))
        self.assertIn("0x0040", w.operation_label.text())
        self.assertIn("最近错误记录", w.operation_label.text())
        self.assertIn("0x33", w.status_label.text())
        self.assertTrue(w.move_button.isEnabled())

    def test_close_during_motion_stops_and_keeps_shared_connection(self):
        w = self.window()
        device = self.manager.newport_device_for(self.stage)
        device.set_velocity(0.1)
        w._start_scan()
        self.assertTrue(self.wait(lambda: w.controller.snapshot().phase == "holding"))
        w._inject()
        self.assertTrue(self.wait(lambda: device.status().moving))
        self.assertFalse(w.close())
        self.assertTrue(self.wait(lambda: not w._busy() and not w._call_tasks))
        self.assertTrue(w.close())
        self.windows.remove(w)
        self.assertFalse(device.status().moving)
        self.assertTrue(device.is_connected)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_home_factory_includes_virtual_newport(self):
        repo = DeviceRepository(Path(self.tmp.name) / "home")
        # This empty repository owns newly generated record IDs. Reusing the
        # fixture's connected stage/NI profiles would create physical aliases.
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repo, manager)
        self.windows.append(home)
        home.create_virtual_lab()
        self.assertTrue(self.wait(lambda: len(repo.list()) == 6 and all(manager.is_connected(r) for r in repo.list())))
        stage = next(r for r in repo.list() if r.kind is DeviceKind.NEWPORT_SMC100)
        home.open_device(stage.id)
        self.assertIsInstance(home.open_windows[stage.id], NewportWindow)


if __name__ == "__main__":
    unittest.main()
