"""Windowed PyInstaller entry point and hardware-free release acceptance check."""
from __future__ import annotations

import ctypes
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
        from PyQt6.QtWidgets import QMessageBox
        from rigol_control.runtime import (
            bundle_root, executable_dir, is_frozen, package_resource,
            prepare_data_dir, resolve_data_dir,
        )
        from rigol_control.app import create_application
        from rigol_control.repository import DeviceRepository
        from rigol_control.ui.main_window import DeviceCenterWindow

        root = bundle_root()
        default_data = resolve_data_dir()
        report.update(bundle_root=str(root), default_data_dir=str(default_data))
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


if __name__ == "__main__":
    raise SystemExit(main())
