import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.drivers.vaunix_lms import (
    PAUSE_SEQUENCE, REPEAT_SEQUENCE, RESUME_SEQUENCE, START_SEQUENCE, STOP_SEQUENCE,
    VaunixDeviceDescriptor, VaunixLmsDevice, VaunixLmsError, VirtualVaunixLmsDevice,
)
from rigol_control.connections import DeviceConnectionManager
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.scan import ScanPoint
from rigol_control.trigger import ControllerState, TriggerStepController


class FakeDll:
    def __init__(self):
        self.opened = []
        self.closed = []
        self.writes = []
        self.reads = []
        self.frequency_statuses = []
        self.power_statuses = []

    def fnLMS_InitDevice(self, handle):
        self.opened.append(handle)
        return 0

    def fnLMS_CloseDevice(self, handle):
        self.closed.append(handle)
        return 0

    def fnLMS_SetFrequencyEx(self, handle, value):
        self.writes.append(("frequency", handle, value))
        return self.frequency_statuses.pop(0) if self.frequency_statuses else 0

    def fnLMS_SetPowerLevel(self, handle, value):
        self.writes.append(("power", handle, value))
        return self.power_statuses.pop(0) if self.power_statuses else 0

    def __getattr__(self, name):
        values = {
            "fnLMS_GetMinFreqEx": 10_000_000, "fnLMS_GetMaxFreqEx": 2_000_000_000,
            "fnLMS_GetMinPwr": -160, "fnLMS_GetMaxPwr": 40,
        }

        def read(*args):
            self.reads.append((name, args))
            return values.get(name, 0)

        return read


class FakeApi:
    dll_version = 0x010F

    def __init__(self):
        self._lock = threading.RLock()
        self.dll = FakeDll()
        self.handle = 7

    def enumerate_devices(self):
        return (VaunixDeviceDescriptor(self.handle, "LMS-TEST", 123, 3),)


