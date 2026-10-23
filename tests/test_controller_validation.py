import unittest
from unittest.mock import Mock

from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import LinkageAxis, MultiDeviceTriggerController, generate_linkage_points
from rigol_control.scan import ScanMode
from rigol_control.trigger import TriggerStepController


class ControllerValidationTests(unittest.TestCase):
    def setUp(self):
        self.counter = Mock()
        self.writers = {"a": Mock(), "b": Mock()}
        self.points = generate_linkage_points(
            (
                LinkageAxis("a", StepParameter.FREQUENCY, 2, "Hz", 1000, 100, 2),
                LinkageAxis("b", StepParameter.AMPLITUDE, 2, "Vpp", 1, 0.1, 2),
            ),
            ScanMode.SYNCHRONOUS,
        )

    def test_invalid_poll_intervals_are_rejected_before_starting_hardware(self):
        for seconds in (float("nan"), float("inf"), -float("inf"), 0, -0.001):
            for controller in ("single", "multi"):
                with self.subTest(seconds=seconds, controller=controller), self.assertRaises(ValueError):
                    if controller == "single":
                        TriggerStepController(
                            self.counter, self.writers["a"], StepParameter.FREQUENCY,
                            2, [1000], monitor_poll_seconds=seconds,
                        )
                    else:
                        MultiDeviceTriggerController(
                            self.counter, self.writers, self.points,
                            monitor_poll_seconds=seconds,
                        )
        self.counter.start.assert_not_called()
        for writer in self.writers.values():
            writer.prepare_trigger.assert_not_called()
            writer.apply_steps.assert_not_called()

    def test_duplicate_or_empty_explicit_device_order_is_rejected(self):
        for order in (("a", "a", "b"), ("a", "b", "b"), ("a", "b", "a"), ()):
            with self.subTest(order=order), self.assertRaises(ValueError):
                MultiDeviceTriggerController(
                    self.counter, self.writers, self.points, device_order=order,
                )
        self.counter.start.assert_not_called()
        for writer in self.writers.values():
            writer.apply_steps.assert_not_called()

    def test_unique_order_and_default_order_are_preserved(self):
        default = MultiDeviceTriggerController(self.counter, self.writers, self.points)
        reversed_order = MultiDeviceTriggerController(
            self.counter, self.writers, self.points, device_order=("b", "a"),
        )
        self.assertEqual(default.device_order, ("a", "b"))
        self.assertEqual(reversed_order.device_order, ("b", "a"))
