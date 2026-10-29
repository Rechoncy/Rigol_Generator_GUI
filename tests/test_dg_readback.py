from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import threading
import unittest
from unittest.mock import patch
from unittest.mock import patch

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
            ":SOUR1:VOLT:HIGH?": "1.25", ":SOUR1:VOLT:LOW?": "-1.25",
            ":SOUR2:VOLT:HIGH?": "0.03162277660168379", ":SOUR2:VOLT:LOW?": "-0.03162277660168379",
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

    def test_unit_reply_in_numeric_field_marks_unsynchronized_and_stops_the_group(self):
        original = self.backend.responses.copy()
        for command in (":SOUR1:FREQ?", ":SOUR1:VOLT?", ":SOUR1:VOLT:HIGH?", ":SOUR1:VOLT:LOW?"):
            for reply in ("VPP", "VRMS", "DBM"):
                with self.subTest(command=command, reply=reply):
                    self.backend.responses = original.copy()
                    if command.endswith((":HIGH?", ":LOW?")):
                        self.backend.responses[":SOUR1:VOLT:UNIT?"] = ""
                    self.backend.responses[command] = reply
                    self.backend.events.clear()
                    with patch.object(self.session, "mark_response_unsynchronized", create=True) as marker:
                        with self.assertRaisesRegex(ValueError, "疑似回复错位.*断开并重新连接"):
                            self.driver.read_output_parameters((1,))
                        marker.assert_called_once_with()
                    self.assertEqual(self.backend.events[-1], ("query", command))
                    self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))
                    self.assertEqual(self.opened, [self.backend])

    def test_empty_unit_recovers_query_only_vpp_from_negative_voltage_levels(self):
        self.backend.responses[":SOUR1:VOLT:HIGH?"] = "-1.0"
        self.backend.responses[":SOUR1:VOLT:LOW?"] = "-5.0"
        for raw in ("", " \r\n", '""', '"   "'):
            with self.subTest(raw=raw):
                self.backend.responses[":SOUR1:VOLT:UNIT?"] = raw
                self.backend.events.clear()
                self.session.query_flags.clear()
                result = self.driver.read_output_parameters((1,))[0]
                self.assertEqual((result.frequency_hz, result.amplitude, result.amplitude_unit), (1.25e6, 4, "VPP"))
                self.assertEqual(result.amplitude_source, "voltage_levels")
                self.assertIsNone(result.native_amplitude_unit)
                self.assertEqual((result.voltage_high_v, result.voltage_low_v), (-1, -5))
                self.assertEqual([command for _, command in self.backend.events], [
                    ":SOUR1:VOLT:UNIT?", ":SOUR1:VOLT:HIGH?", ":SOUR1:VOLT:LOW?",
                    ":SOUR1:FREQ?", ":OUTP1?", ":SOUR1:VOLT:HIGH?", ":SOUR1:VOLT:LOW?",
                ])
                self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))
                self.assertTrue(all(not recover and depth == 1 for _, recover, depth in self.session.query_flags))
                self.assertEqual(self.opened, [self.backend])

    def test_empty_trailing_unit_discards_native_read_and_reads_a_fresh_snapshot(self):
        self.backend.responses.update({
            ":SOUR1:VOLT:UNIT?": ["DBM", ""],
            ":SOUR1:FREQ?": ["1250000", "4567000"],
            ":SOUR1:VOLT?": "-20", ":OUTP1?": ["ON", "OFF"],
            ":SOUR1:VOLT:HIGH?": "-1", ":SOUR1:VOLT:LOW?": "-5",
        })
        result = self.driver.read_output_parameters((1,))[0]
        self.assertEqual((result.frequency_hz, result.amplitude, result.output_enabled), (4567000, 4, False))
        self.assertEqual(result.amplitude_source, "voltage_levels")
        self.assertIsNone(result.native_amplitude_unit)
        commands = [command for _, command in self.backend.events]
        self.assertEqual(len(commands), 11)
        self.assertEqual(commands.count(":SOUR1:VOLT?"), 1)
        self.assertEqual(commands.count(":SOUR1:FREQ?"), 2)
        self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))

    def test_ch2_voltage_level_fallback_keeps_other_channel_and_connection_untouched(self):
        self.backend.responses.update({":SOUR2:VOLT:UNIT?": "", ":SOUR2:VOLT:HIGH?": "-1", ":SOUR2:VOLT:LOW?": "-5"})
        before = self.backend.responses.copy()
        identity, generation = self.driver.identity, self.session.connection_generation
        result = self.driver.read_output_parameters((2,))[0]
        self.assertEqual((result.channel, result.amplitude, result.amplitude_unit), (2, 4, "VPP"))
        self.assertEqual(self.backend.responses, before)
        self.assertEqual(self.driver.identity, identity)
        self.assertEqual(self.session.connection_generation, generation)
        self.assertEqual(self.opened, [self.backend])
        self.assertEqual([command for _, command in self.backend.events], [
            ":SOUR2:VOLT:UNIT?", ":SOUR2:VOLT:HIGH?", ":SOUR2:VOLT:LOW?",
            ":SOUR2:FREQ?", ":OUTP2?", ":SOUR2:VOLT:HIGH?", ":SOUR2:VOLT:LOW?",
        ])
        self.assertTrue(all(not recover and depth == 1 for _, recover, depth in self.session.query_flags))

    def test_invalid_voltage_levels_never_produce_an_amplitude_or_change_output(self):
        self.backend.responses[":SOUR1:VOLT:UNIT?"] = ""
        for high, low in (("", "0"), ("NaN", "0"), ("Inf", "0"), ("1", ""),
                          ("1", "NaN"), ("1", "-Inf"), ("-2", "-1"), ("1e308", "-1e308")):
            with self.subTest(high=high, low=low):
                self.backend.responses.update({":SOUR1:VOLT:HIGH?": high, ":SOUR1:VOLT:LOW?": low})
                self.backend.events.clear()
                with self.assertRaises(ValueError):
                    self.driver.read_output_parameters((1,))
                self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))
                self.assertFalse(any(command == ":SOUR1:VOLT?" for _, command in self.backend.events))

    def test_voltage_level_changes_reject_the_complete_group(self):
        self.backend.responses[":SOUR2:VOLT:UNIT?"] = ""
        for high, low in ((["1", "2"], "-1"), ("1", ["-1", "-2"])):
            with self.subTest(high=high, low=low):
                self.backend.responses.update({":SOUR2:VOLT:HIGH?": high, ":SOUR2:VOLT:LOW?": low})
                self.backend.events.clear()
                with self.assertRaisesRegex(ValueError, "电平.*变化"):
                    self.driver.read_output_parameters()
                self.assertEqual(len(self.backend.events), 12)
                self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))

    def test_unknown_nonempty_unit_does_not_attempt_voltage_level_fallback(self):
        for raw in ("VOLTS", "0", "ERROR", '"UNKNOWN"'):
            with self.subTest(raw=raw):
                self.backend.responses[":SOUR1:VOLT:UNIT?"] = raw
                self.backend.events.clear()
                with self.assertRaisesRegex(ValueError, "幅度单位"):
                    self.driver.read_output_parameters((1,))
                self.assertEqual(self.backend.events, [("query", ":SOUR1:VOLT:UNIT?")])

    def test_unpaired_quotes_are_invalid_units_instead_of_empty_fallback(self):
        for raw in ('"', "'", '"  ', "'  ", '\"VPP', "'VRMS\""):
            with self.subTest(raw=raw):
                self.backend.responses[":SOUR1:VOLT:UNIT?"] = raw
                self.backend.events.clear()
                with self.assertRaisesRegex(ValueError, "幅度单位"):
                    self.driver.read_output_parameters((1,))
                self.assertEqual(self.backend.events, [("query", ":SOUR1:VOLT:UNIT?")])

    def test_fallback_query_error_stops_without_reconnection_or_replay(self):
        self.backend.responses[":SOUR1:VOLT:UNIT?"] = ""
        generation = self.session.connection_generation
        for command in (":SOUR1:VOLT:UNIT?", ":SOUR1:VOLT:HIGH?", ":SOUR1:VOLT:LOW?", ":SOUR1:FREQ?", ":OUTP1?"):
            with self.subTest(command=command):
                self.backend.fail_query = command
                self.backend.events.clear()
                with self.assertRaisesRegex(RuntimeError, "READBACK_TIMEOUT"):
                    self.driver.read_output_parameters((1,))
                self.assertEqual(self.backend.events[-1], ("query", command))
                self.assertEqual(sum(query == command for _, query in self.backend.events), 1)
                self.assertEqual(self.session.connection_generation, generation)
                self.assertEqual(self.opened, [self.backend])
                self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))

    def test_fallback_rejects_a_connection_generation_change(self):
        self.backend.responses[":SOUR2:VOLT:UNIT?"] = ""
        self.backend.on_query = lambda command: self.session._advance_connection_generation() if command == ":OUTP2?" else None
        with self.assertRaisesRegex(RuntimeError, "连接已变化"):
            self.driver.read_output_parameters()

    def test_bad_nonempty_trailing_unit_rejects_native_snapshot_without_fallback(self):
        self.backend.responses[":SOUR1:VOLT:UNIT?"] = ["VRMS", "UNKNOWN"]
        with self.assertRaisesRegex(ValueError, "幅度单位"):
            self.driver.read_output_parameters((1,))
        self.assertEqual(len(self.backend.events), 5)
        self.assertEqual(self.backend.events[-1], ("query", ":SOUR1:VOLT:UNIT?"))
        self.assertFalse(any(":HIGH?" in command or ":LOW?" in command for _, command in self.backend.events))

    def test_second_voltage_pair_timeout_rejects_group_without_reconnect(self):
        self.backend.responses[":SOUR1:VOLT:UNIT?"] = ""
        generation = self.session.connection_generation
        for target in (":SOUR1:VOLT:HIGH?", ":SOUR1:VOLT:LOW?"):
            with self.subTest(target=target):
                counts = {}

                def fail_second(command):
                    counts[command] = counts.get(command, 0) + 1
                    if command == target and counts[command] == 2:
                        raise OSError("SECOND_PAIR_TIMEOUT")

                self.backend.events.clear()
                self.backend.on_query = fail_second
                with self.assertRaisesRegex(RuntimeError, "SECOND_PAIR_TIMEOUT"):
                    self.driver.read_output_parameters((1,))
                self.assertEqual(self.backend.events[-1], ("query", target))
                self.assertEqual(counts[target], 2)
                self.assertTrue(all(kind == "query" for kind, _ in self.backend.events))
                self.assertEqual(self.session.connection_generation, generation)
                self.assertEqual(self.opened, [self.backend])
        self.backend.on_query = None

    def test_equal_voltage_levels_are_zero_vpp_without_fabricating_native_unit(self):
        self.backend.responses.update({":SOUR1:VOLT:UNIT?": "", ":SOUR1:VOLT:HIGH?": "-2", ":SOUR1:VOLT:LOW?": "-2"})
        result = self.driver.read_output_parameters((1,))[0]
        self.assertEqual(result.amplitude, 0)
        self.assertEqual(result.amplitude_unit, "VPP")
        self.assertIsNone(result.native_amplitude_unit)

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
        for command, raw in ((":SOUR1:VOLT:UNIT?", "VOLTS"),
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
        self._assert_readback_serializes_with_writer(fallback=False)

    def test_voltage_level_readback_serializes_with_prepared_writer(self):
        self._assert_readback_serializes_with_writer(fallback=True)

    def _assert_readback_serializes_with_writer(self, *, fallback):
        self.backend.responses[":COUP:STAT?"] = "FREQ:OFF,PHASE:OFF,AMPL:OFF"
        prepared = self.driver.prepare_trigger()
        if fallback:
            self.backend.responses[":SOUR1:VOLT:UNIT?"] = ""
        pause_command = ":SOUR1:VOLT:HIGH?" if fallback else ":SOUR1:VOLT:UNIT?"
        self.backend.events.clear()
        reading, release, writer_requested, write_done = (threading.Event() for _ in range(4))
        errors = []

        def paused(command):
            if command == pause_command and not reading.is_set():
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
        self.assertEqual(len(self.backend.events), 13 if fallback else 11)
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

    def test_virtual_voltage_level_fallback_ignores_native_rms_and_load(self):
        self.generator.channels[2].update(function="SQU", amplitude_vpp=2.0, offset_v=-3.0, load_ohms=None)
        self.generator.amplitude_units[2] = "VRMS"
        before = self.generator.snapshot().channels
        self.assertEqual(float(self.driver.session.query(":SOUR2:VOLT:HIGH?")), -2)
        self.assertEqual(float(self.driver.session.query(":SOURce2:VOLTage:LEVel:IMMediate:LOW?")), -4)
        self.assertEqual(float(self.driver.session.query(":SOURce2:VOLTage:LEVel:IMMediate:HIGH?")), -2)
        self.assertEqual(float(self.driver.session.query(":SOUR2:VOLT:LOW?")), -4)
        original = self.driver.session.query
        self.generator.commands.clear()
        with patch.object(self.driver.session, "query", side_effect=lambda command, **kwargs:
                          "" if command == ":SOUR2:VOLT:UNIT?" else original(command, **kwargs)):
            result = self.driver.read_output_parameters((2,))[0]
        self.assertEqual((result.amplitude, result.amplitude_unit, result.amplitude_source), (2, "VPP", "voltage_levels"))
        self.assertIsNone(result.native_amplitude_unit)
        self.assertEqual(self.generator.snapshot().channels, before)
        self.assertEqual(self.generator.amplitude_units[2], "VRMS")
        self.assertTrue(all(command.startswith((":SOUR2:", ":OUTP2?")) and command.endswith("?")
                            for command in self.generator.commands))


if __name__ == "__main__":
    unittest.main()
