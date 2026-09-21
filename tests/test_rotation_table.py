import os
import time
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QModelIndex, Qt
from PyQt6.QtTest import QSignalSpy
from PyQt6.QtWidgets import QApplication

from rigol_control.rotation_scan import RotationSnapshot
from rigol_control.ui.rotation_table import RotationPointTableModel


class RotationTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["rotation-table-test"])

    def setUp(self):
        self.model = RotationPointTableModel()
        self.model.set_plan((-5.2, -5.1, -5.0), 0.1)

    def value(self, row, column=3):
        return self.model.data(self.model.index(row, column))

    def progress(self, phase, *, index=0, completed=0, cycle=0, **kwargs):
        snapshot = RotationSnapshot(phase=phase, point_index=index, completed=completed,
                                    cycle=cycle, target_deg=(-5.2, -5.1, -5.0)[index], **kwargs)
        self.model.set_progress(snapshot)
        return snapshot

    def test_unknown_initial_position_retains_negative_step_preview(self):
        self.model.set_plan((None,) * 3, -0.05)
        self.assertEqual(self.value(0, 1), "—")
        self.assertEqual(self.value(2, 1), "—")
        self.assertEqual(self.value(2, 2), "-0.100000")
        self.assertEqual(self.value(0, 2), "0.000000")
        self.assertEqual(self.value(0), "起点 / 启动时保持")
        self.assertIsNone(self.model.active_row)

    def test_known_angles_and_read_only_structure(self):
        self.assertEqual(self.value(0, 1), "-5.200000")
        self.assertEqual(self.value(2, 2), "0.200000")
        self.assertEqual(self.model.rowCount(), 3)
        self.assertEqual(self.model.columnCount(), 4)
        self.assertEqual(self.model.rowCount(self.model.index(0, 0)), 0)
        self.assertEqual(self.model.columnCount(self.model.index(0, 0)), 0)
        self.assertEqual(self.model.headerData(1, Qt.Orientation.Horizontal), "目标角度 (°)")
        self.assertEqual(self.model.flags(QModelIndex()), Qt.ItemFlag.NoItemFlags)
        self.assertFalse(self.model.flags(self.model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable)
        self.assertIsNone(self.model.data(QModelIndex()))

    def test_target_is_not_reported_as_arrived_while_moving_or_settling(self):
        self.progress("holding")
        self.assertEqual(self.value(0), "当前保持 / 等待触发")
        self.assertEqual(self.value(1), "待触发")
        self.progress("moving", index=1, received=1)
        self.assertEqual(self.model.active_row, 1)
        self.assertEqual(self.value(0), "本圈已到达")
        self.assertEqual(self.value(1), "移动中")
        self.progress("settling", index=1, received=1)
        self.assertEqual(self.value(1), "到位 / 稳定等待")
        self.progress("holding", index=1, completed=1, received=1)
        self.assertEqual(self.value(1), "当前保持 / 等待触发")
        self.assertEqual(self.value(2), "待触发")

    def test_return_to_p0_starts_new_cycle_without_old_completion_marks(self):
        self.progress("holding", index=2, completed=2)
        self.assertEqual(self.value(1), "本圈已到达")
        self.progress("moving", index=0, completed=2, cycle=1, received=3)
        self.assertEqual(self.value(0), "回跳中")
        self.assertEqual([self.value(i) for i in (1, 2)], ["待触发", "待触发"])
        self.progress("holding", index=0, completed=3, cycle=1, received=3)
        self.assertEqual(self.value(0), "当前保持 / 等待触发")
        self.assertEqual([self.value(i) for i in (1, 2)], ["待触发", "待触发"])
        self.progress("moving", index=1, completed=3, cycle=1, received=4)
        self.assertEqual(self.value(0), "本圈已到达")
        self.assertEqual(self.value(1), "移动中")
        self.assertEqual(self.value(2), "待触发")

    def test_stop_and_error_do_not_claim_unconfirmed_arrival(self):
        self.progress("stopped", index=1, completed=0)
        self.assertEqual(self.value(1), "已停止 / 未确认到位")
        self.progress("stopped", index=1, completed=1)
        self.assertEqual(self.value(1), "已停止 / 已到达")
        self.progress("stopped", index=0, completed=2, cycle=1)
        self.assertEqual(self.value(0), "已停止 / 未确认到位")
        self.progress("error", index=1, completed=0, error="连接中断")
        self.assertEqual(self.value(1), "错误停止")
        self.assertEqual(self.model.data(self.model.index(1, 3), Qt.ItemDataRole.ToolTipRole), "连接中断")

    def test_preparing_without_readback_has_no_active_angle(self):
        self.model.set_progress(RotationSnapshot(phase="preparing"))
        self.assertIsNone(self.model.active_row)
        self.assertEqual(self.value(0), "准备中")
        self.assertEqual(self.value(1), "待触发")

    def test_changed_plan_and_explicit_preview_clear_old_run(self):
        self.progress("stopped", index=2, completed=2)
        reset_spy = QSignalSpy(self.model.modelReset)
        self.model.set_plan((-5.2, -5.1, -5.0), 0.1)
        self.assertEqual(len(reset_spy), 0)
        self.assertEqual(self.model.active_row, 2)
        self.model.set_plan((1.0, 0.9, 0.8), -0.1)
        self.assertEqual(len(reset_spy), 1)
        self.assertIsNone(self.model.active_row)
        self.assertEqual(self.value(0, 1), "1.000000")
        self.assertEqual(self.value(2), "待触发")
        self.progress("holding")
        self.model.set_progress(None)
        self.assertIsNone(self.model.active_row)
        self.assertEqual(self.value(0), "起点 / 启动时保持")

    def test_large_plan_ignores_high_frequency_readbacks_without_model_reset(self):
        points = tuple(i * 0.001 for i in range(10000))
        self.model.set_plan(points, 0.001)
        reset_spy = QSignalSpy(self.model.modelReset)
        change_spy = QSignalSpy(self.model.dataChanged)
        snapshot = RotationSnapshot(phase="moving", received=5000, completed=4999,
                                    point_index=5000, target_deg=5.0)
        self.model.set_progress(snapshot)
        changes = len(change_spy)
        start = time.perf_counter()
        for n in range(10000):
            self.model.set_progress(replace(snapshot, actual_deg=n / 2000, elapsed_ms=n))
        self.assertLess(time.perf_counter() - start, 5.0)
        self.assertEqual(len(reset_spy), 0)
        self.assertEqual(len(change_spy), changes)
        self.assertEqual(self.model.rowCount(), 10000)
        self.assertEqual(self.value(9999, 0), "P9999")
        self.assertEqual(self.value(9999, 1), "9.999000")
        self.assertEqual(self.value(4999), "本圈已到达")
        self.assertEqual(self.value(5001), "待触发")
        self.model.set_progress(replace(snapshot, phase="holding", completed=5000))
        self.assertEqual(len(change_spy), changes + 1)


if __name__ == "__main__":
    unittest.main()
