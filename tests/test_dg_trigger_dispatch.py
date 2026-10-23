import threading
import time
import unittest

from rigol_control.drivers.dg4000 import DG4000Driver, StepParameter, StepValue, BasicWaveform
from rigol_control.drivers.scpi import ScpiSession, ScpiError
from rigol_control.trigger import TriggerStepController, ControllerState
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession
from rigol_control.linkage import MultiDeviceTriggerController, LinkageAxis, generate_linkage_points
from rigol_control.scan import ScanMode


class Counter:
    def __init__(self):
        self.count = 0
        self.starts = 0

    def start(self):
        self.starts += 1

    def read_count(self):
        return self.count

    def stop(self):
        pass


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.generator = VirtualGenerator('SIM::DG4000::1')
        self.generator.scpi_delay_ms = 0
        self.driver = DG4000Driver(VirtualScpiSession(self.generator))
        self.driver.connect()

    def test_ch2_all_step_types_and_count_jump_preserve_entire_ch1(self):
        self.generator.channels[1].update(frequency_hz=12345.0, phase_deg=37.0, output=True, load_ohms=75)
        original = self.generator.snapshot().channels[0]
        for parameter, value in ((StepParameter.FREQUENCY, 2000), (StepParameter.AMPLITUDE, .7),
                                 (StepParameter.PHASE, 123), (StepParameter.PSK_PHASE, 90),
                                 (StepParameter.FSK_FREQUENCY, 5000)):
            with self.subTest(parameter=parameter):
                counter = Counter()
                controller = TriggerStepController(counter, self.driver, parameter, 2, [value, value+1])
                controller.start()
                self.assertEqual(controller.snapshot().applied_count, 0)
                counter.count = 100
                deadline = time.monotonic()+2
                while controller.snapshot().applied_count < 100 and time.monotonic() < deadline:
                    time.sleep(.002)
                controller.stop()
                self.assertEqual(controller.snapshot().applied_count, 100)
                self.assertEqual(self.generator.snapshot().channels[0], original)
        # Only one coupling read per arming, never one per edge.
        # Commands deque is bounded, so use a fresh run to count exactly.
        self.generator.commands.clear()
        writer = self.driver.prepare_trigger()
        for i in range(20):
            writer.apply_steps([StepValue(StepParameter.FREQUENCY, 2, 2000+i)])
        self.assertEqual(list(self.generator.commands).count(':COUP:STAT?'), 1)

    def test_joint_packet_has_absolute_channel_headers_and_one_write(self):
        self.generator.commands.clear()
        writer = self.driver.prepare_trigger()
        writer.apply_steps([StepValue(StepParameter.FREQUENCY, 2, 2000), StepValue(StepParameter.PHASE, 1, 45)])
        self.assertEqual(list(self.generator.commands), [':COUP:STAT?', ':SOUR1:FUNC?', ':SOUR2:FUNC?', ':SOUR2:FREQ 2000;:SOUR1:PHAS 45'])
        self.assertEqual(self.generator.channels[1]['phase_deg'], 45)
        self.assertEqual(self.generator.channels[2]['frequency_hz'], 2000)

    def test_coupling_or_unparseable_status_blocks_arming_before_ni_start(self):
        for reply in ('FREQ:ON,PHASE:OFF,AMPL:OFF', 'FREQ:OFF,PHASE:ON,AMPL:OFF',
                      'FREQ:OFF,PHASE:OFF,AMPL:ON', 'garbage', 'FREQ:OFF,PHASE:OFF'):
            with self.subTest(reply=reply):
                self.generator.values['COUP:STAT'] = reply
                counter = Counter()
                controller = TriggerStepController(counter, self.driver, StepParameter.FREQUENCY, 2, [1000])
                with self.assertRaises(ValueError):
                    controller.start()
                self.assertEqual(counter.starts, 0)
                self.assertEqual(controller.snapshot().applied_count, 0)
                self.assertEqual(controller.snapshot().state, ControllerState.ERROR)

    def test_coupled_manual_settings_fail_without_disabling_global_state(self):
        self.generator.values['COUP:STAT'] = 'FREQ:ON,PHASE:OFF,AMPL:OFF'
        original = self.generator.snapshot().channels
        with self.assertRaisesRegex(ValueError, '耦合'):
            self.driver.configure_basic(2, BasicWaveform('SIN', 2500, .8))
        self.assertEqual(self.generator.snapshot().channels, original)
        self.assertEqual(self.generator.values['COUP:STAT'], 'FREQ:ON,PHASE:OFF,AMPL:OFF')

    def test_invalid_point_is_rejected_before_any_write(self):
        writer = self.driver.prepare_trigger()
        self.generator.commands.clear()
        with self.assertRaises(ValueError):
            writer.apply_steps([StepValue(StepParameter.FREQUENCY, 2, 2000), StepValue(StepParameter.PHASE, 3, 90)])
        self.assertEqual(list(self.generator.commands), [])

    def test_explicit_output_off_does_not_require_coupling_query(self):
        self.generator.values['COUP:STAT'] = 'FREQ:ON,PHASE:OFF,AMPL:OFF'
        self.generator.channels[1]['output'] = True
        self.generator.channels[2]['output'] = True
        self.generator.commands.clear()
        self.driver.set_output(2, False)
        self.assertTrue(self.generator.channels[1]['output'])
        self.assertFalse(self.generator.channels[2]['output'])
        self.assertEqual(list(self.generator.commands), [':OUTP2 OFF'])

    def test_channel_settings_rejects_foreign_and_embedded_global_headers(self):
        for command in (':SOUR1:FREQ 123', ':SOUR2:FREQ 123;*RST', '*RST', ':SOUR2:FREQ?'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                self.driver.write_channel_settings(2, [command])

    def test_failed_trigger_transport_is_not_replayed_or_counted(self):
        class Backend:
            def __init__(self):
                self.writes = []
            def query(self, command):
                if command in {':SOUR1:FUNC?', ':SOUR2:FUNC?'}: return 'SIN'
                return 'RIGOL,DG4162,TEST,1' if command == '*IDN?' else 'FREQ:OFF,PHASE:OFF,AMPL:OFF'
            def write(self, command):
                self.writes.append(command)
                raise OSError('uncertain acceptance')
            def close(self):
                pass
        backend = Backend()
        opens = []
        def factory(*args):
            opens.append(True)
            return backend
        driver = DG4000Driver(ScpiSession('TEST', backend_factory=factory))
        driver.connect()
        counter = Counter()
        controller = TriggerStepController(counter, driver, StepParameter.FREQUENCY, 2, [1000])
        controller.start()
        counter.count = 2
        deadline = time.monotonic()+2
        while controller.snapshot().state != ControllerState.ERROR and time.monotonic() < deadline:
            time.sleep(.002)
        controller.stop()
        self.assertEqual(controller.snapshot().state, ControllerState.ERROR)
        self.assertEqual(controller.snapshot().applied_count, 0)
        self.assertEqual(backend.writes, [':SOUR2:FREQ 1000'])
        self.assertEqual(len(opens), 1)

    def test_recovered_manual_connection_rechecks_coupling_before_retry(self):
        class Backend:
            def __init__(self, coupled):
                self.coupled = coupled
                self.writes = []
            def query(self, command):
                if command == '*IDN?': return 'RIGOL,DG4162,TEST,1'
                return 'FREQ:ON,PHASE:OFF,AMPL:OFF' if self.coupled else 'FREQ:OFF,PHASE:OFF,AMPL:OFF'
            def write(self, command):
                self.writes.append(command)
                raise OSError('USB loss')
            def close(self): pass
        backends = [Backend(False), Backend(True)]
        driver = DG4000Driver(ScpiSession('TEST', backend_factory=lambda *_: backends.pop(0)))
        driver.connect()
        replacement = backends[0]
        with self.assertRaisesRegex(ValueError, '耦合'):
            driver.set_output_load(2, 50)
        self.assertEqual(replacement.writes, [])

    def test_multi_device_preflight_failure_starts_no_ni_or_parameter_write(self):
        events = []
        class Writer:
            def __init__(self, name, fail=False):
                self.name, self.fail = name, fail
            def prepare_trigger(self):
                events.append(self.name + ':prepare')
                if self.fail: raise ValueError('coupled device')
                return self
            def apply_steps(self, values):
                events.append(self.name + ':write')
        counter = Counter()
        points = generate_linkage_points([
            LinkageAxis('a', StepParameter.FREQUENCY, 2, 'Hz', 1000, 100, 2),
            LinkageAxis('b', StepParameter.PHASE, 2, 'deg', 0, 10, 2),
        ], ScanMode.SYNCHRONOUS)
        controller = MultiDeviceTriggerController(counter, {'a': Writer('a'), 'b': Writer('b', True)}, points)
        with self.assertRaisesRegex(ValueError, 'coupled'):
            controller.start()
        self.assertEqual(counter.starts, 0)
        self.assertEqual(events, ['a:prepare', 'b:prepare'])
        self.assertEqual(controller.snapshot().applied_count, 0)

    def test_cancel_queued_start_does_not_read_or_arm_hardware(self):
        counter = Counter()
        controller = TriggerStepController(counter, self.driver, StepParameter.FREQUENCY, 2, [1000])
        self.generator.commands.clear()
        controller.request_stop()
        controller.start()
        self.assertEqual(counter.starts, 0)
        self.assertEqual(list(self.generator.commands), [])
        self.assertEqual(controller.snapshot().state, ControllerState.STOPPED)

    def test_cancel_during_counter_open_closes_it_without_starting_writer(self):
        entered, release = threading.Event(), threading.Event()
        class SlowCounter(Counter):
            def __init__(self):
                super().__init__()
                self.stops = 0
            def start(self):
                super().start()
                entered.set()
                release.wait(2)
            def stop(self):
                self.stops += 1
        counter = SlowCounter()
        controller = TriggerStepController(counter, self.driver, StepParameter.FREQUENCY, 2, [1000])
        worker = threading.Thread(target=controller.start)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            controller.request_stop()
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(counter.stops, 1)
        self.assertEqual(controller.snapshot().applied_count, 0)
        self.assertEqual(controller.snapshot().state, ControllerState.STOPPED)
