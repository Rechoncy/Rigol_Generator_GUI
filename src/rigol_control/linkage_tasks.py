from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from .drivers.dg4000 import StepParameter
from .linkage import (
    DispatchMode,
    LinkageAxis,
    LinkagePoint,
    LinkageStepValue,
    validate_linkage_axes,
)
from .models import utc_now
from .repository import default_data_dir
from .scan import ScanMode


@dataclass(frozen=True, slots=True)
class LinkageTaskRecord:
    id: str
    name: str
    ni_device_id: str
    counter: str
    terminal: str
    rising: bool
    scan_mode: ScanMode
    dispatch_mode: DispatchMode
    axes: tuple[LinkageAxis, ...]
    points: tuple[LinkagePoint, ...]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        ni_device_id: str,
        counter: str,
        terminal: str,
        rising: bool,
        scan_mode: ScanMode,
        axes: tuple[LinkageAxis, ...] | list[LinkageAxis],
        points: tuple[LinkagePoint, ...] | list[LinkagePoint],
        dispatch_mode: DispatchMode = DispatchMode.STRICT_SERIAL,
    ) -> "LinkageTaskRecord":
        now = utc_now()
        task = cls(
            id=str(uuid4()),
            name=name.strip(),
            ni_device_id=ni_device_id.strip(),
            counter=counter.strip(),
            terminal=terminal.strip(),
            rising=bool(rising),
            scan_mode=scan_mode,
            dispatch_mode=dispatch_mode,
            axes=tuple(axes),
            points=tuple(points),
            created_at=now,
            updated_at=now,
        )
        task.validate()
        return task

    def validate(self) -> None:
        if not self.name:
            raise ValueError("联动任务名称不能为空")
        if not self.ni_device_id:
            raise ValueError("联动任务必须指定 NI 触发设备")
        if not self.counter or not self.terminal:
            raise ValueError("NI 计数器和 PFI 端子不能为空")
        if not self.axes or not self.points:
            raise ValueError("联动任务必须包含扫描轴和联合点表")
        axes = validate_linkage_axes(self.axes)
        expected_targets = tuple(axis.target for axis in axes)
        for point_index, point in enumerate(self.points, start=1):
            targets = tuple(
                (item.device_id, item.channel, item.parameter) for item in point.values
            )
            if targets != expected_targets:
                raise ValueError(f"联合点 {point_index} 的目标与扫描轴不一致")
            for axis, item in zip(axes, point.values):
                if item.unit_key != axis.unit_key:
                    raise ValueError(f"联合点 {point_index} 的显示单位与扫描轴不一致")
                if not all(
                    math.isfinite(value) for value in (item.value, item.display_value)
                ):
                    raise ValueError(f"联合点 {point_index} 包含非有限数值")

    def revised(self, **changes: Any) -> "LinkageTaskRecord":
        task = replace(self, **changes, updated_at=utc_now())
        task.validate()
        return task

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ni_device_id": self.ni_device_id,
            "counter": self.counter,
            "terminal": self.terminal,
            "rising": self.rising,
            "scan_mode": self.scan_mode.value,
            "dispatch_mode": self.dispatch_mode.value,
            "axes": [
                {
                    "device_id": axis.device_id,
                    "parameter": axis.parameter.value,
                    "channel": axis.channel,
                    "unit_key": axis.unit_key,
                    "start": axis.start,
                    "step": axis.step,
                    "count": axis.count,
                }
                for axis in self.axes
            ],
            "points": [
                [
                    {
                        "device_id": item.device_id,
                        "parameter": item.parameter.value,
                        "channel": item.channel,
                        "value": item.value,
                        "display_value": item.display_value,
                        "unit_key": item.unit_key,
                    }
                    for item in point.values
                ]
                for point in self.points
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LinkageTaskRecord":
        task = cls(
            id=str(data["id"]),
            name=str(data["name"]),
            ni_device_id=str(data["ni_device_id"]),
            counter=str(data["counter"]),
            terminal=str(data["terminal"]),
            rising=bool(data["rising"]),
            scan_mode=ScanMode(data["scan_mode"]),
            dispatch_mode=DispatchMode(
                data.get("dispatch_mode", DispatchMode.STRICT_SERIAL.value)
            ),
            axes=tuple(
                LinkageAxis(
                    device_id=str(axis["device_id"]),
                    parameter=StepParameter(axis["parameter"]),
                    channel=int(axis["channel"]),
                    unit_key=str(axis["unit_key"]),
                    start=float(axis["start"]),
                    step=float(axis["step"]),
                    count=int(axis["count"]),
                )
                for axis in data["axes"]
            ),
            points=tuple(
                LinkagePoint(
                    tuple(
                        LinkageStepValue(
                            device_id=str(item["device_id"]),
                            parameter=StepParameter(item["parameter"]),
                            channel=int(item["channel"]),
                            value=float(item["value"]),
                            display_value=float(item["display_value"]),
                            unit_key=str(item["unit_key"]),
                        )
                        for item in point
                    )
                )
                for point in data["points"]
            ),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
        task.validate()
        return task


class LinkageTaskRepository:
    SCHEMA_VERSION = 1

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.path = self.data_dir / "linkage_tasks.json"

    def list(self) -> list[LinkageTaskRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取跨设备联动任务：{exc}") from exc
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise RuntimeError("跨设备联动任务配置版本不受支持")
        return [LinkageTaskRecord.from_dict(item) for item in payload.get("tasks", [])]

    def save(self, task: LinkageTaskRecord) -> LinkageTaskRecord:
        task.validate()
        tasks = self.list()
        for index, existing in enumerate(tasks):
            if existing.id == task.id:
                tasks[index] = task
                break
        else:
            tasks.append(task)
        self._write(tasks)
        return task

    def delete(self, task_id: str) -> bool:
        tasks = self.list()
        retained = [item for item in tasks if item.id != task_id]
        if len(retained) == len(tasks):
            return False
        self._write(retained)
        return True

    def _write(self, tasks: list[LinkageTaskRecord]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "tasks": [task.to_dict() for task in tasks],
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError as exc:
            raise RuntimeError(f"无法保存跨设备联动任务：{exc}") from exc
