import unittest

from rigol_control.sequence import CyclicSequence


class CyclicSequenceTests(unittest.TestCase):
    def test_first_trigger_applies_start_point_and_then_wraps(self) -> None:
        sequence = CyclicSequence([100, 200, 300])
        self.assertEqual(sequence.for_trigger(1).value, 100)
        self.assertEqual(sequence.for_trigger(2).value, 200)
        self.assertEqual(sequence.for_trigger(3).value, 300)
        self.assertEqual(sequence.for_trigger(4).value, 100)
        self.assertEqual(sequence.for_trigger(7).index, 0)

    def test_pending_returns_every_unapplied_trigger_in_order(self) -> None:
        sequence = CyclicSequence(["start", "next"])
        pending = sequence.pending(applied_count=1, received_count=5)
        self.assertEqual([item.trigger_number for item in pending], [2, 3, 4, 5])
        self.assertEqual([item.value for item in pending], ["next", "start", "next", "start"])

    def test_no_points_and_invalid_counts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CyclicSequence([])
        sequence = CyclicSequence([1])
        with self.assertRaises(ValueError):
            sequence.for_trigger(0)
        with self.assertRaises(ValueError):
            sequence.pending(2, 1)


if __name__ == "__main__":
    unittest.main()

