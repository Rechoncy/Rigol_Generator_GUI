import unittest

from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.scpi import ScpiError, ScpiSession
from rigol_control.drivers.siglent_ssg import (
    SiglentSsgDriver, SsgModulation, SsgPoint, SsgSweep, VirtualSsgBackend,
    VIRTUAL_SSG_RESOURCE, capabilities,
)
from rigol_control.models import DeviceKind, DeviceRecord


class SiglentDriverTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.
        self.backend = VirtualSsgBackend(clock=lambda: self.now)
        self.session = ScpiSession("SIM::test", backend_factory=lambda *_: self.backend)
        self.session.connect()
        self.driver = SiglentSsgDriver(self.session)
        self.driver.identify()

    def writes(self):
        return [cmd for cmd in self.backend.commands if "?" not in cmd]

    def test_identity_readonly_and_unknown_model(self):
        self.assertTrue(self.driver.caps.vector)
        self.assertEqual(self.writes(), [])
        self.backend.identity = "Other,SSG3032X,SN,1"
        self.driver.identify()
        with self.assertRaises(ValueError):
            self.driver.set_output(True)
        self.assertEqual(self.writes(), [])

    def test_family_and_hardware_capabilities(self):
        for model, max_hz in (("SSG3021X", 2.1e9), ("SSG3032X", 3.2e9),
                              ("SSG6083A", 13.6e9), ("SSG6085A", 20e9), ("SSG6087A", 40e9)):
            self.assertEqual(capabilities(f"Siglent Technologies,{model},SN,1").maximum_hz, max_hz)
        self.assertFalse(capabilities("SIGLENT,SSG6085A,SSG6AA2000000,1").fm_pm)
        self.assertTrue(capabilities("SIGLENT,SSG6085A,SSG6AA3000000,1").fm_pm)
        self.assertFalse(capabilities("SIGLENT,SSG6082A,SN,1").known)

    def test_rf_independent_and_invalid_has_no_writes(self):
        self.driver.set_frequency(2e9)
        self.driver.set_power(-12)
        self.assertEqual(self.backend.get("OUTPut"), "0")
        previous = self.writes()
        for value in (float("nan"), float("inf"), 9e9):
            with self.assertRaises(ValueError):
                self.driver.set_frequency(value)
        self.assertEqual(self.writes(), previous)
        self.driver.set_output(True)
        self.driver.set_output(False)
        self.assertEqual(self.backend.get("OUTPut"), "0")

    def test_power_limit_depends_on_frequency(self):
        self.backend.identity = "SIGLENT,SSG3032X,SN,1"
        self.driver.identify()
        self.driver.set_frequency(9000)
        with self.assertRaises(ValueError):
            self.driver.set_power(10)

    def test_apply_never_starts_or_changes_rf_modulation(self):
        self.driver.apply_modulation(SsgModulation(fm=True, pulse=True, master=True))
        self.driver.set_output(True)
        self.driver.prepare_sweep(SsgSweep())
        self.assertEqual(self.backend.get("SWEep:STATe"), "OFF")
        self.assertEqual(self.backend.get("OUTPut"), "1")
        self.assertEqual(self.backend.get("FM:STATe"), "1")
        self.assertEqual(self.backend.get("PULM:STATe"), "1")
        self.assertNotIn(":SWEep:EXECute", self.writes())
        self.assertNotIn("*TRG", self.writes())

    def test_nested_triggers_consume_separate_events(self):
        self.driver.prepare_sweep(SsgSweep(sweep_trigger="BUS", point_trigger="BUS", points=2))
        self.driver.start()
        self.assertEqual(self.backend.phase, "sweep_wait")
        self.driver.bus_trigger()
        self.assertEqual(self.backend.phase, "point_wait")
        self.assertEqual(self.backend.index, -1)
        self.driver.bus_trigger()
        self.assertEqual(self.backend.index, 0)
        self.now += .04
        self.driver.bus_trigger()
        self.assertEqual(self.backend.index, 1)
        self.driver.stop()
        self.assertEqual(self.driver.state, "off")

    def test_single_requires_execute_and_snapshot_does_not_claim_completion(self):
        self.driver.prepare_sweep(SsgSweep(mode="SINGLE", point_trigger="AUTO", points=2))
        self.driver.start()
        self.assertEqual(self.backend.phase, "single_armed")
        self.driver.execute_single()
        self.assertEqual(self.backend.index, 0)
        self.now += .04
        self.driver.snapshot()
        self.now += .04
        self.driver.snapshot()
        self.assertEqual(self.backend.phase, "single_complete")
        self.assertEqual(self.driver.state, "enabled")

    def test_list_upload_replaces_preset_row_and_reads_back(self):
        points = (SsgPoint(1.2e9, -14, .1), SsgPoint(1.3e9, -15, .2))
        self.driver.prepare_sweep(SsgSweep(kind="LIST", target="LEV_FREQ", table=points))
        self.assertEqual(self.driver.read_list(), points)
        self.assertEqual(len(self.backend.table), 2)
        self.driver.start()
        self.backend.trigger("EXT")
        self.assertEqual(float(self.backend.get("POWer")), -14)

    def test_invalid_list_and_triggers_make_no_writes(self):
        for config in (SsgSweep(kind="LIST", table=(SsgPoint(1e9, -20),)),
                       SsgSweep(point_trigger="EXTERNAL;*RST"),
                       SsgSweep(points=2.5), SsgSweep(dwell_s=.001)):
            with self.assertRaises(ValueError):
                self.driver.prepare_sweep(config)
        self.assertEqual(self.writes(), [])

    def test_closed_loop_and_iq_frequency_guard(self):
        self.backend.put("POWer:SPC:STATe", "1")
        with self.assertRaises(ValueError):
            self.driver.prepare_sweep(SsgSweep())
        self.backend.put("POWer:SPC:STATe", "0")
        self.backend.put("FUNCtion:DM:STATe", "1")
        with self.assertRaises(ValueError):
            self.driver.prepare_sweep(SsgSweep(start_hz=9e3))
        self.assertEqual(self.writes(), [])

    def test_active_sweep_blocks_conflicting_writes_but_allows_rf_off(self):
        self.driver.prepare_sweep(SsgSweep())
        self.driver.start()
        for operation in (lambda: self.driver.set_power(-10), lambda: self.driver.set_frequency(2e9),
                          lambda: self.driver.prepare_sweep(SsgSweep()),
                          lambda: self.driver.apply_modulation(SsgModulation()), lambda: self.driver.raw("*RST")):
            with self.assertRaises(ValueError):
                operation()
        self.driver.set_output(False)
        self.assertEqual(self.backend.get("SWEep:STATe"), "FREQUENCY")

    def test_vector_fm_command_mapping(self):
        self.driver.apply_modulation(SsgModulation(fm=True, fm_source="INT,EXT"))
        self.assertIn(":FM:SOURce INT1,EXT", self.writes())
        self.assertIn(":FM1:DEViation 1000", self.writes())
        self.backend.identity = "SIGLENT,SSG3032X,SN,1"
        self.driver.identify()
        self.backend.commands.clear()
        self.driver.apply_modulation(SsgModulation(fm=True))
        self.assertIn(":FM:DEViation 1000", self.writes())
        self.assertFalse(any(":FM1:" in cmd for cmd in self.writes()))

    def test_modulation_conflicts_and_limited_combination(self):
        for config in (SsgModulation(fm=True, pm=True), SsgModulation(am=True, fm=True, pulse=True)):
            with self.assertRaises(ValueError):
                self.driver.apply_modulation(config)
        self.assertEqual(self.writes(), [])
        warnings = self.driver.apply_modulation(SsgModulation(am=True, pulse=True))
        self.assertTrue(warnings)
        self.backend.put("FUNCtion:DM:STATe", "1")
        self.backend.put("RADio:ARB:MDEStination:PULSe", "M1")
        with self.assertRaises(ValueError):
            self.driver.apply_modulation(SsgModulation(pulse=True))
        self.backend.put("RADio:ARB:MDEStination:PULSe", "NONE")
        self.driver.apply_modulation(SsgModulation(pulse=True))

    def test_pulse_key_mapping_and_bus_is_model_specific(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="KEY"))
        self.assertEqual(self.backend.get("PULM:TRIGger:MODE"), "SINGLE")
        self.backend.identity = "SIGLENT,SSG3021X,SN,1"
        self.driver.identify()
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS"))
        self.assertEqual(self.backend.get("PULM:TRIGger:MODE"), "BUS")
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="KEY"))
        self.assertEqual(self.backend.get("PULM:TRIGger:MODE"), "KEY")
        self.backend.identity = "SIGLENT,SSG6085A,SN,1"
        self.driver.identify()
        with self.assertRaises(ValueError):
            self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS"))

    def test_global_trigger_can_also_trigger_pulse(self):
        self.driver.apply_modulation(SsgModulation(pulse=True, pulse_trigger="BUS"))
        self.driver.prepare_sweep(SsgSweep(point_trigger="BUS"))
        self.driver.start()
        self.driver.bus_trigger()
        self.assertEqual(self.backend.pulse_triggers, 1)
        self.assertEqual(self.backend.index, 0)

    def test_no_retry_after_uncertain_trigger_write(self):
        original = self.backend.write
        def fail(command):
            original(command)
            raise OSError("transport dropped after execution")
        self.backend.write = fail
        with self.assertRaises(ScpiError):
            self.driver.bus_trigger()
        self.assertEqual(self.writes().count("*TRG"), 1)

    def test_readback_rejects_silently_clamped_settings(self):
        original = self.backend.write
        def clamp(command):
            original(command)
            if command.startswith(":POWer "):
                self.backend.put("POWer", "-30")
        self.backend.write = clamp
        with self.assertRaises(ScpiError):
            self.driver.set_power(-10)

    def test_stale_error_is_reported_and_rf_off_still_works(self):
        self.backend.esr = 32
        with self.assertRaises(ScpiError):
            self.driver.set_power(-15)
        self.assertEqual(self.writes(), [])
        self.backend.esr = 32
        self.driver.set_output(False)
        self.driver.stop()

    def test_unknown_raw_write_and_compound_command_rejected(self):
        for command in ("*RST;*IDN?", "*IDN?\n*RST"):
            with self.assertRaises(ValueError):
                self.driver.raw(command)
        self.backend.identity = "SIGLENT,UNTESTED,SN,1"
        self.driver.identify()
        self.assertIn("UNTESTED", self.driver.raw("*IDN?"))
        with self.assertRaises(ValueError):
            self.driver.raw("*RST")

    def test_virtual_manager_reuses_session_and_hardware_state(self):
        manager = DeviceConnectionManager()
        record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG", resource=VIRTUAL_SSG_RESOURCE)
        manager.connect(record)
        session = manager.session_for(record)
        session.write(":FREQuency 2000000000")
        manager.disconnect(record)
        manager.connect(record)
        self.assertIs(manager.session_for(record), session)
        self.assertEqual(float(session.query(":FREQuency?")), 2e9)
        manager.close_all()

    def test_full_configuration_query_is_readonly_and_vector_aware(self):
        result = self.driver.configuration_snapshot()
        self.assertIn(":FM1:DEViation", result)
        self.assertEqual(self.writes(), [])

    def test_front_panel_start_updates_state_when_write_is_rejected(self):
        self.driver.snapshot()
        self.session.write(":SWEep:STATe FREQUENCY")
        with self.assertRaises(ValueError):
            self.driver.set_power(-12)
        self.assertEqual(self.driver.state, "enabled")
