import json
import tempfile
import unittest
from pathlib import Path

from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository, DuplicateDeviceError


class DeviceRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = DeviceRepository(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_round_trip_and_versioned_json(self) -> None:
        record = DeviceRecord.create(
            kind=DeviceKind.DG4000,
            name="实验台 DG4000",
            resource="USB0::TEST::INSTR",
            connection="USB VISA",
        )
        self.repository.save(record)

        loaded = self.repository.list()
        self.assertEqual(loaded, [record])
        payload = json.loads(self.repository.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertFalse(self.repository.path.with_suffix(".json.tmp").exists())

    def test_update_and_delete(self) -> None:
        original = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="NI 卡",
            resource="Dev1",
        )
        self.repository.save(original)
        changed = original.updated(name="NI USB-6363")
        self.repository.save(changed)
        self.assertEqual(self.repository.get(original.id).name, "NI USB-6363")
        self.assertTrue(self.repository.delete(original.id))
        self.assertFalse(self.repository.delete(original.id))
        self.assertEqual(self.repository.list(), [])

    def test_duplicate_kind_and_resource_is_rejected_case_insensitively(self) -> None:
        first = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="NI A",
            resource="Dev1",
        )
        duplicate = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="NI B",
            resource=" dev1 ",
        )
        self.repository.save(first)
        with self.assertRaises(DuplicateDeviceError):
            self.repository.save(duplicate)


if __name__ == "__main__":
    unittest.main()

