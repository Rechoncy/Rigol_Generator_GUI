"""Portable configuration bundles with validation and recoverable replacement.

Only fixed configuration filenames are restored. Measurements and runtime
hardware state are not configuration. Keep original JSON fields for migrations
performed by the respective UI/repositories (including legacy parameter keys).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import __version__
from .models import DeviceKind, DeviceRecord

FORMAT = "rigol-control-configuration"
SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024 * 1024
JOURNAL = ".config-import-pending.json"
DEFAULTS = {
    "devices.json": {"schema_version": 1, "devices": []},
    "device_parameters.json": {"schema_version": 1, "devices": {}},
    "step_tasks.json": {"schema_version": 1, "tasks": []},
    "linkage_tasks.json": {"schema_version": 1, "tasks": [], "active_task_id": None},
}


class ConfigArchiveError(RuntimeError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"重复 JSON 字段：{key}")
        result[key] = value
    return result


def _decode(raw: bytes, maximum_bytes=MAX_BYTES):
    if len(raw) > maximum_bytes:
        raise ConfigArchiveError("配置文件超过允许大小")
    def reject_constant(value):
        raise ValueError(f"不支持的数值：{value}")
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=reject_constant)


def _encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def _digest(value) -> str:
    return hashlib.sha256(_encode(value)).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@dataclass(frozen=True)
class ConfigBundle:
    payload: dict
    warnings: tuple[str, ...]

    @property
    def files(self) -> dict:
        return self.payload["files"]

    @property
    def connected_ids(self) -> tuple[str, ...]:
        return tuple(self.payload.get("connected_device_ids", []))

    @property
    def summary(self) -> str:
        files = self.files
        return (f"设备 {len(files['devices.json']['devices'])} 台；"
                f"设备参数 {len(files['device_parameters.json']['devices'])} 组；"
                f"单机任务 {len(files['step_tasks.json']['tasks'])} 个；"
                f"跨设备任务 {len(files['linkage_tasks.json']['tasks'])} 个。")


def _validate_files(files: dict) -> tuple[str, ...]:
    from .step_tasks import StepTaskRecord
    from .linkage_tasks import LinkageTaskRecord

    if not isinstance(files, dict) or set(files) != set(DEFAULTS):
        raise ValueError("配置包必须包含四个受支持的配置文件，不允许额外文件或路径")
    for name, payload in files.items():
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
            raise ValueError(f"{name} 版本不受支持，请使用兼容的软件版本")
    _encode(files)  # Reject NaN, Infinity, and numeric overflow anywhere, including UI values.
    raw_devices = files["devices.json"].get("devices")
    if not isinstance(raw_devices, list):
        raise ValueError("设备列表格式无效")
    devices = {}
    resources = set()
    for raw in raw_devices:
        if not isinstance(raw, dict) or any(not isinstance(raw.get(k), str) for k in ("id", "kind", "name", "resource")):
            raise ValueError("设备字段格式无效")
        record = DeviceRecord.from_dict(raw)
        key = (record.kind, record.resource.strip().casefold())
        if record.id in devices or key in resources:
            raise ValueError("设备 ID 或同类型资源地址重复")
        devices[record.id] = record
        resources.add(key)
    warnings = set()
    def reference(device_id, kind=None):
        record = devices.get(device_id)
        if record is None:
            warnings.add(f"保留缺失设备引用 {device_id}，相关任务需手动重新选择设备")
        elif kind is not None and record.kind not in kind:
            raise ValueError(f"设备 {record.name} 的类型与任务引用不匹配")
    parameters = files["device_parameters.json"].get("devices")
    if not isinstance(parameters, dict):
        raise ValueError("设备参数格式无效")
    for device_id, entry in parameters.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("parameters"), dict):
            raise ValueError(f"设备 {device_id} 的参数格式无效")
        reference(device_id)
        record = devices.get(device_id)
        if record and entry.get("kind") not in {None, record.kind.value}:
            raise ValueError(f"设备 {device_id} 的参数类型不匹配")
        snapshot = entry["parameters"]
        if "binding_version" in snapshot and snapshot["binding_version"] not in (1, 2):
            raise ValueError("设备参数绑定版本不受支持")
        if not isinstance(snapshot.get("controls", {}), dict) or not isinstance(snapshot.get("custom", {}), dict):
            raise ValueError("设备参数 controls/custom 格式无效")
        custom = snapshot.get("custom", {})
        ni_id = custom.get("ni_device_id")
        if ni_id:
            reference(ni_id, {DeviceKind.NI_DAQ})
    for filename, parser in (("step_tasks.json", StepTaskRecord), ("linkage_tasks.json", LinkageTaskRecord)):
        raw_tasks = files[filename].get("tasks")
        if not isinstance(raw_tasks, list):
            raise ValueError(f"{filename} 任务列表格式无效")
        ids = set()
        for raw in raw_tasks:
            task = parser.from_dict(raw)
            task.validate()
            if not task.id.strip() or task.id in ids:
                raise ValueError("任务 ID 为空或重复")
            ids.add(task.id)
            reference(task.ni_device_id, {DeviceKind.NI_DAQ})
            if filename == "step_tasks.json":
                reference(task.generator_device_id, {DeviceKind.DG4000})
            else:
                for axis in task.axes:
                    reference(axis.device_id, {DeviceKind.DG4000})
                for point in task.points:
                    for value in point.values:
                        reference(value.device_id, {DeviceKind.DG4000})
        active = files[filename].get("active_task_id")
        if active is not None and active not in ids:
            raise ValueError("当前跨设备任务 ID 不存在")
    return tuple(sorted(warnings))


def validate_bundle(payload: dict) -> ConfigBundle:
    try:
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise ValueError("不是本软件导出的完整配置包")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != SCHEMA_VERSION:
            raise ValueError("配置包版本不受支持，请使用兼容的软件版本")
        if len(_encode(payload)) > MAX_BYTES:
            raise ValueError("完整配置包超过 64 MB")
        files = payload.get("files")
        warnings = _validate_files(files)
        if payload.get("sha256") != {name: _digest(value) for name, value in files.items()}:
            raise ValueError("配置包校验失败，文件可能不完整或被修改")
        ids = payload.get("connected_device_ids", [])
        available = {raw["id"] for raw in files["devices.json"]["devices"]}
        if not isinstance(ids, list) or any(not isinstance(i, str) or i not in available for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("连接恢复列表包含无效设备 ID")
        return ConfigBundle(payload, warnings)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        raise ConfigArchiveError(f"配置校验失败：{exc}") from exc


def recover_pending_import(data_dir: str | Path) -> bool:
    """Roll back an interrupted import before any repository reads the files."""
    directory = Path(data_dir)
    journal = directory / JOURNAL
    if not journal.exists():
        return False
    try:
        saved = _decode(journal.read_bytes())
        name = saved["backup_name"]
        if (saved.get("schema_version") != 1 or not isinstance(name, str)
                or not name.startswith("import_") or Path(name).name != name
                or "/" in name or "\\" in name):
            raise ValueError("导入恢复日志格式无效")
        backup = directory / "config_backups" / name
        original = _decode((backup / "original.json").read_bytes(), MAX_BYTES * 6)
        if set(original) != set(DEFAULTS):
            raise ValueError("导入恢复文件不完整")
        decoded = {}
        for key, value in original.items():
            decoded[key] = None if value is None else base64.b64decode(value["base64"], validate=True)
            if value is not None and hashlib.sha256(decoded[key]).hexdigest() != value["sha256"]:
                raise ValueError("导入恢复文件校验失败")
        for key, value in decoded.items():
            target = directory / key
            if value is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, value)
        journal.unlink()
        return True
    except Exception as exc:
        raise ConfigArchiveError(f"未完成的配置导入无法自动回滚，请保留 {journal} 和 config_backups：{exc}") from exc


class ConfigArchiveService:
    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)

    def capture(self, connected_ids=()) -> ConfigBundle:
        recover_pending_import(self.data_dir)
        try:
            files = {name: (_decode((self.data_dir / name).read_bytes())
                            if (self.data_dir / name).exists() else _decode(_encode(default)))
                     for name, default in DEFAULTS.items()}
            payload = {"format": FORMAT, "schema_version": SCHEMA_VERSION,
                       "application_version": __version__, "exported_utc": datetime.now(timezone.utc).isoformat(),
                       "files": files, "sha256": {name: _digest(value) for name, value in files.items()},
                       "connected_device_ids": list(dict.fromkeys(connected_ids))}
            return validate_bundle(payload)
        except (OSError, ValueError, TypeError) as exc:
            raise ConfigArchiveError(f"无法读取当前配置：{exc}") from exc

    def export_file(self, path: str | Path, connected_ids=()) -> ConfigBundle:
        target = Path(path)
        if target.resolve() in {(self.data_dir / name).resolve() for name in (*DEFAULTS, JOURNAL)}:
            raise ConfigArchiveError("导出目标不能覆盖正在使用的配置文件")
        bundle = self.capture(connected_ids)
        _atomic_write(target, _encode(bundle.payload))
        return bundle

    def read_file(self, path: str | Path) -> ConfigBundle:
        try:
            source = Path(path)
            if source.stat().st_size > MAX_BYTES:
                raise ConfigArchiveError("配置包超过 64 MB")
            return validate_bundle(_decode(source.read_bytes()))
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise ConfigArchiveError(f"无法读取配置包：{exc}") from exc

    def import_bundle(self, bundle: ConfigBundle, *, previous_connected_ids=()) -> Path:
        bundle = validate_bundle(bundle.payload)  # Recheck even programmatically supplied bundles.
        recover_pending_import(self.data_dir)
        name = "import_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        backup = self.data_dir / "config_backups" / name
        backup.mkdir(parents=True, exist_ok=False)
        original = {}
        for filename in DEFAULTS:
            target = self.data_dir / filename
            raw = target.read_bytes() if target.exists() else None
            original[filename] = None if raw is None else {
                "base64": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest()}
        _atomic_write(backup / "original.json", _encode(original))
        # Offer a normal importable backup as well; raw rollback still works if old files are corrupt.
        try:
            old = self.capture(previous_connected_ids)
        except ConfigArchiveError:
            old = None
        if old is not None:
            _atomic_write(backup / "configuration.json", _encode(old.payload))
        staged = {filename: _encode(value) for filename, value in bundle.files.items()}
        journal = self.data_dir / JOURNAL
        _atomic_write(journal, _encode({"schema_version": 1, "backup_name": name}))
        try:
            for filename, raw in staged.items():
                _atomic_write(self.data_dir / filename, raw)
            journal.unlink()
        except Exception as exc:
            recover_pending_import(self.data_dir)
            raise ConfigArchiveError(f"导入失败，已回滚原配置：{exc}；备份：{backup}") from exc
        return backup
