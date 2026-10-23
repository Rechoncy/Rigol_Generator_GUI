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
from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.linkage import LinkageAxis, MultiDeviceTriggerController, generate_linkage_points
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState, TriggerStepController
from rigol_control.ui.device_windows import BaseDeviceWindow, DG4000Window
from rigol_control.ui.linkage_window import LinkageTaskWindow
from rigol_control.ui.vaunix_window import VaunixLmsWindow
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class Counter:
    def __init__(self):
        self.count = 0
        self.stopped = False

    def start(self):
        pass

    def read_count(self):
        return self.count

    def stop(self):
        self.stopped = True


class BlockedWriter:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def apply_steps(self, values):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test writer was not released")


class TriggerStopUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["trigger-stop-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.instances = []

    def tearDown(self):
        for window, manager, controller, writer, record, owner in self.instances:
            writer.release.set()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
            controller.stop(timeout_seconds=1)
            self.assertTrue(controller.is_quiescent)
            if not window.close():
                self.assertTrue(self.wait_for(lambda: not window._call_tasks))
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

    def running_window(self, kind):
        folder = Path(self.temporary.name) / str(len(self.instances))
        repository = DeviceRepository(folder)
        manager = DeviceConnectionManager()
        ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        record = DeviceRecord.create(
            kind=DeviceKind.VAUNIX_LMS if kind == "vaunix" else DeviceKind.DG4000,
            name=kind, resource=VIRTUAL_VAUNIX_RESOURCE if kind == "vaunix" else VIRTUAL_DG_RESOURCE,
        )
        for item in (ni, record):
            repository.save(item)
            manager.connect(item)
        counter, writer = Counter(), BlockedWriter()
        if kind == "linkage":
            window = LinkageTaskWindow(repository, manager)
            points = generate_linkage_points(
                (LinkageAxis(record.id, StepParameter.FREQUENCY, 2, "kHz", 1, 1, 2),),
                ScanMode.SYNCHRONOUS,
            )
            controller = MultiDeviceTriggerController(counter, {record.id: writer}, points)
            owner = window._lease_owner = "test-linkage-stop"
            bridge = window.bridge
        else:
            window = (VaunixLmsWindow(record, [ni], manager) if kind == "vaunix"
                      else DG4000Window(record, [ni], manager))
            controller = TriggerStepController(counter, writer, StepParameter.FREQUENCY, 1, [1000, 2000])
            owner = window._lease_owner if kind == "vaunix" else window._trigger_lease_owner
            bridge = window.trigger_bridge
        self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        window.controller = controller
        manager.acquire_devices(owner, (record.id, ni.id))
        controller.add_callback(bridge.begin_session())
        self.instances.append((window, manager, controller, writer, record, owner))
        actual_stop = controller.stop

        def short_stop(timeout_seconds=0.02):
            return actual_stop(timeout_seconds=timeout_seconds)

        controller.stop = short_stop
        controller.start()
        counter.count = 1
        self.assertTrue(self.wait_for(writer.entered.is_set))
        return self.instances[-1]

    @staticmethod
    def stop_ui(window):
        (window._stop if isinstance(window, LinkageTaskWindow) else window._stop_trigger)()

    @staticmethod
    def stop_button(window):
        if isinstance(window, LinkageTaskWindow):
            return window.stop_button
        if isinstance(window, VaunixLmsWindow):
            return window.trigger_stop_button
        return window.trigger_stop

    def assert_timeout_and_retry(self, kind):
        window, manager, controller, writer, record, owner = self.running_window(kind)
        self.stop_ui(window)
        self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        self.assertIs(window.controller, controller)
        self.assertFalse(controller.is_quiescent)
        self.assertIs(controller.snapshot().state, ControllerState.STOPPING)
        self.assertEqual(manager.lease_owner(record.id), owner)
        self.assertTrue(self.stop_button(window).isEnabled())
        writer.release.set()
        self.stop_ui(window)
        self.assertTrue(self.wait_for(lambda: not window._call_tasks and window.controller is None))
        self.assertIsNone(manager.lease_owner(record.id))
        self.assertTrue(controller.is_quiescent)

    def test_dg_stop_timeout_retains_controller_and_lease_until_successful_retry(self):
        self.assert_timeout_and_retry("dg")

    def test_vaunix_stop_timeout_retains_controller_and_lease_until_successful_retry(self):
        self.assert_timeout_and_retry("vaunix")

    def test_linkage_stop_timeout_retains_controller_and_lease_until_successful_retry(self):
        self.assert_timeout_and_retry("linkage")

    def test_close_with_blocked_writer_keeps_window_callbacks_controller_and_lease(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, controller, writer, record, owner = self.running_window(kind)
                window.show()
                self.assertFalse(window.close())
                self.assertTrue(window.isVisible())
                self.assertTrue(self.wait_for(lambda: not window._call_tasks))
                self.assertIs(window.controller, controller)
                self.assertEqual(manager.lease_owner(record.id), owner)
                # Error publication still reaches the surviving window.
                controller._fail("writer remains busy")
                self.assertTrue(self.wait_for(
                    lambda: "writer remains busy" in window.statusBar().currentMessage()
                    if kind != "linkage" else "writer remains busy" in window.run_log.toPlainText()
                ))
                self.assertEqual(manager.lease_owner(record.id), owner)

    def test_error_while_writer_is_busy_does_not_release_any_window_lease(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, controller, writer, record, owner = self.running_window(kind)
                controller._fail("injected monitor error")
                bridge = window.bridge if kind == "linkage" else window.trigger_bridge
                bridge.flush()
                self.assertFalse(controller.is_quiescent)
                self.assertIs(window.controller, controller)
                self.assertEqual(manager.lease_owner(record.id), owner)

    def test_dg_without_manager_rejects_manual_commands_after_error_with_writer_in_flight(self):
        window, manager, controller, writer, record, owner = self.running_window("dg")
        controller._fail("injected error while writer is blocked")
        self.assertIs(controller.snapshot().state, ControllerState.ERROR)
        self.assertFalse(controller.is_quiescent)
        # Exercise the supported direct-driver window path without a manager
        # or a lease acting as an independent manual-operation guard.
        manager.release_devices(owner)
        window.connection_manager = None
        try:
            with self.assertRaises(DeviceBusyError):
                window._require_manual_access()
            errors = []
            with patch.object(BaseDeviceWindow, "submit") as dispatched:
                window.submit(lambda: None, on_error=errors.append)
                dispatched.assert_not_called()
            self.assertEqual(len(errors), 1)
            self.assertIn("请先停止任务", errors[0])
        finally:
            window.connection_manager = manager

    def test_linkage_background_stop_wait_keeps_gui_timer_responsive(self):
        window, manager, controller, writer, record, owner = self.running_window("linkage")
        actual_stop = controller.stop
        controller.stop = lambda: actual_stop(timeout_seconds=0.3)
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            started = time.monotonic()
            window._stop()
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(self.wait_for(lambda: len(ticks) >= 4))
            self.assertTrue(window._call_tasks)
            self.assertEqual(manager.lease_owner(record.id), owner)
            writer.release.set()
            self.assertTrue(self.wait_for(lambda: not window._call_tasks))
        finally:
            timer.stop()
            controller.stop = actual_stop


if __name__ == "__main__":
    unittest.main()
