import unittest

from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.services import ProbeResult, ProbeState


class FakeSession:
    def __init__(self, resource: str) -> None:
        self.resource = resource
        self.is_connected = False
        self.connect_count = 0
        self.disconnect_count = 0

    def connect(self) -> str:
        self.connect_count += 1
        self.is_connected = True
        return f"RIGOL,DG4162,{self.resource},1.0"

    def disconnect(self) -> None:
        self.disconnect_count += 1
        self.is_connected = False

    def write(self, command: str, *, recover: bool = False) -> None:
        del recover
        if not self.is_connected:
            raise RuntimeError("not connected")

    def write_many(self, commands, *, recover: bool = False) -> None:
        for command in commands:
            self.write(command, recover=recover)

    def query(self, command: str, *, recover: bool = False) -> str:
        del command, recover
        if not self.is_connected:
            raise RuntimeError("not connected")
        return "RIGOL,DG4162,TEST,1.0"


class FakeProbeService:
    def probe(self, record):
        return ProbeResult(ProbeState.ONLINE, "已连接", "NI USB-6363 · 1234")


class DeviceConnectionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = []

        def factory(resource):
            session = FakeSession(resource)
            self.sessions.append(session)
            return session

        self.manager = DeviceConnectionManager(
            session_factory=factory,
            probe_service=FakeProbeService(),
        )

    def test_scpi_session_is_shared_connected_and_disconnected(self) -> None:
        record = DeviceRecord.create(
            kind=DeviceKind.DG4000,
            name="DG4000",
            resource="USB0::DG::INSTR",
        )
        driver = self.manager.dg_driver_for(record)
        self.assertIs(driver.session, self.manager.session_for(record))
        self.assertFalse(self.manager.is_connected(record))

        result = self.manager.connect(record)
        self.assertTrue(result.connected)
        self.assertTrue(driver.is_connected)
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(self.sessions[0].connect_count, 1)

        self.manager.connect(record)
        self.assertEqual(self.sessions[0].connect_count, 1)
        self.manager.disconnect(record)
        self.assertFalse(driver.is_connected)
        self.assertEqual(self.sessions[0].disconnect_count, 1)

    def test_changed_resource_replaces_old_session(self) -> None:
        record = DeviceRecord.create(
            kind=DeviceKind.GENERIC_SCPI,
            name="SCPI",
            resource="USB0::OLD::INSTR",
        )
        self.manager.connect(record)
        changed = record.updated(resource="TCPIP0::NEW::INSTR")
        replacement = self.manager.session_for(changed)
        self.assertEqual(replacement.resource, "TCPIP0::NEW::INSTR")
        self.assertEqual(self.sessions[0].disconnect_count, 1)
        self.assertEqual(len(self.sessions), 2)

    def test_ni_connect_is_logical_and_disconnects_cleanly(self) -> None:
        record = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="NI",
            resource="Dev1",
        )
        result = self.manager.connect(record)
        self.assertTrue(result.connected)
        self.assertIn("NI USB-6363", result.identity)
        self.assertTrue(self.manager.is_connected(record))
        self.manager.disconnect(record)
        self.assertFalse(self.manager.is_connected(record))

    def test_virtual_vaunix_driver_is_shared_connected_and_disconnected(self) -> None:
        record = DeviceRecord.create(
            kind=DeviceKind.VAUNIX_LMS,
            name="Vaunix BLX",
            resource=VIRTUAL_VAUNIX_RESOURCE,
            connection="内置模拟器",
        )
        driver = self.manager.vaunix_device_for(record)
        self.assertIs(driver, self.manager.vaunix_device_for(record))
        self.assertFalse(self.manager.is_connected(record))
        result = self.manager.connect(record)
        self.assertTrue(result.connected)
        self.assertIn("BLX-403", result.identity)
        self.assertTrue(driver.is_connected)
        self.assertTrue(self.manager.is_connected(record))

        self.manager.disconnect(record)
        self.assertFalse(driver.is_connected)
        self.assertFalse(self.manager.is_connected(record))

    def test_siglent_native_lease_is_not_labelled_as_linkage(self) -> None:
        record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG, name="SSG", resource="USB0::SSG::INSTR")
        owner = f"siglent-native:{record.id}"
        self.manager.acquire_devices(owner, [record.id])
        with self.assertRaisesRegex(DeviceBusyError, "SSG 通信或原生扫描"):
            self.manager.disconnect(record)
        self.manager.release_devices(owner)


if __name__ == "__main__":
    unittest.main()
