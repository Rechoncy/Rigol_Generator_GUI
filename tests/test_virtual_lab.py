import time
import unittest

from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import BasicWaveform, StepParameter
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.trigger import TriggerStepController
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE, VirtualLab


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class VirtualLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = VirtualLab()
        self.manager = DeviceConnectionManager(virtual_lab=self.lab)
        self.dg = DeviceRecord.create(
            kind=DeviceKind.DG4000,
            name="虚拟 DG4162",
            resource=VIRTUAL_DG_RESOURCE,
            connection="内置模拟器",
        )
        self.ni = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="虚拟 NI USB-6363",
            resource=VIRTUAL_NI_RESOURCE,
            connection="内置模拟器",
        )

    def test_virtual_devices_connect_without_vendor_drivers(self) -> None:
        dg_info = self.manager.connect(self.dg)
        ni_info = self.manager.connect(self.ni)
        self.assertIn("DG4162-VIRTUAL", dg_info.identity)
        self.assertIn("NI USB-6363 VIRTUAL", ni_info.identity)

        driver = self.manager.dg_driver_for(self.dg)
        driver.configure_basic(1, BasicWaveform("SIN", 12345, 2.5, 0.2, 30))
        driver.set_output(1, True)
        snapshot = self.lab.generator_snapshot(VIRTUAL_DG_RESOURCE)
        channel = snapshot.channels[0]
        self.assertEqual(channel.frequency_hz, 12345)
        self.assertEqual(channel.amplitude_vpp, 2.5)
        self.assertEqual(channel.phase_deg, 30)
        self.assertTrue(channel.output)

    def test_virtual_trigger_linkage_handles_batch_and_cycles(self) -> None:
        self.manager.connect(self.dg)
        self.manager.connect(self.ni)
        self.lab.set_scpi_delay(VIRTUAL_DG_RESOURCE, 1)
        counter = self.manager.counter_for(self.ni)
        controller = TriggerStepController(
            counter,
            self.manager.dg_driver_for(self.dg),
            StepParameter.PSK_PHASE,
            1,
            [10, 20, 30],
            monitor_poll_seconds=0.001,
        )
        controller.start()
        self.lab.emit_trigger(VIRTUAL_NI_RESOURCE, 8)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 8))
        snapshot = controller.snapshot()
        generator = self.lab.generator_snapshot(VIRTUAL_DG_RESOURCE)
        controller.stop()

        self.assertEqual(snapshot.received_count, 8)
        self.assertEqual(snapshot.backlog, 0)
        self.assertEqual(snapshot.current_index, 1)
        self.assertEqual(generator.channels[0].psk_phase_deg, 20)


if __name__ == "__main__":
    unittest.main()
