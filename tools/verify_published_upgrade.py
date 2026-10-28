"""Verify real published Setup -> automatic update on a disposable GitHub runner.

This deliberately uses the production identity: the published v0.7.23 runtime
refuses automatic installation for a test identity. Execution is therefore
restricted to a fresh GitHub-hosted Windows runner. Never run a Setup locally.
No source imports, patched metadata, smoke mode, or substitute installers are
used. All diagnostics and seeded data remain available until the runner expires.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import traceback

if sys.platform == "win32":
    import winreg


REPOSITORY = "Rechoncy/Rigol_Generator_GUI"
APP_ID = "{D9D0E8E6-83E5-46F4-B5B1-9B3D7EE9C523}"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}_is1"
PERSISTENT_KEY = r"Software\Rechoncy\LabControlCenter"
PREVIOUS_TAG = "v0.7.23"
PREVIOUS_SHA256 = "69b21ca43ee62cb595de242ce1cabcac14cd5b7cf44a8c7d9f976303558d044b"
PREVIOUS_SIZE = 43252276
PREVIOUS_SOURCE = "1d328f1eeef2bee0896a9a3b44acbf4754b699a6"


def require(condition, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def same_path(left, right) -> bool:
    return Path(left).resolve() == Path(right).resolve()


def read_registration(key_path: str) -> dict:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            values = {}
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, index)
                values[name] = value
            return values
    except FileNotFoundError:
        return {}


def require_disposable_runner() -> Path:
    require(sys.platform == "win32" and os.name == "nt" and struct.calcsize("P") == 8,
            "Real published installer verification requires Windows x64")
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "Windows"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "Refusing production Setup execution outside a disposable GitHub-hosted Windows runner")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "")).resolve(strict=True)
    require(runner_temp.is_dir() and runner_temp.parent != runner_temp,
            "RUNNER_TEMP must be a dedicated existing directory")
    # Abort on any prior production identity, including an incomplete key.
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
            for name in (UNINSTALL_KEY, PERSISTENT_KEY):
                try:
                    with winreg.OpenKey(hive, name, 0, winreg.KEY_READ | view):
                        raise RuntimeError(f"Existing production registry key: {name}; refusing install")
                except FileNotFoundError:
                    pass
    native = Native()
    for name in (r"Local\LabControlCenter.App", r"Local\LabControlCenter.Setup"):
        ctypes.set_last_error(0)
        handle = native.kernel.OpenMutexW(0x00100000, False, name)
        if handle:
            native.kernel.CloseHandle(handle)
            raise RuntimeError(f"Existing production mutex: {name}; refusing install")
        require(ctypes.get_last_error() == 2, f"Cannot establish absence of production mutex: {name}")
    return runner_temp


class Native:
    """Retain handles after validating exact owned executable paths; no name kills."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.OpenMutexW.restype = wintypes.HANDLE
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self.kernel.GetExitCodeProcess.restype = wintypes.BOOL
        self.kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateProcess.restype = wintypes.BOOL
        self.kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        self.kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        self.callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user.EnumWindows.argtypes = [self.callback, wintypes.LPARAM]
        self.user.EnumWindows.restype = wintypes.BOOL
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user.PostMessageW.restype = wintypes.BOOL
        self.owned = {}

    def process_ids(self):
        class Entry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                        ("executable", wintypes.WCHAR * 260)]
        self.kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        self.kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
        self.kernel.Process32FirstW.restype = self.kernel.Process32NextW.restype = wintypes.BOOL
        handle = self.kernel.CreateToolhelp32Snapshot(2, 0)
        require(handle not in (None, ctypes.c_void_p(-1).value), "Cannot enumerate runner processes")
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            more = self.kernel.Process32FirstW(handle, ctypes.byref(entry))
            while more:
                yield entry.pid
                more = self.kernel.Process32NextW(handle, ctypes.byref(entry))
        finally:
            self.kernel.CloseHandle(handle)

    def capture(self, pid: int, executable: Path):
        if pid in self.owned:
            return self.owned[pid]
        handle = self.kernel.OpenProcess(0x00101001, False, pid)  # synchronize/query/terminate
        if not handle:
            return None
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if (not self.kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length))
                or not same_path(buffer.value, executable)):
            self.kernel.CloseHandle(handle)
            return None
        self.owned[pid] = handle
        return handle

    def running(self, handle) -> bool:
        return self.kernel.WaitForSingleObject(handle, 0) == 258

    def exit_code(self, handle) -> int:
        code = wintypes.DWORD()
        require(self.kernel.GetExitCodeProcess(handle, ctypes.byref(code)), "Cannot read owned process exit status")
        return code.value

    def close_windows(self, pid: int) -> int:
        require(pid in self.owned, "Refusing to close an uncaptured process")
        if not self.running(self.owned[pid]):
            return 0
        count = 0

        @self.callback
        def callback(window, _):
            nonlocal count
            window_pid = wintypes.DWORD()
            self.user.GetWindowThreadProcessId(window, ctypes.byref(window_pid))
            if window_pid.value == pid and self.user.IsWindowVisible(window):
                count += bool(self.user.PostMessageW(window, 0x0010, 0, 0))  # WM_CLOSE
            return True

        require(self.user.EnumWindows(callback, 0), "Cannot enumerate owned app windows")
        return count

    def cleanup(self) -> list[int]:
        forced = []
        for pid, handle in self.owned.items():
            if self.running(handle):
                self.close_windows(pid)
        for pid, handle in self.owned.items():
            if self.kernel.WaitForSingleObject(handle, 10000) == 258:
                self.kernel.TerminateProcess(handle, 1)
                self.kernel.WaitForSingleObject(handle, 10000)
                forced.append(pid)
            self.kernel.CloseHandle(handle)
        self.owned.clear()
        return forced


