from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .drivers.dg4000 import StepParameter
from .models import utc_now
from .repository import default_data_dir


@dataclass(frozen=True, slots=True)
class StepTaskRecord:
    id: str
    name: str
    generator_device_id: str
    ni_device_id: str
    parameter: StepParameter
    channel: int
    counter: str
    terminal: str
    rising: bool
    points: tuple[float, ...]
    created_at: str
    updated_at: str

    @classmethod
    def create(
        cls,
        *,
        name: str,
        generator_device_id: str,
        ni_device_id: str,
        parameter: StepParameter,
        channel: int,
        counter: str,
        terminal: str,
        rising: bool,
        points: list[float] | tuple[float, ...],
    ) -> "StepTaskRecord":
        now = utc_now()
        task = cls(
            id=str(uuid4()),
            name=name.strip(),
            generator_device_id=generator_device_id,
            ni_device_id=ni_device_id,
            parameter=parameter,
            channel=channel,
            counter=counter.strip(),
            terminal=terminal.strip(),
            rising=bool(rising),
            points=tuple(float(value) for value in points),
            created_at=now,
            updated_at=now,
        )
        task.validate()
        return task

    def validate(self) -> None:
        if not self.name:
            raise ValueError("任务名称不能为空")
        if not self.generator_device_id or not self.ni_device_id:
            raise ValueError("步进任务必须同时指定 DG4000 和 NI 设备")
        if self.channel not in (1, 2):
            raise ValueError("DG4000 通道只能是 1 或 2")
        if not self.counter or not self.terminal:
            raise ValueError("NI 计数器和触发端子不能为空")
        if not self.points:
            raise ValueError("步进任务至少需要一个点")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "generator_device_id": self.generator_device_id,
            "ni_device_id": self.ni_device_id,
            "parameter": self.parameter.value,
            "channel": self.channel,
            "counter": self.counter,
            "terminal": self.terminal,
            "rising": self.rising,
            "points": list(self.points),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StepTaskRecord":
        task = cls(
            id=str(data["id"]),
            name=str(data["name"]),
            generator_device_id=str(data["generator_device_id"]),
            ni_device_id=str(data["ni_device_id"]),
            parameter=StepParameter(data["parameter"]),
            channel=int(data["channel"]),
            counter=str(data["counter"]),
            terminal=str(data["terminal"]),
            rising=bool(data["rising"]),
            points=tuple(float(value) for value in data["points"]),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )
        task.validate()
        return task


class StepTaskRepository:
    SCHEMA_VERSION = 1

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.path = self.data_dir / "step_tasks.json"

    def list(self) -> list[StepTaskRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取步进任务：{exc}") from exc
        if payload.get("schema_version") != self.SCHEMA_VERSION:
            raise RuntimeError("步进任务配置版本不受支持")
        return [StepTaskRecord.from_dict(item) for item in payload.get("tasks", [])]

    def save(self, task: StepTaskRecord) -> StepTaskRecord:
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

    def _write(self, tasks: list[StepTaskRecord]) -> None:
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
            raise RuntimeError(f"无法保存步进任务：{exc}") from exc
