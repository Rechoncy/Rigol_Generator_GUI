from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from .models import DeviceRecord


class DuplicateDeviceError(ValueError):
    pass


def default_data_dir() -> Path:
    override = os.environ.get("RIGOL_GUI_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    app_data = os.environ.get("APPDATA")
    base = Path(app_data) if app_data else Path.home() / ".config"
    return base / "RigolGeneratorControl"


class DeviceRepository:
    SCHEMA_VERSION = 1

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.path = self.data_dir / "devices.json"

    def list(self) -> list[DeviceRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取设备配置：{exc}") from exc
        if data.get("schema_version") != self.SCHEMA_VERSION:
            raise RuntimeError("设备配置版本不受支持")
        return [DeviceRecord.from_dict(item) for item in data.get("devices", [])]

    def get(self, device_id: str) -> DeviceRecord | None:
        return next((item for item in self.list() if item.id == device_id), None)

    def save(self, record: DeviceRecord) -> DeviceRecord:
        record.validate()
        devices = self.list()
        self._ensure_unique(record, devices)
        for index, existing in enumerate(devices):
            if existing.id == record.id:
                devices[index] = record
                break
        else:
            devices.append(record)
        self._write(devices)
        return record

    def delete(self, device_id: str) -> bool:
        devices = self.list()
        retained = [item for item in devices if item.id != device_id]
        if len(retained) == len(devices):
            return False
        self._write(retained)
        return True

    @staticmethod
    def _ensure_unique(record: DeviceRecord, devices: Iterable[DeviceRecord]) -> None:
        key = (record.kind.value.casefold(), record.resource.strip().casefold())
        for existing in devices:
            existing_key = (
                existing.kind.value.casefold(),
                existing.resource.strip().casefold(),
            )
            if existing.id != record.id and existing_key == key:
                raise DuplicateDeviceError("该类型和资源地址的设备已经存在")

    def _write(self, devices: Iterable[DeviceRecord]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "devices": [item.to_dict() for item in devices],
        }
        temporary = self.path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeError(f"无法保存设备配置：{exc}") from exc

