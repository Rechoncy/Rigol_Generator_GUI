from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QTimer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rigol_control.app import create_application
from rigol_control.connections import DeviceConnectionManager
from rigol_control.drivers.dg4000 import StepParameter
from rigol_control.drivers.vaunix_lms import VIRTUAL_VAUNIX_RESOURCE
from rigol_control.models import DeviceKind
from rigol_control.repository import DeviceRepository
from rigol_control.ui.dialogs import DeviceDialog
from rigol_control.ui.main_window import DeviceCenterWindow
from rigol_control.virtual_lab import VIRTUAL_DG2_RESOURCE, VIRTUAL_DG_RESOURCE, VIRTUAL_NI_RESOURCE


OUTPUT = ROOT / "tmp" / "pdfs" / "screenshots"
DATA = ROOT / "tmp" / "pdfs" / "manual-data"


def wait(app, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    if not predicate():
        raise TimeoutError("界面状态等待超时")


def capture(widget, name: str, app) -> None:
    app.processEvents()
    path = OUTPUT / name
    if not widget.grab().save(str(path)):
        raise RuntimeError(f"无法保存截图：{path}")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    data_resolved = DATA.resolve()
    expected_parent = (ROOT / "tmp" / "pdfs").resolve()
    if DATA.exists() and expected_parent in data_resolved.parents:
        shutil.rmtree(DATA)
    DATA.mkdir(parents=True, exist_ok=True)

    app = create_application(["manual-screenshots"])
    repository = DeviceRepository(DATA)
    manager = DeviceConnectionManager()
    home = DeviceCenterWindow(repository, manager)
    home.resize(1240, 790)
    home.show()
    home.create_virtual_lab()
    wait(
        app,
        lambda: len(repository.list()) == 5
        and all(manager.is_connected(record) for record in repository.list()),
    )
    capture(home, "01-device-center.png", app)

    dialog = DeviceDialog(parent=home)
    dialog.show()
    wait(app, lambda: dialog.resource.count() >= 1)
    capture(dialog, "02-add-device-discovery.png", app)
    dialog.close()

    records = repository.list()
    dg = next(record for record in records if record.resource == VIRTUAL_DG_RESOURCE)
    ni = next(record for record in records if record.resource == VIRTUAL_NI_RESOURCE)
    dg2 = next(record for record in records if record.resource == VIRTUAL_DG2_RESOURCE)
    vaunix = next(
        record for record in records if record.resource == VIRTUAL_VAUNIX_RESOURCE
    )
    home.open_device(dg.id)
    home.open_device(ni.id)
    home.open_device(vaunix.id)
    wait(
        app,
        lambda: dg.id in home.open_windows
        and ni.id in home.open_windows
        and vaunix.id in home.open_windows,
    )
    dg_window = home.open_windows[dg.id]
    ni_window = home.open_windows[ni.id]
    vaunix_window = home.open_windows[vaunix.id]
    dg_window.resize(1180, 760)
    ni_window.resize(1180, 760)
    vaunix_window.resize(1180, 760)

    dg_window._select_page(0)
    capture(dg_window, "03-virtual-dg-overview.png", app)
    dg_window._select_page(1)
    capture(dg_window, "04-channel-waveform.png", app)
    dg_window._select_page(5)
    capture(dg_window, "10-system-impedance.png", app)
    ni_window._select_page(0)
    capture(ni_window, "05-virtual-ni-trigger-console.png", app)
    vaunix_window._select_page(2)
    capture(vaunix_window, "11-vaunix-sweep-chirp.png", app)
    vaunix_window._select_page(5)
    capture(vaunix_window, "12-vaunix-ni-frequency-power-step.png", app)

    dg_window._select_page(4)
    dg_window._add_scan_axis()
    dg_window._generate_scan_table(show_message=False)
    dg_window.simulation_delay.setValue(80)
    dg_window._start_trigger()
    wait(
        app,
        lambda: dg_window.controller is not None
        and dg_window.controller.snapshot().state.value == "running",
    )
    ni_window._emit_virtual_triggers(12)
    wait(
        app,
        lambda: dg_window.controller.snapshot().received_count == 12
        and dg_window.controller.snapshot().backlog > 0,
    )
    capture(dg_window, "06-trigger-backlog.png", app)
    wait(app, lambda: dg_window.controller.snapshot().applied_count == 12, timeout=5.0)
    capture(dg_window, "07-trigger-caught-up.png", app)

    dg_window._stop_trigger()
    wait(app, lambda: dg_window.controller is None)

    home.open_linkage_tasks()
    linkage = home.linkage_window
    linkage.resize(1320, 850)
    first_axis = linkage.axis_editors[0]
    first_axis.device.setCurrentIndex(first_axis.device.findData(dg.id))
    first_axis.start.setValue(1)
    first_axis.step.setValue(1)
    first_axis.count.setValue(4)
    linkage._add_axis()
    second_axis = linkage.axis_editors[1]
    second_axis.device.setCurrentIndex(second_axis.device.findData(dg2.id))
    second_axis.parameter.setCurrentIndex(
        second_axis.parameter.findData(StepParameter.AMPLITUDE)
    )
    second_axis.start.setValue(0.5)
    second_axis.step.setValue(0.25)
    second_axis.count.setValue(4)
    linkage._generate_table(show_message=False)
    linkage._start()
    wait(
        app,
        lambda: linkage.controller is not None
        and linkage.controller.snapshot().state.value == "running",
    )
    linkage._emit_virtual(5)
    wait(app, lambda: linkage.controller.snapshot().applied_count == 5, timeout=5.0)
    linkage.scroll.verticalScrollBar().setValue(
        linkage.scroll.verticalScrollBar().maximum()
    )
    capture(linkage, "09-cross-device-linkage.png", app)
    linkage._stop()

    ni_window._select_page(2)
    ni_window._set_all_analog_channels(True)
    ni_window.ai_sample_rate.setValue(10_000)
    ni_window.ai_block_size.setValue(500)
    ni_window.ai_stream_path.setText(str(DATA / "continuous-ni-input.csv"))
    ni_window._start_analog_input()
    wait(
        app,
        lambda: ni_window.analog_controller is not None
        and ni_window.analog_controller.snapshot().total_samples >= 2500
        and ni_window.ai_plot.displayed_samples >= 2000,
    )
    capture(ni_window, "08-ni-analog-input.png", app)
    ni_window._stop_analog_input()
    wait(app, lambda: ni_window.analog_controller.snapshot().state.value == "stopped")
    newport = next(record for record in records if record.kind is DeviceKind.NEWPORT_SMC100)
    home.open_device(newport.id)
    newport_window = home.open_windows[newport.id]
    newport_window.directory.setText(str(DATA / "rotation_runs"))
    newport_window.refresh_button.click()
    wait(app, lambda: not newport_window._manual_busy)
    capture(newport_window, "20-newport-motion.png", app)
    newport_window.count.setValue(3)
    newport_window.ai_enabled.setChecked(True)
    newport_window.ai_channels.setText("ai0,ai1")
    newport_window._select_page(1)
    newport_window.start_button.click()
    wait(app, lambda: newport_window.controller is not None and newport_window.controller.snapshot().phase == "holding")
    newport_window.inject_button.click()
    wait(app, lambda: newport_window.controller.snapshot().completed == 1)
    newport_window._tick()
    capture(newport_window, "21-newport-rotation-scan.png", app)
    newport_window._select_page(2)
    wait(app, lambda: newport_window.plot.displayed_samples >= 2000)
    capture(newport_window, "22-newport-analog.png", app)
    newport_window.stop_button.click()
    wait(app, lambda: not newport_window.controller.is_running)
    config_export = DATA / "full-configuration.json"
    home.export_configuration(config_export)
    def capture_import_preview():
        dialog = app.activeModalWidget()
        if dialog is not None:
            capture(dialog, "23-configuration-import.png", app)
            dialog.reject()
    QTimer.singleShot(100, capture_import_preview)
    home.import_configuration(config_export)
    home.close()
    app.processEvents()
    print(f"已生成 {len(list(OUTPUT.glob('*.png')))} 张截图：{OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
