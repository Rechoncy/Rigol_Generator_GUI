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
from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.ui.device_windows import DG4000Window
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE


class DgReadbackUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["dg-readback-ui"])

    def setUp(self):
        self.record = DeviceRecord.create(kind=DeviceKind.DG4000, name="Virtual DG", resource=VIRTUAL_DG_RESOURCE)
        self.manager = DeviceConnectionManager()
        self.manager.connect(self.record)
        self.manager.virtual_lab.set_scpi_delay(self.record.resource, 0)
        self.window = DG4000Window(self.record, [], self.manager)
        self.window.show()
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.generator = self.window.driver.session._generator
        self.generator.channels[1].update(frequency_hz=100000000.000001, amplitude_vpp=2.5, output=True)
        self.generator.channels[2].update(frequency_hz=23456.0, amplitude_vpp=0.125, output=False)
        self.generator.commands.clear()

    def tearDown(self):
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.manager.release_devices("readback-test")
        self.window.close()
        self.manager.close_all()
        self.app.processEvents()

    def wait(self, predicate, timeout=4):
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
        for values in self.window.output_readback_values.values():
            self.assertTrue(all(value.text() == "未知" for value in values.values()))

    def test_readback_is_query_only_keeps_channel_identity_and_does_not_overwrite_editors(self):
        before = self.manager.virtual_lab.generator_snapshot(self.record.resource).channels
        editors = self.window._device_parameter_snapshot()
        self.window.channel_controls[2]["frequency"].setFocus()
        self.refresh()
        self.assertEqual(self.window.output_readback_values[1]["frequency"].text(), "100000000.000001 Hz")
        self.assertEqual(self.window.output_readback_values[1]["amplitude"].text(), "2.5 Vpp")
        self.assertEqual(self.window.output_readback_values[1]["output"].text(), "开启")
        self.assertEqual(self.window.output_readback_values[2]["frequency"].text(), "23456 Hz")
        self.assertEqual(self.window.output_readback_values[2]["output"].text(), "关闭（未输出）")
        self.assertIn("回读时间", self.window.output_readback_status.text())
        self.assertEqual(self.manager.virtual_lab.generator_snapshot(self.record.resource).channels, before)
        self.assertEqual(self.window._device_parameter_snapshot(), editors)
        self.assertEqual(len(self.generator.commands), 10)
        self.assertTrue(all(command.endswith("?") for command in self.generator.commands))
        command_count = len(self.generator.commands)
        self.wait(lambda: False, timeout=0.15)
        self.assertEqual(len(self.generator.commands), command_count, "Readback must not start an automatic query loop")

    def test_amplitude_displays_instrument_unit_without_vpp_assumption(self):
        self.generator.amplitude_units.update({1: "VRMS", 2: "DBM"})
        self.refresh()
        self.assertTrue(self.window.output_readback_values[1]["amplitude"].text().endswith(" Vrms"))
        self.assertTrue(self.window.output_readback_values[2]["amplitude"].text().endswith(" dBm"))
        self.assertEqual(self.generator.amplitude_units, {1: "VRMS", 2: "DBM"})
        self.assertTrue(all(command.endswith("?") for command in self.generator.commands))

    def test_slow_read_keeps_qt_responsive_and_coalesces_repeated_requests(self):
        entered, release = Event(), Event()
        original = self.window.driver.session.query

        def slow(command, **kwargs):
            if command == ":SOUR1:FREQ?":
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
            with patch.object(self.window.driver.session, "query", side_effect=slow):
                self.window._refresh_output_readback()
                self.assertTrue(entered.wait(1))
                self.assertFalse(self.window.output_readback_button.isEnabled())
                self.window._refresh_output_readback()
                self.assertTrue(self.wait(lambda: len(ticks) >= 3, timeout=0.4))
                self.assertTrue(self.window._output_readback_pending)
                release.set()
                self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertEqual(len(self.generator.commands), 10)
            self.assertTrue(self.window.output_readback_button.isEnabled())
        finally:
            release.set()
            timer.stop()

    def test_click_does_not_wait_for_an_existing_session_transaction(self):
        entered, release = Event(), Event()

        def busy_transaction():
            with self.window.driver.session.transaction():
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
            self.assertLess(time.monotonic() - started, 0.5, "Button waited for an existing I/O transaction")
            self.assertTrue(self.window._output_readback_pending)
        finally:
            release.set()
            watchdog.cancel()
            owner.join(3)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))

    def test_failed_channel_discards_entire_old_snapshot_and_can_be_retried(self):
        self.refresh()
        original = self.window.driver.session.query
        generation = self.window.driver.session.connection_generation

        def invalid(command, **kwargs):
            return "INVALID_AMPLITUDE" if command == ":SOUR2:VOLT?" else original(command, **kwargs)

        with patch.object(self.window.driver.session, "query", side_effect=invalid), patch.object(QMessageBox, "warning") as warning:
            self.refresh()
            warning.assert_not_called()
        self.assert_unknown()
        self.assertIn("INVALID_AMPLITUDE", self.window.output_readback_status.text())
        self.assertIn(":SOUR2:VOLT?", self.window.output_readback_status.text())
        self.assertNotIn("回读时间", self.window.output_readback_status.text())
        self.assertEqual(self.window.driver.session.connection_generation, generation)
        self.refresh()
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "0.125 Vpp")

    def test_readback_works_with_active_task_lease_without_invalidating_prepared_writer(self):
        writer = self.window.driver.prepare_trigger()
        self.manager.acquire_devices("readback-test", [self.record.id])
        generation = self.window.driver.session.connection_generation
        self.generator.commands.clear()
        self.refresh()
        self.assertEqual(self.manager.lease_owner(self.record.id), "readback-test")
        self.assertEqual(self.window.driver.session.connection_generation, generation)
        self.assertTrue(all(command.endswith("?") for command in self.generator.commands))
        writer.apply_steps([StepValue(StepParameter.FREQUENCY, 2, 34567)])
        self.assertEqual(self.generator.channels[2]["frequency_hz"], 34567)
        self.assertEqual(self.generator.channels[1]["frequency_hz"], 100000000.000001)

    def test_queued_read_does_not_query_a_replacement_connection(self):
        queued = []
        with patch.object(self.window, "submit", side_effect=lambda operation, **callbacks: queued.append((operation, callbacks))):
            self.window._refresh_output_readback()
            operation, callbacks = queued.pop()
            self.manager.disconnect(self.record)
            self.manager.connect(self.record)
            self.generator.commands.clear()
            with self.assertRaisesRegex(RuntimeError, "连接已变化") as raised:
                operation()
            callbacks["on_error"](str(raised.exception))
        self.assert_unknown()
        self.assertEqual(len(self.generator.commands), 0)
        self.assertFalse(self.window._output_readback_pending)
        self.assertTrue(self.window.output_readback_button.isEnabled())

    def test_late_read_result_cannot_repopulate_after_reconnect(self):
        queued = []
        with patch.object(self.window, "submit", side_effect=lambda operation, **callbacks: queued.append((operation, callbacks))):
            self.window._refresh_output_readback()
            operation, callbacks = queued.pop()
            result = operation()
            self.manager.disconnect(self.record)
            self.manager.connect(self.record)
            callbacks["on_success"](result)
        self.assert_unknown()
        self.assertIn("连接已变化", self.window.output_readback_status.text())
        self.assertNotIn("正在回读", self.window.output_readback_status.text())

    def test_disconnect_clears_cached_values_and_overview_shortcut_opens_readback(self):
        self.window.overview_readback_button.click()
        self.assertEqual(self.window.pages.currentIndex(), 1)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.assertIn("回读时间", self.window.output_readback_status.text())
        self.manager.disconnect(self.record)
        self.window.sync_connection_state()
        self.assert_unknown()
        self.assertFalse(self.window.output_readback_button.isEnabled())
        self.assertFalse(self.window.overview_readback_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
