import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rigol_control import config_archive as archive
from rigol_control.config_archive import ConfigArchiveError, ConfigArchiveService
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.linkage import LinkageAxis, generate_linkage_points
from rigol_control.linkage_tasks import LinkageTaskRecord, LinkageTaskRepository
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.scan import ScanMode
from rigol_control.step_tasks import StepTaskRecord, StepTaskRepository


class ConfigArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.repo = DeviceRepository(self.source)
        self.devices = []
        for kind, resource in ((DeviceKind.DG4000, "SIM::DG4000::1"),
                               (DeviceKind.NI_DAQ, "SIM::NI::1"),
                               (DeviceKind.VAUNIX_LMS, "SIM::VAUNIX_LMS::1"),
                               (DeviceKind.NEWPORT_SMC100, "SIM::SMC100::1"),
                               (DeviceKind.GENERIC_SCPI, "TCPIP::127.0.0.1::INSTR")):
            record = DeviceRecord.create(kind=kind, name="设备 " + kind.value, resource=resource)
            self.repo.save(record)
            self.devices.append(record)
        self.params = DeviceParameterRepository(self.source)
        for index, device in enumerate(self.devices):
            self.params.save(device.id, kind=device.kind.value,
                parameters={"binding_version": 2, "controls": {"named/value": {"type": "double", "value": index + 0.25}},
                            "custom": {"ni_device_id": self.devices[1].id, "table": [[1.2, 3.4]]}})
        dg, ni = self.devices[:2]
        self.step = StepTaskRecord.create(name="频率任务", generator_device_id=dg.id, ni_device_id=ni.id,
                                         parameter=StepParameter.FREQUENCY, channel=1, counter="ctr0",
                                         terminal="PFI0", rising=True, points=[1000, 2000])
        StepTaskRepository(self.source).save(self.step)
        axes = (LinkageAxis(device_id=dg.id, parameter=StepParameter.FREQUENCY, channel=1,
                            unit_key="Hz", start=1000, step=1000, count=2),)
        self.linkage = LinkageTaskRecord.create(name="联合实验", ni_device_id=ni.id, counter="ctr0", terminal="PFI0",
            rising=True, scan_mode=ScanMode.SYNCHRONOUS, axes=axes,
            points=generate_linkage_points(axes, ScanMode.SYNCHRONOUS))
        LinkageTaskRepository(self.source).save(self.linkage)
        self.service = ConfigArchiveService(self.source)
        self.bundle = self.service.capture([dg.id, ni.id])

    def signed(self, payload):
        payload = copy.deepcopy(payload)
        payload["sha256"] = {name: archive._digest(value) for name, value in payload["files"].items()}
        return payload

    def test_round_trip_preserves_ids_parameters_tasks_and_connections(self):
        exported = self.root / "配置备份.json"
        self.service.export_file(exported, [r.id for r in self.devices])
        restored = ConfigArchiveService(self.destination)
        bundle = restored.read_file(exported)
        backup = restored.import_bundle(bundle)
        self.assertTrue((backup / "original.json").exists())
        self.assertEqual([r.to_dict() for r in DeviceRepository(self.destination).list()],
                         [r.to_dict() for r in self.repo.list()])
        for device in self.devices:
            self.assertEqual(DeviceParameterRepository(self.destination).load(device.id), self.params.load(device.id))
        self.assertEqual(StepTaskRepository(self.destination).list()[0], self.step)
        self.assertEqual(LinkageTaskRepository(self.destination).list()[0], self.linkage)
        self.assertEqual(LinkageTaskRepository(self.destination).active_task_id(), self.linkage.id)
        self.assertEqual(bundle.connected_ids, tuple(r.id for r in self.devices))
        self.assertFalse((self.destination / archive.JOURNAL).exists())

    def test_overwrite_creates_importable_backup_and_does_not_touch_data(self):
        target_repo = DeviceRepository(self.destination)
        old = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="旧卡", resource="DevOld")
        target_repo.save(old)
        measurement = self.destination / "measurement.csv"
        measurement.write_text("important samples", encoding="utf-8")
        target = ConfigArchiveService(self.destination)
        backup = target.import_bundle(self.bundle, previous_connected_ids=[old.id])
        previous = target.read_file(backup / "configuration.json")
        self.assertEqual(previous.connected_ids, (old.id,))
        self.assertEqual(previous.files["devices.json"]["devices"][0]["id"], old.id)
        target.import_bundle(previous)
        self.assertEqual(target_repo.list(), [old])
        self.assertEqual(measurement.read_text(), "important samples")

    def test_invalid_bundles_leave_destination_untouched(self):
        for mutation in (
            lambda p: p.update(schema_version=99),
            lambda p: p["files"]["devices.json"].update(schema_version=2),
            lambda p: p["files"]["devices.json"]["devices"][0].update(kind="future-device"),
            lambda p: p["files"]["devices.json"]["devices"].append(p["files"]["devices.json"]["devices"][0]),
            lambda p: p["files"]["linkage_tasks.json"].update(active_task_id="missing"),
            lambda p: p["files"].update({"../escape.json": {"schema_version": 1}}),
        ):
            payload = copy.deepcopy(self.bundle.payload)
            mutation(payload)
            candidate = self.root / "invalid.json"
            candidate.write_bytes(archive._encode(self.signed(payload)))
            with self.assertRaises(ConfigArchiveError):
                ConfigArchiveService(self.destination).read_file(candidate)
            self.assertFalse(self.destination.exists())

    def test_checksum_and_nonfinite_rejected(self):
        payload = copy.deepcopy(self.bundle.payload)
        payload["files"]["devices.json"]["devices"][0]["name"] = "modified"
        path = self.root / "tampered.json"
        path.write_bytes(archive._encode(payload))
        with self.assertRaisesRegex(ConfigArchiveError, "校验失败"):
            self.service.read_file(path)
        path.write_text('{"x":NaN}', encoding="utf-8")
        with self.assertRaises(ConfigArchiveError):
            self.service.read_file(path)
        path.write_text('{"format":"a","format":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(ConfigArchiveError, "重复"):
            self.service.read_file(path)

    def test_legacy_parameters_are_preserved_for_existing_migration(self):
        payload = copy.deepcopy(self.bundle.payload)
        entry = payload["files"]["device_parameters.json"]["devices"][self.devices[0].id]
        entry["parameters"] = {"controls": {"page_1/unit_0": {"type": "unit", "value": 1234, "unit": "Hz"}}}
        ConfigArchiveService(self.destination).import_bundle(archive.validate_bundle(self.signed(payload)))
        self.assertEqual(DeviceParameterRepository(self.destination).load(self.devices[0].id), entry["parameters"])

    def test_missing_device_reference_warns_without_remapping(self):
        payload = copy.deepcopy(self.bundle.payload)
        payload["files"]["devices.json"]["devices"] = [d for d in payload["files"]["devices.json"]["devices"] if d["kind"] != "ni_daq"]
        payload["connected_device_ids"] = []
        bundle = archive.validate_bundle(self.signed(payload))
        self.assertTrue(bundle.warnings)
        ConfigArchiveService(self.destination).import_bundle(bundle)
        self.assertEqual(StepTaskRepository(self.destination).list()[0].ni_device_id, self.devices[1].id)

    def test_failure_mid_import_rolls_back_original_bytes(self):
        self.destination.mkdir()
        original = b'{"schema_version":1,"devices":[]}\r\n'
        (self.destination / "devices.json").write_bytes(original)
        write = archive._atomic_write
        failed = False
        def fail_once(path, content):
            nonlocal failed
            if path == self.destination / "device_parameters.json" and not failed:
                failed = True
                raise OSError("disk failure")
            write(path, content)
        with patch.object(archive, "_atomic_write", side_effect=fail_once):
            with self.assertRaisesRegex(ConfigArchiveError, "已回滚"):
                ConfigArchiveService(self.destination).import_bundle(self.bundle)
        self.assertEqual((self.destination / "devices.json").read_bytes(), original)
        for name in ("device_parameters.json", "step_tasks.json", "linkage_tasks.json", archive.JOURNAL):
            self.assertFalse((self.destination / name).exists())

    def test_interrupted_import_recovers_before_repository_reads(self):
        self.destination.mkdir()
        original = b'{"schema_version":1,"devices":[]}\n'
        (self.destination / "devices.json").write_bytes(original)
        write = archive._atomic_write
        def crash(path, content):
            if path == self.destination / "device_parameters.json":
                raise KeyboardInterrupt("simulated process termination")
            write(path, content)
        with patch.object(archive, "_atomic_write", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                ConfigArchiveService(self.destination).import_bundle(self.bundle)
        self.assertTrue((self.destination / archive.JOURNAL).exists())
        self.assertEqual(DeviceRepository(self.destination).list(), [])
        self.assertEqual((self.destination / "devices.json").read_bytes(), original)
        self.assertFalse((self.destination / archive.JOURNAL).exists())

    def test_export_never_overwrites_live_config(self):
        before = self.repo.path.read_bytes()
        with self.assertRaises(ConfigArchiveError):
            self.service.export_file(self.repo.path)
        self.assertEqual(self.repo.path.read_bytes(), before)

    def test_corrupt_local_config_can_be_replaced_with_raw_backup(self):
        self.destination.mkdir()
        (self.destination / "devices.json").write_bytes(b"broken old json")
        backup = ConfigArchiveService(self.destination).import_bundle(self.bundle)
        self.assertEqual(len(DeviceRepository(self.destination).list()), 5)
        self.assertTrue((backup / "original.json").exists())
        self.assertFalse((backup / "configuration.json").exists())


if __name__ == "__main__":
    unittest.main()
