import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6.QtWidgets import QMessageBox

from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.scpi import ScpiSession
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository, DuplicateDeviceError
from rigol_control.ui.main_window import DeviceCenterWindow


class ResourceBackend:
    def __init__(self):
        self.close_entered = threading.Event()
        self.close_release = None
        self.closed = False

    def query(self, command):
        return "RIGOL,DG4162,SERIAL,1" if command == "*IDN?" else "0"

    def write(self, command):
        pass

    def close(self):
        self.close_entered.set()
        if self.close_release is not None:
            self.close_release.wait(2)
        self.closed = True


class PhysicalResourceLeaseTests(unittest.TestCase):
    def setUp(self):
        self.dg = DeviceRecord.create(kind=DeviceKind.DG4000, name="DG", resource="USB0::DG::INSTR")
        self.alias = DeviceRecord.create(kind=DeviceKind.GENERIC_SCPI, name="raw alias", resource="usb0::dg::instr")
        self.backends = []

        def backend_factory(*_args):
            backend = ResourceBackend()
            self.backends.append(backend)
            return backend

        self.manager = DeviceConnectionManager(
            session_factory=lambda resource: ScpiSession(resource, backend_factory=backend_factory)
        )
        self.addCleanup(self.manager.close_all)

    def test_idle_alias_cannot_connect_or_open_a_second_backend(self):
        self.manager.connect(self.dg)
        self.manager.session_for(self.alias)
        with self.assertRaisesRegex(DeviceBusyError, "另一设备记录连接"):
            self.manager.connect(self.alias)
        self.assertEqual(len(self.backends), 1)
        self.assertTrue(self.manager.is_connected(self.dg))
        self.assertFalse(self.manager.is_connected(self.alias))

    def test_probe_cannot_open_a_parallel_alias_session(self):
        self.manager.connect(self.dg)
        probe_calls = []
        service = SimpleNamespace(probe=lambda record: probe_calls.append(record))
        with self.assertRaises(DeviceBusyError):
            self.manager.probe(self.alias, service)
        self.assertFalse(probe_calls)
        result = self.manager.probe(self.dg, service)
        self.assertEqual(result.identity, "RIGOL,DG4162,SERIAL,1")
        self.assertFalse(probe_calls)

    def test_completed_disconnect_cannot_be_followed_by_a_stale_scan_acquisition(self):
        self.manager.connect(self.dg)
        self.manager.disconnect(self.dg)
        with self.assertRaisesRegex(DeviceBusyError, "连接已改变"):
            self.manager.acquire_devices("scan", [self.dg.id])
        self.assertFalse(self.manager.active_leases())

    def test_siglent_owner_can_establish_and_recover_its_own_communication_session(self):
        record = self.dg.updated(kind=DeviceKind.SIGLENT_SSG)
        self.manager.session_for(record)
        owner = f"siglent-native:{record.id}"
        self.manager.acquire_devices(owner, [record.id])
        try:
            with self.assertRaises(DeviceBusyError):
                self.manager.connect(record)
            self.manager.connect(record, owner=owner)
            self.assertTrue(self.manager.is_connected(record))
            self.manager.session_for(record).disconnect()
            self.manager.info(record)
            self.manager.connect(record, owner=owner)
            self.assertEqual(len(self.backends), 2)
            self.assertTrue(self.manager.is_connected(record))
        finally:
            self.manager.release_devices(owner)

    def test_alias_shares_lease_and_cannot_disconnect_or_replace_leased_resource(self):
        self.manager.connect(self.dg)
        self.manager.session_for(self.alias)
        self.manager.acquire_devices("scan", [self.dg.id])
        try:
            self.assertEqual(self.manager.lease_owner(self.alias.id), "scan")
            for action in (
                lambda: self.manager.assert_manual_access(self.alias),
                lambda: self.manager.disconnect(self.alias),
                lambda: self.manager.acquire_devices("other", [self.alias.id]),
                lambda: self.manager.session_for(self.dg.updated(resource="TCPIP0::NEW::INSTR")),
                self.manager.close_all,
            ):
                with self.assertRaises(DeviceBusyError):
                    action()
            self.assertTrue(self.manager.is_connected(self.dg))
            self.assertEqual(self.manager.lease_owner(self.dg.id), "scan")
        finally:
            self.manager.release_devices("scan")

    def test_one_task_cannot_address_a_resource_through_two_aliases(self):
        self.manager.session_for(self.dg)
        self.manager.session_for(self.alias)
        with self.assertRaisesRegex(DeviceBusyError, "多条设备记录"):
            self.manager.acquire_devices("scan", [self.dg.id, self.alias.id])
        self.assertFalse(self.manager.active_leases())

    def test_disconnect_reservation_rejects_a_racing_scan_without_holding_manager_lock(self):
        self.manager.connect(self.dg)
        backend = self.backends[0]
        backend.close_release = threading.Event()
        worker = threading.Thread(target=lambda: self.manager.disconnect(self.dg))
        worker.start()
        try:
            self.assertTrue(backend.close_entered.wait(1))
            self.assertTrue(self.manager.active_transitions())
            with self.assertRaises(DeviceBusyError):
                self.manager.acquire_devices("racing-scan", [self.dg.id])
            # This status query must finish while backend.close is still blocked.
            checked = threading.Event()
            probe = threading.Thread(target=lambda: (self.manager.info(self.dg), checked.set()))
            probe.start()
            self.assertTrue(checked.wait(.5))
            probe.join(1)
        finally:
            backend.close_release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.manager.is_connected(self.dg))
        self.assertFalse(self.manager.active_transitions())

    def test_connect_reservation_blocks_scans_and_alias_opens_during_slow_backend_creation(self):
        entered, release = threading.Event(), threading.Event()
        backends = []

        def create_backend(*_args):
            entered.set()
            release.wait(2)
            backend = ResourceBackend()
            backends.append(backend)
            return backend

        manager = DeviceConnectionManager(
            session_factory=lambda resource: ScpiSession(resource, backend_factory=create_backend)
        )
        self.addCleanup(manager.close_all)
        worker = threading.Thread(target=lambda: manager.connect(self.dg))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(DeviceBusyError):
                manager.acquire_devices("racing-scan", [self.dg.id])
            with self.assertRaises(DeviceBusyError):
                manager.connect(self.alias)
            self.assertTrue(manager.active_transitions())
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(backends), 1)
        self.assertTrue(manager.is_connected(self.dg))

    def test_close_all_blocks_new_acquisition_until_backend_shutdown_finishes(self):
        self.manager.connect(self.dg)
        backend = self.backends[0]
        backend.close_release = threading.Event()
        worker = threading.Thread(target=self.manager.close_all)
        worker.start()
        try:
            self.assertTrue(backend.close_entered.wait(1))
            with self.assertRaises(DeviceBusyError):
                self.manager.acquire_devices("racing-scan", [self.dg.id])
            with self.assertRaises(DeviceBusyError):
                self.manager.connect(self.alias)
            self.assertTrue(self.manager.active_transitions())
        finally:
            backend.close_release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.manager.active_transitions())

    def test_repository_rejects_new_cross_kind_alias_but_keeps_legacy_entries_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = DeviceRepository(Path(folder))
            repository.save(self.dg)
            with self.assertRaises(DuplicateDeviceError):
                repository.save(self.alias)
            repository._write([self.dg, self.alias])  # A configuration from an older release.
            self.assertEqual(len(repository.list()), 2)
            repository.save(self.dg.mark_opened())
            self.assertEqual(len(repository.list()), 2)

    def test_refused_window_close_keeps_home_tracking_and_cannot_shutdown_running_device(self):
        self.manager.connect(self.dg)
        self.manager.acquire_devices("scan", [self.dg.id])
        try:
            with tempfile.TemporaryDirectory() as folder:
                repository = DeviceRepository(Path(folder))
                repository.save(self.dg)
                view = SimpleNamespace(close=lambda: False)
                home = SimpleNamespace(
                    repository=repository,
                    open_windows={self.dg.id: view},
                    connection_manager=self.manager,
                    parameter_repository=DeviceParameterRepository(Path(folder)),
                    _pending_connections=set(),
                    statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
                )
                with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
                    DeviceCenterWindow.delete_device(home, self.dg.id)
                self.assertIs(home.open_windows[self.dg.id], view)
                self.assertIsNotNone(repository.get(self.dg.id))
                with self.assertRaises(DeviceBusyError):
                    self.manager.close_all()
                self.assertTrue(self.manager.is_connected(self.dg))
        finally:
            self.manager.release_devices("scan")

    def test_connect_requests_during_one_probe_are_coalesced_and_delivered_once(self):
        requests = []
        card = SimpleNamespace(
            record=self.dg,
            set_connection_state=lambda *_args, **_kwargs: None,
            set_probe_result=lambda _result: None,
        )
        home = SimpleNamespace(
            repository=SimpleNamespace(get=lambda _id: self.dg),
            cards={self.dg.id: card},
            connection_tasks={},
            probe_tasks={self.dg.id: object()},
            _pending_connections=set(),
            connection_manager=self.manager,
            statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
        )
        for _ in range(20):
            DeviceCenterWindow.connect_device(home, self.dg.id)
        self.assertEqual(home._pending_connections, {self.dg.id})
        self.assertFalse(home.connection_tasks)
        home.connect_device = requests.append
        DeviceCenterWindow._probe_completed(home, self.dg.id, object())
        DeviceCenterWindow._probe_completed(home, self.dg.id, object())
        self.assertEqual(requests, [self.dg.id])
        self.assertFalse(home._pending_connections)

    def test_editing_resource_preserves_refused_window_and_discards_accepted_old_window(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = DeviceRepository(Path(folder))
            repository.save(self.dg)
            self.manager.connect(self.dg)
            view = SimpleNamespace(close=lambda: False)
            home = SimpleNamespace(
                repository=repository,
                open_windows={self.dg.id: view},
                connection_manager=self.manager,
                reload_devices=lambda: None,
                statusBar=lambda: SimpleNamespace(showMessage=lambda *_args: None),
            )
            changed = self.dg.updated(resource="TCPIP0::NEW::INSTR")
            DeviceCenterWindow._save_dialog_record(home, changed)
            self.assertIs(home.open_windows[self.dg.id], view)
            self.assertEqual(repository.get(self.dg.id).resource, self.dg.resource)
            self.assertTrue(self.manager.is_connected(self.dg))
            view.close = lambda: True
            DeviceCenterWindow._save_dialog_record(home, changed)
            self.assertNotIn(self.dg.id, home.open_windows)
            self.assertEqual(repository.get(self.dg.id).resource, changed.resource)
            self.assertFalse(self.manager.is_connected(self.dg))


if __name__ == "__main__":
    unittest.main()
