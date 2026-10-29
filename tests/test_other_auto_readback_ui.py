import time
import unittest
from threading import Event, Thread
from unittest.mock import patch
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMessageBox

import test_siglent_readback as ssg_fixtures
import test_vaunix_readback as vaunix_fixtures
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.trigger import TriggerStepController


class VaunixAutoReadbackUiTests(unittest.TestCase):
    setUpClass = classmethod(vaunix_fixtures.VaunixReadbackUiTests.setUpClass.__func__)
    setUp = vaunix_fixtures.VaunixReadbackUiTests.setUp
    tearDown = vaunix_fixtures.VaunixReadbackUiTests.tearDown
    wait = vaunix_fixtures.VaunixReadbackUiTests.wait
    refresh = vaunix_fixtures.VaunixReadbackUiTests.refresh
    assert_unknown = vaunix_fixtures.VaunixReadbackUiTests.assert_unknown

    def test_successful_output_write_updates_both_readback_and_overview_once(self):
        self.window.output_frequency.setValue(6e9)
        self.window.output_power.setValue(-12.5)
        edits = self.window._device_parameter_snapshot()
        with patch.object(self.device, 'read_output_parameters', wraps=self.device.read_output_parameters) as read:
            self.window._apply_output()
            self.assertTrue(self.wait(lambda: self.window.output_readback_values['frequency'].text() == '6000000000 Hz'))
            self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertEqual(read.call_count, 1)
            self.wait(lambda: False, timeout=.65)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(self.window.output_readback_values['amplitude'].text(), '-12.5 dBm')
        self.assertEqual(self.window.overview_metrics['power'].text(), '-12.5 dBm')
        self.assertEqual(self.window._device_parameter_snapshot(), edits)

    def test_disabled_auto_is_not_persisted_and_never_reads_after_write(self):
        edits = self.window._device_parameter_snapshot()
        self.window.auto_readback_checkbox.setChecked(False)
        self.assertEqual(self.window._device_parameter_snapshot(), edits)
        with patch.object(self.device, 'read_output_parameters', wraps=self.device.read_output_parameters) as read:
            self.window._apply_output()
            self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.wait(lambda: False, timeout=.65)
            read.assert_not_called()

    def test_busy_transaction_defers_auto_without_waiting_or_getters(self):
        entered, release = Event(), Event()
        def hold():
            with self.device.transaction():
                entered.set()
                release.wait(3)
        worker = Thread(target=hold)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            with patch.object(self.device, 'read_output_parameters', wraps=self.device.read_output_parameters) as read:
                self.manager.note_output_change(self.record)
                ticks = []
                self.assertTrue(self.wait(lambda: ticks.append(1) or len(ticks) > 5, timeout=.3))
                self.wait(lambda: False, timeout=.3)
                read.assert_not_called()
                release.set()
                self.assertTrue(self.wait(lambda: '回读时间' in self.window.output_readback_status.text()))
                self.assertEqual(read.call_count, 1)
        finally:
            release.set()
            worker.join(2)

    def test_old_same_connection_result_cannot_cover_new_successful_write(self):
        queued = []
        with patch.object(self.window, 'submit', side_effect=lambda op, **callbacks: queued.append((op, callbacks))):
            self.window._refresh_output_readback()
        operation, callbacks = queued.pop()
        old = operation()
        self.device.configure_output(frequency_hz=6e9, power_dbm=-20, rf_on=True, internal_reference=True)
        self.manager.note_output_change(self.record)
        callbacks['on_success'](old)
        self.assert_unknown()
        self.assertTrue(self.wait(lambda: self.window.output_readback_values['frequency'].text() == '6000000000 Hz'))

    def test_failed_auto_pauses_until_explicit_manual_success(self):
        with patch.object(self.device, 'read_output_parameters', side_effect=RuntimeError('AUTO_DLL_FAILURE')) as read:
            self.manager.note_output_change(self.record)
            self.assertTrue(self.wait(lambda: self.window.auto_readback.paused))
            self.manager.note_output_change(self.record)
            self.wait(lambda: False, timeout=.65)
            self.assertEqual(read.call_count, 1)
            self.assert_unknown()
        self.refresh()
        self.assertFalse(self.window.auto_readback.paused)

    def test_trigger_arming_keeps_p0_and_catchup_reads_only_latest_after_quiet(self):
        class Counter:
            count = 0
            def start(self): pass
            def stop(self): pass
            def read_count(self): return self.count
        counter = Counter()
        controller = TriggerStepController(counter, self.device, StepParameter.FREQUENCY, 1, [6e9, 7e9])
        self.manager.acquire_devices('auto-trigger-test', [self.record.id])
        self.manager.watch_output_task('auto-trigger-test', [self.record], controller)
        self.window.controller = controller
        self.window.auto_readback.reset()
        try:
            with patch.object(self.device, 'read_output_parameters', wraps=self.device.read_output_parameters) as read:
                controller.start()
                self.wait(lambda: False, timeout=.3)
                self.assertEqual(self.device.frequency_hz, 5_123_456_780)
                read.assert_not_called()
                counter.count = 2
                self.assertTrue(self.wait(lambda: controller.snapshot().applied_count == 2))
                self.assertTrue(self.wait(lambda: self.window.output_readback_values['frequency'].text() == '7000000000 Hz'))
                self.assertEqual(controller.snapshot().current_index, 1)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(self.manager.lease_owner(self.record.id), 'auto-trigger-test')
        finally:
            controller.stop()
            self.manager.release_devices('auto-trigger-test')
            self.window.controller = None

    def test_partial_write_failure_invalidates_without_replaying_or_auto_reading(self):
        self.refresh()
        def accepted_then_failed():
            self.device.configure_output(frequency_hz=6e9, power_dbm=-20, rf_on=True, internal_reference=True)
            raise RuntimeError('ACCEPTANCE_UNKNOWN')
        with patch.object(self.device, 'read_output_parameters', wraps=self.device.read_output_parameters) as read, \
                patch.object(QMessageBox, 'warning'):
            self.window.submit(accepted_then_failed, after_write=True)
            self.assertTrue(self.wait(lambda: not self.window._call_tasks))
            self.assertTrue(self.window.auto_readback.paused)
            self.assert_unknown()
            self.wait(lambda: False, timeout=.65)
            read.assert_not_called()
        self.assertEqual(self.device.frequency_hz, 6e9)
        self.refresh()
        self.assertEqual(self.window.output_readback_values['frequency'].text(), '6000000000 Hz')


