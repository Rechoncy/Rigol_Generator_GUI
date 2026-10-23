"""Windowed PyInstaller entry point and hardware-free release acceptance check."""
from __future__ import annotations

import ctypes
import faulthandler
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone


def _write_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _report_startup_error(detail: str, *, interactive: bool) -> None:
    """Keep failures visible even if importing Qt or our runtime module failed."""
    roots = [Path(sys.executable).resolve().parent / "logs"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "RigolGeneratorControl" / "logs")
    log_path = None
    for root in roots:
        try:
            root.mkdir(parents=True, exist_ok=True)
            candidate = root / "startup-error.log"
            with candidate.open("a", encoding="utf-8") as stream:
                stream.write(f"\n[{datetime.now(timezone.utc).isoformat()}]\n{detail}\n")
            log_path = candidate
            break
        except OSError:
            continue
    message = "实验室设备中心启动或运行失败。\n\n" + detail[-2400:]
    message += f"\n\n详细日志：{log_path}" if log_path else "\n\n错误日志无法写入，请检查目录权限。"
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if interactive and sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "实验室设备中心 — 错误", 0x10)


def _enable_fault_logging():
    """Keep Python thread stacks for native crashes that bypass exception hooks."""
    roots = [Path(sys.executable).resolve().parent / "logs"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "RigolGeneratorControl" / "logs")
    for root in roots:
        stream = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            stream = (root / "runtime-fault.log").open("a", encoding="utf-8")
            stream.write(f"\n[{datetime.now(timezone.utc).isoformat()}] process={os.getpid()}\n")
            stream.flush()
            faulthandler.enable(file=stream, all_threads=True)
            return stream  # It must remain open for the full QApplication lifetime.
        except (OSError, RuntimeError):
            if stream is not None:
                stream.close()
    return None


