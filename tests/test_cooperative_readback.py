"""Optional readbacks yield to control work without replaying or returning partial data."""
from contextlib import contextmanager
from threading import Event, RLock, Thread
import time
import unittest
from unittest.mock import patch

from rigol_control.drivers.dg4000 import DG4000Driver
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.drivers.siglent_ssg import SiglentSsgDriver, VirtualSsgBackend
from rigol_control.drivers.vaunix_lms import (
    VaunixDeviceDescriptor, VaunixLmsDevice, VaunixLmsError, VirtualVaunixLmsDevice,
)
from rigol_control.readback import ReadbackDeferred
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession


class ReadbackApi:
    dll_version = 0x010F

    def __init__(self):
        self._lock = RLock()
        self.dll = self
        self.calls = []
        self.after_call = None
        self.values = {
            'fnLMS_GetMinFreqEx': 50_000_000, 'fnLMS_GetMaxFreqEx': 4_000_000_000,
            'fnLMS_GetMinPwr': -160, 'fnLMS_GetMaxPwr': 40,
            'fnLMS_GetDeviceStatus': 3, 'fnLMS_GetFrequencyEx': 600_000_000,
            'fnLMS_GetAbsPowerLevel': -40, 'fnLMS_GetRF_On': 0,
        }

    def enumerate_devices(self):
        return (VaunixDeviceDescriptor(7, 'LMS-TEST', 123, 3),)

    def __getattr__(self, name):
        def call(*args):
            self.calls.append(name)
            if self.after_call:
                self.after_call(name)
            return self.values.get(name, 0)
        return call


@contextmanager
def held_by_other_thread(lock):
    entered, release = Event(), Event()

    def hold():
        with lock:
            entered.set()
            release.wait(3)

    thread = Thread(target=hold)
    thread.start()
    if not entered.wait(1):
        release.set()
        thread.join(3)
        raise AssertionError('Test lock holder did not start')
    try:
        yield
    finally:
        release.set()
        thread.join(3)
        if thread.is_alive():
            raise AssertionError('Test lock holder did not stop')


