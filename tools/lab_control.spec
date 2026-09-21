"""One analysis produces both a portable directory and a standalone executable."""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
support = Path(os.environ["LAB_RELEASE_SUPPORT"])
slug = os.environ["LAB_RELEASE_SLUG"]
datas = [(str(root / "src/rigol_control/assets"), "rigol_control/assets"),
         (str(support), "release")]
hiddenimports = collect_submodules("pyvisa") + collect_submodules("serial.tools")
hiddenimports += collect_submodules("nidaqmx")
datas += collect_data_files("nidaqmx")
for package in ("PyQt6", "PyVISA", "pyserial", "nidaqmx", "numpy", "rigol-generator-gui"):
    datas += copy_metadata(package, recursive=True)
binaries = []
if os.environ.get("LAB_RELEASE_VAUNIX_DLL"):
    binaries.append((os.environ["LAB_RELEASE_VAUNIX_DLL"], "vendor/vaunix"))

a = Analysis(
    [str(root / "tools/frozen_entry.py")],
    pathex=[str(root / "src"), str(root / "tools")],
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    excludes=["tkinter", "PyQt5", "PySide2", "PySide6", "pytest"],
    noarchive=False,
)
allowed_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
                 Path(os.environ["SystemRoot"]).resolve()]
allowed_vendor = Path(os.environ["LAB_RELEASE_VAUNIX_DLL"]).resolve() if os.environ.get("LAB_RELEASE_VAUNIX_DLL") else None
for _, source, _ in a.binaries:
    path = Path(source).resolve()
    if path != allowed_vendor and not any(path.is_relative_to(parent) for parent in allowed_roots):
        raise RuntimeError(f"Refusing an external machine dependency in release: {path}")
pyz = PYZ(a.pure)
common = dict(debug=False, strip=False, upx=False, console=False,
              icon=str(root / "src/rigol_control/assets/app_icon.ico"))
directory_exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
                    name="Lab Control Center", **common)
directory = COLLECT(directory_exe, a.binaries, a.datas, strip=False, upx=False,
                    name="Lab Control Center")
standalone = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=slug, **common)
