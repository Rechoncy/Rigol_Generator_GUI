"""SSG3000X EN01C + C01B user manual checks, separate from the virtual parser.

These validate software behavior against a documented command subset, not firmware
or electrical behavior. Reference: docs/SIGLENT_SSG_CN.md, SSG3021X audit section.
"""
import unittest

from rigol_control.drivers.scpi import ScpiError, ScpiSession
from rigol_control.drivers.siglent_ssg import SiglentSsgDriver, SsgModulation, SsgPoint, SsgSweep, VirtualSsgBackend


class Ssg3021xContractBackend(VirtualSsgBackend):
    def __init__(self):
        super().__init__("Siglent Technologies,SSG3021X,SSG3_TEST,3.1.21")

    def _check_header(self, command):
        header = command.split(" ")[0].rstrip("?").upper()
        if header.startswith((":FM1:", ":FM2:", ":IQ:", ":FUNCTION:DM:", ":RADIO:")):
            raise AssertionError(f"Vector-only command sent to SSG3021X: {command}")

    def query(self, command):
        self._check_header(command)
        return super().query(command)

    def write(self, command):
        self._check_header(command)
        header, _, value = command.upper().partition(" ")
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
