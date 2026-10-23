import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThreadPool, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.trigger import ControllerState
from rigol_control.ui.linkage_window import LinkageTaskWindow
from rigol_control.ui.widgets import CallTask
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class BlockingPreparedWriter:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.prepare_thread = None
        self.batches = []
        self.failure = None

    def validate_step_values(self, values):
        return tuple(values)

    def prepare_trigger(self):
        self.prepare_thread = threading.get_ident()
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test prepare was not released")
        if self.failure:
            raise RuntimeError(self.failure)
        return self

    def apply_steps(self, values):
        self.batches.append(tuple(values))


class RecordingCounter:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def read_count(self):
        return 0

    def stop(self):
        self.stop_calls += 1


class LinkageStartupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["linkage-start-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = DeviceRepository(Path(self.temporary.name))
        self.manager = DeviceConnectionManager()
        self.dg = DeviceRecord.create(kind=DeviceKind.DG4000, name="DG", resource=VIRTUAL_DG_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        for record in (self.dg, self.ni):
            self.repository.save(record)
            self.manager.connect(record)
        self.writer = BlockingPreparedWriter()
        self.counter = RecordingCounter()
        self.window = LinkageTaskWindow(self.repository, self.manager)
        self.window._generate_table(show_message=False)
        self.patches = [patch.object(self.manager, "dg_driver_for", return_value=self.writer),
                        patch.object(self.manager, "counter_for", return_value=self.counter),
                        patch("rigol_control.ui.linkage_window.QMessageBox.warning")]
        self.driver_patch, self.counter_patch, self.warning = [item.start() for item in self.patches]
        self.queue_release = threading.Event()
        self.queue_pool = None

    def tearDown(self):
        self.writer.release.set()
        self.queue_release.set()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.window._stop()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.window.close()
        self.app.processEvents()
        if self.queue_pool is not None:
            self.assertTrue(self.queue_pool.waitForDone(2000))
        for item in reversed(self.patches):
            item.stop()
        self.manager.close_all()
        self.temporary.cleanup()

    def wait_for(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            QTest.qWait(5)
        return bool(predicate())

    def test_blocking_prepare_runs_in_worker_while_gui_timer_continues(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(time.monotonic()))
        timer.start()
        try:
            started = time.monotonic()
            self.window._start()
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(self.wait_for(self.writer.entered.is_set))
            self.assertTrue(self.wait_for(lambda: len(ticks) >= 4))
            self.assertNotEqual(self.writer.prepare_thread, threading.get_ident())
            self.assertTrue(self.window._call_tasks)
            self.assertEqual(self.manager.lease_owner(self.dg.id), self.window._lease_owner)
            self.assertEqual(self.counter.start_calls, 0)
            self.writer.release.set()
            self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
            self.assertIs(self.window.controller.snapshot().state, ControllerState.RUNNING)
            self.assertEqual(self.counter.start_calls, 1)
        finally:
            timer.stop()

    def test_stop_during_prepare_keeps_lease_until_cancelled_without_counter_or_writes(self):
        self.window._start()
        self.assertTrue(self.wait_for(self.writer.entered.is_set))
        controller = self.window.controller
        owner = self.window._lease_owner
        started = time.monotonic()
        self.window._stop()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIs(self.window.controller, controller)
        self.assertEqual(self.manager.lease_owner(self.dg.id), owner)
        self.assertTrue(self.window._pending_stop)
        self.assertFalse(self.window.stop_button.isEnabled())
        self.writer.release.set()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.assertIsNone(self.window.controller)
        self.assertIsNone(self.manager.lease_owner(self.dg.id))
        self.assertEqual(self.counter.start_calls, 0)
        self.assertEqual(self.writer.batches, [])

    def test_stop_before_queued_start_preserves_cancellation(self):
        self.queue_pool = QThreadPool()
        self.queue_pool.setMaxThreadCount(1)
        occupied = threading.Event()

        def occupy_worker():
            occupied.set()
            self.queue_release.wait(5)

        blocker = CallTask(occupy_worker)
        self.queue_pool.start(blocker)
        self.assertTrue(occupied.wait(1))
        self.window.thread_pool = self.queue_pool
        self.window._start()
        self.assertFalse(self.writer.entered.is_set())
        self.window._stop()
        self.assertTrue(self.window._call_tasks)
        self.assertIsNotNone(self.manager.lease_owner(self.dg.id))
        self.queue_release.set()
        self.writer.release.set()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.assertIsNone(self.window.controller)
        self.assertEqual(self.counter.start_calls, 0)
        self.assertEqual(self.writer.batches, [])

    def test_close_during_prepare_is_rejected_until_worker_returns(self):
        self.window.show()
        self.window._start()
        self.assertTrue(self.wait_for(self.writer.entered.is_set))
        self.assertFalse(self.window.close())
        self.assertTrue(self.window.isVisible())
        self.assertTrue(self.window._call_tasks)
        self.assertIsNotNone(self.window.controller)
        self.assertIsNotNone(self.manager.lease_owner(self.dg.id))
        self.writer.release.set()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.assertIsNone(self.window.controller)
        self.assertTrue(self.window.close())
        self.assertEqual(self.counter.start_calls, 0)

    def test_prepare_failure_releases_lease_after_task_finishes(self):
        self.writer.failure = "coupling query failed"
        self.window._start()
        self.assertTrue(self.wait_for(self.writer.entered.is_set))
        self.assertIsNotNone(self.manager.lease_owner(self.dg.id))
        self.writer.release.set()
        self.assertTrue(self.wait_for(lambda: not self.window._call_tasks))
        self.assertIsNone(self.window.controller)
        self.assertIsNone(self.manager.lease_owner(self.dg.id))
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertEqual(self.counter.start_calls, 0)
        self.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
