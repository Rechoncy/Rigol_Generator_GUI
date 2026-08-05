import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceRepository
from rigol_control.services import DiscoveredResource
from rigol_control.ui.device_windows import DG4000Window, NIDeviceWindow
from rigol_control.ui.dialogs import DeviceDialog
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.virtual_lab import VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


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
                lambda: len(repository.list()) == 2
                and all(manager.is_connected(item) for item in repository.list())
            )
        )
        records = repository.list()
        dg = next(item for item in records if item.resource == VIRTUAL_DG_RESOURCE)
        ni = next(item for item in records if item.resource == VIRTUAL_NI_RESOURCE)
        home.open_device(dg.id)
        home.open_device(ni.id)
        dg_window = home.open_windows[dg.id]
        ni_window = home.open_windows[ni.id]
        dg_window._select_page(4)
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
        home.close()
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
