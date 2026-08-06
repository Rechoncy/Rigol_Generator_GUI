import tempfile
import threading
import time
import unittest
from pathlib import Path

from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import (
    LinkageAxis,
    LinkageRunLogger,
    MultiDeviceTriggerController,
    generate_linkage_points,
)
from rigol_control.linkage_tasks import LinkageTaskRecord, LinkageTaskRepository
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState


class FakeCounter:
    def __init__(self) -> None:
        self.count = 0
        self.running = False
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            self.count = 0
            self.running = True

    def read_count(self) -> int:
        with self.lock:
            if not self.running:
                raise RuntimeError("counter stopped")
            return self.count

    def add(self, count: int) -> None:
        with self.lock:
            self.count += count

    def stop(self) -> None:
        with self.lock:
            self.running = False


class RecordingWriter:
    def __init__(self, name: str, order: list[str], *, delay=0.0, fail=False) -> None:
        self.name = name
        self.order = order
        self.delay = delay
        self.fail = fail
        self.batches = []

    def apply_steps(self, values) -> None:
        if self.delay:
            time.sleep(self.delay)
        self.order.append(self.name)
        if self.fail:
            raise RuntimeError("injected failure")
        self.batches.append(tuple(values))


def wait_until(predicate, timeout=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class CrossDeviceScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.axes = (
            LinkageAxis("dg-a", StepParameter.FREQUENCY, 1, "kHz", 1, 1, 2),
            LinkageAxis("dg-b", StepParameter.AMPLITUDE, 2, "mVpp", 500, 250, 2),
        )

    def test_joint_points_keep_device_identity_and_units(self) -> None:
        points = generate_linkage_points(self.axes, ScanMode.SYNCHRONOUS)
        self.assertEqual(len(points), 2)
        self.assertEqual([item.device_id for item in points[0].values], ["dg-a", "dg-b"])
        self.assertEqual([item.value for item in points[1].values], [2000, 0.75])

    def test_first_trigger_is_p0_then_cycles_with_strict_device_barrier(self) -> None:
        counter = FakeCounter()
        order: list[str] = []
        writers = {
            "dg-a": RecordingWriter("dg-a", order),
            "dg-b": RecordingWriter("dg-b", order, delay=0.002),
        }
        controller = MultiDeviceTriggerController(
            counter,
            writers,
            generate_linkage_points(self.axes, ScanMode.SYNCHRONOUS),
            device_order=("dg-a", "dg-b"),
            monitor_poll_seconds=0.001,
        )
        controller.start()
        self.assertEqual(writers["dg-a"].batches, [])
        counter.add(3)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 3))
        snapshot = controller.snapshot()
        controller.stop()

        self.assertEqual(order, ["dg-a", "dg-b"] * 3)
        self.assertEqual(writers["dg-a"].batches[0][0].value, 1000)
        self.assertEqual(writers["dg-a"].batches[1][0].value, 2000)
        self.assertEqual(writers["dg-a"].batches[2][0].value, 1000)
        self.assertEqual(snapshot.current_index, 0)
        self.assertEqual(snapshot.backlog, 0)
        self.assertGreater(snapshot.skew_ms, 0)

    def test_partial_failure_does_not_advance_applied_count_and_is_logged(self) -> None:
        counter = FakeCounter()
        order: list[str] = []
        writers = {
            "dg-a": RecordingWriter("dg-a", order),
            "dg-b": RecordingWriter("dg-b", order, fail=True),
        }
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "run.csv"
            controller = MultiDeviceTriggerController(
                counter,
                writers,
                generate_linkage_points(self.axes, ScanMode.SYNCHRONOUS),
                device_order=("dg-a", "dg-b"),
                monitor_poll_seconds=0.001,
                logger=LinkageRunLogger(log_path),
            )
            controller.start()
            counter.add(1)
            self.assertTrue(
                wait_until(lambda: controller.snapshot().state is ControllerState.ERROR)
            )
            snapshot = controller.snapshot()
            controller.stop()
            self.assertEqual(snapshot.applied_count, 0)
            self.assertEqual(snapshot.backlog, 1)
            self.assertIn("部分下发失败", snapshot.error)
            self.assertIn("partial_error", log_path.read_text(encoding="utf-8-sig"))

    def test_one_hundred_batched_edges_are_replayed_without_loss(self) -> None:
        counter = FakeCounter()
        order: list[str] = []
        writers = {
            "dg-a": RecordingWriter("dg-a", order),
            "dg-b": RecordingWriter("dg-b", order),
        }
        controller = MultiDeviceTriggerController(
            counter,
            writers,
            generate_linkage_points(self.axes, ScanMode.SYNCHRONOUS),
            device_order=("dg-a", "dg-b"),
            monitor_poll_seconds=0.001,
        )
        controller.start()
        counter.add(100)
        self.assertTrue(
            wait_until(lambda: controller.snapshot().applied_count == 100, timeout=2.0)
        )
        snapshot = controller.snapshot()
        controller.stop()
        self.assertEqual(snapshot.received_count, 100)
        self.assertEqual(snapshot.backlog, 0)
        self.assertEqual(len(writers["dg-a"].batches), 100)
        self.assertEqual(len(writers["dg-b"].batches), 100)
        self.assertEqual(snapshot.current_index, 1)

    def test_task_definition_round_trips_with_explicit_edited_points(self) -> None:
        points = list(generate_linkage_points(self.axes, ScanMode.SYNCHRONOUS))
        task = LinkageTaskRecord.create(
            name="two generator scan",
            ni_device_id="ni-1",
            counter="ctr0",
            terminal="PFI0",
            rising=True,
            scan_mode=ScanMode.SYNCHRONOUS,
            axes=self.axes,
            points=points,
        )
        with tempfile.TemporaryDirectory() as temporary:
            repository = LinkageTaskRepository(temporary)
            repository.save(task)
            restored = repository.list()[0]
            self.assertEqual(restored, task)
            self.assertEqual(repository.active_task_id(), task.id)
            repository.set_active_task(restored.id)
            self.assertEqual(repository.active_task_id(), restored.id)


class DeviceLeaseTests(unittest.TestCase):
    def test_leases_are_atomic_and_owner_release_restores_access(self) -> None:
        manager = DeviceConnectionManager()
        manager.acquire_devices("task-a", ("dg-a", "ni-a"))
        with self.assertRaises(DeviceBusyError):
            manager.acquire_devices("task-b", ("dg-b", "ni-a"))
        self.assertIsNone(manager.lease_owner("dg-b"))
        manager.release_devices("task-a")
        manager.acquire_devices("task-b", ("dg-b", "ni-a"))
        self.assertEqual(manager.lease_owner("ni-a"), "task-b")


if __name__ == "__main__":
    unittest.main()
