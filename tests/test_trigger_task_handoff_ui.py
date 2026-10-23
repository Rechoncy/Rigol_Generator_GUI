import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.trigger import ControllerState
from rigol_control.ui.device_windows import BaseDeviceWindow, DG4000Window, GenericScpiWindow
from rigol_control.ui.linkage_window import LinkageTaskWindow
from rigol_control.ui.vaunix_window import VaunixLmsWindow
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class DeferredPool:
    def __init__(self):
        self.tasks = []

    def start(self, task):
        self.tasks.append(task)


class TriggerTaskHandoffUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["trigger-task-handoff-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.instances = []

    def tearDown(self):
        for window, manager, record, ni, pool in self.instances:
            if pool is not None:
                while pool.tasks:
                    pool.tasks.pop(0).run()
                    self.app.processEvents()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
            if window.controller is not None:
                window.controller.stop(timeout_seconds=1)
            window.close()
            manager.close_all()
        self.temporary.cleanup()

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            QTest.qWait(5)
        return bool(predicate())

    def window_for(self, kind, *, deferred=True):
        repository = DeviceRepository(Path(self.temporary.name) / str(len(self.instances)))
        manager = DeviceConnectionManager()
        record = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS if kind == "vaunix" else DeviceKind.DG4000,
                                     name=kind, resource=VIRTUAL_VAUNIX_RESOURCE if kind == "vaunix"
                                     else VIRTUAL_DG_RESOURCE)
        ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        for item in (record, ni):
            repository.save(item)
            manager.connect(item)
        if kind == "linkage":
            window = LinkageTaskWindow(repository, manager)
            window._generate_table(show_message=False)
        elif kind == "vaunix":
            window = VaunixLmsWindow(record, [ni], manager)
            window._generate_trigger_table(show_message=False)
        else:
            window = DG4000Window(record, [ni], manager)
            window._generate_scan_table(show_message=False)
        self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        pool = DeferredPool() if deferred else None
        if pool is not None:
            window.thread_pool = pool
        self.instances.append((window, manager, record, ni, pool))
        return self.instances[-1]

    @staticmethod
    def start(window):
        (window._start if isinstance(window, LinkageTaskWindow) else window._start_trigger)()

    @staticmethod
    def stop(window):
        (window._stop if isinstance(window, LinkageTaskWindow) else window._stop_trigger)()

    def assert_queued_start_cancelled(self, kind):
        window, manager, record, ni, pool = self.window_for(kind)
        self.start(window)
        controller = window.controller
        self.assertEqual(len(pool.tasks), 1)
        self.stop(window)
        # A stop cannot finish first and release a still-queued start task.
        self.assertEqual(len(pool.tasks), 1)
        self.assertIs(window.controller, controller)
        self.assertIsNotNone(manager.lease_owner(record.id))
        self.start(window)
        self.assertIs(window.controller, controller)
        self.assertEqual(len(pool.tasks), 1)
        self.assertFalse(window.close())
        self.assertIs(window.controller, controller)
        pool.tasks.pop(0).run()
        self.app.processEvents()
        self.assertEqual(len(pool.tasks), 1)
        self.assertIs(window.controller, controller)
        self.assertIsNotNone(manager.lease_owner(record.id))
        self.assertEqual(controller.snapshot().applied_count, 0)
        pool.tasks.pop(0).run()
        self.app.processEvents()
        self.assertIsNone(window.controller)
        self.assertIsNone(manager.lease_owner(record.id))

    def test_dg_queued_start_cancel_waits_for_start_task_before_releasing_lease(self):
        self.assert_queued_start_cancelled("dg")

    def test_vaunix_queued_start_cancel_waits_for_start_task_before_releasing_lease(self):
        self.assert_queued_start_cancelled("vaunix")

    def test_linkage_queued_start_cancel_waits_for_start_task_before_releasing_lease(self):
        self.assert_queued_start_cancelled("linkage")

    def test_delayed_old_start_signals_cannot_clear_new_task_or_lease(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, record, ni, pool = self.window_for(kind)
                self.start(window)
                old_start = pool.tasks.pop(0)
                self.stop(window)
                old_start.run()
                self.app.processEvents()
                pool.tasks.pop(0).run()
                self.app.processEvents()
                self.assertIsNone(window.controller)
                self.start(window)
                current = window.controller
                old_start.signals.failed.emit("late old startup error")
                old_start.signals.finished.emit()
                self.app.processEvents()
                self.assertIs(window.controller, current)
                self.assertIsNotNone(manager.lease_owner(record.id))
                self.assertEqual(len(pool.tasks), 1)
                self.stop(window)

    def test_vaunix_manual_writes_are_rejected_while_ni_task_keeps_stepping(self):
        window, manager, record, ni, pool = self.window_for("vaunix", deferred=False)
        errors = []
        window._show_hardware_error = errors.append
        self.start(window)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().state is ControllerState.RUNNING))
        before = window.device.output_snapshot()
        window.output_frequency.setValue(before.frequency_hz + 1e6)
        for operation in (window._apply_output, window._apply_sweep, window._apply_pulse,
                          window._apply_sequence, window._refresh_all):
            operation()
        self.assertEqual(len(errors), 5)
        self.assertTrue(all("请先停止任务" in error for error in errors))
        self.assertEqual(window.device.output_snapshot().frequency_hz, before.frequency_hz)
        manager.virtual_lab.emit_trigger(ni.resource, 2)
        self.assertTrue(self.wait_for(lambda: window.controller.snapshot().applied_count == 2))
        self.stop(window)
        self.assertTrue(self.wait_for(lambda: window.controller is None))

    def test_vaunix_queued_manual_operation_rechecks_lease_before_sdk_call(self):
        window, manager, record, ni, pool = self.window_for("vaunix")
        queued = []

        def capture(instance, operation, **kwargs):
            queued.append(operation)

        with patch.object(BaseDeviceWindow, "submit", capture):
            window.submit(lambda: self.fail("manual operation must not run"))
        manager.acquire_devices("another-task", (record.id,))
        try:
            with self.assertRaises(DeviceBusyError):
                queued[0]()
        finally:
            manager.release_devices("another-task")

    def test_start_refuses_pending_manual_task_instead_of_overlapping_its_write(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, record, ni, pool = self.window_for(kind)
                marker = object()
                window._call_tasks.add(marker)
                try:
                    self.start(window)
                    self.assertIsNone(window.controller)
                    self.assertIsNone(manager.lease_owner(record.id))
                    self.assertFalse(pool.tasks)
                finally:
                    window._call_tasks.discard(marker)

    def test_generic_alias_raw_command_cannot_bypass_dg_lease_at_submission_or_execution(self):
        window, manager, record, ni, pool = self.window_for("dg")
        alias = DeviceRecord.create(kind=DeviceKind.GENERIC_SCPI, name="Raw alias", resource=record.resource)
        generic = GenericScpiWindow(alias, manager)
        queued, errors = [], []
        generic._show_hardware_error = errors.append

        def capture(instance, operation, **kwargs):
            queued.append(operation)

        generator = manager.virtual_lab.scpi_session_for(record.resource)._generator
        generator.commands.clear()
        try:
            with patch.object(BaseDeviceWindow, "submit", capture):
                generic.generic_input.setText("*RST")
                generic._generic_send()
            self.assertEqual(len(queued), 1)
            manager.acquire_devices("dg-trigger", (record.id,))
            with self.assertRaises(DeviceBusyError):
                queued.pop()()
            generic._generic_send()
            self.assertEqual(len(errors), 1)
            self.assertFalse(generator.commands)
        finally:
            manager.release_devices("dg-trigger")
            generic.close()

    def test_generic_window_keeps_pending_raw_task_alive_until_command_returns(self):
        window, manager, record, ni, pool = self.window_for("dg")
        alias = DeviceRecord.create(kind=DeviceKind.GENERIC_SCPI, name="Raw alias", resource=record.resource)
        generic = GenericScpiWindow(alias, manager)
        deferred = DeferredPool()
        generic.thread_pool = deferred
        ran = []
        generic.submit(lambda: ran.append("finished"))
        self.assertEqual(len(generic._call_tasks), 1)
        self.assertFalse(generic.close())
        self.assertEqual(len(generic._call_tasks), 1)
        self.assertEqual(ran, [])
        deferred.tasks.pop(0).run()
        self.app.processEvents()
        self.assertEqual(ran, ["finished"])
        self.assertFalse(generic._call_tasks)
        self.assertTrue(generic.close())


if __name__ == "__main__":
    unittest.main()
