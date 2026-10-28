import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from rigol_control import installation, runtime
from rigol_control.install_update import (InstallHandoff, cancel_handoff, commit_handoff,
                                         prepare_handoff, verify_installer)
from rigol_control.updates import ReleaseAsset, parse_release


class InstalledRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.app = self.root / "program"
        self.app.mkdir()
        self.data = self.root / "user data"
        self.exe = patch.object(sys, "executable", str(self.app / "Lab Control Center.exe"))
        self.frozen = patch.object(sys, "frozen", True, create=True)
        self.exe.start()
        self.frozen.start()
        self.env = patch.dict(os.environ)
        self.env.start()
        os.environ.pop("RIGOL_GUI_DATA_DIR", None)

    def tearDown(self):
        self.env.stop()
        self.exe.stop()
        self.frozen.stop()
        self.temporary.cleanup()

    def marker(self, **values):
        payload = dict(format=installation.MARKER_FORMAT, schema_version=1,
                       app_id=installation.APP_ID, data_dir=str(self.data))
        payload.update(values)
        (self.app / installation.MARKER_NAME).write_text(json.dumps(payload), encoding="utf-8")

    def test_installed_mode_uses_preserved_external_data_directory(self):
        self.marker()
        self.assertTrue(runtime.is_installed())
        self.assertEqual(runtime.resolve_data_dir(), self.data)
        self.assertEqual(runtime.resolve_data_dir(self.root / "explicit"), self.root / "explicit")
        os.environ["RIGOL_GUI_DATA_DIR"] = str(self.root / "environment")
        self.assertEqual(runtime.resolve_data_dir(), self.root / "environment")

    def test_corrupt_marker_never_silently_creates_program_data(self):
        for payload in ("{}", "[" * 1500 + "]" * 1500, "x" * 9000):
            with self.subTest(payload=payload[:20]):
                (self.app / installation.MARKER_NAME).write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "安装标记损坏"):
                    runtime.resolve_data_dir()
        self.assertFalse((self.app / "data").exists())

    def test_rejects_embedded_data_and_wrong_application_identity(self):
        for values in ({"data_dir": str(self.app / "data")}, {"app_id": "other"}, {"data_dir": "relative"}):
            self.marker(**values)
            with self.assertRaises(RuntimeError):
                installation.installed_info()

    def test_portable_mode_keeps_its_original_data_location(self):
        self.assertFalse(runtime.is_installed())
        self.assertEqual(runtime.resolve_data_dir(), self.app / "data")

    def test_incomplete_installation_cannot_fall_back_to_program_data(self):
        (self.app / installation.PENDING_MARKER_NAME).write_text("pending", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "安装尚未完成"):
            installation.is_installed()


class InstallerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.name = "Lab-Control-Center-99.0.0-windows-x64-setup.exe"
        self.path = self.data / "updates/v99.0.0" / self.name
        self.path.parent.mkdir(parents=True)
        self.content = b"MZ" + b"verified installer fixture" * 10
        self.path.write_bytes(self.content)
        self.asset = ReleaseAsset(self.name,
            "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v99.0.0/" + self.name,
            len(self.content), hashlib.sha256(self.content).hexdigest())

    def tearDown(self):
        self.temporary.cleanup()

    def test_rechecks_setup_bytes_and_exact_download_path(self):
        self.assertEqual(verify_installer(self.path, self.asset, self.data), self.path.resolve())
        self.path.write_bytes(self.content[:-1] + b"X")
        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            verify_installer(self.path, self.asset, self.data)
        self.path.write_bytes(self.content[:-1])
        with self.assertRaisesRegex(RuntimeError, "大小"):
            verify_installer(self.path, self.asset, self.data)

    def test_cannot_execute_standalone_or_other_location(self):
        exe = ReleaseAsset(self.name.replace("-setup.exe", ".exe"), "invalid", 1, "0" * 64)
        with self.assertRaises(Exception):
            verify_installer(self.path, exe, self.data)
        elsewhere = self.root / self.name
        elsewhere.write_bytes(self.content)
        with self.assertRaisesRegex(RuntimeError, "路径"):
            verify_installer(elsewhere, self.asset, self.data)

    def test_verified_github_setup_asset_is_supported(self):
        payload = {"draft": False, "prerelease": False, "tag_name": "v99.0.0", "assets": [{
            "name": self.name, "size": self.asset.size, "digest": "sha256:" + self.asset.sha256,
            "state": "uploaded", "browser_download_url": self.asset.url}]}
        self.assertEqual(parse_release(payload).assets, (self.asset,))

    def test_helper_ready_never_creates_install_commit_until_explicit_clean_close(self):
        def launch(command, **kwargs):
            ticket = Path(command[-1])
            data = json.loads(ticket.read_text(encoding="utf-8"))
            Path(data["status_file"]).write_text('{"phase":"waiting-parent"}', encoding="utf-8")
            self.assertNotIn("-Command", command)
            self.assertEqual(command[-4], "-File")
            self.assertFalse(Path(data["commit_file"]).exists())
            return Mock(poll=Mock(return_value=None))

        with patch("rigol_control.install_update.installed_info", return_value={"data_dir": str(self.data)}):
            handoff = prepare_handoff(self.path, self.asset, self.data, launcher=launch)
        self.assertFalse(handoff.commit_file.exists())
        self.assertTrue((handoff.directory / "apply-update.ps1").read_bytes().startswith(b"\xef\xbb\xbf"))
        commit_handoff(handoff)
        self.assertEqual(handoff.commit_file.read_text().strip(), handoff.nonce)
        cancel_handoff(handoff)
        self.assertTrue(handoff.cancel_file.exists())

    def test_helper_dispatch_or_handshake_failure_is_cancelled(self):
        with patch("rigol_control.install_update.installed_info", return_value={}), \
                self.assertRaisesRegex(RuntimeError, "就绪"):
            prepare_handoff(self.path, self.asset, self.data, launcher=lambda *a, **k: Mock(poll=lambda: 1))
        folders = list((self.data / "updates").glob("handoff-*"))
        self.assertEqual(len(folders), 1)
        self.assertTrue((folders[0] / "cancel.txt").exists())
        self.assertFalse((folders[0] / "commit.txt").exists())

    def test_portable_mode_does_not_launch_any_installer(self):
        launcher = Mock()
        with patch("rigol_control.install_update.installed_info", return_value=None), self.assertRaises(RuntimeError):
            prepare_handoff(self.path, self.asset, self.data, launcher=launcher)
        launcher.assert_not_called()
