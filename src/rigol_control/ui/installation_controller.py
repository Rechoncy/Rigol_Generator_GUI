"""Gate automatic installation on idle instruments and an accepted clean close."""
from __future__ import annotations

from PyQt6.QtCore import QObject, QThreadPool, QTimer
from PyQt6.QtWidgets import QApplication, QAbstractSpinBox

from ..install_update import cancel_handoff, commit_handoff, prepare_handoff
from .widgets import CallTask


def installation_busy_reason(window) -> str:
    if (window._transfer_task is not None or window.probe_tasks or window.connection_tasks
            or window._pending_connections):
        return "正在处理设备连接、探测或配置"
    manager = window.connection_manager
    if manager.active_leases() or manager.active_transitions():
        return "设备操作或任务尚未结束"
    children = list(window.open_windows.values())
    if window.linkage_window is not None:
        children.append(window.linkage_window)
    for child in children:
        if (getattr(child, "_call_tasks", None) or getattr(child, "_manual_busy", False)
                or getattr(child, "_manual_cleanup_pending", False)):
            return "设备后台操作或停止清理尚未完成"
        for name in ("controller", "analog_controller"):
            controller = getattr(child, name, None)
            if controller is not None:
                if getattr(controller, "is_running", False):
                    return "扫描或采集仍在运行"
                quiescent = getattr(controller, "is_quiescent", True)
                if not quiescent:
                    return "任务尚未完全停止"
                snapshot = getattr(controller, "snapshot", None)
                if callable(snapshot):
                    state = getattr(getattr(snapshot(), "state", None), "value", "")
                    if state in {"starting", "running", "stopping"}:
                        return "任务尚未完全停止"
        if getattr(child, "test_counter", None) is not None:
            return "计数任务仍在运行"
        for name in ("virtual_generator_timer", "virtual_continuous_timer"):
            timer = getattr(child, name, None)
            if timer is not None and timer.isActive():
                return "连续测试输出仍在运行"
    return ""


class InstallationController(QObject):
    def __init__(self, window) -> None:
        super().__init__(window)
        self.window = window
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._try_start)
        self.pending = None
        self.automatic = False
        self.preparing = False
        self.committing = False
        self.task = None
        self._closed = False
        window.update_panel.install_requested.connect(self.request)

    def request(self, request) -> None:
        if self._closed or self.preparing:
            return
        self.pending = request
        self.automatic = self.window.update_panel.install_request_automatic
        self._try_start()

    def _status(self, message: str, *, retry: bool = True) -> None:
        self.window.update_panel.set_install_status(message, retry=retry)

    def _try_start(self) -> None:
        if self._closed or self.preparing or self.pending is None:
            return
        if self.automatic and not self.window.update_panel.automatic_install.isChecked():
            self.pending = None
            self.timer.stop()
            self._status("自动安装已关闭，更新包保留，可稍后手动安装。")
            return
        reason = installation_busy_reason(self.window)
        if reason:
            self._status(reason + ("；空闲后继续自动安装，可取消自动安装以停止等待。" if self.automatic
                                 else "；停止任务后请再次点击安装更新。"))
            if self.automatic:
                self.timer.start()
            else:
                self.pending = None
                self.timer.stop()
            return
        self.timer.stop()
        path, asset = self.pending
        self.preparing = True
        self._status("正在重新校验安装包并准备升级…", retry=False)
        task = CallTask(lambda: prepare_handoff(path, asset, self.window.repository.data_dir))
        self.task = task
        task.signals.succeeded.connect(self._prepared)
        task.signals.failed.connect(self._failed)
        try:
            self.pool.start(task)
        except Exception as exc:
            self._failed(str(exc))

    def _failed(self, message: str) -> None:
        self.preparing = False
        self.task = None
        self.pending = None
        self.timer.stop()
        self._status(f"安装准备未完成：{message}。更新包已保留，可手动重试。")

    def _prepared(self, handoff) -> None:
        self.preparing = False
        self.task = None
        if self._closed:
            cancel_handoff(handoff)
            return
        try:
            focus = QApplication.focusWidget()
            if focus is not None:
                focus.clearFocus()
            for child in self.window.open_windows.values():
                for editor in child.findChildren(QAbstractSpinBox):
                    editor.interpretText()
            self.window._flush_device_parameters()
        except Exception as exc:
            cancel_handoff(handoff)
            self.pending = None
            self._status(f"参数保存未完成，升级已取消：{exc}。请保留当前窗口处理后重试。")
            return
        reason = installation_busy_reason(self.window)
        if reason or (self.automatic and not self.window.update_panel.automatic_install.isChecked()):
            cancel_handoff(handoff)
            if self.automatic and self.window.update_panel.automatic_install.isChecked():
                self.timer.start()
                self._status(reason + "；已撤回升级准备，等待任务完成。")
            else:
                self.pending = None
                self._status("已取消本次自动安装，更新包保留。")
            return
        app = QApplication.instance()
        previous_quit = app.quitOnLastWindowClosed()
        app.setQuitOnLastWindowClosed(False)
        self.committing = True
        try:
            if not self.window.close():
                cancel_handoff(handoff)
                self.pending = None
                self._status("窗口尚不能安全关闭，升级已取消；任务结束后可重试。")
                return
            # The helper needs BOTH parent exit and this explicit confirmation.
            # A crash, denied close, or failed preparation never authorizes it.
            commit_handoff(handoff)
            self.pending = None
            app.quit()
        except Exception as exc:
            cancel_handoff(handoff)
            self.window.show()
            self.window.connection_timer.start()
            self.window.statusBar().showMessage(f"升级未开始：{exc}。请重新启动应用后再试。", 30000)
        finally:
            self.committing = False
            app.setQuitOnLastWindowClosed(previous_quit)

    def on_window_closed(self) -> None:
        if self.committing:
            return
        self._closed = True
        self.timer.stop()
        self.pending = None
