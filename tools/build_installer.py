"""Build a per-user Windows installer around an already verified portable tree."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from uuid import UUID, uuid5


ROOT = Path(__file__).resolve().parents[1]
APP_ID = "D9D0E8E6-83E5-46F4-B5B1-9B3D7EE9C523"
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", re.ASCII)
TEST_ID = re.compile(r"[A-Za-z0-9_-]{1,40}", re.ASCII)


def require_x64_executable(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(64)
            if header[:2] != b"MZ":
                raise ValueError("missing MZ header")
            offset = struct.unpack_from("<I", header, 0x3C)[0]
            if offset > 16 * 1024 * 1024:
                raise ValueError("invalid PE offset")
            stream.seek(offset)
            pe = stream.read(26)
        if (pe[:4] != b"PE\0\0" or struct.unpack_from("<H", pe, 4)[0] != 0x8664
                or struct.unpack_from("<H", pe, 24)[0] != 0x20B):
            raise ValueError("not AMD64 PE32+")
    except (OSError, ValueError, struct.error) as exc:
        raise RuntimeError(f"A Windows x64 portable executable is required: {path}") from exc


def validate_payload(folder: Path, version: str) -> Path:
    if VERSION.fullmatch(version) is None:
        raise ValueError("Installer version must be a stable x.y.z version")
    folder = folder.expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise RuntimeError("Portable payload must be a directory")
    require_x64_executable(folder / "Lab Control Center.exe")
    for required in ("_internal/base_library.zip", "_internal/python3.dll", "build-info.json", "source.zip"):
        if not (folder / required).is_file():
            raise RuntimeError(f"Portable runtime file is missing: {required}")
    if not any((folder / "_internal").glob("python3[0-9]*.dll")):
        raise RuntimeError("Embedded Python runtime is missing")
    try:
        info = json.loads((folder / "build-info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Portable build metadata is invalid") from exc
    if info.get("version") != version or info.get("platform") != "windows-x64":
        raise RuntimeError("Portable build version/platform do not match the installer")
    # The compiler receives an explicit clean tree. Never silently package a
    # developer's data directory, previous installation or reparse point.
    prohibited = {"data", "runtime-data", "logs", "updates", "installed-mode.json"}
    for path in folder.rglob("*"):
        relative = path.relative_to(folder)
        if relative.parts[0].casefold() in prohibited or relative.parts[0].casefold().startswith("unins"):
            raise RuntimeError(f"Portable payload contains installed/user files: {relative}")
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise RuntimeError(f"Portable payload contains a reparse point: {relative}")
    return folder


def find_iscc(requested: Path | None = None) -> Path:
    if requested is not None:
        candidates = [requested]
    else:
        candidates = []
        if os.environ.get("INNO_SETUP_ISCC"):
            candidates.append(Path(os.environ["INNO_SETUP_ISCC"]))
        candidates.append(ROOT / "build/tools/inno/ISCC.exe")
        available = shutil.which("ISCC.exe")
        if available:
            candidates.append(Path(available))
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if base:
                for name in ("Inno Setup 7", "Inno Setup 6"):
                    candidates.append(Path(base) / name / "ISCC.exe")
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
    raise RuntimeError("Inno Setup compiler not found; pass --iscc or set INNO_SETUP_ISCC")


def build_installer(portable_dir: Path, version: str, output: Path, *, iscc: Path | None = None,
                    test_id: str | None = None) -> Path:
    payload = validate_payload(portable_dir, version)
    compiler = find_iscc(iscc)
    if test_id is not None and TEST_ID.fullmatch(test_id) is None:
        raise ValueError("Test installer ID must be 1-40 ASCII letters/digits/underscore/hyphen")
    suffix = f"-Test-{test_id}" if test_id else ""
    app_id = str(uuid5(UUID(APP_ID), test_id)).upper() if test_id else APP_ID
    name = f"Lab-Control-Center-{version}-windows-x64-setup" + (f"-test-{test_id}" if test_id else "")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / (name + ".exe")
    if target.exists():
        raise FileExistsError(f"Refusing to overwrite an existing installer: {target}")
    # Let ISCC write only into a fresh stage, then publish without replacement.
    with tempfile.TemporaryDirectory(prefix="installer-", dir=output.parent) as temporary:
        stage = Path(temporary)
        arguments = [str(compiler), "/Qp", f"/DAppVersion={version}", f"/DPayloadDir={payload}",
                     f"/DOutputDir={stage}", f"/DOutputName={name}", f"/DInstallerId={app_id}",
                     f"/DInstanceSuffix={suffix}", str(ROOT / "release/installer.iss")]
        subprocess.run(arguments, cwd=ROOT, check=True)
        built = stage / (name + ".exe")
        with built.open("rb") as stream:
            magic = stream.read(2)
        if magic != b"MZ":
            raise RuntimeError("Inno Setup did not produce a Windows installer")
        if os.name == "nt":
            os.rename(built, target)
        else:
            os.link(built, target)
            built.unlink()
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable-dir", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--test-id", help="Isolated acceptance-test AppId/mutex/shortcut suffix; never publish")
    args = parser.parse_args()
    target = build_installer(args.portable_dir, args.version, args.output, iscc=args.iscc, test_id=args.test_id)
    print(json.dumps({"installer": str(target), "version": args.version,
                      "isolated_test": args.test_id is not None}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
