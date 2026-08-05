import tempfile
import unittest
from pathlib import Path

from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.step_tasks import StepTaskRecord, StepTaskRepository


class StepTaskRepositoryTests(unittest.TestCase):
    def test_task_round_trip_preserves_trigger_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = StepTaskRepository(Path(temporary))
            task = StepTaskRecord.create(
                name="循环频率步进",
                generator_device_id="dg-id",
                ni_device_id="ni-id",
                parameter=StepParameter.FREQUENCY,
                channel=1,
                counter="ctr0",
                terminal="PFI0",
                rising=True,
                points=[1000, 2000, 5000],
            )
            repository.save(task)
            self.assertEqual(repository.list(), [task])


if __name__ == "__main__":
    unittest.main()
