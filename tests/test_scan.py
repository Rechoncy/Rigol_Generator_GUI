import math
import unittest

from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.scan import (
    ScanAxis,
    ScanMode,
    generate_scan_points,
    scan_point_count,
)
from rigol_control.units import convert, from_base, to_base


class UnitConversionTests(unittest.TestCase):
    def test_linear_and_angle_units_round_trip_to_scpi_base_units(self) -> None:
        self.assertEqual(to_base(1.25, "frequency", "kHz"), 1250)
        self.assertEqual(from_base(0.0025, "voltage", "mV"), 2.5)
        self.assertAlmostEqual(to_base(math.pi, "angle", "rad"), 180)
        self.assertAlmostEqual(convert(1500, "frequency", "Hz", "kHz"), 1.5)


class MultiAxisScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frequency = ScanAxis(
            StepParameter.FREQUENCY,
            1,
            "kHz",
            0.1,
            0.1,
            3,
        )
        self.amplitude = ScanAxis(
            StepParameter.AMPLITUDE,
            1,
            "mVpp",
            500,
            250,
            3,
        )

    def test_synchronous_axes_create_one_joint_point_per_index(self) -> None:
        points = generate_scan_points(
            [self.frequency, self.amplitude],
            ScanMode.SYNCHRONOUS,
        )
        self.assertEqual(len(points), 3)
        self.assertEqual(
            [[item.value for item in point.values] for point in points],
            [[100, 0.5], [200, 0.75], [300, 1.0]],
        )

    def test_cartesian_mode_uses_last_axis_as_fast_axis(self) -> None:
        frequency = ScanAxis(StepParameter.FREQUENCY, 1, "kHz", 1, 1, 2)
        amplitude = ScanAxis(StepParameter.AMPLITUDE, 1, "Vpp", 1, 1, 3)
        points = generate_scan_points([frequency, amplitude], ScanMode.CARTESIAN)
        self.assertEqual(scan_point_count([frequency, amplitude], ScanMode.CARTESIAN), 6)
        self.assertEqual(
            [[item.value for item in point.values] for point in points],
            [[1000, 1], [1000, 2], [1000, 3], [2000, 1], [2000, 2], [2000, 3]],
        )

    def test_preflight_rejects_mismatched_sync_and_duplicate_targets(self) -> None:
        short_axis = ScanAxis(StepParameter.AMPLITUDE, 1, "Vpp", 1, 1, 2)
        with self.assertRaisesRegex(ValueError, "点数相同"):
            generate_scan_points([self.frequency, short_axis], ScanMode.SYNCHRONOUS)
        duplicate = ScanAxis(StepParameter.FREQUENCY, 1, "MHz", 0.001, 0.001, 3)
        with self.assertRaisesRegex(ValueError, "重复"):
            generate_scan_points([self.frequency, duplicate], ScanMode.CARTESIAN)


if __name__ == "__main__":
    unittest.main()
