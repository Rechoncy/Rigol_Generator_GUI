import threading
import unittest
from unittest.mock import Mock, patch

from rigol_control import poll_waiter


class PollWaiterTests(unittest.TestCase):
    def test_nonfinite_intervals_fail_without_spinning(self):
        event = Mock()
        with patch.object(poll_waiter.time, "sleep") as sleep:
            for seconds in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                    poll_waiter.wait_for_poll(event, seconds)
        sleep.assert_not_called()
        event.wait.assert_not_called()

    def test_fallback_keeps_direct_event_wait(self):
        event = Mock()
        event.wait.return_value = True
        with patch.object(poll_waiter, "_WINDOWS_HIGH_RESOLUTION_SLEEP", False):
            self.assertTrue(poll_waiter.wait_for_poll(event, 0.002))
        event.wait.assert_called_once_with(0.002)

    def test_short_wait_sleeps_and_does_not_spin(self):
        event = threading.Event()
        clock = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with (
            patch.object(poll_waiter, "_WINDOWS_HIGH_RESOLUTION_SLEEP", True),
            patch.object(poll_waiter.time, "monotonic", lambda: clock[0]),
            patch.object(poll_waiter.time, "sleep", sleep),
        ):
            self.assertFalse(poll_waiter.wait_for_poll(event, 0.002))
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.002)

    def test_long_wait_uses_bounded_slices(self):
        event = threading.Event()
        clock = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with (
            patch.object(poll_waiter, "_WINDOWS_HIGH_RESOLUTION_SLEEP", True),
            patch.object(poll_waiter.time, "monotonic", lambda: clock[0]),
            patch.object(poll_waiter.time, "sleep", sleep),
        ):
            self.assertFalse(poll_waiter.wait_for_poll(event, 0.014))
        self.assertTrue(all(0 < value <= 0.005 for value in sleeps))
        self.assertAlmostEqual(sum(sleeps), 0.014)

    def test_stop_during_sleep_cancels_before_the_next_slice(self):
        event = threading.Event()
        with (
            patch.object(poll_waiter, "_WINDOWS_HIGH_RESOLUTION_SLEEP", True),
            patch.object(poll_waiter.time, "sleep", side_effect=lambda _: event.set()) as sleep,
        ):
            self.assertTrue(poll_waiter.wait_for_poll(event, 60))
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args.args[0], 0.005)

    def test_already_stopped_and_nonpositive_waits_do_not_sleep(self):
        event = threading.Event()
        with (
            patch.object(poll_waiter, "_WINDOWS_HIGH_RESOLUTION_SLEEP", True),
            patch.object(poll_waiter.time, "sleep") as sleep,
        ):
            for seconds in (0, -1):
                self.assertFalse(poll_waiter.wait_for_poll(event, seconds))
            event.set()
            self.assertTrue(poll_waiter.wait_for_poll(event, 60))
        sleep.assert_not_called()
