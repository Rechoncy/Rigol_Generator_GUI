import ctypes
from dataclasses import replace
import os
import subprocess
import sys
import unittest
from threading import RLock
from unittest.mock import patch

from rigol_control.connections import DeviceConnectionManager, DeviceConnectionError
from rigol_control.drivers.vaunix_lms import (
    VaunixDeviceDescriptor, VaunixLmsApi, VaunixLmsDevice, VaunixLmsError,
    locate_vaunix_dll,
)
from rigol_control.models import DeviceKind, DeviceRecord


class FakeDll:
    def __init__(self):
        self.closed = []
        self.opened = []
        self.write_status = 0
        self.close_status = 0
        self.read_status = 0
        self.status = 3

    def fnLMS_InitDevice(self, handle):
        self.opened.append(handle)
        return 0

    def fnLMS_CloseDevice(self, handle):
        self.closed.append(handle)
        return self.close_status

    def fnLMS_SetRFOn(self, handle, enabled):
        return self.write_status

    def fnLMS_GetDeviceStatus(self, handle):
        return self.status

    def __getattr__(self, name):
        values = {
            "fnLMS_GetMinFreqEx": 10000000, "fnLMS_GetMaxFreqEx": 4000000000,
            "fnLMS_GetMinPwr": -160, "fnLMS_GetMaxPwr": 40,
        }
        return lambda *args: self.read_status or values.get(name, 0)


class FakeApi:
    dll_version = 0x010F

    def __init__(self):
        self.dll = FakeDll()
        self._lock = RLock()
        self.handle = 7

    def enumerate_devices(self):
        return (VaunixDeviceDescriptor(self.handle, "LMS-TEST", 123, 3),)


class VaunixRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.device = VaunixLmsDevice("VAUNIX::LMS::123", api_factory=lambda: self.api)
        self.device.connect()

    def test_lms_802_readback_skips_blx_only_features(self):
        self.device._capabilities = replace(self.device.capabilities, model="LMS-802")
        with patch.object(self.api.dll, "fnLMS_GetFrequencyStepEx", return_value=0x80010000) as step, \
             patch.object(self.api.dll, "fnLMS_GetFrequencyEx", return_value=550000000), \
             patch.object(self.api.dll, "fnLMS_GetAbsPowerLevel", return_value=-30):
            snapshot = self.device.snapshot()
            self.assertEqual(snapshot.frequency_hz, 5.5e9)
            self.assertEqual(snapshot.power_dbm, -7.5)
            self.assertIsNone(snapshot.sweep_step_hz)
            step.assert_not_called()

    def test_lms_sweep_uses_time_and_never_writes_blx_step(self):
        with patch.object(self.api.dll, "fnLMS_SetFrequencyStepEx", return_value=0x80010000) as step, \
             patch.object(self.api.dll, "fnLMS_SetSweepTime", return_value=0) as duration:
            self.device.configure_sweep(
                start_hz=4e9, end_hz=8e9, step_hz=0, sweep_time_ms=1500,
                dwell_time_ms=10, idle_time_ms=0, upward=True, repeating=False,
                bidirectional=False, external_trigger=False,
            )
            step.assert_not_called()
            duration.assert_called_once_with(7, 1500)

    def test_blx_retains_step_read_write_and_does_not_swallow_errors(self):
        self.device._capabilities = replace(self.device.capabilities, model="BLX-403", is_blx=True)
        with patch.object(self.api.dll, "fnLMS_GetFrequencyStepEx", return_value=10000), \
             patch.object(self.api.dll, "fnLMS_GetSweepTime", return_value=0x80010000) as duration_read, \
             patch.object(self.api.dll, "fnLMS_SetSweepTime", return_value=0x80010000) as duration_write, \
             patch.object(self.api.dll, "fnLMS_SetFrequencyStepEx", return_value=0) as step:
            self.assertEqual(self.device.snapshot().sweep_step_hz, 100000)
            self.device.configure_sweep(
                start_hz=4e9, end_hz=8e9, step_hz=100000, sweep_time_ms=1500,
                dwell_time_ms=10, idle_time_ms=0, upward=True, repeating=False,
                bidirectional=False, external_trigger=False,
            )
            duration_read.assert_not_called()
            duration_write.assert_not_called()
            step.assert_called_once_with(7, 10000)
        with patch.object(self.api.dll, "fnLMS_GetFrequencyStepEx", return_value=0x80010000):
            with self.assertRaisesRegex(VaunixLmsError, "BAD_PARAMETER"):
                self.device.snapshot()

    def test_basic_output_readback_does_not_query_advanced_features(self):
        with patch.object(self.api.dll, "fnLMS_GetFrequencyStepEx", side_effect=AssertionError("step read")), \
             patch.object(self.api.dll, "fnLMS_GetStartFrequencyEx", side_effect=AssertionError("sweep read")):
            self.assertTrue(self.device.output_snapshot().connected)

    @unittest.skipUnless(sys.platform == "win32", "Vaunix Windows DLL integration")
    def test_vendor_dll_lms_readback_and_sweep_in_isolated_test_mode(self):
        try:
            dll = locate_vaunix_dll()
        except VaunixLmsError as exc:
            self.skipTest(str(exc))
        # Keep the SDK's process-global simulation mode out of other tests and UI.
        script = '''
import ctypes, sys
from rigol_control.drivers.vaunix_lms import VaunixLmsApi, VaunixLmsDevice, VaunixDeviceDescriptor
api = VaunixLmsApi(sys.argv[1])
d = api.dll
d.fnLMS_SetTestMode(True)
assert d.fnLMS_GetNumDevices() == 2
handles = (ctypes.c_uint * 64)()
count = d.fnLMS_GetDevInfo(handles)
descriptors = []
for h in handles[:count]:
    buffer = ctypes.create_string_buffer(32)
    d.fnLMS_GetModelNameA(h, ctypes.cast(buffer, ctypes.c_void_p))
    descriptors.append(VaunixDeviceDescriptor(h, buffer.value.decode(), d.fnLMS_GetSerialNumber(h), 1))
api.enumerate_devices = lambda: tuple(descriptors)
for descriptor in descriptors:
    device = VaunixLmsDevice(descriptor.resource, api_factory=lambda: api)
    try:
        device.connect()
        # Reproduce the screenshot's error through the actual vendor implementation.
        assert d.fnLMS_GetFrequencyStepEx(descriptor.device_id) == 0x80010000
        assert device.output_snapshot().connected
        assert device.snapshot().sweep_step_hz is None
        caps = device.capabilities
        device.configure_sweep(start_hz=caps.minimum_frequency_hz, end_hz=caps.maximum_frequency_hz,
            step_hz=0, sweep_time_ms=1234, dwell_time_ms=10, idle_time_ms=0,
            upward=True, repeating=False, bidirectional=False, external_trigger=False)
        assert device.snapshot().sweep_time_ms == 1234
    finally:
        device.disconnect()
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(dll)], capture_output=True, text=True,
            timeout=20, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_close_failure_clears_session_and_reconnect_uses_new_handle(self):
        self.api.dll.close_status = 0x80020000
        with self.assertRaisesRegex(VaunixLmsError, "BAD_HID_IO"):
            self.device.disconnect()
        self.assertFalse(self.device.is_connected)
        self.assertEqual(self.device.identity, "")
        self.api.handle = 9
        self.device.connect()
        self.assertEqual(self.api.dll.opened, [7, 9])

    def test_terminal_usb_errors_invalidate_without_reopening_stale_handle(self):
        for code in (0x80000000, 0xFFFF0000, 0x80030000, 0xFFFF0003,
                     0x80020000, 0xFFFF0002):
            with self.subTest(code=hex(code)):
                self.device.connect()
                opened = list(self.api.dll.opened)
                self.api.dll.write_status = code
                with patch("rigol_control.drivers.vaunix_lms.time.sleep"):
                    with self.assertRaises(VaunixLmsError):
                        self.device.set_rf_on(True)
                self.assertFalse(self.device.is_connected)
                self.assertEqual(self.api.dll.opened, opened)

    def test_parameter_error_does_not_drop_connection(self):
        self.api.dll.write_status = 0x80010000
        with self.assertRaisesRegex(VaunixLmsError, "BAD_PARAMETER"):
            self.device.set_rf_on(True)
        self.assertTrue(self.device.is_connected)

    def test_disconnected_status_never_returns_cached_snapshot_as_connected(self):
        self.api.dll.status = 0
        with self.assertRaisesRegex(VaunixLmsError, "重新连接"):
            self.device.snapshot()
        self.assertFalse(self.device.is_connected)

    def test_failed_capability_read_preserves_error_and_cleans_up(self):
        self.device.disconnect()
        self.api.dll.read_status = 0x80030000
        self.api.dll.close_status = 0x80020000
        with self.assertRaisesRegex(VaunixLmsError, "DEVICE_NOT_READY"):
            self.device.connect()
        self.assertFalse(self.device.is_connected)

    def test_valid_negative_power_and_unsigned_frequency_are_preserved(self):
        self.assertEqual(self.device.capabilities.minimum_power_dbm, -40)
        self.assertEqual(self.device.capabilities.maximum_frequency_hz, 40e9)

    def test_manager_reconnects_without_waiting_for_ui_poll(self):
        manager = DeviceConnectionManager()
        record = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="Test", resource=self.device.resource)
        manager._vaunix_devices[record.id] = self.device
        manager._vaunix_resources[record.id] = record.resource
        manager.connect(record)
        self.device.disconnect()
        self.api.handle = 12
        self.assertTrue(manager.connect(record).connected)
        self.assertEqual(self.api.dll.opened[-1], 12)
        self.api.dll.close_status = 0x80020000
        with self.assertRaises(DeviceConnectionError):
            manager.disconnect(record)
        self.assertNotIn(record.id, manager._vaunix_connected)
        self.assertNotIn(record.id, manager._identities)

    def test_enumeration_skips_device_unplugged_during_metadata_read(self):
        class DiscoveryDll:
            def fnLMS_SetTestMode(self, value):
                pass
            def fnLMS_GetNumDevices(self):
                return 2
            def fnLMS_GetDevInfo(self, handles):
                handles[0], handles[1] = 7, 8
                return 2
            def fnLMS_GetModelNameA(self, handle, buffer):
                ctypes.memmove(buffer, b"LMS-TEST\0", 9)
                return 8
            def fnLMS_GetSerialNumber(self, handle):
                return 123 if handle == 7 else -2147483648
            def fnLMS_GetDeviceStatus(self, handle):
                return 1
        api = object.__new__(VaunixLmsApi)
        api.dll = DiscoveryDll()
        api._lock = RLock()
        self.assertEqual([d.serial_number for d in api.enumerate_devices()], [123])
