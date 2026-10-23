import threading
import time
import tempfile
import unittest
from pathlib import Path

from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import LinkageAxis, LinkageRunLogger, MultiDeviceTriggerController, generate_linkage_points
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState, TriggerStepController


class Counter:
    def __init__(self):
        self.count = 0
        self.starts = 0
        self.stops = 0
        self.fail_close = False
        self.fail_read = False

    def start(self):
        self.starts += 1

    def read_count(self):
        if self.fail_read:
            raise RuntimeError("injected read failure")
        return self.count

    def stop(self):
        self.stops += 1
        if self.fail_close:
            raise RuntimeError("injected close failure")


class BlockingWriter:
    def __init__(self, *, block_prepare=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_prepare = block_prepare
        self.writes = 0

    def prepare_trigger(self):
        if self.block_prepare:
            self.entered.set()
            if not self.release.wait(4):
                raise RuntimeError("test prepare release missing")
        return self

    def apply_steps(self, values):
        self.entered.set()
        if not self.release.wait(4):
            raise RuntimeError("test writer release missing")
        self.writes += 1


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class TriggerShutdownTests(unittest.TestCase):
    def make_controller(self, kind, counter, writer):
        if kind == "single":
            return TriggerStepController(counter, writer, StepParameter.FREQUENCY, 2, [1000, 2000])
        points = generate_linkage_points(
            [LinkageAxis("dg", StepParameter.FREQUENCY, 2, "Hz", 1000, 1000, 2)],
            ScanMode.SYNCHRONOUS,
        )
        return MultiDeviceTriggerController(counter, {"dg": writer}, points)

    def cleanup(self, controller, writer):
        writer.release.set()
        controller.counter.fail_close = False
        controller.stop()

    def test_stop_timeout_retains_workers_and_rejects_restart(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter()
                controller = self.make_controller(kind, counter, writer)
                try:
                    controller.start()
                    counter.count = 3
                    self.assertTrue(writer.entered.wait(1))
                    with self.assertRaisesRegex(RuntimeError, "停止尚未完成"):
                        controller.stop(timeout_seconds=0.02)
                    self.assertEqual(controller.snapshot().state, ControllerState.STOPPING)
                    self.assertFalse(controller.is_quiescent)
                    self.assertIsNotNone(controller._writer_thread)
                    with self.assertRaises(RuntimeError):
                        controller.start()
                    writer.release.set()
                    controller.stop()
                    self.assertTrue(controller.is_quiescent)
                    self.assertEqual(writer.writes, 1)
                    self.assertEqual(controller.snapshot().applied_count, 1)
                finally:
                    self.cleanup(controller, writer)

    def test_stop_during_preflight_does_not_start_ni(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter(block_prepare=True)
                controller = self.make_controller(kind, counter, writer)
                startup = threading.Thread(target=controller.start)
                try:
                    startup.start()
                    self.assertTrue(writer.entered.wait(1))
                    with self.assertRaisesRegex(RuntimeError, "启动检查"):
                        controller.stop(timeout_seconds=0.02)
                    self.assertFalse(controller.is_quiescent)
                    writer.release.set()
                    startup.join(1)
                    self.assertFalse(startup.is_alive())
                    controller.stop()
                    self.assertTrue(controller.is_quiescent)
                    self.assertEqual(counter.starts, 0)
                    self.assertEqual(writer.writes, 0)
                finally:
                    writer.release.set()
                    startup.join(1)
                    self.cleanup(controller, writer)

    def test_start_reservation_cannot_erase_concurrent_stop(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter()
                controller = self.make_controller(kind, counter, writer)
                original = controller._start_run
                reserved, proceed = threading.Event(), threading.Event()

                def delayed_start():
                    reserved.set()
                    if not proceed.wait(4):
                        raise RuntimeError("test startup release missing")
                    original()

                controller._start_run = delayed_start
                startup = threading.Thread(target=controller.start)
                try:
                    startup.start()
                    self.assertTrue(reserved.wait(1))
                    with self.assertRaises(RuntimeError):
                        controller.stop(timeout_seconds=0.02)
                    proceed.set()
                    startup.join(1)
                    controller.stop()
                    self.assertEqual(counter.starts, 0)
                    self.assertTrue(controller.is_quiescent)
                finally:
                    proceed.set()
                    startup.join(1)
                    self.cleanup(controller, writer)

    def test_stop_during_ni_start_closes_it_before_releasing_ownership(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter()
                entered, release = threading.Event(), threading.Event()
                original_start = counter.start

                def delayed_counter_start():
                    entered.set()
                    if not release.wait(4):
                        raise RuntimeError("test NI start release missing")
                    original_start()

                counter.start = delayed_counter_start
                controller = self.make_controller(kind, counter, writer)
                startup = threading.Thread(target=controller.start)
                try:
                    startup.start()
                    self.assertTrue(entered.wait(1))
                    with self.assertRaisesRegex(RuntimeError, "启动检查"):
                        controller.stop(timeout_seconds=0.02)
                    self.assertFalse(controller.is_quiescent)
                    release.set()
                    startup.join(1)
                    controller.stop()
                    self.assertTrue(controller.is_quiescent)
                    self.assertEqual(counter.starts, 1)
                    self.assertEqual(counter.stops, 1)
                    self.assertEqual(writer.writes, 0)
                finally:
                    release.set()
                    startup.join(1)
                    self.cleanup(controller, writer)

    def test_counter_close_failure_keeps_ownership_until_retry(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter()
                controller = self.make_controller(kind, counter, writer)
                try:
                    controller.start()
                    counter.fail_close = True
                    with self.assertRaisesRegex(RuntimeError, "NI 计数任务尚未确认关闭"):
                        controller.stop()
                    self.assertEqual(controller.snapshot().state, ControllerState.ERROR)
                    self.assertFalse(controller.is_quiescent)
                    with self.assertRaises(RuntimeError):
                        controller.start()
                    counter.fail_close = False
                    controller.stop()
                    self.assertTrue(controller.is_quiescent)
                    self.assertEqual(counter.stops, 2)
                finally:
                    self.cleanup(controller, writer)

    def test_monitor_error_does_not_hide_inflight_writer(self):
        for kind in ("single", "multi"):
            with self.subTest(kind=kind):
                counter, writer = Counter(), BlockingWriter()
                controller = self.make_controller(kind, counter, writer)
                try:
                    controller.start()
                    counter.count = 2
                    self.assertTrue(writer.entered.wait(1))
                    counter.fail_read = True
                    self.assertTrue(wait_until(lambda: controller.snapshot().state is ControllerState.ERROR))
                    self.assertFalse(controller.is_quiescent)
                    with self.assertRaises(RuntimeError):
                        controller.start()
                    with self.assertRaises(RuntimeError):
                        controller.stop(timeout_seconds=0.02)
                    writer.release.set()
                    controller.stop()
                    self.assertTrue(controller.is_quiescent)
                    self.assertEqual(writer.writes, 1)
                    self.assertEqual(controller.snapshot().state, ControllerState.ERROR)
                finally:
                    self.cleanup(controller, writer)

    def test_snapshots_have_monotonic_creation_revision(self):
        for kind in ("single", "multi"):
            controller = self.make_controller(kind, Counter(), BlockingWriter())
            first, second = controller.snapshot(), controller.snapshot()
            self.assertGreater(first.revision, 0)
            self.assertGreater(second.revision, first.revision)

    def test_serial_stop_skips_remaining_device_and_does_not_count_partial_point(self):
        counter, first, second = Counter(), BlockingWriter(), BlockingWriter()
        second.release.set()
        points = generate_linkage_points(
            [LinkageAxis(device, StepParameter.FREQUENCY, 2, "Hz", 1000, 0, 1)
             for device in ("first", "second")],
            ScanMode.SYNCHRONOUS,
        )
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "run.csv"
            controller = MultiDeviceTriggerController(
                counter, {"first": first, "second": second}, points,
                device_order=("first", "second"), logger=LinkageRunLogger(log),
            )
            try:
                controller.start()
                counter.count = 1
                self.assertTrue(first.entered.wait(1))
                controller.request_stop()
                first.release.set()
                controller.stop()
                self.assertEqual(first.writes, 1)
                self.assertEqual(second.writes, 0)
                self.assertEqual(controller.snapshot().applied_count, 0)
                self.assertEqual(controller.snapshot().state, ControllerState.STOPPED)
                self.assertIn("partial_stopped", log.read_text(encoding="utf-8-sig"))
            finally:
                self.cleanup(controller, first)


if __name__ == "__main__":
    unittest.main()
