import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.ui.device_windows import DG4000Window
from rigol_control.ui.linkage_window import LinkageTaskWindow
from rigol_control.ui.vaunix_window import VaunixLmsWindow
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class PointTablePrecisionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or create_application(["point-table-precision-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = DeviceRepository(Path(self.temporary.name))
        self.parameters = DeviceParameterRepository(Path(self.temporary.name))
        self.manager = DeviceConnectionManager()
        self.dg = DeviceRecord.create(kind=DeviceKind.DG4000, name="DG", resource=VIRTUAL_DG_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        for record in (self.dg, self.ni):
            self.repository.save(record)
        self.windows = []
        self.values = (100000000.000001, 100000000.000002)

    def tearDown(self):
        for window in self.windows:
            window.close()
        self.app.processEvents()
        self.manager.close_all()
        self.temporary.cleanup()

    def test_dg_microhertz_points_survive_table_generation_parameter_save_and_reopen(self):
        window = DG4000Window(self.dg, [self.ni], self.manager, parameter_repository=self.parameters)
        self.windows.append(window)
        axis = window.scan_axes[0]
        axis.unit.setCurrentIndex(axis.unit.findData("Hz"))
        axis.start.setValue(self.values[0])
        axis.step.setValue(0.000001)
        axis.count.setValue(2)
        window._generate_scan_table(show_message=False)
        expected = tuple(point.values[0].value for point in window._sequence_points())
        self.assertEqual(expected, self.values)
        self.assertNotEqual(*expected)
        self.assertEqual(tuple(float(window.step_table.item(row, 1).text()) for row in range(2)), expected)
        window._save_device_parameters(force=True)
        restored = DG4000Window(self.dg, [self.ni], self.manager, parameter_repository=self.parameters)
        self.windows.append(restored)
        actual = tuple(point.values[0].value for point in restored._sequence_points())
        self.assertEqual(actual, expected)
        self.assertNotEqual(*actual)

    def test_linkage_microhertz_points_survive_task_save_load_and_reopen(self):
        window = LinkageTaskWindow(self.repository, self.manager)
        self.windows.append(window)
        axis = window.axis_editors[0]
        axis.unit.setCurrentIndex(axis.unit.findData("Hz"))
        axis.start.setValue(self.values[0])
        axis.step.setValue(0.000001)
        axis.count.setValue(2)
        window._generate_table(show_message=False)
        expected = tuple(point.values[0].value for point in window._points_from_table())
        self.assertEqual(expected, self.values)
        self.assertNotEqual(*expected)
        window._save_task()
        restored = LinkageTaskWindow(self.repository, self.manager)
        self.windows.append(restored)
        actual = tuple(point.values[0].value for point in restored._points_from_table())
        self.assertEqual(actual, expected)
        self.assertNotEqual(*actual)

    def test_vaunix_editable_sequence_and_trigger_drafts_keep_float_values_when_saved(self):
        record = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="Vaunix", resource=VIRTUAL_VAUNIX_RESOURCE)
        window = VaunixLmsWindow(record, [self.ni], self.manager, parameter_repository=self.parameters)
        self.windows.append(window)
        # Editable drafts are preserved before the SDK's separate range/grid
        # validation. This does not claim microhertz Vaunix hardware support.
        sequence_values = (10000.00000000001, 10000.00000000002)
        window.sequence_table.setRowCount(0)
        for value in sequence_values:
            window._append_sequence_row(value, -10.0, True)
        with patch.object(window, "_trigger_axes_values", return_value=(self.values, ())):
            window._generate_trigger_table(show_message=False)
        window._save_device_parameters(force=True)
        restored = VaunixLmsWindow(record, [self.ni], self.manager, parameter_repository=self.parameters)
        self.windows.append(restored)
        self.assertEqual(tuple(float(restored.sequence_table.item(row, 1).text()) for row in range(2)),
                         sequence_values)
        self.assertEqual(tuple(float(restored.trigger_table.item(row, 1).text()) for row in range(2)), self.values)
