from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
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
        timeout_ms: int = 5000,
        recovery_attempts: int = 1,
        backend_factory: BackendFactory = VisaBackend,
    ) -> None:
        self.resource = resource
        self.timeout_ms = timeout_ms
        self.recovery_attempts = 1 if int(recovery_attempts) > 0 else 0
        self._backend_factory = backend_factory
        self._backend: ScpiBackend | None = None
        self._identity = ""
        self._lock = RLock()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._backend is not None

    @property
    def identity(self) -> str:
        with self._lock:
            return self._identity

    def connect(self) -> str:
        with self._lock:
            if self._backend is not None:
                self._identity = self._query_locked("*IDN?")
                return self._identity
            self._backend = self._backend_factory(self.resource, self.timeout_ms)
            try:
                self._identity = self._query_locked("*IDN?")
                return self._identity
            except Exception:
                self._close_locked()
                raise

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def write(self, command: str, *, recover: bool = False) -> None:
        normalized = self._normalize(command)
        with self._lock:
            self._write_locked(normalized, recover=recover)

    @contextmanager
    def transaction(self):
        """Serialize a related write/readback sequence on the shared session."""
        with self._lock:
            yield self

    def write_many(self, commands: Iterable[str], *, recover: bool = False) -> None:
        normalized = [self._normalize(command) for command in commands]
        with self._lock:
            for command in normalized:
                self._write_locked(command, recover=recover, batch=True)

    def query(self, command: str, *, recover: bool = False) -> str:
        normalized = self._normalize(command)
        with self._lock:
            try:
                result = self._query_locked(normalized)
            except ScpiError as first_error:
                if not recover or self.recovery_attempts < 1:
                    raise
                self._recover_locked(normalized, first_error)
                try:
                    result = self._query_locked(normalized)
                except ScpiError as retry_error:
                    raise ScpiError(
                        f"SCPI 查询在自动重连后仍失败 [{normalized}]：{retry_error}"
                    ) from retry_error
            if normalized.upper() == "*IDN?":
                self._identity = result
            return result

    def _write_locked(
        self,
        command: str,
        *,
        recover: bool,
        batch: bool = False,
    ) -> None:
        backend = self._require_backend()
        try:
            backend.write(command)
            return
        except Exception as exc:
            label = "批量写入" if batch else "写入"
            first_exception = exc
            first_error = ScpiError(f"SCPI {label}失败 [{command}]：{exc}")
        if not recover or self.recovery_attempts < 1:
            raise first_error from first_exception
        self._recover_locked(command, first_error)
        try:
            self._require_backend().write(command)
        except Exception as retry_exc:
            raise ScpiError(
                f"SCPI 写入在自动重连后仍失败 [{command}]：{retry_exc}"
            ) from retry_exc

    def _recover_locked(self, command: str, first_error: Exception) -> None:
        old_backend, self._backend = self._backend, None
        if old_backend is not None:
            try:
                old_backend.close()
            except Exception:
                pass
        try:
            backend = self._backend_factory(self.resource, self.timeout_ms)
            self._backend = backend
            identity = str(backend.query("*IDN?")).strip()
            if not identity:
                raise ScpiConnectionError("自动重连后 *IDN? 返回为空")
            self._identity = identity
        except Exception as recovery_exc:
            self._close_locked_safely()
            raise ScpiConnectionError(
                f"SCPI 通信失败 [{command}]，自动重连也失败：{recovery_exc}；"
                f"原始错误：{first_error}"
            ) from recovery_exc

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
        self._identity = ""
        if backend is not None:
            try:
                backend.close()
            except Exception as exc:
                raise ScpiError(f"关闭 VISA 会话失败：{exc}") from exc

    def _close_locked_safely(self) -> None:
        backend, self._backend = self._backend, None
        self._identity = ""
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    @staticmethod
    def _normalize(command: str) -> str:
        normalized = command.strip()
        if not normalized:
            raise ValueError("SCPI 命令不能为空")
        if "\n" in normalized or "\r" in normalized:
            raise ValueError("单条 SCPI 命令不能包含换行")
        return normalized
