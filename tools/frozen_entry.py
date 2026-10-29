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


def _startup_log_roots():
    roots = []
    try:
        from rigol_control.installation import is_installed
        from rigol_control.runtime import resolve_data_dir
        if is_installed():
            roots.append(resolve_data_dir() / "logs")
    except Exception:
        pass
    if not roots:
        roots.append(Path(sys.executable).resolve().parent / "logs")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "RigolGeneratorControl" / "logs")
    return roots


def _report_startup_error(detail: str, *, interactive: bool) -> None:
    """Keep failures visible even if importing Qt or our runtime module failed."""
    roots = _startup_log_roots()
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
    roots = _startup_log_roots()
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


def _check_default_data_directory(default_data: Path, report: dict) -> None:
    """Check edition defaults without creating or reading any user data files."""
    from rigol_control.installation import installed_info
    from rigol_control.runtime import executable_dir, is_frozen

    if not is_frozen():
        report["default_data_mode"] = "source"
        return
    installation = installed_info()
    if installation is not None:
        expected = Path(installation["data_dir"]).resolve()
        mode = "installed"
    else:
        expected = executable_dir() / "data"
        mode = "portable"
    if default_data != expected:
        raise AssertionError(f"{mode.capitalize()} default data directory is incorrect: {default_data}")
    report["default_data_mode"] = mode


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
        _check_default_data_directory(default_data, report)
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
            detail = "; ".join(dialog_errors) or "No dialog error was recorded"
            raise AssertionError(f"Expected exactly six simulated devices; got {len(records)}. {detail}")

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
        # Isolate the historical manual-query assertions below. The new
        # automatic path is enabled and accepted separately before shutdown.
        for window in home.open_windows.values():
            if hasattr(window, "auto_readback_checkbox"):
                window.auto_readback_checkbox.setChecked(False)
        titles = [home.windowTitle()] + [window.windowTitle() for window in home.open_windows.values()]
        if not all(title.endswith(f"v{__version__}") for title in titles):
            raise AssertionError(f"Window titles do not identify the running version: {titles}")
        report["window_versions_visible"] = True
        # Exercise update presentation without contacting GitHub. Constructing
        # the home window must never schedule networking in this diagnostic path.
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtNetwork import QSslSocket
        from rigol_control.updates import parse_release
        from rigol_control.update_settings import load_update_preference, load_update_preferences

        update = home.update_panel
        if (update._automatic_started or update.startup_timer.isActive()
                or update.periodic_timer.isActive() or update.checker.checking
                or update.downloader.downloading):
            raise AssertionError("Bundle self-test unexpectedly scheduled an update request")
        if not QSslSocket.supportsSsl():
            raise AssertionError("Qt update checker has no usable TLS backend")
        payload = {"tag_name": "v99.0.0", "draft": False, "prerelease": False}
        newer = parse_release(payload, __version__)
        if newer is None:
            raise AssertionError("A newer stable GitHub release was not recognized")
        if (parse_release(dict(payload, tag_name="v0.7.100"), "0.7.99") is None
                or parse_release(dict(payload, tag_name="v0.7.9"), "0.7.21") is not None):
            raise AssertionError("GitHub update versions were compared as text")
        connected_before = tuple(home.connection_manager.is_connected(item) for item in records)
        opened_urls = []
        original_open_url = QDesktopServices.openUrl
        original_check = update.checker.check
        checks = []
        try:
            QDesktopServices.openUrl = lambda url: opened_urls.append(url.toString()) or True
            update.checker.update_available.emit(newer)
            app.processEvents()
            if (update.release_button.isHidden() or "99.0.0" not in update.status.text()
                    or opened_urls):
                raise AssertionError("New GitHub release did not produce a non-modal notice")
            update.release_button.click()
            if opened_urls != [newer.url]:
                raise AssertionError("Explicit update action did not open the canonical release URL")
            update.checker.failed.emit("离线自检：网络暂不可用")
            if (not home.isEnabled() or not update.check_button.isEnabled()
                    or any(not window.isEnabled() for window in home.open_windows.values())
                    or tuple(home.connection_manager.is_connected(item) for item in records)
                    != connected_before or "离线自检" not in update.status.text()):
                raise AssertionError("Update failure interrupted virtual device operation")
            update.checker.check = lambda: checks.append(True) or True
            update.check_button.click()
            if checks != [True]:
                raise AssertionError("Update failure did not permit manual retry")
            update.automatic.setChecked(False)
            if load_update_preference(data_dir) != (False, ""):
                raise AssertionError("Automatic update preference did not persist in the data directory")
            update.automatic.setChecked(True)
            if load_update_preference(data_dir) != (True, ""):
                raise AssertionError("Automatic update preference could not be re-enabled")
            update.checker.up_to_date.emit(__version__)
            update.checker.failed.emit("离线自检：首次检查失败")
            if update.release_button.isHidden():
                raise AssertionError("First failed update check hid the official release page")
            update.release_button.click()
            if opened_urls[-1] != "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/latest":
                raise AssertionError("Failed initial check did not use the fixed latest release URL")
            update.checker.up_to_date.emit(__version__)
        finally:
            QDesktopServices.openUrl = original_open_url
            update.checker.check = original_check
        report["github_update_check"] = {
            "startup_network_disabled_in_self_test": True,
            "newer_release_notice": True, "numeric_version_ordering": True,
            "failure_keeps_device_controls": True, "manual_retry_available": True,
            "first_failure_offers_official_release_page": True,
            "preference_persisted": True, "tls_backend_available": True,
            "tls_backends": QSslSocket.availableBackends(),
            "tls_active_backend": QSslSocket.activeBackend(),
            "hardware_access": False, "network_access": False,
        }
        # Check download controls with a verified local fixture and stubbed
        # transport. No HTTP request, archive extraction or program is started.
        import _ssl
        import ssl
        download_ssl_context = ssl.create_default_context()
        if (download_ssl_context.verify_mode != ssl.CERT_REQUIRED
                or not download_ssl_context.check_hostname):
            raise AssertionError("Update download TLS context does not verify certificates and hostnames")
        download_ssl_module = Path(_ssl.__file__).resolve()
        if is_frozen() and not download_ssl_module.is_relative_to(root):
            raise AssertionError(f"Update download SSL module came from outside the bundle: {download_ssl_module}")
        download_bytes = b"Bundle self-test update fixture; never execute.\n"
        download_digest = hashlib.sha256(download_bytes).hexdigest()
        asset_names = ["Lab-Control-Center-99.0.0-windows-x64-portable.zip",
                       "Lab-Control-Center-99.0.0-windows-x64.exe",
                       "Lab-Control-Center-99.0.0-windows-x64-setup.exe"]
        download_payload = dict(payload, assets=[{
            "name": name, "size": len(download_bytes), "state": "uploaded",
            "digest": f"sha256:{download_digest}",
            "browser_download_url":
                f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v99.0.0/{name}",
        } for name in asset_names])
        downloadable = parse_release(download_payload, __version__)
        if downloadable is None or len(downloadable.assets) != 3:
            raise AssertionError("Valid update attachments were not recognized")
        # Use the real checker state machine with in-memory responses. Exercise
        # API rate limiting, a pinned official manifest redirect and the exact
        # Setup metadata consumed by download/install, without any HTTP traffic.
        from PyQt6.QtCore import QObject, QUrl, pyqtSignal
        from PyQt6.QtNetwork import QNetworkReply, QNetworkRequest
        from rigol_control.updates import LATEST_MANIFEST_URL, REPOSITORY_URL, UpdateChecker
        from rigol_control.updates_download import _validate_asset

        class OfflineUpdateReply(QObject):
            readyRead = pyqtSignal()
            finished = pyqtSignal()

            def __init__(self, url):
                super().__init__()
                self.request_url = url
                self.status, self.target, self.headers = 200, "", {}
                self.data = bytearray()

            def setReadBufferSize(self, limit):
                pass

            def read(self, limit):
                chunk = bytes(self.data[:limit])
                del self.data[:limit]
                return chunk

            def attribute(self, code):
                if code == QNetworkRequest.Attribute.HttpStatusCodeAttribute:
                    return self.status
                if code == QNetworkRequest.Attribute.RedirectionTargetAttribute:
                    return QUrl(self.target) if self.target else None
                return None

            def rawHeader(self, name):
                return self.headers.get(bytes(name).lower(), b"")

            def url(self):
                return QUrl(self.request_url)

            def error(self):
                return (QNetworkReply.NetworkError.ContentAccessDenied if self.status == 403
                        else QNetworkReply.NetworkError.NoError)

            def abort(self):
                self.finished.emit()

            def deleteLater(self):
                pass

            def deliver(self, data=b""):
                self.data.extend(data)
                self.readyRead.emit()
                self.finished.emit()

        class OfflineUpdateManager:
            def __init__(self):
                self.requests, self.replies = [], []

            def get(self, request):
                self.requests.append(request.url().toString())
                reply = OfflineUpdateReply(self.requests[-1])
                self.replies.append(reply)
                return reply

        offline_manager = OfflineUpdateManager()
        rate_checker = UpdateChecker(manager=offline_manager, current_version=__version__)
        fallback_releases, fallback_errors = [], []
        rate_checker.update_available.connect(fallback_releases.append)
        rate_checker.failed.connect(fallback_errors.append)
        try:
            if not rate_checker.check():
                raise AssertionError("Offline rate-limit probe did not start")
            limited = offline_manager.replies[-1]
            limited.status = 403
            limited.headers[b"x-ratelimit-remaining"] = b"0"
            limited.deliver(b'{"message":"API rate limit exceeded"}')
            if offline_manager.requests[-1] != LATEST_MANIFEST_URL:
                raise AssertionError("Limited API did not switch to the official latest manifest")
            redirect = offline_manager.replies[-1]
            redirect.status = 302
            redirect.target = f"{REPOSITORY_URL}/releases/download/v99.0.0/release-manifest.json"
            redirect.deliver()
            manifest_payload = {
                "version": "99.0.0", "platform": "windows-x64", "source_commit": "1" * 40,
                "source_dirty": False,
                "artifacts": [{"file": name, "bytes": len(download_bytes), "sha256": download_digest}
                              for name in asset_names],
            }
            offline_manager.replies[-1].deliver(json.dumps(manifest_payload).encode("utf-8"))
            if fallback_errors or len(fallback_releases) != 1:
                raise AssertionError(f"Official manifest fallback failed: {fallback_errors}")
            if fallback_releases[0] != downloadable:
                raise AssertionError("Fallback changed the verified package metadata")
            for fixture_asset in fallback_releases[0].assets:
                _validate_asset(fixture_asset)
            if not any(item.name.endswith("-setup.exe") for item in fallback_releases[0].assets):
                raise AssertionError("Fallback omitted the installed edition's Setup")
            rate_checker.check()
            app.processEvents()
            if len(offline_manager.requests) != 3 or len(fallback_releases) != 2:
                raise AssertionError("Repeated check did not reuse the recent verified result")
        finally:
            rate_checker.close()
        report["github_rate_limit_fallback"] = {
            "limited_api_uses_official_manifest": True, "release_tag_pinned": True,
            "setup_size_and_sha256_preserved": True, "download_contract_unchanged": True,
            "repeated_check_uses_cache": True, "first_failure_release_page": True,
            "network_access": False, "hardware_access": False,
        }
        download_calls = []
        opened_folders = []
        downloader = update.downloader
        original_start, original_cancel, original_close = downloader.start, downloader.cancel, downloader.close

        def start_fixture_download(asset, destination):
            download_calls.append((asset, Path(destination)))
            downloader._active = True
            downloader.downloading_changed.emit(True)
            return True

        def cancel_fixture_download():
            downloader._active = False
            downloader.downloading_changed.emit(False)

        original_open_url = QDesktopServices.openUrl
        original_update_signal_block = update.blockSignals(True)
        try:
            downloader.start = start_fixture_download
            downloader.cancel = cancel_fixture_download
            downloader.close = cancel_fixture_download
            QDesktopServices.openUrl = lambda url: opened_folders.append(url.toString()) or True
            if not update.automatic_download.isChecked():
                raise AssertionError("Automatic update downloading was not enabled by default")
            update.checker.update_available.emit(downloadable)
            expected_directory = data_dir / "updates" / "v99.0.0"
            expected_suffix = "-portable.zip" if update.package_format == "zip" else ".exe"
            if (len(download_calls) != 1 or download_calls[0][1] != expected_directory
                    or not download_calls[0][0].name.endswith(expected_suffix)
                    or update.download_controls.isHidden() or opened_folders):
                raise AssertionError("New release did not start a matching download in the update directory")
            downloader.progress.emit(len(download_bytes) // 2, len(download_bytes))
            if (update.download_progress.isHidden() or not 400 <= update.download_progress.value() <= 600
                    or "取消下载" != update.download_button.text()):
                raise AssertionError("Update download progress was not displayed")
            selected_asset = download_calls[0][0]
            expected_directory.mkdir(parents=True, exist_ok=True)
            fixture_path = expected_directory / selected_asset.name
            fixture_path.write_bytes(download_bytes)
            if hashlib.sha256(fixture_path.read_bytes()).hexdigest() != selected_asset.sha256:
                raise AssertionError("Local update presentation fixture failed its checksum")
            cancel_fixture_download()
            downloader.finished.emit(fixture_path)
            if (update.open_folder_button.isHidden() or update._downloaded_path != fixture_path
                    or "校验" not in update.download_status.text() or opened_folders):
                raise AssertionError("Verified download did not produce a completion notice")
            update.open_folder_button.click()
            from PyQt6.QtCore import QUrl
            if opened_folders != [QUrl.fromLocalFile(str(expected_directory)).toString()]:
                raise AssertionError("Downloaded update folder was not opened only by explicit action")
            downloader.failed.emit("离线下载自检：连接中断")
            if (not home.isEnabled() or not update.download_button.isEnabled()
                    or any(not window.isEnabled() for window in home.open_windows.values())
                    or tuple(home.connection_manager.is_connected(item) for item in records)
                    != connected_before or "离线下载自检" not in update.download_status.text()):
                raise AssertionError("Update download failure interrupted virtual devices or prevented retry")
            update.download_button.click()
            if len(download_calls) != 2:
                raise AssertionError("Update download did not allow manual retry")
            cancel_fixture_download()
            update.automatic_download.setChecked(False)
            preferences, preference_error = load_update_preferences(data_dir)
            if preference_error or preferences.automatic_download or not preferences.automatic_check:
                raise AssertionError("Download preference was not saved independently from update checking")
            update.checker.up_to_date.emit(__version__)
            update.automatic_download.setChecked(True)
            preferences, preference_error = load_update_preferences(data_dir)
            if preference_error or not preferences.automatic_download or len(download_calls) != 2:
                raise AssertionError("Download preference could not be restored without replaying a download")
        finally:
            cancel_fixture_download()
            downloader.start, downloader.cancel, downloader.close = original_start, original_cancel, original_close
            QDesktopServices.openUrl = original_open_url
            update.blockSignals(original_update_signal_block)
        report["github_update_download"] = {
            "automatic_download_started": True, "progress_displayed": True,
            "verified_ready_notice": True, "manual_retry_available": True,
            "preference_persisted": True, "explicit_folder_open_only": True,
            "device_controls_preserved": True,
            "verified_ssl_context": True, "download_tls_version": ssl.OPENSSL_VERSION,
            "download_ssl_module": str(download_ssl_module),
            "download_ca_certificates": len(download_ssl_context.get_ca_certs()),
            "hardware_access": False, "network_access": False,
        }
        # Installed updates use external data, a verified setup attachment and
        # an accepted clean close. All installation/transport actions below are
        # intercepted; no installer, helper process or real device is started.
        from unittest.mock import patch
        from rigol_control.install_data import run_install_data_cli
        from rigol_control.installation import APP_ID, MARKER_FORMAT, MARKER_NAME
        from rigol_control.runtime import resolve_data_dir
        from rigol_control.ui.update_panel import UpdatePanel

        install_fixture = data_dir / "installed-update-self-test"
        program_root = install_fixture / "program"
        external_data = install_fixture / "user-data"
        program_root.mkdir(parents=True)
        external_data.mkdir()
        program_exe = program_root / "Lab Control Center.exe"
        program_exe.write_bytes(b"Non-executable installed path fixture.\n")
        marker = program_root / MARKER_NAME
        marker.write_text(json.dumps({
            "format": MARKER_FORMAT, "schema_version": 1, "app_id": APP_ID,
            "data_dir": str(external_data.resolve()),
        }), encoding="utf-8")
        with patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(program_exe)), \
                patch.dict(os.environ, {"RIGOL_GUI_DATA_DIR": ""}):
            if resolve_data_dir() != external_data.resolve():
                raise AssertionError("Installed edition did not use the external user-data directory")
        preserved_bytes = b'{"stable-device-id":"self-test-only","resource":"SIM::DEVICE"}\n'
        measurement_bytes = bytes(range(256)) * 8
        (external_data / "devices.json").write_bytes(preserved_bytes)
        (external_data / "measurement.bin").write_bytes(measurement_bytes)
        (external_data / "empty-directory").mkdir()
        backup_report = install_fixture / "backup-report.json"
        if run_install_data_cli([
            "--installer-data-backup", str(external_data),
            "--backup-root", str(install_fixture / "backups"), "--report", str(backup_report),
        ]) != 0:
            raise AssertionError("Installer headless data backup failed")
        backup_receipt = json.loads(backup_report.read_text(encoding="utf-8"))
        if not backup_receipt.get("success") or backup_receipt.get("file_count") != 2:
            raise AssertionError("Installer data backup did not verify all user files")
        # Remove only the synthetic program and selected data to exercise the
        # restore path. Retained external files must remain byte-identical.
        marker.unlink()
        program_exe.unlink()
        program_root.rmdir()
        if (external_data / "devices.json").read_bytes() != preserved_bytes:
            raise AssertionError("Removing old program files changed external user data")
        (external_data / "measurement.bin").unlink()
        (external_data / "empty-directory").rmdir()
        restore_report = install_fixture / "restore-report.json"
        if run_install_data_cli([
            "--installer-data-restore", str(external_data),
            "--backup", backup_receipt["backup_path"], "--report", str(restore_report),
        ]) != 0:
            raise AssertionError("Installer headless data restore failed")
        restore_receipt = json.loads(restore_report.read_text(encoding="utf-8"))
        if (not restore_receipt.get("success") or restore_receipt.get("restored_file_count") != 1
                or (external_data / "devices.json").read_bytes() != preserved_bytes
                or (external_data / "measurement.bin").read_bytes() != measurement_bytes
                or not (external_data / "empty-directory").is_dir()):
            raise AssertionError("Installer data restore did not preserve exact files and empty directories")

        setup_name = "Lab-Control-Center-99.0.0-windows-x64-setup.exe"
        setup_release = parse_release(dict(payload, assets=[{
            "name": setup_name, "size": len(download_bytes), "state": "uploaded",
            "digest": f"sha256:{download_digest}",
            "browser_download_url":
                f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v99.0.0/{setup_name}",
        }]), __version__)
        if setup_release is None or len(setup_release.assets) != 1:
            raise AssertionError("Verified installer release attachment was not recognized")
        installed_panel = None
        with patch("rigol_control.ui.update_panel.is_installed", return_value=True):
            installed_panel = UpdatePanel(install_fixture / "panel-data")
        setup_requests, setup_downloads = [], []
        setup_downloader = installed_panel.downloader
        original_setup_start = setup_downloader.start

        def setup_fixture_download(asset, destination):
            setup_downloads.append((asset, Path(destination)))
            setup_downloader._active = True
            setup_downloader.downloading_changed.emit(True)
            return True

        try:
            installed_panel.install_requested.connect(setup_requests.append)
            setup_downloader.start = setup_fixture_download
            installed_panel.checker.update_available.emit(setup_release)
            if (installed_panel.package_format != "setup" or len(setup_downloads) != 1
                    or setup_downloads[0][0].name != setup_name or setup_requests):
                raise AssertionError("Installed edition did not select only a verified setup attachment")
            selected_setup, setup_directory = setup_downloads[0]
            setup_directory.mkdir(parents=True)
            setup_fixture_path = setup_directory / setup_name
            setup_fixture_path.write_bytes(download_bytes)
            setup_downloader._active = False
            setup_downloader.downloading_changed.emit(False)
            setup_downloader.finished.emit(setup_fixture_path)
            if (setup_requests != [(setup_fixture_path, selected_setup)]
                    or not installed_panel.install_request_automatic
                    or installed_panel.install_button.isEnabled()):
                raise AssertionError("Verified setup completion did not request one automatic safe handoff")
            installed_panel.request_install()
            if len(setup_requests) != 1:
                raise AssertionError("Duplicate installer-ready action replayed an installation request")
            installed_panel.checker.update_available.emit(setup_release)
            if (len(setup_downloads) != 1 or len(setup_requests) != 1
                    or not installed_panel._install_request_pending
                    or installed_panel._downloaded_path != setup_fixture_path):
                raise AssertionError("Repeated version notice reset a verified pending installer")
            installed_panel.set_install_status("离线自检：安装准备失败", retry=True)
            installed_panel.checker.update_available.emit(setup_release)
            if len(setup_downloads) != 1 or len(setup_requests) != 1:
                raise AssertionError("Checking again automatically replayed a failed installer")
            installed_panel.install_button.click()
            if len(setup_requests) != 2:
                raise AssertionError("Explicit Install did not allow retry of the verified package")
        finally:
            setup_downloader._active = False
            setup_downloader.start = original_setup_start
            installed_panel.stop()
            installed_panel.close()
            installed_panel.deleteLater()

        controller = home.installation_controller
        original_automatic_flag = update._install_request_automatic
        original_automatic_install = update.automatic_install.isChecked()
        try:
            update._install_request_automatic = True
            update.automatic_install.setChecked(True)
            with patch.object(home.connection_manager, "active_leases", return_value=("self-test-busy",)), \
                    patch.object(controller.pool, "start") as dispatch:
                controller.request(setup_requests[0])
                if controller.pending != setup_requests[0] or controller.preparing or not controller.timer.isActive():
                    raise AssertionError("Busy devices did not defer automatic installation")
                dispatch.assert_not_called()
            controller.timer.stop()
            controller.pending = None
            sentinel_handoff = object()
            with patch("rigol_control.ui.installation_controller.installation_busy_reason", return_value=""), \
                    patch.object(home, "close", return_value=False), \
                    patch("rigol_control.ui.installation_controller.cancel_handoff") as cancel_install, \
                    patch("rigol_control.ui.installation_controller.commit_handoff") as commit_install, \
                    patch.object(app, "quit") as quit_app:
                controller._prepared(sentinel_handoff)
                cancel_install.assert_called_once_with(sentinel_handoff)
                commit_install.assert_not_called()
                quit_app.assert_not_called()
        finally:
            controller.timer.stop()
            controller.pending = None
            controller.automatic = False
            update._install_request_automatic = original_automatic_flag
            update.automatic_install.setChecked(original_automatic_install)
        if tuple(home.connection_manager.is_connected(item) for item in records) != connected_before:
            raise AssertionError("Installer lifecycle checks changed virtual device connections")
        report["installed_update_lifecycle"] = {
            "external_data_preserved": True, "backup_restore_verified": True,
            "setup_asset_selected": True, "clean_close_required": True,
            "busy_install_deferred": True, "no_installer_execution": True,
            "repeated_check_keeps_pending_installer": True,
            "failed_install_requires_explicit_retry": True,
            "no_hardware_access": True, "hardware_access": False, "network_access": False,
        }
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
        # Check the shipped readback button and failure presentation, using
        # distinct virtual channels and the instrument's existing Vrms unit.
        readback_before = generator.snapshot().channels
        editors_before = dg._device_parameter_snapshot()
        generation_before = dg.driver.session.connection_generation
        generator.commands.clear()
        dg._show_output_readback()
        wait_idle(require_connected=True)
        if (dg.pages.currentIndex() != 1
                or not dg.output_readback_values[2]["amplitude"].text().endswith(" Vrms")
                or "回读时间" not in dg.output_readback_status.text()):
            raise AssertionError("DG readback did not display native units and a completion time")
        if (generator.snapshot().channels != readback_before
                or dg._device_parameter_snapshot() != editors_before
                or len(generator.commands) != 10
                or not all(command.endswith("?") for command in generator.commands)):
            raise AssertionError("DG readback changed settings or did not remain query-only")
        original_dg_query = dg.driver.session.query

        def malformed_dg_amplitude(command, **kwargs):
            if command == ":SOUR2:VOLT?":
                return "BUNDLE_BAD_DG_AMPLITUDE"
            return original_dg_query(command, **kwargs)

        dg.driver.session.query = malformed_dg_amplitude
        try:
            dg.output_readback_button.click()
            wait_idle(require_connected=True)
            if (any(value.text() != "未知" for values in dg.output_readback_values.values()
                    for value in values.values())
                    or "BUNDLE_BAD_DG_AMPLITUDE" not in dg.output_readback_status.text()):
                raise AssertionError("DG failed readback retained stale values or hid the bad reply")
        finally:
            dg.driver.session.query = original_dg_query
        dg.output_readback_button.click()
        wait_idle(require_connected=True)
        if ("回读时间" not in dg.output_readback_status.text()
                or dg.driver.session.connection_generation != generation_before
                or generator.snapshot().channels != readback_before):
            raise AssertionError("DG readback retry reconnected or changed output parameters")
        report["dg_output_readback"] = {
            "both_channels_displayed": True, "native_amplitude_unit_displayed": True,
            "query_only": True, "editors_unchanged": True, "failed_snapshot_invalidated": True,
            "bad_reply_visible": True, "retry_without_reconnect": True, "hardware_access": False,
        }
        # Reproduce an empty UNIT? from the reported DG4162/USB scenario
        # without accessing USB: use the same real driver/UI on a virtual DG.
        # HIGH?/LOW? have documented voltage units, so their difference can be
        # displayed as a labeled configuration span without guessing native units.
        level_calls = []
        units_before_levels = generator.amplitude_units.copy()

        def blank_dg_units(command, **kwargs):
            level_calls.append(command)
            return "\r\n" if command.endswith(":VOLT:UNIT?") else original_dg_query(command, **kwargs)

        dg.driver.session.query = blank_dg_units
        try:
            dg.output_readback_button.click()
            wait_idle(require_connected=True)
            if "兼容回读" not in dg.output_readback_status.text() or len(level_calls) != 14:
                raise AssertionError("Empty DG units did not use the bounded level-query snapshot")
            for channel in (1, 2):
                amplitude_label = dg.output_readback_values[channel]["amplitude"]
                if (not amplitude_label.text().endswith(" Vpp（电平差）")
                        or "单位未知" not in amplitude_label.toolTip()):
                    raise AssertionError("Derived DG amplitude hid its source or invented a native unit")
                expected_span = float(generator.channels[channel]["amplitude_vpp"])
                displayed_span = float(amplitude_label.text().split(" ", 1)[0])
                if abs(displayed_span - expected_span) > max(abs(expected_span) * 1e-7, 1e-12):
                    raise AssertionError("DG level difference did not match the configured voltage span")
            if (not all(command.endswith("?") for command in level_calls)
                    or any(command in level_calls for command in (":SOUR1:VOLT?", ":SOUR2:VOLT?"))
                    or generator.snapshot().channels != readback_before
                    or generator.amplitude_units != units_before_levels
                    or dg._device_parameter_snapshot() != editors_before
                    or dg.driver.session.connection_generation != generation_before):
                raise AssertionError("DG level fallback changed settings, guessed units or reconnected")

            def invalid_dg_level(command, **kwargs):
                if command == ":SOUR2:VOLT:HIGH?":
                    return "BUNDLE_BAD_DG_HIGH_LEVEL"
                return blank_dg_units(command, **kwargs)

            dg.driver.session.query = invalid_dg_level
            dg.output_readback_button.click()
            wait_idle(require_connected=True)
            if (any(value.text() != "未知" for values in dg.output_readback_values.values()
                    for value in values.values())
                    or "BUNDLE_BAD_DG_HIGH_LEVEL" not in dg.output_readback_status.text()):
                raise AssertionError("Bad DG level data retained a partially successful channel group")
        finally:
            dg.driver.session.query = original_dg_query
        dg.output_readback_button.click()
        wait_idle(require_connected=True)
        if (not dg.output_readback_values[2]["amplitude"].text().endswith(" Vrms")
                or "兼容回读" in dg.output_readback_status.text()
                or generator.snapshot().channels != readback_before):
            raise AssertionError("Native DG retry retained a stale compatibility label or changed settings")
        report["dg_readback_voltage_levels"] = {
            "empty_unit_uses_documented_voltage_queries": True,
            "derived_vpp_source_labeled": True, "native_unit_explicitly_unknown": True,
            "query_only_no_reconnect": True, "configuration_and_editors_unchanged": True,
            "bad_levels_invalidate_group": True, "native_retry_clears_compatibility_notice": True,
            "hardware_access": False,
        }
        # mVpp is a local display prefix; UNIT? still returns VPP/VRMS/DBM.
        # Exercise the actual widgets without writing units or assuming a load.
        display_units_before = generator.amplitude_units.copy()
        native_vpp = dg._output_readback_snapshot[0].amplitude
        native_vrms_text = dg.output_readback_values[2]["amplitude"].text()
        generator.commands.clear()
        dg.output_readback_units[1].setCurrentText("mVpp")
        if (float(dg.output_readback_values[1]["amplitude"].text().split()[0]) != native_vpp / .001
                or not dg.output_readback_values[1]["amplitude"].text().endswith(" mVpp")
                or dg.output_readback_values[2]["amplitude"].text() != native_vrms_text
                or dg.output_readback_units[2].isEnabled()
                or generator.commands):
            raise AssertionError("DG display switch performed I/O, lost mVpp scaling or converted native Vrms")
        for explicit in (False, True):
            persisted = dg._persistent_controls(use_explicit_keys=explicit).values()
            if any(combo in persisted for combo in dg.output_readback_units.values()):
                raise AssertionError("DG display choice entered device parameter persistence")
        generator.amplitude_units[2] = "DBM"

        def negative_dbm(command, **kwargs):
            return "-37.25" if command == ":SOUR2:VOLT?" else original_dg_query(command, **kwargs)

        dg.driver.session.query = negative_dbm
        try:
            dg.output_readback_button.click()
            wait_idle(require_connected=True)
            generator.commands.clear()
            dg.output_readback_units[2].setCurrentText("mVpp")
            if (dg.output_readback_values[2]["amplitude"].text() != "-37.25 dBm"
                    or dg.output_readback_units[2].isEnabled() or generator.commands):
                raise AssertionError("DG display choice replaced the native negative dBm value")
        finally:
            generator.amplitude_units.update(display_units_before)
            dg.driver.session.query = original_dg_query
        dg.driver.session.query = blank_dg_units
        try:
            dg.output_readback_button.click()
            wait_idle(require_connected=True)
            for channel in (1, 2):
                label = dg.output_readback_values[channel]["amplitude"]
                if not label.text().endswith(" mVpp（电平差）") or "单位未知" not in label.toolTip():
                    raise AssertionError("DG mVpp level-span display hid the unknown native unit")
        finally:
            dg.driver.session.query = original_dg_query
        dg.output_readback_units[1].setCurrentText("Vpp")
        dg.output_readback_units[2].setCurrentText("Vpp")
        dg.output_readback_button.click()
        wait_idle(require_connected=True)
        if (generator.snapshot().channels != readback_before
                or generator.amplitude_units != display_units_before
                or dg._device_parameter_snapshot() != editors_before):
            raise AssertionError("DG display selection changed source settings or editors")
        # A complete cached read must expire even if the connection timer misses
        # the disconnected interval. Conversely, it must retain a new request
        # that already targets the replacement connection.
        home.connection_manager.disconnect(dg.record)
        home.connection_manager.connect(dg.record)
        generator.commands.clear()
        # Do this before the status timer gets a chance to observe the reconnect.
        dg.output_readback_units[1].setCurrentText("mVpp")
        dg.sync_connection_state()
        if (any(value.text() != "未知" for values in dg.output_readback_values.values()
                for value in values.values())
                or "回读时间" in dg.output_readback_status.text()
                or generator.commands):
            raise AssertionError("DG completed snapshot survived a quick reconnect, or status sync performed I/O")
        home.connection_manager.disconnect(dg.record)
        home.connection_manager.connect(dg.record)
        generator.commands.clear()
        read_entered, release_read = threading.Event(), threading.Event()
        original_dg_read = dg.driver.read_output_parameters

        def delayed_new_connection_read():
            read_entered.set()
            if not release_read.wait(3):
                raise TimeoutError("DG readback generation probe was not released")
            return original_dg_read()

        dg.driver.read_output_parameters = delayed_new_connection_read
        try:
            dg.output_readback_button.click()
            if not read_entered.wait(2):
                raise AssertionError("DG fresh read did not start after reconnect")
            sync_started = time.monotonic()
            dg.sync_connection_state()
            if time.monotonic() - sync_started > .5 or generator.commands:
                raise AssertionError("DG connection sync waited for the read transaction or performed I/O")
            release_read.set()
            wait_idle(require_connected=True)
        finally:
            release_read.set()
            dg.driver.read_output_parameters = original_dg_read
        if ("回读时间" not in dg.output_readback_status.text()
                or dg.output_readback_values[2]["amplitude"].text() == "未知"):
            raise AssertionError("DG connection sync discarded a fresh result from the current connection")
        report["dg_readback_connection_generation"] = {
            "completed_snapshot_invalidated": True, "no_io_during_sync": True,
            "fresh_read_survives_sync": True, "hardware_access": False,
        }
        report["dg_readback_display_units"] = {
            "vpp_to_mvpp_without_io": True, "native_dbm_preserved": True,
            "native_vrms_preserved": True, "level_span_mvpp_labeled": True,
            "display_controls_not_persisted": True, "stale_cache_invalidated": True,
            "hardware_access": False,
        }
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
        # Read the source through the shipped UI while forbidding every basic
        # output setter. Inject an invalid native value through the virtual
        # backend so the packaged validator and error callback are exercised.
        vaunix_before = (device.frequency_hz, device.power_dbm, device.rf_on,
                         device.internal_reference, device.sweep_active)
        vaunix_editors = vaunix._device_parameter_snapshot()
        vaunix_generation = device.connection_generation
        setters = {name: getattr(device, name) for name in (
            "set_frequency_hz", "set_power_dbm", "set_rf_on", "set_internal_reference",
        )}

        def forbidden_readback_write(*_args, **_kwargs):
            raise AssertionError("A readback attempted to write Vaunix output parameters")

        try:
            for name in setters:
                setattr(device, name, forbidden_readback_write)
            vaunix.overview_readback_button.click()
            wait_idle(require_connected=True)
            if (vaunix.pages.currentIndex() != 1
                    or vaunix.output_readback_values["frequency"].text() != "5000000000 Hz"
                    or vaunix.output_readback_values["amplitude"].text() != "-10 dBm"
                    or vaunix.output_readback_values["output"].text() != "关闭（未输出）"
                    or "回读时间" not in vaunix.output_readback_status.text()):
                raise AssertionError(
                    "Vaunix readback did not display frequency, absolute power and RF state: "
                    f"page={vaunix.pages.currentIndex()}, "
                    f"values={ {key: value.text() for key, value in vaunix.output_readback_values.items()} }, "
                    f"status={vaunix.output_readback_status.text()!r}"
                )
            device.power_dbm = float("nan")
            try:
                vaunix.output_readback_button.click()
                wait_idle(require_connected=True)
                if (any(value.text() != "未知" for value in vaunix.output_readback_values.values())
                        or "fnLMS_GetAbsPowerLevel" not in vaunix.output_readback_status.text()
                        or "nan" not in vaunix.output_readback_status.text()):
                    raise AssertionError("Vaunix failed readback retained stale values or hid the bad native value")
            finally:
                device.power_dbm = vaunix_before[1]
            vaunix.output_readback_button.click()
            wait_idle(require_connected=True)
        finally:
            for name, setter in setters.items():
                setattr(device, name, setter)
        if ("回读时间" not in vaunix.output_readback_status.text()
                or device.connection_generation != vaunix_generation
                or vaunix._device_parameter_snapshot() != vaunix_editors
                or (device.frequency_hz, device.power_dbm, device.rf_on,
                    device.internal_reference, device.sweep_active) != vaunix_before):
            raise AssertionError("Vaunix readback changed settings, editors or the connection")
        report["vaunix_output_readback"] = {
            "frequency_amplitude_output_displayed": True, "query_only": True,
            "editors_unchanged": True, "failed_snapshot_invalidated": True,
            "bad_reply_visible": True, "retry_without_reconnect": True, "hardware_access": False,
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
        # The dedicated RF readback must remain independent of all optional
        # modulation/current-point queries and must never overwrite drafts.
        ssg_backend = ssg.session._backend
        ssg_before = dict(ssg_backend.values)
        ssg_editors = ssg._device_parameter_snapshot()
        ssg_generation = ssg.session.connection_generation
        ssg_backend.commands.clear()
        ssg.output_readback_button.click()
        wait_idle(require_connected=True)
        if (ssg.output_readback_values["frequency"].text() != "1000000000 Hz"
                or ssg.output_readback_values["amplitude"].text() != "-20 dBm"
                or ssg.output_readback_values["output"].text() != "关闭（未输出）"
                or "回读时间" not in ssg.output_readback_status.text()
                or ssg_backend.commands != [":FREQuency?", ":POWer?", ":OUTPut?"]):
            raise AssertionError(
                "SSG dedicated readback did not use only the three basic RF queries: "
                f"values={ {key: value.text() for key, value in ssg.output_readback_values.items()} }, "
                f"status={ssg.output_readback_status.text()!r}, commands={ssg_backend.commands!r}"
            )

        def malformed_ssg_readback(command, **kwargs):
            if command == ":POWer?":
                return "BUNDLE_BAD_SSG_POWER"
            return original_query(command, **kwargs)

        ssg.session.query = malformed_ssg_readback
        try:
            ssg.output_readback_button.click()
            wait_idle(require_connected=True)
            if (any(value.text() != "未知" for value in ssg.output_readback_values.values())
                    or "BUNDLE_BAD_SSG_POWER" not in ssg.output_readback_status.text()):
                raise AssertionError("SSG dedicated readback retained stale values or hid the bad reply")
        finally:
            ssg.session.query = original_query
        ssg.output_readback_button.click()
        wait_idle(require_connected=True)
        if ("回读时间" not in ssg.output_readback_status.text()
                or ssg.session.connection_generation != ssg_generation
                or ssg._device_parameter_snapshot() != ssg_editors
                or ssg_backend.values != ssg_before
                or any(not command.endswith("?") for command in ssg_backend.commands)):
            raise AssertionError("SSG dedicated readback changed settings, editors or the connection")
        report["siglent_output_readback"] = {
            "frequency_amplitude_output_displayed": True, "query_only": True,
            "editors_unchanged": True, "failed_snapshot_invalidated": True,
            "bad_reply_visible": True, "retry_without_reconnect": True, "hardware_access": False,
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
        # Exercise the packaged Newport recovery UI using only its in-memory
        # stage. No serial resource, real controller or motion is accessed.
        from rigol_control.drivers.newport_smc100 import MotionError
        newport = next(w for w in home.open_windows.values() if type(w).__name__ == "NewportWindow")
        stage = newport.device

        def wait_newport_worker():
            # A deliberate failed-stop probe remains busy until explicit
            # cleanup; waiting for global idle there would conceal the lease.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                app.processEvents()
                if dialog_errors:
                    raise RuntimeError("\n".join(dialog_errors))
                if not newport._manual_busy and not newport._call_tasks and newport._manual_done.is_set():
                    return
                time.sleep(.005)
            raise TimeoutError("Newport virtual manual worker did not finish")

        newport.refresh_button.click()
        wait_newport_worker()
        moves_before = tuple(stage.move_history)
        stops_before, homes_before = stage.stop_count, stage.home_count
        newport._manual(stage.home, wait_motion=True)
        wait_newport_worker()
        if (newport._last_position is None or not newport.move_button.isEnabled()
                or "READY" not in newport.status_label.text()
                or stage.stop_count != stops_before or stage.home_count != homes_before):
            raise AssertionError("A rejected READY home lost the position, blocked controls, or issued STOP/HOME")
        stage.set_virtual_state(0x3C)
        newport.refresh_button.click()
        wait_newport_worker()
        if not newport.enable_button.isEnabled() or newport.move_button.isEnabled():
            raise AssertionError("Newport DISABLE state lacks a guarded manual-enable action")
        enables_before = stage.enable_count
        newport.enable_button.click()
        wait_newport_worker()
        if (not stage.status().ready or stage.enable_count != enables_before + 1
                or tuple(stage.move_history) != moves_before or stage.home_count != homes_before):
            raise AssertionError("Newport explicit enable did not reach READY without a position or home command")
        stage.set_virtual_state(0x0A)
        newport.refresh_button.click()
        wait_newport_worker()
        stops_before_unref = stage.stop_count
        newport.sidebar_stop.click()
        wait_newport_worker()
        if (newport._manual_cleanup_pending or not newport.home_button.isEnabled()
                or home.connection_manager.lease_owner(newport.record.id) is not None
                or stage.stop_count != stops_before_unref):
            raise AssertionError("Stopping an already unreferenced stage issued illegal ST or locked out homing")
        stage.set_virtual_state(0x33)
        newport.refresh_button.click()
        wait_newport_worker()
        original_start = newport.thread_pool.start

        def reject_newport_task(*_args, **_kwargs):
            raise RuntimeError("BUNDLE_NEWPORT_DISPATCH_REJECTED")

        newport.thread_pool.start = reject_newport_task
        try:
            newport.refresh_button.click()
        finally:
            newport.thread_pool.start = original_start
        if (newport._manual_busy or not newport._manual_done.is_set() or newport._call_tasks
                or home.connection_manager.lease_owner(newport.record.id) is not None):
            raise AssertionError("Rejected Newport worker left pending work or an operation lease")
        newport.refresh_button.click()
        wait_newport_worker()
        original_move, original_stop = stage.move_absolute, stage.stop
        failed_moves, failed_stops = [], []

        def ambiguous_newport_move(angle):
            failed_moves.append(angle)
            raise MotionError("BUNDLE_NEWPORT_MOVE_UNCERTAIN")

        def unconfirmed_newport_stop():
            failed_stops.append(True)
            raise MotionError("BUNDLE_NEWPORT_STOP_UNCONFIRMED")

        stage.move_absolute, stage.stop = ambiguous_newport_move, unconfirmed_newport_stop
        try:
            newport.manual_angle.setValue(.1)
            newport.move_button.click()
            wait_newport_worker()
            if (not newport._manual_cleanup_pending
                    or home.connection_manager.lease_owner(newport.record.id) != newport._manual_owner
                    or newport.move_button.isEnabled() or newport.disconnect_button.isEnabled()
                    or len(failed_moves) != 1 or len(failed_stops) != 1):
                raise AssertionError("Newport unconfirmed STOP released ownership or replayed a command")
        finally:
            stage.move_absolute, stage.stop = original_move, original_stop
        newport.sidebar_stop.click()
        wait_newport_worker()
        if (newport._manual_cleanup_pending or newport._busy()
                or home.connection_manager.lease_owner(newport.record.id) is not None
                or tuple(stage.move_history) != moves_before or stage.home_count != homes_before
                or stage.enable_count != enables_before + 1):
            raise AssertionError("Newport explicit cleanup retry did not finish without motion replay")
        report["newport_manual_recovery"] = {
            "state_rejection_preserves_controls": True, "disabled_enable_without_home": True,
            "dispatch_failure_clears_pending": True, "failed_stop_retains_ownership": True,
            "explicit_stop_retry_completes": True, "no_automatic_motion_replay": True,
            "unreferenced_stop_preserves_homing": True,
            "hardware_access": False,
        }
        def wait_readback(window, expected_frequency):
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                app.processEvents()
                values = window.output_readback_values
                label = values[2]["frequency"] if isinstance(next(iter(values)), int) else values["frequency"]
                if label.text() == expected_frequency and not window._call_tasks:
                    return
                time.sleep(.01)
            raise AssertionError(f"Automatic readback did not show {expected_frequency}")

        dg.auto_readback_checkbox.setChecked(True)
        dg.channel_controls[2]["frequency"].setValue(34567)
        dg._apply_channel(2)
        wait_readback(dg, "34567 Hz")
        before_auto_ch1 = generator.snapshot().channels[0]
        class AutoReadCounter:
            count = 0
            def start(self): pass
            def stop(self): pass
            def read_count(self): return self.count
        auto_counter = AutoReadCounter()
        auto_controller = TriggerStepController(auto_counter, dg.driver, StepParameter.FREQUENCY, 2, [1100, 2200])
        home.connection_manager.acquire_devices("bundle-auto-readback", [dg.record.id])
        home.connection_manager.watch_output_task("bundle-auto-readback", [dg.record], auto_controller)
        dg.controller = auto_controller
        dg.auto_readback.reset()
        try:
            auto_controller.start()
            generator.commands.clear()
            quiescent_until = time.monotonic() + .25
            while time.monotonic() < quiescent_until:
                app.processEvents()
                time.sleep(.01)
            if generator.commands or auto_controller.snapshot().applied_count:
                raise AssertionError("Arming without an edge performed automatic I/O")
            auto_counter.count = 5
            wait_readback(dg, "1100 Hz")
            if (auto_controller.snapshot().applied_count != 5
                    or generator.snapshot().channels[0] != before_auto_ch1
                    or list(generator.commands).count(":SOUR1:VOLT:UNIT?") != 2):
                raise AssertionError("Automatic trigger readback changed CH1, lost edges or read every point")
        finally:
            auto_controller.stop()
            dg.controller = None
            home.connection_manager.release_devices("bundle-auto-readback")
        dg.auto_readback_checkbox.setChecked(False)
        dg._apply_channel(2)
        wait_idle(require_connected=True)
        if any(value.text() != "未知" for values in dg.output_readback_values.values() for value in values.values()):
            raise AssertionError("Disabled automatic readback kept a superseded snapshot")
        vaunix.auto_readback_checkbox.setChecked(True)
        vaunix.output_frequency.setValue(5123456000)
        vaunix._apply_output()
        wait_readback(vaunix, "5123456000 Hz")
        ssg.auto_readback_checkbox.setChecked(True)
        ssg.frequency.setValue(1200000000)
        original_ssg_basic = ssg.driver.read_output_parameters
        def forbidden_extra_basic(*args, **kwargs):
            raise AssertionError("SSG automatic display added redundant basic queries")
        ssg.driver.read_output_parameters = forbidden_extra_basic
        try:
            ssg.apply_frequency()
            wait_readback(ssg, "1200000000 Hz")
        finally:
            ssg.driver.read_output_parameters = original_ssg_basic
        for window in (dg, vaunix, ssg):
            for controls in (window._persistent_controls(), window._persistent_controls(use_explicit_keys=False)):
                if window.auto_readback_checkbox in controls.values():
                    raise AssertionError("Automatic readback preference changed device parameter persistence")
            window.auto_readback_checkbox.setChecked(False)
        report["automatic_output_readback"] = {
            "dg_parameter_apply_refreshes": True, "trigger_burst_coalesced": True,
            "no_query_before_first_trigger": True, "first_p0_and_ch1_preserved": True,
            "disable_invalidates_old_snapshot": True, "vaunix_parameter_apply_refreshes": True,
            "siglent_reuses_existing_snapshot": True, "controls_not_persisted": True,
            "hardware_access": False,
        }
        if home.connection_manager.active_leases():
            raise AssertionError("Virtual windows retained an unexpected device operation lease")
        if not home.close():
            raise AssertionError("Main window refused to close after background work completed")
        home = None
        app.processEvents()
        # A saved virtual laboratory restarts with disconnected records. This
        # path differs from create_virtual_lab(), which connects them directly.
        saved_data = {name: (data_dir / name).read_bytes()
                      for name in ("devices.json", "device_parameters.json")
                      if (data_dir / name).exists()}
        with patch("pyvisa.ResourceManager") as visa_probe, \
                patch("nidaqmx.system.System.local") as ni_probe, \
                patch("serial.Serial") as serial_probe, \
                patch("rigol_control.services.VaunixLmsApi") as vaunix_probe:
            home = DeviceCenterWindow(repository)
            home.show()
            wait_idle()
            reloaded = repository.list()
            if len(reloaded) != 6 or any(home.connection_manager.is_connected(item) for item in reloaded):
                raise AssertionError("Saved virtual restart connected a device or lost a record")
            if any(card.status.text() != "● 可用" for card in home.cards.values()):
                raise AssertionError("Saved virtual restart did not mark every simulator available")
            for native_probe in (visa_probe, ni_probe, serial_probe, vaunix_probe):
                native_probe.assert_not_called()
            if not home.close():
                raise AssertionError("Saved virtual restart retained a probe or refused normal close")
            home = None
            app.processEvents()
        if any((data_dir / name).read_bytes() != contents for name, contents in saved_data.items()):
            raise AssertionError("Saved virtual restart changed persisted devices or parameters")
        report["persisted_virtual_restart"] = {
            "devices_reloaded": True, "all_available": True, "no_automatic_connections": True,
            "data_unchanged": True, "native_backends_untouched": True, "clean_close": True,
            "hardware_access": False,
        }
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
    if any(flag in args for flag in ("--installer-data-backup", "--installer-data-restore")):
        from rigol_control.install_data import run_install_data_cli
        return run_install_data_cli(args)
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
