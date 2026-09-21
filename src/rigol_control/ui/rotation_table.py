"""Read-only scan plan with progress scoped to the current rotation cycle."""
from __future__ import annotations

from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PyQt6.QtGui import QColor

from ..rotation_scan import RotationSnapshot


class RotationPointTableModel(QAbstractTableModel):
    """Keep one tuple of angles; a 10,000-point plan needs no cell widgets.

    ``None`` angles represent a preview whose initial position has not been read.
    A changed plan clears old progress. Reapplying the same plan preserves it;
    call ``set_progress(None)`` to explicitly return to the preview.
    """

    HEADERS = ("点位", "目标角度 (°)", "相对 P0 (°)", "状态")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._points: tuple[float | None, ...] = ()
        self._step_deg = 0.0
        self._progress: RotationSnapshot | None = None

    @property
    def active_row(self) -> int | None:
        return self._active_row(self._progress)

    def _active_row(self, snapshot: RotationSnapshot | None) -> int | None:
        if (snapshot is not None and snapshot.target_deg is not None
                and 0 <= snapshot.point_index < len(self._points)):
            return snapshot.point_index
        return None

    def _completed_row(self, snapshot: RotationSnapshot | None) -> int:
        if (snapshot is None or snapshot.target_deg is None or not self._points
                or snapshot.completed // len(self._points) != snapshot.cycle):
            return -1
        return snapshot.completed % len(self._points)

    def set_plan(self, points: tuple[float | None, ...], step_deg: float) -> None:
        points = tuple(points)
        if points == self._points and step_deg == self._step_deg:
            return
        self.beginResetModel()
        self._points = points
        self._step_deg = step_deg
        self._progress = None
        self.endResetModel()

    @staticmethod
    def _display_key(snapshot: RotationSnapshot | None):
        if snapshot is None:
            return None
        # Actual position, elapsed time and received edges do not change a plan
        # cell. In particular, received edges do not prove successful motion.
        return (snapshot.phase, snapshot.point_index, snapshot.completed,
                snapshot.cycle, snapshot.target_deg, snapshot.error)

    def set_progress(self, snapshot: RotationSnapshot | None) -> None:
        previous = self._progress
        self._progress = snapshot
        if self._display_key(previous) == self._display_key(snapshot) or not self._points:
            return
        old_done, new_done = self._completed_row(previous), self._completed_row(snapshot)
        changed = {self._active_row(previous), self._active_row(snapshot)} - {None}
        if (previous is None or snapshot is None or previous.phase == "preparing"
                or snapshot.phase == "preparing"):
            changed.add(0)
        if old_done != new_done:
            # This expands over the old cycle on a return to P0, and normally
            # contains only the point that has just completed.
            low, high = min(old_done, new_done) + 1, max(old_done, new_done)
            changed.update((low, high))
        if not changed:
            return
        first, last = min(changed), max(changed)
        self.dataChanged.emit(self.index(first, 0), self.index(last, 3),
                              [Qt.ItemDataRole.DisplayRole,
                               Qt.ItemDataRole.ForegroundRole,
                               Qt.ItemDataRole.BackgroundRole,
                               Qt.ItemDataRole.ToolTipRole])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._points)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
            if orientation == Qt.Orientation.Vertical and 0 <= section < len(self._points):
                return str(section + 1)
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def _status(self, row: int) -> str:
        s = self._progress
        if s is None:
            return "起点 / 启动时保持" if row == 0 else "待触发"
        if row == self.active_row:
            if s.phase == "holding":
                return "当前保持 / 等待触发"
            if s.phase == "moving":
                return "回跳中" if row == 0 and s.cycle > 0 else "移动中"
            if s.phase == "settling":
                return "到位 / 稳定等待"
            if s.phase == "error":
                return "错误停止"
            if s.phase == "stopped":
                return ("已停止 / 已到达" if row <= self._completed_row(s)
                        else "已停止 / 未确认到位")
            return "准备中"
        if row <= self._completed_row(s):
            return "本圈已到达"
        if row == 0 and s.phase == "preparing":
            return "准备中"
        return "待触发"

    @staticmethod
    def _angle(value: float | None) -> str:
        if value is None:
            return "—"
        return f"{0.0 if abs(value) < 0.0000005 else value:.6f}"

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._points)
                                      and 0 <= index.column() < len(self.HEADERS)):
            return None
        row, column = index.row(), index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 0:
                return f"P{row}"
            if column == 1:
                return self._angle(self._points[row])
            if column == 2:
                return self._angle(row * self._step_deg)
            return self._status(row)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return (Qt.AlignmentFlag.AlignVCenter
                    | (Qt.AlignmentFlag.AlignRight if column in (1, 2) else Qt.AlignmentFlag.AlignLeft))
        if role == Qt.ItemDataRole.BackgroundRole and row == self.active_row:
            return QColor("#432630" if self._progress.phase == "error" else "#183b47")
        if role == Qt.ItemDataRole.ForegroundRole:
            if row == self.active_row:
                return QColor({"error": "#ff7d9c", "moving": "#f0b95d",
                               "settling": "#f0b95d"}.get(self._progress.phase, "#6aeadc"))
            if row <= self._completed_row(self._progress):
                return QColor("#8fbcb7")
        if role == Qt.ItemDataRole.ToolTipRole:
            if row == self.active_row and self._progress.error:
                return self._progress.error
            if self._points[row] is None:
                return "读取当前位置后显示绝对角度；开始扫描时会重新读取 P0。"
            return ("启动时保持 P0；首个触发移动至 P1；末点的下一次触发回跳 P0。\n"
                    "已到达标记只对应本圈，当前位置以控制器实际回读为准。")
        return None
