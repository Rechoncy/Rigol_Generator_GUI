from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QObject, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..analog import (
    AnalogAcquisitionController,
    AnalogAcquisitionState,
    AnalogAcquisitionUpdate,
)
from ..connections import ConnectionInfo, DeviceConnectionManager
from ..drivers.dg4000 import (
    BasicWaveform,
    DG4000Driver,
    StepParameter,
    StepValue,
    output_load_value,
)
from ..drivers.ni_analog import AnalogInputConfig, NIDaqAnalogInput
from ..drivers.ni_counter import NIDaqEdgeCounter
from ..drivers.scpi import ScpiSession
from ..models import DeviceKind, DeviceRecord
from ..repository import DeviceParameterRepository, default_data_dir
from ..scan import (
    MAX_SCAN_AXES,
    ScanAxis,
    ScanMode,
    ScanPoint,
    generate_scan_points,
    scan_point_count,
)
from ..sequence import MAX_SCAN_POINTS
from ..services import DeviceProbeService, ProbeResult, ProbeState
from ..trigger import ControllerState, TriggerSnapshot, TriggerStepController
from ..units import default_unit, from_base, to_base, unit_spec, unit_specs
from ..virtual_lab import is_virtual_resource
from .waveform import WaveformPlot
from .widgets import CallTask, metric_card


