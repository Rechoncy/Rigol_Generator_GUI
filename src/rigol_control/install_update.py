"""Prepare a verified installer and hand off only after a clean application exit."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from . import __version__
from .installation import installed_info
from .runtime import package_resource
from .updates import ReleaseAsset, _version_parts
from .updates_download import _validate_asset


@dataclass(frozen=True)
class InstallHandoff:
    directory: Path
    ticket: Path
    commit_file: Path
    cancel_file: Path
    status_file: Path
    nonce: str


def _atomic_text(path: Path, text: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".update-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_installer(path: Path, asset: ReleaseAsset, data_dir: Path) -> Path:
    _validate_asset(asset)
    match = re.fullmatch(r"Lab-Control-Center-(\d+\.\d+\.\d+)-windows-x64-setup\.exe", asset.name)
    if match is None or _version_parts(match[1]) <= _version_parts(__version__):
        raise RuntimeError("只能安装经过校验且版本更高的本项目安装包")
    data = data_dir.resolve()
    expected = data / "updates" / ("v" + match[1]) / asset.name
    if path.is_symlink() or path.resolve() != expected:
        raise RuntimeError("安装包路径与已下载的版本不匹配")
    for parent in path.parents:
        if parent == data:
            break
        if parent.is_symlink() or getattr(parent, "is_junction", lambda: False)():
            raise RuntimeError("更新目录不能通过符号链接重定向")
    if not path.is_file() or path.stat().st_size != asset.size:
        raise RuntimeError("安装包不存在或大小已变化，请重新下载")
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("下载内容不是 Windows 安装程序")
        stream.seek(0)
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(256 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != asset.sha256.lower():
            raise RuntimeError("安装包 SHA-256 已变化，请重新下载")
    return path.resolve()


def prepare_handoff(path: Path, asset: ReleaseAsset, data_dir: Path, *, parent_pid: int | None = None,
                    launcher=None, ready_timeout: float = 15.0) -> InstallHandoff:
    """Run in a worker; this never installs, closes windows or touches hardware."""
    info = installed_info()
    if info is None or os.name != "nt":
        raise RuntimeError("自动安装仅用于 Windows 安装版，请运行下载的安装程序")
    verified = verify_installer(path, asset, data_dir)
    install_dir = Path(sys.executable).resolve().parent
    data = data_dir.resolve()
    if data == install_dir or data.is_relative_to(install_dir):
        raise RuntimeError("升级前请把用户数据移到程序目录之外")
    directory = data / "updates" / ("handoff-" + uuid4().hex)
    directory.mkdir(parents=True)
    nonce = uuid4().hex
    handoff = InstallHandoff(directory, directory / "ticket.json", directory / "commit.txt",
                             directory / "cancel.txt", directory / "status.json", nonce)
    script = directory / "apply-update.ps1"
    # Windows PowerShell 5 interprets scripts without a BOM using the system
    # legacy code page. Its parser can mistake CJK bytes for quote characters.
    _atomic_text(script, "\ufeff" + package_resource("assets", "apply_update.ps1").read_text(encoding="utf-8-sig"))
    payload = {"format": "lab-control-center-install-handoff", "schema_version": 1,
               "parent_pid": parent_pid if parent_pid is not None else os.getpid(),
               "nonce": nonce, "installer": str(verified), "sha256": asset.sha256.lower(),
               "bytes": asset.size, "data_dir": str(data), "install_dir": str(install_dir),
               "application": str(install_dir / "Lab Control Center.exe"), "commit_file": str(handoff.commit_file),
               "cancel_file": str(handoff.cancel_file), "status_file": str(handoff.status_file),
               "install_log": str(directory / "install.log")}
    _atomic_text(handoff.ticket, json.dumps(payload, ensure_ascii=False, indent=2))
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-WindowStyle", "Hidden", "-File", str(script), "-Ticket", str(handoff.ticket)]
    try:
        process = (launcher or subprocess.Popen)(command, cwd=str(directory),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if handoff.status_file.exists():
                try:
                    state = json.loads(handoff.status_file.read_text(encoding="utf-8-sig"))
                    if state.get("phase") == "waiting-parent":
                        return handoff
                    if state.get("phase") == "failed":
                        raise RuntimeError("升级等待程序启动失败，请查看下载目录日志")
                except (OSError, ValueError):
                    pass
            if process.poll() is not None:
                raise RuntimeError("升级等待程序未就绪，请稍后重试")
            time.sleep(0.05)
        raise RuntimeError("升级等待程序未在期限内就绪，请稍后重试")
    except BaseException:
        cancel_handoff(handoff)
        raise


def commit_handoff(handoff: InstallHandoff) -> None:
    _atomic_text(handoff.commit_file, handoff.nonce + "\n")


def cancel_handoff(handoff: InstallHandoff) -> None:
    _atomic_text(handoff.cancel_file, "cancelled\n")
