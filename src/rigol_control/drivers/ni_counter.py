from __future__ import annotations

from threading import RLock


class NIDaqError(RuntimeError):
    pass


class NIDaqCleanupUncertainError(NIDaqError):
    pass


def _close_ni_task(task) -> None:
    try:
        task.close()
    except Exception as exc:
        # nidaqmx 1.6 clears Task._handle even if native clear_task fails.
        # A second close would silently return, not confirm resource release.
        # Never restore/replay that lost handle: it may already be reused.
        if getattr(task, "_handle", True) is None:
            raise NIDaqCleanupUncertainError(
                "NI 原生任务清理未确认，Python 任务已失去原生句柄；已保留设备占用。"
                "请在 NI MAX 核对设备状态后退出并重新启动软件。原始错误：" + str(exc)
            ) from exc
        raise


class NIDaqEdgeCounter:
    """Hardware cumulative edge counter; polling never defines a trigger dead time."""

    def __init__(
        self,
        device_name: str,
        *,
        counter: str = "ctr0",
        terminal: str | None = None,
        rising: bool = True,
    ) -> None:
        self.device_name = device_name.strip().strip("/")
        self.counter = counter.strip().split("/")[-1]
        self.terminal = terminal
        self.rising = rising
        self._task = None
        self._cleanup_uncertain: str | None = None
        self._lock = RLock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._task is not None

    def start(self) -> None:
        with self._lock:
            if self._task is not None:
                raise NIDaqError("NI 计数任务已经运行")
            try:
                import nidaqmx  # type: ignore[import-not-found]
                from nidaqmx.constants import Edge  # type: ignore[import-not-found]
            except ImportError as exc:
                raise NIDaqError(
                    "未安装 nidaqmx；请先安装 NI-DAQmx 驱动及 Python 包"
                ) from exc
            task = nidaqmx.Task(new_task_name="DG4000_Trigger_Edge_Count")
            self._task = task
            self._cleanup_uncertain = None
            try:
                channel = task.ci_channels.add_ci_count_edges_chan(
                    f"{self.device_name}/{self.counter}",
                    edge=Edge.RISING if self.rising else Edge.FALLING,
                    initial_count=0,
                )
                if self.terminal:
                    terminal = self.terminal.strip()
                    if not terminal.startswith("/"):
                        terminal = f"/{self.device_name}/{terminal.split('/')[-1]}"
                    channel.ci_count_edges_term = terminal
                task.start()
            except Exception as exc:
                try:
                    _close_ni_task(task)
                except NIDaqCleanupUncertainError as cleanup_exc:
                    self._cleanup_uncertain = str(cleanup_exc)
                    raise NIDaqError(f"无法启动 NI 边沿计数：{exc}；{cleanup_exc}") from exc
                self._task = None
                raise NIDaqError(f"无法启动 NI 边沿计数：{exc}") from exc

    def read_count(self) -> int:
        with self._lock:
            if self._task is None:
                raise NIDaqError("NI 计数任务尚未启动")
            try:
                value = self._task.read(number_of_samples_per_channel=1)
                if isinstance(value, list):
                    value = value[0]
                return int(value)
            except Exception as exc:
                raise NIDaqError(f"读取 NI 累计触发数失败：{exc}") from exc

    def stop(self) -> None:
        with self._lock:
            task = self._task
            if task is None:
                return
            if self._cleanup_uncertain is not None:
                raise NIDaqCleanupUncertainError(self._cleanup_uncertain)
            try:
                task.stop()
            except Exception:
                pass
            finally:
                try:
                    _close_ni_task(task)
                except NIDaqCleanupUncertainError as exc:
                    self._cleanup_uncertain = str(exc)
                    raise
            # Retain the task if close raises, so cleanup can be retried and
            # the caller cannot start a second task over an unconfirmed one.
            self._task = None
