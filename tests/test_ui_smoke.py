import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState
from rigol_control.services import DiscoveredResource
from rigol_control.ui.device_windows import DG4000Window, NIDeviceWindow
from rigol_control.ui.dialogs import DeviceDialog
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.virtual_lab import VIRTUAL_DG2_RESOURCE, VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or create_application(["ui-test"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = DeviceRepository(Path(self.temporary.name))
        self.dg = DeviceRecord.create(
            kind=DeviceKind.DG4000,
            name="DG4000",
            resource="USB0::DG::INSTR",
        )
        self.ni = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="NI DAQ",
            resource="Dev1",
        )
        self.repository.save(self.dg)
        self.repository.save(self.ni)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_home_and_independent_device_windows_can_be_created(self) -> None:
        home = DeviceCenterWindow(self.repository)
        self.assertEqual(len(home.cards), 2)
        home.open_device(self.dg.id)
        self.assertIsInstance(home.open_windows[self.dg.id], DG4000Window)
        home.open_device(self.ni.id)
        self.assertIsInstance(home.open_windows[self.ni.id], NIDeviceWindow)
        self.assertEqual(len(home.open_windows), 2)
        home.close()
        self.app.processEvents()

    def test_device_dialog_auto_discovers_resources_into_editable_combo(self) -> None:
        class FakeDiscovery:
            def discover(self, kind):
                if kind is DeviceKind.NI_DAQ:
                    return [
                        DiscoveredResource(
                            "Dev1",
                            "NI-DAQmx 本机设备",
                            "NI USB-6363 · SN 1234",
                        )
                    ]
                return [
                    DiscoveredResource("USB0::DG::INSTR", "USB VISA"),
                    DiscoveredResource("TCPIP0::DG::INSTR", "TCP/IP VISA"),
                ]

        dialog = DeviceDialog(discovery_service=FakeDiscovery())
        dialog.show()
        self.assertTrue(
            self._wait_for(lambda: dialog.resource.count() == 2),
            dialog.scan_status.text(),
        )
        self.assertTrue(dialog.resource.isEditable())
        self.assertEqual(dialog.selected_resource(), "USB0::DG::INSTR")
        self.assertEqual(dialog.connection.currentText(), "USB VISA")

        dialog.kind.setCurrentIndex(dialog.kind.findData(DeviceKind.NI_DAQ))
        self.assertTrue(
            self._wait_for(lambda: dialog.resource.count() == 1),
            dialog.scan_status.text(),
        )
        self.assertEqual(dialog.selected_resource(), "Dev1")
        self.assertIn("NI USB-6363", dialog.resource.currentText())

        dialog.resource.setEditText("DevManual")
        self.assertEqual(dialog.selected_resource(), "DevManual")
        dialog.close()

    def test_home_connect_disconnect_is_shared_with_settings_window(self) -> None:
        class FakeSession:
            def __init__(self, resource):
                self.resource = resource
                self.is_connected = False

            def connect(self):
                self.is_connected = True
                return "RIGOL,DG4162,TEST,1.0"

            def disconnect(self):
                self.is_connected = False

            def query(self, command):
                return "RIGOL,DG4162,TEST,1.0"

            def write(self, command):
                pass

            def write_many(self, commands):
                pass

        manager = DeviceConnectionManager(session_factory=FakeSession)
        home = DeviceCenterWindow(self.repository, manager)
        home.connect_device(self.dg.id)
        self.assertTrue(
            self._wait_for(
                lambda: manager.is_connected(self.dg)
                and home.cards[self.dg.id].disconnect_button.isEnabled()
            )
        )
        self.assertTrue(home.cards[self.dg.id].disconnect_button.isEnabled())

        home.open_device(self.dg.id)
        window = home.open_windows[self.dg.id]
        self.assertTrue(window.driver.is_connected)
        window.close()
        self.app.processEvents()
        self.assertTrue(manager.is_connected(self.dg))

        home.disconnect_device(self.dg.id)
        self.assertTrue(
            self._wait_for(
                lambda: not manager.is_connected(self.dg)
                and home.cards[self.dg.id].connect_button.isEnabled()
            )
        )
        self.assertTrue(home.cards[self.dg.id].connect_button.isEnabled())
        home.close()
        self.app.processEvents()

    def test_virtual_lab_end_to_end_linkage_without_hardware(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "virtual")
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.create_virtual_lab()
        self.assertTrue(
            self._wait_for(
                lambda: len(repository.list()) == 3
                and all(manager.is_connected(item) for item in repository.list())
            )
        )
        records = repository.list()
        dg = next(item for item in records if item.resource == VIRTUAL_DG_RESOURCE)
        ni = next(item for item in records if item.resource == VIRTUAL_NI_RESOURCE)
        self.assertTrue(any(item.resource == VIRTUAL_DG2_RESOURCE for item in records))
        home.open_device(dg.id)
        home.open_device(ni.id)
        dg_window = home.open_windows[dg.id]
        ni_window = home.open_windows[ni.id]
        frequency_control = dg_window.channel_controls[1]["frequency"]
        self.assertEqual(frequency_control.current_unit(), "kHz")
        self.assertEqual(frequency_control.value(), 1000)
        frequency_control.set_unit("MHz")
        self.assertEqual(frequency_control.display_value(), 0.001)
        self.assertEqual(frequency_control.value(), 1000)
        channel_controls = dg_window.channel_controls[1]
        channel_controls["load"].setCurrentIndex(
            channel_controls["load"].findData(50)
        )
        dg_window._apply_channel(1)
        self.assertTrue(
            self._wait_for(
                lambda: manager.virtual_lab.generator_snapshot(dg.resource)
                .channels[0]
                .load_ohms
                == 50
            )
        )
        dg_window._select_page(4)
        frequency_axis = dg_window.scan_axes[0]
        frequency_axis.start.setValue(0.1)
        frequency_axis.step.setValue(0.1)
        frequency_axis.count.setValue(4)
        dg_window._add_scan_axis(StepParameter.AMPLITUDE, 1)
        amplitude_axis = dg_window.scan_axes[1]
        amplitude_axis.unit.setCurrentIndex(amplitude_axis.unit.findData("mVpp"))
        amplitude_axis.start.setValue(500)
        amplitude_axis.step.setValue(250)
        amplitude_axis.count.setValue(4)
        dg_window._generate_scan_table()
        points = dg_window._sequence_points()
        self.assertEqual([item.value for item in points[1].values], [200, 0.75])
        self.assertEqual(dg_window.step_table.item(0, 3).text(), "起始点（第一个触发）")
        dg_window._start_trigger()
        self.assertTrue(
            self._wait_for(
                lambda: dg_window.controller is not None
                and dg_window.controller.snapshot().state.value == "running"
            )
        )
        ni_window._emit_virtual_triggers(6)
        self.assertTrue(
            self._wait_for(
                lambda: dg_window.controller.snapshot().applied_count == 6
                and dg_window.trigger_metrics["received"].text() == "6"
                and dg_window.trigger_metrics["applied"].text() == "6"
            )
        )
        self.assertEqual(dg_window.trigger_metrics["received"].text(), "6")
        self.assertEqual(dg_window.trigger_metrics["applied"].text(), "6")
        self.assertEqual(
            manager.virtual_lab.generator_snapshot(dg.resource).channels[0].frequency_hz,
            200,
        )
        self.assertEqual(
            manager.virtual_lab.generator_snapshot(dg.resource).channels[0].amplitude_vpp,
            0.75,
        )
        home.close()
        self.app.processEvents()

    def test_virtual_analog_input_plots_and_exports_multichannel_data(self) -> None:
        manager = DeviceConnectionManager()
        record = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="虚拟 NI",
            resource=VIRTUAL_NI_RESOURCE,
            connection="内置模拟器",
        )
        manager.connect(record)
        window = NIDeviceWindow(record, manager)
        window.show()
        window._select_page(2)
        window.ai_channel_list.item(1).setSelected(True)
        window.ai_sample_rate.setValue(20_000)
        window.ai_block_size.setValue(200)
        window._start_analog_input()
        self.assertTrue(
            self._wait_for(
                lambda: window.analog_controller is not None
                and window.analog_controller.snapshot().total_samples >= 400,
                timeout=2.0,
            )
        )
        self.assertTrue(
            self._wait_for(lambda: window.ai_plot.displayed_samples >= 400)
        )
        self.assertGreaterEqual(window.ai_plot.displayed_samples, 400)
        self.assertEqual(window.ai_metrics["channels"].text(), "2")
        window._stop_analog_input()
        self.assertTrue(
            self._wait_for(
                lambda: window.analog_controller.snapshot().state.value == "stopped"
                and window.ai_start.isEnabled()
            )
        )
        path = Path(self.temporary.name) / "ui-analog.csv"
        window.analog_controller.save_csv(path)
        self.assertTrue(path.exists())
        self.assertGreater(path.stat().st_size, 1000)
        window.close()
        self.app.processEvents()

    def test_cross_device_task_window_drives_two_virtual_generators(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "cross-device")
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.create_virtual_lab()
        self.assertTrue(
            self._wait_for(
                lambda: len(repository.list()) == 3
                and all(manager.is_connected(item) for item in repository.list())
            )
        )
        home.open_linkage_tasks()
        window = home.linkage_window
        self.assertIsNotNone(window)
        generators = sorted(
            [item for item in repository.list() if item.kind is DeviceKind.DG4000],
            key=lambda item: item.resource,
        )
        first = window.axis_editors[0]
        first.device.setCurrentIndex(first.device.findData(generators[0].id))
        first.start.setValue(1)
        first.step.setValue(1)
        first.count.setValue(2)
        window._add_axis()
        second = window.axis_editors[1]
        second.device.setCurrentIndex(second.device.findData(generators[1].id))
        second.parameter.setCurrentIndex(second.parameter.findData(StepParameter.AMPLITUDE))
        second.start.setValue(0.5)
        second.step.setValue(0.25)
        second.count.setValue(2)
        window._generate_table(show_message=False)
        self.assertEqual(window.point_table.rowCount(), 2)
        window._save_task()
        self.assertTrue(window.task_repository.path.exists())
        window._start()
        self.assertTrue(
            self._wait_for(
                lambda: window.controller is not None
                and window.controller.snapshot().state is ControllerState.RUNNING
            )
        )
        window._emit_virtual(3)
        self.assertTrue(
            self._wait_for(lambda: window.controller.snapshot().applied_count == 3)
        )
        self.assertEqual(
            manager.virtual_lab.generator_snapshot(generators[0].resource)
            .channels[0]
            .frequency_hz,
            1000,
        )
        self.assertEqual(
            manager.virtual_lab.generator_snapshot(generators[1].resource)
            .channels[0]
            .amplitude_vpp,
            0.5,
        )
        log_path = Path(window.log_path.text().split("：", 1)[1])
        self.assertTrue(log_path.exists())
        window._stop()
        self.assertIsNone(manager.lease_owner(generators[0].id))
        home.close()
        self.app.processEvents()

    def test_cartesian_axis_preflight_and_table_generation(self) -> None:
        manager = DeviceConnectionManager()
        window = DG4000Window(self.dg, [self.dg, self.ni], manager)
        window._add_scan_axis(StepParameter.AMPLITUDE, 1)
        window.scan_axes[1].count.setValue(3)
        self.assertFalse(window.generate_scan_button.isEnabled())
        self.assertIn("点数相同", window.scan_summary.text())

        window.scan_mode.setCurrentIndex(window.scan_mode.findData(ScanMode.CARTESIAN))
        self.assertTrue(window.generate_scan_button.isEnabled())
        self.assertIn("12 个联合点", window.scan_summary.text())
        window._generate_scan_table(show_message=False)
        self.assertEqual(window.step_table.rowCount(), 12)
        self.assertEqual(len(window._sequence_points()), 12)
        window.close()
        self.app.processEvents()

    def _wait_for(self, predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        self.app.processEvents()
        return predicate()


if __name__ == "__main__":
    unittest.main()
