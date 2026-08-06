from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class UnitSpec:
    key: str
    symbol: str
    to_base_factor: float


UNIT_FAMILIES: dict[str, tuple[UnitSpec, ...]] = {
    "frequency": (
        UnitSpec("Hz", "Hz", 1.0),
        UnitSpec("kHz", "kHz", 1_000.0),
        UnitSpec("MHz", "MHz", 1_000_000.0),
    ),
    "amplitude": (
        UnitSpec("Vpp", "Vpp", 1.0),
        UnitSpec("mVpp", "mVpp", 0.001),
    ),
    "voltage": (
        UnitSpec("V", "V", 1.0),
        UnitSpec("mV", "mV", 0.001),
    ),
    "angle": (
        UnitSpec("deg", "°", 1.0),
        UnitSpec("rad", "rad", 180.0 / math.pi),
    ),
    "time": (
        UnitSpec("s", "s", 1.0),
        UnitSpec("ms", "ms", 0.001),
        UnitSpec("us", "us", 0.000_001),
    ),
}


def unit_specs(family: str) -> tuple[UnitSpec, ...]:
    try:
        return UNIT_FAMILIES[family]
    except KeyError as exc:
        raise ValueError(f"未知单位类型：{family}") from exc


def unit_spec(family: str, key: str) -> UnitSpec:
    for spec in unit_specs(family):
        if spec.key == key:
            return spec
    raise ValueError(f"{key} 不是 {family} 支持的单位")


def default_unit(family: str) -> str:
    return unit_specs(family)[0].key


def to_base(value: float, family: str, key: str) -> float:
    number = float(value) * unit_spec(family, key).to_base_factor
    if not math.isfinite(number):
        raise ValueError("换算后的数值必须是有限数值")
    return number


def from_base(value: float, family: str, key: str) -> float:
    number = float(value) / unit_spec(family, key).to_base_factor
    if not math.isfinite(number):
        raise ValueError("换算后的数值必须是有限数值")
    return number


def convert(value: float, family: str, source_key: str, target_key: str) -> float:
    return from_base(to_base(value, family, source_key), family, target_key)
