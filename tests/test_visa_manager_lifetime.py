import threading
import unittest
from unittest.mock import patch

from rigol_control.drivers.scpi import ScpiConnectionError, ScpiSession, VisaBackend
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.services import DeviceDiscoveryService, DeviceProbeService, DiscoveryError, ProbeState


class Instrument:
    def __init__(self, resource, *, property_failure=False, query_failure=False):
        self.resource = resource
        self.closed = False
        self.close_count = 0
        self.property_failure = property_failure
        self.query_failure = query_failure
        self._timeout = 5000

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value):
        if self.property_failure:
            raise RuntimeError('SIMULATED_ATTRIBUTE_SETUP_FAILED')
        self._timeout = value

    def query(self, command):
        if self.closed:
            raise RuntimeError('SIMULATED_INVALID_SESSION')
        if self.query_failure:
            raise TimeoutError('SIMULATED_QUERY_TIMEOUT')
        return 'RIGOL,DG4162,SIMULATED,1.0' if command == '*IDN?' else '1000'

    def close(self):
        if not self.closed:
            self.close_count += 1
            self.closed = True


class SharedManager:
    """Model PyVISA's singleton and its close-all-created-resources contract."""

    def __init__(self):
        self.resources = []
        self.closed = False
        self.close_count = 0
        self.discovery_failure = False
        self.open_failure = False
        self.property_failure = False
        self.query_failure = False
        self.close_entered = None
        self.close_release = None
        self.close_failure = False

    def open_resource(self, resource, **kwargs):
        if self.closed:
            raise RuntimeError('SIMULATED_MANAGER_CLOSED')
        if self.open_failure:
            raise RuntimeError('SIMULATED_OPEN_FAILED')
        instrument = Instrument(resource, property_failure=self.property_failure,
                                query_failure=self.query_failure)
        self.resources.append(instrument)
        return instrument

    def list_resources(self):
        if self.discovery_failure:
            raise RuntimeError('SIMULATED_DISCOVERY_FAILED')
        return ('USB0::SIM_A::INSTR', 'USB0::SIM_B::INSTR')

    def close(self):
        self.close_count += 1
        if self.close_entered is not None:
            self.close_entered.set()
            if not self.close_release.wait(3):
                raise TimeoutError('SIMULATED_CLOSE_WAIT_EXPIRED')
        self.closed = True
        for instrument in self.resources:
            instrument.close()
        if self.close_failure:
            raise RuntimeError('SIMULATED_MANAGER_CLOSE_FAILED')


class ManagerFactory:
    def __init__(self):
        self.managers = []
        self.called = threading.Event()

    def __call__(self):
        self.called.set()
        if not self.managers or self.managers[-1].closed:
            self.managers.append(SharedManager())
        return self.managers[-1]


class VisaManagerLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.factory = ManagerFactory()
        self.patch = patch('pyvisa.ResourceManager', side_effect=self.factory)
        self.patch.start()
        self.sessions = []
        self.backends = []

    def tearDown(self):
        for session in self.sessions:
            try:
                session.disconnect()
            except Exception:
                pass
        for backend in self.backends:
            try:
                backend.close()
            except Exception:
                pass
        self.patch.stop()

    def session(self, suffix):
        session = ScpiSession(f'USB0::SIM_{suffix}::INSTR')
        self.sessions.append(session)
        session.connect()
        return session

    def backend(self, suffix):
        backend = VisaBackend(f'USB0::SIM_{suffix}::INSTR', 5000)
        self.backends.append(backend)
        return backend

    @staticmethod
    def record():
        return DeviceRecord(id='probe-record', name='Probe', kind=DeviceKind.DG4000,
                            resource='USB0::SIM_PROBE::INSTR', connection='USB VISA')

    def assert_alive(self, session):
        self.assertEqual(session.query(':SOUR1:FREQ?'), '1000')

    def test_disconnecting_one_device_preserves_another_and_last_release_closes_manager(self):
        a, b = self.session('A'), self.session('B')
        manager, = self.factory.managers
        b.disconnect()
        self.assert_alive(a)
        self.assertEqual(manager.close_count, 0)
        a.disconnect()
        self.assertEqual(manager.close_count, 1)
        a.disconnect()
        self.assertEqual(manager.close_count, 1)

    def test_discovery_success_preserves_existing_connection(self):
        a = self.session('A')
        found = DeviceDiscoveryService()._discover_visa()
        self.assertEqual(len(found), 2)
        self.assert_alive(a)
        self.assertEqual(self.factory.managers[0].close_count, 0)

    def test_discovery_failure_preserves_existing_connection(self):
        a = self.session('A')
        self.factory.managers[0].discovery_failure = True
        with self.assertRaisesRegex(DiscoveryError, 'SIMULATED_DISCOVERY_FAILED'):
            DeviceDiscoveryService()._discover_visa()
        self.assert_alive(a)

    def test_standalone_discovery_releases_its_last_manager_lease(self):
        self.assertEqual(len(DeviceDiscoveryService()._discover_visa()), 2)
        manager, = self.factory.managers
        self.assertEqual(manager.close_count, 1)

    def test_standalone_probe_closes_temporary_resource_and_last_manager_lease(self):
        self.assertEqual(DeviceProbeService().probe(self.record()).state, ProbeState.ONLINE)
        manager, = self.factory.managers
        self.assertEqual(manager.close_count, 1)
        self.assertTrue(manager.resources[0].closed)

    def test_probe_success_closes_only_its_temporary_resource(self):
        a = self.session('A')
        result = DeviceProbeService().probe(self.record())
        self.assertEqual(result.state, ProbeState.ONLINE)
        manager = self.factory.managers[0]
        self.assertTrue(manager.resources[-1].closed)
        self.assertEqual(manager.close_count, 0)
        self.assert_alive(a)

    def test_probe_timeout_closes_only_its_temporary_resource(self):
        a = self.session('A')
        manager = self.factory.managers[0]
        manager.query_failure = True
        result = DeviceProbeService().probe(self.record())
        self.assertEqual(result.state, ProbeState.OFFLINE)
        self.assertIn('SIMULATED_QUERY_TIMEOUT', result.message)
        self.assertTrue(manager.resources[-1].closed)
        self.assert_alive(a)

    def test_probe_open_failure_preserves_existing_connection(self):
        a = self.session('A')
        self.factory.managers[0].open_failure = True
        result = DeviceProbeService().probe(self.record())
        self.assertEqual(result.state, ProbeState.OFFLINE)
        self.assertIn('SIMULATED_OPEN_FAILED', result.message)
        self.assert_alive(a)

    def test_failed_open_preserves_existing_connection(self):
        a = self.session('A')
        self.factory.managers[0].open_failure = True
        with self.assertRaisesRegex(ScpiConnectionError, 'SIMULATED_OPEN_FAILED'):
            self.backend('B')
        self.assert_alive(a)

    def test_failed_attribute_setup_closes_only_new_resource(self):
        a = self.session('A')
        manager = self.factory.managers[0]
        manager.property_failure = True
        with self.assertRaisesRegex(ScpiConnectionError, 'SIMULATED_ATTRIBUTE_SETUP_FAILED'):
            self.backend('B')
        self.assertTrue(manager.resources[-1].closed)
        self.assert_alive(a)

    def test_failed_attribute_setup_without_other_users_closes_manager(self):
        manager = self.factory()
        manager.property_failure = True
        with self.assertRaisesRegex(ScpiConnectionError, 'SIMULATED_ATTRIBUTE_SETUP_FAILED'):
            self.backend('A')
        self.assertTrue(manager.resources[-1].closed)
        self.assertEqual(manager.close_count, 1)

    def test_failed_open_reports_original_error_when_manager_release_also_fails(self):
        manager = self.factory()
        manager.open_failure = manager.close_failure = True
        with self.assertRaisesRegex(ScpiConnectionError, 'SIMULATED_OPEN_FAILED'):
            self.backend('A')

    def test_repeated_search_probe_and_reconnect_do_not_accumulate_or_close_live_resources(self):
        a = self.session('A')
        manager = self.factory.managers[0]
        for index in range(100):
            b = self.session(f'B_{index}')
            self.assert_alive(b)
            b.disconnect()
            self.assertEqual(len(DeviceDiscoveryService()._discover_visa()), 2)
            self.assertEqual(DeviceProbeService().probe(self.record()).state, ProbeState.ONLINE)
            self.assert_alive(a)
            self.assertEqual(sum(not instrument.closed for instrument in manager.resources), 1)
            self.assertEqual(manager.close_count, 0)
        a.disconnect()
        self.assertEqual(manager.close_count, 1)

    def test_backend_close_is_idempotent(self):
        backend = self.backend('A')
        manager, = self.factory.managers
        backend.close()
        backend.close()
        self.assertEqual(manager.resources[0].close_count, 1)
        self.assertEqual(manager.close_count, 1)

    def test_concurrent_last_releases_close_manager_once(self):
        a, b = self.backend('A'), self.backend('B')
        barrier = threading.Barrier(3)
        errors = []
        def release(backend):
            try:
                barrier.wait(2)
                backend.close()
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=release, args=(backend,)) for backend in (a, b)]
        for worker in workers:
            worker.start()
        barrier.wait(2)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.factory.managers[0].close_count, 1)

    def test_another_device_can_open_and_close_during_a_blocked_query(self):
        a, b = self.backend('A'), self.backend('B')
        entered, release, independent_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original_query = a._instrument.query
        def blocked_query(command):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('SIMULATED_QUERY_WAIT_EXPIRED')
            return original_query(command)
        def read_a():
            try:
                a.query('*IDN?')
            except Exception as exc:
                errors.append(exc)
        def independent_io():
            try:
                b.close()
                c = self.backend('C')
                c.query('*IDN?')
                c.close()
                independent_done.set()
            except Exception as exc:
                errors.append(exc)
        with patch.object(a._instrument, 'query', side_effect=blocked_query):
            reader, other = threading.Thread(target=read_a), threading.Thread(target=independent_io)
            reader.start()
            try:
                self.assertTrue(entered.wait(1))
                other.start()
                self.assertTrue(independent_done.wait(1), 'Independent device I/O waited for a global query lock')
            finally:
                release.set()
                reader.join(3)
                if other.ident is not None:
                    other.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(self.factory.managers[0].close_count, 0)

    def test_new_acquire_waits_for_last_close_to_finish_before_reusing_singleton(self):
        a = self.backend('A')
        manager = self.factory.managers[0]
        manager.close_entered, manager.close_release = threading.Event(), threading.Event()
        acquired, errors = threading.Event(), []
        def open_next():
            try:
                backend = self.backend('B')
                backend.query('*IDN?')
                acquired.set()
            except Exception as exc:
                errors.append(exc)
        closer = threading.Thread(target=a.close)
        opener = threading.Thread(target=open_next)
        closer.start()
        try:
            self.assertTrue(manager.close_entered.wait(1))
            self.factory.called.clear()
            opener.start()
            self.assertFalse(self.factory.called.wait(.1), 'ResourceManager reused during its last close')
        finally:
            manager.close_release.set()
            closer.join(3)
            if opener.ident is not None:
                opener.join(3)
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())
        self.assertEqual(len(self.factory.managers), 2)

    def test_manager_close_failure_does_not_leave_global_acquire_blocked(self):
        a = self.backend('A')
        self.factory.managers[0].close_failure = True
        with self.assertRaisesRegex(RuntimeError, 'SIMULATED_MANAGER_CLOSE_FAILED'):
            a.close()
        b = self.backend('B')
        self.assertIn('DG4162', b.query('*IDN?'))
        a.close()


if __name__ == '__main__':
    unittest.main()
