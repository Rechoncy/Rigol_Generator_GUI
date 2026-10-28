"""Update preferences stored separately from instrument configuration."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile


def load_update_preference(data_dir: Path) -> tuple[bool, str]:
    path = data_dir / "update_settings.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("文件过大")
        settings = json.loads(raw)
        if (not isinstance(settings, dict) or settings.get("schema_version") != 1
                or not isinstance(settings.get("automatic_check"), bool)):
            raise ValueError("格式不受支持")
        return settings["automatic_check"], ""
    except FileNotFoundError:
        return True, ""
    except (OSError, ValueError) as exc:
        # Do not silently re-enable networking when an existing setting is unreadable.
        return False, f"更新设置读取失败，自动检查已暂停：{exc}"


def save_update_preference(data_dir: Path, enabled: bool) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=data_dir,
                                         prefix="update_settings-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema_version": 1, "automatic_check": bool(enabled)}, stream,
                      ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(data_dir / "update_settings.json")
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
