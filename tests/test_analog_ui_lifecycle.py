from dataclasses import replace
import os
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication, QComboBox

from rigol_control.analog import AnalogAcquisitionSnapshot, AnalogAcquisitionState, AnalogAcquisitionUpdate
from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.ni_analog import AnalogInputBlock
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.ui.device_windows import NIDeviceWindow
from rigol_control.ui.newport_window import NewportWindow
from rigol_control.drivers.newport_smc100 import VIRTUAL_SMC100_RESOURCE
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE


class _Controller:
    def __init__(self, *, busy=True):
        self.busy = busy
        self.value = AnalogAcquisitionSnapshot(
            state=AnalogAcquisitionState.ERROR, channel_names=("Dev1/ai0",),
            sample_rate_hz=1000, total_samples=10, retained_samples=10,
            dropped_samples=0, error="stop failed", cleanup_pending=busy, revision=1,
        )
        self.request_stop = Mock()
        self.start = Mock()
        self.add_callback = Mock()
        self.stop_entered = Event()
        self.stop_allowed = Event()
        self.stop_allowed.set()

    @property
    def is_running(self):
        return self.busy

    def snapshot(self):
        return self.value

    def stop(self):
        self.stop_entered.set()
        self.stop_allowed.wait(3)
        self.busy = False
        self.value = replace(self.value, cleanup_pending=False, revision=self.value.revision + 1)

    def clear(self):
        self.value = replace(self.value, total_samples=0, retained_samples=0, revision=self.value.revision + 1)


class _RejectPool:
    def start(self, _task):
        raise RuntimeError("queue unavailable")


class AnalogUiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["analog-lifecycle"])

    def setUp(self):
        self.manager = DeviceConnectionManager()
        self.record = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="Virtual NI", resource=VIRTUAL_NI_RESOURCE)
        self.manager.connect(self.record)
        self.window = NIDeviceWindow(self.record, self.manager)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.original_pool = self.window.thread_pool
        self.controller = _Controller()
        self.window.analog_controller = self.controller
        self.manager.acquire_devices(self.window._ni_lease_owner, [self.record.id])

    def tearDown(self):
        self.controller.stop_allowed.set()
        self.controller.busy = False
        self.window._analog_start_pending = False
        self.window._analog_stop_pending = False
        self.window.thread_pool = self.original_pool
        self.window.test_counter = None
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.window.close()
        self.app.processEvents()
        self.manager.close_all()

    def wait(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def test_error_with_pending_cleanup_keeps_lease_and_blocks_restart(self):
        self.window._analog_updated(AnalogAcquisitionUpdate(self.controller.snapshot()))
        self.assertFalse(self.window.ai_start.isEnabled())
        self.assertTrue(self.window.ai_stop.isEnabled())
        self.assertIn("重试", self.window.ai_stop.text())
        self.assertFalse(self.window.ai_clear.isEnabled())
        self.window._start_analog_input()
        self.assertIs(self.window.analog_controller, self.controller)
        self.window._release_ni_lease_if_idle()
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)

    def test_rejected_stop_dispatch_restores_retry_without_releasing_lease(self):
        self.window.thread_pool = _RejectPool()
        self.window._stop_analog_input()
        self.assertFalse(self.window._analog_stop_pending)
        self.assertTrue(self.window.ai_stop.isEnabled())
        self.assertFalse(self.window.ai_start.isEnabled())
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)
        self.assertIn("queue unavailable", self.window.statusBar().currentMessage())
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertIs(self.window.analog_controller, self.controller)

    def test_disconnect_requests_background_stop_and_preserves_connection(self):
        self.window.thread_pool = _RejectPool()
        self.window._disconnect_ni()
        self.assertTrue(self.manager.is_connected(self.record))
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)
        self.controller.request_stop.assert_called_once()

    def test_stop_before_queued_start_prevents_late_hardware_start(self):
        self.controller.busy = False
        queued = []
        self.window.submit = lambda operation, **callbacks: queued.append((operation, callbacks))
        with patch("rigol_control.ui.device_windows.AnalogAcquisitionController", return_value=self.controller):
            self.window._start_analog_input()
        self.assertTrue(self.window._analog_start_pending)
        self.window._stop_analog_input()
        self.assertEqual(len(queued), 1)
        queued[0][0]()
        queued[0][1]["on_success"]()
        self.assertEqual(len(queued), 1)
        self.controller.start.assert_not_called()
        self.assertIsNone(self.manager.lease_owner(self.record.id))

    def test_stop_does_not_block_qt_and_releases_only_after_completion(self):
        self.controller.stop_allowed.clear()
        timer_fired = []
        QTimer.singleShot(0, lambda: timer_fired.append(True))
        started = time.monotonic()
        self.window._stop_analog_input()
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertTrue(self.wait(lambda: self.controller.stop_entered.is_set() and timer_fired))
        self.assertFalse(self.window.ai_start.isEnabled())
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)
        self.controller.stop_allowed.set()
        self.assertTrue(self.wait(lambda: not self.window._call_tasks and self.window.ai_start.isEnabled()))
        self.assertIsNone(self.manager.lease_owner(self.record.id))
        self.assertTrue(self.window.ai_save_csv.isEnabled())

    def test_delayed_snapshot_and_previous_controller_cannot_rewind_display(self):
        latest = replace(self.controller.snapshot(), total_samples=20, retained_samples=20, revision=5)
        self.window._analog_updated(AnalogAcquisitionUpdate(latest))
        self.window._analog_updated(AnalogAcquisitionUpdate(self.controller.snapshot()))
        self.assertEqual(self.window.ai_metrics["samples"].text(), "20")
        other = _Controller()
        self.window._analog_published((other, AnalogAcquisitionUpdate(replace(latest, total_samples=999, revision=99))))
        self.assertEqual(self.window.ai_metrics["samples"].text(), "20")

    def test_polled_snapshot_does_not_discard_queued_block_with_older_revision(self):
        latest = replace(self.controller.snapshot(), total_samples=20, retained_samples=20, revision=5)
        self.window._analog_updated(AnalogAcquisitionUpdate(latest))
        block = AnalogInputBlock(("Dev1/ai0",), 1000, 0, ((1.0, 2.0, 3.0),))
        self.window._analog_updated(AnalogAcquisitionUpdate(self.controller.snapshot(), block))
        self.assertEqual(self.window.ai_metrics["samples"].text(), "20")
        self.assertEqual(self.window.ai_plot.displayed_samples, 3)
        self.window._analog_updated(AnalogAcquisitionUpdate(self.controller.snapshot(), block))
        self.assertEqual(self.window.ai_plot.displayed_samples, 3)

    def test_clear_rejects_queued_blocks_and_snapshots_from_before_clear(self):
        self.controller.busy = False
        previous = self.controller.snapshot()
        block = AnalogInputBlock(("Dev1/ai0",), 1000, 0, ((1.0, 2.0, 3.0),))
        self.window._analog_updated(AnalogAcquisitionUpdate(previous, block))
        self.window._clear_analog_data()
        self.window._analog_updated(AnalogAcquisitionUpdate(previous, block))
        self.assertEqual(self.window.ai_metrics["samples"].text(), "0")
        self.assertEqual(self.window.ai_plot.displayed_samples, 0)

    def test_quiescent_error_allows_export_and_releases_lease(self):
        self.controller.busy = False
        self.controller.value = replace(self.controller.value, cleanup_pending=False, revision=2)
        self.window._analog_updated(AnalogAcquisitionUpdate(self.controller.snapshot()))
        self.assertTrue(self.window.ai_start.isEnabled())
        self.assertTrue(self.window.ai_save_csv.isEnabled())
        self.assertFalse(self.window.ai_stop.isEnabled())
        self.assertIsNone(self.manager.lease_owner(self.record.id))

    def test_counter_stop_failure_keeps_counter_for_retry(self):
        self.controller.busy = False
        counter = Mock()
        self.window.test_counter = counter
        self.window.thread_pool = _RejectPool()
        self.window._stop_ni_counter()
        self.assertIs(self.window.test_counter, counter)
        self.assertTrue(self.window.ni_counter_stop.isEnabled())
        self.assertFalse(self.window.ni_counter_start.isEnabled())
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)
        self.window.thread_pool = self.original_pool
        self.window._stop_ni_counter()
        self.assertTrue(self.wait(lambda: self.window.test_counter is None and not self.window._call_tasks))
        self.assertIsNone(self.manager.lease_owner(self.record.id))

    def _queue_counter_start(self):
        controls = [QComboBox() for _ in range(3)]
        for control, text in zip(controls, ("Dev1/ctr0", "/Dev1/PFI0", "上升沿")):
            control.addItem(text)
        queued = []
        self.window.submit = lambda operation, **callbacks: queued.append((operation, callbacks))
        counter = Mock()
        with patch.object(self.manager, "counter_for", return_value=counter):
            self.window._start_ni_counter(*controls)
        return counter, queued

    def test_counter_failed_start_retains_reference_and_lease_for_cleanup(self):
        self.controller.busy = False
        counter, queued = self._queue_counter_start()
        with patch.object(self.window, "_show_hardware_error"):
            queued[0][1]["on_error"]("partial task start failed")
        self.assertIs(self.window.test_counter, counter)
        self.assertTrue(self.window.ni_counter_stop.isEnabled())
        self.assertFalse(self.window.ni_counter_start.isEnabled())
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window._ni_lease_owner)

    def test_close_during_queued_counter_start_defers_stop_until_start_returns(self):
        self.controller.busy = False
        counter, queued = self._queue_counter_start()
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(len(queued), 1)
        counter.stop.assert_not_called()
        queued[0][0]()
        queued[0][1]["on_success"]()
        self.assertEqual(len(queued), 2)
        queued[1][0]()
        queued[1][1]["on_success"]()
        self.assertIsNone(self.window.test_counter)
        self.assertIsNone(self.manager.lease_owner(self.record.id))

    def test_newport_cleanup_failure_blocks_close_and_restores_stop_retry(self):
        stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="Stage", resource=VIRTUAL_SMC100_RESOURCE)
        self.manager.connect(stage)
        window = NewportWindow(stage, [self.record], self.manager)
        run = Mock()
        run.is_running = True
        run.cleanup_pending = True
        run.owner = "rotation:test"
        run.snapshot.return_value = Mock(phase="error")
        # Rendering is unrelated to dispatch; avoid mocking a complete scan table.
        window.controller = run
        window._render_scan = Mock()
        window.thread_pool = _RejectPool()
        self.manager.acquire_devices(run.owner, [stage.id])
        try:
            event = QCloseEvent()
            window.closeEvent(event)
            self.assertFalse(event.isAccepted())
            self.assertFalse(window._scan_stop_pending)
            self.assertTrue(window.stop_button.isEnabled())
            self.assertIn("重试停止", window.stop_button.text())
            self.assertFalse(window.start_button.isEnabled())
            self.assertEqual(self.manager.lease_owner(stage.id), run.owner)
            run.request_stop.assert_called_once()
        finally:
            run.is_running = False
            run.cleanup_pending = False
            self.manager.release_devices(run.owner)
            window.close()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
