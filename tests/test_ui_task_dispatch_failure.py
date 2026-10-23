"""A failed Qt task submission must not leave an invisible busy task behind."""
import unittest
from unittest.mock import patch

import test_trigger_task_handoff_ui as fixture_module


class RejectOncePool:
    def __init__(self, delegate):
        self.delegate = delegate
        self.rejected = []

    def start(self, task):
        if not self.rejected:
            self.rejected.append(task)
            raise RuntimeError("Qt worker submission rejected")
        self.delegate.start(task)


class UiTaskDispatchFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.TriggerTaskHandoffUiTests.setUpClass()

    def setUp(self):
        self.fixture = fixture_module.TriggerTaskHandoffUiTests()
        self.fixture.setUp()
        self.pools = []

    def tearDown(self):
        # Rejected tasks were never queued, so they can be removed even when
        # running this regression against the old implementation.
        for window, pool in self.pools:
            for task in pool.rejected:
                window._call_tasks.discard(task)
                if getattr(window, "_trigger_start_task", None) is task:
                    window._trigger_start_task = None
                if getattr(window, "_start_task", None) is task:
                    window._start_task = None
                if getattr(window, "_stop_task", None) is task:
                    window._stop_task = None
        self.fixture.tearDown()

    def test_start_submission_failure_is_reported_and_does_not_leave_busy_state(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, record, ni, _ = self.fixture.window_for(kind, deferred=False)
                pool = RejectOncePool(window.thread_pool)
                self.pools.append((window, pool))
                window.thread_pool = pool
                errors = []
                window._show_hardware_error = errors.append
                with patch("rigol_control.ui.linkage_window.QMessageBox.warning"):
                    self.fixture.start(window)
                self.assertFalse(window._call_tasks)
                self.assertIsNone(window.controller)
                self.assertIsNone(manager.lease_owner(record.id))

    def test_stop_submission_failure_preserves_task_and_allows_retry(self):
        for kind in ("dg", "vaunix", "linkage"):
            with self.subTest(kind=kind):
                window, manager, record, ni, _ = self.fixture.window_for(kind, deferred=False)
                self.fixture.start(window)
                self.assertTrue(self.fixture.wait_for(lambda: not window._call_tasks))
                controller = window.controller
                pool = RejectOncePool(window.thread_pool)
                self.pools.append((window, pool))
                window.thread_pool = pool
                errors = []
                window._show_hardware_error = errors.append
                self.fixture.stop(window)
                self.assertFalse(window._call_tasks)
                self.assertIs(window.controller, controller)
                self.assertIsNotNone(manager.lease_owner(record.id))
                self.fixture.stop(window)
                self.assertTrue(self.fixture.wait_for(lambda: window.controller is None))
                self.assertIsNone(manager.lease_owner(record.id))
