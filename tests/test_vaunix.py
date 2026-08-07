import time
import unittest
from threading import RLock

from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.drivers.vaunix_lms import (
    PAUSE_SEQUENCE,
    REPEAT_SEQUENCE,
    VIRTUAL_VAUNIX_RESOURCE,
    VaunixCapabilities,
    VaunixLmsDevice,
    VaunixSequenceElement,
    VirtualVaunixLmsDevice,
    _power_units,
    format_dll_version,
    validate_power_dbm,
)
from rigol_control.scan import ScanPoint
from rigol_control.trigger import ControllerState, TriggerStepController
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE, VirtualLab


class VaunixVirtualDeviceTests(unittest.TestCase):
    def test_real_driver_retries_transient_hid_status_without_fixed_delay(self) -> None:
        class FakeDll:
            def __init__(self) -> None:
                self.frequency_calls = 0
                self.init_calls = 0

            def fnLMS_SetFrequencyEx(self, _device_id, _frequency) -> int:
                self.frequency_calls += 1
                return 0x80020000 if self.frequency_calls == 1 else 0

            def fnLMS_InitDevice(self, _device_id) -> int:
                self.init_calls += 1
                return 0

        class FakeApi:
            def __init__(self) -> None:
                self.dll = FakeDll()
                self._lock = RLock()

        api = FakeApi()
        device = VaunixLmsDevice("VAUNIX::LMS::123")
        device._api = api
        device._device_id = 7
        device._capabilities = VaunixCapabilities(
            model="LMS-TEST",
            serial_number=123,
            dll_version=0x010F,
            minimum_frequency_hz=1e9,
            maximum_frequency_hz=20e9,
            minimum_power_dbm=-40.0,
            maximum_power_dbm=10.0,
            has_fast_pulse=False,
            has_chirp=False,
            is_blx=False,
        )
        device.set_frequency_hz(5e9)
        self.assertEqual(api.dll.frequency_calls, 2)
        self.assertEqual(api.dll.init_calls, 0)

    def test_sdk_version_uses_packed_decimal_minor_byte(self) -> None:
        self.assertEqual(format_dll_version(0x010F), "1.15")

    def test_power_uses_quarter_db_api_encoding_but_half_db_hardware_grid(self) -> None:
        self.assertEqual(_power_units(-7.5), -30)
        self.assertEqual(validate_power_dbm(3.5), 3.5)
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            _power_units(-7.25)

    def setUp(self) -> None:
        self.device = VirtualVaunixLmsDevice(VIRTUAL_VAUNIX_RESOURCE)
        self.device.command_delay_ms = 0
        self.device.connect()

    def test_full_feature_surface_updates_snapshot(self) -> None:
        self.device.set_frequency_hz(12_345_678_900)
        self.device.set_power_dbm(-7.5)
        self.device.set_rf_on(True)
        self.device.set_internal_reference(False)
        self.device.configure_sweep(
            start_hz=5e9,
            end_hz=7e9,
            step_hz=100e6,
            sweep_time_ms=500,
            dwell_time_ms=20,
            idle_time_ms=30,
            upward=True,
            repeating=True,
            bidirectional=True,
            external_trigger=True,
        )
        self.device.start_sweep(True)
        self.device.configure_pulse(
            on_seconds=2e-6,
            period_seconds=10e-6,
            enabled=True,
            external_enabled=True,
        )
        self.device.configure_chirp(
            enabled=True,
            length_seconds=20e-6,
            repeat_seconds=100e-6,
            external_trigger=True,
        )
        sequence = (
            VaunixSequenceElement(5e9, -10.0, True),
            VaunixSequenceElement(6e9, -5.0, False),
        )
        self.device.configure_sequence(
            sequence,
            start_index=1,
            dwell_time_ms=25,
            idle_time_ms=50,
        )
        self.device.control_sequence(REPEAT_SEQUENCE)
        self.device.control_sequence(PAUSE_SEQUENCE)
        self.device.save_settings()

        snapshot = self.device.snapshot()
        self.assertAlmostEqual(snapshot.frequency_hz, 12_345_678_900)
        self.assertAlmostEqual(snapshot.power_dbm, -7.5)
        self.assertTrue(snapshot.rf_on)
        self.assertFalse(snapshot.internal_reference)
        self.assertTrue(snapshot.sweep_active)
        self.assertTrue(snapshot.sweep_repeat)
        self.assertTrue(snapshot.sweep_bidirectional)
        self.assertTrue(snapshot.external_sweep_trigger)
        self.assertTrue(snapshot.internal_pulse_enabled)
        self.assertTrue(snapshot.external_pulse_enabled)
        self.assertEqual(snapshot.sequence, sequence)
        self.assertEqual(snapshot.sequence_start, 1)
        self.assertEqual(self.device.saved_settings_count, 1)

    def test_ni_counter_drives_frequency_and_power_without_losing_edges(self) -> None:
        lab = VirtualLab()
        counter = lab.edge_counter_for(VIRTUAL_NI_RESOURCE)
        points = (
            ScanPoint(
                (
                    StepValue(StepParameter.FREQUENCY, 1, 5e9),
                    StepValue(StepParameter.AMPLITUDE, 1, -10.0),
                )
            ),
            ScanPoint(
                (
                    StepValue(StepParameter.FREQUENCY, 1, 6e9),
                    StepValue(StepParameter.AMPLITUDE, 1, -5.0),
                )
            ),
        )
        controller = TriggerStepController(counter, self.device, scan_points=points)
        controller.start()
        lab.emit_trigger(VIRTUAL_NI_RESOURCE, 5)
        deadline = time.monotonic() + 2.0
        while controller.snapshot().applied_count < 5 and time.monotonic() < deadline:
            time.sleep(0.005)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.state, ControllerState.RUNNING)
        self.assertEqual(snapshot.received_count, 5)
        self.assertEqual(snapshot.applied_count, 5)
        self.assertEqual(snapshot.current_index, 0)
        self.assertAlmostEqual(self.device.frequency_hz, 5e9)
        self.assertAlmostEqual(self.device.power_dbm, -10.0)
        controller.stop()

    def test_virtual_device_rejects_values_real_sdk_would_reject(self) -> None:
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            self.device.set_power_dbm(-7.25)
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            self.device.set_sequence_element(0, 5e9, -9.25, True)
        original_frequency = self.device.frequency_hz
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            self.device.apply_steps(
                (
                    StepValue(StepParameter.FREQUENCY, 1, 8e9),
                    StepValue(StepParameter.AMPLITUDE, 1, -7.25),
                )
            )
        self.assertEqual(self.device.frequency_hz, original_frequency)
        with self.assertRaisesRegex(ValueError, "周期"):
            self.device.configure_pulse(
                on_seconds=1e-3,
                period_seconds=0.5e-3,
                enabled=True,
                external_enabled=False,
            )
        with self.assertRaisesRegex(ValueError, "1 至 50"):
            self.device.configure_sequence(
                (),
                start_index=0,
                dwell_time_ms=10,
                idle_time_ms=0,
            )


if __name__ == "__main__":
    unittest.main()
