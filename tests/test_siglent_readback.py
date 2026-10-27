import os
import time
import unittest
from threading import Event, Thread, Timer
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.drivers.siglent_ssg import (
    SiglentSsgDriver, SsgCommandError, SsgSweep, VirtualSsgBackend, VIRTUAL_SSG_RESOURCE,
)
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.ui.siglent_window import SiglentWindow


class SiglentReadbackDriverTests(unittest.TestCase):
    def setUp(self):
        self.backend = VirtualSsgBackend(clock=lambda: 0)
        self.session = ScpiSession("SIM::readback", backend_factory=lambda *_: self.backend)
        self.session.connect()
        self.driver = SiglentSsgDriver(self.session)
        self.driver.identify()
        self.backend.commands.clear()

    def tearDown(self):
        self.session.disconnect()

    def test_known_models_use_only_the_three_common_queries_and_preserve_state(self):
        for model in ("SSG3021X", "SSG3032X", "SSG6083A", "SSG6085A", "SSG6087A", "SSG6082A-V"):
            with self.subTest(model=model):
                self.backend.identity = f"SIGLENT,{model},SN,1"
                self.driver.identify()
                self.driver.prepared = SsgSweep()
                self.driver.state = "enabled"
                self.driver.scan_may_be_active = True
                self.backend.put("FREQuency", "1234567890.123456")
                self.backend.put("POWer", "-31.125")
                self.backend.put("OUTPut", "1")
                before = dict(self.backend.values)
                self.backend.commands.clear()
                result = self.driver.read_output_parameters()
                self.assertEqual(result.frequency_hz, 1234567890.123456)
                self.assertEqual(result.power_dbm, -31.125)
                self.assertTrue(result.output_enabled)
                self.assertEqual(result.connection_generation, self.session.connection_generation)
                self.assertEqual(self.backend.commands, [":FREQuency?", ":POWer?", ":OUTPut?"])
                self.assertEqual(self.backend.values, before)
                self.assertEqual(self.driver.prepared, SsgSweep())
                self.assertEqual(self.driver.state, "enabled")
                self.assertTrue(self.driver.scan_may_be_active)

    def test_invalid_frequency_power_and_output_reject_the_snapshot_with_raw_reply(self):
        for command, bad in ((":FREQuency?", "0"), (":FREQuency?", "NaN"),
                             (":FREQuency?", "BAD_HZ"), (":POWer?", "inf"),
                             (":POWer?", "BAD_DBM"), (":OUTPut?", "2")):
            with self.subTest(command=command, bad=bad):
                original = self.session.query
                def invalid(cmd, **kwargs):
                    return bad if cmd == command else original(cmd, **kwargs)
                with patch.object(self.session, "query", side_effect=invalid):
                    with self.assertRaises(SsgCommandError) as caught:
                        self.driver.read_output_parameters()
                self.assertIn(command, str(caught.exception))
                self.assertIn(repr(bad), str(caught.exception))
                self.assertTrue(self.session.is_connected)

    def test_transport_failure_never_recovers_or_releases_scan_evidence(self):
        self.driver.state = "enabled"
        self.driver.scan_may_be_active = True
        generation = self.session.connection_generation
        with patch.object(self.backend, "query", side_effect=TimeoutError("injected timeout")), \
                patch.object(self.session, "connect") as connect:
            with self.assertRaisesRegex(SsgCommandError, "injected timeout"):
                self.driver.read_output_parameters()
            connect.assert_not_called()
        self.assertEqual(self.session.connection_generation, generation)
        self.assertTrue(self.session.is_connected)
        self.assertTrue(self.driver.scan_may_be_active)
        self.assertEqual(self.driver.state, "enabled")

    def test_unknown_model_sends_no_query(self):
        self.backend.identity = "SIGLENT,SSG_UNKNOWN,SN,1"
        self.driver.identify()
        self.backend.commands.clear()
        with self.assertRaises(ValueError):
            self.driver.read_output_parameters()
        self.assertEqual(self.backend.commands, [])

    def test_generation_change_during_group_is_rejected(self):
        original = self.session.query
        def changed(command, **kwargs):
            result = original(command, **kwargs)
            if command == ":OUTPut?":
                self.session._advance_connection_generation()
            return result
        with patch.object(self.session, "query", side_effect=changed):
            with self.assertRaisesRegex(SsgCommandError, "连接已变化"):
                self.driver.read_output_parameters()


class SiglentReadbackUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["ssg-readback-ui"])

    def setUp(self):
        self.record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG", resource=VIRTUAL_SSG_RESOURCE)
        self.manager = DeviceConnectionManager()
        self.window = SiglentWindow(self.record, self.manager)
        self.window.show()
        self.window.connect_device()
        self.idle()
        self.window.poll_timer.stop()
        self.backend = self.window.session._backend
        self.backend.put("FREQuency", "1234567890.123456")
        self.backend.put("POWer", "-31.125")
        self.backend.commands.clear()

    def tearDown(self):
        self.idle()
        self.manager.release_devices(self.window.owner)
        self.manager.release_devices("other-task")
        self.window.driver.scan_may_be_active = False
        self.window.close()
        self.manager.close_all()
        self.app.processEvents()

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(.003)
        return predicate()

    def idle(self):
        self.assertTrue(self.wait(lambda: not self.window._busy and not self.window._call_tasks
                                 and not self.window._pending_safety))

    def refresh(self):
        self.window.output_readback_button.click()
        self.idle()

    def assert_unknown(self):
        self.assertTrue(all(value.text() == "未知" for value in self.window.output_readback_values.values()))

    def test_manual_snapshot_has_native_units_and_preserves_editors_without_polling(self):
        self.window.frequency.setValue(1.5e9)
        self.window.power.setValue(-10)
        editors = self.window._device_parameter_snapshot()
        self.refresh()
        values = self.window.output_readback_values
        self.assertEqual(values["frequency"].text(), "1234567890.123456 Hz")
        self.assertEqual(values["amplitude"].text(), "-31.125 dBm")
        self.assertEqual(values["output"].text(), "关闭（未输出）")
        self.assertIn("回读时间", self.window.output_readback_status.text())
        self.assertEqual(self.window._device_parameter_snapshot(), editors)
        self.assertEqual(self.backend.commands, [":FREQuency?", ":POWer?", ":OUTPut?"])
        self.wait(lambda: False, timeout=.1)
        self.assertEqual(len(self.backend.commands), 3)

    def test_error_invalidates_whole_snapshot_and_retry_keeps_connection(self):
        self.refresh()
        generation = self.window.session.connection_generation
        original = self.window.session.query
        def invalid(command, **kwargs):
            return "BAD_RF_POWER" if command == ":POWer?" else original(command, **kwargs)
        with patch.object(self.window.session, "query", side_effect=invalid), \
                patch.object(QMessageBox, "warning") as warning:
            self.refresh()
            warning.assert_not_called()
        self.assert_unknown()
        self.assertIn("BAD_RF_POWER", self.window.output_readback_status.text())
        self.assertNotIn("回读时间", self.window.output_readback_status.text())
        self.assertEqual(self.window.session.connection_generation, generation)
        self.refresh()
        self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-31.125 dBm")

    def test_readback_during_native_scan_preserves_lease_and_stop_evidence_even_on_error(self):
        self.manager.acquire_devices(self.window.owner, [self.record.id])
        self.window.driver.state = "enabled"
        self.window.driver.scan_may_be_active = True
        self.refresh()
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        self.assertTrue(self.window.driver.scan_may_be_active)
        with patch.object(self.backend, "query", side_effect=TimeoutError("scan-read timeout")):
            self.refresh()
        self.assert_unknown()
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        self.assertEqual(self.window.driver.state, "enabled")
        self.assertTrue(self.window.driver.scan_may_be_active)
        self.assertTrue(self.window.session.is_connected)

    def test_readback_is_allowed_with_other_task_lease_without_releasing_it(self):
        self.manager.acquire_devices("other-task", [self.record.id])
        self.refresh()
        self.assertEqual(self.window.output_readback_values["amplitude"].text(), "-31.125 dBm")
        self.assertEqual(self.manager.lease_owner(self.record.id), "other-task")
        self.assertTrue(all(command.endswith("?") for command in self.backend.commands))

    def test_slow_read_is_async_and_duplicate_requests_are_coalesced(self):
        entered, release = Event(), Event()
        original = self.window.session.query
        def slow(command, **kwargs):
            if command == ":POWer?":
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test read not released")
            return original(command, **kwargs)
        ticks = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()
        try:
            with patch.object(self.window.session, "query", side_effect=slow):
                self.window._refresh_output_readback()
                self.assertTrue(entered.wait(1))
                self.window._refresh_output_readback()
                self.assertFalse(self.window.output_readback_button.isEnabled())
                self.assertTrue(self.wait(lambda: len(ticks) >= 3, timeout=.4))
                release.set()
                self.idle()
            self.assertEqual(len(self.backend.commands), 3)
        finally:
            release.set()
            timer.stop()

    def test_click_never_waits_for_existing_session_transaction(self):
        entered, release = Event(), Event()
        def busy_transaction():
            with self.window.session.transaction():
                entered.set()
                release.wait(3)
        owner = Thread(target=busy_transaction)
        owner.start()
        self.assertTrue(entered.wait(1))
        watchdog = Timer(2, release.set)
        watchdog.start()
        try:
            started = time.monotonic()
            self.window._refresh_output_readback()
            self.assertLess(time.monotonic() - started, .5)
        finally:
            release.set()
            watchdog.cancel()
            owner.join(3)
        self.idle()

    def test_stop_queued_behind_failed_read_still_executes_and_confirms_off(self):
        self.manager.acquire_devices(self.window.owner, [self.record.id])
        self.window.driver.state = "enabled"
        self.window.driver.scan_may_be_active = True
        self.backend.put("SWEep:STATe", "FREQUENCY")
        entered, release = Event(), Event()
        original = self.window.session.query
        def failed_read(command, **kwargs):
            if command == ":POWer?":
                entered.set()
                release.wait(3)
                raise TimeoutError("injected slow RF read timeout")
            return original(command, **kwargs)
        try:
            with patch.object(self.window.session, "query", side_effect=failed_read):
                self.window._refresh_output_readback()
                self.assertTrue(entered.wait(1))
                self.window._safety(self.window.driver.stop)
                self.assertEqual(len(self.window._pending_safety), 1)
                self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
                release.set()
                self.idle()
            self.assertEqual(self.window.driver.state, "off")
            self.assertFalse(self.window.driver.scan_may_be_active)
            self.assertIsNone(self.manager.lease_owner(self.record.id))
            self.assertEqual(self.backend.commands.count(":SWEep:STATe OFF"), 1)
            self.assert_unknown()
        finally:
            release.set()

    def test_queued_and_late_results_reject_replacement_connection(self):
        for after_read in (False, True):
            with self.subTest(after_read=after_read):
                queued = []
                with patch.object(self.window, "submit", side_effect=lambda operation, **callbacks: queued.append((operation, callbacks))):
                    self.window._refresh_output_readback()
                    operation, callbacks = queued.pop()
                    result = operation() if after_read else None
                    self.manager.disconnect(self.record)
                    self.manager.connect(self.record)
                    new_backend = self.window.session._backend
                    new_backend.commands.clear()
                    if after_read:
                        callbacks["on_success"](result)
                    else:
                        with self.assertRaisesRegex(SsgCommandError, "连接已变化") as caught:
                            operation()
                        callbacks["on_error"](str(caught.exception))
                    self.assertEqual(new_backend.commands, [])
                self.assert_unknown()
                self.assertIn("连接已变化", self.window.output_readback_status.text())
                self.assertFalse(self.window._busy)
                self.assertFalse(self.window._output_readback_pending)

    def test_disconnect_clears_values_and_disables_refresh(self):
        self.refresh()
        self.window.disconnect_device()
        self.assert_unknown()
        self.assertFalse(self.window.output_readback_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
