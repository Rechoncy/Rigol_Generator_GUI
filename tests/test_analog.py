import csv
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from rigol_control.analog import AnalogAcquisitionController, AnalogAcquisitionState
from rigol_control.drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
from rigol_control.virtual_lab import VirtualAnalogInput


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class AnalogAcquisitionTests(unittest.TestCase):
    def test_config_rejects_invalid_range_and_duplicate_channels(self) -> None:
        with self.assertRaisesRegex(ValueError, "下限必须小于上限"):
            AnalogInputConfig(("ai0",), 1000, 100, 10, -10).validate()
        with self.assertRaisesRegex(ValueError, "不能重复"):
            AnalogInputConfig(("ai0", "AI0"), 1000, 100).validate()

    def test_virtual_multichannel_acquisition_retention_and_csv(self) -> None:
        config = AnalogInputConfig(
            channels=("SIM::NI_DAQ::1/ai0", "SIM::NI_DAQ::1/ai1"),
            sample_rate_hz=100_000,
            samples_per_read=200,
            minimum_v=-2,
            maximum_v=2,
            terminal_mode="DIFF",
        )
        reader = VirtualAnalogInput(
            config,
            signal_frequency_hz=1000,
            amplitude_v=1.25,
            offset_v=0.1,
            noise_v=0,
        )
        controller = AnalogAcquisitionController(reader, maximum_retained_samples=1000)
        blocks = []
        controller.add_callback(lambda update: blocks.append(update.block) if update.block else None)
        controller.start()
        self.assertTrue(wait_until(lambda: controller.snapshot().total_samples >= 1200))
        controller.stop()

        snapshot = controller.snapshot()
        dataset = controller.dataset()
        self.assertEqual(snapshot.state, AnalogAcquisitionState.STOPPED)
        self.assertEqual(snapshot.retained_samples, 1000)
        self.assertEqual(snapshot.dropped_samples, snapshot.total_samples - 1000)
        self.assertEqual(dataset.first_sample_index, snapshot.dropped_samples)
        self.assertEqual(len(dataset.samples), 2)
        self.assertEqual(len(dataset.samples[0]), 1000)
        self.assertTrue(any(block is not None for block in blocks))

        with tempfile.TemporaryDirectory() as temporary:
            path = controller.save_csv(Path(temporary) / "analog.csv")
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
        self.assertEqual(rows[0][0], "# format")
        self.assertEqual(rows[4][:2], ["sample_index", "elapsed_s"])
        self.assertEqual(rows[4][2:], list(config.channels))
        self.assertEqual(len(rows), 5 + 1000)

    def test_hardware_reader_uses_supported_nidaqmx_terminal_constants(self) -> None:
        class FakeChannels:
            def __init__(self):
                self.calls = []

            def add_ai_voltage_chan(self, channel, **kwargs):
                self.calls.append((channel, kwargs))

        class FakeTiming:
            def __init__(self):
                self.calls = []
                self.samp_clk_rate = 1000.0

            def cfg_samp_clk_timing(self, **kwargs):
                self.calls.append(kwargs)

        class FakeTask:
            def __init__(self):
                self.ai_channels = FakeChannels()
                self.timing = FakeTiming()
                self.started = False
                self.closed = False

            def start(self):
                self.started = True

            def read(self, **kwargs):
                return [[0.1, 0.2], [0.3, 0.4]]

            def stop(self):
                self.started = False

            def close(self):
                self.closed = True

        task = FakeTask()
        nidaqmx_module = types.ModuleType("nidaqmx")
        nidaqmx_module.Task = lambda **kwargs: task
        constants_module = types.ModuleType("nidaqmx.constants")
        constants_module.AcquisitionType = types.SimpleNamespace(CONTINUOUS="continuous")
        constants_module.TerminalConfiguration = types.SimpleNamespace(
            DEFAULT="default",
            RSE="rse",
            NRSE="nrse",
            DIFF="diff",
            PSEUDO_DIFF="pseudo_diff",
        )
        config = AnalogInputConfig(("ai0", "Dev1/ai1"), 1000, 2, -5, 5, "PSEUDODIFF")
        reader = NIDaqAnalogInput("Dev1", config)
        with patch.dict(
            sys.modules,
            {"nidaqmx": nidaqmx_module, "nidaqmx.constants": constants_module},
        ):
            reader.start()
            block = reader.read()
            reader.stop()

        self.assertEqual([call[0] for call in task.ai_channels.calls], ["Dev1/ai0", "Dev1/ai1"])
        self.assertEqual(
            task.ai_channels.calls[0][1]["terminal_config"], "pseudo_diff"
        )
        self.assertEqual(block.samples, ((0.1, 0.2), (0.3, 0.4)))
        self.assertTrue(task.closed)


if __name__ == "__main__":
    unittest.main()
