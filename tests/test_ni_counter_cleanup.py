import sys
import types
import unittest
from unittest.mock import patch

from rigol_control.drivers.ni_counter import NIDaqEdgeCounter, NIDaqError


class CounterCleanupTask:
    def __init__(self, *, start_fails=False, stop_fails=False, close_failures=0):
        self.start_fails = start_fails
        self.stop_fails = stop_fails
        self.close_failures = close_failures
        self.close_calls = 0
        self.stop_calls = 0
        self.closed = False
        self.ci_channels = types.SimpleNamespace(
            add_ci_count_edges_chan=lambda *_args, **_kwargs: types.SimpleNamespace()
        )

    def start(self):
        if self.start_fails:
            raise RuntimeError("counter start failed")

    def stop(self):
        self.stop_calls += 1
        if self.stop_fails:
            raise RuntimeError("counter stop failed")

    def close(self):
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("counter close failed")
        self.closed = True


class NiCounterCleanupTests(unittest.TestCase):
    def fake_nidaqmx(self, task):
        module = types.ModuleType("nidaqmx")
        module.Task = lambda **_kwargs: task
        constants = types.ModuleType("nidaqmx.constants")
        constants.Edge = types.SimpleNamespace(RISING="rising", FALLING="falling")
        return patch.dict(sys.modules, {"nidaqmx": module, "nidaqmx.constants": constants})

    def test_stop_retains_task_after_close_failure_and_retries_cleanup(self):
        for stop_fails in (False, True):
            with self.subTest(stop_fails=stop_fails):
                task = CounterCleanupTask(stop_fails=stop_fails, close_failures=1)
                counter = NIDaqEdgeCounter("Dev1")
                with self.fake_nidaqmx(task):
                    counter.start()
                    with self.assertRaisesRegex(RuntimeError, "counter close failed"):
                        counter.stop()
                    self.assertTrue(counter.is_running)
                    self.assertIs(counter._task, task)
                    self.assertFalse(task.closed)
                    with self.assertRaisesRegex(NIDaqError, "已经运行"):
                        counter.start()
                    counter.stop()
                    self.assertFalse(counter.is_running)
                    self.assertIsNone(counter._task)
                    self.assertTrue(task.closed)
                    self.assertEqual(task.close_calls, 2)

    def test_partial_start_retains_task_when_immediate_close_also_fails(self):
        task = CounterCleanupTask(start_fails=True, close_failures=1)
        counter = NIDaqEdgeCounter("Dev1", terminal="PFI0")
        with self.fake_nidaqmx(task):
            with self.assertRaisesRegex(RuntimeError, "counter close failed"):
                counter.start()
            self.assertTrue(counter.is_running)
            self.assertIs(counter._task, task)
            with self.assertRaisesRegex(NIDaqError, "已经运行"):
                counter.start()
            counter.stop()
            self.assertIsNone(counter._task)
            self.assertFalse(counter.is_running)
            self.assertTrue(task.closed)
            self.assertEqual(task.close_calls, 2)

    def test_partial_start_with_successful_close_releases_task(self):
        task = CounterCleanupTask(start_fails=True)
        counter = NIDaqEdgeCounter("Dev1")
        with self.fake_nidaqmx(task):
            with self.assertRaisesRegex(NIDaqError, "counter start failed"):
                counter.start()
            self.assertFalse(counter.is_running)
            self.assertIsNone(counter._task)
            self.assertTrue(task.closed)
            counter.stop()
            self.assertEqual(task.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