class CooperativeReadbackTests(unittest.TestCase):
    def dg(self):
        generator = VirtualGenerator('SIM::COOPERATIVE_DG')
        generator.scpi_delay_ms = 0
        session = VirtualScpiSession(generator)
        driver = DG4000Driver(session)
        driver.connect()
        generator.commands.clear()
        self.addCleanup(driver.disconnect)
        return generator, session, driver

    def vaunix(self):
        api = ReadbackApi()
        device = VaunixLmsDevice('VAUNIX::LMS::123', api_factory=lambda: api)
        device.connect()
        api.calls.clear()
        self.addCleanup(device.disconnect)
        return api, device

    def test_dg_yields_after_each_native_query_without_followup_or_partial_result(self):
        generator, session, driver = self.dg()
        generation = session.connection_generation
        original = session.query
        for stop_after in range(0, 11):
            with self.subTest(stop_after=stop_after):
                calls = []
                ready = [stop_after != 0]

                def query(command, **kwargs):
                    calls.append((command, kwargs))
                    result = original(command, **kwargs)
                    if len(calls) == stop_after:
                        ready[0] = False
                    return result

                with patch.object(session, 'query', side_effect=query):
                    with self.assertRaises(ReadbackDeferred):
                        driver.read_output_parameters(should_continue=lambda: ready[0])
                self.assertEqual(len(calls), stop_after)
                self.assertTrue(all(kwargs == {'recover': False} for _, kwargs in calls))
                self.assertTrue(all(command.endswith('?') for command, _ in calls))
                self.assertEqual(session.connection_generation, generation)
                self.assertTrue(session.is_connected)

    def test_dg_level_fallback_yields_between_level_queries(self):
        _, session, driver = self.dg()
        original = session.query
        calls = []

        def query(command, **kwargs):
            calls.append(command)
            return '' if command.endswith(':VOLT:UNIT?') else original(command, **kwargs)

        with patch.object(session, 'query', side_effect=query):
            with self.assertRaises(ReadbackDeferred):
                driver.read_output_parameters(should_continue=lambda: len(calls) < 3)
        self.assertEqual(calls, [':SOUR1:VOLT:UNIT?', ':SOUR1:VOLT:HIGH?', ':SOUR1:VOLT:LOW?'])

    def test_ssg_yields_after_each_query_and_preserves_scan_evidence(self):
        backend = VirtualSsgBackend(clock=lambda: 0)
        session = ScpiSession('SIM::COOPERATIVE_SSG', backend_factory=lambda *_: backend)
        session.connect()
        self.addCleanup(session.disconnect)
        driver = SiglentSsgDriver(session)
        driver.identify()
        driver.scan_may_be_active = True
        driver.state = 'enabled'
        generation = session.connection_generation
        original = session.query
        for stop_after in range(0, 4):
            with self.subTest(stop_after=stop_after):
                calls = []

                def query(command, **kwargs):
                    calls.append((command, kwargs))
                    return original(command, **kwargs)

                with patch.object(session, 'query', side_effect=query):
                    with self.assertRaises(ReadbackDeferred):
                        driver.read_output_parameters(should_continue=lambda: len(calls) < stop_after)
                self.assertEqual(len(calls), stop_after)
                self.assertTrue(all(kwargs == {'recover': False} for _, kwargs in calls))
                self.assertEqual(session.connection_generation, generation)
                self.assertTrue(driver.scan_may_be_active)
                self.assertEqual(driver.state, 'enabled')

    def test_vaunix_yields_after_each_getter_without_sets_or_reconnect(self):
        api, device = self.vaunix()
        generation = device.connection_generation
        for stop_after in range(0, 6):
            with self.subTest(stop_after=stop_after):
                api.calls.clear()
                with self.assertRaises(ReadbackDeferred):
                    device.read_output_parameters(should_continue=lambda: len(api.calls) < stop_after)
                self.assertEqual(len(api.calls), stop_after)
                self.assertTrue(all(name.startswith('fnLMS_Get') for name in api.calls))
                self.assertEqual(device.connection_generation, generation)
                self.assertTrue(device.is_connected)

    def test_transport_errors_win_over_new_work_and_are_not_retried(self):
        _, session, driver = self.dg()
        calls = []

        def query(command, **kwargs):
            calls.append(command)
            raise TimeoutError('COOPERATIVE_REAL_TIMEOUT')

        with patch.object(session, 'query', side_effect=query):
            with self.assertRaisesRegex(RuntimeError, 'COOPERATIVE_REAL_TIMEOUT'):
                driver.read_output_parameters(should_continue=lambda: not calls)
        self.assertEqual(len(calls), 1)
        api, device = self.vaunix()
        api.values['fnLMS_GetDeviceStatus'] = 0xFFFF0002
        with self.assertRaisesRegex(VaunixLmsError, 'BAD_HID_IO'):
            device.read_output_parameters(should_continue=lambda: not api.calls)
        self.assertEqual(api.calls, ['fnLMS_GetDeviceStatus'])

    def test_virtual_vaunix_yields_after_simulated_getter_without_changing_settings(self):
        device = VirtualVaunixLmsDevice()
        device.connect()
        device.command_delay_ms = 0
        self.addCleanup(device.disconnect)
        before = device.snapshot()
        allowed = [True]
        original = device._delay

        def delay():
            original()
            allowed[0] = False

        with patch.object(device, '_delay', side_effect=delay):
            with self.assertRaises(ReadbackDeferred):
                device.read_output_parameters(should_continue=lambda: allowed[0])
        self.assertEqual(device.snapshot(), before)

    def test_try_transaction_does_not_wait_or_query_when_device_lock_is_held(self):
        generator, virtual_session, _ = self.dg()
        api, vaunix = self.vaunix()
        real_session = ScpiSession('no-backend-needed')
        virtual_vaunix = VirtualVaunixLmsDevice()
        for target in (real_session, virtual_session, vaunix, virtual_vaunix):
            with self.subTest(target=type(target).__name__):
                with held_by_other_thread(target._lock):
                    started = time.monotonic()
                    with target.try_transaction() as acquired:
                        self.assertFalse(acquired)
                    self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(generator.commands)
        self.assertFalse(api.calls)

    def test_vaunix_try_transaction_also_yields_to_shared_dll_lock(self):
        api, device = self.vaunix()
        with held_by_other_thread(api._lock):
            started = time.monotonic()
            with device.try_transaction() as acquired:
                self.assertFalse(acquired)
            self.assertLess(time.monotonic() - started, .2)
        self.assertFalse(api.calls)
        # A declined attempt must release the device lock too.
        with held_by_other_thread(device._lock):
            pass
        with device.try_transaction() as acquired:
            self.assertTrue(acquired)
            obtained = []

            def try_api():
                got = api._lock.acquire(blocking=False)
                obtained.append(got)
                if got:
                    api._lock.release()

            other = Thread(target=try_api)
            other.start()
            other.join(1)
            self.assertEqual(obtained, [False])

    def test_try_transaction_releases_locks_when_snapshot_yields(self):
        _, session, driver = self.dg()
        api, device = self.vaunix()
        for target, read in ((session, driver.read_output_parameters), (device, device.read_output_parameters)):
            with self.subTest(target=type(target).__name__):
                with self.assertRaises(ReadbackDeferred):
                    with target.try_transaction() as acquired:
                        self.assertTrue(acquired)
                        read(should_continue=lambda: False)
                with held_by_other_thread(target._lock):
                    pass
        with held_by_other_thread(api._lock):
            pass


if __name__ == '__main__':
    unittest.main()
