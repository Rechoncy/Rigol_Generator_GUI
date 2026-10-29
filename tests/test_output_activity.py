from dataclasses import FrozenInstanceError, replace
from threading import Event, Thread
import time
import unittest

from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.linkage import DeviceApplyResult, LinkageSnapshot
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.trigger import ControllerState, TriggerSnapshot, TriggerStepController


class ObservedController:
    def __init__(self, *, linkage=False):
        if linkage:
            self.current = LinkageSnapshot(ControllerState.STOPPED, 0, 0, None, None)
        else:
            self.current = TriggerSnapshot(ControllerState.STOPPED, 0, 0, None, None, None)
        self.is_quiescent = True
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        self.current = replace(self.current, revision=self.calls)
        return self.current

    def update(self, state, received=0, applied=0, *, quiescent=False, **extra):
        self.current = replace(self.current, state=state, received_count=received,
                               applied_count=applied, **extra)
        self.is_quiescent = quiescent

    def start(self):
        raise AssertionError("Activity observation must never start a controller")

    def stop(self):
        raise AssertionError("Activity observation must never stop a controller")


class OutputActivityTests(unittest.TestCase):
    def setUp(self):
        self.manager = DeviceConnectionManager()
        self.dg = DeviceRecord.create(kind=DeviceKind.DG4000, name="A", resource="SIM::DG4000::1")
        self.manager.connect(self.dg)

    def watch(self, controller, records=None, owner="task"):
        records = records or (self.dg,)
        self.manager.acquire_devices(owner, [item.id for item in records])
        self.manager.watch_output_task(owner, records, controller)

    def test_manual_changes_share_physical_alias_revision_and_are_immutable(self):
        alias = DeviceRecord.create(kind=DeviceKind.DG4000, name="Alias", resource=" sim::dg4000::1 ")
        first = self.manager.output_readback_activity(self.dg)
        self.assertEqual(first.revision, 0)
        self.assertFalse(first.busy)
        self.manager.note_output_change(self.dg)
        after = self.manager.output_readback_activity(alias)
        self.assertEqual(after.revision, 1)
        self.assertNotEqual(after.barrier, first.barrier)
        with self.assertRaises(FrozenInstanceError):
            after.revision = 2

    def test_manual_change_revision_is_thread_safe(self):
        threads = [Thread(target=lambda: [self.manager.note_output_change(self.dg) for _ in range(100)])
                   for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(self.manager.output_readback_activity(self.dg).revision, 500)

    def test_unstarted_and_backlogged_tasks_are_busy_and_quiet_barrier_is_stable(self):
        controller = ObservedController()
        self.watch(controller)
        registered = self.manager.output_readback_activity(self.dg)
        self.assertTrue(registered.busy)
        self.assertEqual(registered.revision, 0)
        controller.update(ControllerState.RUNNING, received=3)
        self.assertTrue(self.manager.output_readback_activity(self.dg).busy)
        controller.update(ControllerState.RUNNING, received=3, applied=3)
        first = self.manager.output_readback_activity(self.dg)
        second = self.manager.output_readback_activity(self.dg)
        self.assertFalse(first.busy)
        self.assertGreater(first.revision, 0)
        self.assertEqual(first, second, "snapshot() revision alone must not reset the quiet window")

    def test_release_observes_a_fast_completed_run_and_retains_revision(self):
        controller = ObservedController()
        self.watch(controller)
        controller.update(ControllerState.STOPPED, received=7, applied=7, quiescent=True)
        self.manager.release_devices("task")
        activity = self.manager.output_readback_activity(self.dg)
        self.assertGreater(activity.revision, 0)
        self.assertFalse(activity.busy)
        self.assertEqual(self.manager.active_leases(), {})
        calls = controller.calls
        self.manager.output_readback_activity(self.dg)
        self.assertEqual(controller.calls, calls)

    def test_new_controller_zero_does_not_replay_previous_task_changes(self):
        old = ObservedController()
        self.watch(old)
        old.update(ControllerState.STOPPED, received=100, applied=100, quiescent=True)
        self.manager.release_devices("task")
        old_revision = self.manager.output_readback_activity(self.dg).revision
        new = ObservedController()
        self.watch(new)
        new.update(ControllerState.RUNNING)
        self.assertEqual(self.manager.output_readback_activity(self.dg).revision, old_revision)
        new.update(ControllerState.RUNNING, received=1, applied=1)
        self.assertGreater(self.manager.output_readback_activity(self.dg).revision, old_revision)

    def test_partial_linkage_success_marks_only_successful_resources_and_keeps_failed_stop_lease(self):
        other = DeviceRecord.create(kind=DeviceKind.DG4000, name="B", resource="SIM::DG4000::2")
        self.manager.connect(other)
        controller = ObservedController(linkage=True)
        self.watch(controller, (self.dg, other))
        controller.update(ControllerState.ERROR, received=1, applied=0, device_results=(
            DeviceApplyResult(self.dg.id, 1, 2, True), DeviceApplyResult(other.id, 2, 3, False, "unknown acceptance")))
        first = self.manager.output_readback_activity(self.dg)
        second = self.manager.output_readback_activity(other)
        self.assertEqual(first.revision, 1)
        self.assertEqual(second.revision, 0)
        self.assertTrue(first.busy)
        self.assertTrue(second.busy)
        with self.assertRaises(DeviceBusyError):
            self.manager.release_devices("task")
        self.assertEqual(self.manager.lease_owner(self.dg.id), "task")
        self.assertEqual(self.manager.output_readback_activity(self.dg).revision, 1)
        controller.is_quiescent = True
        self.manager.release_devices("task")
        self.assertFalse(self.manager.output_readback_activity(self.dg).busy)
        self.assertEqual(self.manager.output_readback_activity(other).revision, 0)

    def test_new_partial_success_is_detected_even_when_applied_count_is_unchanged(self):
        other = DeviceRecord.create(kind=DeviceKind.DG4000, name="B", resource="SIM::DG4000::2")
        self.manager.connect(other)
        controller = ObservedController(linkage=True)
        self.watch(controller, (self.dg, other))
        controller.update(ControllerState.RUNNING, received=1, applied=1, device_results=(
            DeviceApplyResult(self.dg.id, 1, 2, True), DeviceApplyResult(other.id, 2, 3, True)))
        first = self.manager.output_readback_activity(self.dg)
        other_revision = self.manager.output_readback_activity(other).revision
        controller.update(ControllerState.ERROR, received=2, applied=1, device_results=(
            DeviceApplyResult(self.dg.id, 4, 5, True), DeviceApplyResult(other.id, 5, 6, False, "failed")))
        self.assertGreater(self.manager.output_readback_activity(self.dg).revision, first.revision)
        self.assertEqual(self.manager.output_readback_activity(other).revision, other_revision)

    def test_alias_observes_task_and_unwatched_leases_are_busy(self):
        alias = self.dg.updated(name="Alias")
        controller = ObservedController()
        self.watch(controller)
        controller.update(ControllerState.RUNNING, received=1, applied=1)
        self.assertGreater(self.manager.output_readback_activity(alias).revision, 0)
        controller.update(ControllerState.STOPPED, received=1, applied=1, quiescent=True)
        self.manager.release_devices("task")
        self.manager.acquire_devices("unknown", (self.dg.id,))
        self.assertTrue(self.manager.output_readback_activity(alias).busy)

    def test_other_vaunix_backlog_blocks_global_dll_readback_but_not_dg(self):
        vaunix = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="V1", resource=VIRTUAL_VAUNIX_RESOURCE)
        other = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="V2", resource="SIM::VAUNIX::LMS::2")
        self.manager.connect(vaunix)
        self.manager.connect(other)
        controller = ObservedController()
        self.watch(controller, (vaunix,))
        controller.update(ControllerState.RUNNING, received=3, applied=1)
        self.assertTrue(self.manager.output_readback_activity(other).busy)
        self.assertFalse(self.manager.output_readback_activity(self.dg).busy)
        controller.update(ControllerState.RUNNING, received=3, applied=3)
        self.assertFalse(self.manager.output_readback_activity(other).busy)
        self.assertEqual(self.manager.output_readback_activity(other).revision, 0)

    def test_watch_requires_lease_and_cannot_replace_active_controller(self):
        controller = ObservedController()
        with self.assertRaises(DeviceBusyError):
            self.manager.watch_output_task("absent", (self.dg,), controller)
        self.watch(controller)
        with self.assertRaises(DeviceBusyError):
            self.manager.watch_output_task("task", (self.dg,), ObservedController())

    def test_controller_state_is_never_read_under_manager_lock(self):
        manager = self.manager
        violations = []

        class UnlockedController(ObservedController):
            def snapshot(self):
                entered = Event()
                worker = Thread(target=lambda: (manager.active_leases(), entered.set()))
                worker.start()
                if not entered.wait(.5):
                    violations.append("snapshot under manager lock")
                    raise AssertionError("Manager lock held across controller.snapshot()")
                worker.join(.5)
                return super().snapshot()

        controller = UnlockedController()
        self.watch(controller)
        controller.update(ControllerState.RUNNING)
        self.assertFalse(self.manager.output_readback_activity(self.dg).busy)
        controller.update(ControllerState.STOPPED, quiescent=True)
        self.manager.release_devices("task")
        self.assertEqual(self.manager.active_leases(), {})
        self.assertEqual(violations, [])

    def test_observation_error_is_busy_without_recounting_old_success(self):
        controller = ObservedController()
        self.watch(controller)
        controller.update(ControllerState.RUNNING, received=2, applied=2)
        first = self.manager.output_readback_activity(self.dg)
        original = controller.snapshot
        controller.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("unavailable state"))
        self.assertTrue(self.manager.output_readback_activity(self.dg).busy)
        with self.assertRaises(DeviceBusyError):
            self.manager.release_devices("task")
        controller.snapshot = original
        recovered = self.manager.output_readback_activity(self.dg)
        self.assertFalse(recovered.busy)
        self.assertEqual(recovered.revision, first.revision)

    def test_real_controller_fast_run_is_observed_on_release_and_keeps_p0_order(self):
        class Counter:
            count = 0
            def start(self):
                pass
            def read_count(self):
                return self.count
            def stop(self):
                pass

        class Writer:
            def __init__(self):
                self.values = []
            def apply_steps(self, values):
                self.values.append(values[0].value)

        counter, writer = Counter(), Writer()
        controller = TriggerStepController(counter, writer, StepParameter.FREQUENCY, 2, [1000, 2000])
        self.watch(controller)
        try:
            controller.start()
            self.assertEqual(writer.values, [], "Arming must not apply a point")
            counter.count = 9
            deadline = time.monotonic() + 2
            while controller.snapshot().applied_count < 9 and time.monotonic() < deadline:
                time.sleep(.002)
            self.assertEqual(controller.snapshot().applied_count, 9)
            controller.stop()
            # No activity poll occurred during the run.
            self.manager.release_devices("task")
            final = self.manager.output_readback_activity(self.dg)
            self.assertGreater(final.revision, 0)
            self.assertFalse(final.busy)
            self.assertEqual(writer.values, [1000, 2000, 1000, 2000, 1000, 2000, 1000, 2000, 1000])
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
