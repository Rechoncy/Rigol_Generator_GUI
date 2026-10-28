"""Persisted virtual profiles must not initialize native drivers on restart."""
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.drivers.newport_smc100 import VIRTUAL_SMC100_RESOURCE
from rigol_control.drivers.siglent_ssg import VIRTUAL_SSG_RESOURCE
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.services import DeviceProbeService, ProbeState
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.virtual_lab import VIRTUAL_DG2_RESOURCE, VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


@contextmanager
def forbidden_native_backends():
    """Block native constructors even when a faulty probe swallows the error."""
    targets = (
        "pyvisa.ResourceManager",
        "nidaqmx.system.System.local",
        "serial.Serial",
        "rigol_control.services.VaunixLmsApi",
    )
    with ExitStack() as stack:
        mocks = {target: stack.enter_context(patch(
            target, side_effect=AssertionError(f"Virtual probe reached native backend: {target}")))
                 for target in targets}
        yield mocks


class VirtualProbeRestartTests(unittest.TestCase):
    PROBE_CASES = (
        (DeviceKind.DG4000, VIRTUAL_DG_RESOURCE),
        (DeviceKind.DG4000, VIRTUAL_DG2_RESOURCE),
        (DeviceKind.NI_DAQ, VIRTUAL_NI_RESOURCE),
        (DeviceKind.GENERIC_SCPI, "SIM::SCPI::saved-profile"),
    )

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["virtual-probe-restart-test"])

    def assert_no_native_calls(self, mocks):
        for mock in mocks.values():
            mock.assert_not_called()

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.003)
        return predicate()

    def test_virtual_dg_and_ni_probes_do_not_initialize_native_backends(self):
        for kind, resource in self.PROBE_CASES:
            with self.subTest(kind=kind, resource=resource):
                record = DeviceRecord.create(kind=kind, name="Saved simulator", resource=resource)
                with forbidden_native_backends() as mocks:
                    result = DeviceProbeService().probe(record)
                    self.assert_no_native_calls(mocks)
                    self.assertEqual(result.state, ProbeState.ONLINE)
                    self.assertTrue(result.identity)

    def test_probe_recognizes_same_case_and_whitespace_variants_as_connection_factory(self):
        for kind, resource in self.PROBE_CASES:
            with self.subTest(kind=kind, resource=resource):
                # Do not use create(), which strips whitespace before probing.
                record = DeviceRecord(id="saved-id", kind=kind, name="Saved simulator",
                                      resource=" \t" + resource.lower() + "  ")
                with forbidden_native_backends() as mocks:
                    result = DeviceProbeService().probe(record)
                    self.assert_no_native_calls(mocks)
                    self.assertEqual(result.state, ProbeState.ONLINE)
                    self.assertTrue(result.identity)

    def test_restart_reads_six_saved_simulators_without_connecting_or_rewriting_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            repository = DeviceRepository(data_dir)
            specifications = (
                (DeviceKind.DG4000, VIRTUAL_DG_RESOURCE),
                (DeviceKind.DG4000, VIRTUAL_DG2_RESOURCE),
                (DeviceKind.NI_DAQ, VIRTUAL_NI_RESOURCE),
                (DeviceKind.SIGLENT_SSG, VIRTUAL_SSG_RESOURCE),
                (DeviceKind.VAUNIX_LMS, VIRTUAL_VAUNIX_RESOURCE),
                (DeviceKind.NEWPORT_SMC100, VIRTUAL_SMC100_RESOURCE),
            )
            records = [DeviceRecord.create(kind=kind, name=f"Saved {index}", resource=resource)
                       for index, (kind, resource) in enumerate(specifications)]
            for record in records:
                repository.save(record)
            parameters = DeviceParameterRepository(data_dir)
            parameters.save(records[0].id, kind=DeviceKind.DG4000.value,
                            parameters={"migration_sentinel": "保存的参数", "frequency_hz": 12345.5})
            before = {name: (data_dir / name).read_bytes()
                      for name in ("devices.json", "device_parameters.json")}

            with forbidden_native_backends() as mocks:
                # A fresh repository/window exercises normal startup; there is
                # no create_virtual_lab() or manual connection to hide probing.
                home = DeviceCenterWindow(DeviceRepository(data_dir))
                home.show()
                try:
                    completed = self.wait(lambda: not home.probe_tasks)
                    statuses = {key: card.status.objectName() for key, card in home.cards.items()}
                    connections = [home.connection_manager.is_connected(record) for record in records]
                    connection_tasks = dict(home.connection_tasks)
                finally:
                    self.wait(lambda: not home.probe_tasks)
                    closed = home.close()
                    self.app.processEvents()

                self.assert_no_native_calls(mocks)
                self.assertTrue(completed, "Persisted virtual probes did not finish")
                self.assertEqual(statuses, {record.id: "StatusOnline" for record in records})
                self.assertEqual(connections, [False] * 6, "A status probe connected a device")
                self.assertFalse(connection_tasks)
                self.assertTrue(closed, "The restored window could not close normally")
                self.assertFalse(home.isVisible())
                self.assertEqual({name: (data_dir / name).read_bytes() for name in before}, before)


if __name__ == "__main__":
    unittest.main()
