"""Exercise install/upgrade/uninstall using an isolated per-user test identity.

The production AppId is never installed. Test payloads use the exact installer
script and current frozen runtime, with only a separate AppId/mutex/shortcut
suffix. The old-version fixture additionally owns one obsolete program file.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

if sys.platform == "win32":
    import winreg
else:
    winreg = None

from build_installer import APP_ID, build_installer


ROOT = Path(__file__).resolve().parents[1]


def require(condition, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_hashes(folder: Path) -> dict[str, str]:
    return {path.relative_to(folder).as_posix(): digest(path)
            for path in sorted(folder.rglob("*")) if path.is_file()}


def read_value(key_path: str, name: str):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                        winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        return winreg.QueryValueEx(key, name)[0]


def wait_for_uninstall(uninstaller: Path, key_path: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            read_value(key_path, "InstallLocation")
            registered = True
        except FileNotFoundError:
            registered = False
        if not uninstaller.exists() and not registered:
            return
        time.sleep(0.05)
    raise RuntimeError("Isolated test uninstaller did not complete program/registry cleanup")


def execute(executable: Path, arguments: list[str], log: Path, *, expected_success: bool = True,
            timeout: int = 180) -> int:
    result = subprocess.run([str(executable), *arguments], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    if expected_success:
        require(result.returncode == 0, f"Process failed ({result.returncode}): {executable}; {log}")
    else:
        require(result.returncode != 0, f"Unsafe installer attempt unexpectedly succeeded: {executable}")
    return result.returncode


def previous_fixture_version(version: str) -> str:
    parts = [int(part) for part in version.split(".")]
    if parts[2]:
        parts[2] -= 1
    return ".".join(map(str, parts))


def verify_installer(portable_dir: Path, version: str, work_dir: Path, *,
                     iscc: Path | None = None) -> dict:
    require(sys.platform == "win32", "Installer acceptance requires Windows")
    portable_dir = portable_dir.resolve(strict=True)
    work_dir = work_dir.resolve()
    if work_dir.exists():
        require(not any(work_dir.iterdir()), f"Refusing to reuse acceptance directory: {work_dir}")
    work_dir.mkdir(parents=True, exist_ok=True)
    test_id = "accept-" + uuid.uuid4().hex[:16]
    test_uuid = str(uuid.uuid5(uuid.UUID(APP_ID), test_id)).upper()
    uninstall_key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{{{test_uuid}}}_is1"
    persistent_key = rf"Software\Rechoncy\LabControlCenter-Test-{test_id}"
    app_dir = work_dir / "安装 O'Brien & app"
    data_dir = work_dir / "用户 data {kept}"
    backup_root = work_dir / "保留 backups"
    data_dir.mkdir()
    (data_dir / "devices.json").write_text('{"schema_version":1,"devices":[]}\n', encoding="utf-8")
    (data_dir / "device_parameters.json").write_text('{"schema_version":1,"devices":{}}\n', encoding="utf-8")
    (data_dir / "recordings").mkdir()
    (data_dir / "recordings" / "capture.csv").write_text("time,value\n0,1.25\n", encoding="utf-8")
    (data_dir / "empty-directory").mkdir()
    (data_dir / "updates").mkdir()
    (data_dir / "updates" / "kept-download.bin").write_bytes(b"cached package retained")
    expected_hashes = file_hashes(data_dir)
    before_data_directories = {path.relative_to(data_dir).as_posix()
                               for path in data_dir.rglob("*") if path.is_dir()}
    old_payload = work_dir / "old-payload-fixture"
    shutil.copytree(portable_dir, old_payload)
    previous = previous_fixture_version(version)
    info_path = old_payload / "build-info.json"
    old_info = json.loads(info_path.read_text(encoding="utf-8"))
    old_info["version"] = previous
    info_path.write_text(json.dumps(old_info, ensure_ascii=False, indent=2), encoding="utf-8")
    obsolete = "old-owned-obsolete-program-file.txt"
    (old_payload / obsolete).write_text("Owned by old fixture installer only.\n", encoding="utf-8")
    outputs = work_dir / "compiled-test-setups"
    old_setup = build_installer(old_payload, previous, outputs, iscc=iscc, test_id=test_id)
    new_setup = build_installer(portable_dir, version, outputs, iscc=iscc, test_id=test_id)
    setup_arguments = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOAUTORUN", "/NOICONS",
                       '/TASKS=', f"/DIR={app_dir}", f"/DATADIR={data_dir}", f"/BACKUPROOT={backup_root}"]
    mutex_handle = None
    lock_handle = None
    installed = False
    report = {
        "success": False, "software_version": version, "test_id": test_id,
        "test_installer_app_id": "{" + test_uuid + "}",
        "production_installer_executed": False, "physical_hardware_access": False,
        "network_access": False,
        "previous_fixture_display_version": previous, "previous_fixture_runtime_version": version,
        "installer_script_sha256": digest(ROOT / "release/installer.iss"),
        "work_directory": str(work_dir),
    }
    uninstaller = app_dir / "unins000.exe"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                  ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    try:
        execute(old_setup, setup_arguments + [f"/LOG={work_dir / '01-install.log'}"],
                work_dir / "01-install-process.log")
        installed = True
        require((app_dir / obsolete).is_file(), "Old fixture program file was not installed")
        require(file_hashes(data_dir) == expected_hashes, "First install changed existing user data")
        marker = json.loads((app_dir / "installed-mode.json").read_text(encoding="utf-8"))
        require(marker["app_id"] == "{" + APP_ID + "}" and Path(marker["data_dir"]) == data_dir,
                "Installed marker does not identify its separate data directory")
        require(marker.get("instance_suffix") == "-Test-" + test_id,
                "Test installation does not have its isolated runtime mutex namespace")
        require(not (app_dir / "install-pending.json").exists(), "Successful first install retained pending marker")
        report.update(first_install_preserved_user_data=True, isolated_instance_namespace=True)

        # A running application must never be force-closed by silent update.
        mutex_handle = kernel.CreateMutexW(None, False, "Local\\LabControlCenter.App-Test-" + test_id)
        require(bool(mutex_handle), "Cannot create acceptance app mutex")
        execute(new_setup, setup_arguments + [f"/LOG={work_dir / '02-busy.log'}"],
                work_dir / "02-busy-process.log", expected_success=False)
        kernel.CloseHandle(mutex_handle)
        mutex_handle = None
        require((app_dir / obsolete).is_file() and uninstaller.is_file(),
                "Busy update removed the previous program")
        report["running_application_blocks_installation"] = True

        # Windows exclusive-read denial proves backup failure blocks uninstall.
        locked = data_dir / "recordings" / "capture.csv"
        lock_handle = kernel.CreateFileW(str(locked), 0x80000000, 0, None, 3, 0x80, None)
        require(lock_handle not in (None, ctypes.c_void_p(-1).value), "Cannot lock acceptance data file")
        execute(new_setup, setup_arguments + [f"/LOG={work_dir / '03-backup-failed.log'}"],
                work_dir / "03-backup-failed-process.log", expected_success=False)
        kernel.CloseHandle(lock_handle)
        lock_handle = None
        require((app_dir / obsolete).is_file() and uninstaller.is_file(),
                "Backup failure removed the previous program")
        require(file_hashes(data_dir) == expected_hashes, "Backup failure changed user data")
        backup_failures = [json.loads(path.read_text(encoding="utf-8"))
                           for path in backup_root.glob("*/backup-report.json")]
        require(any(value.get("success") is False for value in backup_failures),
                "Denied-read scenario did not reach the real data backup helper")
        report["backup_failure_blocks_old_uninstall"] = True

        # A file not owned by either installer must also survive program cleanup.
        (app_dir / "untracked-user-note.txt").write_text("retain", encoding="utf-8")
        execute(new_setup, setup_arguments + [f"/LOG={work_dir / '04-upgrade.log'}"],
                work_dir / "04-upgrade-process.log")
        require(not (app_dir / obsolete).exists(), "Previous owned program file survived upgrade")
        require((app_dir / "untracked-user-note.txt").read_text(encoding="utf-8") == "retain",
                "Upgrade deleted an untracked user file")
        require(read_value(uninstall_key, "DisplayVersion") == version, "New version was not registered")
        backup = Path(read_value(uninstall_key, "LastDataBackup"))
        require((backup / "manifest.json").is_file(), "Upgrade backup manifest is missing")
        require((backup.parent / "previous-uninstall.log").is_file(), "Previous uninstaller was not invoked")
        require(file_hashes(data_dir) == expected_hashes, "Upgrade changed user data")
        require({path.relative_to(data_dir).as_posix() for path in data_dir.rglob("*") if path.is_dir()}
                == before_data_directories, "Upgrade changed empty user directories")
        require(not (app_dir / "install-pending.json").exists(), "Upgrade retained pending marker")
        report.update(previous_program_uninstalled=True, user_data_hashes_preserved=True,
                      empty_directories_preserved=True, untracked_program_file_preserved=True,
                      upgrade_backup=str(backup))

        # Explicitly remove only this disposable fixture file to exercise the
        # real frozen restore CLI's missing-file recovery, without real data.
        locked.unlink()
        executable = app_dir / "Lab Control Center.exe"
        execute(executable, ["--installer-data-restore", str(data_dir), "--backup", str(backup),
                              "--report", str(work_dir / "restore-missing-report.json")],
                work_dir / "restore-missing-process.log")
        require(file_hashes(data_dir) == expected_hashes, "Frozen restore did not recover missing data")
        report["frozen_restore_recovers_missing_files"] = True

        execute(executable, ["--smoke-test", "--data-dir", str(work_dir / "empty-smoke-data")],
                work_dir / "installed-smoke-process.log", timeout=90)
        probe_path = work_dir / "installed-bundle-self-test.json"
        execute(executable, ["--bundle-self-test", str(probe_path)],
                work_dir / "installed-bundle-process.log", timeout=90)
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        require(probe.get("success") is True and probe.get("frozen") is True
                and probe.get("hardware_access") is False, "Installed frozen self-test failed")
        report.update(installed_normal_startup=True, installed_bundle_self_test=True)

        execute(uninstaller, ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                               f"/LOG={work_dir / '05-uninstall.log'}"], work_dir / "05-uninstall-process.log")
        wait_for_uninstall(uninstaller, uninstall_key)
        installed = False
        require(file_hashes(data_dir) == expected_hashes, "Normal uninstall deleted user data")
        require(read_value(persistent_key, "UserDataDir") == str(data_dir),
                "Normal uninstall lost custom data-directory location")
        require(not executable.exists() and not (app_dir / "installed-mode.json").exists(),
                "Normal uninstall retained installed program/marker")
        report.update(normal_uninstall_preserves_user_data=True, data_location_survives_uninstall=True)

        # No /DATADIR: the saved per-user location must be reused after uninstall.
        reinstall_arguments = [argument for argument in setup_arguments if not argument.startswith("/DATADIR=")]
        execute(new_setup, reinstall_arguments + [f"/LOG={work_dir / '06-reinstall.log'}"],
                work_dir / "06-reinstall-process.log")
        installed = True
        marker = json.loads((app_dir / "installed-mode.json").read_text(encoding="utf-8"))
        require(Path(marker["data_dir"]) == data_dir and file_hashes(data_dir) == expected_hashes,
                "Reinstall did not recover the previous custom data directory")
        report["reinstall_recovers_custom_data_directory"] = True
        report["success"] = True
    finally:
        if mutex_handle:
            kernel.CloseHandle(mutex_handle)
        if lock_handle and lock_handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(lock_handle)
        if uninstaller.exists() and (app_dir / "Lab Control Center.exe").exists():
            execute(uninstaller, ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                   f"/LOG={work_dir / '07-test-cleanup.log'}"],
                    work_dir / "07-test-cleanup-process.log")
            wait_for_uninstall(uninstaller, uninstall_key)
        # Only this generated test identity is removed. Test data/backups and
        # evidence remain available; production registry entries are untouched.
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, persistent_key, winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            pass
    report["user_data_files_verified"] = len(expected_hashes)
    require(file_hashes(data_dir) == expected_hashes, "Acceptance cleanup changed user data")
    report["completed_utc"] = datetime.now(timezone.utc).isoformat()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_installer(args.portable_dir, args.version, args.work_dir, iscc=args.iscc)
    except Exception as exc:
        report = {"success": False, "error": str(exc), "physical_hardware_access": False,
                  "production_installer_executed": False, "work_directory": str(args.work_dir)}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
