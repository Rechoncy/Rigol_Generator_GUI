from __future__ import annotations

from collections.abc import Callable, Iterable
from threading import RLock
from typing import Protocol


class ScpiError(RuntimeError):
    pass


class ScpiConnectionError(ScpiError):
    pass


class ScpiBackend(Protocol):
    def write(self, command: str) -> object: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, int], ScpiBackend]


class VisaBackend:
    """Thin lazy PyVISA adapter, isolated so the application can run without it."""

    def __init__(self, resource: str, timeout_ms: int) -> None:
        try:
            import pyvisa  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ScpiConnectionError(
                "未安装 PyVISA；请执行 python -m pip install -e \".[instrument]\""
            ) from exc
        try:
            self._manager = pyvisa.ResourceManager()
            self._instrument = self._manager.open_resource(resource, open_timeout=timeout_ms)
            self._instrument.timeout = timeout_ms
            self._instrument.write_termination = "\n"
            self._instrument.read_termination = "\n"
        except Exception as exc:
            manager = getattr(self, "_manager", None)
            if manager is not None:
                manager.close()
            raise ScpiConnectionError(f"无法打开 VISA 资源 {resource}：{exc}") from exc

    def write(self, command: str) -> object:
        return self._instrument.write(command)

    def query(self, command: str) -> str:
        return str(self._instrument.query(command))

    def close(self) -> None:
        try:
            self._instrument.close()
        finally:
            self._manager.close()


class ScpiSession:
    """One serialized SCPI session shared by UI commands and trigger stepping."""

    def __init__(
        self,
        resource: str,
        *,
        timeout_ms: int = 2000,
        backend_factory: BackendFactory = VisaBackend,
    ) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self._backend_factory = backend_factory
        self._backend: ScpiBackend | None = None
        self._lock = RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._backend is not None

    def connect(self) -> str:
        with self._lock:
            if self._backend is not None:
                return self._query_locked("*IDN?")
            self._backend = self._backend_factory(self.resource, self.timeout_ms)
            try:
                return self._query_locked("*IDN?")
            except Exception:
                self._close_locked()
                raise

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def write(self, command: str) -> None:
        normalized = self._normalize(command)
        with self._lock:
            backend = self._require_backend()
            try:
                backend.write(normalized)
            except Exception as exc:
                raise ScpiError(f"SCPI 写入失败 [{normalized}]：{exc}") from exc

    def write_many(self, commands: Iterable[str]) -> None:
        normalized = [self._normalize(command) for command in commands]
        with self._lock:
            backend = self._require_backend()
            for command in normalized:
                try:
                    backend.write(command)
                except Exception as exc:
                    raise ScpiError(f"SCPI 批量写入失败 [{command}]：{exc}") from exc

    def query(self, command: str) -> str:
        normalized = self._normalize(command)
        with self._lock:
            return self._query_locked(normalized)

    def _query_locked(self, command: str) -> str:
        backend = self._require_backend()
        try:
            return str(backend.query(command)).strip()
        except Exception as exc:
            raise ScpiError(f"SCPI 查询失败 [{command}]：{exc}") from exc

    def _require_backend(self) -> ScpiBackend:
        if self._backend is None:
            raise ScpiConnectionError("VISA 会话尚未连接")
        return self._backend

    def _close_locked(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:
                raise ScpiError(f"关闭 VISA 会话失败：{exc}") from exc

    @staticmethod
    def _normalize(command: str) -> str:
        normalized = command.strip()
        if not normalized:
            raise ValueError("SCPI 命令不能为空")
        if "\n" in normalized or "\r" in normalized:
            raise ValueError("单条 SCPI 命令不能包含换行")
        return normalized

