import csv
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QTabWidget

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter, dg4000_capabilities
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository
from rigol_control.scan import ScanMode
from rigol_control.trigger import ControllerState
from rigol_control.services import DiscoveredResource
from rigol_control.ui.device_windows import DG4000Window, NIDeviceWindow
from rigol_control.ui.dialogs import DeviceDialog
from rigol_control.ui.linkage_window import LinkageTaskWindow
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.ui.vaunix_window import VaunixLmsWindow
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

    def test_dg_frequency_editors_follow_connected_model(self) -> None:
        window = DG4000Window(self.dg, [self.ni])
        frequency_controls = [
            window.channel_controls[1]["frequency"],
            window.channel_controls[2]["frequency"],
            window.modulation_controls["FSK"]["parameter"],
            *window.sweep_frequency_controls,
        ]
        self.assertTrue(
            all(control.maximum_base == 200_000_000 for control in frequency_controls)
        )
        window.channel_controls[1]["frequency"].set_unit("MHz")
        window.channel_controls[1]["frequency"].setValue(200_000_000)
        self.assertEqual(
            window.channel_controls[1]["frequency"].display_value(),
            200,
        )

        note = window._apply_frequency_capabilities(
            dg4000_capabilities("RIGOL TECHNOLOGIES,DG4162,TEST,00.00")
        )
        self.assertIn("160 MHz", note)
        self.assertTrue(
            all(control.maximum_base == 160_000_000 for control in frequency_controls)
        )
        self.assertEqual(window.channel_controls[1]["frequency"].value(), 160_000_000)
        self.assertEqual(window.scan_axes[0].start.maximum(), 160_000)

        note = window._apply_frequency_capabilities(
            dg4000_capabilities("RIGOL TECHNOLOGIES,DG4202,TEST,00.00")
        )
        self.assertIn("200 MHz", note)
        self.assertTrue(
            all(control.maximum_base == 200_000_000 for control in frequency_controls)
        )
        self.assertEqual(window.scan_axes[0].start.maximum(), 200_000)
        window.close()
        self.app.processEvents()

    def test_device_parameters_restore_automatically_after_reopening(self) -> None:
        parameter_repository = DeviceParameterRepository(Path(self.temporary.name))
        dg_window = DG4000Window(
            self.dg,
            [self.ni],
            parameter_repository=parameter_repository,
        )
        dg_window.channel_controls[1]["frequency"].set_unit("MHz")
        dg_window.channel_controls[1]["frequency"].setValue(2_500_000)
        dg_window.channel_controls[1]["amplitude"].setValue(0.875)
        dg_window.channel_controls[1]["output"].setChecked(True)
        dg_window.modulation_controls["PSK"]["channel"].setCurrentIndex(1)
        dg_window.modulation_controls["PSK"]["parameter"].setValue(123.5)
        dg_window.modulation_controls["PSK"]["enabled"].setChecked(True)
        load = dg_window.impedance_controls[1]["load"]
        load.setCurrentIndex(load.findData(75))
        dg_window.step_terminal.setCurrentText("PFI7")
        first_axis = dg_window.scan_axes[0]
        first_axis.start.setValue(1.25)
        first_axis.step.setValue(0.25)
        first_axis.count.setValue(3)
        dg_window._add_scan_axis(StepParameter.AMPLITUDE, 2)
        second_axis = dg_window.scan_axes[1]
        second_axis.start.setValue(0.5)
        second_axis.step.setValue(0.125)
        second_axis.count.setValue(3)
        dg_window._generate_scan_table(show_message=False)
        dg_window.step_table.item(1, 2).setText("0.9375")
        expected_snapshot = dg_window._device_parameter_snapshot()
        self.assertTrue(
            self._wait_for(
                lambda: parameter_repository.load(
                    self.dg.id,
                    kind=DeviceKind.DG4000.value,
                )
                == expected_snapshot,
                timeout=2.0,
            )
        )
        dg_window.close()
        self.app.processEvents()

        restored_dg = DG4000Window(
            self.dg,
            [self.ni],
            parameter_repository=parameter_repository,
        )
        self.assertEqual(
            restored_dg.channel_controls[1]["frequency"].current_unit(),
            "MHz",
        )
        self.assertAlmostEqual(
            restored_dg.channel_controls[1]["frequency"].value(),
            2_500_000,
        )
        self.assertAlmostEqual(
            restored_dg.channel_controls[1]["amplitude"].value(),
            0.875,
        )
        self.assertTrue(restored_dg.channel_controls[1]["output"].isChecked())
        self.assertEqual(
            restored_dg.modulation_controls["PSK"]["channel"].currentData(),
            2,
        )
        self.assertAlmostEqual(
            restored_dg.modulation_controls["PSK"]["parameter"].value(),
            123.5,
        )
        self.assertTrue(
            restored_dg.modulation_controls["PSK"]["enabled"].isChecked()
        )
        self.assertEqual(
            restored_dg.impedance_controls[1]["load"].currentData(),
            75,
        )
        self.assertEqual(restored_dg.step_terminal.currentText(), "PFI7")
        self.assertEqual(len(restored_dg.scan_axes), 2)
        self.assertEqual(restored_dg.scan_axes[1].parameter.currentData(), StepParameter.AMPLITUDE)
        self.assertEqual(restored_dg.scan_axes[1].channel.currentData(), 2)
        self.assertEqual(restored_dg.step_table.item(1, 2).text(), "0.9375")
        restored_dg.close()
        self.app.processEvents()

        ni_window = NIDeviceWindow(
            self.ni,
            parameter_repository=parameter_repository,
        )
        ni_window._set_all_analog_channels(True)
        ni_window.ai_terminal_mode.setCurrentIndex(
            ni_window.ai_terminal_mode.findData("DIFF")
        )
        ni_window.ai_sample_rate.setValue(12_500)
        ni_window.ai_retained_samples.setValue(750_000)
        ni_window.ai_stream_enabled.setChecked(False)
        ni_window.ai_stream_path.setText(str(Path(self.temporary.name) / "saved.csv"))
        ni_window.ai_x_scale.setValue(0.005)
        ni_window.ai_y_auto.setChecked(False)
        ni_window.ai_y_scale.setValue(0.25)
        ni_window.ai_y_center.setValue(0.125)
        ni_window.close()
        self.app.processEvents()

        restored_ni = NIDeviceWindow(
            self.ni,
            parameter_repository=parameter_repository,
        )
        self.assertEqual(len(restored_ni._checked_analog_channels()), 8)
        self.assertEqual(restored_ni.ai_terminal_mode.currentData(), "DIFF")
        self.assertAlmostEqual(restored_ni.ai_sample_rate.value(), 12_500)
        self.assertEqual(restored_ni.ai_retained_samples.value(), 750_000)
        self.assertFalse(restored_ni.ai_stream_enabled.isChecked())
        self.assertTrue(restored_ni.ai_stream_path.text().endswith("saved.csv"))
        self.assertAlmostEqual(restored_ni.ai_plot.seconds_per_division, 0.005)
        self.assertAlmostEqual(restored_ni.ai_plot.volts_per_division, 0.25)
        self.assertAlmostEqual(restored_ni.ai_plot.voltage_center_v, 0.125)
        restored_ni.close()
        self.app.processEvents()

    def test_dg_channels_keep_identity_when_ch2_tab_was_active_at_save(self) -> None:
        parameter_repository = DeviceParameterRepository(Path(self.temporary.name))
        window = DG4000Window(
            self.dg,
            [self.ni],
            parameter_repository=parameter_repository,
        )
        channel_1 = window.channel_controls[1]
        channel_2 = window.channel_controls[2]
        channel_1["waveform"].setCurrentIndex(1)
        channel_1["frequency"].setValue(1_250_000)
        channel_1["amplitude"].setValue(1.25)
        channel_1["offset"].setValue(-0.25)
        channel_1["phase"].setValue(45)
        channel_1["output"].setChecked(True)
        channel_2["waveform"].setCurrentIndex(2)
        channel_2["frequency"].setValue(7_500_000)
        channel_2["amplitude"].setValue(2.5)
        channel_2["offset"].setValue(0.75)
        channel_2["phase"].setValue(270)
        channel_2["output"].setChecked(False)
        load_1 = window.impedance_controls[1]
        load_1["load"].setCurrentIndex(load_1["load"].findData(75))
        load_2 = window.impedance_controls[2]
        load_2["load"].setCurrentIndex(load_2["load"].findData("CUSTOM"))
        load_2["custom_load"].setValue(1234)

        channel_tabs = next(
            tabs
            for tabs in window.findChildren(QTabWidget)
            if tabs.count() == 2 and tabs.tabText(0) == "CH1"
        )
        channel_tabs.setCurrentIndex(1)
        self.app.processEvents()
        window._save_device_parameters(force=True)
        saved = parameter_repository.load(
            self.dg.id,
            kind=DeviceKind.DG4000.value,
        )
        self.assertEqual(saved["binding_version"], 2)
        self.assertIn("named/dg4000.channel.1.frequency", saved["controls"])
        self.assertIn("named/dg4000.channel.2.frequency", saved["controls"])
        window.close()
        self.app.processEvents()

        restored = DG4000Window(
            self.dg,
            [self.ni],
            parameter_repository=parameter_repository,
        )
        restored_1 = restored.channel_controls[1]
        restored_2 = restored.channel_controls[2]
        self.assertEqual(restored_1["waveform"].currentIndex(), 1)
        self.assertAlmostEqual(restored_1["frequency"].value(), 1_250_000)
        self.assertAlmostEqual(restored_1["amplitude"].value(), 1.25)
        self.assertAlmostEqual(restored_1["offset"].value(), -0.25)
        self.assertAlmostEqual(restored_1["phase"].value(), 45)
        self.assertTrue(restored_1["output"].isChecked())
        self.assertEqual(restored_2["waveform"].currentIndex(), 2)
        self.assertAlmostEqual(restored_2["frequency"].value(), 7_500_000)
        self.assertAlmostEqual(restored_2["amplitude"].value(), 2.5)
        self.assertAlmostEqual(restored_2["offset"].value(), 0.75)
        self.assertAlmostEqual(restored_2["phase"].value(), 270)
        self.assertFalse(restored_2["output"].isChecked())
        self.assertEqual(restored.impedance_controls[1]["load"].currentData(), 75)
        self.assertEqual(
            restored.impedance_controls[2]["load"].currentData(),
            "CUSTOM",
        )
        self.assertEqual(restored.impedance_controls[2]["custom_load"].value(), 1234)
        restored.close()
        self.app.processEvents()

    def test_dg_legacy_channel_positions_migrate_in_ch1_ch2_order(self) -> None:
        parameter_repository = DeviceParameterRepository(Path(self.temporary.name))
        parameter_repository.save(
            self.dg.id,
            kind=DeviceKind.DG4000.value,
            parameters={
                "controls": {
                    "page_1/unit_0": {
                        "type": "unit",
                        "value": 1_500_000,
                        "unit": "MHz",
                    },
                    "page_1/unit_1": {
                        "type": "unit",
                        "value": 1.5,
                        "unit": "Vpp",
                    },
                    "page_1/check_0": {"type": "check", "checked": True},
                    "page_1/unit_4": {
                        "type": "unit",
                        "value": 8_500_000,
                        "unit": "MHz",
                    },
                    "page_1/unit_5": {
                        "type": "unit",
                        "value": 2.5,
                        "unit": "Vpp",
                    },
                    "page_1/check_1": {"type": "check", "checked": False},
                },
                "custom": {},
            },
        )
        restored = DG4000Window(
            self.dg,
            [self.ni],
            parameter_repository=parameter_repository,
        )
        self.assertAlmostEqual(
            restored.channel_controls[1]["frequency"].value(),
            1_500_000,
        )
        self.assertAlmostEqual(restored.channel_controls[1]["amplitude"].value(), 1.5)
        self.assertTrue(restored.channel_controls[1]["output"].isChecked())
        self.assertAlmostEqual(
            restored.channel_controls[2]["frequency"].value(),
            8_500_000,
        )
        self.assertAlmostEqual(restored.channel_controls[2]["amplitude"].value(), 2.5)
        self.assertFalse(restored.channel_controls[2]["output"].isChecked())
        migrated = parameter_repository.load(
            self.dg.id,
            kind=DeviceKind.DG4000.value,
        )
        self.assertEqual(migrated["binding_version"], 2)
        restored.close()
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

            def query(self, command, *, recover=False):
                del recover
                if command == "*IDN?":
                    return "RIGOL,DG4162,TEST,1.0"
                if command == ":OUTP1?":
                    return "ON"
                if command == ":OUTP2?":
                    return "0"
                return "0"

            def write(self, command, *, recover=False):
                del command, recover

            def write_many(self, commands, *, recover=False):
                del commands, recover

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
        self.assertEqual(
            window.channel_controls[1]["frequency"].maximum_base,
            160_000_000,
        )
        self.assertTrue(
            self._wait_for(
                lambda: window.overview_metrics["connection"].text() == "已连接"
                and window.overview_metrics["channel_1"].text() == "已打开"
                and window.overview_metrics["channel_2"].text() == "关闭"
            )
        )
        home.disconnect_device(self.dg.id)
        self.assertTrue(
            self._wait_for(
                lambda: not manager.is_connected(self.dg)
                and home.cards[self.dg.id].connect_button.isEnabled()
                and window.overview_metrics["connection"].text() == "未连接"
                and window.overview_metrics["channel_1"].text() == "未知"
            )
        )
        self.assertTrue(home.cards[self.dg.id].connect_button.isEnabled())
        window.close()
        self.app.processEvents()
        home.close()
        self.app.processEvents()

    def test_open_dg_window_discovers_ni_added_and_connected_later(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "dynamic-ni")
        dg = repository.save(
            DeviceRecord.create(
                kind=DeviceKind.DG4000,
                name="虚拟 DG",
                resource=VIRTUAL_DG_RESOURCE,
                connection="内置模拟器",
            )
        )
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.open_device(dg.id)
        dg_window = home.open_windows[dg.id]
        self.assertIsNone(dg_window.ni_source.currentData())
        self.assertFalse(dg_window.ni_source.isEnabled())

        ni = repository.save(
            DeviceRecord.create(
                kind=DeviceKind.NI_DAQ,
                name="后添加的虚拟 NI",
                resource=VIRTUAL_NI_RESOURCE,
                connection="内置模拟器",
            )
        )
        home.reload_devices()
        self.assertGreaterEqual(dg_window.ni_source.findData(ni.id), 0)
        self.assertIn("未连接", dg_window.ni_source.currentText())

        home.open_device(ni.id)
        ni_window = home.open_windows[ni.id]
        self.assertEqual(ni_window.ni_overview_metrics["connection"].text(), "未连接")
        home.connect_device(ni.id)
        self.assertTrue(
            self._wait_for(
                lambda: manager.is_connected(ni)
                and ni_window.ni_overview_metrics["connection"].text() == "已连接"
                and dg_window.ni_source.findData(ni.id) >= 0
                and "已连接" in dg_window.ni_source.currentText()
            )
        )
        home.close()
        self.app.processEvents()

    def test_virtual_lab_end_to_end_linkage_without_hardware(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "virtual")
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.create_virtual_lab()
        self.assertTrue(
            self._wait_for(
                lambda: len(repository.list()) == 6
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
        self.assertTrue(
            self._wait_for(
                lambda: dg_window.overview_metrics["connection"].text() == "已连接"
                and dg_window.overview_metrics["channel_1"].text() == "关闭"
            )
        )
        self.assertEqual(frequency_control.current_unit(), "kHz")
        self.assertEqual(frequency_control.value(), 1000)
        frequency_control.set_unit("MHz")
        self.assertEqual(frequency_control.display_value(), 0.001)
        self.assertEqual(frequency_control.value(), 1000)
        channel_controls = dg_window.channel_controls[1]
        self.assertNotIn("load", channel_controls)
        impedance_controls = dg_window.impedance_controls[1]
        self.assertEqual(dg_window.impedance_controls[2]["load"].currentData(), 50)
        self.assertEqual(impedance_controls["load"].currentData(), 50)
        impedance_controls["load"].setCurrentIndex(
            impedance_controls["load"].findData(75)
        )
        dg_window.impedance_controls[2]["load"].setCurrentIndex(
            dg_window.impedance_controls[2]["load"].findData(600)
        )
        dg_window._apply_all_output_loads()
        self.assertTrue(
            self._wait_for(
                lambda: (
                    manager.virtual_lab.generator_snapshot(dg.resource)
                    .channels[0]
                    .load_ohms
                    == 75
                    and manager.virtual_lab.generator_snapshot(dg.resource)
                    .channels[1]
                    .load_ohms
                    == 600
                )
            )
        )
        frequency_control.setValue(1234)
        channel_controls["output"].setChecked(True)
        dg_window._apply_channel(1)
        self.assertTrue(
            self._wait_for(
                lambda: (
                    manager.virtual_lab.generator_snapshot(dg.resource)
                    .channels[0]
                    .frequency_hz
                    == 1234
                    and manager.virtual_lab.generator_snapshot(dg.resource)
                    .channels[0]
                    .load_ohms
                    == 75
                    and manager.virtual_lab.generator_snapshot(dg.resource)
                    .channels[0]
                    .output
                    and dg_window.overview_metrics["channel_1"].text() == "已打开"
                )
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

    def test_lms_window_uses_basic_readback_and_disables_only_blx_sweep_step(self):
        from dataclasses import replace
        from unittest.mock import patch

        record = DeviceRecord.create(kind=DeviceKind.VAUNIX_LMS, name="LMS-802",
                                     resource=VIRTUAL_VAUNIX_RESOURCE)
        window = VaunixLmsWindow(record, [], parameter_repository=DeviceParameterRepository(self.temporary.name))
        self.addCleanup(window.close)
        device = window.device
        device._caps = replace(device.capabilities, model="LMS-802", is_blx=False,
                               has_fast_pulse=False, has_chirp=False)
        device.connect()
        basic = device.output_snapshot()
        full = replace(device.snapshot(), sweep_step_hz=None)
        with patch.object(device, "output_snapshot", return_value=basic) as read_basic, \
             patch.object(device, "snapshot", side_effect=AssertionError("advanced read on connect")):
            window._connected(device.identity)
            self.assertTrue(self._wait_for(lambda: read_basic.called and
                            window.overview_metrics["frequency"].text() != "—"))
        self.assertFalse(window.sweep_step.isEnabled())
        self.assertTrue(window.sweep_time.isEnabled())
        self.assertTrue(window.trigger_frequency_step.isEnabled())
        before = window.sweep_step.value()
        with patch.object(device, "snapshot", return_value=full):
            window._refresh_all()
            self.assertTrue(self._wait_for(lambda: not window._call_tasks))
        self.assertEqual(window.sweep_step.value(), before)
        self.assertEqual(window.overview_metrics["power"].text(), f"{basic.power_dbm:.6g} dBm")

    def test_virtual_vaunix_full_window_and_ni_frequency_power_linkage(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "vaunix-ui")
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.create_virtual_lab()
        self.assertTrue(
            self._wait_for(
                lambda: len(repository.list()) == 6
                and all(manager.is_connected(item) for item in repository.list())
            )
        )
        vaunix = next(
            item
            for item in repository.list()
            if item.resource == VIRTUAL_VAUNIX_RESOURCE
        )
        ni = next(
            item for item in repository.list() if item.resource == VIRTUAL_NI_RESOURCE
        )
        home.open_device(vaunix.id)
        window = home.open_windows[vaunix.id]
        self.assertIsInstance(window, VaunixLmsWindow)
        self.assertTrue(
            self._wait_for(
                lambda: window.overview_metrics["connection"].text() == "已连接"
            )
        )
        window.output_frequency.setValue(8.5e9)
        window.output_power.setValue(-12.5)
        window.output_rf.setChecked(True)
        window._apply_output()
        device = manager.vaunix_device_for(vaunix)
        self.assertTrue(
            self._wait_for(
                lambda: abs(device.snapshot().frequency_hz - 8.5e9) < 1
                and abs(device.snapshot().power_dbm + 12.5) < 1e-9
                and device.snapshot().rf_on
            )
        )

        window.trigger_frequency_start.setValue(5e9)
        window.trigger_frequency_step.setValue(1e9)
        window.trigger_frequency_count.setValue(2)
        window.trigger_power_enabled.setChecked(True)
        window.trigger_power_start.setValue(-10.0)
        window.trigger_power_step.setValue(5.0)
        window.trigger_power_count.setValue(2)
        window._generate_trigger_table(show_message=False)
        self.assertEqual(window.trigger_table.rowCount(), 2)
        self.assertEqual(window.trigger_ni.currentData(), ni.id)
        window._start_trigger()
        self.assertTrue(
            self._wait_for(
                lambda: window.controller is not None
                and window.controller.snapshot().state is ControllerState.RUNNING
            )
        )
        manager.virtual_lab.emit_trigger(ni.resource, 3)
        self.assertTrue(
            self._wait_for(
                lambda: window.controller.snapshot().applied_count == 3
                and window.trigger_metrics["applied"].text() == "3"
            )
        )
        self.assertAlmostEqual(device.snapshot().frequency_hz, 5e9)
        self.assertAlmostEqual(device.snapshot().power_dbm, -10.0)
        window._stop_trigger()
        self.assertTrue(self._wait_for(lambda: window.controller is None))
        home.close()
        self.app.processEvents()

    def test_vaunix_parameters_sequence_and_trigger_table_restore_per_device(self) -> None:
        parameter_repository = DeviceParameterRepository(
            Path(self.temporary.name) / "vaunix-parameters"
        )
        manager = DeviceConnectionManager()
        vaunix = DeviceRecord.create(
            kind=DeviceKind.VAUNIX_LMS,
            name="虚拟 Vaunix",
            resource=VIRTUAL_VAUNIX_RESOURCE,
            connection="内置模拟器",
        )
        ni = DeviceRecord.create(
            kind=DeviceKind.NI_DAQ,
            name="虚拟 NI",
            resource=VIRTUAL_NI_RESOURCE,
            connection="内置模拟器",
        )
        manager.connect(vaunix)
        manager.connect(ni)
        window = VaunixLmsWindow(
            vaunix,
            [ni],
            manager,
            parameter_repository=parameter_repository,
        )
        self.assertAlmostEqual(window.output_power.singleStep(), 0.5)
        self.assertAlmostEqual(window.trigger_power_start.singleStep(), 0.5)
        self.assertAlmostEqual(window.trigger_power_step.singleStep(), 0.5)
        window.trigger_power_enabled.setChecked(True)
        window.trigger_power_start.setValue(-10.25)
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            window._trigger_axes_values()
        window.trigger_power_start.setValue(-10.0)
        window.sequence_table.item(0, 2).setText("-9.25")
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            window._sequence_elements()
        window.output_frequency.set_unit("MHz")
        window.output_frequency.setValue(7.25e9)
        window.output_power.setValue(-11.5)
        window.sequence_table.item(0, 1).setText("7123.5")
        window.sequence_table.item(0, 2).setText("-9.5")
        window.trigger_frequency_count.setValue(2)
        window.trigger_power_count.setValue(2)
        window._generate_trigger_table(show_message=False)
        window.trigger_table.item(1, 2).setText("-3.75")
        with self.assertRaisesRegex(ValueError, "0.5 dB"):
            window._trigger_points()
        window.trigger_table.item(1, 2).setText("-3.5")
        window.close()
        self.app.processEvents()

        restored = VaunixLmsWindow(
            vaunix,
            [ni],
            manager,
            parameter_repository=parameter_repository,
        )
        self.assertEqual(restored.output_frequency.current_unit(), "MHz")
        self.assertAlmostEqual(restored.output_frequency.value(), 7.25e9)
        self.assertAlmostEqual(restored.output_power.value(), -11.5)
        self.assertEqual(restored.sequence_table.item(0, 1).text(), "7123.5")
        self.assertEqual(restored.sequence_table.item(0, 2).text(), "-9.5")
        self.assertTrue(restored.trigger_power_enabled.isChecked())
        self.assertEqual(restored.trigger_table.rowCount(), 2)
        self.assertEqual(restored.trigger_table.item(1, 2).text(), "-3.5")
        restored.close()
        manager.close_all()
        self.app.processEvents()

    def test_virtual_analog_input_acquires_and_streams_four_channels_in_parallel(self) -> None:
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
        self.assertEqual(window.ai_channel_combo.count(), 4)
        self.assertTrue(window.ai_channel_combo.isEditable())
        self.assertTrue(window.ai_channel_combo.lineEdit().isReadOnly())
        window.ai_channel_combo.showPopup()
        self.app.processEvents()
        second_channel = window.ai_channel_combo.model().index(1, 0)
        QTest.mouseClick(
            window.ai_channel_combo.view().viewport(),
            Qt.MouseButton.LeftButton,
            pos=window.ai_channel_combo.view().visualRect(second_channel).center(),
        )
        self.app.processEvents()
        self.assertEqual(
            window.ai_channel_combo.itemData(1, Qt.ItemDataRole.CheckStateRole),
            Qt.CheckState.Checked,
        )
        self.assertTrue(window.ai_channel_combo.view().isVisible())
        window.ai_channel_combo.hidePopup()
        window._set_all_analog_channels(True)
        self.assertEqual(window.ai_channel_summary.text(), "已勾选 4 / 4 路")
        self.assertIn("已选 4 路", window.ai_channel_combo.currentText())
        self.assertTrue(all(
            window.ai_channel_combo.itemData(index, Qt.ItemDataRole.CheckStateRole)
            == Qt.CheckState.Checked
            for index in range(window.ai_channel_combo.count())
        ))
        self.assertAlmostEqual(window.ai_plot.seconds_per_division, 0.02)
        self.assertIsNone(window.ai_plot.volts_per_division)
        window.ai_x_scale.setValue(0.005)
        window.ai_y_auto.setChecked(False)
        window.ai_y_scale.setValue(0.25)
        window.ai_y_center.setValue(0.1)
        self.assertAlmostEqual(window.ai_plot.seconds_per_division, 0.005)
        self.assertAlmostEqual(window.ai_plot.volts_per_division, 0.25)
        self.assertAlmostEqual(window.ai_plot.voltage_center_v, 0.1)
        self.assertIn("25 ms", window.ai_plot_span.text())
        window.ai_sample_rate.setValue(20_000)
        window.ai_block_size.setValue(200)
        stream_path = Path(self.temporary.name) / "continuous-ui.csv"
        window.ai_stream_path.setText(str(stream_path))
        window._start_analog_input()
        self.assertTrue(
            self._wait_for(
                lambda: window.analog_controller is not None
                and window.analog_controller.snapshot().total_samples >= 1200,
                timeout=2.0,
            )
        )
        self.assertTrue(
            self._wait_for(lambda: window.ai_plot.displayed_samples >= 1200)
        )
        self.assertGreaterEqual(window.ai_plot.displayed_samples, 1200)
        self.assertEqual(window.ai_plot.visible_samples, 500)
        self.assertGreater(
            window.ai_plot.displayed_samples,
            window.ai_plot.visible_samples,
        )
        self.assertEqual(window.ai_metrics["channels"].text(), "4")
        self.assertGreater(int(window.ai_metrics["saved"].text()), 0)
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
        self.assertTrue(stream_path.exists())
        self.assertGreater(stream_path.stat().st_size, 1000)
        with stream_path.open("r", encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.reader(stream))
        self.assertEqual(rows[5][2:], [
            f"{VIRTUAL_NI_RESOURCE}/ai{index}" for index in range(4)
        ])
        self.assertTrue(all(len(row) == 6 for row in rows[6:]))
        self.assertFalse(
            any(
                button.text() == "保存波形图片"
                for button in window.findChildren(QPushButton)
            )
        )
        window.close()
        self.app.processEvents()

    def test_cross_device_task_window_drives_two_virtual_generators(self) -> None:
        repository = DeviceRepository(Path(self.temporary.name) / "cross-device")
        manager = DeviceConnectionManager()
        home = DeviceCenterWindow(repository, manager)
        home.create_virtual_lab()
        self.assertTrue(
            self._wait_for(
                lambda: len(repository.list()) == 6
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
        window.add_axis_button.click()
        self.app.processEvents()
        self.assertEqual(len(window.axis_editors), 2)
        second = window.axis_editors[1]
        second.device.setCurrentIndex(second.device.findData(generators[1].id))
        second.parameter.setCurrentIndex(second.parameter.findData(StepParameter.AMPLITUDE))
        second.start.setValue(0.5)
        second.step.setValue(0.25)
        second.count.setValue(2)
        window.counter.setCurrentIndex(window.counter.findData("ctr2"))
        window.terminal.setCurrentText("PFI7")
        window.edge.setCurrentIndex(1)
        window._generate_table(show_message=False)
        self.assertEqual(window.point_table.rowCount(), 2)
        window.point_table.item(1, 2).setText("0.875")
        window.save_task_button.click()
        self.app.processEvents()
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
        saved_task_id = window._current_task_id
        window.close()
        self.app.processEvents()

        restored = LinkageTaskWindow(repository, manager)
        self.assertEqual(restored._current_task_id, saved_task_id)
        self.assertEqual(restored.ni_source.currentData(), window.ni_source.currentData())
        self.assertEqual(restored.counter.currentData(), "ctr2")
        self.assertEqual(restored.terminal.currentText(), "PFI7")
        self.assertEqual(restored.edge.currentIndex(), 1)
        self.assertEqual(len(restored.axis_editors), 2)
        self.assertEqual(restored.axis_editors[0].device.currentData(), generators[0].id)
        self.assertEqual(restored.axis_editors[1].device.currentData(), generators[1].id)
        self.assertEqual(restored.point_table.item(1, 2).text(), "0.875")
        restored.close()
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
