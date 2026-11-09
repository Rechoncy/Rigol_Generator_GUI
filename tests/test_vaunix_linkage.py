from dataclasses import replace
import unittest
from unittest.mock import patch

from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.drivers.vaunix_lms import (
    VaunixLmsDevice, VaunixLmsError, VirtualVaunixLmsDevice,
)
from rigol_control.linkage import LinkagePoint, LinkageStepValue, MultiDeviceTriggerController
from rigol_control.linkage_parameters import LinkageParameter, RfPowerStepValue
from rigol_control.trigger import ControllerState
from rigol_control.vaunix_linkage import VaunixLinkageWriter
from test_vaunix_trigger_dispatch import Counter, FakeApi, wait_until


class VaunixLinkageTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.real = VaunixLmsDevice("VAUNIX::LMS::123", api_factory=lambda: self.api)
        self.real.connect()
        self.addCleanup(self.real.disconnect)
        self.virtual = VirtualVaunixLmsDevice()
        self.virtual.command_delay_ms = 0
        self.virtual.connect()
        self.addCleanup(self.virtual.disconnect)
        self.virtual._caps = replace(self.real.capabilities, model="BLX-VIRTUAL")
        self.api.dll.reads.clear()
        self.frequency = StepValue(StepParameter.FREQUENCY, 1, 5e9, 5, "GHz")
        self.power = RfPowerStepValue(LinkageParameter.RF_POWER, 1, -10, -10, "dBm")
        self.values = (self.frequency, self.power)

    def test_preflight_and_prepare_do_not_query_or_change_output(self):
        writer = VaunixLinkageWriter(self.real)
        writer.validate_step_values(self.values)
        writer.prepare_trigger()
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual(self.api.dll.writes, [])
        self.assertEqual(self.api.dll.opened, [7])
        before = self.virtual.frequency_hz, self.virtual.power_dbm, self.virtual.rf_on
        virtual_writer = VaunixLinkageWriter(self.virtual)
        virtual_writer.validate_step_values(self.values)
        virtual_writer.prepare_trigger()
        self.assertEqual((self.virtual.frequency_hz, self.virtual.power_dbm, self.virtual.rf_on), before)

    def test_rf_power_is_explicitly_translated_to_existing_sdk_absolute_power(self):
        for device in (self.real, self.virtual):
            VaunixLinkageWriter(device).prepare_trigger().apply_steps(self.values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual((self.virtual.frequency_hz, self.virtual.power_dbm), (5e9, -10))
        self.assertFalse(self.virtual.rf_on)

    def test_direct_adapter_dispatch_also_uses_the_nonreplaying_prepared_writer(self):
        writer = VaunixLinkageWriter(self.real)
        with patch.object(self.real, "apply_steps", side_effect=AssertionError("Unsafe legacy dispatch")):
            writer.apply_steps(self.values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])

    def test_invalid_trailing_power_is_rejected_before_frequency_is_written(self):
        for device in (self.real, self.virtual):
            writer = VaunixLinkageWriter(device).prepare_trigger()
            before = self.virtual.frequency_hz, self.virtual.power_dbm
            with self.assertRaisesRegex(ValueError, "0.5"):
                writer.apply_steps((self.frequency, replace(self.power, value=-7.25)))
            self.assertEqual(self.api.dll.writes, [])
            self.assertEqual((self.virtual.frequency_hz, self.virtual.power_dbm), before)

    def test_power_must_be_explicit_dbm_and_never_reuses_voltage_amplitude(self):
        writer = VaunixLinkageWriter(self.real)
        for unit in (None, "Vpp", "mVpp", "mV", "W"):
            with self.subTest(unit=unit), self.assertRaisesRegex(ValueError, "dBm"):
                writer.validate_step_values((replace(self.power, unit_key=unit),))
        for parameter in (StepParameter.AMPLITUDE, StepParameter.PHASE,
                          StepParameter.PSK_PHASE, StepParameter.FSK_FREQUENCY):
            with self.subTest(parameter=parameter), self.assertRaises(ValueError):
                writer.validate_step_values((StepValue(parameter, 1, -10, -10, "dBm"),))
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual(self.api.dll.writes, [])

    def test_channel_duplicate_empty_and_invalid_frequency_unit_are_rejected(self):
        writer = VaunixLinkageWriter(self.real)
        cases = ((), (replace(self.frequency, channel=2),), (replace(self.power, channel=2),),
                 (self.frequency, self.frequency), (self.power, self.power),
                 (replace(self.frequency, unit_key="dBm"),))
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ValueError):
                writer.validate_step_values(values)
        self.assertEqual(self.api.dll.writes, [])

    def test_entire_table_can_be_validated_without_io_and_rejects_out_of_range_values(self):
        writer = VaunixLinkageWriter(self.real)
        for values in (self.values, (replace(self.frequency, value=6e9), replace(self.power, value=-7.5))):
            writer.validate_step_values(values)
        for item in (replace(self.frequency, value=0), replace(self.frequency, value=30e9),
                     replace(self.frequency, value=float("nan")), replace(self.frequency, value=float("inf")),
                     replace(self.power, value=-41), replace(self.power, value=11),
                     replace(self.power, value=float("nan")), replace(self.power, value=float("inf"))):
            with self.subTest(item=item), self.assertRaises(ValueError):
                writer.validate_step_values((item,))
        self.assertEqual(self.api.dll.reads, [])
        self.assertEqual(self.api.dll.writes, [])

    def test_frequency_encoding_matches_existing_real_and_virtual_sdk_path(self):
        values = (replace(self.frequency, value=5e9 + 6), replace(self.power, value=-7.5))
        for device in (self.real, self.virtual):
            VaunixLinkageWriter(device).prepare_trigger().apply_steps(values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_001), ("power", 7, -30)])
        self.assertEqual((self.virtual.frequency_hz, self.virtual.power_dbm), (5e9 + 10, -7.5))

    def test_reconnect_using_same_native_handle_invalidates_preflight_and_prepared_writers(self):
        for device in (self.real, self.virtual):
            writer = VaunixLinkageWriter(device)
            prepared = writer.prepare_trigger()
            device.disconnect()
            device.connect()
            self.api.dll.writes.clear()
            for operation in (lambda: writer.validate_step_values(self.values), writer.prepare_trigger,
                              lambda: prepared.apply_steps(self.values)):
                with self.subTest(device=type(device).__name__, operation=operation), \
                        self.assertRaisesRegex(VaunixLmsError, "重新布防"):
                    operation()
            self.assertEqual(self.api.dll.writes, [])

    def test_changed_cached_identity_or_capabilities_invalidates_the_binding(self):
        writer = VaunixLinkageWriter(self.real)
        prepared = writer.prepare_trigger()
        self.real._capabilities = replace(self.real.capabilities, serial_number=456)
        for operation in (lambda: writer.validate_step_values(self.values), writer.prepare_trigger,
                          lambda: prepared.apply_steps(self.values)):
            with self.assertRaisesRegex(VaunixLmsError, "能力|型号"):
                operation()
        self.assertEqual(self.api.dll.writes, [])

    def test_uncertain_power_acceptance_is_not_replayed(self):
        writer = VaunixLinkageWriter(self.real).prepare_trigger()
        self.api.dll.power_statuses = [0x80020000, 0]
        with patch("rigol_control.drivers.vaunix_lms.time.sleep") as sleep:
            with self.assertRaisesRegex(VaunixLmsError, "BAD_HID_IO"):
                writer.apply_steps(self.values)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])
        self.assertEqual(self.api.dll.opened, [7])
        self.assertFalse(self.real.is_connected)
        sleep.assert_not_called()

    def point(self, frequency, power):
        return LinkagePoint((
            LinkageStepValue("dg", StepParameter.FREQUENCY, 2, 2000, 2, "kHz"),
            LinkageStepValue("vaunix", StepParameter.FREQUENCY, 1, frequency, frequency, "Hz"),
            LinkageStepValue("vaunix", LinkageParameter.RF_POWER, 1, power, power, "dBm"),
        ))

    def controller(self, points):
        class DgWriter:
            def __init__(self):
                self.values = []
            def apply_steps(self, values):
                self.values.append(tuple(values))
        counter, dg = Counter(), DgWriter()
        controller = MultiDeviceTriggerController(counter,
            {"dg": dg, "vaunix": VaunixLinkageWriter(self.real)}, points,
            device_order=("dg", "vaunix"))
        self.addCleanup(controller.stop)
        return controller, counter, dg

    def test_mixed_controller_waits_for_first_edge_and_keeps_every_accumulated_point(self):
        controller, counter, dg = self.controller((self.point(5e9, -10), self.point(6e9, -5)))
        controller.start()
        self.assertEqual(self.api.dll.writes, [])
        self.assertEqual(dg.values, [])
        counter.add(100)
        self.assertTrue(wait_until(lambda: controller.snapshot().applied_count == 100))
        controller.stop()
        self.assertEqual(self.api.dll.writes, [
            ("frequency", 7, 500_000_000), ("power", 7, -40),
            ("frequency", 7, 600_000_000), ("power", 7, -20),
        ] * 50)
        self.assertEqual(len(dg.values), 100)
        self.assertTrue(all(item.channel == 2 for values in dg.values for item in values))
        self.assertEqual(controller.snapshot().backlog, 0)
        self.assertEqual(self.api.dll.reads, [])

    def test_mixed_controller_records_partial_failure_without_advancing_or_replaying(self):
        controller, counter, dg = self.controller((self.point(5e9, -10),))
        self.api.dll.power_statuses = [0x80020000, 0]
        controller.start()
        counter.add(3)
        self.assertTrue(wait_until(lambda: controller.snapshot().state is ControllerState.ERROR))
        controller.stop()
        snapshot = controller.snapshot()
        self.assertEqual((snapshot.received_count, snapshot.applied_count, snapshot.backlog), (3, 0, 3))
        self.assertEqual([(item.device_id, item.success) for item in snapshot.device_results],
                         [("dg", True), ("vaunix", False)])
        self.assertEqual(len(dg.values), 1)
        self.assertEqual(self.api.dll.writes, [("frequency", 7, 500_000_000), ("power", 7, -40)])


if __name__ == "__main__":
    unittest.main()
