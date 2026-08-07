from __future__ import annotations

import math
from collections import deque
from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPaintEvent, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from ..drivers.ni_analog import AnalogInputBlock


class WaveformPlot(QWidget):
    """Dependency-free real-time waveform plot with bounded display history."""

    HORIZONTAL_DIVISIONS = 5
    VERTICAL_DIVISIONS = 4

    COLORS = (
        QColor("#55d9cc"),
        QColor("#8f7cff"),
        QColor("#f0b95d"),
        QColor("#ff7d9c"),
        QColor("#6db8ff"),
        QColor("#a8dc6c"),
        QColor("#ff9f5a"),
        QColor("#cf8cff"),
    )

    def __init__(self, parent: QWidget | None = None, *, maximum_points: int = 50_000) -> None:
        super().__init__(parent)
        self.maximum_points = maximum_points
        self.channel_names: tuple[str, ...] = ()
        self.sample_rate_hz = 0.0
        self.first_sample_index = 0
        self._buffers: tuple[deque[float], ...] = ()
        self.seconds_per_division = 0.02
        self.volts_per_division: float | None = None
        self.voltage_center_v = 0.0
        self.setMinimumHeight(240)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setToolTip(
            "实时显示最近时间窗口；X/Y 标度只影响绘图，连续文本仍逐块保存全部原始数据。"
        )

    @property
    def displayed_samples(self) -> int:
        return len(self._buffers[0]) if self._buffers else 0

    @property
    def visible_samples(self) -> int:
        if self.sample_rate_hz <= 0:
            return self.displayed_samples
        requested = max(
            2,
            int(
                math.ceil(
                    self.seconds_per_division
                    * self.HORIZONTAL_DIVISIONS
                    * self.sample_rate_hz
                )
            ),
        )
        return min(self.displayed_samples, requested)

    def set_time_scale(self, seconds_per_division: float) -> None:
        value = float(seconds_per_division)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("X 轴时基必须是大于 0 的有限值")
        self.seconds_per_division = value
        self.update()

    def set_voltage_scale(
        self,
        volts_per_division: float | None,
        *,
        center_v: float = 0.0,
    ) -> None:
        center = float(center_v)
        if not math.isfinite(center):
            raise ValueError("Y 轴中心必须是有限值")
        if volts_per_division is None:
            self.volts_per_division = None
        else:
            scale = float(volts_per_division)
            if not math.isfinite(scale) or scale <= 0:
                raise ValueError("Y 轴标度必须是大于 0 的有限值")
            self.volts_per_division = scale
        self.voltage_center_v = center
        self.update()

    def clear(self) -> None:
        self.channel_names = ()
        self.sample_rate_hz = 0.0
        self.first_sample_index = 0
        self._buffers = ()
        self.update()

    def append_block(self, block: AnalogInputBlock) -> None:
        if block.channel_names != self.channel_names or not math.isclose(
            block.sample_rate_hz, self.sample_rate_hz
        ):
            self.channel_names = block.channel_names
            self.sample_rate_hz = block.sample_rate_hz
            self._buffers = tuple(
                deque(maxlen=self.maximum_points) for _ in block.channel_names
            )
        for buffer, values in zip(self._buffers, block.samples):
            buffer.extend(values)
        retained = self.displayed_samples
        self.first_sample_index = block.first_sample_index + block.sample_count - retained
        self.update()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor("#0b1320"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        plot = QRectF(90, 22, max(10, self.width() - 110), max(10, self.height() - 64))
        painter.fillRect(plot, QColor("#0f1927"))
        self._draw_grid(painter, plot)
        if not self._buffers or self.displayed_samples < 2:
            painter.setPen(QColor("#7f8ca2"))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "等待模拟输入数据…")
            return

        visible_count = self.visible_samples
        visible_buffers = tuple(list(buffer)[-visible_count:] for buffer in self._buffers)
        if self.volts_per_division is None:
            all_values = [value for values in visible_buffers for value in values]
            minimum = min(all_values)
            maximum = max(all_values)
            if math.isclose(minimum, maximum):
                padding = max(abs(minimum) * 0.1, 0.5)
            else:
                padding = (maximum - minimum) * 0.08
            minimum -= padding
            maximum += padding
        else:
            half_span = self.volts_per_division * self.VERTICAL_DIVISIONS / 2
            minimum = self.voltage_center_v - half_span
            maximum = self.voltage_center_v + half_span
        self._draw_axis_labels(
            painter,
            plot,
            minimum,
            maximum,
            visible_count,
        )

        painter.save()
        painter.setClipRect(plot)
        for index, values in enumerate(visible_buffers):
            self._draw_trace(
                painter,
                plot,
                values,
                minimum,
                maximum,
                self.COLORS[index % len(self.COLORS)],
            )
        painter.restore()
        self._draw_legend(painter, plot)

    def _draw_grid(self, painter: QPainter, plot: QRectF) -> None:
        painter.setPen(QPen(QColor("#253249"), 1))
        for index in range(6):
            x = plot.left() + plot.width() * index / 5
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        for index in range(5):
            y = plot.top() + plot.height() * index / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
        painter.setPen(QPen(QColor("#40506a"), 1))
        painter.drawRect(plot)

    def _draw_axis_labels(
        self,
        painter: QPainter,
        plot: QRectF,
        minimum: float,
        maximum: float,
        visible_count: int,
    ) -> None:
        painter.setPen(QColor("#9aa8bc"))
        for index in range(5):
            fraction = index / 4
            value = maximum - (maximum - minimum) * fraction
            y = plot.top() + plot.height() * fraction
            painter.drawText(
                QRectF(22, y - 9, 62, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{value:.4g}",
            )
        first_visible_index = (
            self.first_sample_index + self.displayed_samples - visible_count
        )
        start_seconds = first_visible_index / self.sample_rate_hz
        end_seconds = (self.first_sample_index + self.displayed_samples - 1) / self.sample_rate_hz
        painter.drawText(
            QRectF(plot.left(), plot.bottom() + 7, plot.width() / 2, 20),
            Qt.AlignmentFlag.AlignLeft,
            f"{start_seconds:.6g} s",
        )
        painter.drawText(
            QRectF(plot.center().x(), plot.bottom() + 7, plot.width() / 2, 20),
            Qt.AlignmentFlag.AlignRight,
            f"{end_seconds:.6g} s",
        )
        painter.save()
        painter.translate(9, plot.center().y())
        painter.rotate(-90)
        painter.drawText(QRectF(-50, -10, 100, 20), Qt.AlignmentFlag.AlignCenter, "电压 (V)")
        painter.restore()

    def _draw_trace(
        self,
        painter: QPainter,
        plot: QRectF,
        values: list[float],
        minimum: float,
        maximum: float,
        color: QColor,
    ) -> None:
        painter.setPen(QPen(color, 1.25))
        count = len(values)
        pixels = max(1, int(plot.width()))

        def y(value: float) -> float:
            return plot.bottom() - (value - minimum) / (maximum - minimum) * plot.height()

        if count <= pixels * 2:
            path = QPainterPath()
            path.moveTo(plot.left(), y(values[0]))
            denominator = max(1, count - 1)
            for index, value in enumerate(values[1:], start=1):
                x = plot.left() + plot.width() * index / denominator
                path.lineTo(x, y(value))
            painter.drawPath(path)
            return

        for pixel in range(pixels):
            start = pixel * count // pixels
            stop = max(start + 1, (pixel + 1) * count // pixels)
            bucket = values[start:stop]
            x = plot.left() + pixel
            painter.drawLine(QPointF(x, y(min(bucket))), QPointF(x, y(max(bucket))))

    def _draw_legend(self, painter: QPainter, plot: QRectF) -> None:
        x = plot.left() + 8
        y = plot.top() + 7
        for index, name in enumerate(self.channel_names):
            color = self.COLORS[index % len(self.COLORS)]
            painter.fillRect(QRectF(x, y + 4, 12, 3), color)
            painter.setPen(QColor("#dce5f2"))
            label = name.split("/")[-1]
            painter.drawText(QPointF(x + 17, y + 11), label)
            x += 24 + painter.fontMetrics().horizontalAdvance(label)
