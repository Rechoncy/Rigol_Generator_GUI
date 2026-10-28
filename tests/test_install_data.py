"""Installer data preservation uses disposable folders and never opens hardware."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rigol_control import install_data
from rigol_control.install_data import (
    InstallDataError, restore_user_data, run_install_data_cli, snapshot_user_data,
)


class InstallDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / "用户 data O'Brien"
        self.backups = self.root / "upgrade-backups"
        self.data.mkdir()
        self.contents = {
            "devices.json": b'{"stable_id":"a-123","resource":"SIM::DEVICE"}\n',
            "device_parameters.json": '{"说明":"待应用值，不连接硬件"}\n'.encode(),
            "recordings/测量 1.bin": bytes(range(256)) * 100,
            "recordings/nested/result.csv": b"time,value\n0,1.5\n",
            "empty-file": b"",
        }
        for relative, content in self.contents.items():
            path = self.data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (self.data / "empty/directory").mkdir(parents=True)
        (self.data / "updates/v0.7.22").mkdir(parents=True)
        (self.data / "updates/v0.7.22/download.exe").write_bytes(b"cache is not user data")

    def snapshot(self):
        result = snapshot_user_data(self.data, self.backups)
        self.assertTrue(result["success"])
        return Path(result["backup_path"])

    def edit_manifest(self, backup, change):
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        change(manifest)
        (backup / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_full_byte_round_trip_preserves_measurements_configuration_and_empty_dirs(self):
        result = snapshot_user_data(self.data, self.backups)
        backup = Path(result["backup_path"])
        self.assertEqual(result["file_count"], len(self.contents))
        self.assertEqual(result["byte_count"], sum(map(len, self.contents.values())))
        self.assertEqual(hashlib.sha256((backup / "manifest.json").read_bytes()).hexdigest(),
                         result["manifest_sha256"])
        self.assertFalse((backup / "files/updates").exists())
        manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["source_existed"])
        self.assertEqual(manifest["source_data_dir"], str(self.data))
        restored = self.root / "restored"
        verification = restore_user_data(restored, backup)
        self.assertEqual(verification["restored_file_count"], len(self.contents))
        self.assertTrue((restored / "empty/directory").is_dir())
        for relative, content in self.contents.items():
            self.assertEqual((restored / relative).read_bytes(), content)
            self.assertEqual((self.data / relative).read_bytes(), content)

    def test_missing_data_is_a_valid_empty_snapshot_and_restore_creates_data(self):
        missing = self.root / "first installation/data"
        result = snapshot_user_data(missing, self.backups)
        self.assertFalse(missing.exists())
        self.assertFalse(result["source_existed"])
        self.assertEqual(result["file_count"], 0)
        restored = restore_user_data(missing, result["backup_path"])
        self.assertTrue(missing.is_dir())
        self.assertEqual(restored["verified_file_count"], 0)
        self.assertEqual(list(missing.iterdir()), [])

    def test_retained_files_are_verified_and_only_missing_files_are_restored(self):
        backup = self.snapshot()
        (self.data / "recordings/测量 1.bin").unlink()
        extra = self.data / "new-version-created.json"
        extra.write_bytes(b"new version data")
        result = restore_user_data(self.data, backup)
        self.assertEqual(result["restored_file_count"], 1)
        self.assertEqual(extra.read_bytes(), b"new version data")
        self.assertTrue((self.data / "updates/v0.7.22/download.exe").is_file())

    def test_existing_conflict_is_not_overwritten_and_preflight_prevents_partial_restore(self):
        backup = self.snapshot()
        target = self.root / "target"
        target.mkdir()
        conflict = target / "devices.json"
        conflict.write_bytes(b"keep newer user data")
        with self.assertRaisesRegex(InstallDataError, "已保留原文件"):
            restore_user_data(target, backup)
        self.assertEqual(conflict.read_bytes(), b"keep newer user data")
        self.assertEqual([path.name for path in target.iterdir()], ["devices.json"])

    def test_corrupt_backup_bytes_are_rejected_before_target_changes(self):
        backup = self.snapshot()
        (backup / "files/devices.json").write_bytes(b"corrupt")
        target = self.root / "target"
        with self.assertRaisesRegex(InstallDataError, "SHA-256"):
            restore_user_data(target, backup)
        self.assertFalse(target.exists())

    def test_backup_rejects_unlisted_files_and_missing_files(self):
        backup = self.snapshot()
        additional = backup / "files/unlisted.txt"
        additional.write_bytes(b"extra")
        with self.assertRaisesRegex(InstallDataError, "列表"):
            restore_user_data(self.root / "target", backup)
        additional.unlink()
        (backup / "files/empty-file").unlink()
        with self.assertRaisesRegex(InstallDataError, "列表"):
            restore_user_data(self.root / "target", backup)

    def test_malicious_relative_paths_and_duplicate_paths_are_rejected(self):
        backup = self.snapshot()
        original = (backup / "manifest.json").read_bytes()
        for bad in ("../outside", "/absolute", "C:/outside", "a\\b", "a:b", "a/../b",
                    "a//b", "a/./b", "a.", "a ", "CON.txt", "file\x00.txt"):
            with self.subTest(path=bad):
                (backup / "manifest.json").write_bytes(original)
                self.edit_manifest(backup, lambda manifest: manifest["files"][0].update(path=bad))
                with self.assertRaises(InstallDataError):
                    restore_user_data(self.root / "target", backup)
        (backup / "manifest.json").write_bytes(original)
        self.edit_manifest(backup, lambda manifest: manifest["files"].append(copy.deepcopy(manifest["files"][0])))
        with self.assertRaisesRegex(InstallDataError, "重复"):
            restore_user_data(self.root / "target", backup)
        self.assertFalse((self.root / "outside").exists())

    def test_manifest_type_deep_json_duplicate_fields_and_missing_parent_rejected(self):
        backup = self.snapshot()
        original = (backup / "manifest.json").read_bytes()
        for raw in (b"[" * 1500 + b"]" * 1500, b'[]', b'{"format":"x","format":"y"}'):
            with self.subTest(raw=raw[:40]):
                (backup / "manifest.json").write_bytes(raw)
                with self.assertRaises(InstallDataError):
                    restore_user_data(self.root / "target", backup)
        (backup / "manifest.json").write_bytes(original)
        self.edit_manifest(backup, lambda manifest: manifest.update(directories=[]))
        with self.assertRaisesRegex(InstallDataError, "父目录"):
            restore_user_data(self.root / "target", backup)

    def test_overlapping_data_and_backup_locations_are_rejected(self):
        for backup in (self.data, self.data / "backups", self.root):
            with self.subTest(backup=backup), self.assertRaisesRegex(InstallDataError, "分开"):
                snapshot_user_data(self.data, backup)
        self.assertEqual((self.data / "devices.json").read_bytes(), self.contents["devices.json"])

    def test_copy_failure_does_not_publish_partial_backup_or_change_original(self):
        real_read = install_data._read_stable
        def fail(path, **kwargs):
            if kwargs.get("destination") is not None and path.name == "empty-file":
                raise OSError("disk full")
            return real_read(path, **kwargs)
        with patch.object(install_data, "_read_stable", side_effect=fail):
            with self.assertRaisesRegex(InstallDataError, "disk full"):
                self.snapshot()
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual((self.data / "devices.json").read_bytes(), self.contents["devices.json"])

    def test_source_changes_during_copy_fail_without_published_snapshot(self):
        real_read = install_data._read_stable
        changed = False
        def change(path, **kwargs):
            nonlocal changed
            result = real_read(path, **kwargs)
            if kwargs.get("destination") is not None and not changed:
                changed = True
                (self.data / "new-during-backup.txt").write_bytes(b"external writer")
            return result
        with patch.object(install_data, "_read_stable", side_effect=change):
            with self.assertRaisesRegex(InstallDataError, "目录已变化"):
                self.snapshot()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_publish_race_preserves_file_created_by_another_writer(self):
        backup = self.snapshot()
        target = self.root / "target"
        real_publish = install_data._publish_file
        def collide(temporary, destination):
            destination.write_bytes(b"created concurrently")
            return real_publish(temporary, destination)
        with patch.object(install_data, "_publish_file", side_effect=collide):
            with self.assertRaises(InstallDataError):
                restore_user_data(target, backup)
        self.assertTrue(any(path.read_bytes() == b"created concurrently" for path in target.rglob("*") if path.is_file()))
        self.assertFalse(list(target.rglob(".restore-*.tmp")))

    def test_symlink_in_data_or_backup_or_target_is_never_followed(self):
        outside = self.root / "outside.txt"
        outside.write_bytes(b"outside")
        link = self.data / "linked.txt"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        with self.assertRaisesRegex(InstallDataError, "链接"):
            self.snapshot()
        link.unlink()
        backup = self.snapshot()
        (backup / "files/devices.json").unlink()
        (backup / "files/devices.json").symlink_to(outside)
        with self.assertRaisesRegex(InstallDataError, "链接"):
            restore_user_data(self.root / "target", backup)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_reparse_point_detection_catches_windows_junction_attributes(self):
        original_scan = os.scandir
        class ReparseEntry:
            name = "junction"
            def stat(self, follow_symlinks=False):
                result = type("FakeStat", (), {})()
                result.st_mode = 0o040755
                result.st_file_attributes = 0x400
                return result
        class Entries:
            def __enter__(self):
                return iter([ReparseEntry()])
            def __exit__(self, *args):
                pass
        with patch.object(install_data.os, "scandir", return_value=Entries()):
            with self.assertRaisesRegex(InstallDataError, "重解析"):
                snapshot_user_data(self.data, self.backups)
        self.assertFalse(self.backups.exists())

    def test_cli_backup_restore_reports_success_without_touching_devices(self):
        report = self.root / "receipt.json"
        code = run_install_data_cli(["--installer-data-backup", str(self.data),
                                     "--backup-root", str(self.backups), "--report", str(report)])
        self.assertEqual(code, 0)
        result = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(result["success"])
        restore_report = self.root / "restored-receipt.json"
        target = self.root / "new-data"
        code = run_install_data_cli(["--installer-data-restore", str(target),
                                     "--backup", result["backup_path"], "--report", str(restore_report)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(restore_report.read_text(encoding="utf-8"))["success"])
        self.assertEqual((target / "devices.json").read_bytes(), self.contents["devices.json"])

    def test_cli_failure_report_and_report_inside_data_do_not_modify_user_data(self):
        report = self.root / "failed.json"
        code = run_install_data_cli(["--installer-data-backup", str(self.data),
                                     "--backup-root", str(self.data / "nested"), "--report", str(report)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(report.read_text(encoding="utf-8"))["success"])
        unsafe_report = self.data / "devices.json"
        with patch.object(install_data, "snapshot_user_data") as snapshot:
            code = run_install_data_cli(["--installer-data-backup", str(self.data),
                                         "--backup-root", str(self.backups), "--report", str(unsafe_report)])
        self.assertEqual(code, 1)
        snapshot.assert_not_called()
        self.assertEqual(unsafe_report.read_bytes(), self.contents["devices.json"])
