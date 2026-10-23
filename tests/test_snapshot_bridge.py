import os
import threading
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.trigger import ControllerState, TriggerSnapshot
from rigol_control.ui.snapshot_bridge import SnapshotBridge


def snapshot(count, state=ControllerState.RUNNING):
    return TriggerSnapshot(state, count, count, count - 1, float(count),
                           "test error" if state is ControllerState.ERROR else None)


class SnapshotBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["snapshot-bridge-test"])

    def setUp(self):
        self.bridge = SnapshotBridge()
        self.addCleanup(self.bridge.close)

    def test_ten_thousand_worker_updates_deliver_only_latest_final_snapshot(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)

        def produce():
            for count in range(1, 10_001):
                publisher(snapshot(count))
            publisher(snapshot(10_000, ControllerState.STOPPED))

        worker = threading.Thread(target=produce)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(spy), 0)
        self.assertTrue(spy.wait(1000))
        QTest.qWait(110)
        self.assertEqual(len(spy), 1)
        self.assertEqual(spy[0][0].applied_count, 10_000)
        self.assertIs(spy[0][0].state, ControllerState.STOPPED)

    def test_stopped_and_error_states_survive_delayed_running_callback(self):
        for state in (ControllerState.STOPPED, ControllerState.ERROR):
            with self.subTest(state=state):
                publisher = self.bridge.begin_session()
                spy = QSignalSpy(self.bridge.updated)
                publisher(snapshot(4, state))
                publisher(snapshot(3))
                self.bridge.flush()
                self.assertEqual(len(spy), 1)
                self.assertIs(spy[0][0].state, state)
                self.assertEqual(spy[0][0].applied_count, 4)
                publisher(snapshot(5))
                self.bridge.flush()
                self.assertEqual(len(spy), 1)

    def test_restart_clears_pending_update_and_rejects_former_session_publisher(self):
        old_publisher = self.bridge.begin_session()
        old_publisher(snapshot(100))
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(snapshot(1))
        old_publisher(snapshot(101, ControllerState.ERROR))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertEqual(spy[0][0].applied_count, 1)
        self.assertIs(spy[0][0].state, ControllerState.RUNNING)

    def test_background_publish_is_delivered_on_gui_thread(self):
        gui_thread = threading.get_ident()
        deliveries = []
        self.bridge.updated.connect(lambda item: deliveries.append((threading.get_ident(), item)))
        publisher = self.bridge.begin_session()
        worker = threading.Thread(target=lambda: publisher(snapshot(7)))
        worker.start()
        worker.join(timeout=5)
        self.assertEqual(deliveries, [])
        QTest.qWait(150)
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0][0], gui_thread)
        self.assertEqual(deliveries[0][1].applied_count, 7)

    def test_delayed_counts_do_not_move_preview_backwards_even_after_flush(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(snapshot(20))
        self.bridge.flush()
        publisher(snapshot(19))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertEqual(spy[0][0].applied_count, 20)

    def test_terminal_snapshots_cannot_rewind_received_or_applied_counts(self):
        for state in (ControllerState.ERROR, ControllerState.STOPPED):
            for counts in ((20, 20), (21, 19)):
                with self.subTest(state=state, counts=counts):
                    publisher = self.bridge.begin_session()
                    spy = QSignalSpy(self.bridge.updated)
                    publisher(TriggerSnapshot(ControllerState.RUNNING, 21, 20, 19, 20.0, None))
                    self.bridge.flush()
                    stale = TriggerSnapshot(state, counts[0], counts[1], 18, 19.0, None)
                    publisher(stale)
                    self.bridge.flush()
                    self.assertEqual(len(spy), 1)
                    self.assertEqual(spy[0][0].applied_count, 20)

    def test_delayed_terminal_cannot_replace_newer_terminal_with_more_progress(self):
        for current in (ControllerState.ERROR, ControllerState.STOPPED):
            for stale in (ControllerState.ERROR, ControllerState.STOPPED):
                with self.subTest(current=current, stale=stale):
                    publisher = self.bridge.begin_session()
                    spy = QSignalSpy(self.bridge.updated)
                    publisher(snapshot(20, current))
                    self.bridge.flush()
                    publisher(snapshot(19, stale))
                    self.bridge.flush()
                    self.assertEqual(len(spy), 1)
                    self.assertIs(spy[0][0].state, current)
                    self.assertEqual(spy[0][0].applied_count, 20)

    def test_stopping_rejects_delayed_running_and_starting_updates(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(snapshot(4, ControllerState.STOPPING))
        self.bridge.flush()
        for state in (ControllerState.STARTING, ControllerState.RUNNING):
            publisher(snapshot(4, state))
            self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0].state, ControllerState.STOPPING)

    def test_final_stopped_rejects_delayed_error_and_allows_explicit_error_stop(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(snapshot(4, ControllerState.ERROR))
        self.bridge.flush()
        publisher(snapshot(4, ControllerState.STOPPED))
        self.bridge.flush()
        self.assertEqual(len(spy), 2)
        self.assertIs(spy[1][0].state, ControllerState.STOPPED)
        publisher(snapshot(4, ControllerState.ERROR))
        self.bridge.flush()
        self.assertEqual(len(spy), 2)

    def test_creation_revision_preserves_new_error_against_delayed_same_count_stopped(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        newest_error = replace(snapshot(4, ControllerState.ERROR), revision=3)
        old_stopped = replace(snapshot(4, ControllerState.STOPPED), revision=2)
        publisher(newest_error)
        self.bridge.flush()
        publisher(old_stopped)
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0], newest_error)
        final_stop = replace(snapshot(4, ControllerState.STOPPED), revision=4)
        publisher(final_stop)
        self.bridge.flush()
        self.assertEqual(len(spy), 2)
        self.assertIs(spy[1][0], final_stop)

    def test_newer_creation_revision_can_report_error_after_stopped(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(replace(snapshot(4, ControllerState.STOPPED), revision=2))
        self.bridge.flush()
        newer_error = replace(snapshot(4, ControllerState.ERROR), revision=3)
        publisher(newer_error)
        self.bridge.flush()
        self.assertEqual(len(spy), 2)
        self.assertIs(spy[1][0], newer_error)

    def test_equal_and_unstamped_revisions_cannot_replace_a_stamped_snapshot(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        original = replace(snapshot(4, ControllerState.ERROR), revision=3)
        publisher(original)
        self.bridge.flush()
        publisher(replace(snapshot(4, ControllerState.STOPPED), revision=3))
        publisher(snapshot(4, ControllerState.STOPPED))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0], original)

    def test_new_session_resets_creation_revision(self):
        old_publisher = self.bridge.begin_session()
        old_publisher(replace(snapshot(100, ControllerState.STOPPED), revision=100))
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        new_snapshot = replace(snapshot(1), revision=1)
        publisher(new_snapshot)
        old_publisher(replace(snapshot(101, ControllerState.ERROR), revision=101))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0], new_snapshot)

    def test_higher_revision_still_cannot_rewind_counts(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        original = replace(snapshot(4), revision=3)
        publisher(original)
        self.bridge.flush()
        publisher(replace(snapshot(3, ControllerState.ERROR), revision=4))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0], original)

    def test_stop_flush_delivers_final_snapshot_before_ui_reset(self):
        publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        publisher(snapshot(12, ControllerState.STOPPED))
        self.bridge.flush()
        self.assertEqual(len(spy), 1)
        self.assertIs(spy[0][0].state, ControllerState.STOPPED)
        QTest.qWait(110)
        self.assertEqual(len(spy), 1)

    def test_closed_window_ignores_late_session_and_can_start_again(self):
        old_publisher = self.bridge.begin_session()
        spy = QSignalSpy(self.bridge.updated)
        self.bridge.close()
        old_publisher(snapshot(9, ControllerState.ERROR))
        self.bridge.flush()
        self.assertEqual(len(spy), 0)
        publisher = self.bridge.begin_session()
        publisher(snapshot(1))
        self.assertTrue(spy.wait(1000))
        self.assertEqual(len(spy), 1)
        self.assertEqual(spy[0][0].applied_count, 1)


if __name__ == "__main__":
    unittest.main()