def gh_json(endpoint: str) -> dict:
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True,
                            encoding="utf-8", errors="strict", timeout=60, check=True)
    return json.loads(result.stdout)


def published_release(tag: str, diagnostics: Path) -> dict:
    require(re.fullmatch(r"v\d+\.\d+\.\d+", tag), "Expected a stable vx.y.z release tag")
    value = gh_json(f"repos/{REPOSITORY}/releases/tags/{tag}")
    require(value.get("tag_name") == tag and value.get("draft") is False
            and value.get("prerelease") is False
            and value.get("html_url") == f"https://github.com/{REPOSITORY}/releases/tag/{tag}",
            "Published release identity is invalid")
    name = f"Lab-Control-Center-{tag[1:]}-windows-x64-setup.exe"
    assets = [asset for asset in value["assets"] if asset.get("name") == name]
    require(len(assets) == 1, f"Expected exactly one published Setup: {name}")
    asset = assets[0]
    require(asset.get("state") == "uploaded" and type(asset.get("size")) is int
            and 0 < asset["size"] <= 1024 ** 3
            and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", asset.get("digest", ""))
            and asset.get("browser_download_url") == f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}",
            "Published Setup URL/size/digest is invalid")
    reference = gh_json(f"repos/{REPOSITORY}/git/ref/tags/{tag}")["object"]
    for _ in range(4):
        if reference["type"] == "commit":
            break
        require(reference["type"] == "tag", "Release tag does not identify a commit")
        reference = gh_json(f"repos/{REPOSITORY}/git/tags/{reference['sha']}")["object"]
    require(reference["type"] == "commit" and re.fullmatch(r"[0-9a-f]{40}", reference["sha"]),
            "Release source revision is not a full commit SHA")
    write_json(diagnostics / f"release-{tag}.json", value)
    return {"tag": tag, "name": name, "size": asset["size"], "sha256": asset["digest"][7:].lower(),
            "source_commit": reference["sha"], "release_id": value["id"], "asset_id": asset["id"]}


