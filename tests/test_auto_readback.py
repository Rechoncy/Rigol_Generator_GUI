import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.trigger import ControllerState, TriggerSnapshot
from rigol_control.ui.auto_readback import AutoReadback, window_readback_activity


class AutoReadbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(['auto-readback-tests'])

    def setUp(self):
        self.now = 0.0
        self.clock = patch('rigol_control.ui.auto_readback.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.state = SimpleNamespace(revision=0, barrier=(), busy=False)
        self.generation = 1
        self.connected = True
        self.pending = False
        self.ready = True
        self.started = []
        self.messages = []

        def refresh():
            self.pending = True
            self.started.append(self.scheduler.capture(automatic=True))

        self.scheduler = AutoReadback(
            None, refresh=refresh, invalidate=self.messages.append,
            activity=lambda: self.state, connected=lambda: self.connected,
            generation=lambda: self.generation, pending=lambda: self.pending,
            ready=lambda: self.ready,
        )
        self.addCleanup(self.scheduler.stop)

    def tick(self, at):
        self.now = at
        self.scheduler._tick()

    def write(self, at):
        self.state.revision += 1
        self.state.barrier = ('write', self.state.revision)
        self.tick(at)

    def finish(self, outcome='success', ticket=None):
        self.pending = False
        self.scheduler.finish(ticket or self.started[-1], outcome)

    def test_no_change_does_not_start_any_read(self):
        for moment in (0, .1, 1, 100):
            self.tick(moment)
        self.assertEqual(self.started, [])
        self.assertEqual(self.messages, [])

    def test_stopping_automatic_schedule_still_allows_a_new_manual_read(self):
        old = self.scheduler.capture(automatic=True)
        self.scheduler.stop()
        self.assertFalse(self.scheduler.accepts(old))
        manual = self.scheduler.capture(automatic=False)
        self.assertTrue(self.scheduler.accepts(manual))
        self.scheduler.finish(manual, 'success')
        self.tick(100)
        self.assertEqual(self.started, [])

    def test_unknown_busy_transition_invalidates_cached_display_without_arming_a_read(self):
        self.state.busy = True
        self.state.barrier = ('error', 'unknown accepted result')
        self.tick(.1)
        self.assertIn('未知', self.messages[-1])
        self.state.busy = False
        self.state.barrier = ()
        self.tick(1)
        self.tick(2)
        self.assertEqual(self.started, [])

    def test_burst_coalesces_to_latest_revision_after_quiet_period(self):
        self.write(0)
        self.write(.1)
        self.write(.2)
        self.tick(.349)
        self.assertEqual(self.started, [])
        self.tick(.351)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0].revision, 3)
        self.tick(10)
        self.assertEqual(len(self.started), 1, 'In-flight read must not queue another task')
        self.finish()
        self.tick(20)
        self.assertEqual(len(self.started), 1)

    def test_minimum_interval_is_measured_from_read_start(self):
        self.write(0)
        self.tick(.151)
        self.finish()
        self.write(.2)
        self.tick(.351)
        self.assertEqual(len(self.started), 1)
        self.tick(.652)
        self.assertEqual(len(self.started), 2)

    def test_busy_and_barrier_changes_delay_reads_until_activity_settles(self):
        self.write(0)
        self.state.busy = True
        self.tick(1)
        self.assertEqual(self.started, [])
        self.state.busy = False
        self.state.barrier = ('drained',)
        self.tick(1.1)
        self.tick(1.249)
        self.assertEqual(self.started, [])
        self.tick(1.251)
        self.assertEqual(len(self.started), 1)

    def test_pending_and_cached_ready_predicates_do_not_launch_work(self):
        self.write(0)
        self.pending = True
        self.tick(1)
        self.assertEqual(self.started, [])
        self.pending, self.ready = False, False
        self.tick(2)
        self.assertEqual(self.started, [])
        self.ready = True
        self.tick(2.001)
        self.assertEqual(len(self.started), 1)

    def test_disabling_auto_keeps_write_invalidation_but_starts_no_reads(self):
        self.scheduler.set_enabled(False)
        self.write(0)
        self.tick(100)
        self.assertEqual(self.started, [])
        self.assertIn('请手动回读', self.messages[-1])

    def test_failure_pauses_across_new_write_events_until_manual_read_success(self):
        self.write(0)
        self.tick(.151)
        self.finish('failure')
        self.write(1)
        self.tick(2)
        self.assertEqual(len(self.started), 1)
        manual = self.scheduler.capture()
        self.scheduler.finish(manual, 'success')
        self.write(3)
        self.tick(3.151)
        self.assertEqual(len(self.started), 2)

    def test_checkbox_reenable_is_an_explicit_retry_after_failure(self):
        self.write(0)
        self.tick(.151)
        self.finish('failure')
        self.tick(1)
        self.scheduler.set_enabled(False)
        self.scheduler.set_enabled(True)
        self.tick(1.149)
        self.assertEqual(len(self.started), 1)
        self.tick(1.151)
        self.assertEqual(len(self.started), 2)

    def test_same_connection_write_rejects_old_result_and_coalesces_one_new_read(self):
        self.write(0)
        self.tick(.151)
        old = self.started[-1]
        self.state.revision += 1
        self.state.barrier = ('write', self.state.revision)
        self.assertFalse(self.scheduler.accepts(old))
        self.assertFalse(self.scheduler.can_continue(old))
        self.finish('stale', old)
        self.tick(1)
        self.tick(1.151)
        self.assertEqual(len(self.started), 2)
        self.assertEqual(self.started[-1].revision, 2)

    def test_receiving_a_new_trigger_invalidates_guard_without_reading_before_apply(self):
        ticket = self.scheduler.capture(automatic=True)
        self.state.barrier = ('running', 1, 0)
        self.assertFalse(self.scheduler.accepts(ticket))
        self.assertFalse(self.scheduler.can_continue(ticket))
        self.tick(1)
        self.tick(2)
        self.assertEqual(self.started, [])

    def test_manual_capture_observes_write_before_timer_so_its_result_is_not_invalidated(self):
        self.state.revision = 1
        self.state.barrier = ('write', 1)
        ticket = self.scheduler.capture()
        self.tick(1)
        self.assertTrue(self.scheduler.accepts(ticket))
        self.assertEqual(self.messages, [])

    def test_generation_change_discards_pending_dirty_state_without_new_read(self):
        self.write(0)
        self.generation += 1
        self.tick(1)
        self.tick(2)
        self.assertEqual(self.started, [])

    def test_late_old_connection_completion_does_not_schedule_new_connection_read(self):
        for outcome in ('success', 'deferred', 'failure', 'stale'):
            with self.subTest(outcome=outcome):
                self.scheduler.reset()
                self.scheduler.request()
                old = self.scheduler.capture(automatic=True)
                self.generation += 1
                self.tick(self.now + 1)
                self.scheduler.finish(old, outcome)
                self.tick(self.now + 1)
                self.assertEqual(self.started, [])

    def test_late_old_completion_cannot_pause_or_rearm_after_newer_manual_success(self):
        for outcome in ('success', 'deferred', 'failure', 'stale'):
            with self.subTest(outcome=outcome):
                self.scheduler.reset()
                old = self.scheduler.capture(automatic=True)
                self.scheduler.request()
                new = self.scheduler.capture()
                self.scheduler.finish(new, 'success')
                self.scheduler.finish(old, outcome)
                self.tick(self.now + 1)
                self.assertEqual(self.started, [], 'A superseded completion must not schedule more reads')
                self.assertFalse(self.scheduler.paused, 'A superseded failure must not pause newer success')

    def test_finished_stop_prevents_timer_and_late_completion_from_starting_more_work(self):
        self.write(0)
        self.tick(.151)
        old = self.started[-1]
        self.assertTrue(self.scheduler.timer.isActive())
        self.scheduler.stop()
        self.assertFalse(self.scheduler.timer.isActive())
        self.finish('deferred', old)
        self.scheduler.request()
        self.tick(100)
        self.assertEqual(len(self.started), 1)
        self.assertFalse(self.scheduler.accepts(old))

    def test_continue_guard_rechecks_current_busy_and_ready_status_without_dispatch(self):
        ticket = self.scheduler.capture(automatic=True)
        self.assertTrue(self.scheduler.can_continue(ticket))
        self.ready = False
        self.assertFalse(self.scheduler.can_continue(ticket))
        self.ready = True
        self.state.busy = True
        self.assertFalse(self.scheduler.can_continue(ticket))
        self.assertEqual(self.started, [])

    def test_explicit_reenable_after_blocked_close_restarts_timer_but_rejects_old_ticket(self):
        self.write(0)
        self.tick(.151)
        old = self.started[-1]
        self.scheduler.set_enabled(False)
        self.scheduler.stop()
        self.tick(1)
        self.assertEqual(len(self.started), 1)
        self.scheduler.set_enabled(True)
        self.assertTrue(self.scheduler.timer.isActive())
        self.assertFalse(self.scheduler.accepts(old))
        self.finish('failure', old)
        self.assertFalse(self.scheduler.paused)
        self.tick(1.151)
        self.assertEqual(len(self.started), 2)


