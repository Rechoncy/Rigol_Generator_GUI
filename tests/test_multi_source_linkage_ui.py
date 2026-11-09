import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.drivers.siglent_ssg import VIRTUAL_SSG_RESOURCE
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.linkage import LinkageAxis, generate_linkage_points
from rigol_control.linkage_parameters import LinkageParameter, SsgLinkageMode
from rigol_control.linkage_tasks import LinkageTaskRecord
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState
from rigol_control.ui.linkage_window import LinkageAxisEditor, LinkageTaskWindow
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class MultiSourceLinkageUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["multi-source-linkage-ui-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = DeviceRepository(Path(self.temporary.name))
        self.manager = DeviceConnectionManager()
        self.dg = DeviceRecord.create(kind=DeviceKind.DG4000, name="DG", resource=VIRTUAL_DG_RESOURCE)
        self.vaunix = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="Vaunix", resource=VIRTUAL_VAUNIX_RESOURCE)
        self.ssg = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG", resource=VIRTUAL_SSG_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        for record in (self.dg, self.vaunix, self.ssg, self.ni):
            self.repository.save(record)
            self.manager.connect(record)
        self.windows = []
        self.release_events = []
        self.warning_patch = patch("rigol_control.ui.linkage_window.QMessageBox.warning")
        self.warning = self.warning_patch.start()

    def tearDown(self):
        for event in self.release_events:
            event.set()
        for window in reversed(self.windows):
            if isinstance(window, LinkageTaskWindow):
                self.assertTrue(self.wait_for(lambda: not window._call_tasks))
                window._stop()
                self.assertTrue(self.wait_for(lambda: not window._call_tasks))
            self.assertTrue(window.close())
        self.app.processEvents()
        self.manager.close_all()
        self.warning_patch.stop()
        self.temporary.cleanup()

    def wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            QTest.qWait(5)
        return bool(predicate())

    def window(self):
        window = LinkageTaskWindow(self.repository, self.manager)
        self.windows.append(window)
        return window

    def axis(self, window, record, parameter, unit, start, step, count=2):
        if len(window.axis_editors) == 1 and window.point_table.rowCount() == 0:
            editor = window.axis_editors[0]
        else:
            window._add_axis()
            editor = window.axis_editors[-1]
        editor.device.setCurrentIndex(editor.device.findData(record.id))
        editor.parameter.setCurrentIndex(editor.parameter.findData(parameter))
        editor.unit.setCurrentIndex(editor.unit.findData(unit))
        editor.start.setValue(start)
        editor.step.setValue(step)
        editor.count.setValue(count)
        # The helper may be called again before generating the final table.
        window.point_table.setRowCount(1)
        return editor

    def start(self, window):
        window._generate_table(show_message=False)
        window._start()
        self.assertTrue(self.wait_for(lambda: window.controller is not None
                                     and window.controller.snapshot().state is ControllerState.RUNNING
                                     and not window._call_tasks),
                        str(self.warning.call_args))

    def test_source_selector_includes_rf_generators_without_a_dg(self):
        self.repository.delete(self.dg.id)
        window = self.window()
        self.assertEqual({item.kind for item in window.generators},
                         {DeviceKind.VAUNIX_LMS, DeviceKind.SIGLENT_SSG})
        self.assertEqual(len(window.axis_editors), 1)
        editor = window.axis_editors[0]
        self.assertEqual({editor.parameter.itemData(i) for i in range(editor.parameter.count())},
                         {StepParameter.FREQUENCY, LinkageParameter.RF_POWER})
        self.assertEqual(editor.channel.count(), 1)
        self.assertFalse(editor.channel.isEnabled())

    def test_voltage_does_not_become_dbm_when_switching_source_kind(self):
        editor = LinkageAxisEditor([self.dg, self.vaunix, self.ssg])
        self.windows.append(editor)
        editor.parameter.setCurrentIndex(editor.parameter.findData(StepParameter.AMPLITUDE))
        editor.start.setValue(0.125)
        editor.channel.setCurrentIndex(editor.channel.findData(2))
        editor.device.setCurrentIndex(editor.device.findData(self.vaunix.id))
        self.assertIs(editor.parameter.currentData(), StepParameter.FREQUENCY)
        self.assertEqual(editor.channel.currentData(), 1)
        self.assertEqual(editor.parameter.findData(StepParameter.AMPLITUDE), -1)
        editor.parameter.setCurrentIndex(editor.parameter.findData(LinkageParameter.RF_POWER))
        self.assertEqual(editor.start.value(), -20)
        self.assertEqual(editor.unit.count(), 1)
        self.assertEqual(editor.unit.currentData(), "dBm")
        self.assertIn("dB", editor.step_caption.text())

    def test_saved_dg_channel_and_voltage_units_are_preserved(self):
        axis = LinkageAxis(self.dg.id, StepParameter.AMPLITUDE, 2, "mVpp", 125, 25, 2)
        editor = LinkageAxisEditor([self.vaunix, self.dg], axis)
        self.windows.append(editor)
        self.assertEqual(editor.model(), axis)
        self.assertEqual(editor.channel.currentData(), 2)
        self.assertEqual(editor.unit.currentData(), "mVpp")

    def test_saved_rf_power_restores_explicit_dbm_and_single_output(self):
        axis = LinkageAxis(self.vaunix.id, LinkageParameter.RF_POWER, 1, "dBm", -10, .5, 2)
        editor = LinkageAxisEditor([self.dg, self.vaunix], axis)
        self.windows.append(editor)
        self.assertEqual(editor.model(), axis)
        self.assertEqual(editor.channel.count(), 1)
        self.assertEqual(editor.unit.currentData(), "dBm")

    def test_mixed_task_restores_source_kinds_mode_dwell_and_edited_table(self):
        window = self.window()
        self.axis(window, self.vaunix, LinkageParameter.RF_POWER, "dBm", -10, .5)
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        window.ssg_dwell.setValue(.025)
        window._generate_table(show_message=False)
        window.point_table.item(1, 1).setText("-8.5")
        window._save_task()
        self.assertFalse(self.warning.called, str(self.warning.call_args))
        restored = self.window()
        task = restored._build_task()
        self.assertEqual(dict(task.source_kinds), {
            self.vaunix.id: DeviceKind.VAUNIX_LMS, self.ssg.id: DeviceKind.SIGLENT_SSG,
        })
        self.assertIs(task.ssg_mode, SsgLinkageMode.NATIVE_EXTERNAL)
        self.assertEqual(task.ssg_dwell_s, .025)
        self.assertEqual(task.points[1].values[0].value, -8.5)
        self.assertIn("软件", restored.metric_captions["applied"].text())

    def test_legacy_task_never_rebinds_dg_values_to_rf_source(self):
        window = self.window()
        axis = LinkageAxis(self.dg.id, StepParameter.AMPLITUDE, 1, "Vpp", .5, .25, 2)
        task = LinkageTaskRecord.create(
            name="legacy DG task", ni_device_id=self.ni.id, counter="ctr0", terminal="PFI0",
            rising=True, scan_mode=ScanMode.SYNCHRONOUS, axes=(axis,),
            points=generate_linkage_points((axis,), ScanMode.SYNCHRONOUS),
        )
        self.repository.save(self.dg.updated(kind=DeviceKind.VAUNIX_LMS, resource="VAUNIX::LMS::999999"))
        with self.assertRaisesRegex(ValueError, "类型已改变"):
            window._validate_saved_bindings(task)

    def test_device_kind_changed_while_editor_open_blocks_start_before_writes(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        window._generate_table(show_message=False)
        self.repository.save(self.dg.updated(kind=DeviceKind.VAUNIX_LMS, resource="VAUNIX::LMS::999999"))
        window._start()
        self.assertIsNone(window.controller)
        self.assertFalse(self.manager.active_leases())
        self.assertIn("类型已改变", self.warning.call_args.args[-1])

    def test_resource_changed_while_editor_open_requires_binding_review(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        window._generate_table(show_message=False)
        self.repository.save(self.dg.updated(resource="USB0::NOT-OPENED::INSTR"))
        window._start()
        self.assertIsNone(window.controller)
        self.assertFalse(self.manager.active_leases())
        self.assertIn("资源地址已改变", self.warning.call_args.args[-1])

    def test_dg_and_vaunix_use_the_same_first_p0_and_cycle(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        self.axis(window, self.vaunix, LinkageParameter.RF_POWER, "dBm", -10, .5)
        def dg_snapshot():
            return self.manager.virtual_lab.generator_snapshot(self.dg.resource)
        vaunix = self.manager.vaunix_device_for(self.vaunix)
        before = dg_snapshot().channels, vaunix.power_dbm
        self.start(window)
        self.assertEqual((dg_snapshot().channels, vaunix.power_dbm), before)
        window._emit_virtual(3)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().applied_count == 3))
        self.assertEqual(dg_snapshot().channels[0].frequency_hz, 1000)
        self.assertEqual(vaunix.power_dbm, -10)
        self.assertEqual(window.controller.snapshot().current_index, 0)
        self.assertIn(self.vaunix.id, window._runtime_rows)
        self.assertTrue(self.wait_for(
            lambda: window.metrics["applied"].text() == "3"
            and window._runtime_rows[self.vaunix.id].status.text() == "完成"
        ))
        self.assertEqual(window._runtime_rows[self.vaunix.id].status.text(), "完成")

    def test_native_point_limit_is_checked_before_upload(self):
        window = self.window()
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1, .001, 501)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        self.assertFalse(window.generate_button.isEnabled())
        self.assertIn("2 至 500", window.preflight.text())
        window._generate_table(show_message=False)
        self.assertIn("2 至 500", self.warning.call_args.args[-1])

    def test_virtual_ni_cannot_arm_a_real_native_ssg(self):
        real = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="Not opened",
                                   resource="USB0::0x1234::0x5678::NOT-A-DEVICE::INSTR")
        self.repository.save(real)
        window = self.window()
        self.axis(window, real, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        window._generate_table(show_message=False)
        with patch.object(self.manager, "is_connected", return_value=True), \
                patch("pyvisa.ResourceManager", side_effect=AssertionError("REAL_RESOURCE_OPENED")) as native_open:
            window._start()
            native_open.assert_not_called()
        self.assertIsNone(window.controller)
        self.assertFalse(self.manager.active_leases())
        self.assertIn("虚拟 NI", self.warning.call_args.args[-1])

    def test_participant_connection_stays_off_gui_thread(self):
        window = self.window()
        self.axis(window, self.vaunix, StepParameter.FREQUENCY, "GHz", 1, .1)
        self.manager.disconnect(self.vaunix)
        entered, release = threading.Event(), threading.Event()
        self.release_events.append(release)
        original = self.manager.connect
        threads, ticks = [], []
        def blocking_connect(record):
            threads.append(threading.get_ident())
            entered.set()
            if not release.wait(3):
                raise RuntimeError("CONNECT_FIXTURE_NOT_RELEASED")
            return original(record)
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start()
        try:
            with patch.object(self.manager, "connect", side_effect=blocking_connect):
                window._connect_required_devices()
                self.assertTrue(self.wait_for(entered.is_set))
                self.assertTrue(self.wait_for(lambda: len(ticks) >= 3))
                self.assertNotEqual(threads[0], threading.get_ident())
                self.assertFalse(window.close())
                release.set()
                self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        finally:
            timer.stop()

    def test_connection_dispatch_failure_restores_controls(self):
        window = self.window()
        class FailedPool:
            def start(self, _task):
                raise RuntimeError("SIMULATED_CONNECT_DISPATCH_FAILURE")
        window.thread_pool = FailedPool()
        window._connect_required_devices()
        self.assertFalse(window._call_tasks)
        self.assertIsNone(window._connect_task)
        self.assertTrue(window.connect_button.isEnabled())
        self.assertTrue(window.start_button.isEnabled())
        self.assertIn("SIMULATED_CONNECT_DISPATCH_FAILURE", self.warning.call_args.args[-1])

    def test_ssg_software_step_joins_dg_without_enabling_native_scan(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        self.axis(window, self.ssg, LinkageParameter.RF_POWER, "dBm", -20, 1)
        self.start(window)
        session = self.manager.session_for(self.ssg)
        self.assertEqual(session.query(":SWEep:STATe?"), "OFF")
        window._emit_virtual(2)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().applied_count == 2))
        self.assertEqual(float(session.query(":POWer?")), -19)
        self.assertEqual(session.query(":SWEep:STATe?"), "OFF")
        self.assertFalse(window._native_participants)

    def test_native_status_never_claims_ni_count_is_ssg_acceptance(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        self.start(window)
        participant = window._native_participants[self.ssg.id]
        self.assertTrue(participant.requires_stop)
        window._emit_virtual(3)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().applied_count == 3))
        self.assertTrue(self.wait_for(lambda: window.metrics["received"].text() == "3"
                                     and window.metrics["applied"].text() == "3"))
        snapshot = window.controller.snapshot()
        self.assertNotIn(self.ssg.id, {item.device_id for item in snapshot.device_results})
        self.assertIn("未知", window._runtime_rows[self.ssg.id].status.text())
        self.assertNotIn("完成", window._runtime_rows[self.ssg.id].status.text())
        self.assertIn("软件", window.metric_captions["applied"].text())
        with patch.object(self.manager.session_for(self.ssg), "query", side_effect=AssertionError("UI_POLLED_SSG")):
            window._controller_updated(snapshot)

    def test_native_stop_failure_keeps_all_leases_until_explicit_retry(self):
        window = self.window()
        self.axis(window, self.dg, StepParameter.FREQUENCY, "kHz", 1, 1)
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        self.start(window)
        participant = window._native_participants[self.ssg.id]
        original_stop = participant.stop
        attempts = []
        def stop_with_one_failure():
            attempts.append(True)
            if len(attempts) == 1:
                raise RuntimeError("SIMULATED_NATIVE_OFF_NOT_CONFIRMED")
            return original_stop()
        with patch.object(participant, "stop", side_effect=stop_with_one_failure):
            window._stop()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
            self.assertIsNotNone(window.controller)
            self.assertTrue(participant.requires_stop)
            self.assertEqual(self.manager.lease_owner(self.ssg.id), window._lease_owner)
            self.assertEqual(self.manager.lease_owner(self.dg.id), window._lease_owner)
            self.assertEqual(window.stop_button.text(), "重试停止")
            old_generation = self.manager.session_for(self.ssg).connection_generation
            window._stop()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        self.assertIsNone(window.controller)
        self.assertFalse(self.manager.active_leases())
        self.assertFalse(participant.requires_stop)
        self.assertGreater(self.manager.session_for(self.ssg).connection_generation, old_generation)

    def test_native_only_reports_observed_edges_without_software_completion(self):
        window = self.window()
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        before = self.manager.output_readback_activity(self.ssg).revision
        self.start(window)
        self.assertGreater(self.manager.output_readback_activity(self.ssg).revision, before)
        window._emit_virtual(3)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().received_count == 3))
        self.assertTrue(self.wait_for(lambda: window.metrics["received"].text() == "3"))
        snapshot = window.controller.snapshot()
        self.assertEqual(snapshot.applied_count, 0)
        self.assertIsNone(snapshot.current_index)
        self.assertEqual(snapshot.device_results, ())
        self.assertEqual(window.metrics["applied"].text(), "—")
        self.assertEqual(window.metrics["backlog"].text(), "—")
        self.assertEqual(window.metrics["current"].text(), "—")
        self.assertIn("未知", window._runtime_rows[self.ssg.id].status.text())

    def test_startup_cleanup_failure_is_not_retried_without_user_request(self):
        from rigol_control.siglent_linkage import SsgNativeLinkageParticipant
        attempts = []
        class FailedNativeStart(SsgNativeLinkageParticipant):
            def arm(self):
                super().arm()
                raise RuntimeError("SIMULATED_POST_ARM_FAILURE")

            def stop(self):
                attempts.append(True)
                if len(attempts) == 1:
                    raise RuntimeError("SIMULATED_STARTUP_OFF_FAILURE")
                return super().stop()
        window = self.window()
        self.axis(window, self.ssg, StepParameter.FREQUENCY, "GHz", 1.5, .1)
        window.ssg_mode.setCurrentIndex(window.ssg_mode.findData(SsgLinkageMode.NATIVE_EXTERNAL))
        window._generate_table(show_message=False)
        with patch("rigol_control.siglent_linkage.SsgNativeLinkageParticipant", FailedNativeStart):
            window._start()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        self.assertEqual(len(attempts), 1)
        self.assertIsNotNone(window.controller)
        self.assertFalse(window.controller.is_quiescent)
        self.assertEqual(self.manager.lease_owner(self.ssg.id), window._lease_owner)
        self.assertEqual(window.stop_button.text(), "重试停止")
        QTest.qWait(30)
        self.app.processEvents()
        self.assertEqual(len(attempts), 1, "Startup failure automatically retried a failed stop")
        old_generation = self.manager.session_for(self.ssg).connection_generation
        window._stop()
        self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        self.assertIsNone(window.controller)
        self.assertFalse(self.manager.active_leases())
        self.assertGreater(self.manager.session_for(self.ssg).connection_generation, old_generation)


if __name__ == "__main__":
    unittest.main()
