import threading
import unittest

from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import (
    LinkageAxis, MultiDeviceTriggerController, generate_linkage_points,
)
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState
from test_linkage import FakeCounter, RecordingWriter, wait_until


class NativeScan:
    def __init__(self, events=None, name="ssg"):
        self.events = events if events is not None else []
        self.name = name
        self.requires_stop = False
        self.prepare_calls = self.arm_calls = self.stop_calls = 0
        self.prepare_hook = self.arm_hook = self.stop_hook = None
        self.fail_prepare = self.fail_arm = self.fail_stop = False

    def prepare(self):
        self.prepare_calls += 1
        self.events.append(self.name + ":prepare")
        if self.prepare_hook:
            self.prepare_hook()
        if self.fail_prepare:
            self.requires_stop = True
            raise RuntimeError("NATIVE_PREPARE_FAILED")

    def arm(self):
        self.arm_calls += 1
        self.events.append(self.name + ":arm")
        # A failed write can already have enabled the physical instrument.
        self.requires_stop = True
        if self.arm_hook:
            self.arm_hook()
        if self.fail_arm:
            raise RuntimeError("NATIVE_ARM_FAILED")

    def stop(self):
        self.stop_calls += 1
        self.events.append(self.name + ":stop")
        if self.stop_hook:
            self.stop_hook()
        if self.fail_stop:
            raise RuntimeError("NATIVE_OFF_UNCONFIRMED")
        self.requires_stop = False


class ObservedCounter(FakeCounter):
    def __init__(self, events=None):
        super().__init__()
        self.events = events if events is not None else []
        self.fail_start = self.fail_stop = self.fail_read = False
        self.stop_calls = 0

    def start(self):
        self.events.append("counter:start")
        super().start()
        if self.fail_start:
            raise RuntimeError("COUNTER_START_FAILED")

    def read_count(self):
        if self.fail_read:
            raise RuntimeError("COUNTER_READ_FAILED")
        return super().read_count()

    def stop(self):
        self.events.append("counter:stop")
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("COUNTER_STOP_FAILED")
        super().stop()


class RecoverableNative(NativeScan):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recovery_calls = 0
        self.fail_recovery = False

    def recover_and_stop(self):
        self.recovery_calls += 1
        self.events.append(self.name + ":recover-stop")
        if self.fail_recovery:
            raise RuntimeError("NATIVE_RECOVERY_OFF_UNCONFIRMED")
        self.requires_stop = False


class NativeLinkageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.counter = ObservedCounter(self.events)
        self.native = NativeScan(self.events)
        self.writer = RecordingWriter("dg", self.events)
        self.controllers = []

    def tearDown(self):
        self.native.fail_prepare = self.native.fail_arm = self.native.fail_stop = False
        self.counter.fail_stop = self.counter.fail_read = False
        for controller in self.controllers:
            for participant in controller.native_participants.values():
                participant.fail_stop = False
                participant.fail_recovery = False
            controller.stop()

    def controller(self, *, natives=None, writers=None, native_only=False):
        if natives is None:
            natives = {"ssg": self.native}
        if writers is None:
            writers = {} if native_only else {"dg": self.writer}
        axes = tuple(
            LinkageAxis(device_id, StepParameter.FREQUENCY, 1, "Hz", 1000, 100, 2)
            for device_id in (*writers, *natives)
        )
        controller = MultiDeviceTriggerController(
            self.counter, writers,
            generate_linkage_points(axes, ScanMode.SYNCHRONOUS),
            native_participants=natives, monitor_poll_seconds=.001,
        )
        self.controllers.append(controller)
        return controller

    def test_native_is_prepared_before_counter_and_armed_before_first_software_p0(self):
        controller = self.controller()
        controller.start()
        self.assertEqual(self.events[:3], ["ssg:prepare", "counter:start", "ssg:arm"])
        self.assertEqual(self.writer.batches, [])
        self.counter.add(1)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 1))
        self.assertEqual(self.writer.batches[0][0].value, 1000)
        self.assertEqual(self.native.arm_calls, 1)

    def test_edges_dispatch_only_software_and_never_report_native_acceptance(self):
        controller = self.controller()
        controller.start()
        self.counter.add(5)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 5))
        snapshot = controller.snapshot()
        self.assertEqual([batch[0].value for batch in self.writer.batches],
                         [1000, 1100, 1000, 1100, 1000])
        self.assertEqual([item.device_id for item in snapshot.device_results], ["dg"])
        self.assertEqual((self.native.prepare_calls, self.native.arm_calls, self.native.stop_calls), (1, 1, 0))

    def test_native_only_monitors_edges_without_software_completion_or_fake_point(self):
        controller = self.controller(native_only=True)
        controller.start()
        self.counter.add(3)
        self.assertTrue(wait_until(lambda: controller.snapshot().received_count == 3))
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.applied_count, 0)
        self.assertIsNone(snapshot.current_index)
        self.assertEqual(snapshot.device_results, ())
        self.assertIsNone(controller._writer_thread)

    def test_same_device_cannot_be_software_and_native(self):
        points = generate_linkage_points(
            (LinkageAxis("dg", StepParameter.FREQUENCY, 1, "Hz", 1000, 100, 2),),
            ScanMode.SYNCHRONOUS,
        )
        with self.assertRaisesRegex(ValueError, "同时"):
            MultiDeviceTriggerController(
                self.counter, {"dg": self.writer}, points,
                native_participants={"dg": self.native},
            )

    def test_counter_start_failure_does_not_arm_and_cleans_partial_counter(self):
        self.counter.fail_start = True
        controller = self.controller()
        with self.assertRaisesRegex(RuntimeError, "COUNTER_START_FAILED"):
            controller.start()
        self.assertEqual(self.native.arm_calls, 0)
        self.assertEqual(self.counter.stop_calls, 1)
        self.assertTrue(controller.is_quiescent)
        self.assertEqual(self.writer.batches, [])

    def test_prepare_failure_cleans_unknown_native_and_never_starts_counter(self):
        self.native.fail_prepare = True
        controller = self.controller()
        with self.assertRaisesRegex(RuntimeError, "NATIVE_PREPARE_FAILED"):
            controller.start()
        self.assertNotIn("counter:start", self.events)
        self.assertEqual(self.native.stop_calls, 1)
        self.assertFalse(self.native.requires_stop)
        self.assertTrue(controller.is_quiescent)

    def test_arm_failure_stops_every_already_or_possibly_armed_participant(self):
        second = NativeScan(self.events, "ssg2")
        second.fail_arm = True
        controller = self.controller(natives={"ssg": self.native, "ssg2": second})
        with self.assertRaisesRegex(RuntimeError, "NATIVE_ARM_FAILED"):
            controller.start()
        self.assertEqual((self.native.stop_calls, second.stop_calls), (1, 1))
        self.assertEqual(self.counter.stop_calls, 1)
        self.assertTrue(controller.is_quiescent)

    def test_trigger_during_arm_rejects_start_and_does_not_replay_p0(self):
        self.native.arm_hook = lambda: self.counter.add(1)
        controller = self.controller()
        with self.assertRaisesRegex(RuntimeError, "暂停外部触发"):
            controller.start()
        self.assertEqual(self.writer.batches, [])
        self.assertEqual(self.native.stop_calls, 1)
        self.assertEqual(controller.snapshot().applied_count, 0)
        self.assertTrue(controller.is_quiescent)

    def test_baseline_read_failure_stops_armed_native(self):
        self.counter.fail_read = True
        controller = self.controller()
        with self.assertRaisesRegex(RuntimeError, "COUNTER_READ_FAILED"):
            controller.start()
        self.assertEqual(self.native.stop_calls, 1)
        self.assertTrue(controller.is_quiescent)

    def test_cancel_during_prepare_never_arms_or_starts_counter(self):
        controller = self.controller()
        self.native.prepare_hook = controller.request_stop
        controller.start()
        self.assertEqual(self.native.arm_calls, 0)
        self.assertNotIn("counter:start", self.events)
        self.assertTrue(controller.is_quiescent)
        self.assertIs(controller.snapshot().state, ControllerState.STOPPED)

    def test_cancel_during_arm_stops_native_and_counter_without_software_writes(self):
        controller = self.controller()
        self.native.arm_hook = controller.request_stop
        controller.start()
        self.assertEqual(self.native.stop_calls, 1)
        self.assertEqual(self.counter.stop_calls, 1)
        self.assertEqual(self.writer.batches, [])
        self.assertTrue(controller.is_quiescent)

    def test_cancel_cleanup_failure_is_not_automatically_repeated(self):
        controller = self.controller()
        self.native.arm_hook = controller.request_stop
        self.native.fail_stop = True
        with self.assertRaisesRegex(RuntimeError, "清理未确认"):
            controller.start()
        self.assertEqual(self.native.stop_calls, 1)
        self.assertFalse(controller.is_quiescent)
        self.native.fail_stop = False
        controller.stop()
        self.assertEqual(self.native.stop_calls, 2)
        self.assertTrue(controller.is_quiescent)

    def test_native_stop_failure_retains_manager_lease_until_explicit_retry(self):
        controller = self.controller()
        manager = DeviceConnectionManager()
        record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG",
                                     resource="VIRTUAL::NATIVE-LIFECYCLE")
        manager.acquire_devices("native-test", (record.id,))
        manager.watch_output_task("native-test", (record,), controller)
        controller.start()
        self.native.fail_stop = True
        with self.assertRaises(RuntimeError):
            controller.stop()
        self.assertFalse(controller.is_quiescent)
        self.assertTrue(self.native.requires_stop)
        with self.assertRaises(DeviceBusyError):
            manager.release_devices("native-test")
        self.assertEqual(manager.lease_owner(record.id), "native-test")
        self.native.fail_stop = False
        controller.stop()
        manager.release_devices("native-test")
        self.assertIsNone(manager.lease_owner(record.id))

    def test_failed_counter_cleanup_still_attempts_every_native_stop(self):
        second = NativeScan(self.events, "ssg2")
        controller = self.controller(natives={"ssg": self.native, "ssg2": second})
        controller.start()
        self.counter.fail_stop = self.native.fail_stop = True
        with self.assertRaises(RuntimeError):
            controller.stop()
        self.assertEqual((self.native.stop_calls, second.stop_calls), (1, 1))
        self.assertFalse(second.requires_stop)
        self.assertFalse(controller.is_quiescent)
        self.counter.fail_stop = self.native.fail_stop = False
        controller.stop()
        self.assertTrue(controller.is_quiescent)
        self.assertEqual(second.stop_calls, 1)

    def test_software_failure_stops_native_without_replaying_partial_point(self):
        self.writer.fail = True
        controller = self.controller()
        controller.start()
        self.counter.add(2)
        self.assertTrue(wait_until(lambda: controller.snapshot().state is ControllerState.ERROR))
        self.assertTrue(wait_until(lambda: controller.is_quiescent))
        self.assertEqual(self.native.stop_calls, 1)
        self.assertEqual(self.events.count("dg"), 1)
        self.assertEqual(controller.snapshot().applied_count, 0)

    def test_native_cleanup_does_not_block_cached_controller_status(self):
        entered, release = threading.Event(), threading.Event()
        def block_stop():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("NATIVE_STOP_WAIT_EXPIRED")
        self.native.stop_hook = block_stop
        controller = self.controller()
        controller.start()
        errors = []
        def stop():
            try:
                controller.stop()
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=stop)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertIsNotNone(controller.snapshot())
            self.assertFalse(controller.is_quiescent)
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(controller.is_quiescent)

    def test_automatic_failure_never_recovers_and_explicit_retry_may_recover_once(self):
        native = RecoverableNative(self.events)
        native.fail_stop = True
        self.writer.fail = True
        controller = self.controller(natives={"ssg": native})
        controller.start()
        self.counter.add(1)
        self.assertTrue(wait_until(lambda: native.stop_calls == 1))
        self.assertTrue(wait_until(lambda: controller._writer_thread is not None
                                  and not controller._writer_thread.is_alive()))
        self.assertEqual(native.recovery_calls, 0)
        self.assertFalse(controller.is_quiescent)
        controller.stop()
        self.assertEqual(native.recovery_calls, 1)
        self.assertEqual(native.stop_calls, 1)
        self.assertTrue(controller.is_quiescent)
        self.assertFalse(controller._explicit_native_retry)
        self.assertIsNone(controller._explicit_native_retry_thread)

    def test_failed_explicit_native_recovery_retains_state_until_another_explicit_retry(self):
        native = RecoverableNative(self.events)
        native.fail_stop = native.fail_recovery = True
        controller = self.controller(natives={"ssg": native})
        controller.start()
        with self.assertRaises(RuntimeError):
            controller.stop()
        self.assertEqual(native.recovery_calls, 0)
        with self.assertRaisesRegex(RuntimeError, "NATIVE_RECOVERY_OFF_UNCONFIRMED"):
            controller.stop()
        self.assertEqual(native.recovery_calls, 1)
        self.assertTrue(native.requires_stop)
        self.assertFalse(controller.is_quiescent)
        self.assertFalse(controller._explicit_native_retry)
        native.fail_recovery = False
        controller.stop()
        self.assertEqual(native.recovery_calls, 2)
        self.assertTrue(controller.is_quiescent)


if __name__ == "__main__":
    unittest.main()