def verify_setup(path: Path, release: dict) -> None:
    require(path.is_file() and not path.is_symlink() and path.stat().st_size == release["size"],
            f"Published Setup size mismatch: {path}")
    with path.open("rb") as stream:
        require(stream.read(2) == b"MZ", f"Published Setup is not a Windows executable: {path}")
    require(sha256(path) == release["sha256"], f"Published Setup SHA-256 mismatch: {path}")


def seed_data(data: Path) -> tuple[dict, list[str]]:
    data.mkdir()
    records = []
    parameters = {}
    for kind, resource in (("dg4000", "SIM::DG4000::1"), ("ni_daq", "SIM::NI_DAQ::1"),
                           ("siglent_ssg", "SIM::SIGLENT::SSG6082A-V"),
                           ("newport_smc100", "SIM::SMC100::1"), ("vaunix_lms", "SIM::VAUNIX_LMS::1")):
        identifier = "ci-simulator-" + kind
        records.append({"id": identifier, "kind": kind, "name": "CI simulator " + kind,
                        "resource": resource, "connection": "内置模拟器",
                        "created_at": "2026-10-28T00:00:00+00:00", "updated_at": "2026-10-28T00:00:00+00:00",
                        "last_opened_at": None})
        parameters[identifier] = {"kind": kind, "parameters": {"binding_version": 2,
            "controls": {"named/dg4000.channel.1.frequency": {"type": "unit", "value": 12345.0, "unit": "Hz"},
                         "named/dg4000.channel.1.amplitude": {"type": "unit", "value": 1.234, "unit": "Vpp"}},
            "custom": {"ci_preserved_note": "nonempty saved simulator parameters"}}}
    write_json(data / "devices.json", {"schema_version": 1, "devices": records})
    write_json(data / "device_parameters.json", {"schema_version": 1, "devices": parameters})
    # Deliberately omit automatic_install to verify the legacy preferences default.
    write_json(data / "update_settings.json", {"schema_version": 1, "automatic_check": True,
                                              "automatic_download": True})
    (data / "recordings" / "empty session").mkdir(parents=True)
    (data / "empty-directory").mkdir()
    (data / "recordings" / "capture.csv").write_bytes(b"time_s,voltage_V\r\n0,1.234\r\n0.1,-0.456\r\n")
    (data / "recordings" / "capture.bin").write_bytes(struct.pack("<4d", 0.0, 1.234, 0.1, -0.456))
    hashes = {path.relative_to(data).as_posix(): sha256(path) for path in data.rglob("*") if path.is_file()}
    empty = [path.relative_to(data).as_posix() for path in data.rglob("*") if path.is_dir() and not any(path.iterdir())]
    return hashes, empty


def verify_data(data: Path, hashes: dict, empty: list[str]) -> None:
    for relative, expected in hashes.items():
        require((data / relative).is_file() and sha256(data / relative) == expected,
                f"Seeded user data changed: {relative}")
    for relative in empty:
        require((data / relative).is_dir() and not any((data / relative).iterdir()),
                f"Empty user directory changed: {relative}")


def owned_registration(app: Path, data: Path, version: str | None = None) -> dict:
    registration = read_registration(UNINSTALL_KEY)
    require(registration and same_path(registration.get("InstallLocation", ""), app),
            "Production uninstall registration is not owned by this run; refusing action")
    require(same_path(registration.get("UserDataDir", ""), data)
            and same_path(read_registration(PERSISTENT_KEY).get("UserDataDir", ""), data),
            "Production data registration is not owned by this run; refusing action")
    if version is not None:
        require(registration.get("DisplayVersion") == version, "Registered installed version differs")
    return registration


def verify_installed(app: Path, data: Path, release: dict) -> dict:
    registration = owned_registration(app, data, release["tag"][1:])
    marker = read_json(app / "installed-mode.json")
    require(marker.get("format") == "lab-control-center-installed" and marker.get("schema_version") == 1
            and marker.get("app_id") == APP_ID and marker.get("instance_suffix") == ""
            and same_path(marker["data_dir"], data), "Installed production marker differs")
    require(not (app / "install-pending.json").exists(), "Installer retained its pending marker")
    build = read_json(app / "build-info.json")
    require(build.get("version") == release["tag"][1:] and build.get("source_commit") == release["source_commit"]
            and build.get("source_dirty") is False, "Installed runtime does not match the clean published source")
    return registration


