"""Shared shutdown rules for cumulative-counter task controllers."""
from __future__ import annotations

import math
import time
from threading import Lock, RLock, current_thread


class ControllerLifecycle:
    def _init_lifecycle(self) -> None:
        self._start_thread = None
        self._counter_open = False
        self._counter_close_lock = Lock()
        self._shutdown_lock = RLock()

    def _quiescent_locked(self) -> bool:
        return (self._start_thread is None and not self._counter_open
                and not any(t is not None and t.is_alive()
                            for t in (self._monitor_thread, self._writer_thread)))

    @property
    def is_quiescent(self) -> bool:
        """True only after startup, workers and NI cleanup have finished."""
        with self._condition:
            return self._quiescent_locked()

    def start(self) -> None:
        state = type(self._state)
        with self._shutdown_lock, self._condition:
            if (not self._quiescent_locked()
                    or self._state in {state.STARTING, state.RUNNING, state.STOPPING}):
                raise RuntimeError("上一次任务仍在启动、下发或停止，请完成停止后再启动")
            self._start_thread = current_thread()
            self._state = state.STARTING
            self._stop_event.clear()
        try:
            self._start_run()
        except Exception as exc:
            if self._state is not state.ERROR:
                self._fail(f"启动任务失败：{exc}")
            raise
        finally:
            with self._condition:
                self._start_thread = None
                self._condition.notify_all()
            self._publish()

    def _open_counter(self) -> None:
        # An unsuccessful start can also leave a partially opened NI task.
        with self._condition:
            self._counter_open = True
        self.counter.start()

    def _close_counter(self) -> None:
        with self._counter_close_lock:
            with self._condition:
                opened = self._counter_open
            if opened:
                self.counter.stop()
                with self._condition:
                    self._counter_open = False

    def request_stop(self) -> None:
        """Signal cancellation without joining or doing instrument I/O."""
        with self._condition:
            if self._state is type(self._state).STOPPED:
                self._cancel_before_start = True
            self._stop_event.set()
            self._condition.notify_all()

    def stop(self, timeout_seconds: float = 3.0) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("停止等待时间必须是非负有限数值")
        state = type(self._state)
        with self._shutdown_lock:
            with self._condition:
                if self._state is state.STOPPED and self._quiescent_locked():
                    return
                if self._state is not state.ERROR:
                    self._state = state.STOPPING
                self._stop_event.set()
                self._condition.notify_all()
                startup = self._start_thread
            self._publish()
            deadline = time.monotonic() + timeout_seconds
            # Qt's QThreadPool enters Python through a _DummyThread, which
            # cannot be joined. Startup completion is our own condition flag.
            with self._condition:
                if startup is not None and startup is not current_thread():
                    self._condition.wait_for(
                        lambda: self._start_thread is None,
                        timeout=max(0.0, deadline - time.monotonic()),
                    )
                threads = (self._monitor_thread, self._writer_thread)
            for thread in threads:
                if thread is not None and thread is not current_thread() and thread.is_alive():
                    thread.join(max(0.0, deadline - time.monotonic()))
            with self._condition:
                pending = [t.name for t in threads if t is not None and t.is_alive()]
                if self._start_thread is not None:
                    pending.insert(0, "启动检查")
            if pending:
                self._publish()
                raise RuntimeError("停止尚未完成，后台操作仍在执行：" + "、".join(pending)
                                   + "；已保留设备占用，请等待后重试停止")
            try:
                self._close_counter()
            except Exception as exc:
                with self._condition:
                    self._state = state.ERROR
                    self._error = self._error or f"关闭 NI 计数任务失败：{exc}"
                self._publish()
                raise RuntimeError("NI 计数任务尚未确认关闭，已保留设备占用，请重试停止") from exc
            with self._condition:
                if self._state is not state.ERROR:
                    self._state = state.STOPPED
                self._monitor_thread = self._writer_thread = None
            self._publish()
