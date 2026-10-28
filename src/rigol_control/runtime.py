"""Paths and startup checks shared by source and self-contained releases."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from .installation import installed_info, is_installed


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """Return the real launcher directory, never the one-file extraction folder."""
    return Path(sys.executable).resolve().parent


def bundle_root() -> Path:
    """Return bundled read-only resources, or the source checkout root."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", executable_dir())).resolve()
    return Path(__file__).resolve().parents[2]


def package_resource(*parts: str) -> Path:
    """PyInstaller preserves package-relative __file__ paths for assets."""
    return Path(__file__).resolve().parent.joinpath(*parts)


def resolve_data_dir(data_dir: Path | str | None = None) -> Path:
    """Explicit path > environment > portable EXE sibling > source default."""
    if data_dir is not None:
        return Path(data_dir).expanduser().resolve()
    override = os.environ.get("RIGOL_GUI_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    installation = installed_info()
    if installation is not None:
        return Path(installation["data_dir"]).resolve()
    if is_frozen():
        return executable_dir() / "data"
    app_data = os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".config"
    return base / "RigolGeneratorControl"


def prepare_data_dir(data_dir: Path | str | None = None) -> Path:
    """Fail visibly before opening the UI if configuration cannot be saved."""
    path = resolve_data_dir(data_dir)
    try:
        path.mkdir(parents=True, exist_ok=True)
        # Actual file creation catches ACL/read-only-volume failures that
        # os.access() cannot reliably predict on Windows.
        with tempfile.TemporaryFile(prefix=".write-check-", dir=path) as stream:
            stream.write(b"rigol-control")
            stream.flush()
    except OSError as exc:
        raise RuntimeError(
            f"数据目录不可写：{path}\n"
            "请将软件移到有写入权限的目录，或使用 --data-dir / "
            "RIGOL_GUI_DATA_DIR 指定数据目录。软件未切换到其他目录。\n"
            f"原因：{exc}"
        ) from exc
    return path


def prepare_qt_environment() -> None:
    """Prevent another Python/Qt install from supplying incompatible plugins."""
    if not is_frozen():
        return
    root = bundle_root()
    for name in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        value = os.environ.get(name)
        if value is None:
            continue
        bundled = []
        for entry in value.split(os.pathsep):
            if not entry:
                continue
            try:
                candidate = Path(entry).resolve()
                candidate.relative_to(root)
            except (OSError, ValueError):
                continue
            bundled.append(str(candidate))
        if bundled:
            os.environ[name] = os.pathsep.join(bundled)
        else:
            os.environ.pop(name, None)
