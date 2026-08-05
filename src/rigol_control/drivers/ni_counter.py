from __future__ import annotations

from threading import RLock


class NIDaqError(RuntimeError):
    pass


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
                task.close()
                raise NIDaqError(f"无法启动 NI 边沿计数：{exc}") from exc
            self._task = task

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
            task, self._task = self._task, None
            if task is None:
                return
            try:
                task.stop()
            except Exception:
                pass
            finally:
                task.close()