class Counter:
    def __init__(self):
        self.count = 0
        self._lock = threading.Lock()

    def start(self):
        with self._lock:
            self.count = 0

    def read_count(self):
        with self._lock:
            return self.count

    def add(self, count):
        with self._lock:
            self.count += count

    def stop(self):
        pass


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class VaunixTriggerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.device = VaunixLmsDevice("VAUNIX::LMS::123", api_factory=lambda: self.api)
        self.device.connect()
        self.addCleanup(self.device.disconnect)
        self.api.dll.reads.clear()
        self.values = (
            StepValue(StepParameter.FREQUENCY, 1, 5e9),
            StepValue(StepParameter.AMPLITUDE, 1, -10),
        )

    def test_prepared_writer_performs_no_queries_and_one_sdk_call_per_parameter(self):
        writer = self.device.prepare_trigger()
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual(self.api.dll.writes, [])
        writer.apply_steps(self.values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])
        self.assertEqual(self.api.dll.reads, [])

    def test_uncertain_first_setting_is_not_replayed_or_followed_by_power(self):
        writer = self.device.prepare_trigger()
        self.api.dll.frequency_statuses = [0x80020000, 0]
        with patch("rigol_control.drivers.vaunix_lms.time.sleep") as sleep:
            with self.assertRaisesRegex(VaunixLmsError, "BAD_HID_IO"):
                writer.apply_steps(self.values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000)])
        self.assertEqual(self.api.dll.opened, [7])
        self.assertFalse(self.device.is_connected)
        sleep.assert_not_called()

    def test_nonidempotent_actions_are_not_replayed_after_uncertain_acceptance(self):
        actions = (
            ("fnLMS_StartSweep", lambda: self.device.start_sweep(True)),
            ("fnLMS_SaveSettings", self.device.save_settings),
            *(
                ("fnLMS_StartSequence", lambda control=control: self.device.control_sequence(control))
                for control in (START_SEQUENCE, REPEAT_SEQUENCE, PAUSE_SEQUENCE, RESUME_SEQUENCE)
            ),
        )
        for name, operation in actions:
            with self.subTest(name=name, operation=operation):
                self.device.connect()
                self.device._capabilities = replace(self.device.capabilities, is_blx=True)
                executed = []

                def uncertain_call(*args):
                    executed.append(args)  # Simulate a side effect before USB reports its error.
                    return 0x80020000

                with patch.object(self.api.dll, name, uncertain_call):
                    with self.assertRaisesRegex(VaunixLmsError, "BAD_HID_IO"):
                        operation()
                self.assertEqual(len(executed), 1)
                self.assertFalse(self.device.is_connected)

    def test_idempotent_sweep_and_sequence_stop_can_retry(self):
        for name, operation in (
            ("fnLMS_StartSweep", lambda: self.device.start_sweep(False)),
            ("fnLMS_StartSequence", lambda: self.device.control_sequence(STOP_SEQUENCE)),
        ):
            with self.subTest(name=name):
                self.device._capabilities = replace(self.device.capabilities, is_blx=True)
                with (
                    patch.object(self.api.dll, name, side_effect=(0x80020000, 0)) as call,
                    patch("rigol_control.drivers.vaunix_lms.time.sleep"),
                ):
                    operation()
                self.assertEqual(call.call_count, 2)
                self.assertTrue(self.device.is_connected)

    def test_partial_failure_is_not_counted_or_followed_by_another_point(self):
        counter = Counter()
        self.api.dll.power_statuses = [0x80020000, 0]
        controller = TriggerStepController(counter, self.device, scan_points=(ScanPoint(self.values),))
        controller.start()
        self.addCleanup(controller.stop)
        counter.add(3)
        self.assertTrue(wait_until(lambda: controller.snapshot().state is ControllerState.ERROR))
        controller.stop()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.applied_count, 0)
        self.assertEqual(snapshot.received_count, 3)
        self.assertEqual(snapshot.backlog, 3)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])
        self.assertEqual(self.api.dll.opened, [7])

    def test_one_hundred_accumulated_edges_keep_every_frequency_power_pair_in_order(self):
        counter = Counter()
        second = (
            StepValue(StepParameter.FREQUENCY, 1, 6e9),
            StepValue(StepParameter.AMPLITUDE, 1, -5),
        )
        controller = TriggerStepController(
            counter, self.device, scan_points=(ScanPoint(self.values), ScanPoint(second)),
        )
        controller.start()
        self.addCleanup(controller.stop)
        counter.add(100)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 100))
        controller.stop()
        expected = [
            ("frequency", 7, 500_000_000), ("power", 7, -40),
            ("frequency", 7, 600_000_000), ("power", 7, -20),
        ] * 50
        self.assertEqual(self.api.dll.writes, expected)
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual(controller.snapshot().backlog, 0)

    def test_reconnect_invalidates_old_writer_even_when_sdk_reuses_the_handle(self):
        for new_handle in (7, 9):
            with self.subTest(new_handle=new_handle):
                old_writer = self.device.prepare_trigger()
                self.device.disconnect()
                self.api.handle = new_handle
                self.device.connect()
                self.api.dll.writes.clear()
                with self.assertRaisesRegex(VaunixLmsError, "重新布防"):
                    old_writer.apply_steps(self.values)
                self.assertEqual(self.api.dll.writes, [])
                self.device.prepare_trigger().apply_steps(self.values)
                self.assertTrue(all(handle == new_handle for _, handle, _ in self.api.dll.writes))

    def test_virtual_reconnect_also_invalidates_old_writer(self):
        device = VirtualVaunixLmsDevice()
        device.command_delay_ms = 0
        device.connect()
        writer = device.prepare_trigger()
        device.disconnect()
        device.connect()
        original = device.frequency_hz
        with self.assertRaisesRegex(VaunixLmsError, "重新布防"):
            writer.apply_steps(self.values)
        self.assertEqual(device.frequency_hz, original)
        device.prepare_trigger().apply_steps(self.values)
        self.assertEqual(device.frequency_hz, 5e9)

    def test_invalid_batch_is_fully_rejected_before_any_sdk_write(self):
        writer = self.device.prepare_trigger()
        for values in (
            (),
            (self.values[0], StepValue(StepParameter.AMPLITUDE, 1, -7.25)),
            (self.values[0], StepValue(StepParameter.PHASE, 1, 90)),
            (StepValue(StepParameter.FREQUENCY, 2, 5e9),),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                writer.apply_steps(values)
            self.assertEqual(self.api.dll.writes, [])

    def test_disconnected_devices_cannot_prepare_a_trigger_writer(self):
        self.device.disconnect()
        for device in (self.device, VirtualVaunixLmsDevice()):
            with self.subTest(device=type(device).__name__), self.assertRaises(VaunixLmsError):
                device.prepare_trigger()

    def test_transaction_serializes_prepared_writes(self):
        writer = self.device.prepare_trigger()
        entered = threading.Event()
        finished = threading.Event()

        def write():
            entered.set()
            writer.apply_steps(self.values)
            finished.set()

        with self.device.transaction():
            worker = threading.Thread(target=write)
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(finished.is_set())
            self.assertEqual(self.api.dll.writes, [])
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())

    def test_connection_card_status_does_not_wait_for_a_blocked_sdk_write(self):
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        record = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="Test", resource=self.device.resource)
        manager = DeviceConnectionManager()
        manager._vaunix_devices[record.id] = self.device
        manager._vaunix_connected.add(record.id)
        manager._identities[record.id] = self.device.identity
        results = []

        def write(*args):
            entered.set()
            release.wait(3)
            return 0

        def observe():
            results.append((manager.info(record).connected, self.device.identity))
            observed.set()

        with patch.object(self.api.dll, "fnLMS_SetFrequencyEx", write):
            worker = threading.Thread(target=lambda: self.device.set_frequency_hz(5e9))
            reader = threading.Thread(target=observe)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                reader.start()
                self.assertTrue(observed.wait(0.3), "Connection card waited behind USB I/O")
                self.assertEqual(results, [(True, self.device.identity)])
            finally:
                release.set()
                worker.join(3)
                if reader.ident is not None:
                    reader.join(3)

    def test_disconnect_publishes_cached_false_before_sdk_close_returns(self):
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        results = []

        def close(*args):
            entered.set()
            release.wait(3)
            return 0

        def observe():
            results.append((self.device.is_connected, self.device.identity))
            observed.set()

        with patch.object(self.api.dll, "fnLMS_CloseDevice", close):
            worker = threading.Thread(target=self.device.disconnect)
            reader = threading.Thread(target=observe)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                reader.start()
                self.assertTrue(observed.wait(0.3), "Cached status waited behind SDK close")
                self.assertEqual(results, [(False, "")])
            finally:
                release.set()
                worker.join(3)
                if reader.ident is not None:
                    reader.join(3)
