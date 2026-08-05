import unittest

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


if __name__ == "__main__":
    unittest.main()
