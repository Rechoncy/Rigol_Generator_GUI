import sys
import threading
import types
import unittest
from unittest.mock import patch

from rigol_control.drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
from rigol_control.drivers.ni_counter import NIDaqError
from rigol_control.drivers.ni_counter import NIDaqEdgeCounter


class AnalogCleanupTask:
    def __init__(self, *, start_fails=False, stop_fails=False, close_failures=0):
        self.start_fails = start_fails
        self.stop_fails = stop_fails
        self.close_failures = close_failures
        self.close_calls = 0
        self.stop_calls = 0
        self.closed = False
        self.ai_channels = types.SimpleNamespace(add_ai_voltage_chan=lambda *_a, **_k: None)
        self.timing = types.SimpleNamespace(
            cfg_samp_clk_timing=lambda **_kwargs: None, samp_clk_rate=1000.0
        )

    def start(self):
        if self.start_fails:
            raise RuntimeError("analog start failed")

    def stop(self):
        self.stop_calls += 1
        if self.stop_fails:
            raise RuntimeError("analog stop failed")

    def close(self):
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("analog close failed")
        self.closed = True


class NiAnalogCleanupTests(unittest.TestCase):
    def fake_nidaqmx(self, task):
        module = types.ModuleType("nidaqmx")
        module.Task = lambda **_kwargs: task
        constants = types.ModuleType("nidaqmx.constants")
        constants.AcquisitionType = types.SimpleNamespace(CONTINUOUS="continuous")
        constants.TerminalConfiguration = types.SimpleNamespace(
            DEFAULT="default", RSE="rse", NRSE="nrse", DIFF="diff", PSEUDO_DIFF="pseudo_diff"
        )
        return patch.dict(sys.modules, {"nidaqmx": module, "nidaqmx.constants": constants})

    def make_reader(self):
        return NIDaqAnalogInput("Dev1", AnalogInputConfig(("ai0",), 1000, 2))

    def test_close_failure_keeps_native_task_until_explicit_retry(self):
        for stop_fails in (False, True):
            with self.subTest(stop_fails=stop_fails):
                task = AnalogCleanupTask(stop_fails=stop_fails, close_failures=1)
                reader = self.make_reader()
                with self.fake_nidaqmx(task):
                    reader.start()
                    with self.assertRaisesRegex(RuntimeError, "analog close failed"):
                        reader.stop()
                    self.assertTrue(reader.is_running)
                    self.assertIs(reader._task, task)
                    self.assertFalse(task.closed)
                    with self.assertRaisesRegex(NIDaqError, "已经运行"):
                        reader.start()
                    reader.stop()
                self.assertFalse(reader.is_running)
                self.assertIsNone(reader._task)
                self.assertTrue(task.closed)
                self.assertEqual(task.close_calls, 2)

    def test_partial_start_and_failed_close_keep_task_and_both_errors(self):
        task = AnalogCleanupTask(start_fails=True, close_failures=1)
        reader = self.make_reader()
        with self.fake_nidaqmx(task):
            with self.assertRaisesRegex(NIDaqError, "analog start failed.*analog close failed"):
                reader.start()
            self.assertTrue(reader.is_running)
            self.assertIs(reader._task, task)
            with self.assertRaisesRegex(NIDaqError, "已经运行"):
                reader.start()
            reader.stop()
        self.assertFalse(reader.is_running)
        self.assertTrue(task.closed)
        self.assertEqual(task.close_calls, 2)

    def test_partial_start_with_successful_close_releases_task(self):
        task = AnalogCleanupTask(start_fails=True)
        reader = self.make_reader()
        with self.fake_nidaqmx(task):
            with self.assertRaisesRegex(NIDaqError, "analog start failed"):
                reader.start()
            reader.stop()
        self.assertFalse(reader.is_running)
        self.assertIsNone(reader._task)
        self.assertTrue(task.closed)
        self.assertEqual(task.close_calls, 1)

    def test_actual_daqmx_close_cannot_turn_native_clear_failure_into_noop_success(self):
        # Use the installed library's real close method, with no Task
        # constructor, device discovery or native driver calls.
        from nidaqmx import Task

        for kind in ("analog", "counter"):
            with self.subTest(kind=kind):
                clear_calls = []

                def failed_clear(handle):
                    clear_calls.append(handle)
                    raise RuntimeError("NATIVE_CLEAR_FAILED")

                task = object.__new__(Task)
                task._handle = 123
                task._saved_name = "in-memory-cleanup-probe"
                task._grpc_options = None
                task._event_handler_lock = threading.RLock()
                task._event_handlers = {}
                task._interpreter = types.SimpleNamespace(
                    stop_task=lambda _handle: None, clear_task=failed_clear
                )
                reader = self.make_reader() if kind == "analog" else NIDaqEdgeCounter("Dev1")
                reader._task = task
                with self.assertRaisesRegex(RuntimeError, "NATIVE_CLEAR_FAILED"):
                    reader.stop()
                self.assertIsNone(task._handle)
                self.assertTrue(reader.is_running)
                with self.assertRaisesRegex(NIDaqError, "原生.*未确认"):
                    reader.stop()
                self.assertTrue(reader.is_running)
                self.assertIs(reader._task, task)
                self.assertEqual(clear_calls, [123])


if __name__ == "__main__":
    unittest.main()
