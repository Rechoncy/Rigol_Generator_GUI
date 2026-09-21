"""Distribution boundary checks: user data and host Python must stay outside bundles."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_build", ROOT / "tools/build_release.py")
release_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_build)


class ReleaseBuildTests(unittest.TestCase):
    def test_source_archive_allowlist_excludes_user_and_build_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wanted = ["README.md", "pyproject.toml", "src/rigol_control/app.py", "release/README.md"]
            unwanted = ["data/devices.json", "runtime-data/devices.json", ".git/config", "build/env/private.py",
                        "src/rigol_control/__pycache__/app.pyc", "src/rigol_control/settings.json"]
            for name in wanted + unwanted:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("test", encoding="utf-8")
            actual = {p.relative_to(root).as_posix() for p in release_build.source_files(root)}
            self.assertEqual(actual, set(wanted))

    def test_packaged_probe_isolated_from_host_python_and_user_configuration(self):
        values = {"PATH": r"C:\external-python", "SystemRoot": r"C:\Windows", "PYTHONHOME": r"C:\real-python",
                  "CONDA_PREFIX": r"C:\conda", "VIRTUAL_ENV": r"C:\venv", "RIGOL_GUI_DATA_DIR": r"C:\my-data",
                  "QT_PLUGIN_PATH": r"C:\other-qt", "VAUNIX_LMS_DLL": r"C:\vendor.dll"}
        with patch.dict(os.environ, values, clear=True):
            env = release_build.isolated_environment()
        self.assertNotIn("external-python", env["PATH"])
        for key in ("CONDA_PREFIX", "VIRTUAL_ENV", "RIGOL_GUI_DATA_DIR", "QT_PLUGIN_PATH", "VAUNIX_LMS_DLL"):
            self.assertNotIn(key, env)
        self.assertIn("missing", env["PYTHONHOME"])
        self.assertEqual(env["QT_QPA_PLATFORM"], "offscreen")
