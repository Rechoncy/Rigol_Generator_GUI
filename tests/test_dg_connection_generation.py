import unittest

from rigol_control.drivers.dg4000 import DG4000Driver, StepParameter, StepValue
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession


class GenerationBackend:
    def __init__(self, *, coupled=False):
        self.coupled = coupled
        self.queries = []
        self.writes = []
        self.fail_query = None
        self.fail_write = None

    def query(self, command):
        self.queries.append(command)
        if command == self.fail_query:
            raise OSError("lost connection")
        if command == "*IDN?":
            return "RIGOL,DG4162,TEST,1.0"
        if command == ":COUP:STAT?":
            return f"FREQ:{'ON' if self.coupled else 'OFF'},PHASE:OFF,AMPL:OFF"
        if command in {":SOUR1:FUNC?", ":SOUR2:FUNC?"}:
            return "SIN"
        return "0"

    def write(self, command):
        self.writes.append(command)
        if command == self.fail_write:
            raise OSError("lost connection")

    def close(self):
        pass


class DgConnectionGenerationTests(unittest.TestCase):
    values = (StepValue(StepParameter.FREQUENCY, 2, 2345),)

    def session_with_backends(self, *backends):
        iterator = iter(backends)
        session = ScpiSession("test", backend_factory=lambda *_: next(iterator))
        self.addCleanup(session.disconnect)
        session.connect()
        return session

    def test_disconnect_and_reconnect_invalidate_preflight_without_any_new_io(self):
        original = GenerationBackend()
        replacement = GenerationBackend(coupled=True)
        session = self.session_with_backends(original, replacement)
        driver = DG4000Driver(session)
        prepared = driver.prepare_trigger()
        checked_generation = session.connection_generation
        session.disconnect()
        self.assertGreater(session.connection_generation, checked_generation)
        original.queries.clear()
        original.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "通道检查已失效"):
            prepared.apply_steps(self.values)
        self.assertFalse(original.queries)
        self.assertFalse(original.writes)

        disconnected_generation = session.connection_generation
        session.connect()
        self.assertGreater(session.connection_generation, disconnected_generation)
        replacement.queries.clear()
        with self.assertRaisesRegex(RuntimeError, "通道检查已失效"):
            prepared.apply_steps(self.values)
        self.assertFalse(replacement.queries)
        self.assertFalse(replacement.writes)
        # Re-arming is explicit and must perform a fresh coupling check.
        with self.assertRaisesRegex(ValueError, "耦合已启用"):
            driver.prepare_trigger()
        self.assertEqual(replacement.queries, [":COUP:STAT?"])

    def test_automatic_query_and_write_recovery_invalidate_a_task_writer(self):
        for operation in ("query", "write"):
            with self.subTest(operation=operation):
                original = GenerationBackend()
                replacement = GenerationBackend(coupled=True)
                session = self.session_with_backends(original, replacement)
                prepared = DG4000Driver(session).prepare_trigger()
                generation = session.connection_generation
                if operation == "query":
                    original.fail_query = ":SYST:ERR?"
                    session.query(":SYST:ERR?", recover=True)
                else:
                    original.fail_write = "*CLS"
                    session.write("*CLS", recover=True)
                self.assertGreater(session.connection_generation, generation)
                replacement.queries.clear()
                replacement.writes.clear()
                with self.assertRaisesRegex(RuntimeError, "通道检查已失效"):
                    prepared.apply_steps(self.values)
                self.assertFalse(replacement.queries)
                self.assertFalse(replacement.writes)

    def test_identity_refresh_keeps_same_backend_preflight_valid(self):
        backend = GenerationBackend()
        session = self.session_with_backends(backend)
        prepared = DG4000Driver(session).prepare_trigger()
        generation = session.connection_generation
        session.query("*IDN?")
        session.connect()
        self.assertEqual(session.connection_generation, generation)
        with self.assertRaises(AttributeError):
            session.connection_generation = 100
        backend.queries.clear()
        prepared.apply_steps(self.values)
        self.assertFalse(backend.queries)
        self.assertEqual(backend.writes, [":SOUR2:FREQ 2345"])

    def test_virtual_session_matches_lifecycle_and_preserves_ch1(self):
        generator = VirtualGenerator("SIM::DG4000::1")
        generator.scpi_delay_ms = 0
        session = VirtualScpiSession(generator)
        self.addCleanup(session.disconnect)
        session.connect()
        generator.channels[1].update(frequency_hz=87123.0, output=True)
        ch1_before = generator.snapshot().channels[0]
        prepared = DG4000Driver(session).prepare_trigger()
        generation = session.connection_generation
        session.query("*IDN?")
        session.connect()
        self.assertEqual(session.connection_generation, generation)
        prepared.apply_steps(self.values)
        self.assertEqual(generator.snapshot().channels[0], ch1_before)
        session.disconnect()
        session.connect()
        self.assertGreater(session.connection_generation, generation)
        generator.commands.clear()
        with self.assertRaisesRegex(RuntimeError, "通道检查已失效"):
            prepared.apply_steps(self.values)
        self.assertFalse(generator.commands)
        self.assertEqual(generator.snapshot().channels[0], ch1_before)


if __name__ == "__main__":
    unittest.main()
