from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .models import DeviceKind, DeviceRecord
from .runtime import resolve_data_dir


class DuplicateDeviceError(ValueError):
    pass


def default_data_dir() -> Path:
    return resolve_data_dir()


class DeviceRepository:
    SCHEMA_VERSION = 1

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        from .config_archive import recover_pending_import
        recover_pending_import(self.data_dir)
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
        previous = next((item for item in devices if item.id == record.id), None)
        # Older files may already contain two SCPI profiles for one resource.
        # Keep them readable/editable so users can remove the obsolete entry;
        # only adding a record or changing its address/type introduces a duplicate.
        if (previous is None or previous.kind is not record.kind
                or previous.resource.strip().casefold() != record.resource.strip().casefold()):
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
        scpi_kinds = {DeviceKind.DG4000, DeviceKind.SIGLENT_SSG, DeviceKind.GENERIC_SCPI}
        def resource_key(item: DeviceRecord) -> tuple[str, str]:
            kind = "scpi" if item.kind in scpi_kinds else item.kind.value.casefold()
            return kind, item.resource.strip().casefold()
        key = resource_key(record)
        for existing in devices:
            existing_key = resource_key(existing)
            if existing.id != record.id and existing_key == key:
                message = ("该物理资源地址的设备已经存在，不能通过不同 SCPI 类型重复添加"
                           if record.kind in scpi_kinds else "该类型和资源地址的设备已经存在")
                raise DuplicateDeviceError(message)

    def _write(self, devices: Iterable[DeviceRecord]) -> None:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "devices": [item.to_dict() for item in devices],
        }
        temporary = self.path.with_suffix(".json.tmp")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeError(f"无法保存设备配置：{exc}") from exc


class DeviceParameterRepository:
    """Store UI parameter snapshots independently for every stable device ID."""

    SCHEMA_VERSION = 1

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = resolve_data_dir(data_dir)
        self.path = self.data_dir / "device_parameters.json"

    def load(self, device_id: str, *, kind: str | None = None) -> dict[str, Any]:
        devices = self._read()
        entry = devices.get(device_id)
        if not isinstance(entry, dict):
            return {}
        if kind is not None and entry.get("kind") not in {None, kind}:
            return {}
        parameters = entry.get("parameters", {})
        return dict(parameters) if isinstance(parameters, dict) else {}

    def save(
        self,
        device_id: str,
        *,
        kind: str,
        parameters: dict[str, Any],
    ) -> None:
        if not device_id.strip():
            raise ValueError("设备 ID 不能为空")
        devices = self._read()
        devices[device_id] = {
            "kind": kind,
            "parameters": parameters,
        }
        self._write(devices)

    def delete(self, device_id: str) -> bool:
        devices = self._read()
        if device_id not in devices:
            return False
        del devices[device_id]
        self._write(devices)
        return True

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取设备参数：{exc}") from exc
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise RuntimeError("设备参数配置版本不受支持")
        devices = payload.get("devices", {})
        if not isinstance(devices, dict):
            raise RuntimeError("设备参数配置格式无效")
        return devices

    def _write(self, devices: dict[str, Any]) -> None:
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "devices": devices,
        }
        temporary = self.path.with_suffix(".json.tmp")
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(f"无法保存设备参数：{exc}") from exc
