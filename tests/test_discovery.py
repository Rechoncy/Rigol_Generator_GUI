import unittest
from unittest.mock import patch

from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind
from rigol_control.services import DeviceDiscoveryService


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


if __name__ == "__main__":
    unittest.main()
