"""Adapt explicit RF-power linkage values to Vaunix's existing trigger writer."""

from __future__ import annotations

from .drivers.dg4000 import StepParameter, StepValue
from .drivers.vaunix_lms import (
    VaunixLmsDevice, VaunixLmsError, VirtualVaunixLmsDevice, _step_commands,
)
from .linkage_parameters import LinkageParameter
from .units import unit_spec


class VaunixLinkageWriter:
    """Validate one device's values without I/O and bind a run to its connection.

    Linkage RF power is explicitly dBm. The legacy Vaunix trigger driver uses
    StepParameter.AMPLITUDE for absolute power, whereas DG uses that enum for
    volts; translation is restricted to this device boundary.
    """

    def __init__(self, device: VaunixLmsDevice | VirtualVaunixLmsDevice) -> None:
        self.device = device
        if not device.is_connected:
            raise VaunixLmsError("Vaunix 尚未连接；请连接后重新布防")
        self._connection_generation = device.connection_generation
        self._capabilities = device.capabilities
        self._identity = device.identity
        self._assert_binding()

    def _assert_binding(self) -> None:
        if (not self.device.is_connected
                or self.device.connection_generation != self._connection_generation):
            raise VaunixLmsError("Vaunix 连接已经断开或重建，原联动任务已失效；请停止并重新布防")
        if (self.device.capabilities != self._capabilities
                or self.device.identity != self._identity):
            raise VaunixLmsError("Vaunix 型号、身份或能力已经变化；请停止并重新布防")

    def _validated_values(self, values) -> tuple[StepValue, ...]:
        self._assert_binding()
        normalized = tuple(values)
        if not normalized:
            raise ValueError("Vaunix 联动点至少需要一个参数")
        converted = []
        seen = set()
        for item in normalized:
            if item.channel != 1:
                raise ValueError("Vaunix 只有一个射频输出，联动通道必须为 1")
            if item.parameter is StepParameter.FREQUENCY:
                if item.unit_key is not None:
                    unit_spec("frequency", item.unit_key)
                parameter = StepParameter.FREQUENCY
            elif item.parameter is LinkageParameter.RF_POWER:
                if item.unit_key != "dBm":
                    raise ValueError("Vaunix 射频输出功率必须明确使用 dBm")
                parameter = StepParameter.AMPLITUDE
            else:
                raise ValueError("Vaunix 跨设备联动仅支持频率和射频输出功率（dBm）")
            if parameter in seen:
                raise ValueError("Vaunix 联动点不能重复设置同一参数")
            seen.add(parameter)
            converted.append(StepValue(parameter, 1, item.value, item.display_value, item.unit_key))
        result = tuple(converted)
        # Pure validation shared with real and virtual SDK implementations:
        # capability bounds, unsigned 10-Hz encoding and the 0.5-dB power grid.
        _step_commands(result, self._capabilities)
        return result

    def validate_step_values(self, values) -> None:
        self._validated_values(values)

    def prepare_trigger(self):
        with self.device.transaction():
            self._assert_binding()
            prepared = self.device.prepare_trigger()
            self._assert_binding()
            return _PreparedVaunixLinkageWriter(self, prepared)

    def apply_steps(self, values) -> None:
        # One-shot callers receive the same nonreplaying setter path. A running
        # MultiDeviceTriggerController prepares and retains its writer once.
        self.prepare_trigger().apply_steps(values)


class _PreparedVaunixLinkageWriter:
    def __init__(self, source: VaunixLinkageWriter, prepared) -> None:
        self.source = source
        self.prepared = prepared

    def apply_steps(self, values) -> None:
        with self.source.device.transaction():
            converted = self.source._validated_values(values)
            # The existing driver also checks generation under its device lock
            # and never retries setters with an unknown acceptance result.
            self.prepared.apply_steps(converted)
