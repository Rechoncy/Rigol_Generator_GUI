"""Installed acceptance must check external data without creating real data."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from rigol_control import installation, runtime

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("frozen_default_probe", ROOT / "tools/frozen_entry.py")
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)


class FrozenDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.app = self.root / "application"
        self.app.mkdir()
        self.data = self.root / "external user data"
        self.exe = patch.object(sys, "executable", str(self.app / "Lab Control Center.exe"))
        self.frozen = patch.object(sys, "frozen", True, create=True)
        self.env = patch.dict(os.environ)
        self.exe.start()
        self.frozen.start()
        self.env.start()
        os.environ.pop("RIGOL_GUI_DATA_DIR", None)

    def tearDown(self):
        self.env.stop()
        self.frozen.stop()
        self.exe.stop()
        self.temporary.cleanup()

    def test_portable_baseline_checks_executable_data_without_creating_it(self):
        report = {}
        entry._check_default_data_directory(runtime.resolve_data_dir(), report)
        self.assertEqual(report["default_data_mode"], "portable")
        self.assertFalse((self.app / "data").exists())

    def test_installed_baseline_requires_external_marker_data(self):
        (self.app / installation.MARKER_NAME).write_text(json.dumps({
            "format": installation.MARKER_FORMAT, "schema_version": 1,
            "app_id": installation.APP_ID, "data_dir": str(self.data)}), encoding="utf-8")
        report = {}
        entry._check_default_data_directory(runtime.resolve_data_dir(), report)
        self.assertEqual(report["default_data_mode"], "installed")
        with self.assertRaises(AssertionError):
            entry._check_default_data_directory(self.app / "data", {})
        self.assertFalse(self.data.exists())

    def test_pending_installer_never_becomes_a_portable_data_baseline(self):
        (self.app / installation.PENDING_MARKER_NAME).write_text("pending", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            entry._check_default_data_directory(self.app / "data", {})
        self.assertFalse((self.app / "data").exists())
