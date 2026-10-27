import os
import time
import unittest
from threading import Event, RLock, Thread, Timer
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.drivers.vaunix_lms import (
    VIRTUAL_VAUNIX_RESOURCE, VaunixDeviceDescriptor, VaunixLmsDevice, VaunixLmsError,
)
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.ui.vaunix_window import VaunixLmsWindow


class ReadbackDll:
    def __init__(self):
        self.calls = []
        self.values = {
            "fnLMS_GetMinFreqEx": 50_000_000,
            "fnLMS_GetMaxFreqEx": 4_000_000_000,
            "fnLMS_GetMinPwr": -160,
            "fnLMS_GetMaxPwr": 40,
            "fnLMS_GetDeviceStatus": 3,
            "fnLMS_GetFrequencyEx": 3_500_000_001,
            "fnLMS_GetAbsPowerLevel": -31,
            "fnLMS_GetRF_On": 1,
        }

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            result = self.values.get(name, 0)
            return result.pop(0) if isinstance(result, list) else result
        return call


class ReadbackApi:
    dll_version = 0x010F

    def __init__(self):
        self.dll = ReadbackDll()
        self._lock = RLock()

    def enumerate_devices(self):
        return (VaunixDeviceDescriptor(7, "LMS-TEST", 123, 3),)


class VaunixReadbackDriverTests(unittest.TestCase):
    def setUp(self):
        self.api = ReadbackApi()
        self.device = VaunixLmsDevice("VAUNIX::LMS::123", api_factory=lambda: self.api)
        self.device.connect()
        self.api.dll.calls.clear()
        self.addCleanup(self.device.disconnect)

    def test_unsigned_frequency_and_absolute_negative_power_use_only_documented_getters(self):
        generation = self.device.connection_generation
        reading = self.device.read_output_parameters()
        self.assertEqual(reading.frequency_hz, 35_000_000_010)
        self.assertEqual(reading.power_dbm, -7.75)
        self.assertTrue(reading.rf_on)
        self.assertEqual(reading.connection_generation, generation)
        self.assertEqual([name for name, _ in self.api.dll.calls], [
            "fnLMS_GetDeviceStatus", "fnLMS_GetFrequencyEx", "fnLMS_GetAbsPowerLevel",
            "fnLMS_GetRF_On", "fnLMS_GetDeviceStatus",
        ])
        self.assertEqual(self.device.connection_generation, generation)

    def test_zero_power_and_rf_off_are_not_treated_as_missing(self):
        self.api.dll.values.update(fnLMS_GetAbsPowerLevel=0, fnLMS_GetRF_On=0)
        reading = self.device.read_output_parameters()
        self.assertEqual(reading.power_dbm, 0)
        self.assertFalse(reading.rf_on)

    def test_sdk_error_is_not_retried_or_followed_by_close_or_reconnect(self):
        generation = self.device.connection_generation
        self.api.dll.values["fnLMS_GetFrequencyEx"] = 0xFFFF0002
        with self.assertRaisesRegex(VaunixLmsError, "GetFrequencyEx.*BAD_HID_IO"):
            self.device.read_output_parameters()
        self.assertEqual([name for name, _ in self.api.dll.calls], [
            "fnLMS_GetDeviceStatus", "fnLMS_GetFrequencyEx",
        ])
        self.assertTrue(self.device.is_connected)
        self.assertEqual(self.device.connection_generation, generation)

    def test_invalid_values_discard_the_group(self):
        for name, value in (
            ("fnLMS_GetFrequencyEx", 0), ("fnLMS_GetFrequencyEx", -1),
            ("fnLMS_GetFrequencyEx", 4_000_000_001),
            ("fnLMS_GetAbsPowerLevel", -161), ("fnLMS_GetAbsPowerLevel", 41),
            ("fnLMS_GetRF_On", 2), ("fnLMS_GetRF_On", "ON"),
        ):
            with self.subTest(name=name, value=value):
                old = self.api.dll.values[name]
                self.api.dll.values[name] = value
                with self.assertRaisesRegex(VaunixLmsError, name):
                    self.device.read_output_parameters()
                self.api.dll.values[name] = old

    def test_unplugged_status_never_returns_a_partial_snapshot(self):
        generation = self.device.connection_generation
        self.api.dll.values["fnLMS_GetDeviceStatus"] = [3, 0]
        with self.assertRaisesRegex(VaunixLmsError, "回读期间 USB 已断开"):
            self.device.read_output_parameters()
        self.assertEqual(self.device.connection_generation, generation)
        self.assertTrue(all(name.startswith("fnLMS_Get") for name, _ in self.api.dll.calls))

    def test_read_does_not_invalidate_prepared_trigger_writer(self):
        writer = self.device.prepare_trigger()
        self.device.read_output_parameters()
        self.api.dll.calls.clear()
        writer.apply_steps((StepValue(StepParameter.FREQUENCY, 1, 6e9),))
        self.assertEqual(self.api.dll.calls, [("fnLMS_SetFrequencyEx", (7, 600_000_000))])

    def test_generation_lookup_never_waits_for_an_existing_io_transaction(self):
        entered, release = Event(), Event()
        def block():
            with self.device.transaction():
                entered.set()
                release.wait(3)
        owner = Thread(target=block)
        owner.start()
        self.assertTrue(entered.wait(1))
        watchdog = Timer(1, release.set)
        watchdog.start()
        try:
            started = time.monotonic()
            self.assertGreater(self.device.connection_generation, 0)
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()
            watchdog.cancel()
            owner.join(3)


class VaunixReadbackUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["vaunix-readback-ui"])

    def setUp(self):
        self.record = DeviceRecord.create(
            kind=DeviceKind.VAUNIX_LMS, name="Virtual Vaunix", resource=VIRTUAL_VAUNIX_RESOURCE,
        )
        self.manager = DeviceConnectionManager()
        self.manager.connect(self.record)
        self.window = VaunixLmsWindow(self.record, [], self.manager)
        self.window.show()
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.device = self.window.device
        self.device.command_delay_ms = 0
        self.device.frequency_hz = 5_123_456_780
        self.device.power_dbm = -7.5
        self.device.rf_on = True

    def tearDown(self):
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.manager.release_devices("vaunix-readback-test")
        self.window.close()
        self.manager.close_all()
        self.app.processEvents()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        return predicate()

    def refresh(self):
        self.window.output_readback_button.click()
        self.assertTrue(self.wait(lambda: not self.window._output_readback_pending and not self.window._call_tasks))

    def assert_unknown(self):
        self.assertTrue(all(label.text() == "未知" for label in self.window.output_readback_values.values()))
        self.assertNotIn("回读时间", self.window.output_readback_status.text())

    def test_display_keeps_edits_and_device_settings_and_never_polls(self):
        edits = self.window._device_parameter_snapshot()
        snapshot = self.device.snapshot()
        with patch.object(self.device, "read_output_parameters", wraps=self.device.read_output_parameters) as read:
            self.refresh()
            self.assertEqual(self.window.output_readback_values["frequency"].text(), "5123456780 Hz")
            self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-7.5 dBm")
            self.assertEqual(self.window.output_readback_values["output"].text(), "开启")
            self.assertIn("回读时间", self.window.output_readback_status.text())
            self.assertEqual(self.device.snapshot(), snapshot)
            self.assertEqual(self.window._device_parameter_snapshot(), edits)
            self.wait(lambda: False, timeout=0.15)
            read.assert_called_once_with()

    def test_failure_clears_old_group_and_can_retry_without_reconnection(self):
        self.refresh()
        generation = self.device.connection_generation
        self.device.power_dbm = float("nan")
        self.refresh()
        self.assert_unknown()
        self.assertIn("GetAbsPowerLevel", self.window.output_readback_status.text())
        self.assertEqual(self.device.connection_generation, generation)
        self.device.power_dbm = -10
        self.device.rf_on = False
        self.refresh()
        self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-10 dBm")
        self.assertEqual(self.window.output_readback_values["output"].text(), "关闭（未输出）")

    def test_slow_read_is_async_and_coalesces_clicks(self):
        entered, release = Event(), Event()
        original = self.device.read_output_parameters
        def slow():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test read not released")
            return original()
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            with patch.object(self.device, "read_output_parameters", side_effect=slow) as read:
                self.window._refresh_output_readback()
                self.assertTrue(entered.wait(1))
                self.window._refresh_output_readback()
                self.assertFalse(self.window.output_readback_button.isEnabled())
                self.assertTrue(self.wait(lambda: len(ticks) >= 3, timeout=0.4))
                release.set()
                self.assertTrue(self.wait(lambda: not self.window._call_tasks))
                read.assert_called_once_with()
        finally:
            release.set()
            timer.stop()

    def test_click_does_not_wait_for_existing_trigger_transaction(self):
        entered, release = Event(), Event()
        def busy():
            with self.device.transaction():
                entered.set()
                release.wait(3)
        owner = Thread(target=busy)
        owner.start()
        self.assertTrue(entered.wait(1))
        watchdog = Timer(1, release.set)
        watchdog.start()
        try:
            started = time.monotonic()
            self.window._refresh_output_readback()
            self.assertLess(time.monotonic() - started, 0.3)
        finally:
            release.set()
            watchdog.cancel()
            owner.join(3)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))

    def test_manual_read_is_allowed_during_lease_and_preserves_prepared_writer(self):
        writer = self.device.prepare_trigger()
        self.manager.acquire_devices("vaunix-readback-test", [self.record.id])
        self.refresh()
        self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-7.5 dBm")
        errors = []
        self.window.submit(lambda: self.device.set_frequency_hz(6e9), on_error=errors.append)
        self.assertTrue(errors)
        self.assertEqual(self.device.frequency_hz, 5_123_456_780)
        writer.apply_steps((StepValue(StepParameter.FREQUENCY, 1, 6e9),))
        self.assertEqual(self.device.frequency_hz, 6e9)

    def test_reconnect_before_result_delivery_discards_old_result(self):
        original = self.device.read_output_parameters
        def old_connection():
            reading = original()
            self.device.disconnect()
            self.device.connect()
            return reading
        with patch.object(self.device, "read_output_parameters", side_effect=old_connection):
            self.refresh()
        self.assert_unknown()
        self.assertIn("连接已变化", self.window.output_readback_status.text())

    def test_fresh_read_after_external_reconnect_survives_connection_timer_sync(self):
        self.device.disconnect()
        self.device.connect()
        generation = self.device.connection_generation
        self.assertNotEqual(self.window._ui_connection_generation, generation)
        entered, release = Event(), Event()
        original = self.device.read_output_parameters

        def slow_new_connection():
            entered.set()
            if not release.wait(3):
                raise TimeoutError("test read not released")
            return original()

        try:
            with patch.object(self.device, "read_output_parameters", side_effect=slow_new_connection):
                self.window.output_readback_button.click()
                self.assertTrue(entered.wait(1))
                self.window.sync_connection_state()
                release.set()
                self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertEqual(self.window.output_readback_values["frequency"].text(), "5123456780 Hz")
            self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-7.5 dBm")
            self.assertIn("回读时间", self.window.output_readback_status.text())
            self.assertEqual(self.window._ui_connection_generation, generation)
        finally:
            release.set()

    def test_queued_read_is_cancelled_before_querying_a_replacement_connection(self):
        entered, reconnect = Event(), Event()
        def replace():
            with self.device.transaction():
                entered.set()
                reconnect.wait(3)
                self.device.disconnect()
                self.device.connect()
        owner = Thread(target=replace)
        owner.start()
        self.assertTrue(entered.wait(1))
        try:
            with patch.object(self.device, "read_output_parameters", wraps=self.device.read_output_parameters) as read:
                self.window._refresh_output_readback()
                reconnect.set()
                self.assertTrue(self.wait(lambda: not self.window._call_tasks))
                read.assert_not_called()
            self.assert_unknown()
        finally:
            reconnect.set()
            owner.join(3)

    def test_disconnect_and_reconnect_invalidate_snapshot_and_overview_has_shortcut(self):
        self.window.overview_readback_button.click()
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.manager.disconnect(self.record)
        self.window.sync_connection_state()
        self.assert_unknown()
        self.assertFalse(self.window.output_readback_button.isEnabled())
        self.manager.connect(self.record)
        self.window.sync_connection_state()
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.assert_unknown()
        self.refresh()
        self.device.disconnect()
        self.device.connect()
        self.window.sync_connection_state()
        self.assert_unknown()


if __name__ == "__main__":
    unittest.main()
