import time
import unittest
from unittest.mock import patch

from rigol_control.analog import AnalogAcquisitionController, AnalogAcquisitionSnapshot, AnalogAcquisitionState
from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.newport_smc100 import VIRTUAL_SMC100_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.drivers.ni_analog import AnalogInputConfig
from rigol_control.rotation_scan import RotationScanConfig, RotationScanController
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE


class _Counter:
    def __init__(self, *, fail_start=False, fail_stop=True):
        self.fail_start = fail_start
        self.fail_stop = fail_stop
        self.starts = self.stops = 0

    def start(self):
        self.starts += 1
        if self.fail_start:
            raise RuntimeError("counter start outcome uncertain")

    def read_count(self):
        return 0

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("counter close failed")


class _Analog:
    def __init__(self):
        self.stops = 0
        self.fail_stop = True

    def add_callback(self, callback):
        pass

    def start(self):
        pass

    def snapshot(self):
        return AnalogAcquisitionSnapshot(
            state=AnalogAcquisitionState.RUNNING, channel_names=("Dev1/ai0",),
            sample_rate_hz=1000, total_samples=0, retained_samples=0, dropped_samples=0,
        )

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            raise RuntimeError("analog reader remains active")


class RotationCleanupRetryTests(unittest.TestCase):
    def setUp(self):
        self.manager = DeviceConnectionManager()
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="Stage", resource=VIRTUAL_SMC100_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        self.manager.connect(self.stage)
        self.manager.connect(self.ni)
        self.device = self.manager.newport_device_for(self.stage)
        self.controller = None

    def tearDown(self):
        if self.controller is not None:
            if isinstance(self.controller.counter, _Counter):
                self.controller.counter.fail_stop = False
            if isinstance(self.controller.analog, _Analog):
                self.controller.analog.fail_stop = False
            self.controller.stop()
        self.manager.close_all()

    def make(self, counter, analog=None):
        self.controller = RotationScanController(
            self.device, counter, RotationScanConfig(point_count=3, settle_seconds=0),
            manager=self.manager, device_ids=(self.stage.id, self.ni.id), analog=analog,
        )
        return self.controller

    def wait(self, predicate):
        end = time.monotonic() + 2
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.002)
        return predicate()

    def finish_worker(self, controller):
        controller.request_stop()
        controller._thread.join(2)
        self.assertFalse(controller._thread.is_alive())

    def assert_reserved(self, controller):
        self.assertTrue(controller.is_running)
        self.assertTrue(controller.cleanup_pending)
        self.assertEqual(self.manager.lease_owner(self.ni.id), controller.owner)
        with self.assertRaises(DeviceBusyError):
            self.manager.acquire_devices("other-task", (self.ni.id,))
        with self.assertRaises(RuntimeError):
            controller.start()

    def test_analog_stop_failure_retains_lease_until_explicit_cleanup_retry(self):
        analog = _Analog()
        counter = _Counter(fail_stop=False)
        controller = self.make(counter, analog)
        controller.start()
        self.assertTrue(self.wait(lambda: controller.snapshot().phase == "holding"))
        self.finish_worker(controller)
        self.assert_reserved(controller)
        self.assertEqual(analog.stops, 1)
        self.assertEqual(controller.snapshot().phase, "error")
        analog.fail_stop = False
        controller.stop()
        self.assertFalse(controller.is_running)
        self.assertIsNone(self.manager.lease_owner(self.ni.id))
        self.assertEqual(analog.stops, 2)
        self.assertEqual(counter.stops, 1)
        self.assertEqual(self.device.move_history, [])

    def test_counter_stop_failure_retries_cleanup_without_rearming_or_moving(self):
        counter = _Counter()
        controller = self.make(counter)
        controller.start()
        self.assertTrue(self.wait(lambda: controller.snapshot().phase == "holding"))
        self.finish_worker(controller)
        self.assert_reserved(controller)
        with self.assertRaisesRegex(RuntimeError, "停止未确认"):
            controller.stop()
        self.assert_reserved(controller)
        counter.fail_stop = False
        controller.stop()
        self.assertFalse(controller.is_running)
        self.assertEqual(counter.starts, 1)
        self.assertEqual(controller.snapshot().completed, 0)
        self.assertEqual(self.device.move_history, [])
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_failed_counter_start_still_requires_confirmed_cleanup(self):
        counter = _Counter(fail_start=True)
        controller = self.make(counter)
        controller.start()
        controller._thread.join(2)
        self.assert_reserved(controller)
        self.assertIn("start outcome uncertain", controller.snapshot().error)
        self.assertEqual(counter.stops, 1)
        counter.fail_stop = False
        controller.stop()
        self.assertFalse(controller.is_running)
        self.assertEqual(counter.starts, 1)

    def test_unconfirmed_motion_stop_retains_both_leases_and_retries_only_stop(self):
        controller = self.make(_Counter(fail_stop=False))
        controller.start()
        self.assertTrue(self.wait(lambda: controller.snapshot().phase == "holding"))
        # Exercise the same finalizer reached after an uncertain MOVE outcome.
        controller._motion_cleanup_pending = True
        with patch.object(self.device, "stop", side_effect=RuntimeError("stop reply lost")):
            self.finish_worker(controller)
        self.assert_reserved(controller)
        self.assertIn("stop reply lost", controller.snapshot().error)
        history = list(self.device.move_history)
        controller.stop()
        self.assertFalse(controller.is_running)
        self.assertEqual(self.device.move_history, history)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_rotation_log_failure_from_analog_callback_stops_acquisition_and_scan(self):
        reader = self.manager.analog_input_for(
            self.ni, AnalogInputConfig((f"{VIRTUAL_NI_RESOURCE}/ai0",), 1000, 10),
        )
        analog = AnalogAcquisitionController(reader, maximum_retained_samples=1000)
        controller = self.make(_Counter(fail_stop=False), analog)
        original_record = controller._record

        def fail_block_record(event, block=None):
            if event == "analog_block_received":
                raise OSError("ROTATION_LOG_DISK_FULL")
            original_record(event, block)

        with patch.object(controller, "_record", side_effect=fail_block_record):
            controller.start()
            self.assertTrue(self.wait(lambda: controller._thread is not None and not controller._thread.is_alive()))
        self.assertIn("ROTATION_LOG_DISK_FULL", controller.snapshot().error)
        self.assertEqual(analog.snapshot().state, AnalogAcquisitionState.ERROR)
        self.assertFalse(analog.is_running)
        self.assertEqual(self.device.move_history, [])
        self.assertIsNone(self.manager.lease_owner(self.ni.id))


if __name__ == "__main__":
    unittest.main()
