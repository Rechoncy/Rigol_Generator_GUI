import unittest
from threading import Event
from unittest.mock import patch

import test_dg_readback_ui as dg_fixture
from test_dg_trigger_dispatch import Counter
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.trigger import TriggerStepController


class DgAutoReadbackTests(unittest.TestCase):
    setUpClass = classmethod(dg_fixture.DgReadbackUiTests.setUpClass.__func__)
    setUp = dg_fixture.DgReadbackUiTests.setUp
    tearDown = dg_fixture.DgReadbackUiTests.tearDown
    wait = dg_fixture.DgReadbackUiTests.wait
    refresh = dg_fixture.DgReadbackUiTests.refresh
    assert_unknown = dg_fixture.DgReadbackUiTests.assert_unknown

    def start_controller(self):
        counter = Counter()
        controller = TriggerStepController(counter, self.window.driver, StepParameter.FREQUENCY, 2, [1000, 2000])
        self.manager.acquire_devices("readback-test", [self.record.id])
        self.manager.watch_output_task("readback-test", [self.record], controller)
        self.window.controller = controller
        self.window.auto_readback.reset()
        controller.start()
        return counter, controller

    def stop_controller(self, controller):
        controller.stop()
        self.window.controller = None
        self.manager.release_devices("readback-test")

    def test_parameter_apply_automatically_reads_actual_values_without_overwriting_editors(self):
        controls = self.window.channel_controls[2]
        controls["frequency"].setValue(34567)
        controls["amplitude"].setValue(.25)
        editors = self.window._device_parameter_snapshot()
        self.window._apply_channel(2)
        self.assertTrue(self.wait(lambda: self.window.output_readback_values[2]["frequency"].text() == "34567 Hz"))
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "0.25 Vpp")
        self.assertEqual(self.window._device_parameter_snapshot(), editors)
        commands = len(self.generator.commands)
        self.wait(lambda: False, .65)
        self.assertEqual(len(self.generator.commands), commands, "No perpetual polling after a single apply")
        self.assertNotIn(self.window.auto_readback_checkbox, self.window._persistent_controls().values())

    def test_disabled_automatic_read_stays_unknown_then_explicit_reenable_refreshes(self):
        self.window.auto_readback_checkbox.setChecked(False)
        self.window._apply_channel(2)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.wait(lambda: False, .3)
        self.assert_unknown()
        self.assertNotIn(":SOUR2:VOLT:UNIT?", self.generator.commands)
        self.window.auto_readback_checkbox.setChecked(True)
        self.assertTrue(self.wait(lambda: "回读时间" in self.window.output_readback_status.text()))

    def test_disabling_automatic_read_keeps_valid_cache_for_local_mvpp_display(self):
        self.refresh()
        self.generator.commands.clear()
        self.window.auto_readback_checkbox.setChecked(False)
        self.window.output_readback_units[1].setCurrentText("mVpp")
        self.assertEqual(self.window.output_readback_values[1]["amplitude"].text(), "2500 mVpp")
        self.assertEqual(list(self.generator.commands), [])

    def test_trigger_burst_reads_latest_point_preserves_first_p0_and_ch1(self):
        original_ch1 = self.generator.snapshot().channels[0]
        counter, controller = self.start_controller()
        try:
            self.generator.commands.clear()
            self.wait(lambda: False, .25)
            self.assertEqual(controller.snapshot().applied_count, 0)
            self.assertEqual(list(self.generator.commands), [], "Arming must not start an automatic query")
            counter.count = 5
            self.assertTrue(self.wait(lambda: controller.snapshot().applied_count == 5))
            self.assertTrue(self.wait(lambda: self.window.output_readback_values[2]["frequency"].text() == "1000 Hz"))
            self.assertEqual(self.generator.snapshot().channels[0], original_ch1)
            writes = [command for command in self.generator.commands if not command.endswith("?")]
            self.assertEqual(writes, [":SOUR2:FREQ 1000", ":SOUR2:FREQ 2000", ":SOUR2:FREQ 1000",
                                      ":SOUR2:FREQ 2000", ":SOUR2:FREQ 1000"])
            self.assertEqual(list(self.generator.commands).count(":SOUR1:VOLT:UNIT?"), 2)
        finally:
            self.stop_controller(controller)

    def test_new_trigger_interrupts_automatic_group_after_inflight_query(self):
        counter, controller = self.start_controller()
        entered, release = Event(), Event()
        self.window.auto_readback.timer.stop()
        original = self.window.driver.session.query
        calls = []

        def slow(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test query was not released")
            return original(command, **kwargs)

        try:
            counter.count = 1
            self.assertTrue(self.wait(lambda: controller.snapshot().applied_count == 1))
            with patch.object(self.window.driver.session, "query", side_effect=slow):
                self.window._refresh_output_readback(automatic=True)
                self.assertTrue(entered.wait(1))
                counter.count = 2
                self.assertTrue(self.wait(lambda: controller.snapshot().received_count == 2))
                release.set()
                self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertEqual(calls, [":SOUR1:VOLT:UNIT?"])
            self.assertTrue(self.wait(lambda: controller.snapshot().applied_count == 2))
            self.assert_unknown()
            self.assertFalse(self.window.auto_readback.paused)
            self.window._refresh_output_readback(automatic=True)
            self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertEqual(self.window.output_readback_values[2]["frequency"].text(), "2000 Hz")
        finally:
            release.set()
            self.stop_controller(controller)

    def test_late_read_on_same_connection_cannot_overwrite_new_parameter(self):
        queued = []
        self.window.auto_readback.timer.stop()
        with patch.object(self.window, "submit", side_effect=lambda operation, **callbacks: queued.append((operation, callbacks))):
            self.window._refresh_output_readback()
        operation, callbacks = queued.pop()
        result = operation()
        self.window.driver.apply_step(StepParameter.FREQUENCY, 2, 45678)
        self.manager.note_output_change(self.record)
        callbacks["on_success"](result)
        self.assert_unknown()
        self.window._refresh_output_readback(automatic=True)
        self.assertTrue(self.wait(lambda: not self.window._call_tasks))
        self.assertEqual(self.window.output_readback_values[2]["frequency"].text(), "45678 Hz")

    def test_automatic_failure_pauses_without_retries_until_manual_read_succeeds(self):
        original = self.window.driver.read_output_parameters
        calls = []

        def once(**kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("AUTO_READ_BAD_REPLY")
            return original(**kwargs)

        with patch.object(self.window.driver, "read_output_parameters", side_effect=once):
            self.window._apply_channel(2)
            self.assertTrue(self.wait(lambda: "AUTO_READ_BAD_REPLY" in self.window.output_readback_status.text()))
            self.assertTrue(self.window.auto_readback.paused)
            self.window._apply_channel(1)
            self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.wait(lambda: False, .65)
            self.assertEqual(len(calls), 1)
            self.refresh()
            self.assertFalse(self.window.auto_readback.paused)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
