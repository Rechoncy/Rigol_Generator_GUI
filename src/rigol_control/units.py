from __future__ import annotations

from dataclasses import dataclass
import math


def roundtrip_number(value: float) -> str:
    """Keep distinct float values distinct in editable tables and commands."""
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("数值必须是有限数值")
    result = repr(number)
    return result[:-2] if result.endswith(".0") else result


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
        UnitSpec("GHz", "GHz", 1_000_000_000.0),
    ),
    "amplitude": (
        UnitSpec("Vpp", "Vpp", 1.0),
        UnitSpec("mVpp", "mVpp", 0.001),
    ),
    "voltage": (
        UnitSpec("V", "V", 1.0),
        UnitSpec("mV", "mV", 0.001),
    ),
    # Nonlinear conversion to dBm: voltage across a matched 50-ohm load.
    "rf_power": (
        UnitSpec("dBm", "dBm", 1.0),
        UnitSpec("mV", "mV", 0.001),
        UnitSpec("mVpp", "mVpp", 0.001),
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
    if family == "rf_power":
        unit_spec(family, key)
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("射频幅度必须是有限数值")
        if key == "dBm":
            return number
        if number <= 0:
            raise ValueError("射频电压幅度必须大于 0；关闭输出请使用 RF 关闭")
        peak_factor = 2 * math.sqrt(2) if key == "mVpp" else 1
        return 20 * math.log10(number) - 20 * math.log10(peak_factor) - 10 * math.log10(50) - 30
    number = float(value) * unit_spec(family, key).to_base_factor
    if not math.isfinite(number):
        raise ValueError("换算后的数值必须是有限数值")
    return number


def from_base(value: float, family: str, key: str) -> float:
    if family == "rf_power":
        unit_spec(family, key)
        dbm = float(value)
        if not math.isfinite(dbm):
            raise ValueError("射频幅度必须是有限数值")
        if key == "dBm":
            return dbm
        peak_factor = 2 * math.sqrt(2) if key == "mVpp" else 1
        try:
            number = 1000 * math.sqrt(50 * .001) * 10 ** (dbm / 20) * peak_factor
        except OverflowError as exc:
            raise ValueError("射频幅度超出可换算范围") from exc
        if not math.isfinite(number) or number <= 0:
            raise ValueError("射频幅度超出可换算范围")
        return number
    number = float(value) / unit_spec(family, key).to_base_factor
    if not math.isfinite(number):
        raise ValueError("换算后的数值必须是有限数值")
    return number


def convert(value: float, family: str, source_key: str, target_key: str) -> float:
    return from_base(to_base(value, family, source_key), family, target_key)
