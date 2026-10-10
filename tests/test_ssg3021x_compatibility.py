"""SSG3000X C01A/EN01C + C01B user manual checks, separate from the virtual parser.

These validate software behavior against a documented command subset, not firmware
or electrical behavior. Reference: docs/SIGLENT_SSG_CN.md, SSG3021X audit section.
"""
import unittest

from rigol_control.drivers.scpi import ScpiError, ScpiSession
from rigol_control.drivers.siglent_ssg import SiglentSsgDriver, SsgModulation, SsgPoint, SsgSweep, VirtualSsgBackend


class Ssg3021xContractBackend(VirtualSsgBackend):
    # Explicit C01A command headers, independent of the virtual parser's keys.
    DOCUMENTED_HEADERS = {
        "*IDN", "*ESR", "*OPC", "*TRG", ":FREQUENCY", ":POWER", ":OUTPUT", ":OUTPUT:MODULATION", ":MODULATION",
        ":POWER:SPC:STATE", ":SWEEP:STATE", ":SWEEP:TYPE", ":SWEEP:MODE", ":SWEEP:DIRECT",
        ":SWEEP:SWEEP:TRIGGER:TYPE", ":SWEEP:POINT:TRIGGER:TYPE", ":INPUT:TRIGGER:SLOPE",
        ":SWEEP:STEP:START:FREQUENCY", ":SWEEP:STEP:STOP:FREQUENCY",
        ":SWEEP:STEP:START:LEVEL", ":SWEEP:STEP:STOP:LEVEL", ":SWEEP:STEP:DWELL",
        ":SWEEP:STEP:POINTS", ":SWEEP:STEP:SHAPE", ":SWEEP:STEP:SPACE", ":SWEEP:EXECUTE",
        ":SWEEP:LIST:INITIALIZE:PRESET", ":SWEEP:LIST:CHANGE", ":SWEEP:LIST:ADDLIST",
        ":SWEEP:LIST:CPOINT", ":SWEEP:LIST:LIST", ":SWEEP:CURRENT:DATA",
        ":SWEEP:CURRENT:FREQUENCY", ":SWEEP:CURRENT:LEVEL",
        ":AM:STATE", ":AM:SOURCE", ":AM:WAVEFORM", ":AM:FREQUENCY", ":AM:DEPTH",
        ":FM:STATE", ":FM:SOURCE", ":FM:WAVEFORM", ":FM:FREQUENCY", ":FM:DEVIATION",
        ":PM:STATE", ":PM:SOURCE", ":PM:WAVEFORM", ":PM:FREQUENCY", ":PM:DEVIATION",
        ":PULM:STATE", ":PULM:SOURCE", ":PULM:MODE", ":PULM:PERIOD", ":PULM:WIDTH", ":PULM:DELAY",
        ":PULM:OUT:STATE", ":PULM:TRIGGER:STATE", ":PULM:TRIGGER:MODE",
        ":PULM:TRIGGER:EXTERNAL:SLOPE", ":PULM:TRIGGER:EXTERNAL:GATE:POLARITY",
    }

    def __init__(self):
        super().__init__("Siglent Technologies,SSG3021X,SSG3_TEST,3.1.21")

    def _check_header(self, command):
        header = command.split(" ")[0].rstrip("?").upper()
        if header.startswith((":FM1:", ":FM2:", ":IQ:", ":FUNCTION:DM:", ":RADIO:")):
            raise AssertionError(f"Vector-only command sent to SSG3021X: {command}")
        if header not in self.DOCUMENTED_HEADERS:
            raise AssertionError(f"Command not in the C01A header contract: {command}")

    def query(self, command):
        self._check_header(command)
        if command.upper() == ":OUTPUT:MODULATION?":
            # Model the user's USB timeout on this documented equivalent form.
            raise OSError("VI_ERROR_TMO: OUTPut modulation alias did not reply")
        if command.upper() == ":SWEEP:CURRENT:DATA?":
            raise OSError("VI_ERROR_TMO: combined current scan query did not reply")
        return super().query(command)

    def write(self, command):
        self._check_header(command)
        header, _, value = command.upper().partition(" ")
        if header == ":AM:DEPTH" and not 0 <= float(value) <= 1:
            self.commands.append(command)
            self.esr |= 16
            return
        if header == ":PULM:TRIGGER:MODE" and value not in ("AUTO", "KEY", "EXT", "EXTERNAL", "EGATE", "BUS"):
            raise AssertionError("Undocumented pulse trigger")
        if header in (":PULM:DELAY", ":PULM:TRIGGER:EXTERNAL:SLOPE") and self.get("PULM:TRIGger:MODE") not in ("EXT", "EXTERNAL"):
            raise AssertionError("External-trigger parameters must only be sent in EXT mode")
        if header == ":PULM:TRIGGER:EXTERNAL:GATE:POLARITY" and self.get("PULM:TRIGger:MODE") != "EGATE":
            raise AssertionError("Gate polarity must only be sent in EGATE mode")
        if header == ":PULM:PERIOD" and float(value) < float(self.get("PULM:WIDTh")) + 20e-9:
            raise AssertionError("Pulse period was shortened before width")
        if header == ":PULM:WIDTH" and float(value) > float(self.get("PULM:PERiod")) - 20e-9:
            raise AssertionError("Pulse width was increased before period")
        super().write(command)


