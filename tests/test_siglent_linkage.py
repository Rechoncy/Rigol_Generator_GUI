import unittest

from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.drivers.scpi import ScpiError, ScpiSession
from rigol_control.drivers.siglent_ssg import VirtualSsgBackend, capabilities
from rigol_control.linkage import LinkageAxis, MultiDeviceTriggerController, generate_linkage_points
from rigol_control.linkage_parameters import LinkageParameter, RfPowerStepValue
from rigol_control.scan import ScanMode
from rigol_control.siglent_linkage import SsgNativeLinkageParticipant, SsgSoftwareLinkageWriter
from test_linkage import FakeCounter, RecordingWriter, wait_until


def frequency(value, channel=1, unit="Hz"):
    return StepValue(StepParameter.FREQUENCY, channel, value, value, unit)


def power(value, channel=1, unit="dBm"):
    return RfPowerStepValue(LinkageParameter.RF_POWER, channel, value, value, unit)


class Instrument(VirtualSsgBackend):
    """Reject invalid intermediate CW settings and model lost accepted replies."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_write = None
        self.query_override = None

    def write(self, command):
        super().write(command)
        head = command.partition(" ")[0].upper()
        if head in {":FREQUENCY", ":POWER"}:
            current_frequency = float(self.get("FREQuency"))
            current_power = float(self.get("POWer"))
            low, high = capabilities(self.identity).power_bounds(current_frequency)
            if not low <= current_power <= high:
                self.esr |= 16
        if self.fail_write is not None and self.fail_write(command):
            raise TimeoutError("ACCEPTED_SETTING_REPLY_LOST")

    def query(self, command):
        if self.query_override is not None:
            replacement = self.query_override(command)
            if replacement is not None:
                self.commands.append(command)
                return replacement
        return super().query(command)


class SiglentLinkageTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.sessions = []
        self.session, self.backend = self.instrument("SSG3021X")
        self.points = ((frequency(1.2e9), power(-10)),
                       (frequency(1.3e9), power(-11)))

    def tearDown(self):
        for session in self.sessions:
            session.disconnect()

    def instrument(self, model, resource=None):
        backend = Instrument(identity=f"SIGLENT,{model},LINKAGE-TEST,1.0", clock=lambda: self.now)
        session = ScpiSession(resource or f"SIM::SIGLENT::{model}",
                              backend_factory=lambda *_: backend)
        session.connect()
        self.sessions.append(session)
        backend.commands.clear()
        return session, backend

    def writes(self, backend=None):
        return [command for command in (backend or self.backend).commands if "?" not in command]

    def cw_writes(self, backend=None):
        return [command for command in self.writes(backend)
                if command.partition(" ")[0].upper() in {":FREQUENCY", ":POWER", ":OUTPUT"}]

    def projected(self, mode):
        axes = (LinkageAxis("ssg", StepParameter.FREQUENCY, 1, "MHz", 1200, 100, 2),
                LinkageAxis("ssg", LinkageParameter.RF_POWER, 1, "dBm", -10, -1, 2))
        return tuple(point.grouped()["ssg"] for point in generate_linkage_points(axes, mode))

    def test_software_prepare_reads_context_and_full_table_without_any_output_write(self):
        self.backend.put("OUTPut", "1")
        self.backend.put("OUTPut:MODulation", "1")
        self.backend.put("AM:STATe", "1")
        writer = SsgSoftwareLinkageWriter(self.session, self.points)
        self.assertIs(writer.prepare_trigger(), writer)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.backend.get("OUTPut"), "1")
        self.assertEqual(self.backend.get("OUTPut:MODulation"), "1")
        self.assertEqual(self.backend.get("AM:STATe"), "1")

    def test_software_synchronous_and_cartesian_points_apply_in_projected_order(self):
        for mode in (ScanMode.SYNCHRONOUS, ScanMode.CARTESIAN):
            with self.subTest(mode=mode):
                projected = self.projected(mode)
                writer = SsgSoftwareLinkageWriter(self.session, projected)
                writer.prepare_trigger()
                for values in projected:
                    writer.apply_steps(values)
                    self.assertEqual(float(self.backend.get("FREQuency")), values[0].value)
                    self.assertEqual(float(self.backend.get("POWer")), values[1].value)
                self.assertEqual(self.backend.get("OUTPut"), "0")

    def test_software_controller_first_edge_is_p0_then_last_wraps_without_native_scan(self):
        axes = (LinkageAxis("ssg", StepParameter.FREQUENCY, 1, "GHz", 1.2, .1, 2),)
        points = generate_linkage_points(axes, ScanMode.SYNCHRONOUS)
        writer = SsgSoftwareLinkageWriter(self.session, tuple(point.grouped()["ssg"] for point in points))
        counter = FakeCounter()
        controller = MultiDeviceTriggerController(counter, {"ssg": writer}, points,
                                                  monitor_poll_seconds=.001)
        controller.start()
        try:
            self.assertEqual(self.writes(), [])
            counter.add(3)
            self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 3))
            configured = [float(command.split()[1]) for command in self.writes()
                          if command.startswith(":FREQuency ")]
            self.assertEqual(configured, [1.2e9, 1.3e9, 1.2e9])
            self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
        finally:
            controller.stop()

    def test_unknown_model_is_rejected_before_instrument_configuration(self):
        session, backend = self.instrument("SSG9999X")
        for cls in (SsgSoftwareLinkageWriter, SsgNativeLinkageParticipant):
            with self.subTest(cls=cls), self.assertRaisesRegex(ValueError, "未识别"):
                cls(session, self.points)
        self.assertEqual(self.writes(backend), [])

    def test_wrong_channel_parameter_and_units_are_rejected_without_writes(self):
        invalid = (frequency(1e9, channel=2), power(-10, channel=2), power(-10, unit="mVpp"),
                   power(-10, unit=None), frequency(1e9, unit="mVpp"),
                   StepValue(StepParameter.AMPLITUDE, 1, .1, 100, "mVpp"),
                   StepValue(StepParameter.PHASE, 1, 10), frequency(float("nan")), power(float("inf")))
        for value in invalid:
            for cls in (SsgSoftwareLinkageWriter, SsgNativeLinkageParticipant):
                with self.subTest(value=value, cls=cls), self.assertRaises(ValueError):
                    cls(self.session, ((value,), (value,)))
        self.assertEqual(self.writes(), [])

    def test_mixed_point_targets_or_duplicates_are_rejected(self):
        for points in (((frequency(1e9),), (power(-10),)),
                       ((frequency(1e9), frequency(1.1e9)),) * 2):
            with self.subTest(points=points), self.assertRaises(ValueError):
                SsgSoftwareLinkageWriter(self.session, points)

    def test_full_table_is_checked_before_first_write_and_bad_prepare_disables_writer(self):
        writer = SsgSoftwareLinkageWriter(self.session,
                                         ((frequency(1.2e9), power(-10)), (frequency(1.3e9), power(100))))
        with self.assertRaises(ValueError):
            writer.prepare_trigger()
        with self.assertRaisesRegex(ScpiError, "预检"):
            writer.apply_steps(self.points[0])
        self.assertEqual(self.writes(), [])

    def test_frequency_first_transition_does_not_pass_illegal_power_in_old_band(self):
        session, backend = self.instrument("SSG6085A")
        backend.put("FREQuency", "6000000000")
        backend.put("POWer", "10")
        point = (frequency(7e9), power(25))
        writer = SsgSoftwareLinkageWriter(session, (point,))
        writer.prepare_trigger()
        writer.apply_steps(point)
        self.assertEqual(self.cw_writes(backend), [":FREQuency 7000000000", ":POWer 25"])

    def test_power_first_transition_does_not_enter_new_band_at_illegal_old_power(self):
        session, backend = self.instrument("SSG6085A")
        backend.put("FREQuency", "6000000000")
        backend.put("POWer", "-130")
        point = (frequency(7e9), power(10))
        writer = SsgSoftwareLinkageWriter(session, (point,))
        writer.prepare_trigger()
        writer.apply_steps(point)
        self.assertEqual(self.cw_writes(backend), [":POWer 10", ":FREQuency 7000000000"])

    def test_no_safe_intermediate_transition_is_rejected_before_setting(self):
        session, backend = self.instrument("SSG6085A")
        backend.put("FREQuency", "6000000000")
        backend.put("POWer", "-130")
        writer = SsgSoftwareLinkageWriter(session, ((frequency(7e9), power(25)),))
        with self.assertRaisesRegex(ValueError, "过渡点"):
            writer.prepare_trigger()
        self.assertEqual(self.writes(backend), [])

    def test_last_to_p0_transition_is_checked_before_any_setting(self):
        session, backend = self.instrument("SSG6085A")
        backend.put("FREQuency", "7000000000")
        backend.put("POWer", "10")
        points = ((frequency(7e9), power(25)), (frequency(7e9), power(10)),
                  (frequency(6e9), power(-130)))
        writer = SsgSoftwareLinkageWriter(session, points)
        with self.assertRaisesRegex(ValueError, "过渡点"):
            writer.prepare_trigger()
        self.assertEqual(self.writes(backend), [])

    def test_scan_and_closed_loop_block_both_paths_without_output_settings(self):
        for key, value in (("SWEep:STATe", "FREQUENCY"), ("POWer:SPC:STATe", "1")):
            for cls in (SsgSoftwareLinkageWriter, SsgNativeLinkageParticipant):
                with self.subTest(key=key, cls=cls):
                    self.backend.put(key, value)
                    item = cls(self.session, self.points)
                    with self.assertRaises(ValueError):
                        item.prepare_trigger() if cls is SsgSoftwareLinkageWriter else item.prepare()
                    self.backend.put(key, "OFF" if key == "SWEep:STATe" else "0")
        self.assertEqual(self.writes(), [])

    def test_iq_low_frequency_is_blocked_in_both_paths(self):
        session, backend = self.instrument("SSG6082A-V")
        backend.put("FUNCtion:DM:STATe", "1")
        points = ((frequency(9000),), (frequency(10000),))
        for cls in (SsgSoftwareLinkageWriter, SsgNativeLinkageParticipant):
            with self.subTest(cls=cls):
                item = cls(session, points)
                with self.assertRaisesRegex(ValueError, "IQ"):
                    item.prepare_trigger() if cls is SsgSoftwareLinkageWriter else item.prepare()
        self.assertEqual(self.writes(backend), [])

    def test_per_point_preflight_rechecks_front_panel_context_and_invalidates_writer(self):
        writer = SsgSoftwareLinkageWriter(self.session, self.points)
        writer.prepare_trigger()
        self.backend.put("POWer:SPC:STATe", "1")
        with self.assertRaises(ValueError):
            writer.apply_steps(self.points[0])
        self.backend.put("POWer:SPC:STATe", "0")
        with self.assertRaisesRegex(ScpiError, "预检"):
            writer.apply_steps(self.points[0])
        self.assertEqual(self.writes(), [])

    def test_accepted_software_write_failure_stops_batch_and_is_not_replayed(self):
        writer = SsgSoftwareLinkageWriter(self.session, self.points)
        writer.prepare_trigger()
        self.backend.fail_write = lambda command: command.startswith(":FREQuency ")
        with self.assertRaisesRegex(ScpiError, "ACCEPTED_SETTING_REPLY_LOST"):
            writer.apply_steps(self.points[0])
        self.assertEqual(float(self.backend.get("FREQuency")), 1.2e9)
        before = list(self.backend.commands)
        with self.assertRaisesRegex(ScpiError, "结果未知"):
            writer.apply_steps(self.points[0])
        self.assertEqual(self.backend.commands, before)
        self.assertEqual(self.cw_writes(), [":FREQuency 1200000000"])

    def test_bad_readback_or_esr_invalidates_software_writer(self):
        for fail_readback in (False, True):
            with self.subTest(fail_readback=fail_readback):
                writer = SsgSoftwareLinkageWriter(self.session, self.points)
                writer.prepare_trigger()
                if fail_readback:
                    self.backend.query_override = lambda command: "123" if command == ":FREQuency?" else None
                else:
                    self.backend.esr = 32
                with self.assertRaises((ScpiError, ValueError)):
                    writer.apply_steps(self.points[0])
                before = list(self.backend.commands)
                with self.assertRaises(ScpiError):
                    writer.apply_steps(self.points[0])
                self.assertEqual(self.backend.commands, before)
                self.backend.query_override = None

    def test_connection_generation_change_invalidates_both_existing_bindings(self):
        software = SsgSoftwareLinkageWriter(self.session, self.points)
        native = SsgNativeLinkageParticipant(self.session, self.points)
        software.prepare_trigger()
        native.prepare()
        self.session.disconnect()
        self.session.connect()
        self.backend.commands.clear()
        for action in (lambda: software.apply_steps(self.points[0]), software.prepare_trigger, native.arm):
            with self.subTest(action=action), self.assertRaisesRegex(ScpiError, "重建"):
                action()
        self.assertEqual(self.backend.commands, [])

    def test_native_projects_full_synchronous_and_cartesian_lists_and_preserves_output(self):
        for mode in (ScanMode.SYNCHRONOUS, ScanMode.CARTESIAN):
            with self.subTest(mode=mode):
                projected = self.projected(mode)
                native = SsgNativeLinkageParticipant(self.session, projected, rising=False, dwell_s=.05)
                self.backend.commands.clear()
                native.prepare()
                self.assertEqual([(point.frequency_hz, point.power_dbm, point.dwell_s)
                                  for point in self.backend.table],
                                 [(values[0].value, values[1].value, .05) for values in projected])
                self.assertEqual(self.backend.get("SWEep:SWEep:TRIGger:TYPE"), "AUTO")
                self.assertEqual(self.backend.get("SWEep:POINt:TRIGger:TYPE"), "EXT")
                self.assertEqual(self.backend.get("INPut:TRIGger:SLOPe"), "NEGATIVE")
                self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
                self.assertEqual(self.cw_writes(), [])
                self.assertFalse(native.requires_stop)

    def test_native_fixed_other_axis_is_read_from_instrument(self):
        for points, expected_target in ((((frequency(1.2e9),), (frequency(1.3e9),)), "FREQUENCY"),
                                        (((power(-10),), (power(-11),)), "LEVEL")):
            with self.subTest(target=expected_target):
                native = SsgNativeLinkageParticipant(self.session, points)
                native.prepare()
                self.assertEqual(native._config.target, expected_target)
                for point in self.backend.table:
                    self.assertEqual(point.power_dbm if expected_target == "FREQUENCY" else point.frequency_hz,
                                     -20 if expected_target == "FREQUENCY" else 1e9)

    def test_native_point_and_dwell_limits_are_rejected_before_writes(self):
        for points, dwell in (((self.points[0],), .01), (self.points * 251, .01),
                              (self.points, .001), (self.points, float("nan"))):
            with self.subTest(count=len(points), dwell=dwell), self.assertRaises(ValueError):
                SsgNativeLinkageParticipant(self.session, points, dwell_s=dwell)
        self.assertEqual(self.writes(), [])

    def test_native_arm_waits_for_simulated_ext_and_never_sends_bus_trigger(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.backend.index, -1)
        self.assertEqual(float(self.backend.get("FREQuency")), 1e9)
        native.emit_virtual_trigger()
        self.assertEqual((self.backend.index, float(self.backend.get("FREQuency"))), (0, 1.2e9))
        self.now += .02
        native.emit_virtual_trigger()
        self.assertEqual(self.backend.index, 1)
        self.now += .02
        native.emit_virtual_trigger()
        self.assertEqual(self.backend.index, 0)
        self.assertNotIn("*TRG", self.writes())
        native.stop()
        self.assertFalse(native.requires_stop)
        self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")

    def test_native_failed_second_prepare_cannot_arm_stale_successful_config(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        self.backend.put("POWer:SPC:STATe", "1")
        with self.assertRaises(ValueError):
            native.prepare()
        self.backend.put("POWer:SPC:STATe", "0")
        before = list(self.backend.commands)
        with self.assertRaisesRegex(ScpiError, "上传"):
            native.arm()
        self.assertEqual(self.backend.commands, before)

    def test_native_upload_acceptance_failure_never_arms_or_replays(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        self.backend.fail_write = lambda command: command.startswith(":SWEep:LIST:CHANGe ")
        with self.assertRaises(ScpiError):
            native.prepare()
        self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
        before = list(self.backend.commands)
        with self.assertRaises(ScpiError):
            native.arm()
        self.assertEqual(self.backend.commands, before)

    def test_native_accepted_arm_failure_keeps_stop_required_without_arm_replay(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        self.backend.fail_write = lambda command: (command.upper().startswith(":SWEEP:STATE ")
                                                  and not command.upper().endswith(" OFF"))
        with self.assertRaises(ScpiError):
            native.arm()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.backend.get("SWEep:STATe"), "LEV_FREQ")
        before = list(self.backend.commands)
        with self.assertRaises(ScpiError):
            native.arm()
        self.assertEqual(self.backend.commands, before)
        self.backend.fail_write = None
        native.stop()
        self.assertFalse(native.requires_stop)

    def test_native_accepted_stop_failure_keeps_flag_until_explicit_off_retry(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.backend.fail_write = lambda command: command.upper() == ":SWEEP:STATE OFF"
        with self.assertRaises(ScpiError):
            native.stop()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
        self.assertEqual(self.writes().count(":SWEep:STATe OFF"), 1)
        self.backend.fail_write = None
        native.stop()
        self.assertFalse(native.requires_stop)
        self.assertEqual(self.writes().count(":SWEep:STATe OFF"), 2)

    def test_native_incorrect_off_readback_keeps_stop_required(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.backend.query_override = lambda command: "FREQUENCY" if command == ":SWEep:STATe?" else None
        with self.assertRaises(ScpiError):
            native.stop()
        self.assertTrue(native.requires_stop)
        self.backend.query_override = None
        native.stop()
        self.assertFalse(native.requires_stop)

    def test_native_virtual_input_rejects_real_resource_before_any_io(self):
        session, backend = self.instrument("SSG3021X", resource="USB::FAKE-REAL-RESOURCE")
        native = SsgNativeLinkageParticipant(session, self.points)
        with self.assertRaisesRegex(ValueError, "模拟器"):
            native.emit_virtual_trigger()
        self.assertEqual(backend.commands, [])

    def test_explicit_native_recovery_reconnects_same_identity_and_only_confirms_off(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        old_generation = self.session.connection_generation
        self.backend.commands.clear()
        native.recover_and_stop()
        self.assertGreater(self.session.connection_generation, old_generation)
        self.assertFalse(native.requires_stop)
        self.assertEqual(self.writes(), [":SWEep:STATe OFF"])
        self.assertIsNone(native._config)
        before = list(self.backend.commands)
        for action in (native.prepare, native.arm):
            with self.assertRaises(ScpiError):
                action()
        self.assertEqual(self.backend.commands, before)

    def test_recovery_identity_mismatch_never_sends_off_or_old_settings(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.backend.identity = "SIGLENT,SSG3021X,REPLACED-INSTRUMENT,1.0"
        self.backend.commands.clear()
        with self.assertRaisesRegex(ScpiError, "身份"):
            native.recover_and_stop()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.backend.commands, ["*IDN?"])

    def test_recovery_changed_resource_is_rejected_before_io(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.session.resource = "SIM::SIGLENT::OTHER-RESOURCE"
        self.backend.commands.clear()
        with self.assertRaisesRegex(ScpiError, "资源"):
            native.recover_and_stop()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.backend.commands, [])

    def test_recovery_without_original_serial_is_rejected_before_io(self):
        session, backend = self.instrument("SSG3021X")
        session.disconnect()
        backend.identity = "SIGLENT,SSG3021X,,1.0"
        session.connect()
        native = SsgNativeLinkageParticipant(session, self.points)
        native.prepare()
        native.arm()
        backend.commands.clear()
        with self.assertRaisesRegex(ScpiError, "序列号"):
            native.recover_and_stop()
        self.assertTrue(native.requires_stop)
        self.assertEqual(backend.commands, [])

    def test_recovery_off_failure_still_requires_stop_and_never_reinstates_list(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        self.backend.commands.clear()
        self.backend.fail_write = lambda command: command.upper() == ":SWEEP:STATE OFF"
        with self.assertRaises(ScpiError):
            native.recover_and_stop()
        self.assertTrue(native.requires_stop)
        self.assertEqual(self.writes(), [":SWEep:STATe OFF"])
        self.assertIsNone(native._config)
        self.backend.fail_write = None
        native.recover_and_stop()
        self.assertFalse(native.requires_stop)
        self.assertEqual(self.writes(), [":SWEep:STATe OFF"] * 2)

    def test_recovery_off_reply_during_generation_change_does_not_clear_pending_stop(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        native.arm()
        original_query = self.backend.query
        def query(command):
            result = original_query(command)
            if command == ":SWEep:STATe?":
                self.session._advance_connection_generation()
            return result
        self.backend.query = query
        with self.assertRaisesRegex(ScpiError, "重建"):
            native.recover_and_stop()
        self.assertTrue(native.requires_stop)

    def test_driver_start_probe_false_preserves_verified_off_without_enable(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        self.backend.commands.clear()
        result = native.driver.start(should_continue=lambda: False)
        self.assertIs(result, False)
        self.assertEqual(native.driver.state, "off")
        self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
        self.assertFalse(native.driver.scan_may_be_active)
        self.assertFalse(any(command.upper().startswith(":SWEEP:STATE ")
                             for command in self.writes()))

    def test_driver_start_true_probe_preserves_normal_none_return(self):
        native = SsgNativeLinkageParticipant(self.session, self.points)
        native.prepare()
        try:
            self.assertIsNone(native.driver.start(should_continue=lambda: True))
            self.assertEqual(self.backend.get("SWEep:STATe"), "LEV_FREQ")
        finally:
            native.driver.stop()

    def test_controller_cancel_after_native_reprepare_never_starts_scan_or_replays_output(self):
        axes = (LinkageAxis("dg", StepParameter.FREQUENCY, 1, "Hz", 1000, 100, 2),
                LinkageAxis("ssg", StepParameter.FREQUENCY, 1, "GHz", 1.2, .1, 2))
        points = generate_linkage_points(axes, ScanMode.SYNCHRONOUS)
        native = SsgNativeLinkageParticipant(self.session,
                                             tuple(point.grouped()["ssg"] for point in points))
        counter = FakeCounter()
        writer = RecordingWriter("dg", [])
        controller = MultiDeviceTriggerController(
            counter, {"dg": writer}, points, native_participants={"ssg": native},
        )
        original_prepare = native.driver.prepare_sweep
        prepare_calls = []
        def prepare(config):
            result = original_prepare(config)
            prepare_calls.append(config)
            # The first prepare belongs to participant.prepare(); the second
            # is driver.start()'s complete pre-enable validation/upload.
            if len(prepare_calls) == 2:
                controller.request_stop()
            return result
        native.driver.prepare_sweep = prepare
        controller.start()
        try:
            self.assertEqual(len(prepare_calls), 2)
            self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
            self.assertFalse(native.requires_stop)
            self.assertFalse(counter.running)
            self.assertTrue(controller.is_quiescent)
            self.assertEqual(writer.batches, [])
            self.assertEqual(self.cw_writes(), [])
            self.assertNotIn("*TRG", self.writes())
            self.assertFalse(any(command.upper().startswith(":SWEEP:STATE ")
                                 for command in self.writes()))
            self.assertEqual(controller.snapshot().applied_count, 0)
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
