import csv
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import LinkageAxis, LinkageRunLogger, MultiDeviceTriggerController, generate_linkage_points
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState


class Counter:
    def __init__(self):
        self.count = 0

    def start(self):
        pass

    def stop(self):
        pass

    def read_count(self):
        return self.count


class Writer:
    def __init__(self, *, fail=False, block=False):
        self.fail = fail
        self.block = block
        self.entered = threading.Event()
        self.release = threading.Event()
        self.batches = []

    def apply_steps(self, values):
        self.entered.set()
        if self.block and not self.release.wait(3):
            raise RuntimeError("test writer not released")
        if self.fail:
            raise RuntimeError("device B failed")
        self.batches.append(values)


class BrokenLogger:
    def __init__(self):
        self.attempts = []

    def write(self, **kwargs):
        self.attempts.append(kwargs)
        raise OSError("disk full")


class LinkageLogFailureTests(unittest.TestCase):
    def make_controller(self, **second_kwargs):
        counter, first, second, logger = Counter(), Writer(), Writer(**second_kwargs), BrokenLogger()
        points = generate_linkage_points(
            [LinkageAxis(device, StepParameter.FREQUENCY, 2, "Hz", 1000, 1000, 2)
             for device in ("a", "b")], ScanMode.SYNCHRONOUS,
        )
        controller = MultiDeviceTriggerController(counter, {"a": first, "b": second}, points, logger=logger)
        return controller, first, second, logger

    def wait_error(self, controller):
        deadline = time.monotonic() + 1
        while controller.snapshot().state is not ControllerState.ERROR and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertEqual(controller.snapshot().state, ControllerState.ERROR)

    def test_completed_point_remains_completed_if_log_fails(self):
        controller, first, second, logger = self.make_controller()
        try:
            controller.start()
            controller.counter.count = 3
            self.wait_error(controller)
            controller.stop()
            snapshot = controller.snapshot()
            self.assertEqual(snapshot.applied_count, 1)
            self.assertEqual(snapshot.current_index, 0)
            self.assertEqual(len(snapshot.device_results), 2)
            self.assertTrue(all(result.success for result in snapshot.device_results))
            self.assertIn("本点全部设备已完成下发", snapshot.error)
            self.assertIn("disk full", snapshot.error)
            self.assertEqual([len(writer.batches) for writer in (first, second)], [1, 1])
            self.assertEqual(len(logger.attempts), 1)
        finally:
            controller.stop()

    def test_partial_results_and_device_error_survive_log_failure(self):
        controller, first, second, logger = self.make_controller(fail=True)
        try:
            controller.start()
            controller.counter.count = 2
            self.wait_error(controller)
            controller.stop()
            snapshot = controller.snapshot()
            self.assertEqual(snapshot.applied_count, 0)
            self.assertEqual([result.success for result in snapshot.device_results], [True, False])
            self.assertIn("device B failed", snapshot.error)
            self.assertIn("disk full", snapshot.error)
            self.assertEqual(len(first.batches), 1)
            self.assertEqual(len(second.batches), 0)
            self.assertEqual(logger.attempts[0]["status"], "partial_error")
        finally:
            controller.stop()

    def test_log_failure_after_cancel_retains_partial_result(self):
        controller, first, second, logger = self.make_controller()
        first.block = True
        try:
            controller.start()
            controller.counter.count = 1
            self.assertTrue(first.entered.wait(1))
            controller.request_stop()
            first.release.set()
            controller.stop()
            snapshot = controller.snapshot()
            self.assertEqual(snapshot.state, ControllerState.ERROR)
            self.assertEqual(snapshot.applied_count, 0)
            self.assertEqual([r.device_id for r in snapshot.device_results], ["a"])
            self.assertIn("部分执行", snapshot.error)
            self.assertEqual(len(second.batches), 0)
            self.assertEqual(logger.attempts[0]["status"], "partial_stopped")
        finally:
            first.release.set()
            controller.stop()

    def test_existing_empty_log_receives_header(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.csv"
            path.touch()
            logger = LinkageRunLogger(path)
            logger.write(trigger_number=1, point_index=0, dispatch_started_at=time.time(),
                         results=(), status="complete", error=None)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
            self.assertEqual(rows[0][0], "trigger_number")
            self.assertEqual(rows[1][0], "1")
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