class Ssg3021xCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.backend = Ssg3021xContractBackend()
        self.session = ScpiSession("TEST", backend_factory=lambda *_: self.backend)
        self.session.connect()
        self.driver = SiglentSsgDriver(self.session)
        self.driver.identify()

    def writes(self):
        return [c for c in self.backend.commands if "?" not in c]

    def test_current_values_use_separate_queries_without_combined_data_or_inferred_counts(self):
        self.driver.prepare_sweep(SsgSweep())
        self.driver.start()
        original = self.backend.query
        def response(command):
            values = {":SWEep:CURRent:FREQuency?": "1234000000", ":SWEep:CURRent:LEVel?": "-31"}
            return values[command] if command in values else original(command)
        self.backend.query = response
        result = self.driver.snapshot(include_modulation=False, include_current_point=True)
        self.assertEqual(result[":SWEep:CURRent:FREQuency"], "1234000000")
        self.assertEqual(result[":SWEep:CURRent:LEVel"], "-31")
        self.assertNotIn(":SWEep:CURRent:DATA", result)
        self.assertNotIn(":SWEep:CURRent:FREQuency", self.driver.snapshot(include_modulation=False))

    def test_current_values_reject_non_numeric_or_non_finite_replies(self):
        self.driver.prepare_sweep(SsgSweep())
        self.driver.start()
        original = self.backend.query
        try:
            for reply in ("", "not ready", "nan", "inf", "0"):
                with self.subTest(reply=reply):
                    self.backend.query = lambda command, value=reply: value if command == ":SWEep:CURRent:FREQuency?" else original(command)
                    with self.assertRaisesRegex(ScpiError, "当前扫描值无法解析"):
                        self.driver.snapshot(include_modulation=False, include_current_point=True)
        finally:
            self.backend.query = original

    def test_identification_readback_and_modulation_never_use_vector_commands(self):
        self.driver.snapshot()
        self.driver.configuration_snapshot()
        self.assertEqual(self.driver.caps.maximum_hz, 2.1e9)
        self.assertTrue(self.driver.caps.fm_pm)
        self.assertFalse(self.driver.caps.vector)
        for c in (SsgModulation(am=True, fm=True), SsgModulation(am=True, pm=True),
                  SsgModulation(fm=True, pulse=True), SsgModulation(pm=True, pulse=True)):
            self.driver.apply_modulation(c)
        self.assertIn(":FM:DEViation 1000", self.writes())

    def test_am_percentage_is_converted_to_documented_fraction(self):
        # EN01C 3.4.7.2.4 / Plus CN01A printed p.57: DEPTh is 0..1.
        for percent, fraction in ((0, 0), (30, .3), (100, 1)):
            with self.subTest(percent=percent):
                self.driver.apply_modulation(SsgModulation(am=True, am_depth=percent, master=True))
                self.assertEqual(float(self.backend.get("AM:DEPTh")), fraction)
                self.assertEqual(self.backend.get("AM:STATe"), "1")

    def test_primary_master_command_covers_write_read_and_pulse_bus_for_3000x(self):
        for model in ("SSG3021X", "SSG3032X"):
            with self.subTest(model=model):
                self.backend.identity = f"Siglent Technologies,{model},SSG3_TEST,3.1.21"
                self.driver.identify()
                self.backend.commands.clear()
                self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
                self.assertEqual(self.driver.snapshot()[":OUTPut:MODulation"], "1")
                count = self.backend.pulse_triggers
                self.driver.pulse_bus_trigger()
                self.assertEqual(self.backend.pulse_triggers, count + 1)
                self.assertIn(":MODulation 1", self.writes())
                self.assertIn(":MODulation?", self.backend.commands)
                self.assertFalse(any(c.upper().startswith(":OUTPUT:MODULATION") for c in self.backend.commands))

    def test_basic_snapshot_succeeds_when_all_modulation_queries_timeout(self):
        original = self.backend.query
        def unsupported(command):
            if command.upper().startswith((":MODULATION", ":OUTPUT:MODULATION", ":AM:", ":FM:", ":PM:", ":PULM:")):
                raise OSError("VI_ERROR_TMO: modulation query timed out")
            return original(command)
        self.backend.query = unsupported
        result = self.driver.snapshot(include_modulation=False)
        self.assertEqual(result[":POWer"], "-20")
        self.assertEqual(result[":OUTPut"], "0")
        self.assertEqual(result[":SWEep:STATe"], "OFF")
        self.assertNotIn(":OUTPut:MODulation", result)
        with self.assertRaisesRegex(ScpiError, "MODulation.*VI_ERROR_TMO"):
            self.driver.snapshot(include_modulation=True)

    def test_command_error_identifies_rejected_command_and_stops_batch(self):
        with self.assertRaisesRegex(ScpiError, r"\[:AM:DEPTh 30\].*ESR=16"):
            self.driver._batch([":AM:DEPTh 30", ":AM:STATe ON"])
        self.assertNotIn(":AM:STATe ON", self.writes())
        self.assertEqual(self.backend.get("AM:STATe"), "0")

    def test_mode_specific_pulse_queries_are_not_sent_in_auto(self):
        result = self.driver.configuration_snapshot()
        for key in (":PULM:DELay", ":PULM:TRIGger:EXTernal:SLOPe",
                    ":PULM:TRIGger:EXTernal:GATE:POLarity"):
            self.assertNotIn(key, result)
            self.assertNotIn(key + "?", self.backend.commands)

    def test_pulse_train_is_changed_before_reading_internal_period(self):
        self.backend.put("PULM:MODE", "PTRAIN")
        original = self.backend.query
        def reject_period_in_train(command):
            if command == ":PULM:PERiod?" and self.backend.get("PULM:MODE") == "PTRAIN":
                raise AssertionError("PERiod? is only defined for single/double pulse")
            return original(command)
        self.backend.query = reject_period_in_train
        result = self.driver.configuration_snapshot()
        self.assertNotIn(":PULM:PERiod", result)
        self.driver.apply_modulation(SsgModulation(pulse=True))
        self.assertEqual(self.backend.get("PULM:MODE"), "SINGLE")

    def test_unrecognized_scan_reply_is_not_assumed_to_be_running(self):
        self.backend.put("SWEep:STATe", "Off")
        self.driver._idle()
        self.assertEqual(self.driver.state, "off")
        self.backend.put("SWEep:STATe", "BAD REPLY")
        with self.assertRaisesRegex(ScpiError, "无法解析扫描状态"):
            self.driver.snapshot()
        self.assertFalse(self.driver.scan_may_be_active)
        self.assertEqual(self.driver.state, "unknown")

    def test_external_pulse_detailed_query_does_not_read_internal_timing(self):
        self.backend.put("PULM:SOURce", "EXTERNAL")
        result = self.driver.configuration_snapshot()
        self.assertNotIn(":PULM:PERiod", result)
        self.assertNotIn(":PULM:WIDTh", result)

    def test_frequency_and_power_segment_boundaries(self):
        for hz, maximum in ((9e3, 9), (99999, 9), (1e5, 15), (999999, 15), (1e6, 20), (2.1e9, 20)):
            with self.subTest(hz=hz):
                self.driver.set_power(-20)
                self.driver.set_frequency(hz)
                self.driver.set_power(maximum)
                with self.assertRaises(ValueError):
                    self.driver.set_power(maximum + .01)
        with self.assertRaises(ValueError):
            self.driver.set_frequency(2.1e9 + 1)
        with self.assertRaises(ValueError):
            self.driver.set_frequency(8999)

    def test_voltage_api_converts_to_dbm_and_preserves_rf_off(self):
        self.driver.set_power(200, "mVpp")
        self.assertAlmostEqual(float(self.backend.get("POWer")), -10)
        self.driver.set_power(100, "mV")
        self.assertAlmostEqual(float(self.backend.get("POWer")), -6.98970004336)
        self.assertEqual(self.backend.get("OUTPut"), "0")
        self.assertFalse(any("mVpp" in command for command in self.writes()))

    def test_voltage_inputs_keep_model_power_limits(self):
        for amplitude, unit in ((5000, "mV"), (10000, "mVpp"), (0, "mV")):
            with self.subTest(amplitude=amplitude, unit=unit), self.assertRaises(ValueError):
                self.driver.set_power(amplitude, unit)
        self.assertEqual(self.writes(), [])

    def test_voltage_amplitudes_accept_readback_at_documented_resolution(self):
        original = self.backend.write
        def quantize_power(command):
            original(command)
            header = command.split(" ")[0].upper()
            if header in (":POWER", ":SWEEP:STEP:START:LEVEL", ":SWEEP:STEP:STOP:LEVEL"):
                self.backend.put(header, f"{float(self.backend.get(header)):.2f}")
            elif header in (":SWEEP:LIST:ADDLIST", ":SWEEP:LIST:CHANGE"):
                self.backend.table = [SsgPoint(p.frequency_hz, round(p.power_dbm, 2), p.dwell_s)
                                      for p in self.backend.table]
        self.backend.write = quantize_power
        self.driver.set_power(100, "mV")
        self.assertEqual(float(self.backend.get("POWer")), -6.99)
        self.driver.prepare_sweep(SsgSweep(start_dbm=-6.98970004336, stop_dbm=-5.23974123))
        self.assertEqual(float(self.backend.get("SWEep:STEP:STARt:LEVel")), -6.99)
        self.assertEqual(float(self.backend.get("SWEep:STEP:STOP:LEVel")), -5.24)
        points = (SsgPoint(1e9, -6.98970004336), SsgPoint(1.1e9, -5.23974123))
        self.driver.prepare_sweep(SsgSweep(kind="LIST", target="LEVEL", table=points))
        self.assertEqual([p.power_dbm for p in self.driver.read_list()], [-6.99, -5.24])
        self.assertEqual(self.backend.get("OUTPut"), "0")

    def test_amplitude_resolution_does_not_hide_larger_or_unrelated_errors(self):
        for header in (":POWer", ":SWEep:STEP:STARt:LEVel", ":SWEep:STEP:STOP:LEVel"):
            self.backend.put(header, "-9.98")
            with self.subTest(header=header), self.assertRaisesRegex(ScpiError, "回读不一致"):
                self.driver._verify(header, -10)
        self.backend.put("FREQuency", "9000.005")
        with self.assertRaisesRegex(ScpiError, "回读不一致"):
            self.driver._verify(":FREQuency", 9000)
        self.backend.put("SWEep:STEP:DWELl", "0.035")
        with self.assertRaisesRegex(ScpiError, "回读不一致"):
            self.driver._verify(":SWEep:STEP:DWELl", .03)
        original = self.backend.write
        def wrong_list_power(command):
            original(command)
            if command.split(" ")[0].upper() in (":SWEEP:LIST:ADDLIST", ":SWEEP:LIST:CHANGE"):
                p = self.backend.table[-1]
                self.backend.table[-1] = SsgPoint(p.frequency_hz, p.power_dbm + .02, p.dwell_s)
        self.backend.write = wrong_list_power
        points = (SsgPoint(1e9, -10), SsgPoint(1.1e9, -20))
        with self.assertRaisesRegex(ScpiError, "扫描列表回读与请求不一致"):
            self.driver.prepare_sweep(SsgSweep(kind="LIST", table=points))

    def test_step_all_trigger_sources_and_both_edges_preserve_am(self):
        self.driver.apply_modulation(SsgModulation(am=True, master=True))
        for source in ("AUTO", "KEY", "BUS", "EXT"):
            for slope in ("POSITIVE", "NEGATIVE"):
                with self.subTest(source=source, slope=slope):
                    self.driver.prepare_sweep(SsgSweep(sweep_trigger=source, point_trigger=source, slope=slope))
                    self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
                    self.driver.start()
                    self.assertEqual(self.backend.get("AM:STATe"), "1")
                    self.assertEqual(self.backend.get("OUTPut"), "0")
                    self.driver.stop()

    def test_list_and_single_scan_use_documented_3000x_commands(self):
        points = (SsgPoint(9000, -100, .01), SsgPoint(2.1e9, -20, 100))
        self.driver.prepare_sweep(SsgSweep(kind="LIST", target="LEV_FREQ", table=points, mode="SINGLE"))
        self.assertEqual(self.driver.read_list(), points)
        self.driver.start()
        self.driver.execute_single()
        self.assertIn(":SWEep:EXECute", self.writes())
        self.driver.stop()

    def test_each_pulse_trigger_only_writes_its_applicable_parameters(self):
        for trigger in ("AUTO", "KEY", "EXT", "EGATE", "BUS"):
            with self.subTest(trigger=trigger):
                self.backend.commands.clear()
                self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger=trigger))
                commands = self.writes()
                self.assertEqual(any(c.startswith(":PULM:DELay ") for c in commands), trigger == "EXT")
                self.assertEqual(any(":GATE:POLarity " in c for c in commands), trigger == "EGATE")
                self.assertIn(f":PULM:TRIGger:MODE {trigger}", commands)

    def test_bus_pulse_applies_without_trigger_and_button_sends_once(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        self.assertNotIn("*TRG", self.writes())
        self.assertEqual(self.backend.get("OUTPut"), "0")
        trace = self.backend.commands
        self.assertLess(trace.index(":PULM:TRIGger:MODE?"), trace.index(":PULM:STATe ON"))
        self.assertEqual(self.driver.snapshot()[":PULM:TRIGger:MODE"], "BUS")
        self.driver.pulse_bus_trigger()
        self.assertEqual(self.writes().count("*TRG"), 1)
        self.assertEqual(self.backend.pulse_triggers, 1)
        self.assertEqual(self.backend.get("OUTPut"), "0")

    def test_ignored_bus_setting_does_not_enable_pulse_or_send_trigger(self):
        original = self.backend.write
        self.backend.put("PULM:STATe", "1")
        def ignore_bus(command):
            if command == ":PULM:TRIGger:MODE BUS":
                self.backend.commands.append(command)
                return
            original(command)
        self.backend.write = ignore_bus
        with self.assertRaises(ScpiError):
            self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        self.assertEqual(self.backend.get("PULM:STATe"), "0")
        self.assertNotIn(":PULM:STATe ON", self.writes())
        self.assertNotIn("*TRG", self.writes())

    def test_bus_rejected_by_firmware_does_not_fall_back_to_auto(self):
        original = self.backend.write
        def reject_bus(command):
            if command == ":PULM:TRIGger:MODE BUS":
                self.backend.commands.append(command)
                self.backend.esr |= 32
                return
            original(command)
        self.backend.write = reject_bus
        with self.assertRaises(ScpiError):
            self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        self.assertNotIn(":PULM:TRIGger:MODE AUTO", self.writes())
        self.assertEqual(self.backend.get("PULM:STATe"), "0")

    def test_pulse_button_rechecks_front_panel_settings(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        for key, bad, good in (("PULM:TRIGger:MODE", "AUTO", "BUS"),
                               ("PULM:SOURce", "EXTERNAL", "INTERNAL"),
                               ("PULM:STATe", "0", "1"), ("OUTPut:MODulation", "0", "1")):
            self.backend.put(key, bad)
            with self.assertRaises(ValueError):
                self.driver.pulse_bus_trigger()
            self.backend.put(key, good)
        self.assertNotIn("*TRG", self.writes())

    def test_bus_pulse_write_failure_is_not_replayed(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        original = self.backend.write
        def drop(command):
            original(command)
            if command == "*TRG":
                raise OSError("response lost after trigger")
        self.backend.write = drop
        with self.assertRaises(ScpiError):
            self.driver.pulse_bus_trigger()
        self.assertEqual(self.writes().count("*TRG"), 1)
        self.assertEqual(self.backend.pulse_triggers, 1)

    def test_pulse_button_remains_global_during_bus_point_scan(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS", master=True))
        self.driver.prepare_sweep(SsgSweep(point_trigger="BUS"))
        self.driver.start()
        self.driver.pulse_bus_trigger()
        self.assertEqual(self.backend.pulse_triggers, 1)
        self.assertEqual(self.backend.index, 0)

    def test_external_pulse_input_does_not_reprogram_internal_generator(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_source="EXTERNAL",
                                                   pulse_trigger="BUS", pulse_period=-1, pulse_delay=-1))
        commands = self.writes()
        self.assertIn(":PULM:SOURce EXTERNAL", commands)
        self.assertFalse(any(c.startswith((":PULM:TRIGger:MODE ", ":PULM:MODE ", ":PULM:PERiod ", ":PULM:WIDTh ", ":PULM:DELay ")) for c in commands))

    def test_disabled_pulse_settings_do_not_block_analog_modulation(self):
        self.driver.apply_modulation(SsgModulation(am=True, pulse=False, pulse_trigger="BUS", pulse_width=-1))
        self.assertEqual(self.backend.get("AM:STATe"), "1")

    def test_pulse_width_period_transition_order(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_period=.0001, pulse_width=.00005))
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_period=.01, pulse_width=.009))
        self.assertEqual(float(self.backend.get("PULM:WIDTh")), .009)

    def test_sub_mhz_pulse_is_allowed_with_performance_notice(self):
        self.driver.set_frequency(9000)
        warnings = self.driver.apply_modulation(SsgModulation(pulse=True))
        self.assertTrue(any("不保证" in w for w in warnings))

    def test_short_level_readback_is_not_combined_scan(self):
        self.backend.put("SWEep:STATe", "LEV")
        self.driver._verify(":SWEep:STATe", "LEVEL")
        with self.assertRaises(ScpiError):
            self.driver._verify(":SWEep:STATe", "LEV_FREQ")

    def test_modulation_incompatibility_rejected_before_write(self):
        with self.assertRaises(ValueError):
            self.driver.apply_modulation(SsgModulation(fm=True, pm=True))
        self.assertEqual(self.writes(), [])

    def test_scan_limits_and_closed_loop_conflict(self):
        for config in (SsgSweep(points=1), SsgSweep(points=65536),
                       SsgSweep(dwell_s=.0099), SsgSweep(dwell_s=100.001),
                       SsgSweep(stop_hz=2.100000001e9)):
            with self.assertRaises(ValueError):
                self.driver.prepare_sweep(config)
        self.backend.put("POWer:SPC:STATe", "1")
        with self.assertRaises(ValueError):
            self.driver.prepare_sweep(SsgSweep())
        self.assertEqual(self.writes(), [])