class SiglentAutoReadbackUiTests(unittest.TestCase):
    setUpClass = classmethod(ssg_fixtures.SiglentReadbackUiTests.setUpClass.__func__)
    setUp = ssg_fixtures.SiglentReadbackUiTests.setUp
    tearDown = ssg_fixtures.SiglentReadbackUiTests.tearDown
    wait = ssg_fixtures.SiglentReadbackUiTests.wait
    idle = ssg_fixtures.SiglentReadbackUiTests.idle
    refresh = ssg_fixtures.SiglentReadbackUiTests.refresh
    assert_unknown = ssg_fixtures.SiglentReadbackUiTests.assert_unknown

    def test_write_reuses_complete_snapshot_without_extra_basic_queries(self):
        self.window.frequency.setValue(1.5e9)
        self.window.power.setValue(-10)
        editors = self.window._device_parameter_snapshot()
        with patch.object(self.window.driver, 'read_output_parameters', wraps=self.window.driver.read_output_parameters) as read:
            self.window.apply_frequency()
            self.idle()
            self.assertEqual(self.window.output_readback_values['frequency'].text(), '1500000000 Hz')
            self.assertEqual(self.window.output_readback_values['amplitude'].text(), '-31.125 dBm')
            commands_after_write = tuple(self.backend.commands)
            self.wait(lambda: False, timeout=.65)
            read.assert_not_called()
        self.assertEqual(tuple(self.backend.commands), commands_after_write)
        # Verified settings may query values themselves; the existing full
        # snapshot supplies all three fields without appending a basic round.
        for command in (':FREQuency?', ':POWer?', ':OUTPut?'):
            self.assertIn(command, commands_after_write)
        self.assertEqual(self.window._device_parameter_snapshot(), editors)

    def test_connection_and_edits_do_not_start_auto_query_or_persist_checkbox(self):
        self.assert_unknown()
        self.window.frequency.setValue(1.5e9)
        before = self.window._device_parameter_snapshot()
        self.window.auto_readback_checkbox.setChecked(False)
        self.assertEqual(self.window._device_parameter_snapshot(), before)
        self.wait(lambda: False, timeout=.6)
        self.assertEqual(self.backend.commands, [])
        self.window.apply_frequency()
        self.idle()
        self.assert_unknown()

    def test_native_scan_reuses_real_poll_and_keeps_external_counts_unknown(self):
        self.window.apply_sweep()
        self.idle()
        self.window.start_sweep()
        self.idle()
        self.backend.put('FREQuency', '1600000000')
        self.backend.put('POWer', '-22.5')
        self.backend.commands.clear()
        with patch.object(self.window.driver, 'read_output_parameters', wraps=self.window.driver.read_output_parameters) as read:
            self.window._poll()
            self.idle()
            self.assertEqual(self.window.output_readback_values['frequency'].text(), '1600000000 Hz')
            self.assertEqual(self.window.output_readback_values['amplitude'].text(), '-22.5 dBm')
            read.assert_not_called()
        self.assertEqual(self.manager.lease_owner(self.record.id), self.window.owner)
        self.assertEqual(self.window.scan_metrics['received'].text(), '未知')
        self.assertEqual(self.window.scan_metrics['backlog'].text(), '未知')

    def test_bad_native_snapshot_stays_unknown_until_manual_retry(self):
        self.window.apply_sweep()
        self.idle()
        self.window.start_sweep()
        self.idle()
        self.backend.put('POWer', 'BAD_POWER')
        self.window._poll()
        self.idle()
        self.assertTrue(self.window.auto_readback.paused)
        self.assert_unknown()
        self.backend.put('POWer', '-22.5')
        self.backend.commands.clear()
        self.window._poll()
        self.idle()
        self.assert_unknown()
        self.assertEqual(self.backend.commands, [':SWEep:STATe?'])
        self.refresh()
        self.assertEqual(self.window.output_readback_values['amplitude'].text(), '-22.5 dBm')
        self.assertFalse(self.window.auto_readback.paused)

    def test_typed_reuse_rejects_any_invalid_field_without_partial_display(self):
        valid = {':FREQuency': '1500000000', ':POWer': '-20', ':OUTPut': 'ON'}
        generation = self.window.session.connection_generation
        for key, value in ((':FREQuency', 'nan'), (':POWer', 'inf'), (':OUTPut', 'UNKNOWN')):
            with self.subTest(key=key):
                snapshot = dict(valid, **{key: value})
                with self.assertRaises(Exception):
                    self.window._snapshot_output_readback(snapshot, generation)
        self.assert_unknown()

    def test_failed_write_acceptance_invalidates_cache_without_replay(self):
        self.refresh()
        def accepted_then_failed():
            self.window.driver.set_frequency(1.5e9)
            raise ValueError('ACCEPTANCE_UNKNOWN')
        with patch.object(self.window.driver, 'read_output_parameters', wraps=self.window.driver.read_output_parameters) as read:
            self.window._run(accepted_then_failed, after_write=True)
            self.idle()
            self.assertTrue(self.window.auto_readback.paused)
            self.assert_unknown()
            count = len(self.backend.commands)
            self.wait(lambda: False, timeout=.65)
            self.assertEqual(len(self.backend.commands), count)
            read.assert_not_called()
        self.refresh()
        self.assertEqual(self.window.output_readback_values['frequency'].text(), '1500000000 Hz')

    def test_rejected_close_disables_auto_and_user_can_reenable(self):
        self.manager.acquire_devices(self.window.owner, [self.record.id])
        event = QCloseEvent()
        self.window.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.window.auto_readback_checkbox.isChecked())
        self.assertFalse(self.window.auto_readback.timer.isActive())
        self.manager.release_devices(self.window.owner)
        self.window.auto_readback_checkbox.setChecked(True)
        self.assertTrue(self.window.auto_readback.timer.isActive())


if __name__ == '__main__':
    unittest.main()