def page_shell(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(22, 20, 22, 22)
    layout.setSpacing(14)
    heading = QLabel(title)
    heading.setObjectName("WindowTitle")
    description = QLabel(subtitle)
    description.setObjectName("Muted")
    description.setWordWrap(True)
    layout.addWidget(heading)
    layout.addWidget(description)
    return page, layout


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(17, 15, 17, 17)
    layout.setSpacing(12)
    heading = QLabel(title)
    heading.setObjectName("SectionTitle")
    layout.addWidget(heading)
    return frame, layout


def value_spin(value: float, minimum: float, maximum: float, suffix: str, decimals: int = 6) -> QDoubleSpinBox:
    control = QDoubleSpinBox()
    control.setDecimals(decimals)
    control.setRange(minimum, maximum)
    control.setValue(value)
    control.setSuffix(f" {suffix}")
    control.setKeyboardTracking(False)
    return control


class CompactDoubleSpinBox(QDoubleSpinBox):
    """Keep high edit precision without filling the UI with trailing zeroes."""

    def textFromValue(self, value: float) -> str:
        return f"{value:.12g}"


class CheckableComboBox(QComboBox):
    """A compact multi-select dropdown whose entries use checkboxes."""

    checkedItemsChanged = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText("请选择一个或多个模拟输入端口")
        self.view().viewport().installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            watched is self.view().viewport()
            and event.type() == QEvent.Type.MouseButtonRelease
        ):
            position = event.position().toPoint()  # type: ignore[attr-defined]
            model_index = self.view().indexAt(position)
            if model_index.isValid():
                row = model_index.row()
                checked = self.itemData(row, Qt.ItemDataRole.CheckStateRole)
                new_state = (
                    Qt.CheckState.Unchecked
                    if checked == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                self.setItemData(row, new_state, Qt.ItemDataRole.CheckStateRole)
                self._refresh_summary()
                self.checkedItemsChanged.emit()
                return True
        return super().eventFilter(watched, event)

    def add_checkable_item(self, text: str, data: Any, *, checked: bool = False) -> None:
        self.addItem(text, data)
        self.setItemData(
            self.count() - 1,
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
            Qt.ItemDataRole.CheckStateRole,
        )
        self._refresh_summary()

    def checked_data(self) -> tuple[Any, ...]:
        return tuple(
            self.itemData(index)
            for index in range(self.count())
            if self.itemData(index, Qt.ItemDataRole.CheckStateRole)
            == Qt.CheckState.Checked
        )

    def set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.blockSignals(True)
        for index in range(self.count()):
            self.setItemData(index, state, Qt.ItemDataRole.CheckStateRole)
        self.blockSignals(False)
        self._refresh_summary()
        self.checkedItemsChanged.emit()

    def _refresh_summary(self) -> None:
        labels = [
            self.itemText(index).split("/")[-1]
            for index in range(self.count())
            if self.itemData(index, Qt.ItemDataRole.CheckStateRole)
            == Qt.CheckState.Checked
        ]
        if not labels:
            text = "未选择采集端口"
        elif len(labels) <= 3:
            text = f"已选 {len(labels)} 路：" + ", ".join(labels)
        else:
            text = f"已选 {len(labels)} 路：" + ", ".join(labels[:3]) + "…"
        self.lineEdit().setText(text)


class UnitValueWidget(QWidget):
    """A compact decimal editor whose selectable unit preserves the base value."""

    def __init__(
        self,
        value: float,
        minimum: float,
        maximum: float,
        family: str,
        unit_key: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.family = family
        self.minimum_base = float(minimum)
        self.maximum_base = float(maximum)
        self.spin = CompactDoubleSpinBox()
        self.spin.setDecimals(9)
        self.spin.setKeyboardTracking(False)
        self.unit_combo = QComboBox()
        for spec in unit_specs(family):
            self.unit_combo.addItem(spec.symbol, spec.key)
        self._active_unit = unit_key or default_unit(family)
        index = self.unit_combo.findData(self._active_unit)
        if index < 0:
            raise ValueError(f"{self._active_unit} 不是 {family} 支持的单位")
        self.unit_combo.setCurrentIndex(index)
        self._apply_display_range()
        self.setValue(value)
        self.unit_combo.currentIndexChanged.connect(self._unit_changed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.spin, 1)
        layout.addWidget(self.unit_combo)

    def _apply_display_range(self) -> None:
        self.spin.setRange(
            from_base(self.minimum_base, self.family, self._active_unit),
            from_base(self.maximum_base, self.family, self._active_unit),
        )

    def _unit_changed(self, _index: int) -> None:
        base_value = to_base(self.spin.value(), self.family, self._active_unit)
        self._active_unit = self.unit_combo.currentData()
        self._apply_display_range()
        self.spin.setValue(from_base(base_value, self.family, self._active_unit))

    def value(self) -> float:
        return to_base(self.spin.value(), self.family, self._active_unit)

    def display_value(self) -> float:
        return self.spin.value()

    def setValue(self, base_value: float) -> None:
        self.spin.setValue(from_base(base_value, self.family, self._active_unit))

    def current_unit(self) -> str:
        return self._active_unit

    def set_unit(self, unit_key: str) -> None:
        index = self.unit_combo.findData(unit_key)
        if index < 0:
            raise ValueError(f"{unit_key} 不是 {self.family} 支持的单位")
        self.unit_combo.setCurrentIndex(index)


def unit_value_spin(
    value: float,
    minimum: float,
    maximum: float,
    family: str,
    unit_key: str | None = None,
) -> UnitValueWidget:
    return UnitValueWidget(value, minimum, maximum, family, unit_key)


@dataclass(slots=True)
class ScanAxisControls:
    frame: QFrame
    number: QLabel
    parameter: QComboBox
    channel: QComboBox
    unit: QComboBox
    start: CompactDoubleSpinBox
    step: CompactDoubleSpinBox
    count: QSpinBox
    move_up: QPushButton
    move_down: QPushButton
    remove: QPushButton
    active_parameter: StepParameter
    active_unit: str


class TriggerBridge(QObject):
    updated = pyqtSignal(object)


class AnalogBridge(QObject):
    updated = pyqtSignal(object)


class BaseDeviceWindow(QMainWindow):
    PARAMETER_BINDING_VERSION = 2
    PARAMETER_KEY_PROPERTY = "deviceParameterKey"

    def __init__(
        self,
        record: DeviceRecord,
        navigation: list[tuple[str, Callable[[], QWidget]]],
        parameter_repository: DeviceParameterRepository | None = None,
    ) -> None:
        super().__init__()
        self.record = record
        self.parameter_repository = parameter_repository
        self._restoring_device_parameters = False
        self._last_parameter_snapshot: dict[str, Any] | None = None
        self.thread_pool = QThreadPool.globalInstance()
        self._call_tasks: set[CallTask] = set()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{record.name} · 设备设置")
        self.resize(1180, 760)
        self.setMinimumSize(940, 620)

        root = QWidget()
        root.setObjectName("AppRoot")
        body = QHBoxLayout(root)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        sidebar = QFrame()
        sidebar.setFixedWidth(230)
        sidebar.setStyleSheet("background:#111824; border-right:1px solid #273145;")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(15, 20, 15, 18)
        side.setSpacing(5)

        kind = QLabel(record.kind.label)
        kind.setStyleSheet(f"color:{record.kind.accent}; font-weight:700;")
        name = QLabel(record.name)
        name.setObjectName("CardTitle")
        name.setWordWrap(True)
        resource = QLabel(record.resource)
        resource.setObjectName("Muted")
        resource.setWordWrap(True)
        side.addWidget(kind)
        side.addWidget(name)
        side.addWidget(resource)
        side.addSpacing(18)

        self.pages = QStackedWidget()
        self.nav_buttons: list[QPushButton] = []
        for index, (label, page_factory) in enumerate(navigation):
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, i=index: self._select_page(i))
            self.nav_buttons.append(button)
            side.addWidget(button)
            self.pages.addWidget(page_factory())
        side.addStretch()
        self.driver_status = QLabel("● 驱动尚未连接")
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setToolTip("保存配置不依赖仪器驱动；实际下发命令前需安装并连接驱动。")
        side.addWidget(self.driver_status)
        self._select_page(0)

        body.addWidget(sidebar)
        body.addWidget(self.pages, 1)
        self.setCentralWidget(root)
        self._parameter_save_timer = QTimer(self)
        self._parameter_save_timer.setInterval(500)
        self._parameter_save_timer.timeout.connect(self._save_device_parameters)
        self._restore_device_parameters()
        if self.parameter_repository is not None:
            self._save_device_parameters(force=True)
            self._parameter_save_timer.start()

    def submit(
        self,
        operation: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        task = CallTask(operation)
        self._call_tasks.add(task)
        if on_success:
            task.signals.succeeded.connect(on_success)
        task.signals.failed.connect(on_error or self._show_hardware_error)
        task.signals.finished.connect(lambda item=task: self._call_tasks.discard(item))
        self.thread_pool.start(task)

    def _show_hardware_error(self, message: str) -> None:
        QMessageBox.warning(self, "仪器操作失败", message)

    def _select_page(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        for position, button in enumerate(self.nav_buttons):
            button.setChecked(position == index)

    @staticmethod
    def _parameter_data(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        return str(value)

    @classmethod
    def _set_parameter_key(cls, control: QWidget, key: str) -> None:
        """Give a persisted control a stable key independent of Qt child order."""
        control.setProperty(cls.PARAMETER_KEY_PROPERTY, key)

    def _persistent_controls(
        self,
        *,
        use_explicit_keys: bool = True,
    ) -> dict[str, QWidget]:
        bindings: dict[str, QWidget] = {}

        def visit(parent: QWidget, page_index: int, counters: dict[str, int]) -> None:
            for child in parent.children():
                if not isinstance(child, QWidget):
                    continue
                if bool(child.property("skipDeviceParameterPersistence")):
                    continue
                if isinstance(child, UnitValueWidget):
                    kind = "unit"
                elif isinstance(child, CheckableComboBox):
                    kind = "multi_combo"
                elif isinstance(child, QComboBox):
                    kind = "combo"
                elif isinstance(child, QDoubleSpinBox):
                    kind = "double"
                elif isinstance(child, QSpinBox):
                    kind = "spin"
                elif isinstance(child, QCheckBox):
                    kind = "check"
                elif isinstance(child, QLineEdit):
                    kind = "line"
                else:
                    visit(child, page_index, counters)
                    continue
                explicit_key = child.property(self.PARAMETER_KEY_PROPERTY)
                if use_explicit_keys and explicit_key:
                    key = f"named/{explicit_key}"
                    existing = bindings.get(key)
                    if existing is not None and existing is not child:
                        raise RuntimeError(f"设备参数保存键重复：{explicit_key}")
                    bindings[key] = child
                    continue
                ordinal = counters.get(kind, 0)
                counters[kind] = ordinal + 1
                bindings[f"page_{page_index}/{kind}_{ordinal}"] = child

        for page_index in range(self.pages.count()):
            visit(self.pages.widget(page_index), page_index, {})
        return bindings

    def _legacy_persistent_controls(self) -> dict[str, QWidget]:
        """Bindings used by snapshots written before stable semantic keys."""
        return self._persistent_controls(use_explicit_keys=False)

    def _serialize_parameter_control(self, control: QWidget) -> dict[str, Any]:
        if isinstance(control, UnitValueWidget):
            return {
                "type": "unit",
                "value": control.value(),
                "unit": control.current_unit(),
            }
        if isinstance(control, CheckableComboBox):
            return {
                "type": "multi_combo",
                "checked": [
                    self._parameter_data(value) for value in control.checked_data()
                ],
            }
        if isinstance(control, QComboBox):
            return {
                "type": "combo",
                "data": self._parameter_data(control.currentData()),
                "text": control.currentText(),
            }
        if isinstance(control, QDoubleSpinBox):
            return {"type": "double", "value": control.value()}
        if isinstance(control, QSpinBox):
            return {"type": "spin", "value": control.value()}
        if isinstance(control, QCheckBox):
            return {"type": "check", "checked": control.isChecked()}
        if isinstance(control, QLineEdit):
            return {"type": "line", "text": control.text()}
        raise TypeError(f"不支持保存控件 {type(control).__name__}")

    def _restore_parameter_control(
        self,
        control: QWidget,
        state: dict[str, Any],
    ) -> None:
        if isinstance(control, UnitValueWidget) and state.get("type") == "unit":
            unit_key = str(state.get("unit", control.current_unit()))
            if control.unit_combo.findData(unit_key) >= 0:
                control.set_unit(unit_key)
            control.setValue(float(state.get("value", control.value())))
            return
        if (
            isinstance(control, CheckableComboBox)
            and state.get("type") == "multi_combo"
        ):
            checked = set(state.get("checked", []))
            control.blockSignals(True)
            for index in range(control.count()):
                value = self._parameter_data(control.itemData(index))
                control.setItemData(
                    index,
                    Qt.CheckState.Checked if value in checked else Qt.CheckState.Unchecked,
                    Qt.ItemDataRole.CheckStateRole,
                )
            control.blockSignals(False)
            control._refresh_summary()
            control.checkedItemsChanged.emit()
            return
        if isinstance(control, QComboBox) and state.get("type") == "combo":
            saved_data = state.get("data")
            index = -1
            if saved_data is not None:
                index = next(
                    (
                        item_index
                        for item_index in range(control.count())
                        if self._parameter_data(control.itemData(item_index)) == saved_data
                    ),
                    -1,
                )
            if index < 0:
                index = control.findText(str(state.get("text", "")))
            if index >= 0:
                control.setCurrentIndex(index)
            elif control.isEditable():
                control.setCurrentText(str(state.get("text", "")))
            return
        if isinstance(control, QDoubleSpinBox) and state.get("type") == "double":
            control.setValue(float(state.get("value", control.value())))
            return
        if isinstance(control, QSpinBox) and state.get("type") == "spin":
            control.setValue(int(state.get("value", control.value())))
            return
        if isinstance(control, QCheckBox) and state.get("type") == "check":
            control.setChecked(bool(state.get("checked", control.isChecked())))
            return
        if isinstance(control, QLineEdit) and state.get("type") == "line":
            control.setText(str(state.get("text", "")))

    def _custom_device_parameters(self) -> dict[str, Any]:
        return {}

    def _restore_custom_device_parameters(self, state: dict[str, Any]) -> None:
        del state

    def _device_parameter_snapshot(self) -> dict[str, Any]:
        return {
            "binding_version": self.PARAMETER_BINDING_VERSION,
            "controls": {
                key: self._serialize_parameter_control(control)
                for key, control in self._persistent_controls().items()
            },
            "custom": self._custom_device_parameters(),
        }

    def _restore_device_parameters(self) -> None:
        if self.parameter_repository is None:
            return
        try:
            snapshot = self.parameter_repository.load(
                self.record.id,
                kind=self.record.kind.value,
            )
            if not snapshot:
                return
            self._restoring_device_parameters = True
            controls = self._persistent_controls()
            legacy_controls = (
                self._legacy_persistent_controls()
                if snapshot.get("binding_version") != self.PARAMETER_BINDING_VERSION
                else {}
            )
            saved_controls = snapshot.get("controls", {})
            if isinstance(saved_controls, dict):
                for key, state in saved_controls.items():
                    control = controls.get(key) or legacy_controls.get(key)
                    if control is not None and isinstance(state, dict):
                        self._restore_parameter_control(control, state)
            custom = snapshot.get("custom", {})
            if isinstance(custom, dict):
                self._restore_custom_device_parameters(custom)
            self.statusBar().showMessage("已恢复该设备上次保存的参数", 4000)
        except Exception as exc:
            self.statusBar().showMessage(f"设备参数恢复失败：{exc}", 10000)
        finally:
            self._restoring_device_parameters = False

    def _save_device_parameters(self, force: bool = False) -> None:
        if self.parameter_repository is None or self._restoring_device_parameters:
            return
        try:
            snapshot = self._device_parameter_snapshot()
            if not force and snapshot == self._last_parameter_snapshot:
                return
            self.parameter_repository.save(
                self.record.id,
                kind=self.record.kind.value,
                parameters=snapshot,
            )
            self._last_parameter_snapshot = snapshot
        except Exception as exc:
            self.statusBar().showMessage(f"设备参数自动保存失败：{exc}", 10000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._parameter_save_timer.stop()
        self._save_device_parameters(force=True)
        super().closeEvent(event)

class DG4000Window(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        ni_devices: list[DeviceRecord],
        connection_manager: DeviceConnectionManager | None = None,
        device_provider: Callable[[], list[DeviceRecord]] | None = None,
        parameter_repository: DeviceParameterRepository | None = None,
    ) -> None:
        self.ni_devices = ni_devices
        self.device_provider = device_provider
        self.connection_manager = connection_manager
        self.driver = (
            connection_manager.dg_driver_for(record)
            if connection_manager is not None
            else DG4000Driver(ScpiSession(record.resource))
        )
        self.controller: TriggerStepController | None = None
        self._trigger_lease_owner = f"single-trigger:{record.id}"
        self.trigger_bridge = TriggerBridge()
        self.trigger_bridge.updated.connect(self._trigger_updated)
        self._last_trigger_error: str | None = None
        self._ui_connected = False
        self._ni_source_signature: tuple[tuple[str, str, str, bool], ...] = ()
        self.channel_controls: dict[int, dict[str, Any]] = {}
        self.impedance_controls: dict[int, dict[str, Any]] = {}
        self.modulation_controls: dict[str, dict[str, Any]] = {}
        navigation = [
            ("总览", self._overview_page),
            ("通道与波形", self._channel_page),
            ("调制", self._modulation_page),
            ("扫频与脉冲串", self._sweep_burst_page),
            ("触发步进", self._trigger_step_page),
            ("存储与系统", self._system_page),
        ]
        super().__init__(record, navigation, parameter_repository)
        if self.driver.is_connected:
            identity = (
                self.connection_manager.info(record).identity
                if self.connection_manager is not None
                else self.driver.identify()
            )
            self._connected(identity)

    def sync_connection_state(self) -> None:
        """Reflect connection changes made from the device center."""

        if self.connection_manager is None:
            return
        connected = self.connection_manager.is_connected(self.record)
        if connected and not self._ui_connected:
            self._connected(self.connection_manager.info(self.record))
        elif not connected and self._ui_connected:
            self._disconnected()

    def _overview_page(self) -> QWidget:
        page, layout = page_shell("DG4000 设备总览", "管理连接、输出状态与最近的联动任务。")
        metrics = QHBoxLayout()
        self.overview_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("connection", "连接状态", "未连接", "#f0b95d"),
            ("channel_1", "通道 1", "未知", "#8995aa"),
            ("channel_2", "通道 2", "未知", "#8995aa"),
            ("backlog", "触发积压", "0", "#55d9cc"),
        ):
            card, number = metric_card(caption, value, color)
            self.overview_metrics[key] = number
            metrics.addWidget(card)
        layout.addLayout(metrics)

        connection, connection_layout = panel("设备连接")
        form = QFormLayout()
        form.addRow("资源地址", QLabel(self.record.resource))
        form.addRow("连接方式", QLabel(self.record.connection or "VISA / SCPI"))
        self.identity_label = QLabel("连接后读取 *IDN?")
        self.identity_label.setWordWrap(True)
        form.addRow("身份信息", self.identity_label)
        connection_layout.addLayout(form)
        buttons = QHBoxLayout()
        self.connect_button = QPushButton("连接设备")
        self.connect_button.setObjectName("PrimaryButton")
        self.connect_button.clicked.connect(self._connect_device)
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.setEnabled(False)
        self.disconnect_button.clicked.connect(self._disconnect_device)
        self.self_test_button = QPushButton("执行自检")
        self.self_test_button.setEnabled(False)
        self.self_test_button.clicked.connect(lambda: self._execute_scpi("*TST?"))
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.self_test_button)
        buttons.addStretch()
        connection_layout.addLayout(buttons)
        layout.addWidget(connection)

        note, note_layout = panel("实施状态")
        status = QLabel(
            "设备中心、参数页面和触发步进模型已经建立。硬件会话层将使用单一 SCPI 写入队列，"
            "确保频率、幅度、相位、PSK 相位与 FSK 频率按触发顺序下发。"
        )
        status.setWordWrap(True)
        status.setObjectName("Muted")
        note_layout.addWidget(status)
        layout.addWidget(note)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            virtual_panel, virtual_layout = panel("虚拟 DG4000 实时状态")
            self.virtual_generator_state = QLabel()
            self.virtual_generator_state.setWordWrap(True)
            self.virtual_generator_state.setStyleSheet("font-family:Consolas; color:#b8c7da;")
            virtual_layout.addWidget(self.virtual_generator_state)
            self.virtual_generator_timer = QTimer(self)
            self.virtual_generator_timer.setInterval(250)
            self.virtual_generator_timer.timeout.connect(self._refresh_virtual_generator_state)
            self.virtual_generator_timer.start()
            self._refresh_virtual_generator_state()
            layout.addWidget(virtual_panel)
        layout.addStretch()
        return page

    def _refresh_virtual_generator_state(self) -> None:
        if self.connection_manager is None or not hasattr(self, "virtual_generator_state"):
            return
        snapshot = self.connection_manager.virtual_lab.generator_snapshot(self.record.resource)
        lines = []
        for channel in snapshot.channels:
            load_text = "High-Z" if channel.load_ohms is None else f"{channel.load_ohms} Ω"
            lines.append(
                f"CH{channel.channel}: {channel.function}  {channel.frequency_hz:g} Hz  "
                f"{channel.amplitude_vpp:g} Vpp  {channel.phase_deg:g}°  "
                f"负载={load_text}  输出={'ON' if channel.output else 'OFF'}  "
                f"{channel.modulation_type}={'ON' if channel.modulation_enabled else 'OFF'}  "
                f"PSK={channel.psk_phase_deg:g}°  FSK={channel.fsk_frequency_hz:g} Hz"
            )
        lines.append(
            f"SCPI 命令数：{snapshot.command_count}    模拟下发延迟：{snapshot.scpi_delay_ms} ms"
        )
        self.virtual_generator_state.setText("\n".join(lines))

    def _connect_device(self) -> None:
        self.connect_button.setEnabled(False)
        self.connect_button.setText("连接中…")
        self.submit(
            lambda: (
                self.connection_manager.connect(self.record)
                if self.connection_manager is not None
                else self.driver.connect()
            ),
            on_success=self._connected,
            on_error=self._connect_failed,
        )

    def _connected(self, identity: Any) -> None:
        identity_text = identity.identity if isinstance(identity, ConnectionInfo) else str(identity)
        self.identity_label.setText(identity_text)
        self.connect_button.setText("已连接")
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.self_test_button.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self._ui_connected = True
        self.overview_metrics["connection"].setText("已连接")
        self.statusBar().showMessage("DG4000 VISA 会话已连接", 5000)
        self._refresh_output_states()

    def _refresh_output_states(self) -> None:
        if not self.driver.is_connected:
            self._set_output_metrics(None)
            return
        self.submit(
            self.driver.output_states,
            on_success=lambda states: self._set_output_metrics(states),
            on_error=self._output_state_query_failed,
        )

    def _set_output_metrics(self, states: tuple[bool, bool] | None) -> None:
        for channel in (1, 2):
            state = None if states is None else states[channel - 1]
            self.overview_metrics[f"channel_{channel}"].setText(
                "未知" if state is None else ("已打开" if state else "关闭")
            )

    def _output_state_query_failed(self, message: str) -> None:
        self._set_output_metrics(None)
        self.statusBar().showMessage(f"已连接，但读取 CH1/CH2 输出状态失败：{message}", 8000)

    def _connect_failed(self, message: str) -> None:
        self.connect_button.setText("连接设备")
        self.connect_button.setEnabled(True)
        self._show_hardware_error(message)

    def _disconnect_device(self) -> None:
        if self.controller is not None:
            self._stop_trigger()
        self.disconnect_button.setEnabled(False)
        self.submit(
            lambda: (
                self.connection_manager.disconnect(self.record)
                if self.connection_manager is not None
                else self.driver.disconnect()
            ),
            on_success=self._disconnected,
        )

    def _disconnected(self, _result: Any = None) -> None:
        self.identity_label.setText("连接后读取 *IDN?")
        self.connect_button.setText("连接设备")
        self.connect_button.setEnabled(True)
        self.self_test_button.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self._ui_connected = False
        self.overview_metrics["connection"].setText("未连接")
        self._set_output_metrics(None)
        self.statusBar().showMessage("DG4000 已断开", 5000)

    def _require_connection(self) -> bool:
        if self.driver.is_connected:
            return True
        QMessageBox.information(self, "尚未连接", "请先在“总览”页连接 DG4000。")
        return False

    def _channel_page(self) -> QWidget:
        page, layout = page_shell(
            "通道与基本波形",
            "配置两个输出通道的波形、频率、幅度、偏置、初相位和输出状态。负载阻抗请在“存储与系统”中设置。",
        )
        tabs = QTabWidget()
        for channel in (1, 2):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            settings, settings_layout = panel(f"CH{channel} 输出")
            form = QFormLayout()
            waveform = QComboBox()
            waveform.addItems(["正弦波", "方波", "锯齿波", "脉冲", "噪声", "任意波"])
            frequency = unit_value_spin(1000, 1e-6, 160e6, "frequency", "kHz")
            amplitude = unit_value_spin(1.0, 0.001, 20.0, "amplitude", "Vpp")
            offset = unit_value_spin(0, -10, 10, "voltage", "V")
            phase = unit_value_spin(0, 0, 360, "angle", "deg")
            form.addRow("波形", waveform)
            form.addRow("频率", frequency)
            form.addRow("幅度", amplitude)
            form.addRow("直流偏置", offset)
            form.addRow("初相位", phase)
            output = QCheckBox("启用通道输出")
            form.addRow("输出", output)
            for name, control in {
                "waveform": waveform,
                "frequency": frequency,
                "amplitude": amplitude,
                "offset": offset,
                "phase": phase,
                "output": output,
            }.items():
                self._set_parameter_key(control, f"dg4000.channel.{channel}.{name}")
            self.channel_controls[channel] = {
                "waveform": waveform,
                "frequency": frequency,
                "amplitude": amplitude,
                "offset": offset,
                "phase": phase,
                "output": output,
            }
            settings_layout.addLayout(form)
            apply_button = QPushButton(f"应用到 CH{channel}")
            apply_button.setObjectName("PrimaryButton")
            apply_button.clicked.connect(lambda checked=False, selected=channel: self._apply_channel(selected))
            settings_layout.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
            tab_layout.addWidget(settings)
            tab_layout.addStretch()
            tabs.addTab(tab, f"CH{channel}")
        layout.addWidget(tabs)
        return page

    def _legacy_persistent_controls(self) -> dict[str, QWidget]:
        bindings = super()._legacy_persistent_controls()

        # Older releases numbered controls in QObject.children() order. A
        # QTabWidget changes that order whenever CH1/CH2 is selected, which is
        # the source of the channel-swap bug. Interpret legacy channel entries
        # in their intended visual order (CH1, then CH2) during migration.
        unit_names = ("frequency", "amplitude", "offset", "phase")
        for channel in (1, 2):
            channel_index = channel - 1
            controls = self.channel_controls[channel]
            bindings[f"page_1/combo_{channel_index}"] = controls["waveform"]
            bindings[f"page_1/check_{channel_index}"] = controls["output"]
            for value_index, name in enumerate(unit_names):
                bindings[f"page_1/unit_{channel_index * 4 + value_index}"] = (
                    controls[name]
                )

            impedance = self.impedance_controls[channel]
            bindings[f"page_5/combo_{channel_index}"] = impedance["load"]
            bindings[f"page_5/spin_{channel_index}"] = impedance["custom_load"]
        return bindings

    def _apply_channel(self, channel: int) -> None:
        if not self._require_connection():
            return
        controls = self.channel_controls[channel]
        waveform = BasicWaveform(
            function=controls["waveform"].currentText(),
            frequency_hz=controls["frequency"].value(),
            amplitude_vpp=controls["amplitude"].value(),
            offset_v=controls["offset"].value(),
            phase_deg=controls["phase"].value(),
        )
        output = controls["output"].isChecked()

        def apply() -> None:
            self.driver.configure_waveform_output(
                channel,
                waveform,
                enabled=output,
            )

        def applied(_result: Any = None) -> None:
            self.overview_metrics[f"channel_{channel}"].setText(
                "已打开" if output else "关闭"
            )
            self.statusBar().showMessage(f"CH{channel} 参数已下发", 4000)

        self.submit(apply, on_success=applied)

    def _modulation_page(self) -> QWidget:
        page, layout = page_shell("调制", "覆盖 DG4000 的模拟调制、数字键控和脉宽调制设置。")
        tabs = QTabWidget()
        for mode in ("AM", "FM", "PM", "ASK", "FSK", "PSK", "PWM"):
            tab = QWidget()
            tab_layout = QVBoxLayout(tab)
            settings, settings_layout = panel(f"{mode} 参数")
            form = QFormLayout()
            modulation_channel = QComboBox()
            modulation_channel.addItem("CH1", 1)
            modulation_channel.addItem("CH2", 2)
            source = QComboBox()
            source.addItems(["内部", "外部"])
            shape = QComboBox()
            shape.addItems(["正弦", "方波", "三角波", "上锯齿", "下锯齿", "噪声"])
            internal_frequency = unit_value_spin(100, 0.001, 1e6, "frequency", "Hz")
            form.addRow("输出通道", modulation_channel)
            form.addRow("调制源", source)
            form.addRow("内部波形", shape)
            form.addRow("调制频率", internal_frequency)
            if mode == "FSK":
                parameter = unit_value_spin(2000, 1e-6, 160e6, "frequency", "kHz")
                form.addRow("跳跃频率", parameter)
            elif mode == "PSK":
                parameter = unit_value_spin(180, 0, 360, "angle", "deg")
                form.addRow("调制相位", parameter)
            elif mode == "AM":
                parameter = value_spin(50, 0, 120, "%", 2)
                form.addRow("调制度", parameter)
            elif mode == "FM":
                parameter = unit_value_spin(10, 0, 1e8, "frequency", "kHz")
                form.addRow("频偏", parameter)
            elif mode == "PM":
                parameter = unit_value_spin(10, 0, 360, "angle", "deg")
                form.addRow("相偏", parameter)
            elif mode == "ASK":
                parameter = unit_value_spin(1, 0, 20, "amplitude", "Vpp")
                form.addRow("键控幅度", parameter)
            elif mode == "PWM":
                parameter = unit_value_spin(10e-6, 0, 1, "time", "us")
                form.addRow("脉宽偏移", parameter)
            else:
                parameter = value_spin(10, 0, 1e8, "", 3)
                form.addRow("偏移/深度", parameter)
            settings_layout.addLayout(form)
            enabled = QCheckBox(f"启用 {mode}")
            settings_layout.addWidget(enabled)
            self.modulation_controls[mode] = {
                "channel": modulation_channel,
                "source": source,
                "shape": shape,
                "frequency": internal_frequency,
                "parameter": parameter,
                "enabled": enabled,
            }
            apply_button = QPushButton("应用调制设置")
            apply_button.setObjectName("PrimaryButton")
            apply_button.clicked.connect(lambda checked=False, selected=mode: self._apply_modulation(selected))
            settings_layout.addWidget(apply_button, alignment=Qt.AlignmentFlag.AlignLeft)
            tab_layout.addWidget(settings)
            tab_layout.addStretch()
            tabs.addTab(tab, mode)
        layout.addWidget(tabs)
        return page

    def _apply_modulation(self, mode: str) -> None:
        if not self._require_connection():
            return
        controls = self.modulation_controls[mode]
        source = "INT" if controls["source"].currentText() == "内部" else "EXT"
        channel = controls["channel"].currentData()
        internal_frequency = controls["frequency"].value() if source == "INT" else None
        parameter = controls["parameter"].value()
        enabled = controls["enabled"].isChecked()
        self.submit(
            lambda: self.driver.configure_modulation(
                channel,
                mode,
                source=source,
                internal_function=controls["shape"].currentText(),
                internal_frequency_hz=internal_frequency,
                parameter=parameter,
                enabled=enabled,
            ),
            on_success=lambda _=None: self.statusBar().showMessage(
                f"{mode} 参数已下发到 CH{channel}", 4000
            ),
        )

    def _sweep_burst_page(self) -> QWidget:
        page, layout = page_shell("扫频与脉冲串", "配置仪器原生扫频、标记频率、触发源及脉冲串周期。")
        tabs = QTabWidget()
        sweep = QWidget()
        sweep_layout = QVBoxLayout(sweep)
        sweep_panel, sweep_form_layout = panel("扫频设置")
        form = QFormLayout()
        mode = QComboBox()
        mode.addItems(["线性", "对数", "步进"])
        sweep_start = unit_value_spin(1000, 1e-6, 160e6, "frequency", "kHz")
        sweep_stop = unit_value_spin(10000, 1e-6, 160e6, "frequency", "kHz")
        sweep_time = unit_value_spin(1, 0.001, 500, "time", "s")
        form.addRow("扫频类型", mode)
        form.addRow("起始频率", sweep_start)
        form.addRow("终止频率", sweep_stop)
        form.addRow("扫频时间", sweep_time)
        trigger = QComboBox()
        trigger.addItems(["内部", "外部", "手动"])
        form.addRow("触发源", trigger)
        sweep_form_layout.addLayout(form)
        sweep_apply = QPushButton("应用并启用扫频")
        sweep_apply.setObjectName("PrimaryButton")
        sweep_apply.clicked.connect(
            lambda: self._apply_sweep(mode, sweep_start, sweep_stop, sweep_time, trigger)
        )
        sweep_form_layout.addWidget(sweep_apply, alignment=Qt.AlignmentFlag.AlignLeft)
        sweep_layout.addWidget(sweep_panel)
        sweep_layout.addStretch()
        tabs.addTab(sweep, "扫频")

        burst = QWidget()
        burst_layout = QVBoxLayout(burst)
        burst_panel, burst_form_layout = panel("脉冲串设置")
        burst_form = QFormLayout()
        burst_mode = QComboBox()
        burst_mode.addItems(["N 周期", "无限", "门控"])
        count = QSpinBox()
        count.setRange(1, 1_000_000)
        count.setValue(10)
        burst_form.addRow("模式", burst_mode)
        burst_form.addRow("周期数", count)
        burst_period = unit_value_spin(0.1, 1e-6, 500, "time", "ms")
        burst_phase = unit_value_spin(0, 0, 360, "angle", "deg")
        burst_form.addRow("周期", burst_period)
        burst_form.addRow("起始相位", burst_phase)
        burst_form_layout.addLayout(burst_form)
        burst_apply = QPushButton("应用并启用脉冲串")
        burst_apply.setObjectName("PrimaryButton")
        burst_apply.clicked.connect(
            lambda: self._apply_burst(burst_mode, count, burst_period, burst_phase)
        )
        burst_form_layout.addWidget(burst_apply, alignment=Qt.AlignmentFlag.AlignLeft)
        burst_layout.addWidget(burst_panel)
        burst_layout.addStretch()
        tabs.addTab(burst, "脉冲串")
        layout.addWidget(tabs)
        return page

    def _apply_sweep(
        self,
        mode: QComboBox,
        start: UnitValueWidget,
        stop: UnitValueWidget,
        duration: UnitValueWidget,
        trigger: QComboBox,
    ) -> None:
        if not self._require_connection():
            return
        spacing = {"线性": "LIN", "对数": "LOG", "步进": "STEP"}[mode.currentText()]
        source = {"内部": "INT", "外部": "EXT", "手动": "MAN"}[trigger.currentText()]
        commands = [
            f":SOUR1:FREQ:START {start.value():.12g}",
            f":SOUR1:FREQ:STOP {stop.value():.12g}",
            f":SOUR1:SWE:TIME {duration.value():.12g}",
            f":SOUR1:SWE:SPAC {spacing}",
            f":SOUR1:SWE:TRIG:SOUR {source}",
            ":SOUR1:SWE:STAT ON",
        ]
        self.submit(
            lambda: self.driver.write_many(commands),
            on_success=lambda _=None: self.statusBar().showMessage("CH1 扫频已启用", 4000),
        )

    def _apply_burst(
        self,
        mode: QComboBox,
        count: QSpinBox,
        period: UnitValueWidget,
        phase: UnitValueWidget,
    ) -> None:
        if not self._require_connection():
            return
        mode_word = {"N 周期": "TRIG", "无限": "INF", "门控": "GAT"}[mode.currentText()]
        commands = [
            f":SOUR1:BURS:MODE {mode_word}",
            f":SOUR1:BURS:NCYC {count.value()}",
            f":SOUR1:BURS:INT:PER {period.value():.12g}",
            f":SOUR1:BURS:PHAS {phase.value():.12g}",
            ":SOUR1:BURS ON",
        ]
        self.submit(
            lambda: self.driver.write_many(commands),
            on_success=lambda _=None: self.statusBar().showMessage("CH1 脉冲串已启用", 4000),
        )

    def _trigger_step_page(self) -> QWidget:
        page, layout = page_shell(
            "NI 触发步进",
            "每个扫描点可联动多个 DG4000 参数。本联动任务中的 NI 只检测外部触发；第一个触发应用联合起始点，末点后自动循环。",
        )
        config, config_layout = panel("任务配置")
        form = QFormLayout()
        self.ni_source = QComboBox()
        self.ni_source.currentTextChanged.connect(self.ni_source.setToolTip)
        ni_source_row = QWidget()
        ni_source_layout = QHBoxLayout(ni_source_row)
        ni_source_layout.setContentsMargins(0, 0, 0, 0)
        ni_source_layout.setSpacing(6)
        ni_source_layout.addWidget(self.ni_source, 1)
        refresh_ni = QPushButton("刷新")
        refresh_ni.setToolTip("重新读取设备中心中的 NI 采集卡及连接状态")
        refresh_ni.clicked.connect(self.refresh_ni_devices)
        ni_source_layout.addWidget(refresh_ni)
        self.refresh_ni_devices(force=True)
        self.step_counter = QComboBox()
        for index in range(4):
            self.step_counter.addItem(f"ctr{index}", f"ctr{index}")
        self.step_terminal = QComboBox()
        self.step_terminal.setEditable(True)
        self.step_terminal.addItems([f"PFI{index}" for index in range(16)])
        self.step_edge = QComboBox()
        self.step_edge.addItems(["上升沿", "下降沿"])
        self.scan_mode = QComboBox()
        for mode in ScanMode:
            self.scan_mode.addItem(mode.label, mode)
        self.scan_mode.setToolTip(
            "同步联动：各轴第 n 点组成联合点，要求点数相同。\n"
            "组合扫描：生成所有轴的笛卡尔积，最后一个轴变化最快。"
        )
        self.scan_mode.currentIndexChanged.connect(self._scan_configuration_changed)
        form.addRow("NI 计数设备", ni_source_row)
        form.addRow("NI 计数器", self.step_counter)
        form.addRow("PFI 触发端子", self.step_terminal)
        form.addRow("触发边沿", self.step_edge)
        form.addRow("轴联动方式", self.scan_mode)
        self.step_command = QLabel("由扫描轴生成")
        self.step_command.setWordWrap(True)
        self.step_command.setStyleSheet("font-family:Consolas; color:#55d9cc;")
        form.addRow("每点 SCPI", self.step_command)
        self.scan_summary = QLabel()
        self.scan_summary.setWordWrap(True)
        form.addRow("点表预检", self.scan_summary)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            self.simulation_delay = QSpinBox()
            self.simulation_delay.setRange(0, 2000)
            self.simulation_delay.setSuffix(" ms")
            self.simulation_delay.setValue(
                self.connection_manager.virtual_lab.generator_snapshot(
                    self.record.resource
                ).scpi_delay_ms
            )
            self.simulation_delay.setToolTip("增大延迟可模拟真实 VISA 较慢时的触发积压")
            self.simulation_delay.valueChanged.connect(
                lambda value: self.connection_manager.virtual_lab.set_scpi_delay(
                    self.record.resource, value
                )
            )
            form.addRow("模拟下发延迟", self.simulation_delay)
        config_layout.addLayout(form)

        sequence_panel, sequence_layout = panel("多轴扫描与联合点表")
        axis_toolbar = QHBoxLayout()
        self.add_scan_axis_button = QPushButton("＋ 增加扫描轴")
        self.add_scan_axis_button.clicked.connect(self._add_scan_axis)
        self.generate_scan_button = QPushButton("生成联合点表")
        self.generate_scan_button.setObjectName("PrimaryButton")
        self.generate_scan_button.clicked.connect(self._generate_scan_table)
        axis_toolbar.addWidget(self.add_scan_axis_button)
        axis_toolbar.addStretch()
        axis_toolbar.addWidget(self.generate_scan_button)
        sequence_layout.addLayout(axis_toolbar)

        self.scan_axes: list[ScanAxisControls] = []
        self.scan_axis_tabs = QTabWidget()
        self.scan_axis_tabs.setProperty("skipDeviceParameterPersistence", True)
        self.scan_axis_tabs.setFixedHeight(165)
        sequence_layout.addWidget(self.scan_axis_tabs)

        point_toolbar = QHBoxLayout()
        self.add_scan_point_button = QPushButton("添加联合点")
        self.remove_scan_point_button = QPushButton("删除选中")
        self.add_scan_point_button.clicked.connect(self._add_manual_scan_point)
        self.remove_scan_point_button.clicked.connect(self._remove_sequence_rows)
        point_toolbar.addWidget(self.add_scan_point_button)
        point_toolbar.addWidget(self.remove_scan_point_button)
        point_toolbar.addStretch()
        point_note = QLabel("表格数值使用各轴所选单位，可直接输入小数")
        point_note.setObjectName("Muted")
        point_toolbar.addWidget(point_note)
        sequence_layout.addLayout(point_toolbar)

        self.step_table = QTableWidget(0, 3)
        self.step_table.setProperty("skipDeviceParameterPersistence", True)
        self.step_table.setMinimumHeight(125)
        self.step_table.setAlternatingRowColors(True)
        self.step_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._scan_table_axes: tuple[ScanAxis, ...] = ()
        self._scan_table_mode: ScanMode | None = None
        sequence_layout.addWidget(self.step_table)
        self._add_scan_axis(StepParameter.FREQUENCY, 1, refresh=False)
        self._scan_configuration_changed()
        self._generate_scan_table(show_message=False)

        config.setMinimumWidth(340)
        upper = QHBoxLayout()
        upper.setSpacing(14)
        upper.addWidget(config, 1)
        upper.addWidget(sequence_panel, 2)
        layout.addLayout(upper, 1)

        status = QHBoxLayout()
        self.trigger_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("received", "已收到触发", "0", "#8f7cff"),
            ("applied", "已完成下发", "0", "#55d9cc"),
            ("backlog", "待处理积压", "0", "#f0b95d"),
            ("current", "当前点", "—", "#e7ebf4"),
        ):
            card, number = metric_card(caption, value, color)
            self.trigger_metrics[key] = number
            status.addWidget(card)
        layout.addLayout(status)

        actions = QHBoxLayout()
        self.trigger_start = QPushButton("启动联动")
        self.trigger_start.setObjectName("PrimaryButton")
        self.trigger_start.clicked.connect(self._start_trigger)
        self.trigger_stop = QPushButton("停止")
        self.trigger_stop.setEnabled(False)
        self.trigger_stop.clicked.connect(self._stop_trigger)
        clear = QPushButton("显示清零")
        clear.clicked.connect(self._clear_trigger_display)
        actions.addWidget(self.trigger_start)
        actions.addWidget(self.trigger_stop)
        actions.addWidget(clear)
        actions.addStretch()
        note = QLabel("不设置最小触发间隔；过快触发进入有序积压队列")
        note.setObjectName("Muted")
        actions.addWidget(note)
        layout.addLayout(actions)
        return page

    def update_ni_devices(self, all_devices: list[DeviceRecord]) -> None:
        self.ni_devices = [item for item in all_devices if item.kind is DeviceKind.NI_DAQ]
        self.refresh_ni_devices(from_provider=False)

    def refresh_ni_devices(
        self,
        _checked: bool = False,
        *,
        force: bool = False,
        from_provider: bool = True,
    ) -> None:
        if from_provider and self.device_provider is not None:
            try:
                self.ni_devices = [
                    item for item in self.device_provider() if item.kind is DeviceKind.NI_DAQ
                ]
            except Exception as exc:
                self.statusBar().showMessage(f"刷新 NI 设备列表失败：{exc}", 8000)
        if not hasattr(self, "ni_source"):
            return
        signature = tuple(
            (
                item.id,
                item.name,
                item.resource,
                bool(
                    self.connection_manager is not None
                    and self.connection_manager.is_connected(item)
                ),
            )
            for item in self.ni_devices
        )
        if not force and signature == self._ni_source_signature:
            return
        selected_id = self.ni_source.currentData()
        self.ni_source.blockSignals(True)
        self.ni_source.clear()
        if self.ni_devices:
            for device in self.ni_devices:
                connected = bool(
                    self.connection_manager is not None
                    and self.connection_manager.is_connected(device)
                )
                state = "已连接" if connected else "未连接"
                self.ni_source.addItem(
                    f"● {state} · {device.name} ({device.resource})", device.id
                )
            selected_index = self.ni_source.findData(selected_id)
            if selected_index < 0 and self.connection_manager is not None:
                selected_index = next(
                    (
                        index
                        for index, device in enumerate(self.ni_devices)
                        if self.connection_manager.is_connected(device)
                    ),
                    0,
                )
            self.ni_source.setCurrentIndex(max(0, selected_index))
            self.ni_source.setEnabled(self.controller is None)
        else:
            self.ni_source.addItem("请先在设备中心添加 NI 采集卡", None)
            self.ni_source.setEnabled(False)
        self.ni_source.blockSignals(False)
        self.ni_source.setToolTip(self.ni_source.currentText())
        self._ni_source_signature = signature

    @staticmethod
    def _axis_defaults(parameter: StepParameter) -> tuple[str, float, float, int]:
        return {
            StepParameter.FREQUENCY: ("kHz", 1.0, 1.0, 4),
            StepParameter.AMPLITUDE: ("Vpp", 1.0, 0.1, 4),
            StepParameter.PHASE: ("deg", 0.0, 45.0, 4),
            StepParameter.PSK_PHASE: ("deg", 0.0, 45.0, 4),
            StepParameter.FSK_FREQUENCY: ("kHz", 2.0, 1.0, 4),
        }[parameter]

    def _new_scan_axis_controls(
        self,
        parameter: StepParameter,
        channel: int,
    ) -> ScanAxisControls:
        frame = QFrame()
        frame.setObjectName("MetricCard")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(10, 8, 10, 9)
        outer.setSpacing(6)
        header = QHBoxLayout()
        number = QLabel()
        number.setStyleSheet("font-weight:700; color:#55d9cc;")
        parameter_box = QComboBox()
        for item in StepParameter:
            parameter_box.addItem(item.label, item)
        parameter_box.setCurrentIndex(parameter_box.findData(parameter))
        channel_box = QComboBox()
        channel_box.addItem("CH1", 1)
        channel_box.addItem("CH2", 2)
        channel_box.setCurrentIndex(channel_box.findData(channel))
        unit_box = QComboBox()
        unit_key, start_value, step_value, count_value = self._axis_defaults(parameter)
        for spec in unit_specs(parameter.unit_family):
            unit_box.addItem(spec.symbol, spec.key)
        unit_box.setCurrentIndex(unit_box.findData(unit_key))
        move_up = QPushButton("↑")
        move_down = QPushButton("↓")
        remove = QPushButton("删除")
        remove.setObjectName("DangerButton")
        for button in (move_up, move_down):
            button.setFixedWidth(34)
        header.addWidget(number)
        header.addWidget(parameter_box, 1)
        header.addWidget(channel_box)
        header.addWidget(unit_box)
        header.addWidget(move_up)
        header.addWidget(move_down)
        header.addWidget(remove)
        outer.addLayout(header)

        fields = QGridLayout()
        fields.setHorizontalSpacing(8)
        fields.setVerticalSpacing(3)
        start = CompactDoubleSpinBox()
        step = CompactDoubleSpinBox()
        for control in (start, step):
            control.setDecimals(9)
            control.setRange(-1e15, 1e15)
            control.setKeyboardTracking(False)
        start.setValue(start_value)
        step.setValue(step_value)
        count = QSpinBox()
        count.setRange(1, MAX_SCAN_POINTS)
        count.setValue(count_value)
        count.setSuffix(" 点")
        fields.addWidget(QLabel("扫描起点"), 0, 0)
        fields.addWidget(QLabel("扫描步长"), 0, 1)
        fields.addWidget(QLabel("扫描点数"), 0, 2)
        fields.addWidget(start, 1, 0)
        fields.addWidget(step, 1, 1)
        fields.addWidget(count, 1, 2)
        fields.setColumnStretch(0, 2)
        fields.setColumnStretch(1, 2)
        fields.setColumnStretch(2, 1)
        outer.addLayout(fields)

        controls = ScanAxisControls(
            frame=frame,
            number=number,
            parameter=parameter_box,
            channel=channel_box,
            unit=unit_box,
            start=start,
            step=step,
            count=count,
            move_up=move_up,
            move_down=move_down,
            remove=remove,
            active_parameter=parameter,
            active_unit=unit_key,
        )
        parameter_box.currentIndexChanged.connect(
            lambda _index, axis=controls: self._axis_parameter_changed(axis)
        )
        channel_box.currentIndexChanged.connect(
            lambda _index, axis=controls: self._axis_channel_changed(axis)
        )
        unit_box.currentIndexChanged.connect(
            lambda _index, axis=controls: self._axis_unit_changed(axis)
        )
        start.valueChanged.connect(self._scan_configuration_changed)
        step.valueChanged.connect(self._scan_configuration_changed)
        count.valueChanged.connect(self._scan_configuration_changed)
        move_up.clicked.connect(lambda _checked=False, axis=controls: self._move_scan_axis(axis, -1))
        move_down.clicked.connect(lambda _checked=False, axis=controls: self._move_scan_axis(axis, 1))
        remove.clicked.connect(lambda _checked=False, axis=controls: self._remove_scan_axis(axis))
        return controls

    def _add_scan_axis(
        self,
        parameter: StepParameter | bool | None = None,
        channel: int | None = None,
        *,
        refresh: bool = True,
    ) -> None:
        if isinstance(parameter, bool):
            parameter = None
        if len(self.scan_axes) >= MAX_SCAN_AXES:
            QMessageBox.information(self, "扫描轴已满", f"最多支持 {MAX_SCAN_AXES} 个扫描轴。")
            return
        used = {
            (axis.channel.currentData(), axis.parameter.currentData()) for axis in self.scan_axes
        }
        if parameter is None:
            target = next(
                (
                    (candidate_channel, candidate_parameter)
                    for candidate_channel in (1, 2)
                    for candidate_parameter in StepParameter
                    if (candidate_channel, candidate_parameter) not in used
                ),
                None,
            )
            if target is None:
                QMessageBox.information(self, "没有可用轴", "所有通道和参数组合都已使用。")
                return
            channel, parameter = target
        controls = self._new_scan_axis_controls(parameter, channel or 1)
        self.scan_axes.append(controls)
        self._rebuild_scan_axes_layout()
        self.scan_axis_tabs.setCurrentWidget(controls.frame)
        if refresh:
            self._scan_configuration_changed()

    def _remove_scan_axis(self, axis: ScanAxisControls) -> None:
        if len(self.scan_axes) <= 1:
            QMessageBox.information(self, "保留扫描轴", "至少需要一个扫描轴。")
            return
        self.scan_axes.remove(axis)
        axis.frame.deleteLater()
        self._rebuild_scan_axes_layout()
        self._scan_configuration_changed()

    def _move_scan_axis(self, axis: ScanAxisControls, offset: int) -> None:
        index = self.scan_axes.index(axis)
        destination = index + offset
        if destination < 0 or destination >= len(self.scan_axes):
            return
        self.scan_axes[index], self.scan_axes[destination] = (
            self.scan_axes[destination],
            self.scan_axes[index],
        )
        self._rebuild_scan_axes_layout()
        self._scan_configuration_changed()

    def _rebuild_scan_axes_layout(self) -> None:
        selected = self.scan_axis_tabs.currentWidget()
        while self.scan_axis_tabs.count():
            self.scan_axis_tabs.removeTab(0)
        for index, axis in enumerate(self.scan_axes, start=1):
            axis.number.setText(f"轴 {index}")
            axis.move_up.setEnabled(index > 1)
            axis.move_down.setEnabled(index < len(self.scan_axes))
            self.scan_axis_tabs.addTab(
                axis.frame,
                f"轴 {index} · CH{axis.channel.currentData()} {axis.parameter.currentData().short_label}",
            )
        if selected in [axis.frame for axis in self.scan_axes]:
            self.scan_axis_tabs.setCurrentWidget(selected)
        elif self.scan_axes:
            self.scan_axis_tabs.setCurrentIndex(len(self.scan_axes) - 1)

    def _axis_parameter_changed(self, axis: ScanAxisControls) -> None:
        parameter = axis.parameter.currentData()
        unit_key, start, step, count = self._axis_defaults(parameter)
        axis.unit.blockSignals(True)
        axis.unit.clear()
        for spec in unit_specs(parameter.unit_family):
            axis.unit.addItem(spec.symbol, spec.key)
        axis.unit.setCurrentIndex(axis.unit.findData(unit_key))
        axis.unit.blockSignals(False)
        axis.active_parameter = parameter
        axis.active_unit = unit_key
        axis.start.setValue(start)
        axis.step.setValue(step)
        axis.count.setValue(count)
        self._rebuild_scan_axes_layout()
        self._scan_configuration_changed()

    def _axis_channel_changed(self, axis: ScanAxisControls) -> None:
        self._rebuild_scan_axes_layout()
        self.scan_axis_tabs.setCurrentWidget(axis.frame)
        self._scan_configuration_changed()

    def _axis_unit_changed(self, axis: ScanAxisControls) -> None:
        new_unit = axis.unit.currentData()
        if not new_unit or new_unit == axis.active_unit:
            return
        family = axis.active_parameter.unit_family
        start_base = to_base(axis.start.value(), family, axis.active_unit)
        step_base = to_base(axis.step.value(), family, axis.active_unit)
        axis.active_unit = new_unit
        axis.start.setValue(from_base(start_base, family, new_unit))
        axis.step.setValue(from_base(step_base, family, new_unit))
        self._scan_configuration_changed()

    def _scan_axis_models(self) -> tuple[ScanAxis, ...]:
        return tuple(
            ScanAxis(
                parameter=axis.parameter.currentData(),
                channel=axis.channel.currentData(),
                unit_key=axis.unit.currentData(),
                start=axis.start.value(),
                step=axis.step.value(),
                count=axis.count.value(),
            )
            for axis in self.scan_axes
        )

    def _scan_configuration_changed(self, _value: Any = None) -> None:
        try:
            axes = self._scan_axis_models()
            mode = self.scan_mode.currentData()
            total = scan_point_count(axes, mode)
        except ValueError as exc:
            self.scan_summary.setText(str(exc))
            self.scan_summary.setStyleSheet("color:#ff9397;")
            self.generate_scan_button.setEnabled(False)
            return
        commands = [axis.parameter.command(axis.channel) for axis in axes]
        self.step_command.setText("\n".join(commands))
        mode_note = "最后一轴变化最快" if mode is ScanMode.CARTESIAN else "各轴同序号联动"
        self.scan_summary.setText(
            f"预计 {total} 个联合点 · 每触发 {len(axes)} 条 SCPI · {mode_note}"
        )
        self.scan_summary.setStyleSheet("color:#54d6a3;")
        self.generate_scan_button.setEnabled(True)

    def _generate_scan_table(self, _checked: bool = False, *, show_message: bool = True) -> None:
        try:
            axes = self._scan_axis_models()
            mode = self.scan_mode.currentData()
            points = generate_scan_points(axes, mode)
        except ValueError as exc:
            QMessageBox.warning(self, "无法生成扫描表", str(exc))
            return
        headers = ["序号"]
        for index, axis in enumerate(axes, start=1):
            symbol = unit_spec(axis.parameter.unit_family, axis.unit_key).symbol
            headers.append(f"轴{index} CH{axis.channel} {axis.parameter.short_label} ({symbol})")
        headers.append("说明")
        self.step_table.setColumnCount(len(headers))
        self.step_table.setHorizontalHeaderLabels(headers)
        self.step_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        for column in range(1, len(headers) - 1):
            self.step_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Stretch
            )
        self.step_table.horizontalHeader().setSectionResizeMode(
            len(headers) - 1, QHeaderView.ResizeMode.Stretch
        )
        self.step_table.setUpdatesEnabled(False)
        try:
            self.step_table.setRowCount(0)
            for index, point in enumerate(points):
                note = "起始点（第一个触发）" if index == 0 else ""
                self._append_scan_point_row(
                    [
                        item.display_value if item.display_value is not None else item.value
                        for item in point.values
                    ],
                    note,
                )
        finally:
            self.step_table.setUpdatesEnabled(True)
            self.step_table.viewport().update()
        self._scan_table_axes = axes
        self._scan_table_mode = mode
        if show_message:
            self.statusBar().showMessage(
                f"已生成 {len(points)} 个联合扫描点，每点联动 {len(axes)} 个参数",
                5000,
            )

    def _sequence_points(self) -> list[ScanPoint]:
        axes = self._scan_axis_models()
        mode = self.scan_mode.currentData()
        if axes != self._scan_table_axes or mode is not self._scan_table_mode:
            raise ValueError("扫描轴配置已改变，请重新生成联合点表")
        if self.step_table.rowCount() < 1:
            raise ValueError("至少需要一个联合扫描点")
        points: list[ScanPoint] = []
        for row in range(self.step_table.rowCount()):
            values: list[StepValue] = []
            for axis_index, axis in enumerate(axes):
                item = self.step_table.item(row, axis_index + 1)
                try:
                    display_value = float(item.text().strip())
                    base_value = to_base(
                        display_value,
                        axis.parameter.unit_family,
                        axis.unit_key,
                    )
                except (AttributeError, ValueError) as exc:
                    raise ValueError(
                        f"第 {row + 1} 行、轴 {axis_index + 1} 不是有效数值"
                    ) from exc
                values.append(
                    StepValue(
                        parameter=axis.parameter,
                        channel=axis.channel,
                        value=base_value,
                        display_value=display_value,
                        unit_key=axis.unit_key,
                    )
                )
            points.append(ScanPoint(tuple(values)))
        return points

    def _sequence_values(self) -> list[float]:
        points = self._sequence_points()
        if len(points[0].values) != 1:
            raise ValueError("当前为多轴点表，请使用联合扫描点")
        return [point.values[0].value for point in points]

    def _append_scan_point_row(self, display_values: list[float], note: str) -> None:
        row = self.step_table.rowCount()
        self.step_table.insertRow(row)
        number = QTableWidgetItem(str(row + 1))
        number.setFlags(number.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.step_table.setItem(row, 0, number)
        for column, value in enumerate(display_values, start=1):
            self.step_table.setItem(row, column, QTableWidgetItem(f"{value:.12g}"))
        self.step_table.setItem(row, len(display_values) + 1, QTableWidgetItem(note))

    def _add_manual_scan_point(self) -> None:
        if not self._scan_table_axes:
            QMessageBox.information(self, "尚未生成", "请先生成联合点表。")
            return
        if self.step_table.rowCount() >= MAX_SCAN_POINTS:
            QMessageBox.information(self, "点数已满", f"最多支持 {MAX_SCAN_POINTS} 个点。")
            return
        if self.step_table.rowCount():
            last = self.step_table.rowCount() - 1
            values = [
                float(self.step_table.item(last, column + 1).text())
                for column in range(len(self._scan_table_axes))
            ]
        else:
            values = [axis.start for axis in self._scan_table_axes]
        self._append_scan_point_row(values, "")

    def _start_trigger(self) -> None:
        if not self._require_connection():
            return
        ni_id = self.ni_source.currentData()
        ni_record = next((item for item in self.ni_devices if item.id == ni_id), None)
        if ni_record is None:
            QMessageBox.warning(self, "缺少 NI 设备", "请先在设备中心添加并选择 NI 采集卡。")
            return
        if (
            self.connection_manager is not None
            and not self.connection_manager.is_connected(ni_record)
        ):
            QMessageBox.warning(
                self,
                "NI 设备未连接",
                "所选 NI 采集卡尚未连接。请先在设备中心或 NI 设置窗口连接，再刷新列表。",
            )
            return
        try:
            points = self._sequence_points()
        except ValueError as exc:
            QMessageBox.warning(self, "序列无效", str(exc))
            return
        if self.connection_manager is not None:
            try:
                self.connection_manager.acquire_devices(
                    self._trigger_lease_owner,
                    (self.record.id, ni_record.id),
                )
            except Exception as exc:
                QMessageBox.warning(self, "设备正忙", str(exc))
                return
        try:
            counter = (
                self.connection_manager.counter_for(
                    ni_record,
                    counter=self.step_counter.currentData(),
                    terminal=self.step_terminal.currentText(),
                    rising=self.step_edge.currentText() == "上升沿",
                )
                if self.connection_manager is not None
                else NIDaqEdgeCounter(
                    ni_record.resource,
                    counter=self.step_counter.currentData(),
                    terminal=self.step_terminal.currentText(),
                    rising=self.step_edge.currentText() == "上升沿",
                )
            )
            controller = TriggerStepController(
                counter,
                self.driver,
                scan_points=points,
            )
        except Exception as exc:
            if self.connection_manager is not None:
                self.connection_manager.release_devices(self._trigger_lease_owner)
            QMessageBox.warning(self, "无法创建触发任务", str(exc))
            return
        controller.add_callback(self.trigger_bridge.updated.emit)
        self.controller = controller
        self._last_trigger_error = None
        self.trigger_start.setEnabled(False)
        self.trigger_start.setText("启动中…")
        self._set_scan_editing_enabled(False)
        self.submit(
            controller.start,
            on_success=lambda _=None: self.statusBar().showMessage("NI 触发步进已启动", 4000),
            on_error=self._trigger_start_failed,
        )

    def _trigger_start_failed(self, message: str) -> None:
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._trigger_lease_owner)
        self.controller = None
        self.trigger_start.setText("启动联动")
        self.trigger_start.setEnabled(True)
        self.trigger_stop.setEnabled(False)
        self._set_scan_editing_enabled(True)
        self._show_hardware_error(message)

    def _stop_trigger(self) -> None:
        controller = self.controller
        if controller is None:
            return
        self.trigger_stop.setEnabled(False)

        def stopped(_result: Any = None) -> None:
            if self.connection_manager is not None:
                self.connection_manager.release_devices(self._trigger_lease_owner)
            self.controller = None
            self.trigger_start.setText("启动联动")
            self.trigger_start.setEnabled(True)
            self._set_scan_editing_enabled(True)
            self.statusBar().showMessage("触发步进已停止", 4000)

        self.submit(controller.stop, on_success=stopped)

    def _trigger_updated(self, snapshot: TriggerSnapshot) -> None:
        self.trigger_metrics["received"].setText(str(snapshot.received_count))
        self.trigger_metrics["applied"].setText(str(snapshot.applied_count))
        self.trigger_metrics["backlog"].setText(str(snapshot.backlog))
        self.overview_metrics["backlog"].setText(str(snapshot.backlog))
        if snapshot.current_index is None:
            self.trigger_metrics["current"].setText("—")
            self.trigger_metrics["current"].setToolTip("")
        else:
            details = []
            for item in snapshot.current_values:
                display_value = item.display_value if item.display_value is not None else item.value
                unit_key = item.unit_key or default_unit(item.parameter.unit_family)
                symbol = unit_spec(item.parameter.unit_family, unit_key).symbol
                details.append(
                    f"CH{item.channel} {item.parameter.label} = {display_value:.12g} {symbol}"
                )
            if len(details) == 1:
                self.trigger_metrics["current"].setText(
                    f"#{snapshot.current_index + 1} · {details[0].split(' = ', 1)[1]}"
                )
            else:
                self.trigger_metrics["current"].setText(
                    f"#{snapshot.current_index + 1} · {len(details)} 轴"
                )
            self.trigger_metrics["current"].setToolTip("\n".join(details))
        running = snapshot.state in {ControllerState.STARTING, ControllerState.RUNNING}
        self.trigger_stop.setEnabled(running or snapshot.state is ControllerState.ERROR)
        if snapshot.state is ControllerState.RUNNING:
            self.trigger_start.setText("运行中")
        elif snapshot.state is ControllerState.ERROR:
            self.trigger_start.setText("任务错误")
        if snapshot.error and snapshot.error != self._last_trigger_error:
            self._last_trigger_error = snapshot.error
            self.statusBar().showMessage(snapshot.error, 10000)
            if self.connection_manager is not None:
                self.connection_manager.release_devices(self._trigger_lease_owner)

    def _clear_trigger_display(self) -> None:
        if self.controller and self.controller.snapshot().state is ControllerState.RUNNING:
            QMessageBox.information(self, "任务正在运行", "请先停止任务，再清除界面计数。")
            return
        for key in ("received", "applied", "backlog"):
            self.trigger_metrics[key].setText("0")
        self.overview_metrics["backlog"].setText("0")
        self.trigger_metrics["current"].setText("—")
        self.trigger_metrics["current"].setToolTip("")

    def _set_scan_editing_enabled(self, enabled: bool) -> None:
        self.ni_source.setEnabled(enabled and bool(self.ni_devices))
        self.scan_mode.setEnabled(enabled)
        self.add_scan_axis_button.setEnabled(enabled)
        self.generate_scan_button.setEnabled(enabled)
        self.add_scan_point_button.setEnabled(enabled)
        self.remove_scan_point_button.setEnabled(enabled)
        for axis in self.scan_axes:
            axis.frame.setEnabled(enabled)
        if enabled:
            self.step_table.setEditTriggers(
                QAbstractItemView.EditTrigger.DoubleClicked
                | QAbstractItemView.EditTrigger.EditKeyPressed
                | QAbstractItemView.EditTrigger.AnyKeyPressed
            )
            self._scan_configuration_changed()
        else:
            self.step_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

    def _remove_sequence_rows(self) -> None:
        rows = sorted({index.row() for index in self.step_table.selectedIndexes()}, reverse=True)
        if not rows and self.step_table.currentRow() >= 0:
            rows = [self.step_table.currentRow()]
        for row in rows:
            if self.step_table.rowCount() > 1:
                self.step_table.removeRow(row)
        note_column = self.step_table.columnCount() - 1
        for row in range(self.step_table.rowCount()):
            self.step_table.item(row, 0).setText(str(row + 1))
            if row == 0 and not self.step_table.item(row, note_column).text():
                self.step_table.item(row, note_column).setText("起始点（第一个触发）")

    def _custom_device_parameters(self) -> dict[str, Any]:
        if not hasattr(self, "scan_axes") or not hasattr(self, "step_table"):
            return {}
        axes = [
            {
                "parameter": axis.parameter.currentData().value,
                "channel": axis.channel.currentData(),
                "unit": axis.unit.currentData(),
                "start": axis.start.value(),
                "step": axis.step.value(),
                "count": axis.count.value(),
            }
            for axis in self.scan_axes
        ]
        rows: list[dict[str, Any]] = []
        value_columns = len(self.scan_axes)
        for row in range(self.step_table.rowCount()):
            values = []
            for column in range(1, value_columns + 1):
                item = self.step_table.item(row, column)
                values.append(item.text() if item is not None else "")
            note_item = self.step_table.item(row, value_columns + 1)
            rows.append(
                {
                    "values": values,
                    "note": note_item.text() if note_item is not None else "",
                }
            )
        mode = self.scan_mode.currentData()
        return {
            "trigger_scan": {
                "mode": mode.value if isinstance(mode, ScanMode) else str(mode),
                "axes": axes,
                "rows": rows,
            }
        }

    def _restore_custom_device_parameters(self, state: dict[str, Any]) -> None:
        trigger_scan = state.get("trigger_scan")
        if not isinstance(trigger_scan, dict):
            return
        parsed_axes: list[tuple[StepParameter, int, str, float, float, int]] = []
        saved_axes = trigger_scan.get("axes", [])
        if not isinstance(saved_axes, list):
            return
        for item in saved_axes[:MAX_SCAN_AXES]:
            if not isinstance(item, dict):
                continue
            try:
                parameter = StepParameter(str(item["parameter"]))
                channel = int(item["channel"])
                unit_key = str(item["unit"])
                start = float(item["start"])
                step = float(item["step"])
                count = int(item["count"])
                if channel not in (1, 2):
                    continue
                unit_spec(parameter.unit_family, unit_key)
            except (KeyError, TypeError, ValueError):
                continue
            parsed_axes.append(
                (parameter, channel, unit_key, start, step, count)
            )
        if not parsed_axes:
            return

        try:
            mode = ScanMode(str(trigger_scan.get("mode", "")))
        except ValueError:
            mode = self.scan_mode.currentData()
        mode_index = self.scan_mode.findData(mode)
        if mode_index >= 0:
            self.scan_mode.setCurrentIndex(mode_index)

        for axis in self.scan_axes:
            axis.frame.setParent(None)
            axis.frame.deleteLater()
        self.scan_axes.clear()
        for parameter, channel, unit_key, start, step, count in parsed_axes:
            self._add_scan_axis(parameter, channel, refresh=False)
            axis = self.scan_axes[-1]
            unit_index = axis.unit.findData(unit_key)
            if unit_index >= 0:
                axis.unit.setCurrentIndex(unit_index)
            axis.start.setValue(start)
            axis.step.setValue(step)
            axis.count.setValue(count)
        self._rebuild_scan_axes_layout()
        self._generate_scan_table(show_message=False)

        rows = trigger_scan.get("rows")
        if isinstance(rows, list) and rows:
            self.step_table.setRowCount(0)
            for row_index, row_state in enumerate(rows[:MAX_SCAN_POINTS]):
                if not isinstance(row_state, dict):
                    continue
                values = row_state.get("values", [])
                if not isinstance(values, list) or len(values) != len(self.scan_axes):
                    continue
                self._append_scan_point_row(
                    [0.0] * len(self.scan_axes),
                    str(row_state.get("note", "")),
                )
                table_row = self.step_table.rowCount() - 1
                for column, value in enumerate(values, start=1):
                    self.step_table.item(table_row, column).setText(str(value))
                self.step_table.item(table_row, 0).setText(str(row_index + 1))
        self._scan_configuration_changed()

    def _impedance_control_row(self, channel: int) -> QWidget:
        load = QComboBox()
        load.addItem("高阻（INFINITY）", None)
        load.addItem("50 Ω（默认）", 50)
        load.addItem("75 Ω", 75)
        load.addItem("600 Ω", 600)
        load.addItem("自定义", "CUSTOM")
        load.setCurrentIndex(load.findData(50))
        load.setToolTip(
            f"对应 :OUTP{channel}:LOAD；用于匹配实际负载并校准仪器显示幅度。"
        )
        custom = QSpinBox()
        custom.setRange(1, 10_000)
        custom.setValue(50)
        custom.setSuffix(" Ω")
        custom.setEnabled(False)
        load.currentIndexChanged.connect(
            lambda _index, combo=load, control=custom: control.setEnabled(
                combo.currentData() == "CUSTOM"
            )
        )
        apply_button = QPushButton(f"应用到 CH{channel}")
        apply_button.clicked.connect(
            lambda _checked=False, selected=channel: self._apply_output_load(selected)
        )
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(load, 1)
        row_layout.addWidget(custom)
        row_layout.addWidget(apply_button)
        self.impedance_controls[channel] = {
            "load": load,
            "custom_load": custom,
            "apply": apply_button,
        }
        self._set_parameter_key(load, f"dg4000.channel.{channel}.load")
        self._set_parameter_key(custom, f"dg4000.channel.{channel}.custom_load")
        return row

    def _selected_output_load(self, channel: int) -> int | None:
        controls = self.impedance_controls[channel]
        load_data = controls["load"].currentData()
        return controls["custom_load"].value() if load_data == "CUSTOM" else load_data

    def _apply_output_load(self, channel: int) -> None:
        if not self._require_connection():
            return
        load_ohms = self._selected_output_load(channel)
        self.submit(
            lambda: self.driver.set_output_load(channel, load_ohms),
            on_success=lambda _=None: self.statusBar().showMessage(
                f"CH{channel} 负载阻抗已设置为 "
                f"{'高阻' if load_ohms is None else f'{load_ohms} Ω'}",
                5000,
            ),
        )

    def _apply_all_output_loads(self) -> None:
        if not self._require_connection():
            return
        commands = tuple(
            f":OUTP{channel}:LOAD {output_load_value(self._selected_output_load(channel))}"
            for channel in (1, 2)
        )
        self.submit(
            lambda: self.driver.write_many(commands),
            on_success=lambda _=None: self.statusBar().showMessage(
                "CH1/CH2 负载阻抗已同时下发", 5000
            ),
        )

    def _system_page(self) -> QWidget:
        page, layout = page_shell(
            "存储与系统",
            "管理输出负载阻抗、仪器状态文件、SCPI 控制台、显示和系统命令。",
        )
        impedance, impedance_layout = panel("输出负载阻抗")
        impedance_note = QLabel(
            "CH1/CH2 默认选择 50 Ω。设置通过 :OUTP<n>:LOAD 独立下发，不会在应用频率、"
            "幅度或波形时被重复改写。高阻或自定义 1–10000 Ω 仍然可用。"
        )
        impedance_note.setObjectName("Muted")
        impedance_note.setWordWrap(True)
        impedance_layout.addWidget(impedance_note)
        impedance_form = QFormLayout()
        for channel in (1, 2):
            impedance_form.addRow(f"CH{channel} 负载", self._impedance_control_row(channel))
        impedance_layout.addLayout(impedance_form)
        apply_all = QPushButton("同时应用 CH1/CH2")
        apply_all.setObjectName("PrimaryButton")
        apply_all.clicked.connect(self._apply_all_output_loads)
        impedance_layout.addWidget(apply_all, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(impedance)
        storage, storage_layout = panel("仪器状态")
        buttons = QHBoxLayout()
        state_commands = {
            "保存状态": "*SAV 1",
            "调用状态": "*RCL 1",
            "恢复默认": "*RST",
            "读取错误队列": ":SYST:ERR?",
        }
        for label, command in state_commands.items():
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, selected=command: self._execute_scpi(selected))
            buttons.addWidget(button)
        buttons.addStretch()
        storage_layout.addLayout(buttons)
        layout.addWidget(storage)
        console, console_layout = panel("SCPI 控制台")
        self.scpi_input = QLineEdit()
        self.scpi_input.setPlaceholderText("输入查询或命令，例如 *IDN?")
        self.scpi_input.returnPressed.connect(lambda: self._execute_scpi(self.scpi_input.text()))
        self.scpi_log = QPlainTextEdit()
        self.scpi_log.setReadOnly(True)
        self.scpi_log.setPlaceholderText("命令和返回值将在此显示")
        self.scpi_log.setMaximumBlockCount(1000)
        console_layout.addWidget(self.scpi_input)
        console_layout.addWidget(self.scpi_log)
        execute = QPushButton("执行")
        execute.setObjectName("PrimaryButton")
        execute.clicked.connect(lambda: self._execute_scpi(self.scpi_input.text()))
        console_layout.addWidget(execute, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(console, 1)
        return page

    def _execute_scpi(self, command: str) -> None:
        command = command.strip()
        if not command:
            return
        if not self._require_connection():
            return

        def operation() -> str:
            if command.endswith("?"):
                return self.driver.query(command)
            self.driver.write(command)
            return "已发送"

        def completed(result: Any) -> None:
            if hasattr(self, "scpi_log"):
                self.scpi_log.appendPlainText(f"> {command}\n{result}")
            else:
                self.statusBar().showMessage(f"{command} → {result}", 8000)
            if not command.endswith("?"):
                self._refresh_output_states()

        self.submit(operation, on_success=completed)

    def closeEvent(self, event: QCloseEvent) -> None:
        if hasattr(self, "virtual_generator_timer"):
            self.virtual_generator_timer.stop()
        if self.controller is not None:
            self.controller.stop()
            self.controller = None
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._trigger_lease_owner)
        if self.driver.is_connected and self.connection_manager is None:
            try:
                self.driver.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


class NIDeviceWindow(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        connection_manager: DeviceConnectionManager | None = None,
        parameter_repository: DeviceParameterRepository | None = None,
    ) -> None:
        self.probe_service = DeviceProbeService()
        self.connection_manager = connection_manager
        self._ni_lease_owner = f"ni-window:{record.id}"
        self.test_counter: NIDaqEdgeCounter | None = None
        self.counter_read_pending = False
        self.analog_controller: AnalogAcquisitionController | None = None
        self.analog_bridge = AnalogBridge()
        self.analog_bridge.updated.connect(self._analog_updated)
        self._last_analog_error: str | None = None
        self._ui_connected = False
        navigation = [
            ("总览", self._overview_page),
            ("触发输入", self._trigger_page),
            ("模拟输入", self._analog_input_page),
            ("计数器", self._counter_page),
            ("路由", self._routing_page),
            ("诊断", self._diagnostics_page),
        ]
        super().__init__(record, navigation, parameter_repository)
        if self.connection_manager is not None and self.connection_manager.is_connected(record):
            self._ni_connected(self.connection_manager.info(record))

    def sync_connection_state(self) -> None:
        if self.connection_manager is None:
            return
        connected = self.connection_manager.is_connected(self.record)
        if connected and not self._ui_connected:
            self._ni_connected(self.connection_manager.info(self.record))
        elif not connected and self._ui_connected:
            self._ni_disconnected()

    def _overview_page(self) -> QWidget:
        page, layout = page_shell(
            "NI 采集卡总览",
            "支持外部触发计数和独立模拟电压采集；不通过 AO 电压控制 DG4000 参数。",
        )
        metrics = QHBoxLayout()
        self.ni_overview_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("connection", "设备状态", "未连接", "#f0b95d"),
            ("trigger", "触发任务", "未运行", "#8995aa"),
            ("received", "累计触发", "0", "#8f7cff"),
            ("backlog", "缓冲积压", "0", "#55d9cc"),
        ):
            card, number = metric_card(caption, value, color)
            self.ni_overview_metrics[key] = number
            metrics.addWidget(card)
        layout.addLayout(metrics)
        info, info_layout = panel("设备信息")
        form = QFormLayout()
        form.addRow("NI 设备名", QLabel(self.record.resource))
        form.addRow("连接方式", QLabel(self.record.connection or "NI-DAQmx 本机设备"))
        form.addRow("用途", QLabel("外部边沿触发联动；独立模拟输入、绘图与 CSV 保存"))
        self.ni_identity = QLabel("检测后读取产品类型和序列号")
        self.ni_identity.setWordWrap(True)
        form.addRow("驱动报告", self.ni_identity)
        info_layout.addLayout(form)
        connection_buttons = QHBoxLayout()
        self.ni_connect_button = QPushButton("连接并读取能力")
        self.ni_connect_button.setObjectName("PrimaryButton")
        self.ni_disconnect_button = QPushButton("断开")
        self.ni_disconnect_button.setEnabled(False)
        self.ni_connect_button.clicked.connect(self._connect_ni)
        self.ni_disconnect_button.clicked.connect(self._disconnect_ni)
        connection_buttons.addWidget(self.ni_connect_button)
        connection_buttons.addWidget(self.ni_disconnect_button)
        connection_buttons.addStretch()
        info_layout.addLayout(connection_buttons)
        layout.addWidget(info)
        if is_virtual_resource(self.record.resource) and self.connection_manager is not None:
            virtual_panel, virtual_layout = panel("虚拟触发控制台")
            virtual_note = QLabel(
                "无需接线：先在 DG4000 的“触发步进”页启动任务，再从这里发送单次、批量或连续触发。"
            )
            virtual_note.setObjectName("Muted")
            virtual_note.setWordWrap(True)
            virtual_layout.addWidget(virtual_note)
            controls = QHBoxLayout()
            emit_one = QPushButton("发送 1 次触发")
            emit_one.setObjectName("PrimaryButton")
            emit_one.clicked.connect(lambda: self._emit_virtual_triggers(1))
            self.virtual_batch_count = QSpinBox()
            self.virtual_batch_count.setRange(1, 100000)
            self.virtual_batch_count.setValue(10)
            emit_batch = QPushButton("批量发送")
            emit_batch.clicked.connect(
                lambda: self._emit_virtual_triggers(self.virtual_batch_count.value())
            )
            controls.addWidget(emit_one)
            controls.addWidget(self.virtual_batch_count)
            controls.addWidget(emit_batch)
            controls.addStretch()
            virtual_layout.addLayout(controls)

            continuous = QHBoxLayout()
            self.virtual_interval = QSpinBox()
            self.virtual_interval.setRange(1, 60000)
            self.virtual_interval.setValue(100)
            self.virtual_interval.setSuffix(" ms/次")
            self.virtual_continuous_start = QPushButton("开始连续触发")
            self.virtual_continuous_stop = QPushButton("停止连续触发")
            self.virtual_continuous_stop.setEnabled(False)
            self.virtual_continuous_start.clicked.connect(self._start_virtual_continuous)
            self.virtual_continuous_stop.clicked.connect(self._stop_virtual_continuous)
            continuous.addWidget(self.virtual_interval)
            continuous.addWidget(self.virtual_continuous_start)
            continuous.addWidget(self.virtual_continuous_stop)
            continuous.addStretch()
            self.virtual_total_label = QLabel(
                f"累计发送：{self.connection_manager.virtual_lab.trigger_total(self.record.resource)}"
            )
            self.virtual_total_label.setStyleSheet("color:#55d9cc; font-weight:700;")
            continuous.addWidget(self.virtual_total_label)
            virtual_layout.addLayout(continuous)
            self.virtual_trigger_timer = QTimer(self)
            self.virtual_trigger_timer.timeout.connect(lambda: self._emit_virtual_triggers(1))
            layout.addWidget(virtual_panel)
        layout.addStretch()
        return page

    def _emit_virtual_triggers(self, count: int) -> None:
        if self.connection_manager is None:
            return
        if not self.connection_manager.is_connected(self.record):
            QMessageBox.information(self, "虚拟 NI 未连接", "请先连接虚拟 NI 设备。")
            return
        total = self.connection_manager.virtual_lab.emit_trigger(self.record.resource, count)
        self.virtual_total_label.setText(f"累计发送：{total}")

    def _start_virtual_continuous(self) -> None:
        if self.connection_manager is None:
            return
        if not self.connection_manager.is_connected(self.record):
            QMessageBox.information(self, "虚拟 NI 未连接", "请先连接虚拟 NI 设备。")
            return
        self.virtual_trigger_timer.setInterval(self.virtual_interval.value())
        self.virtual_trigger_timer.start()
        self.virtual_continuous_start.setEnabled(False)
        self.virtual_continuous_stop.setEnabled(True)

    def _stop_virtual_continuous(self) -> None:
        if hasattr(self, "virtual_trigger_timer"):
            self.virtual_trigger_timer.stop()
        if hasattr(self, "virtual_continuous_start"):
            self.virtual_continuous_start.setEnabled(True)
            self.virtual_continuous_stop.setEnabled(False)

    def _connect_ni(self) -> None:
        self.ni_connect_button.setEnabled(False)

        if self.connection_manager is not None:
            self.submit(
                lambda: self.connection_manager.connect(self.record),
                on_success=self._ni_connected,
                on_error=self._ni_connection_failed,
            )
            return

        def completed(result: ProbeResult) -> None:
            self.ni_identity.setText(result.identity or result.message)
            if result.state is ProbeState.ONLINE:
                self._ni_connected(ConnectionInfo(self.record.id, True, result.identity, result.message))
            else:
                self._ni_connection_failed(result.message)

        self.submit(lambda: self.probe_service.probe(self.record), on_success=completed)

    def _ni_connected(self, result: Any) -> None:
        identity = result.identity if isinstance(result, ConnectionInfo) else str(result)
        self.ni_identity.setText(identity or "NI-DAQmx 已识别设备")
        self.ni_connect_button.setEnabled(False)
        self.ni_connect_button.setText("已连接")
        self.ni_disconnect_button.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self._ui_connected = True
        self.ni_overview_metrics["connection"].setText("已连接")
        self.statusBar().showMessage("NI 设备已由 NI-DAQmx 识别", 5000)
        self._refresh_analog_channels()

    def _ni_connection_failed(self, message: str) -> None:
        self.ni_connect_button.setEnabled(True)
        self.ni_connect_button.setText("连接并读取能力")
        self._show_hardware_error(message)

    def _disconnect_ni(self) -> None:
        self.ni_disconnect_button.setEnabled(False)
        if self.test_counter is not None:
            self._stop_ni_counter()
        if self.analog_controller is not None and self.analog_controller.snapshot().state in {
            AnalogAcquisitionState.STARTING,
            AnalogAcquisitionState.RUNNING,
            AnalogAcquisitionState.ERROR,
        }:
            self.analog_controller.stop()
            self._analog_stopped()
        if self.connection_manager is None:
            self._ni_disconnected()
            return
        self.submit(
            lambda: self.connection_manager.disconnect(self.record),
            on_success=self._ni_disconnected,
        )

    def _ni_disconnected(self, _result: Any = None) -> None:
        self.ni_identity.setText("检测后读取产品类型和序列号")
        self.ni_connect_button.setText("连接并读取能力")
        self.ni_connect_button.setEnabled(True)
        self.ni_disconnect_button.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)
        self._ui_connected = False
        self.ni_overview_metrics["connection"].setText("未连接")
        self.statusBar().showMessage("NI 设备连接已释放", 4000)

    def _trigger_page(self) -> QWidget:
        page, layout = page_shell(
            "触发输入",
            "选择接收外部触发的 PFI 端子和有效边沿；模拟输入在独立页面配置。",
        )
        settings, settings_layout = panel("数字边沿")
        form = QFormLayout()
        terminal = QComboBox()
        terminal.setEditable(True)
        terminal.addItems([f"/{self.record.resource}/PFI{i}" for i in range(16)])
        edge = QComboBox()
        edge.addItems(["上升沿", "下降沿"])
        debounce = QCheckBox("启用 NI 硬件数字滤波（可选）")
        form.addRow("输入端子", terminal)
        form.addRow("有效边沿", edge)
        form.addRow("输入处理", debounce)
        settings_layout.addLayout(form)
        layout.addWidget(settings)
        note, note_layout = panel("触发规则")
        label = QLabel(
            "软件不设置最小触发间隔。硬件计数器保存实际收到的边沿总数，控制程序比较“已收到”和“已下发”计数，"
            "逐项补发全部参数；因此快速触发不会被软件节流，但 DG4000 的生效时刻可能落后于触发。"
        )
        label.setWordWrap(True)
        label.setObjectName("Muted")
        note_layout.addWidget(label)
        layout.addWidget(note)
        layout.addStretch()
        return page

    def _analog_input_page(self) -> QWidget:
        page, layout = page_shell(
            "模拟输入采集",
            "使用 NI-DAQmx 硬件定时连续采集电压；实时曲线用于监视，完整样本在采集过程中持续写入 CSV/TXT 文本。",
        )

        upper = QHBoxLayout()
        upper.setSpacing(14)
        settings, settings_layout = panel("采集配置")
        settings.setMinimumWidth(375)
        self.ai_config_tabs = QTabWidget()
        acquisition_tab = QWidget()
        form = QFormLayout(acquisition_tab)
        self.ai_channel_combo = CheckableComboBox()
        self.ai_channel_combo.checkedItemsChanged.connect(
            self._update_analog_channel_summary
        )
        default_channels = (
            tuple(f"{self.record.resource}/ai{index}" for index in range(4))
            if is_virtual_resource(self.record.resource)
            else tuple(f"{self.record.resource}/ai{index}" for index in range(8))
        )
        self._set_analog_channel_items(default_channels)
        channel_actions_widget = QWidget()
        channel_actions = QHBoxLayout(channel_actions_widget)
        channel_actions.setContentsMargins(0, 0, 0, 0)
        channel_actions.setSpacing(6)
        self.ai_channel_summary = QLabel()
        self.ai_channel_summary.setObjectName("Muted")
        self.ai_channels_all = QPushButton("全选")
        self.ai_channels_none = QPushButton("清空")
        self.ai_channels_all.clicked.connect(
            lambda: self._set_all_analog_channels(True)
        )
        self.ai_channels_none.clicked.connect(
            lambda: self._set_all_analog_channels(False)
        )
        channel_actions.addWidget(self.ai_channel_summary)
        channel_actions.addStretch()
        channel_actions.addWidget(self.ai_channels_all)
        channel_actions.addWidget(self.ai_channels_none)
        channel_note = QLabel(
            "下拉项可同时勾选多个 ai 端口；所有已选端口共享 NI 硬件采样时钟并行采集。"
        )
        channel_note.setObjectName("Muted")
        channel_note.setWordWrap(True)
        form.addRow("采集端口（可多选）", self.ai_channel_combo)
        form.addRow(channel_actions_widget)
        form.addRow(channel_note)
        self._update_analog_channel_summary()
        self.ai_terminal_mode = QComboBox()
        for label, value in (
            ("设备默认", "DEFAULT"),
            ("RSE 单端参考地", "RSE"),
            ("NRSE 非参考单端", "NRSE"),
            ("DIFF 差分", "DIFF"),
            ("伪差分", "PSEUDODIFF"),
        ):
            self.ai_terminal_mode.addItem(label, value)
        self.ai_sample_rate = unit_value_spin(
            10_000, 1, 5_000_000, "frequency", "kHz"
        )
        self.ai_minimum = unit_value_spin(-10, -100, 99.999999, "voltage", "V")
        self.ai_maximum = unit_value_spin(10, -99.999999, 100, "voltage", "V")
        self.ai_block_size = QSpinBox()
        self.ai_block_size.setRange(1, 1_000_000)
        self.ai_block_size.setValue(1000)
        self.ai_block_size.setSingleStep(100)
        self.ai_block_size.setSuffix(" 点/通道")
        self.ai_retained_samples = QSpinBox()
        self.ai_retained_samples.setRange(1_000, 5_000_000)
        self.ai_retained_samples.setValue(500_000)
        self.ai_retained_samples.setSingleStep(10_000)
        self.ai_retained_samples.setSuffix(" 点/通道")
        form.addRow("端接方式", self.ai_terminal_mode)
        form.addRow("采样率", self.ai_sample_rate)
        form.addRow("量程下限", self.ai_minimum)
        form.addRow("量程上限", self.ai_maximum)
        form.addRow("每次读取", self.ai_block_size)
        form.addRow("内存保留上限", self.ai_retained_samples)
        acquisition_note = QLabel(
            "采样率和每次读取点数均按每通道计算；全部勾选通道共享硬件采样时钟。"
        )
        acquisition_note.setObjectName("Muted")
        acquisition_note.setWordWrap(True)
        form.addRow(acquisition_note)
        self.ai_config_tabs.addTab(acquisition_tab, "采集")

        record_tab = QWidget()
        record_form = QFormLayout(record_tab)
        self.ai_stream_enabled = QCheckBox("采集时连续写入 CSV/TXT 文本")
        self.ai_stream_enabled.setChecked(True)
        self.ai_stream_path = QLineEdit(
            str(
                default_data_dir()
                / "acquisitions"
                / f"NI_AI_{datetime.now():%Y%m%d_%H%M%S}.csv"
            )
        )
        self.ai_stream_browse = QPushButton("选择…")
        self.ai_stream_browse.clicked.connect(self._browse_analog_stream_path)
        stream_path_row = QWidget()
        stream_path_layout = QHBoxLayout(stream_path_row)
        stream_path_layout.setContentsMargins(0, 0, 0, 0)
        stream_path_layout.setSpacing(6)
        stream_path_layout.addWidget(self.ai_stream_path, 1)
        stream_path_layout.addWidget(self.ai_stream_browse)
        record_form.addRow(self.ai_stream_enabled)
        record_form.addRow("连续文本文件", stream_path_row)
        stream_note = QLabel(
            "每个 NI 数据块立即追加并刷新到磁盘；内存上限只影响绘图窗口，不截断文本文件。"
        )
        stream_note.setObjectName("Muted")
        stream_note.setWordWrap(True)
        record_form.addRow(stream_note)
        record_columns_note = QLabel(
            "每个采样时刻占一行，每个已勾选通道占一个独立电压列。"
        )
        record_columns_note.setObjectName("Muted")
        record_columns_note.setWordWrap(True)
        record_form.addRow(record_columns_note)
        self.ai_config_tabs.addTab(record_tab, "记录")

        self.analog_config_widgets = [
            self.ai_channel_combo,
            self.ai_channels_all,
            self.ai_channels_none,
            self.ai_terminal_mode,
            self.ai_sample_rate,
            self.ai_minimum,
            self.ai_maximum,
            self.ai_block_size,
            self.ai_retained_samples,
            self.ai_stream_enabled,
            self.ai_stream_path,
            self.ai_stream_browse,
        ]

        if is_virtual_resource(self.record.resource):
            virtual_tab = QWidget()
            virtual_form = QFormLayout(virtual_tab)
            self.ai_virtual_frequency = unit_value_spin(
                50, 0, 1_000_000, "frequency", "Hz"
            )
            self.ai_virtual_amplitude = unit_value_spin(1, 0, 100, "voltage", "V")
            self.ai_virtual_offset = unit_value_spin(0, -100, 100, "voltage", "V")
            self.ai_virtual_noise = unit_value_spin(0.01, 0, 10, "voltage", "mV")
            virtual_form.addRow("正弦频率", self.ai_virtual_frequency)
            virtual_form.addRow("峰值幅度", self.ai_virtual_amplitude)
            virtual_form.addRow("直流偏置", self.ai_virtual_offset)
            virtual_form.addRow("噪声峰值", self.ai_virtual_noise)
            virtual_note = QLabel("各通道具有 45° 相位差，便于验证多通道绘图。")
            virtual_note.setObjectName("Muted")
            virtual_note.setWordWrap(True)
            virtual_form.addRow(virtual_note)
            self.ai_config_tabs.addTab(virtual_tab, "虚拟源")
            self.analog_config_widgets.extend(
                [
                    self.ai_virtual_frequency,
                    self.ai_virtual_amplitude,
                    self.ai_virtual_offset,
                    self.ai_virtual_noise,
                ]
            )
        settings_layout.addWidget(self.ai_config_tabs, 1)

        self.ai_start = QPushButton("开始采集")
        self.ai_start.setObjectName("PrimaryButton")
        self.ai_stop = QPushButton("停止")
        self.ai_stop.setEnabled(False)
        self.ai_clear = QPushButton("清空显示")
        self.ai_save_csv = QPushButton("导出缓存 CSV")
        self.ai_save_csv.setEnabled(False)
        self.ai_start.clicked.connect(self._start_analog_input)
        self.ai_stop.clicked.connect(self._stop_analog_input)
        self.ai_clear.clicked.connect(self._clear_analog_data)
        self.ai_save_csv.clicked.connect(self._save_analog_csv)
        action_row = QHBoxLayout()
        action_row.addWidget(self.ai_start)
        action_row.addWidget(self.ai_stop)
        action_row.addWidget(self.ai_clear)
        action_row.addWidget(self.ai_save_csv)
        settings_layout.addLayout(action_row)

        plot_panel, plot_layout = panel("实时波形")
        plot_scale_layout = QGridLayout()
        plot_scale_layout.setHorizontalSpacing(8)
        plot_scale_layout.setVerticalSpacing(6)
        self.ai_x_scale = unit_value_spin(
            0.02, 1e-6, 3600, "time", "ms"
        )
        self.ai_y_auto = QCheckBox("Y 轴自动")
        self.ai_y_auto.setChecked(True)
        self.ai_y_scale = unit_value_spin(
            0.5, 1e-9, 1000, "voltage", "V"
        )
        self.ai_y_center = unit_value_spin(
            0.0, -1000, 1000, "voltage", "V"
        )
        self.ai_plot_span = QLabel()
        self.ai_plot_span.setObjectName("Muted")
        plot_scale_layout.addWidget(QLabel("X 轴时基"), 0, 0)
        plot_scale_layout.addWidget(self.ai_x_scale, 0, 1, 1, 2)
        plot_scale_layout.addWidget(QLabel("/ 格"), 0, 3)
        plot_scale_layout.addWidget(self.ai_plot_span, 0, 4, 1, 3)
        plot_scale_layout.addWidget(self.ai_y_auto, 1, 0)
        plot_scale_layout.addWidget(QLabel("Y 轴标度"), 1, 1)
        plot_scale_layout.addWidget(self.ai_y_scale, 1, 2)
        plot_scale_layout.addWidget(QLabel("/ 格"), 1, 3)
        plot_scale_layout.addWidget(QLabel("中心"), 1, 4)
        plot_scale_layout.addWidget(self.ai_y_center, 1, 5, 1, 2)
        plot_scale_layout.setColumnStretch(6, 1)
        plot_layout.addLayout(plot_scale_layout)
        self.ai_plot = WaveformPlot()
        for control in (self.ai_x_scale, self.ai_y_scale, self.ai_y_center):
            control.spin.valueChanged.connect(self._update_analog_plot_scales)
            control.unit_combo.currentIndexChanged.connect(
                self._update_analog_plot_scales
            )
        self.ai_y_auto.toggled.connect(self._update_analog_plot_scales)
        self._update_analog_plot_scales()
        plot_layout.addWidget(self.ai_plot, 1)
        plot_actions = QHBoxLayout()
        self.ai_plot_status = QLabel("● 等待采集")
        self.ai_plot_status.setObjectName("StatusPending")
        self.ai_record_status = QLabel("连续文本记录：等待启动")
        self.ai_record_status.setObjectName("Muted")
        self.ai_record_status.setWordWrap(True)
        plot_actions.addWidget(self.ai_plot_status)
        plot_actions.addStretch()
        plot_actions.addWidget(self.ai_record_status)
        plot_layout.addLayout(plot_actions)
        upper.addWidget(settings, 1)
        upper.addWidget(plot_panel, 2)
        layout.addLayout(upper, 1)

        metrics = QHBoxLayout()
        self.ai_metrics: dict[str, QLabel] = {}
        for key, caption, value, color in (
            ("channels", "采集通道", "0", "#8f7cff"),
            ("samples", "每通道总点数", "0", "#55d9cc"),
            ("duration", "采集时长", "0 s", "#f0b95d"),
            ("retained", "保留 / 丢弃", "0 / 0", "#e7ebf4"),
            ("saved", "已写入文本", "0", "#54d6a3"),
        ):
            card, number = metric_card(caption, value, color)
            self.ai_metrics[key] = number
            metrics.addWidget(card)
        layout.addLayout(metrics)
        self.ai_statistics = QLabel("最近数据块统计：—")
        self.ai_statistics.setObjectName("Muted")
        self.ai_statistics.setWordWrap(True)
        layout.addWidget(self.ai_statistics)
        return page

    def _set_analog_channel_items(self, channel_names: tuple[str, ...]) -> None:
        if not hasattr(self, "ai_channel_combo"):
            return
        checked = set(self.ai_channel_combo.checked_data())
        self.ai_channel_combo.blockSignals(True)
        self.ai_channel_combo.clear()
        for index, channel in enumerate(channel_names):
            self.ai_channel_combo.add_checkable_item(
                channel,
                channel,
                checked=channel in checked or (not checked and index == 0),
            )
        self.ai_channel_combo.blockSignals(False)
        self.ai_channel_combo._refresh_summary()
        self._update_analog_channel_summary()

    def _checked_analog_channels(self) -> tuple[str, ...]:
        return tuple(str(channel) for channel in self.ai_channel_combo.checked_data())

    def _set_all_analog_channels(self, checked: bool) -> None:
        self.ai_channel_combo.set_all_checked(checked)

    def _update_analog_channel_summary(self) -> None:
        if not hasattr(self, "ai_channel_summary"):
            return
        self.ai_channel_summary.setText(
            f"已勾选 {len(self._checked_analog_channels())} / "
            f"{self.ai_channel_combo.count()} 路"
        )

    def _update_analog_plot_scales(self, _value: Any = None) -> None:
        if not hasattr(self, "ai_plot"):
            return
        seconds_per_division = self.ai_x_scale.value()
        automatic_y = self.ai_y_auto.isChecked()
        self.ai_y_scale.setEnabled(not automatic_y)
        self.ai_y_center.setEnabled(not automatic_y)
        self.ai_plot.set_time_scale(seconds_per_division)
        self.ai_plot.set_voltage_scale(
            None if automatic_y else self.ai_y_scale.value(),
            center_v=self.ai_y_center.value(),
        )
        total_seconds = seconds_per_division * self.ai_plot.HORIZONTAL_DIVISIONS
        if total_seconds < 0.001:
            span_text = f"{total_seconds * 1_000_000:.6g} µs"
        elif total_seconds < 1:
            span_text = f"{total_seconds * 1000:.6g} ms"
        else:
            span_text = f"{total_seconds:.6g} s"
        self.ai_plot_span.setText(
            f"显示最近 {span_text}（{self.ai_plot.HORIZONTAL_DIVISIONS} 格）"
        )

    def _refresh_analog_channels(self) -> None:
        if not hasattr(self, "ai_channel_combo"):
            return

        def completed(channels: Any) -> None:
            normalized = tuple(str(channel) for channel in channels)
            if normalized:
                self._set_analog_channel_items(normalized)
                self.statusBar().showMessage(
                    f"检测到 {len(normalized)} 个模拟输入通道", 4000
                )
            else:
                self._set_analog_channel_items(())
                self.statusBar().showMessage("该 NI 设备未报告模拟输入通道", 8000)

        if self.connection_manager is not None:
            operation = lambda: self.connection_manager.analog_channels_for(self.record)
        else:
            operation = lambda: NIDaqAnalogInput.available_channels(self.record.resource)
        self.submit(operation, on_success=completed, on_error=self._analog_channel_error)

    def _analog_channel_error(self, message: str) -> None:
        self.statusBar().showMessage(f"模拟输入通道检测失败：{message}", 8000)

    def _analog_config(self) -> AnalogInputConfig:
        config = AnalogInputConfig(
            channels=self._checked_analog_channels(),
            sample_rate_hz=self.ai_sample_rate.value(),
            samples_per_read=self.ai_block_size.value(),
            minimum_v=self.ai_minimum.value(),
            maximum_v=self.ai_maximum.value(),
            terminal_mode=self.ai_terminal_mode.currentData(),
        )
        config.validate()
        return config

    def _start_analog_input(self) -> None:
        connected = (
            self.connection_manager.is_connected(self.record)
            if self.connection_manager is not None
            else self.ni_disconnect_button.isEnabled()
        )
        if not connected:
            QMessageBox.information(self, "NI 尚未连接", "请先在总览页连接 NI 设备。")
            return
        try:
            if self.connection_manager is not None:
                self.connection_manager.acquire_devices(
                    self._ni_lease_owner, (self.record.id,)
                )
            config = self._analog_config()
            stream_path = (
                self._continuous_stream_destination()
                if self.ai_stream_enabled.isChecked()
                else None
            )
            if self.connection_manager is not None:
                reader = self.connection_manager.analog_input_for(
                    self.record,
                    config,
                    signal_frequency_hz=(
                        self.ai_virtual_frequency.value()
                        if hasattr(self, "ai_virtual_frequency")
                        else 50.0
                    ),
                    amplitude_v=(
                        self.ai_virtual_amplitude.value()
                        if hasattr(self, "ai_virtual_amplitude")
                        else 1.0
                    ),
                    offset_v=(
                        self.ai_virtual_offset.value()
                        if hasattr(self, "ai_virtual_offset")
                        else 0.0
                    ),
                    noise_v=(
                        self.ai_virtual_noise.value()
                        if hasattr(self, "ai_virtual_noise")
                        else 0.01
                    ),
                )
            else:
                reader = NIDaqAnalogInput(self.record.resource, config)
            controller = AnalogAcquisitionController(
                reader,
                maximum_retained_samples=self.ai_retained_samples.value(),
                stream_path=stream_path,
            )
        except Exception as exc:
            self._release_ni_lease_if_idle()
            QMessageBox.warning(self, "采集配置无效", str(exc))
            return

        controller.add_callback(self.analog_bridge.updated.emit)
        self.analog_controller = controller
        self._last_analog_error = None
        self.ai_plot.clear()
        self.ai_start.setEnabled(False)
        self.ai_start.setText("启动中…")
        self.ai_stop.setEnabled(False)
        self.ai_save_csv.setEnabled(False)
        self.ai_record_status.setText(
            f"连续文本记录：{stream_path}" if stream_path else "连续文本记录：已关闭"
        )
        self._set_analog_config_enabled(False)
        self.submit(
            controller.start,
            on_success=lambda _=None: self.statusBar().showMessage(
                "NI 模拟输入采集已启动", 4000
            ),
            on_error=self._analog_start_failed,
        )

    def _analog_start_failed(self, message: str) -> None:
        self.ai_start.setText("开始采集")
        self.ai_start.setEnabled(True)
        self.ai_stop.setEnabled(False)
        self._set_analog_config_enabled(True)
        self._release_ni_lease_if_idle()
        self._show_hardware_error(message)

    def _stop_analog_input(self) -> None:
        controller = self.analog_controller
        if controller is None:
            return
        self.ai_stop.setEnabled(False)
        self.submit(controller.stop, on_success=self._analog_stopped)

    def _analog_stopped(self, _result: Any = None) -> None:
        self.ai_start.setText("开始采集")
        self.ai_start.setEnabled(True)
        self.ai_stop.setEnabled(False)
        self._set_analog_config_enabled(True)
        controller = self.analog_controller
        has_data = bool(controller and controller.snapshot().retained_samples)
        self.ai_save_csv.setEnabled(has_data)
        self._release_ni_lease_if_idle()
        snapshot = controller.snapshot() if controller else None
        if snapshot and snapshot.stream_path:
            self.statusBar().showMessage(
                f"连续采集已停止，{snapshot.saved_samples} 点/通道已写入 {snapshot.stream_path}",
                8000,
            )
        else:
            self.statusBar().showMessage("NI 模拟输入采集已停止，内存数据仍保留", 4000)

    def _analog_updated(self, update: AnalogAcquisitionUpdate) -> None:
        snapshot = update.snapshot
        if update.block is not None:
            self.ai_plot.append_block(update.block)
        self.ai_metrics["channels"].setText(str(len(snapshot.channel_names)))
        self.ai_metrics["samples"].setText(str(snapshot.total_samples))
        self.ai_metrics["duration"].setText(f"{snapshot.duration_seconds:.6g} s")
        self.ai_metrics["retained"].setText(
            f"{snapshot.retained_samples} / {snapshot.dropped_samples}"
        )
        self.ai_metrics["saved"].setText(
            str(snapshot.saved_samples) if snapshot.stream_path else "关闭"
        )
        if snapshot.stream_path:
            record_state = "写入中" if snapshot.streaming else "已关闭"
            self.ai_record_status.setText(
                f"连续文本记录：{record_state} · {snapshot.saved_samples} 点/通道 · "
                f"{snapshot.stream_path}"
            )
        else:
            self.ai_record_status.setText("连续文本记录：已关闭")
        if snapshot.channel_names and snapshot.minimum_v:
            details = []
            for name, minimum, maximum, rms in zip(
                snapshot.channel_names,
                snapshot.minimum_v,
                snapshot.maximum_v,
                snapshot.rms_v,
            ):
                details.append(
                    f"{name.split('/')[-1]}: min {minimum:.6g} V, "
                    f"max {maximum:.6g} V, RMS {rms:.6g} V"
                )
            self.ai_statistics.setText("最近数据块统计：" + "；".join(details))

        state_text = {
            AnalogAcquisitionState.STOPPED: "● 已停止",
            AnalogAcquisitionState.STARTING: "● 启动中",
            AnalogAcquisitionState.RUNNING: "● 正在采集",
            AnalogAcquisitionState.STOPPING: "● 正在停止",
            AnalogAcquisitionState.ERROR: "● 采集错误",
        }[snapshot.state]
        self.ai_plot_status.setText(state_text)
        online = snapshot.state is AnalogAcquisitionState.RUNNING
        self.ai_plot_status.setObjectName(
            "StatusOnline"
            if online
            else (
                "StatusOffline"
                if snapshot.state is AnalogAcquisitionState.ERROR
                else "StatusPending"
            )
        )
        self.ai_plot_status.style().unpolish(self.ai_plot_status)
        self.ai_plot_status.style().polish(self.ai_plot_status)
        self.ai_stop.setEnabled(online)
        if snapshot.state is AnalogAcquisitionState.RUNNING:
            self.ai_start.setText("采集中")
        elif snapshot.state is AnalogAcquisitionState.ERROR:
            self.ai_start.setText("开始采集")
            self.ai_start.setEnabled(True)
            self._set_analog_config_enabled(True)
            self._release_ni_lease_if_idle()
        self.ai_save_csv.setEnabled(
            snapshot.retained_samples > 0
            and snapshot.state not in {
                AnalogAcquisitionState.STARTING,
                AnalogAcquisitionState.RUNNING,
                AnalogAcquisitionState.STOPPING,
            }
        )
        if snapshot.error and snapshot.error != self._last_analog_error:
            self._last_analog_error = snapshot.error
            self.statusBar().showMessage(snapshot.error, 10000)

    def _set_analog_config_enabled(self, enabled: bool) -> None:
        for widget in self.analog_config_widgets:
            widget.setEnabled(enabled)
        self.ai_clear.setEnabled(enabled)

    def _clear_analog_data(self) -> None:
        controller = self.analog_controller
        saved_samples = 0
        has_stream = False
        if controller is not None:
            try:
                controller.clear()
            except RuntimeError as exc:
                QMessageBox.information(self, "采集正在运行", str(exc))
                return
            snapshot = controller.snapshot()
            saved_samples = snapshot.saved_samples
            has_stream = bool(snapshot.stream_path)
        self.ai_plot.clear()
        self.ai_metrics["channels"].setText("0")
        self.ai_metrics["samples"].setText("0")
        self.ai_metrics["duration"].setText("0 s")
        self.ai_metrics["retained"].setText("0 / 0")
        self.ai_metrics["saved"].setText(str(saved_samples) if has_stream else "关闭")
        self.ai_statistics.setText("最近数据块统计：—")
        self.ai_save_csv.setEnabled(False)

    def _browse_analog_stream_path(self) -> None:
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "选择连续采集文本文件",
            self.ai_stream_path.text().strip(),
            "CSV 文本 (*.csv);;TXT 文本 (*.txt)",
        )
        if path:
            self.ai_stream_path.setText(path)

    def _continuous_stream_destination(self) -> Path:
        raw = self.ai_stream_path.text().strip()
        if not raw:
            raise ValueError("启用连续文本记录时必须选择保存文件")
        destination = Path(raw)
        if destination.suffix.lower() not in {".csv", ".txt"}:
            destination = destination.with_suffix(destination.suffix + ".csv")
        if destination.exists():
            stem = destination.stem
            suffix = destination.suffix
            for index in range(2, 10_000):
                candidate = destination.with_name(f"{stem}_{index}{suffix}")
                if not candidate.exists():
                    destination = candidate
                    break
            else:
                raise ValueError("无法为连续采集创建不重复的文件名")
        self.ai_stream_path.setText(str(destination))
        return destination

    def _save_analog_csv(self) -> None:
        controller = self.analog_controller
        if controller is None or controller.snapshot().retained_samples == 0:
            QMessageBox.information(self, "没有数据", "请先完成一次模拟输入采集。")
            return
        default_name = f"NI_AI_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "保存模拟输入数据",
            str(Path.cwd() / default_name),
            "CSV 数据 (*.csv)",
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != ".csv":
            destination = destination.with_suffix(destination.suffix + ".csv")
        self.ai_save_csv.setEnabled(False)
        self.submit(
            lambda: controller.save_csv(destination),
            on_success=lambda result: self._analog_csv_saved(result),
            on_error=lambda message: (
                self.ai_save_csv.setEnabled(True),
                self._show_hardware_error(message),
            ),
        )

    def _analog_csv_saved(self, path: Any) -> None:
        self.ai_save_csv.setEnabled(True)
        self.statusBar().showMessage(f"模拟输入数据已保存：{path}", 8000)

    def _counter_page(self) -> QWidget:
        page, layout = page_shell("计数器", "为联动任务选择硬件边沿计数器并查看单调递增的触发总数。")
        settings, settings_layout = panel("计数任务")
        form = QFormLayout()
        counter = QComboBox()
        counter.addItems([f"{self.record.resource}/ctr{i}" for i in range(4)])
        terminal = QComboBox()
        terminal.setEditable(True)
        terminal.addItems([f"/{self.record.resource}/PFI{i}" for i in range(16)])
        edge = QComboBox()
        edge.addItems(["上升沿", "下降沿"])
        sample_mode = QComboBox()
        sample_mode.addItems(["连续硬件计数", "按需读取累计值"])
        form.addRow("计数器", counter)
        form.addRow("PFI 端子", terminal)
        form.addRow("有效边沿", edge)
        form.addRow("读取方式", sample_mode)
        settings_layout.addLayout(form)
        count_card, self.ni_count_label = metric_card("当前累计边沿", "0", "#8f7cff")
        settings_layout.addWidget(count_card)
        controls = QHBoxLayout()
        start = QPushButton("开始计数")
        start.setObjectName("PrimaryButton")
        self.ni_counter_start = start
        self.ni_counter_stop = QPushButton("停止")
        self.ni_counter_stop.setEnabled(False)
        start.clicked.connect(lambda: self._start_ni_counter(counter, terminal, edge))
        self.ni_counter_stop.clicked.connect(self._stop_ni_counter)
        controls.addWidget(start)
        controls.addWidget(self.ni_counter_stop)
        controls.addStretch()
        settings_layout.addLayout(controls)
        self.ni_counter_timer = QTimer(self)
        self.ni_counter_timer.setInterval(100)
        self.ni_counter_timer.timeout.connect(self._read_ni_counter)
        layout.addWidget(settings)
        layout.addStretch()
        return page

    def _start_ni_counter(self, counter: QComboBox, terminal: QComboBox, edge: QComboBox) -> None:
        if self.test_counter is not None:
            return
        try:
            if self.connection_manager is not None:
                self.connection_manager.acquire_devices(
                    self._ni_lease_owner, (self.record.id,)
                )
            self.test_counter = (
                self.connection_manager.counter_for(
                    self.record,
                    counter=counter.currentText(),
                    terminal=terminal.currentText(),
                    rising=edge.currentText() == "上升沿",
                )
                if self.connection_manager is not None
                else NIDaqEdgeCounter(
                    self.record.resource,
                    counter=counter.currentText(),
                    terminal=terminal.currentText(),
                    rising=edge.currentText() == "上升沿",
                )
            )
        except Exception as exc:
            self.test_counter = None
            self._release_ni_lease_if_idle()
            QMessageBox.warning(self, "无法创建计数任务", str(exc))
            return
        self.ni_counter_start.setEnabled(False)

        def started(_result: Any = None) -> None:
            self.ni_counter_stop.setEnabled(True)
            self.ni_counter_timer.start()
            self.ni_overview_metrics["trigger"].setText("运行中")
            self.statusBar().showMessage("NI 累计边沿计数已启动", 4000)

        def failed(message: str) -> None:
            self.test_counter = None
            self.ni_counter_start.setEnabled(True)
            self.ni_overview_metrics["trigger"].setText("错误")
            self._release_ni_lease_if_idle()
            self._show_hardware_error(message)

        self.submit(self.test_counter.start, on_success=started, on_error=failed)

    def _read_ni_counter(self) -> None:
        if self.test_counter is None or self.counter_read_pending:
            return
        self.counter_read_pending = True

        def received(value: Any) -> None:
            self.counter_read_pending = False
            self.ni_count_label.setText(str(value))
            self.ni_overview_metrics["received"].setText(str(value))

        def failed(message: str) -> None:
            self.counter_read_pending = False
            self.statusBar().showMessage(message, 8000)

        self.submit(self.test_counter.read_count, on_success=received, on_error=failed)

    def _stop_ni_counter(self) -> None:
        counter, self.test_counter = self.test_counter, None
        if counter is None:
            return
        self.ni_counter_timer.stop()
        self.ni_counter_stop.setEnabled(False)

        def stopped(_result: Any = None) -> None:
            self.ni_counter_start.setEnabled(True)
            self.ni_overview_metrics["trigger"].setText("未运行")
            self.ni_overview_metrics["backlog"].setText("0")
            self._release_ni_lease_if_idle()
            self.statusBar().showMessage("NI 计数已停止", 4000)

        self.submit(counter.stop, on_success=stopped)

    def _release_ni_lease_if_idle(self) -> None:
        if self.connection_manager is None or self.test_counter is not None:
            return
        analog_busy = False
        if self.analog_controller is not None:
            analog_busy = self.analog_controller.snapshot().state in {
                AnalogAcquisitionState.STARTING,
                AnalogAcquisitionState.RUNNING,
                AnalogAcquisitionState.STOPPING,
            }
        if not analog_busy:
            self.connection_manager.release_devices(self._ni_lease_owner)

    def _routing_page(self) -> QWidget:
        page, layout = page_shell("端子与路由", "记录外部触发接线并检查 PFI/计数器端子映射。")
        routes, routes_layout = panel("建议接线")
        text = QLabel(
            "外部触发源 → NI PFI 输入；NI 与触发源共地。DG4000 通过 USB/LAN 接收软件 SCPI 命令，"
            "无需将 NI AO 接到 DG4000，也无需将 DG4000 输出接回 NI。"
        )
        text.setWordWrap(True)
        routes_layout.addWidget(text)
        layout.addWidget(routes)
        layout.addStretch()
        return page

    def _diagnostics_page(self) -> QWidget:
        page, layout = page_shell("诊断", "检查 NI-DAQmx 安装、设备枚举、端子能力与最近的计数错误。")
        diagnostics, diagnostics_layout = panel("本机环境")
        checks = QLabel("○ NI-DAQmx Python 包\n○ NI-DAQmx 系统驱动\n○ 设备名匹配\n○ PFI/计数器资源可用")
        checks.setObjectName("Muted")
        diagnostics_layout.addWidget(checks)
        run = QPushButton("运行诊断")
        run.setObjectName("PrimaryButton")
        run.clicked.connect(self._connect_ni)
        diagnostics_layout.addWidget(run, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(diagnostics)
        layout.addStretch()
        return page

    def closeEvent(self, event: QCloseEvent) -> None:
        self._stop_virtual_continuous()
        if self.analog_controller is not None:
            try:
                self.analog_controller.stop()
            except Exception:
                pass
        if self.test_counter is not None:
            self.ni_counter_timer.stop()
            try:
                self.test_counter.stop()
            except Exception:
                pass
            self.test_counter = None
        if self.connection_manager is not None:
            self.connection_manager.release_devices(self._ni_lease_owner)
        super().closeEvent(event)


class GenericScpiWindow(BaseDeviceWindow):
    def __init__(
        self,
        record: DeviceRecord,
        connection_manager: DeviceConnectionManager | None = None,
        parameter_repository: DeviceParameterRepository | None = None,
    ) -> None:
        self.connection_manager = connection_manager
        self.session = (
            connection_manager.session_for(record)
            if connection_manager is not None
            else ScpiSession(record.resource)
        )
        super().__init__(
            record,
            [("SCPI 控制台", self._console_page)],
            parameter_repository,
        )
        if self.session.is_connected:
            identity = (
                self.connection_manager.info(record).identity
                if self.connection_manager is not None
                else self.session.query("*IDN?")
            )
            self._generic_connected(identity)

    def _console_page(self) -> QWidget:
        page, layout = page_shell("通用 SCPI 设备", "连接 VISA 资源并手动发送命令或查询。")
        connection, connection_layout = panel("VISA 连接")
        row = QHBoxLayout()
        self.generic_status = QLabel("● 未连接")
        self.generic_status.setObjectName("StatusPending")
        self.generic_connect = QPushButton("连接并读取 *IDN?")
        self.generic_connect.setObjectName("PrimaryButton")
        self.generic_connect.clicked.connect(self._generic_connect)
        self.generic_disconnect = QPushButton("断开")
        self.generic_disconnect.setEnabled(False)
        self.generic_disconnect.clicked.connect(self._generic_disconnect)
        row.addWidget(self.generic_status)
        row.addStretch()
        row.addWidget(self.generic_connect)
        row.addWidget(self.generic_disconnect)
        connection_layout.addLayout(row)
        layout.addWidget(connection)
        console, console_layout = panel("命令控制台")
        self.generic_input = QLineEdit()
        self.generic_input.setPlaceholderText("例如 *IDN?")
        self.generic_input.returnPressed.connect(self._generic_send)
        self.generic_log = QPlainTextEdit()
        self.generic_log.setReadOnly(True)
        console_layout.addWidget(self.generic_input)
        console_layout.addWidget(self.generic_log)
        send = QPushButton("发送")
        send.setObjectName("PrimaryButton")
        send.clicked.connect(self._generic_send)
        console_layout.addWidget(send, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(console, 1)
        return page

    def _generic_connect(self) -> None:
        self.generic_connect.setEnabled(False)

        def failed(message: str) -> None:
            self.generic_connect.setEnabled(True)
            self._show_hardware_error(message)

        self.submit(
            lambda: (
                self.connection_manager.connect(self.record)
                if self.connection_manager is not None
                else self.session.connect()
            ),
            on_success=self._generic_connected,
            on_error=failed,
        )

    def _generic_connected(self, identity: Any) -> None:
        identity_text = identity.identity if isinstance(identity, ConnectionInfo) else str(identity)
        self.generic_status.setText("● 已连接")
        self.generic_status.setObjectName("StatusOnline")
        self.generic_status.style().unpolish(self.generic_status)
        self.generic_status.style().polish(self.generic_status)
        if identity_text:
            self.generic_log.appendPlainText(identity_text)
        self.generic_connect.setText("已连接")
        self.generic_connect.setEnabled(False)
        self.generic_disconnect.setEnabled(True)
        self.driver_status.setObjectName("StatusOnline")
        self.driver_status.setText("● 已连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)

    def _generic_disconnect(self) -> None:
        self.generic_disconnect.setEnabled(False)
        self.submit(
            lambda: (
                self.connection_manager.disconnect(self.record)
                if self.connection_manager is not None
                else self.session.disconnect()
            ),
            on_success=self._generic_disconnected,
        )

    def _generic_disconnected(self, _result: Any = None) -> None:
        self.generic_status.setText("● 未连接")
        self.generic_status.setObjectName("StatusPending")
        self.generic_status.style().unpolish(self.generic_status)
        self.generic_status.style().polish(self.generic_status)
        self.generic_connect.setText("连接并读取 *IDN?")
        self.generic_connect.setEnabled(True)
        self.generic_disconnect.setEnabled(False)
        self.driver_status.setObjectName("StatusPending")
        self.driver_status.setText("● 驱动尚未连接")
        self.driver_status.style().unpolish(self.driver_status)
        self.driver_status.style().polish(self.driver_status)

    def _generic_send(self) -> None:
        command = self.generic_input.text().strip()
        if not command:
            return
        if not self.session.is_connected:
            QMessageBox.information(self, "尚未连接", "请先连接 VISA 资源。")
            return

        def operation() -> str:
            if command.endswith("?"):
                return self.session.query(command)
            self.session.write(command)
            return "已发送"

        self.submit(
            operation,
            on_success=lambda result: self.generic_log.appendPlainText(f"> {command}\n{result}"),
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.session.is_connected and self.connection_manager is None:
            try:
                self.session.disconnect()
            except Exception:
                pass
        super().closeEvent(event)


def create_device_window(
    record: DeviceRecord,
    all_devices: list[DeviceRecord],
    connection_manager: DeviceConnectionManager | None = None,
    device_provider: Callable[[], list[DeviceRecord]] | None = None,
    parameter_repository: DeviceParameterRepository | None = None,
) -> BaseDeviceWindow:
    if record.kind is DeviceKind.DG4000:
        ni_devices = [item for item in all_devices if item.kind is DeviceKind.NI_DAQ]
        return DG4000Window(
            record,
            ni_devices,
            connection_manager,
            device_provider=device_provider,
            parameter_repository=parameter_repository,
        )
    if record.kind is DeviceKind.NI_DAQ:
        return NIDeviceWindow(record, connection_manager, parameter_repository)
    if record.kind is DeviceKind.VAUNIX_LMS:
        from .vaunix_window import VaunixLmsWindow

        ni_devices = [item for item in all_devices if item.kind is DeviceKind.NI_DAQ]
        return VaunixLmsWindow(
            record,
            ni_devices,
            connection_manager,
            device_provider=device_provider,
            parameter_repository=parameter_repository,
        )
    return GenericScpiWindow(record, connection_manager, parameter_repository)
