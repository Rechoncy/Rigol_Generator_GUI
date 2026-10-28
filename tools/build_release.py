"""Build, relocate and verify Windows bundles without copying local configuration."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dist/release"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_vaunix_dll(requested: Path | None, root: Path = ROOT) -> Path | None:
    """Prefer an explicit SDK path, otherwise include the DLL in the project root."""
    path = (requested if requested is not None else root / "vnx_fmsynth.dll").resolve()
    if not path.is_file():
        if requested is not None:
            raise FileNotFoundError(path)
        return None
    data = path.read_bytes()
    try:
        offset = struct.unpack_from("<I", data, 0x3c)[0]
        if data[:2] != b"MZ" or data[offset:offset + 4] != b"PE\0\0":
            raise ValueError("missing PE header")
        machine = struct.unpack_from("<H", data, offset + 4)[0]
        characteristics = struct.unpack_from("<H", data, offset + 22)[0]
        magic = struct.unpack_from("<H", data, offset + 24)[0]
        if machine != 0x8664 or magic != 0x20b or not characteristics & 0x2000:
            raise ValueError("not an AMD64 PE32+ DLL")
    except (struct.error, ValueError) as exc:
        raise RuntimeError(f"Vaunix library must be a Windows x64 DLL: {path}") from exc
    return path


def source_revision() -> tuple[str | None, bool | None]:
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip())
        return revision, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, None  # Source ZIPs deliberately omit .git and remain buildable.


def source_files(root: Path):
    """Explicit allow-list: never walk the user's data, SDKs, venv or Git state."""
    for name in ("README.md", "pyproject.toml"):
        yield root / name
    for directory in ("src", "tests", "tools", "docs", "release"):
        for path in sorted((root / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.suffix.lower() in {".py", ".md", ".toml", ".txt", ".ps1", ".iss", ".spec", ".cs", ".cmd", ".svg", ".ico", ".png"}:
                yield path


def write_source_archive(destination: Path):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in source_files(ROOT):
            archive.write(path, path.relative_to(ROOT).as_posix())


def collect_notices(destination: Path) -> dict[str, str]:
    destination.mkdir(parents=True)
    versions = {}
    notices = ["Third-party components included in this build.",
               "See individual license files and package metadata; project licensing is not changed by this inventory.", ""]
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
        name = distribution.metadata.get("Name", "unknown")
        if name.lower() in {"pip", "rigol-generator-gui"}:
            continue
        versions[name] = distribution.version
        notices.append(f"{name} {distribution.version}: {distribution.metadata.get('License-Expression') or distribution.metadata.get('License') or 'see metadata'}")
        package_dir = destination / name
        package_dir.mkdir(exist_ok=True)
        (package_dir / "METADATA.txt").write_text(distribution.read_text("METADATA") or "", encoding="utf-8")
        for item in distribution.files or ():
            if not any(token in item.name.lower() for token in ("license", "copying", "copyright", "notice")):
                continue
            source = Path(distribution.locate_file(item))
            if not source.is_file():
                continue
            # Flatten distribution-relative paths and prohibit parent-directory traversal.
            target = package_dir / "__".join(part for part in item.parts if part not in ("..", "."))
            shutil.copy2(source, target)
    python_license = next((p for p in (Path(sys.base_prefix) / "LICENSE.txt", Path(sys.base_prefix) / "LICENSE_PYTHON.txt") if p.is_file()), None)
    if python_license is None:
        raise RuntimeError("Python license file not found in build runtime")
    shutil.copy2(python_license, destination / "PYTHON-LICENSE.txt")
    (destination / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(notices), encoding="utf-8")
    return versions


def isolated_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in tuple(env):
        if key.upper().startswith(("PYTHON", "CONDA", "VIRTUAL_ENV", "QT_", "PYI_")) or key in {"LAB_CONTROL_PYTHON", "RIGOL_GUI_DATA_DIR", "VAUNIX_LMS_DLL"}:
            env.pop(key, None)
    # System DLLs are available, but no external Python or Conda executables are discoverable.
    system = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join(map(str, (system / "System32", system)))
    # Poisoned settings prove frozen execution ignores externally configured Python.
    env["PYTHONHOME"] = r"Z:\missing-external-python"
    env["PYTHONPATH"] = r"Z:\missing-site-packages"
    env["LAB_CONTROL_PYTHON"] = r"Z:\missing-python.exe"
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def run_probe(executable: Path, report: Path, cwd: Path) -> dict:
    subprocess.run([str(executable), "--bundle-self-test", str(report)], cwd=cwd,
                   env=isolated_environment(), check=True, timeout=90,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    result = json.loads(report.read_text(encoding="utf-8"))
    if not result.get("success") or not result.get("frozen"):
        raise RuntimeError(f"Packaged self-test failed: {report}")
    return result


def bundle_zip(folder: Path, destination: Path):
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder.parent).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vaunix-dll", type=Path,
                        help="Override the Windows x64 vendor DLL (default: project-root vnx_fmsynth.dll if present)")
    args = parser.parse_args()
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise RuntimeError("Build on 64-bit Windows using 64-bit Python")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Use tools/build_release.ps1 to build in the isolated release venv")
    sys.path.insert(0, str(ROOT / "src"))
    from rigol_control import __version__
    version = __version__
    slug = f"Lab-Control-Center-{version}-windows-x64"
    dll = resolve_vaunix_dll(args.vaunix_dll)
    (ROOT / "build").mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="release-", dir=ROOT / "build"))
    support = stage / "support"
    support.mkdir()
    shutil.copytree(ROOT / "docs", support / "docs", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "release/README.md", support / "README.md")
    shutil.copy2(ROOT / "release" / f"NOTES-{version}.md", support / "RELEASE-NOTES.md")
    dependencies = collect_notices(support / "licenses")
    write_source_archive(support / "source.zip")
    revision, dirty = source_revision()
    info = {"version": version, "python": platform.python_version(), "platform": "windows-x64",
            "source_commit": revision, "source_dirty": dirty, "source_archive_sha256": sha256(support / "source.zip"),
            "dependencies": dependencies, "vaunix_dll_bundled": bool(dll),
            "built_at_utc": datetime.now(timezone.utc).isoformat()}
    if dll:
        info["vaunix_dll"] = {"file": "vnx_fmsynth.dll", "bundle_path": "vendor/vaunix/vnx_fmsynth.dll",
                              "architecture": "windows-x64", "bytes": dll.stat().st_size, "sha256": sha256(dll)}
    (support / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    env = dict(os.environ, LAB_RELEASE_SUPPORT=str(support), LAB_RELEASE_SLUG=slug)
    # PyInstaller also searches PATH for dependent DLLs. Do not collect a foreign
    # ICU/Qt/OpenSSL from Conda, Poppler or another application's runtime.
    system = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join(map(str, (Path(sys.executable).parent, Path(sys.base_prefix), system / "System32", system)))
    for key in tuple(env):
        if key.upper().startswith(("PYTHON", "CONDA", "QT_")):
            env.pop(key, None)
    env.pop("LAB_RELEASE_VAUNIX_DLL", None)
    if dll:
        env["LAB_RELEASE_VAUNIX_DLL"] = str(dll)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--distpath", str(stage / "binaries"), "--workpath", str(stage / "work"),
                    str(ROOT / "tools/lab_control.spec")], cwd=ROOT, env=env, check=True)
    # Relocate into a path with Chinese, spaces and an apostrophe before validation.
    relocated = stage / "迁移验证 O'Brien & portable"
    relocated.mkdir()
    portable = relocated / (slug + "-portable")
    shutil.move(str(stage / "binaries/Lab Control Center"), portable)
    standalone = relocated / (slug + ".exe")
    shutil.move(str(stage / "binaries" / standalone.name), standalone)
    for name in ("README.md", "RELEASE-NOTES.md", "build-info.json"):
        shutil.copy2(support / name, portable / name)
    # Keep the guide and notices accessible without opening a temporary extraction directory.
    shutil.copytree(support / "docs", portable / "docs")
    shutil.copytree(support / "licenses", portable / "licenses")
    shutil.copy2(support / "source.zip", portable / "source.zip")
    (portable / "release").mkdir()
    shutil.copy2(ROOT / "release/README.md", portable / "release/README.md")
    shutil.copy2(ROOT / "release" / f"NOTES-{version}.md", portable / "release" / f"NOTES-{version}.md")
    shutil.copy2(support / "RELEASE-NOTES.md", portable / f"NOTES-{version}.md")
    reports = stage / "verification"
    reports.mkdir()
    first = run_probe(portable / "Lab Control Center.exe", reports / "portable.json", stage)
    second = run_probe(standalone, reports / "standalone.json", stage)
    # Exercise the ordinary GUI startup, independently of the diagnostic entry point.
    startup_env = isolated_environment()
    startup_env.pop("QT_QPA_PLATFORM", None)  # Load the actual Windows platform plugin.
    for exe, name in ((portable / "Lab Control Center.exe", "directory"), (standalone, "standalone")):
        subprocess.run([str(exe), "--smoke-test", "--data-dir", str(reports / (name + "-data"))],
                       env=startup_env, cwd=stage, check=True, timeout=90,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    zip_path = OUTPUT / (slug + "-portable.zip")
    bundle_zip(portable, zip_path)
    shutil.copy2(standalone, OUTPUT / standalone.name)
    shutil.copy2(support / "source.zip", OUTPUT / (slug + "-source.zip"))
    shutil.copy2(support / "RELEASE-NOTES.md", OUTPUT / "RELEASE-NOTES.md")
    # Verify the distributable archive itself, rather than only its pre-zip directory.
    extracted = stage / "zip-roundtrip"
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extracted)
    third = run_probe(extracted / portable.name / "Lab Control Center.exe", reports / "zip.json", stage)
    # Installed and portable editions share these exact verified application bytes.
    sys.path.insert(0, str(ROOT / "tools"))
    from build_installer import build_installer
    installer = build_installer(portable, version, OUTPUT)
    from verify_installer import verify_installer
    installer_acceptance = verify_installer(portable, version, stage / "installer-acceptance")
    if not installer_acceptance.get("success"):
        raise RuntimeError("Installer install/upgrade/data-preservation acceptance failed")
    info["installer"] = {"file": installer.name, "bytes": installer.stat().st_size,
                         "sha256": sha256(installer), "per_user": True,
                         "data_separate_from_program": True, "automatic_old_version_uninstall": True}
    info["verification"] = {"directory": first, "standalone": second, "zip_roundtrip": third,
                            "normal_startup_passed": True, "normal_startup_platform": "windows", "external_python_on_path": False,
                            "physical_hardware_tested": False, "installer": installer_acceptance}
    artifacts = [zip_path, OUTPUT / standalone.name, installer, OUTPUT / (slug + "-source.zip"), OUTPUT / "RELEASE-NOTES.md"]
    info["artifacts"] = [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in artifacts]
    manifest = OUTPUT / "release-manifest.json"
    manifest.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts.append(manifest)
    (OUTPUT / "SHA256SUMS.txt").write_text("".join(f"{sha256(p)}  {p.name}\n" for p in artifacts), encoding="ascii")
    print(json.dumps({"output": str(OUTPUT), "artifacts": info["artifacts"], "verification": "passed"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
