from dataclasses import replace
import unittest
from unittest.mock import patch

from rigol_control.drivers.vaunix_lms import (
    VaunixLmsDevice, VaunixSequenceElement, VirtualVaunixLmsDevice,
)
from test_vaunix_trigger_dispatch import FakeApi


class VaunixPreflightTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.real = VaunixLmsDevice("VAUNIX::LMS::123", api_factory=lambda: self.api)
        self.real.connect()
        self.addCleanup(self.real.disconnect)
        self.real._capabilities = replace(self.real.capabilities, is_blx=True)
        self.virtual = VirtualVaunixLmsDevice()
        self.virtual.command_delay_ms = 0
        self.virtual.connect()
        self.virtual._caps = replace(self.real.capabilities, model="BLX-VIRTUAL")
        setter_patch = patch.object(self.real, "_set")
        self.sdk = setter_patch.start()
        self.addCleanup(setter_patch.stop)
        self.elements = (VaunixSequenceElement(5e9, -10, True), VaunixSequenceElement(6e9, -5, False))
        self.sequence_settings = dict(start_index=0, dwell_time_ms=10, idle_time_ms=0)
        self.sweep_settings = dict(
            start_hz=5e9, end_hz=6e9, step_hz=1e6, sweep_time_ms=1000,
            dwell_time_ms=10, idle_time_ms=0, upward=True, repeating=False,
            bidirectional=False, external_trigger=False,
        )

    def virtual_configuration(self):
        return (
            tuple(self.virtual.sequence), self.virtual.sequence_start,
            self.virtual.sequence_dwell_ms, self.virtual.sequence_idle_ms,
            self.virtual.sweep_start_hz, self.virtual.sweep_end_hz,
            self.virtual.sweep_step_hz, self.virtual.sweep_time_ms,
            self.virtual.dwell_time_ms, self.virtual.idle_time_ms,
            self.virtual.sweep_up, self.virtual.sweep_repeat,
            self.virtual.sweep_bidirectional, self.virtual.external_sweep_trigger,
        )

    def test_invalid_later_sequence_element_never_writes_earlier_elements(self):
        for invalid in (
            VaunixSequenceElement(999e9, -10, True),
            VaunixSequenceElement(6e9, 100, True),
            VaunixSequenceElement(6e9, -7.25, True),
            VaunixSequenceElement(float("nan"), -10, True),
        ):
            for device in (self.real, self.virtual):
                with self.subTest(invalid=invalid, device=type(device).__name__):
                    before = self.virtual_configuration()
                    with self.assertRaises(ValueError):
                        device.configure_sequence((self.elements[0], invalid), **self.sequence_settings)
                    self.sdk.assert_not_called()
                    self.assertEqual(self.virtual_configuration(), before)

    def test_sequence_integer_settings_are_checked_before_any_element_write(self):
        for key in ("start_index", "dwell_time_ms", "idle_time_ms"):
            for bad in (float("nan"), float("inf"), "bad", 0x80000000):
                for device in (self.real, self.virtual):
                    with self.subTest(key=key, bad=bad, device=type(device).__name__):
                        settings = {**self.sequence_settings, key: bad}
                        before = self.virtual_configuration()
                        with self.assertRaises(ValueError):
                            device.configure_sequence(self.elements, **settings)
                        self.sdk.assert_not_called()
                        self.assertEqual(self.virtual_configuration(), before)

    def test_blx_sweep_time_fields_are_checked_before_any_frequency_write(self):
        for key in ("dwell_time_ms", "idle_time_ms"):
            for bad in (float("nan"), float("inf"), "bad", 0x80000000):
                for device in (self.real, self.virtual):
                    with self.subTest(key=key, bad=bad, device=type(device).__name__):
                        before = self.virtual_configuration()
                        with self.assertRaises(ValueError):
                            device.configure_sweep(**{**self.sweep_settings, key: bad})
                        self.sdk.assert_not_called()
                        self.assertEqual(self.virtual_configuration(), before)

    def test_lms_sweep_duration_is_checked_before_any_frequency_write(self):
        self.real._capabilities = replace(self.real.capabilities, is_blx=False)
        self.virtual._caps = replace(self.virtual.capabilities, is_blx=False)
        for bad in (float("nan"), float("inf"), "bad", 0x80000000):
            for device in (self.real, self.virtual):
                with self.subTest(bad=bad, device=type(device).__name__):
                    before = self.virtual_configuration()
                    with self.assertRaises(ValueError):
                        device.configure_sweep(**{**self.sweep_settings, "sweep_time_ms": bad})
                    self.sdk.assert_not_called()
                    self.assertEqual(self.virtual_configuration(), before)

    def test_sweep_quantization_cannot_collapse_step_or_endpoints(self):
        for changes in (
            {"step_hz": 1}, {"step_hz": 4.9},
            {"start_hz": 5e9 + 1, "end_hz": 5e9 + 2},
        ):
            for device in (self.real, self.virtual):
                with self.subTest(changes=changes, device=type(device).__name__):
                    before = self.virtual_configuration()
                    with self.assertRaisesRegex(ValueError, "10 Hz"):
                        device.configure_sweep(**{**self.sweep_settings, **changes})
                    self.sdk.assert_not_called()
                    self.assertEqual(self.virtual_configuration(), before)

    def test_valid_sequence_uses_matching_sdk_and_virtual_quantization(self):
        elements = (VaunixSequenceElement(5e9 + 6, -7.5, True),)
        for device in (self.real, self.virtual):
            device.configure_sequence(elements, start_index=0, dwell_time_ms=9, idle_time_ms=-1)
        self.assertEqual(self.sdk.call_args_list[0].args, ("fnLMS_SetSequenceElement", 0, 500_000_001, -30, True))
        self.assertEqual(self.sdk.call_args_list[-2].args, ("fnLMS_SetSequenceDwellTime", 10))
        self.assertEqual(self.sdk.call_args_list[-1].args, ("fnLMS_SetSequenceIdleTime", 0))
        self.assertEqual(self.virtual.sequence, [VaunixSequenceElement(5e9 + 10, -7.5, True)])
        self.assertEqual(self.virtual.sequence_dwell_ms, 10)
        self.assertEqual(self.virtual.sequence_idle_ms, 0)

    def test_valid_blx_sweep_quantization_matches_sdk_encoding(self):
        settings = {**self.sweep_settings, "step_hz": 14, "dwell_time_ms": 9, "idle_time_ms": -1}
        for device in (self.real, self.virtual):
            device.configure_sweep(**settings)
        commands = {call.args[0]: call.args[1] for call in self.sdk.call_args_list}
        self.assertEqual(commands["fnLMS_SetFrequencyStepEx"], 1)
        self.assertEqual(commands["fnLMS_SetDwellTime"], 10)
        self.assertEqual(commands["fnLMS_SetIdleTime"], 0)
        self.assertEqual(self.virtual.sweep_step_hz, 10)
        self.assertEqual(self.virtual.dwell_time_ms, 10)
        self.assertEqual(self.virtual.idle_time_ms, 0)

    def test_lms_ignores_blx_only_fields_and_uses_time_in_both_implementations(self):
        self.real._capabilities = replace(self.real.capabilities, is_blx=False)
        self.virtual._caps = replace(self.virtual.capabilities, is_blx=False)
        settings = {**self.sweep_settings, "step_hz": 0, "dwell_time_ms": float("nan"),
                    "idle_time_ms": float("inf"), "sweep_time_ms": 1234}
        for device in (self.real, self.virtual):
            device.configure_sweep(**settings)
        commands = {call.args[0]: call.args[1] for call in self.sdk.call_args_list}
        self.assertEqual(commands["fnLMS_SetSweepTime"], 1234)
        self.assertNotIn("fnLMS_SetFrequencyStepEx", commands)
        self.assertEqual(self.virtual.sweep_time_ms, 1234)