def run_setup(executable: Path, arguments: list[str], diagnostic: Path, timeout=180) -> None:
    result = subprocess.run([str(executable), *arguments], capture_output=True, timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    diagnostic.write_bytes(result.stdout + result.stderr)
    require(result.returncode == 0, f"Installer/uninstaller returned {result.returncode}; see {diagnostic}")


def wait_absent(app: Path, timeout=45) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not read_registration(UNINSTALL_KEY) and not (app / "unins000.exe").exists():
            return
        time.sleep(0.2)
    raise RuntimeError("Owned uninstaller did not finish registry/program cleanup")


def accessible_text(pid: int, script: Path) -> list[str]:
    if not script.exists():
        script.write_text("""param([int]$ApplicationProcessId)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
$condition = [Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ProcessIdProperty, $ApplicationProcessId)
$roots = [Windows.Automation.AutomationElement]::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $condition)
$names = @()
foreach ($root in $roots) {
  $names += $root.Current.Name
  $elements = $root.FindAll([Windows.Automation.TreeScope]::Descendants, [Windows.Automation.Condition]::TrueCondition)
  foreach ($element in $elements) { $names += $element.Current.Name }
}
ConvertTo-Json -InputObject @($names) -Compress
""", encoding="utf-8-sig")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass",
                             "-File", str(script), "-ApplicationProcessId", str(pid)],
                            capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=15,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    require(result.returncode == 0, "Cannot read restarted GUI accessibility text: " + result.stderr[-1000:])
    return json.loads(result.stdout)


