import copy
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from rigol_control import config_archive as archive
from rigol_control.config_archive import ConfigArchiveError, ConfigArchiveService
from rigol_control.drivers.dg4000 import StepParameter, StepValue
from rigol_control.linkage import LinkageAxis, LinkagePoint, generate_linkage_points
from rigol_control.linkage_parameters import LinkageParameter, RfPowerStepValue, SsgLinkageMode
from rigol_control.linkage_tasks import LinkageTaskRecord, LinkageTaskRepository
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.scan import ScanMode


class MultiSourceLinkageModelTests(unittest.TestCase):
    @staticmethod
    def axis(device="dg", parameter=StepParameter.FREQUENCY, *, channel=1,
             unit="Hz", start=1000, step=1000, count=2):
        return LinkageAxis(device, parameter, channel, unit, start, step, count)

    def axes(self, *, count=2):
        return (
            self.axis(count=count, channel=2),
            self.axis("vaunix", LinkageParameter.RF_POWER, unit="dBm", start=-10, step=.5, count=count),
            self.axis("ssg", count=count, unit="GHz", start=1, step=.1),
        )

    @staticmethod
    def kinds():
        return (("dg", DeviceKind.DG4000), ("vaunix", DeviceKind.VAUNIX_LMS),
                ("ssg", DeviceKind.SIGLENT_SSG))

    def task(self, axes=None, **changes):
        axes = self.axes() if axes is None else tuple(axes)
        mode = changes.pop("scan_mode", ScanMode.SYNCHRONOUS)
        points = changes.pop("points", generate_linkage_points(axes, mode))
        values = dict(name="多源联动", ni_device_id="ni", counter="ctr0", terminal="PFI0",
                      rising=True, scan_mode=mode, axes=axes, points=points,
                      source_kinds=self.kinds())
        values.update(changes)
        return LinkageTaskRecord.create(**values)

    def test_dbm_power_generation_stays_distinct_from_dg_voltage(self):
        axes = (self.axis(parameter=StepParameter.AMPLITUDE, unit="mVpp", start=125, step=5, count=3),
                self.axis("vaunix", LinkageParameter.RF_POWER, unit="dBm", start=-10, step=.5, count=3))
        points = generate_linkage_points(axes, ScanMode.SYNCHRONOUS)
        self.assertEqual([point.values[1].value for point in points], [-10, -9.5, -9])
        self.assertEqual([point.values[0].value for point in points], [.125, .13, .135])
        self.assertIsInstance(points[0].grouped()["vaunix"][0], RfPowerStepValue)
        self.assertIsInstance(points[0].grouped()["dg"][0], StepValue)
        self.assertEqual(points[0].grouped()["vaunix"][0].unit_key, "dBm")
        self.task(axes, source_kinds=(("dg", DeviceKind.DG4000), ("vaunix", DeviceKind.VAUNIX_LMS)))

    def test_three_source_synchronous_and_cartesian_point_order(self):
        synchronous = self.task()
        self.assertEqual(len(synchronous.points), 2)
        self.assertEqual([value.device_id for value in synchronous.points[0].values], ["dg", "vaunix", "ssg"])
        axes = (self.axis(count=2), self.axis("vaunix", LinkageParameter.RF_POWER,
                    unit="dBm", start=-10, step=.5, count=3),
                self.axis("ssg", unit="GHz", start=1, step=.1, count=2))
        cartesian = self.task(axes, scan_mode=ScanMode.CARTESIAN,
                              ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL)
        self.assertEqual(len(cartesian.points), 12)
        self.assertEqual([point.values[2].value for point in cartesian.points[:4]], [1e9, 1.1e9, 1e9, 1.1e9])
        self.assertEqual([point.values[1].value for point in cartesian.points[:6]], [-10, -10, -9.5, -9.5, -9, -9])
        self.assertEqual(cartesian.points[-1].values[0].value, 2000)

    def test_legacy_dg_dictionary_round_trip_keeps_voltage_semantics(self):
        axes = (self.axis(parameter=StepParameter.AMPLITUDE, unit="Vpp", start=.1, step=.1),)
        original = self.task(axes, source_kinds=())
        legacy = original.to_dict()
        for key in ("source_kinds", "ssg_mode", "ssg_dwell_s"):
            legacy.pop(key)
        restored = LinkageTaskRecord.from_dict(legacy)
        self.assertEqual(restored, original)
        self.assertEqual(restored.source_kinds, ())
        self.assertEqual(restored.axes[0].parameter, StepParameter.AMPLITUDE)
        self.assertEqual(restored.axes[0].unit_key, "Vpp")
        self.assertEqual(LinkageTaskRecord.from_dict(restored.to_dict()), restored)

    def test_new_source_bindings_and_ssg_mode_dwell_round_trip(self):
        task = self.task(ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL, ssg_dwell_s=.125)
        encoded = task.to_dict()
        self.assertEqual(encoded["source_kinds"], {key: kind.value for key, kind in self.kinds()})
        self.assertEqual(encoded["ssg_mode"], "native_external")
        self.assertEqual(encoded["ssg_dwell_s"], .125)
        self.assertEqual(LinkageTaskRecord.from_dict(encoded), task)

    def test_incomplete_duplicate_or_extra_source_bindings_are_rejected(self):
        cases = (self.kinds()[:-1], self.kinds() + (("extra", DeviceKind.DG4000),),
                 self.kinds() + (("ssg", DeviceKind.SIGLENT_SSG),))
        for kinds in cases:
            with self.subTest(kinds=kinds), self.assertRaisesRegex(ValueError, "唯一|完整"):
                self.task(source_kinds=kinds)

    def test_wrong_source_types_or_dg_voltage_rf_power_rebinding_are_rejected(self):
        for kind in (DeviceKind.NI_DAQ, DeviceKind.NEWPORT_SMC100, DeviceKind.GENERIC_SCPI):
            kinds = (("dg", kind), *self.kinds()[1:])
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.task(source_kinds=kinds)
        for kind, axis in ((DeviceKind.DG4000, self.axis("source", LinkageParameter.RF_POWER, unit="dBm", start=-10, step=.5)),
                           (DeviceKind.VAUNIX_LMS, self.axis("source", StepParameter.AMPLITUDE, unit="Vpp", start=.1, step=.1)),
                           (DeviceKind.SIGLENT_SSG, self.axis("source", StepParameter.AMPLITUDE, unit="mVpp", start=100, step=10))):
            with self.subTest(kind=kind, axis=axis), self.assertRaises(ValueError):
                self.task((axis,), source_kinds=(("source", kind),))

    def test_single_output_sources_reject_channel_two_while_dg_retains_it(self):
        for device_id in ("vaunix", "ssg"):
            axes = tuple(replace(axis, channel=2) if axis.device_id == device_id else axis for axis in self.axes())
            with self.subTest(device_id=device_id), self.assertRaisesRegex(ValueError, "通道.*1"):
                self.task(axes)
        self.assertEqual(self.task().axes[0].channel, 2)

    def test_unbound_legacy_rf_power_or_voltage_unit_for_rf_power_is_rejected(self):
        rf = self.axis("source", LinkageParameter.RF_POWER, unit="dBm", start=-10, step=.5)
        with self.assertRaises(ValueError):
            self.task((rf,), source_kinds=())
        for unit in ("Vpp", "mVpp", "mV", "dB"):
            with self.subTest(unit=unit), self.assertRaises(ValueError):
                generate_linkage_points((replace(rf, unit_key=unit),), ScanMode.SYNCHRONOUS)

    def test_native_ssg_limits_the_expanded_joint_table_to_two_through_five_hundred(self):
        for count in (1, 501):
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, "2 至 500"):
                self.task(self.axes(count=count), ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL)
        for count in (2, 500):
            task = self.task(self.axes(count=count), ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL)
            self.assertEqual(len(task.points), count)
        axes = (self.axis(count=23), self.axis("ssg", count=23))
        with self.assertRaisesRegex(ValueError, "2 至 500"):
            self.task(axes, scan_mode=ScanMode.CARTESIAN, ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL,
                      source_kinds=(("dg", DeviceKind.DG4000), ("ssg", DeviceKind.SIGLENT_SSG)))

    def test_nonfinite_axis_point_or_dwell_values_are_rejected(self):
        task = self.task()
        for number in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(number=number), self.assertRaises(ValueError):
                generate_linkage_points((replace(self.axes()[0], start=number),), ScanMode.SYNCHRONOUS)
            for field in ("value", "display_value"):
                point = task.points[0]
                altered = replace(point.values[1], **{field: number})
                points = (LinkagePoint((point.values[0], altered, point.values[2])), *task.points[1:])
                with self.subTest(number=number, field=field), self.assertRaisesRegex(ValueError, "非有限"):
                    task.revised(points=points)
            with self.subTest(dwell=number), self.assertRaises(ValueError):
                task.revised(ssg_dwell_s=number)
        for dwell in (.009, 100.01):
            with self.subTest(dwell=dwell), self.assertRaisesRegex(ValueError, "驻留"):
                task.revised(ssg_dwell_s=dwell)

    def test_invalid_serialized_kind_or_mode_is_rejected(self):
        task = self.task()
        for key, value in (("ssg_mode", "unknown-mode"),
                           ("source_kinds", {"dg": "unknown-source", "vaunix": "vaunix_lms", "ssg": "siglent_ssg"})):
            payload = task.to_dict()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                LinkageTaskRecord.from_dict(payload)


class MultiSourceLinkageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.destination = self.root / "source", self.root / "destination"
        self.repository = DeviceRepository(self.source)
        self.devices = {}
        for device_id, kind, resource in (
            ("dg", DeviceKind.DG4000, "SIM::DG4000::1"),
            ("vaunix", DeviceKind.VAUNIX_LMS, "SIM::VAUNIX_LMS::1"),
            ("ssg", DeviceKind.SIGLENT_SSG, "SIM::SIGLENT_SSG::1"),
            ("ni", DeviceKind.NI_DAQ, "SIM::NI::1"),
        ):
            self.devices[device_id] = DeviceRecord(device_id, kind, "source-" + device_id, resource)
            self.repository.save(self.devices[device_id])
        fixture = MultiSourceLinkageModelTests()
        self.task = fixture.task(ssg_mode=SsgLinkageMode.NATIVE_EXTERNAL, ssg_dwell_s=.25)
        LinkageTaskRepository(self.source).save(self.task)
        self.service = ConfigArchiveService(self.source)

    @staticmethod
    def signed(payload):
        payload = copy.deepcopy(payload)
        payload["sha256"] = {key: archive._digest(value) for key, value in payload["files"].items()}
        return payload

    def test_three_source_bundle_export_import_preserves_types_units_mode_and_points(self):
        target = self.root / "mixed.json"
        self.service.export_file(target, list(self.devices))
        restored = ConfigArchiveService(self.destination)
        bundle = restored.read_file(target)
        restored.import_bundle(bundle)
        imported, = LinkageTaskRepository(self.destination).list()
        # A source-kind binding is a JSON object, so canonical archive key
        # ordering must not be interpreted as generator dispatch ordering.
        self.assertEqual(imported.to_dict(), self.task.to_dict())
        self.assertEqual(LinkageTaskRepository(self.destination).active_task_id(), self.task.id)
        self.assertEqual(DeviceRepository(self.destination).list(), self.repository.list())
        self.assertEqual(set(bundle.connected_ids), set(self.devices))

    def test_frequency_capability_overlap_does_not_permit_source_type_rebinding(self):
        payload = self.service.capture().payload
        for raw in payload["files"]["devices.json"]["devices"]:
            if raw["id"] == "ssg":
                raw["kind"] = DeviceKind.VAUNIX_LMS.value
        with self.assertRaisesRegex(ConfigArchiveError, "类型.*不匹配"):
            archive.validate_bundle(self.signed(payload))

    def test_legacy_dg_voltage_archive_cannot_be_rebound_to_rf_source(self):
        fixture = MultiSourceLinkageModelTests()
        axis = fixture.axis(parameter=StepParameter.AMPLITUDE, unit="Vpp", start=.1, step=.1)
        old = fixture.task((axis,), source_kinds=())
        LinkageTaskRepository(self.source).delete(self.task.id)
        LinkageTaskRepository(self.source).save(old)
        payload = self.service.capture().payload
        raw_task = payload["files"]["linkage_tasks.json"]["tasks"][0]
        for key in ("source_kinds", "ssg_mode", "ssg_dwell_s"):
            raw_task.pop(key)
        for raw in payload["files"]["devices.json"]["devices"]:
            if raw["id"] == "dg":
                raw["kind"] = DeviceKind.SIGLENT_SSG.value
        with self.assertRaisesRegex(ConfigArchiveError, "类型.*不匹配"):
            archive.validate_bundle(self.signed(payload))

    def test_missing_typed_source_is_preserved_as_a_warning_without_remapping(self):
        payload = self.service.capture().payload
        payload["files"]["devices.json"]["devices"] = [
            raw for raw in payload["files"]["devices.json"]["devices"] if raw["id"] != "ssg"]
        bundle = archive.validate_bundle(self.signed(payload))
        self.assertTrue(any("ssg" in warning for warning in bundle.warnings))
        ConfigArchiveService(self.destination).import_bundle(bundle)
        restored, = LinkageTaskRepository(self.destination).list()
        self.assertEqual(dict(restored.source_kinds), dict(self.task.source_kinds))
        self.assertEqual(restored.points, self.task.points)


if __name__ == "__main__":
    unittest.main()
