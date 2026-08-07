import unittest
from unittest.mock import patch

from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.services import DeviceDiscoveryService, DeviceProbeService, ProbeState


class DeviceDiscoveryTests(unittest.TestCase):
    def test_visa_connection_is_inferred_from_resource_prefix(self) -> None:
        cases = {
            "USB0::0x1AB1::INSTR": "USB VISA",
            "TCPIP0::192.168.1.10::INSTR": "TCP/IP VISA",
            "GPIB0::8::INSTR": "GPIB VISA",
            "ASRL3::INSTR": "串口 VISA",
            "PXI0::1::INSTR": "VISA",
        }
        for resource, expected in cases.items():
            with self.subTest(resource=resource):
                self.assertEqual(
                    DeviceDiscoveryService.connection_for_visa(resource),
                    expected,
                )

    def test_vaunix_discovery_includes_virtual_and_serial_stable_hardware_resources(self) -> None:
        class Descriptor:
            resource = "VAUNIX::LMS::4210123"
            model = "LMS-421D"
            serial_number = 4210123

        class FakeApi:
            def enumerate_devices(self):
                return (Descriptor(),)

        with patch("rigol_control.services.VaunixLmsApi", return_value=FakeApi()):
            resources = DeviceDiscoveryService().discover(DeviceKind.VAUNIX_LMS)

        self.assertEqual(resources[0].resource, VIRTUAL_VAUNIX_RESOURCE)
        self.assertEqual(resources[1].resource, "VAUNIX::LMS::4210123")
        self.assertEqual(resources[1].connection, "Vaunix USB DLL")
        self.assertIn("LMS-421D", resources[1].identity)

    def test_vaunix_discovery_keeps_virtual_device_when_dll_is_unavailable(self) -> None:
        with patch("rigol_control.services.VaunixLmsApi", side_effect=OSError("missing")):
            resources = DeviceDiscoveryService().discover(DeviceKind.VAUNIX_LMS)

        self.assertEqual([item.resource for item in resources], [VIRTUAL_VAUNIX_RESOURCE])

    def test_vaunix_probe_only_enumerates_and_never_opens_device(self) -> None:
        class Descriptor:
            resource = "VAUNIX::LMS::4210123"
            model = "LMS-421D"
            serial_number = 4210123

        class FakeApi:
            dll_version = 0x010F

            def enumerate_devices(self):
                return (Descriptor(),)

        record = DeviceRecord.create(
            kind=DeviceKind.VAUNIX_LMS,
            name="Vaunix",
            resource="VAUNIX::LMS::4210123",
        )
        with patch("rigol_control.services.VaunixLmsApi", return_value=FakeApi()):
            result = DeviceProbeService().probe(record)

        self.assertEqual(result.state, ProbeState.ONLINE)
        self.assertIn("不会打开或关闭设备句柄", result.message)
        self.assertIn("DLL 1.15", result.identity)


if __name__ == "__main__":
    unittest.main()
