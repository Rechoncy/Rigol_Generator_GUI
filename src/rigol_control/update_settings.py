"""Update preferences stored separately from instrument configuration."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
import tempfile


@dataclass(frozen=True)
class UpdatePreferences:
    automatic_check: bool = True
    automatic_download: bool = True
    automatic_install: bool = True


def load_update_preferences(data_dir: Path) -> tuple[UpdatePreferences, str]:
    path = data_dir / "update_settings.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("文件过大")
        settings = json.loads(raw)
        if (not isinstance(settings, dict) or settings.get("schema_version") != 1
                or not isinstance(settings.get("automatic_check"), bool)
                or not isinstance(settings.get("automatic_download", True), bool)
                or not isinstance(settings.get("automatic_install", True), bool)):
            raise ValueError("格式不受支持")
        return UpdatePreferences(settings["automatic_check"], settings.get("automatic_download", True),
                                 settings.get("automatic_install", True)), ""
    except FileNotFoundError:
        return UpdatePreferences(), ""
    except (OSError, ValueError, RecursionError) as exc:
        # Do not silently re-enable networking when an existing setting is unreadable.
        return UpdatePreferences(False, False, False), f"更新设置读取失败，自动检查已暂停：{exc}"


def save_update_preferences(data_dir: Path, settings: UpdatePreferences) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir,
                                         prefix="update_settings-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema_version": 1, "automatic_check": settings.automatic_check,
                       "automatic_download": settings.automatic_download,
                       "automatic_install": settings.automatic_install}, stream,
                      ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(data_dir / "update_settings.json")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def load_update_preference(data_dir: Path) -> tuple[bool, str]:
    settings, error = load_update_preferences(data_dir)
    return settings.automatic_check, error


def save_update_preference(data_dir: Path, enabled: bool) -> None:
    settings, _ = load_update_preferences(data_dir)
    save_update_preferences(data_dir, replace(settings, automatic_check=bool(enabled)))
