import threading
import time
import unittest

from rigol_control.drivers.dg4000 import BasicWaveform, DG4000Driver, StepParameter
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.trigger import ControllerState, TriggerStepController


class FakeScpiBackend:
    def __init__(self) -> None:
        self.writes = []
        self.queries = []
        self.closed = False

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, command: str) -> str:
        self.queries.append(command)
        if command == "*IDN?":
            return "RIGOL TECHNOLOGIES,DG4162,TEST,00.00\n"
        return "0"

    def close(self) -> None:
        self.closed = True


class FakeCounter:
    def __init__(self) -> None:
        self.count = 0
        self.started = False
        self.lock = threading.Lock()

    def start(self) -> None:
        with self.lock:
            self.count = 0
            self.started = True

    def read_count(self) -> int:
        with self.lock:
            if not self.started:
                raise RuntimeError("not started")
            return self.count

    def add_edges(self, count: int) -> None:
        with self.lock:
            self.count += count

    def stop(self) -> None:
        with self.lock:
            self.started = False


class RecordingWriter:
    def __init__(self, delay: float = 0.0) -> None:
        self.values = []
        self.delay = delay
        self.lock = threading.Lock()

    def apply_step(self, parameter, channel, value) -> None:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.values.append((parameter, channel, value))


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class ScpiDriverTests(unittest.TestCase):
    def test_session_serializes_typed_and_raw_commands(self) -> None:
        backend = FakeScpiBackend()
        session = ScpiSession("USB0::TEST", backend_factory=lambda resource, timeout: backend)
        driver = DG4000Driver(session)
        identity = driver.connect()
        self.assertIn("DG4162", identity)
        driver.configure_basic(1, BasicWaveform("SIN", 1000, 2.5, 0.1, 45))
        driver.apply_step(StepParameter.PSK_PHASE, 2, 180)
        driver.write(":DISP:BRIG 80")  # Any manual command remains reachable.
        self.assertEqual(
            backend.writes[:5],
            [
                ":SOUR1:FUNC SIN",
                ":SOUR1:FREQ 1000",
                ":SOUR1:VOLT 2.5",
                ":SOUR1:VOLT:OFFS 0.1",
                ":SOUR1:PHAS 45",
            ],
        )
        self.assertEqual(backend.writes[5], ":SOUR2:MOD:PSK:PHAS 180")
        self.assertEqual(backend.writes[6], ":DISP:BRIG 80")
        driver.disconnect()
        self.assertTrue(backend.closed)

    def test_all_requested_step_targets_use_manual_commands(self) -> None:
        expected = {
            StepParameter.FREQUENCY: ":SOUR2:FREQ",
            StepParameter.AMPLITUDE: ":SOUR2:VOLT",
            StepParameter.PHASE: ":SOUR2:PHAS",
            StepParameter.PSK_PHASE: ":SOUR2:MOD:PSK:PHAS",
            StepParameter.FSK_FREQUENCY: ":SOUR2:MOD:FSK:FREQ",
        }
        for parameter, command in expected.items():
            with self.subTest(parameter=parameter):
                self.assertEqual(parameter.command(2), command)


class TriggerControllerTests(unittest.TestCase):
    def test_count_jump_is_not_lost_and_first_trigger_is_start_point(self) -> None:
        counter = FakeCounter()
        writer = RecordingWriter(delay=0.001)
        controller = TriggerStepController(
            counter,
            writer,
            StepParameter.FREQUENCY,
            1,
            [1000, 2000, 3000],
            monitor_poll_seconds=0.001,
        )
        controller.start()
        time.sleep(0.01)
        self.assertEqual(writer.values, [])  # Start point is not sent before trigger 1.
        counter.add_edges(8)  # All eight arrive between software observations.
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 8))
        snapshot = controller.snapshot()
        controller.stop()

        self.assertEqual(snapshot.received_count, 8)
        self.assertEqual(snapshot.backlog, 0)
        self.assertEqual(
            [entry[2] for entry in writer.values],
            [1000, 2000, 3000, 1000, 2000, 3000, 1000, 2000],
        )
        self.assertEqual(controller.snapshot().state, ControllerState.STOPPED)

    def test_backlog_is_visible_while_slow_writer_catches_up(self) -> None:
        counter = FakeCounter()
        writer = RecordingWriter(delay=0.02)
        controller = TriggerStepController(
            counter,
            writer,
            StepParameter.AMPLITUDE,
            2,
            [1, 2],
            monitor_poll_seconds=0.001,
        )
        controller.start()
        counter.add_edges(5)
        self.assertTrue(wait_until(lambda: controller.snapshot().received_count == 5))
        self.assertGreater(controller.snapshot().backlog, 0)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 5, timeout=1.5))
        controller.stop()


if __name__ == "__main__":
    unittest.main()
