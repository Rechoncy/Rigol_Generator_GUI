from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import threading
import unittest

from rigol_control.drivers.dg4000 import DG4000Driver, StepParameter, StepValue
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession


class ReadbackBackend:
    def __init__(self):
        self.responses = {
            "*IDN?": "RIGOL TECHNOLOGIES,DG4162,SAME_DEVICE,1.0",
            ":SOUR1:VOLT:UNIT?": "VPP", ":SOUR1:FREQ?": "+1.250000E+06",
            ":SOUR1:VOLT?": "+2.500000E+00", ":OUTP1?": "ON",
            ":SOUR2:VOLT:UNIT?": "DBM", ":SOUR2:FREQ?": "+2.750000E+03",
            ":SOUR2:VOLT?": "-2.000000E+01", ":OUTP2?": "OFF",
            ":COUP:STAT?": "FREQ:ON,PHASE:ON,AMPL:ON",
            ":SOUR1:FUNC?": "SIN", ":SOUR2:FUNC?": "SIN",
        }
        self.events = []
        self.on_query = None
        self.fail_query = None

    def query(self, command):
        self.events.append(("query", command))
        if self.on_query is not None:
            self.on_query(command)
        if self.fail_query == command:
            raise OSError("READBACK_TIMEOUT")
        response = self.responses[command]
        return response.pop(0) if isinstance(response, list) else response

    def write(self, command):
        self.events.append(("write", command))

    def close(self):
        pass


class AuditedSession(ScpiSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transaction_depth = 0
        self.query_flags = []

    @contextmanager
    def transaction(self):
        with super().transaction():
            self.transaction_depth += 1
            try:
                yield self
            finally:
                self.transaction_depth -= 1

    def query(self, command, *, recover=False):
        self.query_flags.append((command, recover, self.transaction_depth))
        return super().query(command, recover=recover)


class DgOutputReadbackTests(unittest.TestCase):
    def setUp(self):
        self.backend = ReadbackBackend()
        self.opened = []

        def factory(*_args):
            self.opened.append(self.backend)
            return self.backend

        self.session = AuditedSession("test", backend_factory=factory)
        self.driver = DG4000Driver(self.session)
        self.driver.connect()
        self.backend.events.clear()
        self.addCleanup(self.driver.disconnect)

    def test_reads_both_channels_in_native_units_without_any_write_or_recovery(self):
        identity = self.driver.identity
        pin = self.driver._identity
        generation = self.session.connection_generation
        result = self.driver.read_output_parameters()
        self.assertEqual([item.channel for item in result], [1, 2])
        self.assertEqual((result[0].frequency_hz, result[0].amplitude, result[0].amplitude_unit),
                         (1.25e6, 2.5, "VPP"))
        self.assertEqual((result[1].frequency_hz, result[1].amplitude, result[1].amplitude_unit),
                         (2750, -20, "DBM"))
        self.assertTrue(result[0].output_enabled)
        self.assertFalse(result[1].output_enabled)
        self.assertEqual([item.connection_generation for item in result], [generation, generation])
        self.assertEqual(self.driver.identity, identity)
        self.assertEqual(self.driver._identity, pin)
        self.assertEqual(self.opened, [self.backend])
        self.assertEqual(len(self.backend.events), 10)
        self.assertTrue(all(kind == "query" and command.endswith("?") for kind, command in self.backend.events))
        self.assertNotIn(("query", ":COUP:STAT?"), self.backend.events)
        self.assertTrue(all(not recover and depth == 1 for _, recover, depth in self.session.query_flags))
        with self.assertRaises(FrozenInstanceError):
            result[0].amplitude = 999

    def test_selected_channel_order_and_explicit_ch2_headers_are_preserved(self):
        result = self.driver.read_output_parameters((2,))
        self.assertEqual([item.channel for item in result], [2])
        self.assertEqual([command for _, command in self.backend.events], [
            ":SOUR2:VOLT:UNIT?", ":SOUR2:FREQ?", ":SOUR2:VOLT?", ":OUTP2?", ":SOUR2:VOLT:UNIT?",
        ])
        self.assertEqual([item.channel for item in self.driver.read_output_parameters((2, 1))], [2, 1])

    def test_invalid_channel_group_performs_no_io(self):
        for channels in ((), (1, 1), (3,), (True,), ("1",)):
            with self.subTest(channels=channels):
                with self.assertRaises(ValueError):
                    self.driver.read_output_parameters(channels)
                self.assertFalse(self.backend.events)

    def test_numeric_errors_include_command_and_original_response(self):
        for command in (":SOUR1:FREQ?", ":SOUR1:VOLT?"):
            original = self.backend.responses[command]
            for raw in ("", "NaN", "+Inf", "1E309", "ERROR,-113", "-1.250000E+00"):
                with self.subTest(command=command, raw=raw):
                    self.backend.responses[command] = raw
                    with self.assertRaises(ValueError) as caught:
                        self.driver.read_output_parameters()
                    self.assertIn(command, str(caught.exception))
                    self.assertIn(repr(raw), str(caught.exception))
                    self.assertFalse(any(kind == "write" for kind, _ in self.backend.events))
            self.backend.responses[command] = original

    def test_unit_and_output_errors_include_raw_reply(self):
        for command, raw in ((":SOUR1:VOLT:UNIT?", "VOLTS"), (":SOUR1:VOLT:UNIT?", ""),
                             (":OUTP1?", "UNKNOWN")):
            with self.subTest(command=command, raw=raw):
                original = self.backend.responses[command]
                self.backend.responses[command] = raw
                with self.assertRaises(ValueError) as caught:
                    self.driver.read_output_parameters()
                self.assertIn(command, str(caught.exception))
                self.assertIn(repr(raw), str(caught.exception))
                self.backend.responses[command] = original

    def test_unit_change_rejects_whole_group_instead_of_returning_partial_channels(self):
        self.backend.responses[":SOUR2:VOLT:UNIT?"] = ["DBM", "VRMS"]
        with self.assertRaisesRegex(ValueError, r":SOUR2:VOLT:UNIT\?.*DBM.*VRMS"):
            self.driver.read_output_parameters()
        self.assertEqual(len(self.backend.events), 10)

    def test_transport_error_does_not_reconnect_or_advance_generation(self):
        self.backend.fail_query = ":SOUR2:VOLT?"
        generation = self.session.connection_generation
        with self.assertRaisesRegex(RuntimeError, r":SOUR2:VOLT\?.*READBACK_TIMEOUT"):
            self.driver.read_output_parameters()
        self.assertEqual(self.opened, [self.backend])
        self.assertEqual(self.session.connection_generation, generation)
        self.assertFalse(any(kind == "write" for kind, _ in self.backend.events))

    def test_external_connection_generation_change_rejects_the_group(self):
        def changed(command):
            if command == ":OUTP2?":
                self.session._advance_connection_generation()
        self.backend.on_query = changed
        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
            self.driver.read_output_parameters()

    def test_readback_serializes_with_a_prepared_parameter_writer_without_invalidating_it(self):
        self.backend.responses[":COUP:STAT?"] = "FREQ:OFF,PHASE:OFF,AMPL:OFF"
        prepared = self.driver.prepare_trigger()
        self.backend.events.clear()
        reading, release, writer_requested, write_done = (threading.Event() for _ in range(4))
        errors = []

        def paused(command):
            if command == ":SOUR1:VOLT:UNIT?" and not reading.is_set():
                reading.set()
                if not release.wait(2):
                    raise TimeoutError("readback test was not released")

        def readback():
            try:
                self.driver.read_output_parameters()
            except Exception as exc:
                errors.append(exc)

        def write():
            writer_requested.set()
            try:
                prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 4567),))
            except Exception as exc:
                errors.append(exc)
            finally:
                write_done.set()

        self.backend.on_query = paused
        reader = threading.Thread(target=readback)
        writer = threading.Thread(target=write)
        reader.start()
        try:
            self.assertTrue(reading.wait(1))
            writer.start()
            self.assertTrue(writer_requested.wait(1))
            self.assertFalse(write_done.wait(.03))
        finally:
            release.set()
            reader.join(2)
            if writer.ident is not None:
                writer.join(2)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.backend.events[-1], ("write", ":SOUR2:FREQ 4567"))
        self.assertEqual(len(self.backend.events), 11)
        self.assertTrue(all(kind == "query" for kind, _ in self.backend.events[:-1]))


