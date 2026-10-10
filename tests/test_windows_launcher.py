"""Exercise the actual Windows launcher and Shell link after relocation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows launcher integration")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / "original"
        (self.project / "tools").mkdir(parents=True)
        (self.project / "src/rigol_control").mkdir(parents=True)
        (self.project / "src/rigol_control/app.py").write_text("", encoding="utf-8")
        shutil.copy2(ROOT / "Lab Control Center.exe", self.project)
        for name in ("create_shortcut.ps1", "windows_shortcut.cs"):
            shutil.copy2(ROOT / "tools" / name, self.project / "tools")
        self.env = {**os.environ, "LAB_CONTROL_PYTHON": sys.executable}

    def powershell(self, script, *args):
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *map(str, args)],
            capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode("utf-8-sig").strip()

    def launch(self, *args):
        return subprocess.run(
            [str(self.project / "Lab Control Center.exe"), "--launcher-no-dialog", *args],
            cwd=self.base, env=self.env, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def test_relocated_launcher_selects_python_and_preserves_arguments(self):
        (self.project / "tools/launch_app.py").write_text(
            "import json,sys,os\nfrom pathlib import Path\n"
            "Path('started.json').write_text(json.dumps({'args':sys.argv[1:],"
            "'python':sys.executable,'cwd':os.getcwd()}),encoding='utf-8')\n",
            encoding="utf-8",
        )
        moved = self.base / "换电脑 O'Brien & space"
        self.project.rename(moved)
        self.project = moved
        args = ["--data-dir", str(moved / "参数 空格"), 'literal"quote', "C:\\trailing\\"]
        # A stale Python setting on the new PC must not block discovery via PATH.
        self.env["LAB_CONTROL_PYTHON"] = str(self.base / "old-computer/python.exe")
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env["PATH"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0)
        data = json.loads((moved / "started.json").read_text(encoding="utf-8"))
        self.assertEqual(data["args"], args)
        self.assertEqual(Path(data["cwd"]), moved)
        self.assertEqual(Path(data["python"]), Path(sys.executable))

    def test_shortcut_resolves_after_original_target_directory_is_gone(self):
        self.powershell(self.project / "tools/create_shortcut.ps1")
        moved = self.base / "new computer 移动目录"
        self.project.rename(moved)
        self.project = moved
        script = self.base / "resolve.ps1"
        script.write_text(
            'param($Project)\n$ErrorActionPreference="Stop"\n'
            '[Console]::OutputEncoding=[Text.Encoding]::UTF8\n'
            'Add-Type -Path (Join-Path $Project "tools/windows_shortcut.cs")\n'
            '[LabControlShortcut]::ResolvedTarget((Join-Path $Project "Lab Control Center.lnk"))\n',
            encoding="utf-8-sig",
        )
        resolved = self.powershell(script, moved)
        # Windows Shell expands an 8.3 TEMP path on hosted runners. Compare
        # file identity so aliases cannot make a correctly relocated link fail.
        self.assertTrue(Path(resolved).samefile(moved / "Lab Control Center.exe"))

    def test_application_failure_returns_failure_and_writes_diagnostic_log(self):
        (self.project / "tools/launch_app.py").write_text(
            "raise RuntimeError('launcher-regression-diagnostic')\n", encoding="utf-8"
        )
        logs = Path(os.environ["LOCALAPPDATA"]) / "RigolGeneratorControl/logs"
        before = set(logs.glob("launcher-*.log"))
        result = self.launch()
        self.assertNotEqual(result.returncode, 0)
        new_logs = set(logs.glob("launcher-*.log")) - before
        self.assertTrue(new_logs)
        self.assertTrue(any("launcher-regression-diagnostic" in p.read_text(encoding="utf-8-sig") for p in new_logs))

    def test_incomplete_project_reports_failure(self):
        (self.project / "src/rigol_control/app.py").unlink()
        self.assertNotEqual(self.launch().returncode, 0)
