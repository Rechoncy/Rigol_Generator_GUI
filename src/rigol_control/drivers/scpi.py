from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import contextmanager
import math
import re
from threading import Lock, RLock
import time
from typing import Protocol


class ScpiError(RuntimeError):
    pass


class ScpiConnectionError(ScpiError):
    pass


class ScpiResponseSyncError(ScpiError):
    """A pending reply cannot safely be associated with a subsequent query."""


class ScpiBackend(Protocol):
    def write(self, command: str) -> object: ...

    def query(self, command: str) -> str: ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, int], ScpiBackend]


class VisaBackend:
    """Thin lazy PyVISA adapter, isolated so the application can run without it."""

    MAX_EMPTY_RESPONSE_FRAMES = 16
    _NONEMPTY_QUERY = re.compile(
        r"(?:\*(?:IDN|OPC)|COUP:STAT|OUTP[12](?::STAT)?|"
        r"SOUR[12]:(?:FREQ|FUNC|VOLT:UNIT|"
        r"VOLT(?::LEV)?(?::IMM)?(?::(?:AMPL|HIGH|LOW))?))\?"
    )
    _HEADER_ALIASES = {
        "FREQUENCY": "FREQ", "FUNCTION": "FUNC", "VOLTAGE": "VOLT",
        "LEVEL": "LEV", "IMMEDIATE": "IMM", "AMPLITUDE": "AMPL",
        "COUPLING": "COUP", "STATE": "STAT",
    }

    def __init__(self, resource: str, timeout_ms: int) -> None:
        try:
            timeout = float(timeout_ms)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError()
            self._query_timeout_ms = math.ceil(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ScpiConnectionError("VISA 查询超时必须为正数毫秒") from exc
        self._reply_unsynchronized = False
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
        if self._reply_unsynchronized:
            raise ScpiResponseSyncError("VISA 回复关联未确认；请显式断开并重新连接后再查询")
        if not self._requires_nonempty_response(command):
            # Unknown console queries can legitimately return an empty string.
            return str(self._instrument.query(command))

        deadline = time.monotonic() + self._query_timeout_ms / 1000
        original_timeout = self._instrument.timeout
        read_cap = self._query_timeout_ms
        try:
            original_ms = float(original_timeout)
            if math.isfinite(original_ms) and original_ms > 0:
                read_cap = min(read_cap, original_ms)
        except (TypeError, ValueError, OverflowError):
            pass
        attempted = failed = False
        try:
            self._instrument.timeout = max(1, int(read_cap))
            attempted = True
            # PyVISA query performs one write, its configured query_delay and
            # one read. A terminator-only frame can make that read empty.
            response = str(self._instrument.query(command))
            empty_frames = 0
            while not response.strip():
                empty_frames += 1
                remaining = deadline - time.monotonic()
                if empty_frames >= self.MAX_EMPTY_RESPONSE_FRAMES:
                    raise ScpiResponseSyncError(
                        f"VISA 查询连续收到空回复帧 [{command}]；回复关联未确认，请断开重连"
                    )
                if remaining <= 0:
                    raise ScpiResponseSyncError(
                        f"VISA 查询等待有效回复超时 [{command}]；回复关联未确认，请断开重连"
                    )
                self._instrument.timeout = max(1, int(min(read_cap, remaining * 1000)))
                try:
                    # Continue the pending response; never send the query again
                    # or discard a nonempty value in search of a preferred type.
                    response = str(self._instrument.read())
                except Exception as exc:
                    raise ScpiResponseSyncError(
                        f"VISA 查询空帧后的回复未读完 [{command}]；请断开重连：{exc}"
                    ) from exc
            return response
        except Exception:
            failed = True
            if attempted:
                self.mark_response_unsynchronized()
            raise
        finally:
            try:
                self._instrument.timeout = original_timeout
            except Exception:
                self.mark_response_unsynchronized()
                if not failed:
                    raise

    @classmethod
    def _requires_nonempty_response(cls, command: str) -> bool:
        normalized = command.strip().upper().removeprefix(":")
        if not normalized.endswith("?"):
            return False
        headers = []
        for header in normalized[:-1].split(":"):
            numbered = re.fullmatch(r"(SOURCE|SOUR|OUTPUT|OUTP)([12])", header)
            if numbered:
                header = ("SOUR" if numbered[1] in {"SOURCE", "SOUR"} else "OUTP") + numbered[2]
            headers.append(cls._HEADER_ALIASES.get(header, header))
        return cls._NONEMPTY_QUERY.fullmatch(":".join(headers) + "?") is not None

    def mark_response_unsynchronized(self) -> None:
        """Invalidate reply association in memory; do not clear or read VISA."""
        self._reply_unsynchronized = True

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
        # UI status checks must never wait for VISA I/O or a transaction. Only
        # publish cached state under this short lock; never call I/O inside it.
        self._status_lock = Lock()
        self._status = (False, "")
        self._connection_generation = 0

    @property
    def is_connected(self) -> bool:
        with self._status_lock:
            return self._status[0]

    @property
    def identity(self) -> str:
        with self._status_lock:
            return self._status[1]

    @property
    def connection_generation(self) -> int:
        """Identify a backend lifetime without performing or waiting for I/O."""
        with self._status_lock:
            return self._connection_generation

    def _advance_connection_generation(self) -> None:
        with self._status_lock:
            self._connection_generation += 1

    def _publish_status(self, connected: bool) -> None:
        with self._status_lock:
            self._status = (connected, self._identity if connected else "")

    def connect(self) -> str:
        with self._lock:
            if self._backend is not None:
                self._identity = self._query_locked("*IDN?")
                self._publish_status(True)
                return self._identity
            self._backend = self._backend_factory(self.resource, self.timeout_ms)
            self._advance_connection_generation()
            try:
                self._identity = self._query_locked("*IDN?")
                self._publish_status(True)
                return self._identity
            except Exception:
                self._close_locked()
                raise

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()

    def write(self, command: str, *, recover: bool = False, after_recover: Callable[[], None] | None = None) -> None:
        normalized = self._normalize(command)
        with self._lock:
            self._write_locked(normalized, recover=recover, after_recover=after_recover)

    @contextmanager
    def transaction(self):
        """Serialize a related write/readback sequence on the shared session."""
        with self._lock:
            yield self

    @contextmanager
    def try_transaction(self):
        """Acquire an optional read's I/O transaction without queuing behind writes."""
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()

    def write_many(self, commands: Iterable[str], *, recover: bool = False, after_recover: Callable[[], None] | None = None) -> None:
        normalized = [self._normalize(command) for command in commands]
        with self._lock:
            for command in normalized:
                self._write_locked(command, recover=recover, batch=True, after_recover=after_recover)

    def query(self, command: str, *, recover: bool = False) -> str:
        normalized = self._normalize(command)
        with self._lock:
            try:
                result = self._query_locked(normalized)
            except ScpiResponseSyncError:
                # A delayed reply must never be attributed to a replayed/new
                # query. Only an explicit reconnect creates a fresh backend.
                raise
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
                self._publish_status(True)
            return result

    def mark_response_unsynchronized(self) -> None:
        """Optionally invalidate a backend's response association without I/O."""
        with self._lock:
            marker = getattr(self._backend, "mark_response_unsynchronized", None)
            if callable(marker):
                marker()

    def _write_locked(
        self,
        command: str,
        *,
        recover: bool,
        batch: bool = False,
        after_recover: Callable[[], None] | None = None,
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
        if after_recover is not None:
            after_recover()
        try:
            self._require_backend().write(command)
        except Exception as retry_exc:
            raise ScpiError(
                f"SCPI 写入在自动重连后仍失败 [{command}]：{retry_exc}"
            ) from retry_exc

    def _recover_locked(self, command: str, first_error: Exception) -> None:
        # Keep the last published state during serialized recovery. Publishing
        # a transient disconnect would make the manager discard its connection
        # while a successful recovery is still in flight. Failure clears it.
        old_backend, self._backend = self._backend, None
        if old_backend is not None:
            self._advance_connection_generation()
            try:
                old_backend.close()
            except Exception:
                pass
        try:
            backend = self._backend_factory(self.resource, self.timeout_ms)
            self._backend = backend
            self._advance_connection_generation()
            identity = str(backend.query("*IDN?")).strip()
            if not identity:
                raise ScpiConnectionError("自动重连后 *IDN? 返回为空")
            self._identity = identity
            self._publish_status(True)
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
        except ScpiResponseSyncError:
            raise
        except Exception as exc:
            raise ScpiError(f"SCPI 查询失败 [{command}]：{exc}") from exc

    def _require_backend(self) -> ScpiBackend:
        if self._backend is None:
            raise ScpiConnectionError("VISA 会话尚未连接")
        return self._backend

    def _close_locked(self) -> None:
        self._publish_status(False)
        backend, self._backend = self._backend, None
        self._identity = ""
        if backend is not None:
            self._advance_connection_generation()
            try:
                backend.close()
            except Exception as exc:
                raise ScpiError(f"关闭 VISA 会话失败：{exc}") from exc

    def _close_locked_safely(self) -> None:
        self._publish_status(False)
        backend, self._backend = self._backend, None
        self._identity = ""
        if backend is not None:
            self._advance_connection_generation()
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
