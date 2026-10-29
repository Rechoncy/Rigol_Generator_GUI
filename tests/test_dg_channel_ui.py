import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository
from rigol_control.trigger import ControllerState
from rigol_control.ui.device_windows import BaseDeviceWindow, DG4000Window
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class DgNativeChannelUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or create_application(["dg-channel-ui-test"])

    def setUp(self) -> None:
        self.record = DeviceRecord.create(
            kind=DeviceKind.DG4000,
            name="Virtual DG4000",
            resource=VIRTUAL_DG_RESOURCE,
        )
        self.manager = DeviceConnectionManager()
        self.window = DG4000Window(self.record, [], self.manager)
        self.addCleanup(self._close_window)
        self.manager.connect(self.record)
        self.manager.virtual_lab.set_scpi_delay(self.record.resource, 0)

    def _close_window(self) -> None:
        self.window.close()
        self.manager.close_all()
        self.app.processEvents()

    def _wait_for(self, condition, timeout=4.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.005)
        return False

    def test_native_sweep_and_burst_snapshot_selected_channel_before_async_write(self) -> None:
        queued = []

        def submit(operation, *, on_success=None, on_error=None, after_write=False):
            queued.append((operation, on_success))

        self.window.submit = submit
        lab = self.manager.virtual_lab
        generator = lab.scpi_session_for(self.record.resource)._generator
        for selector, button, label, expected_count in (
            (self.window.sweep_channel, self.window.sweep_apply_button, "扫频", 6),
            (self.window.burst_channel, self.window.burst_apply_button, "脉冲串", 5),
        ):
            with self.subTest(mode=label):
                # A pending UI task must retain CH2 even if the selection changes
                # before its worker executes. CH1 is configured independently.
                generator.channels[1].update(
                    frequency_hz=1234567.0,
                    amplitude_vpp=2.75,
                    phase_deg=123.0,
                    output=True,
                )
                before = lab.generator_snapshot(self.record.resource).channels[0]
                selector.setCurrentIndex(selector.findData(2))
                generator.commands.clear()
                button.click()
                selector.setCurrentIndex(selector.findData(1))
                self.assertEqual(len(queued), 1)
                operation, on_success = queued.pop()
                result = operation()
                on_success(result)
                commands = [item for item in generator.commands if not item.endswith("?")]
                self.assertEqual(len(commands), expected_count)
                self.assertTrue(all(item.startswith(":SOUR2:") for item in commands))
                self.assertEqual(lab.generator_snapshot(self.record.resource).channels[0], before)
                self.assertIn(f"CH2 {label}", self.window.statusBar().currentMessage())

                # The same control still deliberately targets CH1 when selected.
                generator.commands.clear()
                button.click()
                operation, on_success = queued.pop()
                on_success(operation())
                commands = [item for item in generator.commands if not item.endswith("?")]
                self.assertEqual(len(commands), expected_count)
                self.assertTrue(all(item.startswith(":SOUR1:") for item in commands))

    def test_sweep_and_burst_channels_persist_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = DeviceParameterRepository(Path(directory))
            self.window.parameter_repository = repository
            self.window.sweep_channel.setCurrentIndex(1)
            self.window.burst_channel.setCurrentIndex(0)
            self.window._save_device_parameters(force=True)
            restored = DG4000Window(self.record, [], parameter_repository=repository)
            try:
                self.assertEqual(restored.sweep_channel.currentData(), 2)
                self.assertEqual(restored.burst_channel.currentData(), 1)
            finally:
                restored.close()
                self.app.processEvents()
            # Avoid the fixture later trying to save into a removed temp folder.
            self.window.parameter_repository = None

    def test_manual_commands_cannot_interrupt_a_running_ch2_scan(self) -> None:
        ni = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="Virtual NI",
            resource=VIRTUAL_NI_RESOURCE,
        )
        self.manager.connect(ni)
        self.window.ni_devices = [ni]
        self.window.refresh_ni_devices(force=True)
        axis = self.window.scan_axes[0]
        axis.channel.setCurrentIndex(axis.channel.findData(2))
        axis.parameter.setCurrentIndex(axis.parameter.findData(StepParameter.FREQUENCY))
        axis.start.setValue(2)
        axis.step.setValue(1)
        axis.count.setValue(3)
        self.window._generate_scan_table(show_message=False)
        lab = self.manager.virtual_lab
        generator = lab.scpi_session_for(self.record.resource)._generator
        generator.channels[1].update(frequency_hz=31415.0, output=True, phase_deg=42.0)
        ch1_before = lab.generator_snapshot(self.record.resource).channels[0]
        errors = []
        self.window._show_hardware_error = errors.append
        self.window._start_trigger()
        self.assertTrue(self._wait_for(lambda: self.window.controller is not None
                                       and self.window.controller.snapshot().state is ControllerState.RUNNING))
        generator.commands.clear()
        self.window._apply_channel(1)
        self.window._apply_modulation("PSK")
        self.window._apply_output_load(2)
        for command in ("*RST", ":COUP:STAT ON", ":SOUR1:FREQ 123"):
            self.window._execute_scpi(command)
        self.assertEqual(len(errors), 6)
        self.assertTrue(all("请先停止任务" in error for error in errors))
        self.assertFalse(generator.commands)
        lab.emit_trigger(ni.resource, 2)
        self.assertTrue(self._wait_for(lambda: self.window.controller.snapshot().applied_count == 2))
        self.assertEqual(lab.generator_snapshot(self.record.resource).channels[0], ch1_before)
        self.assertEqual(lab.generator_snapshot(self.record.resource).channels[1].frequency_hz, 3000)
        self.assertIs(self.window.controller.snapshot().state, ControllerState.RUNNING)
        self.window._stop_trigger()
        self.assertTrue(self._wait_for(lambda: self.window.controller is None))
        self.assertIsNone(self.manager.lease_owner(self.record.id))

    def test_manual_command_rechecks_a_lease_acquired_after_submission(self) -> None:
        queued = []

        def capture_submit(window, operation, *, on_success=None, on_error=None):
            queued.append(operation)

        generator = self.manager.virtual_lab.scpi_session_for(self.record.resource)._generator
        generator.commands.clear()
        with patch.object(BaseDeviceWindow, "submit", capture_submit):
            self.window._execute_scpi("*RST")
        self.assertEqual(len(queued), 1)
        self.manager.acquire_devices("linkage:other-window", (self.record.id,))
        try:
            with self.assertRaisesRegex(DeviceBusyError, "请先停止任务"):
                queued.pop()()
            self.assertFalse(generator.commands)
            errors = []
            self.window._show_hardware_error = errors.append
            self.window._execute_scpi(":COUP:STAT ON")
            self.assertEqual(len(errors), 1)
            self.assertFalse(generator.commands)
        finally:
            self.manager.release_devices("linkage:other-window")


if __name__ == "__main__":
    unittest.main()
