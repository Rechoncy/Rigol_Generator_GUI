from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceKind(str, Enum):
    DG4000 = "dg4000"
    NI_DAQ = "ni_daq"
    GENERIC_SCPI = "generic_scpi"

    @property
    def label(self) -> str:
        return {
            DeviceKind.DG4000: "RIGOL DG4000",
            DeviceKind.NI_DAQ: "NI 数据采集卡",
            DeviceKind.GENERIC_SCPI: "通用 SCPI 设备",
        }[self]

    @property
    def accent(self) -> str:
        return {
            DeviceKind.DG4000: "#18c9b8",
            DeviceKind.NI_DAQ: "#8f7cff",
            DeviceKind.GENERIC_SCPI: "#f3a84f",
        }[self]


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    id: str
    kind: DeviceKind
    name: str
    resource: str
    connection: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_opened_at: str | None = None

    @classmethod
    def create(
        cls,
        *,
        kind: DeviceKind,
        name: str,
        resource: str,
        connection: str = "",
    ) -> "DeviceRecord":
        now = utc_now()
        record = cls(
            id=str(uuid4()),
            kind=kind,
            name=name.strip(),
            resource=resource.strip(),
            connection=connection.strip(),
            created_at=now,
            updated_at=now,
        )
        record.validate()
        return record

    def validate(self) -> None:
        if not self.id.strip():
            raise ValueError("设备 ID 不能为空")
        if not self.name.strip():
            raise ValueError("设备名称不能为空")
        if not self.resource.strip():
            raise ValueError("资源地址/设备名不能为空")

    def updated(self, **changes: Any) -> "DeviceRecord":
        record = replace(self, **changes, updated_at=utc_now())
        record.validate()
        return record

    def mark_opened(self) -> "DeviceRecord":
        return self.updated(last_opened_at=utc_now())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "name": self.name,
            "resource": self.resource,
            "connection": self.connection,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_opened_at": self.last_opened_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceRecord":
        record = cls(
            id=str(data["id"]),
            kind=DeviceKind(data["kind"]),
            name=str(data["name"]),
            resource=str(data["resource"]),
            connection=str(data.get("connection", "")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_opened_at=data.get("last_opened_at"),
        )
        record.validate()
        return record