class WindowReadbackActivityTests(unittest.TestCase):
    def window(self, state, *, received=0, applied=0, quiescent=True):
        manager_state = SimpleNamespace(revision=7, barrier=('source',), busy=False)
        controller = SimpleNamespace(
            snapshot=lambda: TriggerSnapshot(state, received, applied, None, None, None),
            is_quiescent=quiescent,
        )
        return SimpleNamespace(
            connection_manager=SimpleNamespace(output_readback_activity=lambda _: manager_state),
            record=object(), controller=controller,
        )

    def test_starting_stopping_backlog_and_unconfirmed_cleanup_are_busy(self):
        for state, received, applied, quiescent in (
            (ControllerState.STARTING, 0, 0, True),
            (ControllerState.STOPPING, 1, 1, True),
            (ControllerState.RUNNING, 2, 1, False),
            (ControllerState.ERROR, 1, 1, False),
            (ControllerState.STOPPED, 1, 1, False),
        ):
            with self.subTest(state=state, received=received):
                self.assertTrue(window_readback_activity(self.window(
                    state, received=received, applied=applied, quiescent=quiescent)).busy)

    def test_running_without_backlog_and_quiescent_terminal_state_can_read(self):
        for state in (ControllerState.RUNNING, ControllerState.STOPPED, ControllerState.ERROR):
            with self.subTest(state=state):
                self.assertFalse(window_readback_activity(self.window(
                    state, received=1, applied=1, quiescent=True)).busy)

    def test_arming_without_first_apply_does_not_claim_a_successful_step(self):
        window = self.window(ControllerState.STARTING)
        initial = window_readback_activity(window)
        self.assertEqual(initial.revision, 7)
        window.controller.snapshot = lambda: TriggerSnapshot(ControllerState.RUNNING, 1, 1, 0, 100, None)
        changed = window_readback_activity(window)
        self.assertEqual(changed.revision, initial.revision, "Only the manager records successful write revisions")
        self.assertNotEqual(changed.barrier, initial.barrier)

    def test_zero_trigger_detach_does_not_invent_an_output_change(self):
        window = self.window(ControllerState.RUNNING)
        armed = window_readback_activity(window)
        window.controller = None
        stopped = window_readback_activity(window)
        self.assertEqual(armed.revision, stopped.revision)

    def test_quiescent_error_does_not_wait_forever_for_unapplied_failed_points(self):
        state = window_readback_activity(self.window(
            ControllerState.ERROR, received=10, applied=3, quiescent=True))
        self.assertFalse(state.busy)


if __name__ == '__main__':
    unittest.main()
