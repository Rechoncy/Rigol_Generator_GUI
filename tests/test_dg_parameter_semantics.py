import unittest

from rigol_control.drivers.dg4000 import BasicWaveform, DG4000Driver, StepParameter, StepValue, finite_number
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.units import roundtrip_number
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession


class RecoveryBackend:
    def __init__(self, identity, *, fail_write=False):
        self.identity, self.fail_write = identity, fail_write
        self.writes = []

    def query(self, command):
        if command == '*IDN?': return self.identity
        if command == ':COUP:STAT?': return 'FREQ:OFF,PHASE:OFF,AMPL:OFF'
        return 'SIN'

    def write(self, command):
        self.writes.append(command)
        if self.fail_write: raise OSError('uncertain USB write')

    def close(self):
        pass


class DgParameterSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.generator = VirtualGenerator('SIM::DG4000::units')
        self.generator.scpi_delay_ms = 0
        self.driver = DG4000Driver(VirtualScpiSession(self.generator))
        self.driver.connect()
        self.addCleanup(self.driver.disconnect)

    def test_typed_amplitude_is_vpp_even_when_instrument_unit_is_vrms_or_dbm(self):
        ch1 = self.generator.snapshot().channels[0]
        for unit in ('VRMS', 'DBM'):
            with self.subTest(unit=unit):
                self.driver.write(f':SOUR2:VOLT:UNIT {unit}')
                self.driver.write(':SOUR2:VOLT 1')
                self.assertNotEqual(self.generator.channels[2]['amplitude_vpp'], 1)
                self.driver.configure_basic(2, BasicWaveform('SIN', 1000, 1))
                self.assertEqual(self.generator.channels[2]['amplitude_vpp'], 1)
                prepared = self.driver.prepare_trigger()
                self.generator.commands.clear()
                prepared.apply_steps((StepValue(StepParameter.AMPLITUDE, 2, .5),))
                self.assertEqual(self.generator.channels[2]['amplitude_vpp'], .5)
                self.assertEqual(list(self.generator.commands), [':SOUR2:VOLT 0.5VPP'])
                self.assertEqual(self.generator.snapshot().channels[0], ch1)

    def test_ask_amplitude_also_carries_explicit_vpp(self):
        self.driver.configure_modulation(2, 'ASK', parameter=2.5)
        self.assertIn(':SOUR2:MOD:ASK:AMPL 2.5VPP', self.generator.commands)

    def test_microhertz_values_remain_distinct_in_scpi_and_roundtrip(self):
        values = (100000000.000001, 100000000.000002)
        prepared = self.driver.prepare_trigger()
        self.generator.commands.clear()
        for value in values:
            self.assertEqual(float(roundtrip_number(value)), value)
            self.assertEqual(float(finite_number(value)), value)
            prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, value),))
        commands = list(self.generator.commands)
        self.assertNotEqual(commands[0], commands[1])
        self.assertEqual([float(c.split()[1]) for c in commands], list(values))

    def test_invalid_phase_is_rejected_before_any_parameter_write(self):
        prepared = self.driver.prepare_trigger()
        for phase in (-1, 361, float('nan')):
            for parameter in (StepParameter.PHASE, StepParameter.PSK_PHASE):
                with self.subTest(phase=phase, parameter=parameter):
                    self.generator.commands.clear()
                    with self.assertRaises(ValueError):
                        prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 1000),
                                              StepValue(parameter, 2, phase)))
                    self.assertFalse(self.generator.commands)
            self.generator.commands.clear()
            with self.assertRaises(ValueError):
                self.driver.configure_basic(2, BasicWaveform('SIN', 1000, 1, phase_deg=phase))
            self.assertFalse(self.generator.commands)
        for phase in (0, 360):
            prepared.apply_steps((StepValue(StepParameter.PHASE, 2, phase),))

    def test_known_model_waveform_limits_reject_before_writing(self):
        limits = {'DG4062': (25e6, 1e6, 15e6), 'DG4102': (40e6, 3e6, 25e6),
                  'DG4162': (50e6, 4e6, 40e6), 'DG4202': (60e6, 5e6, 50e6)}
        for model, maxima in limits.items():
            backend = RecoveryBackend(f'RIGOL,{model},SAME,1')
            driver = DG4000Driver(ScpiSession('test', backend_factory=lambda *_, b=backend: b))
            driver.connect()
            try:
                for function, maximum in zip(('SQU', 'RAMP', 'PULS', 'USER'), (*maxima, maxima[-1])):
                    backend.writes.clear()
                    with self.subTest(model=model, function=function), self.assertRaises(ValueError):
                        driver.configure_basic(2, BasicWaveform(function, maximum + 1, 1))
                    self.assertFalse(backend.writes)
                    driver.configure_basic(2, BasicWaveform(function, maximum, 1))
            finally:
                driver.disconnect()

    def test_trigger_checks_captured_waveform_without_querying_per_point(self):
        self.driver.write(':SOUR2:FUNC RAMP')
        prepared = self.driver.prepare_trigger()
        self.generator.commands.clear()
        with self.assertRaises(ValueError):
            prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 5e6),))
        self.assertFalse(self.generator.commands)
        prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 4e6),))
        self.assertEqual(list(self.generator.commands), [':SOUR2:FREQ 4000000'])

    def test_real_waveform_reply_names_allow_an_unrelated_channel_to_arm(self):
        for name, normalized in (('PULSE', 'PULS'), ('NOISE', 'NOIS'), ('HARMONIC', 'HARM'),
                                 ('CUSTOM', 'USER'), ('SINC', 'USER'), ('MCNOSIE', 'USER')):
            with self.subTest(name=name):
                self.generator.channels[1]['function'] = name
                prepared = self.driver.prepare_trigger()
                self.assertEqual(prepared.waveforms[1], normalized)
                prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 1000),))

    def test_recovery_does_not_send_old_settings_to_changed_model_or_serial(self):
        for replacement_identity in ('RIGOL,DG4062,SAME,1', 'RIGOL,DG4202,OTHER,1'):
            initial = RecoveryBackend('RIGOL,DG4202,SAME,1', fail_write=True)
            replacement = RecoveryBackend(replacement_identity)
            backends = iter((initial, replacement))
            driver = DG4000Driver(ScpiSession('test', backend_factory=lambda *_: next(backends)))
            driver.connect()
            try:
                with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                    driver.configure_basic(2, BasicWaveform('SIN', 200e6, 1))
                self.assertFalse(replacement.writes)
                with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                    driver.set_output_load(2, 50)
                with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                    driver.set_output(2, False)
                with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                    driver.prepare_trigger()
                with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                    driver.apply_steps((StepValue(StepParameter.AMPLITUDE, 2, 1),))
                self.assertFalse(replacement.writes)
                driver.identify()
                driver.set_output_load(2, 50)
                self.assertEqual(replacement.writes, [':OUTP2:LOAD 50'])
            finally:
                driver.disconnect()

    def test_shared_driver_preserves_the_managers_original_identity_pin(self):
        from rigol_control.connections import DeviceConnectionManager
        from rigol_control.models import DeviceKind, DeviceRecord
        initial = RecoveryBackend('RIGOL,DG4202,SAME,1', fail_write=True)
        replacement = RecoveryBackend('RIGOL,DG4202,OTHER,1')
        backends = iter((initial, replacement))
        manager = DeviceConnectionManager(session_factory=lambda resource: ScpiSession(
            resource, backend_factory=lambda *_: next(backends)))
        record = DeviceRecord.create(kind=DeviceKind.DG4000, name='DG', resource='test')
        manager.connect(record)
        try:
            with self.assertRaises(ValueError):
                manager.dg_driver_for(record).set_output_load(2, 50)
            with self.assertRaisesRegex(ValueError, '型号或序列号已变化'):
                manager.dg_driver_for(record).prepare_trigger()
            self.assertFalse(replacement.writes)
        finally:
            manager.close_all()


if __name__ == '__main__':
    unittest.main()