def _bundle_self_test(report_path: Path) -> int:
    """Check the bundled interpreter, dependencies and six virtual UI windows.

    No real device is enumerated or connected. All configuration is disposable,
    and the deadline remains effective if a Qt callback or import gets stuck.
    """
    report_path = report_path.expanduser().resolve()
    report = {
        "success": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": str(Path(sys.executable).resolve()),
        "python_version": sys.version,
        "sys_path": list(sys.path),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "virtual_device_count": 0,
        "virtual_window_count": 0,
        "hardware_access": False,
    }
    finished = threading.Event()

    def watchdog() -> None:
        if not finished.wait(60):
            failure = dict(report, success=False, error="Bundle self-test exceeded 60 seconds.")
            try:
                _write_json(report_path, failure)
            finally:
                os._exit(124)

    timer = threading.Thread(target=watchdog, name="bundle-self-test-deadline", daemon=True)
    timer.start()
    app = home = None
    dialog_errors: list[str] = []
    original_hook = sys.excepthook
    old_data = os.environ.pop("RIGOL_GUI_DATA_DIR", None)
    temporary = None
    try:
        # Import the real optional packages and their dynamically selected native
        # backends without opening VISA libraries, serial ports or NI devices.
        import PyQt6
        import pyvisa
        import pyvisa.ctwrapper.highlevel
        import serial
        import serial.tools.list_ports
        import nidaqmx
        import nidaqmx._library_interpreter
        import numpy
        if sys.platform == "win32":
            import serial.tools.list_ports_windows
        from PyQt6.QtCore import QLibraryInfo, QThreadPool, QT_VERSION_STR, PYQT_VERSION_STR, qVersion
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QMessageBox, QPushButton
        from rigol_control.runtime import (
            bundle_root, executable_dir, is_frozen, package_resource,
            prepare_data_dir, resolve_data_dir,
        )
        from rigol_control.app import create_application
        from rigol_control import __version__
        from rigol_control.repository import DeviceRepository
        from rigol_control.ui.main_window import DeviceCenterWindow

        root = bundle_root()
        default_data = resolve_data_dir()
        report.update(bundle_root=str(root), default_data_dir=str(default_data), software_version=__version__)
        build_info = root / "release" / "build-info.json"
        if build_info.is_file():
            info = json.loads(build_info.read_text(encoding="utf-8"))
            report["vaunix_dll"] = {"bundled": bool(info.get("vaunix_dll_bundled"))}
            if info.get("vaunix_dll_bundled"):
                from rigol_control.drivers.vaunix_lms import VaunixLmsApi, locate_vaunix_dll
                expected = info["vaunix_dll"]
                dll_path = locate_vaunix_dll()
                if dll_path != (root / "vendor/vaunix/vnx_fmsynth.dll").resolve():
                    raise AssertionError(f"Vaunix DLL came from outside the bundle: {dll_path}")
                digest = hashlib.sha256(dll_path.read_bytes()).hexdigest()
                if digest != expected["sha256"] or dll_path.stat().st_size != expected["bytes"]:
                    raise AssertionError("Bundled Vaunix DLL differs from the build input")
                # Bind every driver export, then query only the DLL version.
                # Do not construct the API object, enumerate devices or change RF.
                library = VaunixLmsApi.load_library(dll_path)
                report["vaunix_dll"].update(
                    path=str(dll_path), sha256=digest, bytes=dll_path.stat().st_size,
                    dll_version=hex(library.fnLMS_GetDLLVersion()),
                    bound_exports=sum(name.startswith("fnLMS_") for name in vars(library)),
                    loaded=True, hardware_access=False,
                )
                # This SDK imports the MSVC runtime. Loading successfully on a
                # development machine alone could hide an unbundled runtime.
                if is_frozen() and sys.platform == "win32":
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
                    kernel.GetModuleHandleW.restype = ctypes.c_void_p
                    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
                    kernel.GetModuleFileNameW.restype = ctypes.c_uint32
                    runtimes = {}
                    for name in ("MSVCP140.dll", "VCRUNTIME140.dll", "VCRUNTIME140_1.dll"):
                        handle = kernel.GetModuleHandleW(name)
                        buffer = ctypes.create_unicode_buffer(32768)
                        if not handle or not kernel.GetModuleFileNameW(handle, buffer, len(buffer)):
                            raise AssertionError(f"Loaded Vaunix runtime could not be located: {name}")
                        runtime_path = Path(buffer.value).resolve()
                        if not runtime_path.is_relative_to(root):
                            raise AssertionError(f"Vaunix runtime came from outside the bundle: {runtime_path}")
                        runtimes[name] = str(runtime_path)
                    report["vaunix_dll"]["native_runtime"] = runtimes
        if is_frozen() and default_data != executable_dir() / "data":
            raise AssertionError(f"Portable default data directory is incorrect: {default_data}")
        dependencies = {
            "PyQt6": PyQt6, "PyVISA": pyvisa, "pyserial": serial,
            "nidaqmx": nidaqmx, "numpy": numpy,
        }
        report["dependencies"] = {
            name: {"version": importlib.metadata.version(name), "file": str(module.__file__)}
            for name, module in dependencies.items()
        }
        if is_frozen():
            for name, dependency in report["dependencies"].items():
                if not Path(dependency["file"]).resolve().is_relative_to(root):
                    raise AssertionError(f"Dependency came from outside the bundle: {name}")
        report["dynamic_backends"] = [
            "pyvisa.ctwrapper.highlevel", "serial.tools.list_ports",
            "nidaqmx._library_interpreter",
        ]
        if sys.platform == "win32":
            report["dynamic_backends"].append("serial.tools.list_ports_windows")

        report_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="bundle-self-test-", dir=report_path.parent)
        data_dir = prepare_data_dir(Path(temporary.name) / "data")
        os.environ["RIGOL_GUI_DATA_DIR"] = str(data_dir)
        report["test_data_dir"] = str(data_dir)

        # Exercise the shipped analog lifecycle with an entirely in-memory
        # reader: a failed native close must retain ownership until retried.
        from rigol_control.analog import AnalogAcquisitionController, AnalogAcquisitionState

        class CleanupProbeReader:
            def __init__(self):
                self.reading = threading.Event()
                self.cancelled = threading.Event()
                self.close_attempts = 0

            def start(self):
                pass

            def read(self):
                self.reading.set()
                if not self.cancelled.wait(5):
                    raise TimeoutError("Virtual analog cleanup probe was not cancelled")
                raise OSError("Virtual analog read cancelled")

            def stop(self):
                self.close_attempts += 1
                self.cancelled.set()
                if self.close_attempts == 1:
                    raise OSError("BUNDLE_ANALOG_CLOSE_FAILURE")

        analog_reader = CleanupProbeReader()
        analog_probe = AnalogAcquisitionController(analog_reader, maximum_retained_samples=1000)
        try:
            analog_probe.start()
            if not analog_reader.reading.wait(2):
                raise AssertionError("Virtual analog reader did not start")
            try:
                analog_probe.stop(timeout_seconds=2)
            except RuntimeError as exc:
                if "BUNDLE_ANALOG_CLOSE_FAILURE" not in str(exc):
                    raise
            else:
                raise AssertionError("Analog cleanup failure was reported as a successful stop")
            snapshot = analog_probe.snapshot()
            if (snapshot.state is not AnalogAcquisitionState.ERROR
                    or not snapshot.cleanup_pending or not analog_probe.is_running):
                raise AssertionError("Analog cleanup failure did not retain the active task")
            try:
                analog_probe.clear()
            except RuntimeError:
                pass
            else:
                raise AssertionError("Analog data was cleared before failed cleanup finished")
            analog_probe.stop(timeout_seconds=2)
            if analog_probe.is_running or analog_probe.snapshot().cleanup_pending:
                raise AssertionError("Explicit analog cleanup retry did not finish")
            if analog_reader.close_attempts != 2:
                raise AssertionError("Analog cleanup was automatically replayed")
            report["analog_cleanup_retry"] = {
                "failed_close_reported": True, "ownership_retained": True,
                "clear_blocked_during_cleanup": True, "explicit_retry_completed": True,
                "no_automatic_close_replay": True, "hardware_access": False,
            }
        finally:
            analog_reader.cancelled.set()
            if analog_probe.is_running:
                analog_probe.stop(timeout_seconds=2)

        # Exercise the actual packaged nidaqmx close semantics, still without
        # constructing a native task or loading any NI driver.
        from types import SimpleNamespace
        from rigol_control.drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
        from rigol_control.drivers.ni_counter import NIDaqCleanupUncertainError

        clear_attempts = []

        def failed_native_clear(handle):
            clear_attempts.append(handle)
            raise OSError("BUNDLE_NATIVE_CLEAR_FAILURE")

        native_task = object.__new__(nidaqmx.Task)
        native_task._handle = 123
        native_task._saved_name = "in-memory-bundle-probe"
        native_task._grpc_options = None
        native_task._event_handler_lock = threading.RLock()
        native_task._event_handlers = {}
        native_task._interpreter = SimpleNamespace(stop_task=lambda _handle: None,
                                                   clear_task=failed_native_clear)
        native_reader = NIDaqAnalogInput("BUNDLE_NO_HARDWARE", AnalogInputConfig(("ai0",), 1000, 2))
        native_reader._task = native_task
        for _ in range(2):
            try:
                native_reader.stop()
            except NIDaqCleanupUncertainError:
                pass
            else:
                raise AssertionError("A lost NI native handle was reported as cleared")
        if not native_reader.is_running or clear_attempts != [123] or native_task._handle is not None:
            raise AssertionError("Unconfirmed native cleanup did not remain blocked")
        report["analog_cleanup_retry"]["lost_native_handle_never_treated_as_success"] = True

        def no_dialog(_parent, title, message, *_args, **_kwargs):
            dialog_errors.append(f"{title}: {message}")
            return QMessageBox.StandardButton.Ok

        for name in ("critical", "warning", "information", "question"):
            setattr(QMessageBox, name, no_dialog)
        sys.excepthook = lambda kind, value, tb: dialog_errors.append(
            "".join(traceback.format_exception(kind, value, tb))
        )
        app = create_application([sys.argv[0]])
        app.setQuitOnLastWindowClosed(False)
        icon_path = package_resource("assets", "app_icon.ico")
        if not icon_path.is_file() or QIcon(str(icon_path)).isNull() or app.windowIcon().isNull():
            raise AssertionError(f"Application icon is missing or unreadable: {icon_path}")
        plugins_path = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
        platform_plugins = sorted(path.name for path in (plugins_path / "platforms").glob("*"))
        if not platform_plugins:
            raise AssertionError(f"Qt platform plugins are missing: {plugins_path}")
        if sys.platform == "win32" and "qwindows.dll" not in platform_plugins:
            raise AssertionError("Windows Qt platform plugin qwindows.dll is missing")
        if is_frozen() and not plugins_path.resolve().is_relative_to(root):
            raise AssertionError(f"Qt plugins came from outside the bundle: {plugins_path}")
        report["qt"] = {
            "qt_version": qVersion(), "qt_compile_version": QT_VERSION_STR,
            "pyqt_version": PYQT_VERSION_STR,
            "platform": app.platformName(), "plugins_path": str(plugins_path),
            "platform_plugins": platform_plugins, "icon": str(icon_path),
        }
        repository = DeviceRepository(data_dir)
        home = DeviceCenterWindow(repository)
        home.show()
        home.create_virtual_lab()
        records = repository.list()
        report["virtual_device_count"] = len(records)
        if len(records) != 6 or any(not item.resource.startswith("SIM::") for item in records):
            raise AssertionError("Expected exactly six simulated devices")

        def wait_idle(*, require_connected: bool = False) -> None:
            deadline = time.monotonic() + 20
            stable_since = None
            while time.monotonic() < deadline:
                app.processEvents()
                if dialog_errors:
                    raise RuntimeError("\n".join(dialog_errors))
                busy = bool(home.connection_tasks or home.probe_tasks)
                busy = busy or QThreadPool.globalInstance().activeThreadCount() > 0
                for window in home.open_windows.values():
                    busy = busy or bool(getattr(window, "_call_tasks", ()))
                    busy = busy or bool(getattr(window, "_manual_busy", False))
                    flag = getattr(window, "_busy", False)
                    busy = busy or (flag() if callable(flag) else flag)
                if require_connected:
                    busy = busy or not all(home.connection_manager.is_connected(item) for item in records)
                if not busy:
                    stable_since = stable_since or time.monotonic()
                    if time.monotonic() - stable_since >= 0.2:
                        return
                else:
                    stable_since = None
                time.sleep(0.01)
            raise TimeoutError("Virtual device background work did not become idle within 20 seconds")

        wait_idle(require_connected=True)
        for record in records:
            home.open_device(record.id)
        wait_idle(require_connected=True)
        report["virtual_window_count"] = len(home.open_windows)
        report["virtual_windows"] = [type(window).__name__ for window in home.open_windows.values()]
        if len(home.open_windows) != 6:
            raise AssertionError("Expected six open virtual device windows")
        titles = [home.windowTitle()] + [window.windowTitle() for window in home.open_windows.values()]
        if not all(title.endswith(f"v{__version__}") for title in titles):
            raise AssertionError(f"Window titles do not identify the running version: {titles}")
        report["window_versions_visible"] = True
        # Verify the global input policy on the real six device windows,
        # including dynamically created numeric fields and unit dropdowns.
        from PyQt6.QtCore import QPoint, QPointF, Qt
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtWidgets import QApplication, QAbstractSpinBox, QComboBox
        checked_spins = checked_combos = 0
        for window in home.open_windows.values():
            for control in window.findChildren(QAbstractSpinBox) + window.findChildren(QComboBox):
                is_spin = isinstance(control, QAbstractSpinBox)
                before = control.value() if is_spin else control.currentIndex()
                control.setFocus()
                for delta in (120, -120):
                    point = control.rect().center()
                    event = QWheelEvent(QPointF(point), QPointF(control.mapToGlobal(point)),
                                        QPoint(), QPoint(0, delta), Qt.MouseButton.NoButton,
                                        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                    QApplication.sendEvent(control, event)
                    after = control.value() if is_spin else control.currentIndex()
                    if after != before:
                        raise AssertionError(f"Mouse wheel changed {type(window).__name__} input")
                if is_spin:
                    checked_spins += 1
                else:
                    checked_combos += 1
        if not checked_spins or not checked_combos:
            raise AssertionError("Wheel acceptance check found no numeric inputs or dropdowns")
        report["input_wheel_policy"] = {"numeric_inputs_unchanged": True, "closed_dropdowns_unchanged": True,
                                        "numeric_inputs_checked": checked_spins, "dropdowns_checked": checked_combos}
        # Exercise the packaged NI-to-DG path, keeping the other virtual
        # channel's entire state unchanged and never opening real hardware.
        from rigol_control.drivers.dg4000 import StepParameter
        from rigol_control.trigger import TriggerStepController
        from rigol_control.poll_waiter import _WINDOWS_HIGH_RESOLUTION_SLEEP
        dg = next(w for w in home.open_windows.values() if type(w).__name__ == "DG4000Window")
        ni = next(r for r in records if r.resource.startswith("SIM::NI_DAQ"))
        generator = dg.driver.session._generator
        ch1_before = generator.snapshot().channels[0]
        generator.commands.clear()
        controller = TriggerStepController(home.connection_manager.counter_for(ni), dg.driver,
                                           StepParameter.FREQUENCY, 2, [1000, 2000, 3000])
        try:
            controller.start()
            if controller.snapshot().applied_count:
                raise AssertionError("DG parameters changed before the first NI trigger")
            home.connection_manager.virtual_lab.emit_trigger(ni.resource, 25)
            deadline = time.monotonic() + 5
            while controller.snapshot().applied_count < 25 and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.002)
            if controller.snapshot().applied_count != 25 or controller.snapshot().backlog:
                raise AssertionError("DG trigger stepping lost cumulative edges")
        finally:
            controller.stop()
        if not controller.is_quiescent:
            raise AssertionError("DG trigger controller reported stopped with pending work")
        first_revision = controller.snapshot().revision
        if first_revision < 1 or controller.snapshot().revision <= first_revision:
            raise AssertionError("DG trigger snapshots have no monotonic revision")
        if generator.snapshot().channels[0] != ch1_before:
            raise AssertionError("DG CH2 trigger stepping modified CH1")
        if list(generator.commands).count(":COUP:STAT?") != 1:
            raise AssertionError("Coupling check ran per edge instead of once before arming")
        if any(list(generator.commands).count(f":SOUR{channel}:FUNC?") != 1 for channel in (1, 2)):
            raise AssertionError("Waveform checks did not run exactly once per channel before arming")
        from rigol_control.drivers.dg4000 import StepValue
        prepared = dg.driver.prepare_trigger()
        dg.driver.write(":SOUR2:VOLT:UNIT VRMS")
        prepared.apply_steps((StepValue(StepParameter.AMPLITUDE, 2, 1.25),))
        if generator.channels[2]["amplitude_vpp"] != 1.25:
            raise AssertionError("Typed DG amplitude inherited the instrument's Vrms unit")
        precise_frequencies = (100000000.000001, 100000000.000002)
        for frequency in precise_frequencies:
            prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, frequency),))
            if generator.channels[2]["frequency_hz"] != frequency:
                raise AssertionError("DG command serialization lost a microhertz step")
        if generator.snapshot().channels[0] != ch1_before:
            raise AssertionError("DG unit or precision checks changed CH1")
        generator.values["COUP:STAT"] = "FREQ:ON,PHASE:OFF,AMPL:OFF"
        try:
            try:
                controller.start()
            except ValueError:
                pass
            else:
                controller.stop()
                raise AssertionError("A coupled DG device was allowed to arm")
        finally:
            generator.values.pop("COUP:STAT", None)
        prepared = dg.driver.prepare_trigger()
        dg.driver.session.disconnect()
        dg.driver.session.connect()
        command_count = len(generator.commands)
        try:
            from rigol_control.drivers.dg4000 import StepValue
            prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 2, 4000),))
        except RuntimeError as exc:
            if "通道检查已失效" not in str(exc):
                raise
        else:
            raise AssertionError("A stale DG trigger writer survived reconnection")
        if len(generator.commands) != command_count:
            raise AssertionError("Stale DG trigger writer issued I/O after reconnection")
        report["dg_trigger_dispatch"] = {"ch2_preserved_ch1": True, "all_25_edges_applied": True,
                                          "one_coupling_query_per_run": True, "coupled_arming_blocked": True,
                                          "waveforms_read_once_per_channel": True,
                                          "amplitude_vpp_explicit": True, "microhertz_points_preserved": True,
                                          "quiescent_after_stop": True, "ordered_snapshot_revisions": True,
                                          "reconnected_writer_blocked_without_io": True,
                                          "high_resolution_poll_wait": _WINDOWS_HIGH_RESOLUTION_SLEEP}
        from rigol_control.scan import ScanPoint
        vaunix = next(w for w in home.open_windows.values() if type(w).__name__ == "VaunixLmsWindow")
        device = vaunix.device
        before = (device.frequency_hz, device.power_dbm)
        controller = TriggerStepController(
            home.connection_manager.counter_for(ni), device,
            scan_points=[ScanPoint((StepValue(StepParameter.FREQUENCY, 1, frequency),
                                    StepValue(StepParameter.AMPLITUDE, 1, power)))
                         for frequency, power in ((5e9, -10), (6e9, -9))],
        )
        try:
            controller.start()
            if (device.frequency_hz, device.power_dbm) != before:
                raise AssertionError("Vaunix arming changed output before the first trigger")
            home.connection_manager.virtual_lab.emit_trigger(ni.resource, 25)
            deadline = time.monotonic() + 5
            while controller.snapshot().applied_count < 25 and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(.002)
            if controller.snapshot().applied_count != 25 or controller.snapshot().backlog:
                raise AssertionError("Vaunix trigger stepping lost cumulative edges")
        finally:
            controller.stop()
        if not controller.is_quiescent or (device.frequency_hz, device.power_dbm) != (5e9, -10):
            raise AssertionError("Vaunix point order or stop cleanup failed")
        prepared = device.prepare_trigger()
        device.disconnect()
        device.connect()
        try:
            prepared.apply_steps((StepValue(StepParameter.FREQUENCY, 1, 7e9),))
        except RuntimeError as exc:
            if "原触发任务已失效" not in str(exc):
                raise
        else:
            raise AssertionError("A stale Vaunix trigger writer survived reconnection")
        if device.frequency_hz != 5e9 or device.rf_on:
            raise AssertionError("Stale Vaunix writer changed output or RF was enabled")
        report["vaunix_trigger_dispatch"] = {
            "arming_preserved_output": True, "all_25_edges_applied": True,
            "frequency_power_order_preserved": True, "quiescent_after_stop": True,
            "reconnected_writer_blocked_without_io": True, "rf_remained_off": True,
        }
        # Exercise the actual SSG connect button and its GUI completion callback,
        # including an invalid numeric reply. Merely opening the window misses it.
        ssg = next(window for window in home.open_windows.values() if type(window).__name__ == "SiglentWindow")
        connect_button = next(button for button in ssg.findChildren(QPushButton)
                              if button.text() == "连接 / 读取身份")
        original_query = ssg.session.query
        ssg.session.query = lambda command, **kwargs: "BUNDLE_INVALID_POWER" if command == ":POWer?" else original_query(command, **kwargs)
        try:
            connect_button.click()
            wait_idle(require_connected=True)
            if "[:POWer?]" not in ssg.log.toPlainText():
                raise AssertionError("Malformed SSG power reply was not reported")
            if json.loads(ssg.readback.toPlainText())[":POWer"] != "BUNDLE_INVALID_POWER":
                raise AssertionError("Malformed SSG power reply was not preserved")
            diagnostics = list((data_dir / "diagnostics").glob("siglent-*.json"))
            if len(diagnostics) != 1 or "BUNDLE_INVALID_POWER" not in diagnostics[0].read_text(encoding="utf-8"):
                raise AssertionError("SSG callback failure diagnostic was not saved")
            diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
            if (diagnostic["software_version"] != __version__
                    or diagnostic["executable"] != str(Path(sys.executable).resolve())
                    or diagnostic["frozen"] != is_frozen()
                    or not diagnostic["module_path"]):
                raise AssertionError("SSG diagnostic does not identify the running program")
            report["diagnostic_runtime_identified"] = True
        finally:
            ssg.session.query = original_query
        if home.connection_manager.active_leases():
            raise AssertionError("SSG callback failure retained an idle device lease")
        connect_button.click()
        wait_idle(require_connected=True)
        if "dBm" not in ssg.rf_status.text() or ssg.session.query(":OUTPut?") != "0":
            raise AssertionError("SSG could not recover readback with RF off")
        report["siglent_connect_button"] = {
            "malformed_power_contained": True, "raw_response_recorded": True,
            "diagnostic_saved": True, "retry_readback_passed": True, "rf_remained_off": True,
        }
        query_calls = []
        def ordinary_ssg_query(command, **kwargs):
            query_calls.append(command)
            if command == "*IDN?":
                return "SIGLENT,SSG3021X,BUNDLE_USB_TEST,3.1.21"
            if command.upper() == ":OUTPUT:MODULATION?":
                raise OSError("VI_ERROR_TMO: unsupported equivalent modulation query")
            return original_query(command, **kwargs)
        ssg.session.query = ordinary_ssg_query
        try:
            connect_button.click()
            wait_idle(require_connected=True)
            if ssg.driver.caps.model != "SSG3021X" or "BUNDLE_USB_TEST" not in ssg.identity_label.text():
                raise AssertionError("SSG3021X identity was not displayed after basic connection")
            if any(command.upper().startswith((":MODULATION", ":OUTPUT:MODULATION", ":AM:", ":FM:", ":PM:", ":PULM:"))
                   for command in query_calls):
                raise AssertionError("Basic SSG connection unexpectedly queried modulation")
            modulation_button = next(button for button in ssg.findChildren(QPushButton)
                                     if button.text() == "读取调制状态")
            modulation_button.click()
            wait_idle(require_connected=True)
            if ":MODulation?" not in query_calls or "调制总开关=0" not in ssg.mod_status.text():
                raise AssertionError("SSG3021X explicit modulation read did not use its primary command")
            if ssg.session.query(":OUTPut?") != "0":
                raise AssertionError("SSG compatibility query enabled RF")
        finally:
            ssg.session.query = original_query
        report["siglent_usb_timeout_compatibility"] = {
            "basic_connection_without_modulation": True, "primary_modulation_command": True,
            "identity_visible": True, "rf_remained_off": True,
        }
        # Armed hardware must remain stoppable after a later query drops USB.
        ssg.poll_timer.stop()
        ssg.connect_device()  # Restore the virtual device's real identity before reconnect checks.
        wait_idle(require_connected=True)
        point_trigger = ssg.controls["sweep.point_trigger"]
        point_trigger.setCurrentIndex(point_trigger.findData("EXT"))
        ssg.apply_sweep()
        wait_idle(require_connected=True)
        ssg.start_sweep()
        wait_idle(require_connected=True)
        if not ssg.driver.scan_may_be_active:
            raise AssertionError("SSG scan was not armed for recovery check")
        query_calls = []
        fail_current_value = False
        def point_timeout(command, **kwargs):
            query_calls.append(command)
            if command == ":SWEep:CURRent:DATA?":
                raise AssertionError("Combined scan DATA query must not be used")
            if fail_current_value and command == ":SWEep:CURRent:FREQuency?":
                raise OSError("BUNDLE_CURRENT_POINT_TIMEOUT")
            return original_query(command, **kwargs)
        ssg.session.query = point_timeout
        try:
            ssg._poll()
            wait_idle(require_connected=True)
            if any(command.startswith(":SWEep:CURRent:") for command in query_calls):
                raise AssertionError("Ordinary scan polling queried the optional current point")
            next(b for b in ssg.findChildren(QPushButton) if b.text() == "刷新当前值预览").click()
            wait_idle(require_connected=True)
            if (ssg._current_scan_preview is None or ssg.scan_metrics["received"].text() != "未知"
                    or ssg.scan_metrics["backlog"].text() != "未知"):
                raise AssertionError("Scan monitor lacks current values or invented trigger statistics")
            if not all(command in query_calls for command in (":SWEep:CURRent:FREQuency?", ":SWEep:CURRent:LEVel?")):
                raise AssertionError("Scan monitor did not use separate numeric queries")
            fail_current_value = True
            ssg.scan_preview_auto.setChecked(True)
            next(b for b in ssg.findChildren(QPushButton) if b.text() == "刷新当前值预览").click()
            wait_idle()
            if ssg._current_scan_preview is not None or ssg.scan_preview_auto.isChecked():
                raise AssertionError("Failed scan preview retained stale values or automatic polling")
            if ssg.session.is_connected or not home.connection_manager.active_leases():
                raise AssertionError("Scan timeout must retain active-scan evidence after disconnect")
            query_calls.clear()
            next(b for b in ssg.findChildren(QPushButton) if b.text() == "停止扫描").click()
            wait_idle(require_connected=True)
            if query_calls != [":SWEep:STATe?"] or ssg.driver.state != "off":
                raise AssertionError("Stop did not reconnect and confirm OFF with only essential readback")
            if ssg.session.query(":OUTPut?") != "0":
                raise AssertionError("Scan recovery changed RF output")
        finally:
            ssg.session.query = original_query
        report["siglent_scan_stop_recovery"] = {
            "ordinary_poll_skips_current_point": True, "timeout_retains_scan_lease": True,
            "stop_button_reconnects": True, "off_confirmed_without_full_refresh": True,
            "rf_remained_off": True,
        }
        report["siglent_scan_monitor"] = {"separate_current_queries": True, "trigger_counts_unknown": True,
                                           "failed_preview_invalidated": True, "auto_refresh_stopped_on_error": True}
        # Hold the real session I/O lock while the home page checks status.
        # Immediate timeout mocks cannot detect this GUI-thread lock regression.
        from PyQt6.QtCore import QTimer
        ssg.apply_sweep()
        wait_idle(require_connected=True)
        ssg.start_sweep()
        wait_idle(require_connected=True)
        backend = ssg.session._backend
        backend_query = backend.query
        entered, release = threading.Event(), threading.Event()
        def slow_preview(command):
            if command == ":SWEep:CURRent:FREQuency?":
                entered.set()
                release.wait(4)
                raise OSError("BUNDLE_SLOW_PREVIEW_TIMEOUT")
            return backend_query(command)
        backend.query = slow_preview
        watchdog = threading.Timer(4, release.set)
        watchdog.start()
        try:
            ssg.refresh_scan_preview()
            if not entered.wait(2):
                raise AssertionError("Slow scan preview did not begin")
            started = time.monotonic()
            home._sync_connection_states()
            if time.monotonic() - started > .5:
                raise AssertionError("GUI connection status waited for VISA I/O")
            next(b for b in ssg.findChildren(QPushButton) if b.text() == "停止扫描").click()
            if not ssg._pending_safety or release.is_set():
                raise AssertionError("Stop button did not queue while preview was blocked")
            QTimer.singleShot(20, release.set)
            wait_idle(require_connected=True)
            if ssg.driver.state != "off" or home.connection_manager.active_leases():
                raise AssertionError("Queued stop did not recover after slow preview timeout")
        finally:
            release.set()
            watchdog.cancel()
            backend.query = backend_query
        report["siglent_slow_preview"] = {"status_nonblocking": True, "gui_timer_responsive": True,
                                          "stop_queued_during_query": True, "stop_recovered_after_timeout": True}
        if [b.text() for b in ssg.nav_buttons][1:3] != ["触发步进扫描", "列表扫描"]:
            raise AssertionError("STEP and LIST editors were not separated")
        for index, kind in ((1, "STEP"), (2, "LIST")):
            ssg._select_page(index)
            layout = ssg._scan_layouts[kind]
            if layout.itemAt(layout.count() - 1).widget() is not ssg.scan_monitor_widget:
                raise AssertionError("Shared scan monitor is not at the bottom of its page")
        ssg.apply_sweep("LIST")
        wait_idle(require_connected=True)
        if ssg.driver.prepared.kind != "LIST":
            raise AssertionError("LIST page did not prepare a LIST configuration")
        ssg.start_sweep("STEP")
        if ssg._busy or ssg.driver.state != "off":
            raise AssertionError("STEP editor was allowed to start a LIST configuration")
        ssg.start_sweep("LIST")
        wait_idle(require_connected=True)
        ssg._safety(ssg.driver.stop)
        wait_idle(require_connected=True)
        report["siglent_scan_pages"] = {"separate_editors": True, "bottom_monitor": True,
                                         "matching_configuration_required": True, "list_start_stop": True}
        if home.connection_manager.active_leases():
            raise AssertionError("Virtual windows retained an unexpected device operation lease")
        if not home.close():
            raise AssertionError("Main window refused to close after background work completed")
        home = None
        app.processEvents()
        if not QThreadPool.globalInstance().waitForDone(3000):
            raise TimeoutError("Qt background workers did not shut down")
        if dialog_errors:
            raise RuntimeError("\n".join(dialog_errors))
        report["success"] = True
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        sys.excepthook = original_hook
        try:
            if home is not None:
                home.close()
            if app is not None:
                app.processEvents()
                app.quit()
            if temporary is not None:
                temporary.cleanup()
                report["temporary_data_removed"] = True
        except Exception:
            report["success"] = False
            report["cleanup_error"] = traceback.format_exc()
        if old_data is None:
            os.environ.pop("RIGOL_GUI_DATA_DIR", None)
        else:
            os.environ["RIGOL_GUI_DATA_DIR"] = old_data
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_json(report_path, report)
        finally:
            finished.set()
    return 0 if report["success"] else 1


def main() -> int:
    args = sys.argv[1:]
    interactive = "--smoke-test" not in args and "--bundle-self-test" not in args
    if "--bundle-self-test" in args:
        if len(args) != 2 or args[0] != "--bundle-self-test":
            if sys.stderr is not None:
                print("Usage: LabControlCenter.exe --bundle-self-test <report.json>", file=sys.stderr)
            return 2
        return _bundle_self_test(Path(args[1]))

    uncaught = []
    fault_stream = _enable_fault_logging() if interactive else None

    def exception_hook(kind, value, tb):
        uncaught.append(True)
        _report_startup_error("".join(traceback.format_exception(kind, value, tb)), interactive=interactive)
        # Qt callback exceptions do not propagate through QApplication.exec().
        from PyQt6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            QApplication.instance().exit(1)

    sys.excepthook = exception_hook
    try:
        from rigol_control.app import main as app_main
        result = app_main(args)
        return 1 if uncaught else result
    except Exception:
        _report_startup_error(traceback.format_exc(), interactive=interactive)
        return 1
    finally:
        if fault_stream is not None:
            faulthandler.disable()
            fault_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
