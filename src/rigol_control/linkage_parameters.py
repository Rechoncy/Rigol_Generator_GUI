"""Source-specific linkage parameters; RF dBm must not reuse DG voltage units."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .drivers.dg4000 import StepParameter
from .models import DeviceKind


class LinkageParameter(str, Enum):
    RF_POWER = "rf_power"

    @property
    def label(self) -> str:
        return "射频输出功率"

    @property
    def short_label(self) -> str:
        return "功率"

    @property
    def unit(self) -> str:
        return "dBm"

    @property
    def unit_family(self) -> str:
        return "power_dbm"


class SsgLinkageMode(str, Enum):
    SOFTWARE = "software_step"
    NATIVE_EXTERNAL = "native_external"

    @property
    def label(self) -> str:
        return {self.SOFTWARE: "NI 软件逐点设置", self.NATIVE_EXTERNAL: "仪器原生列表 / 公共外部触发"}[self]


@dataclass(frozen=True, slots=True)
class RfPowerStepValue:
    parameter: LinkageParameter
    channel: int
    value: float
    display_value: float | None = None
    unit_key: str | None = "dBm"


def linkage_parameter(value: str) -> StepParameter | LinkageParameter:
    return LinkageParameter.RF_POWER if value == LinkageParameter.RF_POWER.value else StepParameter(value)


def parameters_for_kind(kind: DeviceKind) -> tuple[StepParameter | LinkageParameter, ...]:
    if kind is DeviceKind.DG4000:
        return tuple(StepParameter)
    if kind in {DeviceKind.VAUNIX_LMS, DeviceKind.SIGLENT_SSG}:
        return StepParameter.FREQUENCY, LinkageParameter.RF_POWER
    return ()


def validate_source_axis(axis, kind: DeviceKind) -> None:
    if axis.parameter not in parameters_for_kind(kind):
        raise ValueError("该仪器不支持此联动参数；DG 电压幅度与射频 dBm 功率不能互相改绑")
    if axis.channel not in ((1, 2) if kind is DeviceKind.DG4000 else (1,)):
        raise ValueError("该仪器只有一个射频输出，联动通道必须为 1")
    if axis.parameter is LinkageParameter.RF_POWER and axis.unit_key != "dBm":
        raise ValueError("射频联动功率使用 dBm，功率步长使用 dB")