class VirtualDgOutputReadbackTests(unittest.TestCase):
    def setUp(self):
        self.generator = VirtualGenerator("SIM::DG4000::readback")
        self.generator.scpi_delay_ms = 0
        self.driver = DG4000Driver(VirtualScpiSession(self.generator))
        self.driver.connect()
        self.addCleanup(self.driver.disconnect)

    def test_virtual_vrms_readback_preserves_vpp_settings_and_other_channel(self):
        self.driver.write_many((":SOUR1:FREQ 1234", ":SOUR1:VOLT 2VPP", ":OUTP1 ON",
                                ":SOUR2:FREQ 9876", ":SOUR2:VOLT 0.8VPP", ":OUTP2 OFF",
                                ":SOUR2:VOLT:UNIT VRMS"))
        before = self.generator.snapshot().channels
        units = self.generator.amplitude_units.copy()
        values = self.generator.values.copy()
        self.generator.commands.clear()
        result = self.driver.read_output_parameters()
        self.assertEqual((result[0].frequency_hz, result[0].amplitude), (1234, 2))
        self.assertEqual((result[1].frequency_hz, result[1].amplitude_unit), (9876, "VRMS"))
        self.assertAlmostEqual(result[1].amplitude, .8 / (2 * 2 ** .5))
        self.assertTrue(result[0].output_enabled)
        self.assertFalse(result[1].output_enabled)
        self.assertEqual(self.generator.snapshot().channels, before)
        self.assertEqual(self.generator.amplitude_units, units)
        self.assertEqual(self.generator.values, values)
        self.assertTrue(all(command.endswith("?") for command in self.generator.commands))

    def test_virtual_dbm_readback_returns_native_power_without_changing_unit(self):
        self.driver.write_many((":SOUR2:VOLT:UNIT DBM", ":SOUR2:VOLT -20"))
        before = self.generator.snapshot().channels
        self.generator.commands.clear()
        result = self.driver.read_output_parameters((2,))[0]
        self.assertEqual(result.amplitude_unit, "DBM")
        self.assertAlmostEqual(result.amplitude, -20)
        self.assertEqual(self.generator.snapshot().channels, before)
        self.assertEqual(self.generator.amplitude_units[2], "DBM")
        self.assertTrue(all(command.endswith("?") for command in self.generator.commands))

    def test_virtual_non_sine_rms_is_not_fabricated(self):
        self.driver.write_many((":SOUR2:FUNC SQU", ":SOUR2:VOLT:UNIT VRMS"))
        with self.assertRaisesRegex(RuntimeError, "未模拟非正弦波"):
            self.driver.read_output_parameters((2,))


if __name__ == "__main__":
    unittest.main()