def verify_upgrade(previous_tag: str, target_tag: str, diagnostics: Path, timeout: int, report: dict) -> None:
    runner_temp = require_disposable_runner()
    diagnostics = diagnostics.resolve()
    require(diagnostics.is_relative_to(runner_temp) and diagnostics != runner_temp,
            "Diagnostics must be beneath RUNNER_TEMP")
    diagnostics.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="rigol-published-upgrade-", dir=runner_temp)).resolve(strict=True)
    require(work.is_relative_to(runner_temp) and work != runner_temp, "Unsafe test workspace")
    report.update(work_directory=str(work), production_installer_executed=False,
                  runner_environment=os.environ["RUNNER_ENVIRONMENT"], repository=REPOSITORY,
                  hardware_resources="Built-in SIM resources only; v0.7.23 may attempt native driver loading",
                  current_version_overridden=False, mocked_release_or_installer=False)
    previous = published_release(previous_tag, diagnostics)
    require(previous_tag == PREVIOUS_TAG and previous["sha256"] == PREVIOUS_SHA256
            and previous["size"] == PREVIOUS_SIZE and previous["source_commit"] == PREVIOUS_SOURCE,
            "Only the pinned, unmodified first installed release v0.7.23 is supported")
    target = published_release(target_tag, diagnostics)
    require(tuple(map(int, target_tag[1:].split("."))) > tuple(map(int, previous_tag[1:].split("."))),
            "Target must be newer than the real previous runtime")
    latest = gh_json(f"repos/{REPOSITORY}/releases/latest")
    write_json(diagnostics / "latest-before-start.json", latest)
    require(latest.get("id") == target["release_id"] and latest.get("tag_name") == target_tag,
            "Target must be the actual latest stable public release used by the unmodified updater")
    report.update(previous_release=previous, target_release=target)
    packages = work / "packages"
    packages.mkdir()
    subprocess.run(["gh", "release", "download", previous_tag, "--repo", REPOSITORY, "--pattern", previous["name"],
                    "--dir", str(packages)], check=True, timeout=180)
    old_setup = packages / previous["name"]
    verify_setup(old_setup, previous)
    app, data, backups = work / "app", work / "用户 data O'Brien & {保留}", work / "initial-backups"
    hashes, empty = seed_data(data)
    write_json(diagnostics / "seeded-files.json", {"sha256": hashes, "empty_directories": empty})
    native = Native()
    old = None
    install_started = False
    try:
        # Repeat the identity guard immediately before touching the production identity.
        require_disposable_runner()
        install_started = True
        report["production_installer_executed"] = True
        run_setup(old_setup, ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOAUTORUN", "/NOICONS", "/TASKS=",
                             f"/DIR={app}", f"/DATADIR={data}", f"/BACKUPROOT={backups}",
                             f"/LOG={diagnostics / '01-install-previous.log'}"], diagnostics / "01-install-process.log")
        verify_installed(app, data, previous)
        verify_data(data, hashes, empty)
        for name in ("build-info.json", "installed-mode.json"):
            shutil.copy2(app / name, diagnostics / ("previous-" + name))
        report["previous_runtime_installed_unmodified"] = True
        executable = app / "Lab Control Center.exe"
        child_environment = dict(os.environ)
        for name in ("GH_TOKEN", "GITHUB_TOKEN", "RIGOL_GUI_DATA_DIR", "QT_QPA_PLATFORM", "PYTHONPATH", "PYTHONHOME"):
            child_environment.pop(name, None)
        with (diagnostics / "02-old-runtime.log").open("wb") as output:
            # No --smoke-test, startup injection, settings toggle, or version override.
            old = subprocess.Popen([str(executable)], cwd=app, env=child_environment, stdout=output, stderr=output)
        old_handle = native.capture(old.pid, executable)
        require(old_handle, "Cannot retain the original installed runtime process handle")
        report["old_pid"] = old.pid
        started = time.monotonic()
        deadline = started + timeout
        handoff = None
        new_pid = None
        new_handle = None
        phases = []
        while time.monotonic() < deadline:
            for status in (data / "updates").glob("handoff-*/status.json"):
                try:
                    state = read_json(status)
                except (OSError, ValueError):
                    continue
                if state.get("phase") not in phases:
                    phases.append(state.get("phase"))
                    report["observed_handoff_phases"] = phases
                    print("Handoff:", state.get("phase"), flush=True)
                require(state.get("phase") not in {"failed", "cancelled"}, "Real handoff stopped: " + str(state))
                if state.get("phase") == "installed":
                    handoff = status.parent
            if handoff and not native.running(old_handle):
                for pid in native.process_ids():
                    if pid != old.pid:
                        handle = native.capture(pid, executable)
                        if handle and native.running(handle):
                            new_pid, new_handle = pid, handle
                            break
            if new_pid:
                break
            require(old.poll() in (None, 0), "Old runtime exited abnormally before upgrade completed")
            time.sleep(1)
        require(handoff is not None and new_pid is not None and new_handle is not None,
                "Timed out waiting for the real updater, installer, and normal restarted application")
        require(old.wait(timeout=10) == 0 and native.exit_code(old_handle) == 0,
                "Original installed application did not exit normally")
        ticket = read_json(handoff / "ticket.json")
        require(ticket.get("parent_pid") == old.pid and ticket.get("sha256") == target["sha256"]
                and ticket.get("bytes") == target["size"] and same_path(ticket["data_dir"], data)
                and same_path(ticket["install_dir"], app) and same_path(ticket["application"], executable),
                "Real handoff ticket differs from the independently validated target")
        require((handoff / "commit.txt").read_text(encoding="utf-8").strip() == ticket["nonce"]
                and not (handoff / "cancel.txt").exists(), "Old application did not commit a clean handoff")
        downloaded = data / "updates" / target_tag / target["name"]
        require(same_path(ticket["installer"], downloaded), "Updater downloaded an unexpected path")
        verify_setup(downloaded, target)
        registration = verify_installed(app, data, target)
        write_json(diagnostics / "installed-registration.json", registration)
        for name in ("build-info.json", "installed-mode.json"):
            shutil.copy2(app / name, diagnostics / ("target-" + name))
        backup = Path(registration["LastDataBackup"]).resolve(strict=True)
        require(backup.is_relative_to(Path(os.environ["LOCALAPPDATA"]).resolve()) or backup.is_relative_to(work),
                "Installer backup is outside the disposable runner's user/work directories")
        report["upgrade_backup"] = str(backup)
        verify_data(data, hashes, empty)
        manifest = read_json(backup / "manifest.json")
        backup_report, restore_report = read_json(backup.parent / "backup-report.json"), read_json(backup.parent / "restore-report.json")
        manifest_digest = sha256(backup / "manifest.json")
        require(same_path(manifest["source_data_dir"], data) and manifest.get("excluded_top_level") == ["updates"],
                "Backup manifest does not identify the test data")
        for evidence in (backup_report, restore_report):
            require(evidence.get("success") is True and evidence.get("manifest_sha256") == manifest_digest,
                    "Installer backup/restore did not verify the same full manifest SHA-256")
        entries = {entry["path"]: entry for entry in manifest["files"]}
        for relative, digest in hashes.items():
            require(entries.get(relative, {}).get("sha256") == digest
                    and sha256(backup / "files" / relative) == digest, "Backup changed seeded data: " + relative)
        require(set(empty).issubset(set(manifest["directories"])), "Backup omitted empty data directories")
        require((backup.parent / "previous-uninstall.log").is_file(), "New Setup did not invoke the previous uninstaller")
        report.update(old_parent_normal_exit=True, automatic_download_sha256_verified=True,
                      installer_handoff_committed=True, actual_previous_uninstaller_invoked=True,
                      installed_target_source_verified=True, seeded_files_unchanged=True,
                      empty_directories_unchanged=True, upgrade_backup=str(backup), manifest_sha256=manifest_digest,
                      normal_restart_pid=new_pid, upgrade_seconds=round(time.monotonic() - started, 3))
        # Read the normal Qt GUI's accessible status, without injecting any app code.
        expected_status = f"检查完成：GitHub 正式版 {target_tag}，当前版本无需更新。"
        check_deadline = min(deadline, time.monotonic() + 75)
        names = []
        while time.monotonic() < check_deadline:
            require(native.running(new_handle), "Normally restarted application exited unexpectedly")
            try:
                names = accessible_text(new_pid, work / "read-accessibility.ps1")
                write_json(diagnostics / "restarted-ui-text.json", names)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                # Qt providers can briefly invalidate elements while laying out.
                write_json(diagnostics / "restarted-ui-read-error.json", {"error": str(exc)})
            if expected_status in names:
                break
            time.sleep(2)
        require(expected_status in names, "Restarted GUI did not expose a completed up-to-date check; inspect UI diagnostics")
        require(gh_json(f"repos/{REPOSITORY}/releases/latest").get("id") == target["release_id"],
                "Latest release changed during acceptance")
        report["restarted_gui_checked_latest"] = True
        require(native.close_windows(new_pid) > 0, "Restarted application has no normal window to close")
        require(native.kernel.WaitForSingleObject(new_handle, 30000) == 0 and native.exit_code(new_handle) == 0,
                "Restarted application did not accept normal WM_CLOSE and exit cleanly")
        report["restarted_application_normal_close"] = True
        verify_data(data, hashes, empty)
        owned_registration(app, data, target_tag[1:])
        run_setup(app / "unins000.exe", ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                         f"/LOG={diagnostics / '03-normal-uninstall.log'}"],
                  diagnostics / "03-normal-uninstall-process.log")
        wait_absent(app)
        verify_data(data, hashes, empty)
        require((backup / "manifest.json").is_file() and sha256(backup / "manifest.json") == manifest_digest,
                "Normal uninstall removed or changed the verified data backup")
        require(not executable.exists() and not (app / "installed-mode.json").exists(),
                "Normal uninstall retained the program/installation marker")
        require(same_path(read_registration(PERSISTENT_KEY).get("UserDataDir", ""), data),
                "Normal uninstall removed the custom data location")
        report.update(normal_uninstall_retains_data_and_backup=True, data_location_survives_uninstall=True,
                      user_data_files_verified=len(hashes), success=True)
    finally:
        if old is not None and old.poll() is None:
            try:
                write_json(diagnostics / "old-ui-before-cleanup.json",
                           accessible_text(old.pid, work / "read-accessibility.ps1"))
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
                write_json(diagnostics / "old-ui-read-error.json", {"error": str(exc)})
        report["forced_owned_process_cleanup"] = native.cleanup()
        # Cleanup may only touch this run's registration after exact ownership checks.
        try:
            if install_started and read_registration(UNINSTALL_KEY):
                owned_registration(app, data)
                require((app / "unins000.exe").is_file(), "Owned cleanup uninstaller is missing")
                run_setup(app / "unins000.exe", ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                                 f"/LOG={diagnostics / '04-cleanup-uninstall.log'}"],
                          diagnostics / "04-cleanup-uninstall-process.log")
                wait_absent(app)
            persistent = read_registration(PERSISTENT_KEY)
            if persistent:
                require(same_path(persistent.get("UserDataDir", ""), data), "Refusing to delete unowned data registration")
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSISTENT_KEY, 0,
                                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                    require(winreg.QueryInfoKey(key)[0] == 0 and winreg.QueryInfoKey(key)[1] == 1,
                            "Refusing to delete unexpected persistent registry metadata")
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, PERSISTENT_KEY, winreg.KEY_WOW64_64KEY)
            if install_started:
                verify_data(data, hashes, empty)
        finally:
            # Do not remove user data, backups, packages, or program remnants.
            for path in data.glob("updates/handoff-*/*"):
                if path.suffix.lower() in {".json", ".log", ".txt", ".ps1"} and path.is_file():
                    destination = diagnostics / "handoffs" / path.parent.name / path.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination)
            for path in data.glob("*.json"):
                shutil.copy2(path, diagnostics / ("retained-" + path.name))
            for path in backups.rglob("*.json"):
                destination = diagnostics / "initial-backups" / path.relative_to(backups)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
            backup_path = report.get("upgrade_backup")
            if backup_path:
                for path in Path(backup_path).parent.rglob("*"):
                    if path.is_file() and path.suffix.lower() in {".json", ".log"}:
                        destination = diagnostics / "upgrade-backup" / path.relative_to(Path(backup_path).parent)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, destination)
            write_json(diagnostics / "retained-paths.json", {"workspace": str(work), "data": str(data),
                       "initial_backups": str(backups), "upgrade_backup": report.get("upgrade_backup")})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-tag", default=PREVIOUS_TAG)
    parser.add_argument("--target-tag", default="v0.7.24")
    parser.add_argument("--diagnostics-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=600, help="Automatic upgrade deadline in seconds (60-600)")
    args = parser.parse_args()
    report = {"success": False, "started_utc": datetime.now(timezone.utc).isoformat()}
    try:
        require(60 <= args.timeout <= 600, "Upgrade timeout must be between 60 and 600 seconds")
        # Guard before writing files or fetching metadata, including diagnostic output.
        runner_temp = require_disposable_runner()
        candidate = args.diagnostics_dir.resolve()
        require(candidate.is_relative_to(runner_temp) and candidate != runner_temp,
                "Diagnostics must be beneath RUNNER_TEMP")
        diagnostic_path = candidate
        verify_upgrade(args.previous_tag, args.target_tag, diagnostic_path, args.timeout, report)
    except Exception as exc:
        report.update(success=False, error=f"{type(exc).__name__}: {exc}")
        if "diagnostic_path" in locals():
            diagnostic_path.mkdir(parents=True, exist_ok=True)
            (diagnostic_path / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    report["completed_utc"] = datetime.now(timezone.utc).isoformat()
    # Local guard refusal prints only; it must not create a production test workspace.
    if "diagnostic_path" in locals():
        write_json(diagnostic_path / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
