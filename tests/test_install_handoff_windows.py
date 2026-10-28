"""Offline Windows handoff regression: only locally compiled inert EXEs run."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
PYTHON = Path(sys.executable).resolve()
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def child_parent(configuration):
    from rigol_control.install_update import commit_handoff, prepare_handoff
    from rigol_control.updates import ReleaseAsset
    config = json.loads(Path(configuration).read_text(encoding="utf-8"))
    asset = ReleaseAsset(**config["asset"])
    helper_process = []

    def launcher(command, **kwargs):
        assert command[-2] == "-Ticket" and command[-4] == "-File"
        process = subprocess.Popen(command, **kwargs)
        helper_process.append(process)
        return process

    with patch("rigol_control.install_update.installed_info", return_value={}), \
            patch.object(sys, "executable", config["application"]):
        handoff = prepare_handoff(Path(config["installer"]), asset, Path(config["data_dir"]),
                                  launcher=launcher, ready_timeout=15)
    if config["commit"]:
        commit_handoff(handoff)
    save(Path(config["ready"]), {
        "parent_pid": os.getpid(), "helper_pid": helper_process[0].pid,
        "commit_published": config["commit"], "ticket": str(handoff.ticket),
        "status": str(handoff.status_file), "commit_file": str(handoff.commit_file),
        "nonce": handoff.nonce,
    })
    deadline = time.monotonic() + 30
    while not Path(config["release"]).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Acceptance controller did not release its dummy parent")
        time.sleep(0.025)
    return 0


def wait_until(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise TimeoutError("Fixture acceptance timed out")


class OwnedWindowsProcess:
    def __init__(self, pid):
        self.pid = pid
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        self.kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, pid)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "Cannot bind our known helper process")

    def exit_code(self):
        if self.kernel.WaitForSingleObject(self.handle, 0) != 0:
            return None
        value = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.handle, ctypes.byref(value)):
            raise OSError(ctypes.get_last_error(), "Cannot obtain helper exit code")
        return value.value

    def close(self):
        if self.handle:
            if self.exit_code() is None:
                # On any failed fixture assertion, stop only our captured helper
                # so it cannot leave an error dialog needing human interaction.
                self.kernel.TerminateProcess(self.handle, 125)
                self.kernel.WaitForSingleObject(self.handle, 5000)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def run_case(fixture_root, compiled):
    commit = True
    case = fixture_root / 'positive'
    data = case / "用户 data O'Brien & {保留}"
    program = case / "程序 O'Brien & {安装}"
    data.mkdir(parents=True)
    program.mkdir()
    installer = data / "updates/v99.0.0/Lab-Control-Center-99.0.0-windows-x64-setup.exe"
    installer.parent.mkdir(parents=True)
    installer.write_bytes(compiled.read_bytes())
    application = program / "Lab Control Center.exe"
    application.write_bytes(compiled.read_bytes())
    sentinel = data / "用户数据.txt"
    sentinel_bytes = "保留用户数据，不执行真实安装。\n".encode("utf-8")
    sentinel.write_bytes(sentinel_bytes)
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    assert installer.read_bytes()[:2] == b"MZ"
    installer_receipt = installer.parent / "installer-arguments.json"
    application_receipt = program / "application-arguments.json"
    config = {
        "asset": {"name": installer.name,
                  "url": "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v99.0.0/" + installer.name,
                  "size": installer.stat().st_size, "sha256": digest},
        "installer": str(installer), "application": str(application), "data_dir": str(data),
        "commit": commit, "ready": str(case / "parent-ready.json"),
        "release": str(case / "release-parent.txt"),
    }
    config_path = case / "configuration.json"
    save(config_path, config)
    helper = None
    actual_parent = None
    child_environment = dict(os.environ)
    child_environment.pop("PSModulePath", None)
    with (case / "dummy-parent.log").open("w", encoding="utf-8") as parent_log:
        parent = subprocess.Popen([str(PYTHON), str(SCRIPT), "--dummy-parent", str(config_path)],
                                  cwd=ROOT, stdout=parent_log, stderr=subprocess.STDOUT,
                                  creationflags=CREATE_NO_WINDOW, env=child_environment)
        try:
            def ready():
                if parent.poll() is not None:
                    raise RuntimeError("Dummy parent failed before helper readiness; inspect fixture log")
                return Path(config["ready"]).exists()
            wait_until(ready)
            receipt = json.loads(Path(config["ready"]).read_text(encoding="utf-8"))
            helper = OwnedWindowsProcess(receipt["helper_pid"])
            actual_parent = OwnedWindowsProcess(receipt["parent_pid"])
            status_file = Path(receipt["status"])
            ticket_file = Path(receipt["ticket"])
            ticket = json.loads(ticket_file.read_text(encoding="utf-8"))
            original_script = (ROOT / "src/rigol_control/assets/apply_update.ps1").read_text(encoding="utf-8-sig")
            used_script = ticket_file.parent / "apply-update.ps1"
            assert used_script.read_bytes().startswith(b"\xef\xbb\xbf")
            assert used_script.read_text(encoding="utf-8-sig") == original_script
            # A venv python.exe may be a redirector with its own outer PID.
            # The production ticket correctly uses the interpreter's real PID.
            assert ticket["parent_pid"] == receipt["parent_pid"]
            assert Path(ticket["installer"]) == installer and ticket["sha256"] == digest
            assert Path(ticket["data_dir"]) == data and Path(ticket["install_dir"]) == program
            assert Path(ticket["application"]) == application
            assert ticket["bytes"] == installer.stat().st_size
            assert json.loads(status_file.read_text(encoding="utf-8-sig"))["phase"] == "waiting-parent"
            if commit:
                assert Path(receipt["commit_file"]).read_text(encoding="utf-8").strip() == receipt["nonce"]
            else:
                assert not Path(receipt["commit_file"]).exists()
            # Even with confirmation present the real script must wait for the
            # original process handle, not merely for a commit-file appearance.
            time.sleep(0.6)
            assert parent.poll() is None and actual_parent.exit_code() is None and helper.exit_code() is None
            assert not installer_receipt.exists() and not application_receipt.exists()
            assert json.loads(status_file.read_text(encoding="utf-8-sig"))["phase"] == "waiting-parent"
            Path(config["release"]).write_text("normal exit allowed", encoding="utf-8")
            assert parent.wait(timeout=10) == 0
            def complete():
                if status_file.exists():
                    try:
                        state = json.loads(status_file.read_text(encoding="utf-8-sig"))
                    except (OSError, ValueError):
                        return False
                    if state["phase"] == "failed":
                        raise RuntimeError("Fixture helper reported failure: " + state["message"])
                return helper.exit_code() is not None
            wait_until(complete)
            state = json.loads(status_file.read_text(encoding="utf-8-sig"))
            assert helper.exit_code() == 0 and state["phase"] == "installed"
            wait_until(application_receipt.exists)
            install_call = json.loads(installer_receipt.read_text(encoding="utf-8-sig"))
            app_call = json.loads(application_receipt.read_text(encoding="utf-8-sig"))
            # The inert app returns immediately after publishing its receipt.
            # Do not open/terminate its reported PID, which may already be gone.
            expected_installer_arguments = ["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOAUTORUN",
                                            "/DATADIR=" + str(data), "/DIR=" + str(program),
                                            "/LOG=" + ticket["install_log"]]
            assert install_call["args"] == expected_installer_arguments, install_call["args"]
            assert app_call["args"] == ["--data-dir", str(data)], app_call["args"]
            assert sentinel.read_bytes() == sentinel_bytes
            assert hashlib.sha256(installer.read_bytes()).hexdigest() == digest
            return {"success": True, "parent_exit_code": 0, "helper_exit_code": 0, "phase": state["phase"],
                    "confirmed_but_live_parent_did_not_execute": True,
                    "original_data_argument_preserved": True, "original_install_argument_preserved": True,
                    "installer_arguments": install_call["args"], "application_arguments": app_call["args"],
                    "no_autorun_and_no_restart": True, "restarted_application_with_data_dir": True,
                    "fixture_user_bytes_preserved": True, "installer_sha256_verified": digest,
                    "installer_bytes": installer.stat().st_size, "fixture_root": str(case)}
        finally:
            if helper is not None:
                helper.close()
            if actual_parent is not None:
                actual_parent.close()
            Path(config["release"]).write_text("fixture controller cleanup", encoding="utf-8")
            if parent.poll() is None:
                try:
                    parent.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    parent.terminate()
                    parent.wait(timeout=10)



CSHARP_FIXTURE = 'using System;\nusing System.Collections.Generic;\nusing System.Diagnostics;\nusing System.IO;\nusing System.Reflection;\nusing System.Text;\nusing System.Web.Script.Serialization;\npublic static class InertFixture {\n  [STAThread] public static int Main(string[] args) {\n    string executable = Assembly.GetExecutingAssembly().Location;\n    string role = Path.GetFileName(executable).StartsWith("Lab-Control-Center-") ? "installer" : "application";\n    string receipt = Path.Combine(Path.GetDirectoryName(executable), role + "-arguments.json");\n    var value = new Dictionary<string, object>();\n    value["kind"] = role; value["args"] = args; value["executable"] = executable;\n    value["pid"] = Process.GetCurrentProcess().Id; value["utc"] = DateTime.UtcNow.ToString("o");\n    File.WriteAllText(receipt + ".tmp", new JavaScriptSerializer().Serialize(value), new UTF8Encoding(false));\n    File.Move(receipt + ".tmp", receipt);\n    return 0;\n  }\n}\n'


@unittest.skipUnless(os.name == "nt", "Windows PowerShell 5 acceptance requires Windows")
class NativeInstallerHandoffTests(unittest.TestCase):
    def test_committed_handoff_waits_for_parent_and_preserves_literal_arguments(self):
        framework = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319"
        compiler, reference = framework / "csc.exe", framework / "System.Web.Extensions.dll"
        if not compiler.is_file() or not reference.is_file():
            self.skipTest("Windows .NET Framework fixture compiler is unavailable")
        with tempfile.TemporaryDirectory(prefix="handoff 中文 O'Brien & {fixture}-") as temporary:
            fixture = Path(temporary).resolve()
            code = fixture / "InertFixture.cs"
            code.write_text(CSHARP_FIXTURE, encoding="utf-8")
            executable = fixture / "InertFixture.exe"
            subprocess.run([str(compiler), "/nologo", "/target:winexe", "/optimize+",
                            "/reference:" + str(reference), "/out:" + str(executable), str(code)],
                           cwd=fixture, check=True, creationflags=CREATE_NO_WINDOW,
                           capture_output=True, text=True)
            result = run_case(fixture, executable)
            self.assertTrue(result["success"])
            self.assertTrue(result["confirmed_but_live_parent_did_not_execute"])
            self.assertTrue(result["original_data_argument_preserved"])
            self.assertTrue(result["original_install_argument_preserved"])
            self.assertTrue(result["no_autorun_and_no_restart"])
            self.assertTrue(result["restarted_application_with_data_dir"])
            self.assertTrue(result["fixture_user_bytes_preserved"])
            self.assertEqual(result["helper_exit_code"], 0)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--dummy-parent":
        raise SystemExit(child_parent(sys.argv[2]))
    unittest.main()
