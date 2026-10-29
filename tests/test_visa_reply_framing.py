from collections import deque
import unittest
from unittest.mock import Mock, patch

from rigol_control.drivers.dg4000 import DG4000Driver
from rigol_control.drivers.scpi import (
    ScpiError, ScpiResponseSyncError, ScpiSession, VisaBackend,
)


class FramedInstrument:
    def __init__(self, replies=None, *, trailing_blank=False, clock=None, read_delays=()):
        self.replies = replies or {}
        self.trailing_blank = trailing_blank
        self.clock = clock
        self.read_delays = deque(read_delays)
        self.frames = deque()
        self.timeout = 5000
        self.events = []
        self.closed = False

    def write(self, command):
        self.events.append(("write", command))
        replies = self.replies.get(command, ())
        self.frames.extend(replies if isinstance(replies, (list, tuple)) else [replies])
        if self.trailing_blank:
            self.frames.append("")

    def query(self, command):
        self.write(command)
        return self.read()

    def read(self):
        self.events.append(("read", self.timeout))
        if self.clock is not None and self.read_delays:
            self.clock[0] += self.read_delays.popleft()
        if not self.frames:
            raise TimeoutError("SIMULATED_PENDING_REPLY_TIMEOUT")
        response = self.frames.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class VisaReplyFramingTests(unittest.TestCase):
    def backend(self, instrument, timeout_ms=5000):
        manager = Mock()
        manager.open_resource.return_value = instrument
        with patch("pyvisa.ResourceManager", return_value=manager):
            backend = VisaBackend("USB::SIMULATED", timeout_ms)
        return backend

    @staticmethod
    def dg_replies():
        return {
            "*IDN?": "RIGOL,DG4162,SIMULATED,1.0",
            ":SOUR1:VOLT:UNIT?": "VPP", ":SOUR1:FREQ?": "1000",
            ":SOUR1:VOLT?": "0.125", ":OUTP1?": "0",
            ":SOUR1:VOLT:HIGH?": "0.0625", ":SOUR1:VOLT:LOW?": "-0.0625",
        }

    def test_normal_scalar_and_zero_reply_need_only_one_query_read(self):
        for value in ("VPP", "0", "0.0"):
            with self.subTest(value=value):
                instrument = FramedInstrument({":SOUR1:VOLT:UNIT?": value})
                backend = self.backend(instrument)
                self.assertEqual(backend.query(":SOUR1:VOLT:UNIT?"), value)
                self.assertEqual(instrument.events, [("write", ":SOUR1:VOLT:UNIT?"), ("read", 5000)])

    def test_fifo_regression_reproduces_published_high_query_receiving_prior_unit(self):
        instrument = FramedInstrument(self.dg_replies(), trailing_blank=True)
        backend = self.backend(instrument)
        # This is the published pre-fix VisaBackend.query body. The FIFO itself
        # does not guess which command a read belongs to.
        with patch.object(VisaBackend, "query", lambda item, command: str(item._instrument.query(command))):
            session = ScpiSession("USB::SIMULATED", backend_factory=lambda *_: backend)
            session.connect()
            with self.assertRaisesRegex(ValueError, r"VOLT:HIGH.*VPP"):
                DG4000Driver(session).read_output_parameters(channels=(1,))
        self.assertEqual([event[1] for event in instrument.events if event[0] == "write"],
                         ["*IDN?", ":SOUR1:VOLT:UNIT?", ":SOUR1:VOLT:HIGH?"])

    def test_trailing_empty_frames_stay_with_same_query_and_do_not_shift_dg_fields(self):
        instrument = FramedInstrument(self.dg_replies(), trailing_blank=True)
        backend = self.backend(instrument)
        session = ScpiSession("USB::SIMULATED", backend_factory=lambda *_: backend)
        session.connect()
        reading, = DG4000Driver(session).read_output_parameters(channels=(1,))
        self.assertEqual((reading.frequency_hz, reading.amplitude, reading.amplitude_unit), (1000, .125, "VPP"))
        self.assertEqual(reading.amplitude_source, "native")
        self.assertFalse(reading.output_enabled)
        self.assertEqual([event[1] for event in instrument.events if event[0] == "write"],
                         ["*IDN?", ":SOUR1:VOLT:UNIT?", ":SOUR1:FREQ?", ":SOUR1:VOLT?",
                          ":OUTP1?", ":SOUR1:VOLT:UNIT?"])
        self.assertEqual(instrument.timeout, 5000)

    def test_initial_empty_identity_is_continued_without_reissuing_idn(self):
        instrument = FramedInstrument({"*IDN?": ["\r\n", "RIGOL,DG4162,SIMULATED,1.0"]})
        backend = self.backend(instrument)
        self.assertTrue(backend.query("*IDN?").startswith("RIGOL,"))
        self.assertEqual(sum(event == ("write", "*IDN?") for event in instrument.events), 1)
        self.assertEqual(sum(event[0] == "read" for event in instrument.events), 2)

    def test_whitelist_supports_actual_short_headers_and_standard_long_headers(self):
        for command in ("*IDN?", "*OPC?", ":COUP:STAT?", ":COUPLING:STATE?", ":SOUR1:FUNC?",
                        ":SOURCE2:FUNCTION?", ":OUTP1?", ":OUTPUT2:STATE?", ":SOUR2:FREQ?",
                        ":SOURCE1:FREQUENCY?", ":SOURCE1:VOLTAGE:UNIT?",
                        ":SOURCE1:VOLTAGE:LEVEL:IMMEDIATE:AMPLITUDE?",
                        ":SOURCE2:VOLTAGE:LEVEL:IMMEDIATE:HIGH?", ":SOUR1:VOLT:LOW?"):
            with self.subTest(command=command):
                instrument = FramedInstrument({command: [" \t\r", "0"]})
                backend = self.backend(instrument)
                self.assertEqual(backend.query(command), "0")
                self.assertEqual(sum(event[0] == "write" for event in instrument.events), 1)
                self.assertEqual(sum(event[0] == "read" for event in instrument.events), 2)

    def test_unknown_queries_and_quoted_empty_strings_keep_original_semantics(self):
        for command, response in (("*OPT?", ""), (":DISPlay:TEXT?", ""),
                                  (":DATA?", "\r\n"), (":SOUR1:VOLT:UNIT?", '""')):
            with self.subTest(command=command, response=response):
                instrument = FramedInstrument({command: response})
                backend = self.backend(instrument)
                self.assertEqual(backend.query(command), response)
                self.assertEqual(sum(event[0] == "read" for event in instrument.events), 1)
                self.assertFalse(backend._reply_unsynchronized)

    def test_nonempty_wrong_type_is_not_skipped_in_search_of_numeric_reply(self):
        instrument = FramedInstrument({":SOUR1:VOLT:HIGH?": ["VPP", "1.25"]})
        backend = self.backend(instrument)
        self.assertEqual(backend.query(":SOUR1:VOLT:HIGH?"), "VPP")
        self.assertEqual(list(instrument.frames), ["1.25"])
        self.assertEqual(sum(event[0] == "read" for event in instrument.events), 1)

    def test_empty_frame_limit_is_bounded_and_subsequent_query_has_zero_io(self):
        instrument = FramedInstrument({"*OPC?": [""] * VisaBackend.MAX_EMPTY_RESPONSE_FRAMES})
        backend = self.backend(instrument)
        with self.assertRaisesRegex(ScpiResponseSyncError, "连续收到空回复帧"):
            backend.query("*OPC?")
        self.assertEqual(sum(event[0] == "read" for event in instrument.events), VisaBackend.MAX_EMPTY_RESPONSE_FRAMES)
        before = list(instrument.events)
        with self.assertRaisesRegex(ScpiResponseSyncError, "显式断开"):
            backend.query(":SOUR1:FREQ?")
        self.assertEqual(instrument.events, before)
        backend.write(":OUTP1 OFF")
        self.assertEqual(instrument.events[-1], ("write", ":OUTP1 OFF"))
        self.assertEqual(instrument.timeout, 5000)

    def test_deadline_includes_first_read_and_restores_original_timeout(self):
        clock = [0.0]
        instrument = FramedInstrument({"*OPC?": ["", "", "1"]}, clock=clock, read_delays=(.004, .005, 0))
        backend = self.backend(instrument, 10)
        instrument.timeout = 777
        with patch("rigol_control.drivers.scpi.time.monotonic", side_effect=lambda: clock[0]):
            self.assertEqual(backend.query("*OPC?"), "1")
        timeouts = [event[1] for event in instrument.events if event[0] == "read"]
        self.assertEqual(timeouts[0], 10)
        self.assertLessEqual(timeouts[1], 6)
        self.assertLessEqual(timeouts[2], 1)
        self.assertEqual(instrument.timeout, 777)

    def test_expired_total_budget_does_not_start_another_read(self):
        clock = [0.0]
        instrument = FramedInstrument({"*IDN?": ["", "RIGOL"]}, clock=clock, read_delays=(.010,))
        backend = self.backend(instrument, 10)
        with patch("rigol_control.drivers.scpi.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(ScpiResponseSyncError, "等待有效回复超时"):
                backend.query("*IDN?")
        self.assertEqual(sum(event[0] == "read" for event in instrument.events), 1)
        self.assertEqual(instrument.timeout, 10)

    def test_followup_read_error_does_not_replay_or_replace_its_cause(self):
        error = TimeoutError("SIMULATED_FOLLOWUP_TIMEOUT")
        instrument = FramedInstrument({"*OPC?": ["", error]})
        backend = self.backend(instrument)
        with self.assertRaises(ScpiResponseSyncError) as raised:
            backend.query("*OPC?")
        self.assertIs(raised.exception.__cause__, error)
        self.assertEqual(sum(event[0] == "write" for event in instrument.events), 1)
        self.assertEqual(instrument.timeout, 5000)

    def test_first_real_query_error_keeps_original_exception_and_marks_pending(self):
        error = OSError("SIMULATED_FIRST_QUERY_ERROR")
        instrument = FramedInstrument({"*IDN?": error})
        backend = self.backend(instrument)
        with self.assertRaises(OSError) as raised:
            backend.query("*IDN?")
        self.assertIs(raised.exception, error)
        self.assertTrue(backend._reply_unsynchronized)
        self.assertEqual(instrument.timeout, 5000)

    def test_first_io_failure_preserves_existing_explicit_opt_in_recovery(self):
        first_instrument = FramedInstrument({"*IDN?": "RIGOL,DG4162,ONE,1.0",
                                             ":SOUR1:FREQ?": OSError("SIMULATED_TRANSIENT_IO")})
        second_instrument = FramedInstrument({"*IDN?": "RIGOL,DG4162,TWO,1.0", ":SOUR1:FREQ?": "2000"})
        first, second = self.backend(first_instrument), self.backend(second_instrument)
        factory = Mock(side_effect=[first, second])
        session = ScpiSession("USB::SIMULATED", backend_factory=factory)
        session.connect()
        self.assertEqual(session.query(":SOUR1:FREQ?", recover=True), "2000")
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first_instrument.closed)
        self.assertEqual(sum(event == ("write", ":SOUR1:FREQ?") for event in first_instrument.events), 1)

    def test_nonfinite_existing_resource_timeout_cannot_make_continuation_unbounded(self):
        for original in (None, float("inf")):
            with self.subTest(original=original):
                instrument = FramedInstrument({"*OPC?": ["", "1"]})
                backend = self.backend(instrument, 10)
                instrument.timeout = original
                self.assertEqual(backend.query("*OPC?"), "1")
                self.assertTrue(all(1 <= event[1] <= 10 for event in instrument.events if event[0] == "read"))
                self.assertEqual(instrument.timeout, original)

    def test_optional_session_marker_is_noop_for_legacy_backend(self):
        backend = Mock(spec=["query", "write", "close"])
        backend.query.return_value = "RIGOL,DG4162,SIMULATED,1.0"
        session = ScpiSession("USB::SIMULATED", backend_factory=lambda *_: backend)
        session.connect()
        backend.reset_mock()
        generation = session.connection_generation
        session.mark_response_unsynchronized()
        self.assertEqual(backend.mock_calls, [])
        self.assertEqual(session.connection_generation, generation)

    def test_tainted_session_query_never_auto_recovers_even_if_recover_requested(self):
        instrument = FramedInstrument({"*IDN?": "RIGOL,DG4162,SIMULATED,1.0"})
        backend = self.backend(instrument)
        factory = Mock(return_value=backend)
        session = ScpiSession("USB::SIMULATED", backend_factory=factory)
        session.connect()
        session.mark_response_unsynchronized()
        before = list(instrument.events)
        with self.assertRaises(ScpiResponseSyncError):
            session.query("*IDN?", recover=True)
        self.assertEqual(instrument.events, before)
        self.assertEqual(factory.call_count, 1)
        self.assertFalse(instrument.closed)

    def test_explicit_disconnect_and_connect_replace_unsynchronized_backend(self):
        first = self.backend(FramedInstrument({"*IDN?": "RIGOL,DG4162,ONE,1.0"}))
        second = self.backend(FramedInstrument({"*IDN?": "RIGOL,DG4162,TWO,1.0"}))
        factory = Mock(side_effect=[first, second])
        session = ScpiSession("USB::SIMULATED", backend_factory=factory)
        session.connect()
        session.mark_response_unsynchronized()
        session.disconnect()
        self.assertIn("TWO", session.connect())
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first._instrument.closed)

    def test_invalid_nonfinite_timeout_is_rejected_before_opening_visa(self):
        for timeout in (None, float("inf"), float("nan"), 0, -1):
            with self.subTest(timeout=timeout), patch("pyvisa.ResourceManager") as manager:
                with self.assertRaises(ScpiError):
                    VisaBackend("USB::SIMULATED", timeout)
                manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
