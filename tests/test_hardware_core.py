import threading
import time
import unittest

from rigol_control.drivers.dg4000 import (
    BasicWaveform,
    DG4000_FAMILY_MAX_FREQUENCY_HZ,
    DG4000Driver,
    StepParameter,
    StepValue,
    dg4000_capabilities,
    output_load_value,
    parse_output_state,
)
from rigol_control.scan import ScanAxis, ScanMode, generate_scan_points
from rigol_control.drivers.scpi import ScpiError, ScpiSession
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
        self.batches = []
        self.delay = delay
        self.lock = threading.Lock()

    def apply_step(self, parameter, channel, value) -> None:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.values.append((parameter, channel, value))

    def apply_steps(self, values) -> None:
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            batch = tuple(values)
            self.batches.append(batch)
            self.values.extend((item.parameter, item.channel, item.value) for item in batch)


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class ScpiDriverTests(unittest.TestCase):
    def test_dg4000_model_frequency_capabilities_and_driver_guard(self) -> None:
        expected = {
            "DG4062": 60_000_000,
            "DG4102": 100_000_000,
            "DG4162": 160_000_000,
            "DG4202": 200_000_000,
        }
        for model, maximum in expected.items():
            with self.subTest(model=model):
                capabilities = dg4000_capabilities(
                    f"RIGOL TECHNOLOGIES,{model},TEST,00.00"
                )
                self.assertEqual(capabilities.model, model)
                self.assertTrue(capabilities.model_recognized)
                self.assertEqual(capabilities.maximum_frequency_hz, maximum)

        virtual = dg4000_capabilities(
            "RIGOL TECHNOLOGIES,DG4162-VIRTUAL,SIM0001,00.01"
        )
        self.assertEqual(virtual.model, "DG4162")
        self.assertEqual(virtual.maximum_frequency_hz, 160_000_000)
        unknown = dg4000_capabilities("RIGOL TECHNOLOGIES,DG4999,TEST,00.00")
        self.assertFalse(unknown.model_recognized)
        self.assertEqual(
            unknown.maximum_frequency_hz,
            DG4000_FAMILY_MAX_FREQUENCY_HZ,
        )

        dg4162_backend = FakeScpiBackend()
        dg4162 = DG4000Driver(
            ScpiSession(
                "USB0::DG4162",
                backend_factory=lambda _resource, _timeout: dg4162_backend,
            )
        )
        dg4162.connect()
        with self.assertRaisesRegex(ValueError, "DG4162.*160 MHz"):
            dg4162.configure_basic(1, BasicWaveform("SIN", 200_000_000, 1.0))

        class Dg4202Backend(FakeScpiBackend):
            def query(self, command: str) -> str:
                self.queries.append(command)
                if command == "*IDN?":
                    return "RIGOL TECHNOLOGIES,DG4202,TEST,00.00\n"
                return "0"

        dg4202_backend = Dg4202Backend()
        dg4202 = DG4000Driver(
            ScpiSession(
                "USB0::DG4202",
                backend_factory=lambda _resource, _timeout: dg4202_backend,
            )
        )
        dg4202.connect()
        dg4202.configure_basic(1, BasicWaveform("SIN", 200_000_000, 1.0))
        self.assertIn(":SOUR1:FREQ 200000000", dg4202_backend.writes)

    def test_idempotent_settings_reconnect_once_after_transient_io_failure(self) -> None:
        class FailingWriteBackend(FakeScpiBackend):
            def write(self, command: str) -> None:
                raise OSError("simulated USB transport loss")

        backends = []

        def factory(_resource, _timeout):
            backend = FailingWriteBackend() if not backends else FakeScpiBackend()
            backends.append(backend)
            return backend

        driver = DG4000Driver(ScpiSession("USB0::TEST", backend_factory=factory))
        driver.connect()
        driver.configure_basic(1, BasicWaveform("SIN", 1000, 1.0))
        self.assertEqual(len(backends), 2)
        self.assertTrue(backends[0].closed)
        self.assertEqual(backends[1].queries, ["*IDN?"])
        self.assertEqual(backends[1].writes[0], ":SOUR1:FUNC SIN")

        raw_backends = []

        def raw_factory(_resource, _timeout):
            backend = FailingWriteBackend()
            raw_backends.append(backend)
            return backend

        raw_driver = DG4000Driver(
            ScpiSession("USB0::RAW", backend_factory=raw_factory)
        )
        raw_driver.connect()
        with self.assertRaisesRegex(ScpiError, "simulated USB transport loss"):
            raw_driver.write("*TRG")
        self.assertEqual(len(raw_backends), 1)

    def test_session_serializes_typed_and_raw_commands(self) -> None:
        backend = FakeScpiBackend()
        session = ScpiSession("USB0::TEST", backend_factory=lambda resource, timeout: backend)
        driver = DG4000Driver(session)
        identity = driver.connect()
        self.assertIn("DG4162", identity)
        driver.configure_basic(1, BasicWaveform("SIN", 1000, 2.5, 0.1, 45))
        driver.apply_step(StepParameter.PSK_PHASE, 2, 180)
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
        driver.apply_steps(
            (
                StepValue(StepParameter.FREQUENCY, 1, 2000),
                StepValue(StepParameter.AMPLITUDE, 1, 1.5),
            )
        )
        driver.write(":DISP:BRIG 80")  # Any manual command remains reachable.
        self.assertEqual(backend.writes[6:8], [":SOUR1:FREQ 2000", ":SOUR1:VOLT 1.5"])
        self.assertEqual(backend.writes[8], ":DISP:BRIG 80")
        driver.disconnect()
        self.assertTrue(backend.closed)

    def test_output_load_supports_high_impedance_and_manual_range(self) -> None:
        self.assertEqual(output_load_value(None), "INFINITY")
        self.assertEqual(output_load_value(50), "50")
        with self.assertRaises(ValueError):
            output_load_value(0)
        with self.assertRaises(ValueError):
            output_load_value(50.5)

        backend = FakeScpiBackend()
        session = ScpiSession("USB0::TEST", backend_factory=lambda resource, timeout: backend)
        driver = DG4000Driver(session)
        driver.connect()
        driver.configure_channel(
            2,
            BasicWaveform("SIN", 2000, 1.5, 0.1, 30),
            load_ohms=75,
            enabled=True,
        )
        self.assertEqual(backend.writes[0], ":OUTP2:LOAD 75")
        self.assertEqual(backend.writes[-1], ":OUTP2 ON")

        backend.writes.clear()
        driver.configure_waveform_output(
            1,
            BasicWaveform("SQU", 5000, 2.0, 0.0, 10),
            enabled=False,
        )
        self.assertFalse(any(":LOAD" in command for command in backend.writes))
        self.assertEqual(backend.writes[-1], ":OUTP1 OFF")

    def test_output_state_query_accepts_numeric_and_text_responses(self) -> None:
        self.assertTrue(parse_output_state("1"))
        self.assertTrue(parse_output_state('"ON"'))
        self.assertFalse(parse_output_state("0"))
        self.assertFalse(parse_output_state("off"))
        with self.assertRaises(ValueError):
            parse_output_state("RIGOL")

        backend = FakeScpiBackend()
        session = ScpiSession("USB0::TEST", backend_factory=lambda resource, timeout: backend)
        driver = DG4000Driver(session)
        driver.connect()
        self.assertEqual(driver.output_states(), (False, False))
        self.assertEqual(backend.queries[-2:], [":OUTP1?", ":OUTP2?"])

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
    def test_multi_axis_point_is_written_as_one_ordered_batch(self) -> None:
        counter = FakeCounter()
        writer = RecordingWriter()
        points = generate_scan_points(
            [
                ScanAxis(StepParameter.FREQUENCY, 1, "kHz", 1, 1, 2),
                ScanAxis(StepParameter.AMPLITUDE, 1, "Vpp", 1, 0.5, 2),
            ],
            ScanMode.SYNCHRONOUS,
        )
        controller = TriggerStepController(
            counter,
            writer,
            scan_points=points,
            monitor_poll_seconds=0.001,
        )
        controller.start()
        counter.add_edges(2)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 2))
        snapshot = controller.snapshot()
        controller.stop()

        self.assertEqual(len(writer.batches), 2)
        self.assertEqual([item.value for item in writer.batches[1]], [2000, 1.5])
        self.assertEqual(len(snapshot.current_values), 2)
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
