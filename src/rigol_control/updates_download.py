"""Verified release downloads; all network, hashing and disk I/O stay off the GUI."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
from threading import Event, Thread
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener,
)

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QNetworkProxy

from . import __version__
from .updates import MAX_DOWNLOAD_BYTES, REPOSITORY_URL, ReleaseAsset, _system_https_proxy


DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000
SOCKET_TIMEOUT_SECONDS = 30
CHUNK_BYTES = 256 * 1024
_DOWNLOAD_HOSTS = frozenset(("github.com", "release-assets.githubusercontent.com"))
_ASSET_NAME = re.compile(
    r"Lab-Control-Center-(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\."
    r"(0|[1-9][0-9]{0,8})-windows-x64(?:-portable\.zip|\.exe)", re.ASCII)


class DownloadError(Exception):
    """A message safe to display without exposing signed redirect URLs."""


class _Cancelled(Exception):
    pass


def _validate_asset(asset: ReleaseAsset) -> None:
    if not isinstance(asset, ReleaseAsset):
        raise DownloadError("没有可自动下载的有效附件，请打开发布页下载")
    match = _ASSET_NAME.fullmatch(asset.name)
    if (match is None or type(asset.size) is not int or not 0 < asset.size <= MAX_DOWNLOAD_BYTES
            or re.fullmatch(r"[0-9a-fA-F]{64}", asset.sha256, re.ASCII) is None):
        raise DownloadError("附件名称、大小或 SHA-256 无效，请打开发布页下载")
    version = ".".join(match.groups())
    allowed = {f"{REPOSITORY_URL}/releases/download/{tag}/{asset.name}"
               for tag in (version, "v" + version)}
    if asset.url not in allowed:
        raise DownloadError("附件下载地址不属于本项目，请打开发布页下载")


def _validate_target(url: str) -> None:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise DownloadError("下载地址无效，已停止下载") from None
    if (parts.scheme != "https" or parts.hostname not in _DOWNLOAD_HOSTS
            or parts.username is not None or parts.password is not None
            or port not in (None, 443)):
        raise DownloadError("下载跳转不在允许的 GitHub HTTPS 地址内，已停止下载")


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirects = 0

    def redirect_request(self, request, response, code, message, headers, newurl):
        self.redirects += 1
        if self.redirects > 5:
            raise DownloadError("下载跳转次数过多，已停止下载")
        _validate_target(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _http_proxy_address(proxy) -> str:
    if proxy.type() != QNetworkProxy.ProxyType.HttpProxy:
        raise DownloadError("自动下载暂不支持 SOCKS 等代理，请切换系统 HTTP 代理或打开发布页下载")
    host = proxy.hostName()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{proxy.port()}"


class _SystemProxyHandler(ProxyHandler):
    def __init__(self, resolver) -> None:
        # Empty mapping prevents urllib from consulting environment/global
        # proxy settings before this per-request system/PAC decision.
        super().__init__({})
        self._resolver = resolver

    def https_open(self, request):
        _validate_target(request.full_url)
        proxy = self._resolver(request.full_url)
        if proxy.type() == QNetworkProxy.ProxyType.NoProxy:
            return None
        # The Qt lookup already applied the system bypass/PAC decision. Using
        # Request.set_proxy avoids urllib's second registry/environment bypass
        # check undoing that decision. HTTPS still uses a verified TLS tunnel.
        request.set_proxy(_http_proxy_address(proxy), "http")
        return None


def _build_https_opener(proxy_handler):
    # A private opener avoids global urllib/environment proxy mutation. The
    # standard SSL context checks hostnames and the Windows/system root store.
    context = ssl.create_default_context()
    return build_opener(proxy_handler, HTTPSHandler(context=context), _SafeRedirectHandler())


def _make_opener(proxy):
    """Static proxy opener retained for explicit injected transports/tests."""
    if proxy.type() == QNetworkProxy.ProxyType.NoProxy:
        proxies = {}
    else:
        proxy_url = "http://" + _http_proxy_address(proxy)
        proxies = {"http": proxy_url, "https": proxy_url}
    return _build_https_opener(ProxyHandler(proxies))


def _check_cancel(cancelled: Event, deadline: float) -> None:
    if cancelled.is_set():
        raise _Cancelled()
    if time.monotonic() >= deadline:
        raise DownloadError("下载超时（10 分钟），请稍后重试")


def _existing_verified(path: Path, asset: ReleaseAsset, cancelled: Event, deadline: float) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or path.stat().st_size != asset.size:
        raise DownloadError("下载目录已有同名文件且内容不同，请更换保存目录或移走该文件后重试")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            _check_cancel(cancelled, deadline)
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != asset.sha256.lower():
        raise DownloadError("下载目录已有同名文件且校验不匹配，请更换保存目录或移走该文件后重试")
    return True


def _publish_without_overwrite(temporary: Path, final: Path) -> None:
    if os.name == "nt":
        # Windows rename is atomic on this volume and refuses existing targets.
        os.rename(temporary, final)
    else:
        # POSIX rename would replace an existing target. A hard link is atomic
        # and refuses collisions; both paths are in the same destination folder.
        os.link(temporary, final)
        temporary.unlink()


def _download(asset: ReleaseAsset, destination: Path, cancelled: Event, deadline: float,
              progress, proxy_resolver, opener_factory) -> Path:
    _check_cancel(cancelled, deadline)
    destination.mkdir(parents=True, exist_ok=True)
    final = destination / asset.name
    if _existing_verified(final, asset, cancelled, deadline):
        _check_cancel(cancelled, deadline)
        progress(asset.size, asset.size)
        return final
    if opener_factory is not None:
        # Preserve injectable transport behavior without doing real I/O in
        # regression tests. The production path below resolves every URL.
        proxy = proxy_resolver() if proxy_resolver is not None else _system_https_proxy(asset.url)
        _check_cancel(cancelled, deadline)
        opener = opener_factory(proxy)
    else:
        def resolve(url):
            _check_cancel(cancelled, deadline)
            proxy = proxy_resolver() if proxy_resolver is not None else _system_https_proxy(url)
            _check_cancel(cancelled, deadline)
            return proxy

        opener = _build_https_opener(_SystemProxyHandler(resolve))
    request = Request(asset.url, headers={"User-Agent": f"Lab-Control-Center/{__version__}",
                                         "Accept": "application/octet-stream"})
    temporary = None
    try:
        with opener.open(request, timeout=SOCKET_TIMEOUT_SECONDS) as response:
            _validate_target(response.geturl())
            if response.getcode() != 200:
                raise DownloadError("下载服务器未返回完整附件，请稍后重试")
            declared_size = response.headers.get("Content-Length")
            if declared_size is not None:
                try:
                    valid_size = int(declared_size) == asset.size
                except (ValueError, TypeError):
                    valid_size = False
                if not valid_size:
                    raise DownloadError("附件长度与 GitHub 版本信息不一致，已停止下载")
            digest = hashlib.sha256()
            received, last_progress = 0, 0.0
            progress(0, asset.size)
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{asset.name}.", suffix=".part",
                                             dir=destination, delete=False) as stream:
                temporary = Path(stream.name)
                # HTTPResponse.read1 returns available bytes without waiting to
                # fill a complete 256 KiB block on a slow connection.
                read = getattr(response, "read1", response.read)
                while True:
                    _check_cancel(cancelled, deadline)
                    chunk = read(min(CHUNK_BYTES, asset.size - received + 1))
                    _check_cancel(cancelled, deadline)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > asset.size:
                        raise DownloadError("附件超过声明大小，已停止下载")
                    stream.write(chunk)
                    digest.update(chunk)
                    now = time.monotonic()
                    if now - last_progress >= 0.1:
                        progress(received, asset.size)
                        last_progress = now
                if received != asset.size:
                    raise DownloadError("附件下载不完整，请稍后重试")
                if digest.hexdigest() != asset.sha256.lower():
                    raise DownloadError("附件 SHA-256 校验失败，未保存为可用更新包")
                stream.flush()
                os.fsync(stream.fileno())
            _check_cancel(cancelled, deadline)
            try:
                _publish_without_overwrite(temporary, final)
            except FileExistsError:
                raise DownloadError("下载目录出现同名文件，已保留原文件，请更换目录后重试") from None
            temporary = None
            progress(received, asset.size)
            return final
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # An incomplete .part is never advertised as a usable update.


def _error_message(error: Exception) -> str:
    if isinstance(error, DownloadError):
        return str(error)
    if isinstance(error, HTTPError):
        if error.code in (403, 429):
            return "GitHub 暂时限制下载，请稍后重试或打开发布页下载"
        if error.code == 404:
            return "该版本附件不存在或暂时无法访问，请打开发布页下载"
        return f"下载失败（HTTP {error.code}），请稍后重试"
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "下载连接超时，请检查网络后重试"
    if isinstance(error, URLError):
        return "下载连接失败，请检查网络、系统代理或证书后重试"
    if isinstance(error, OSError):
        return "下载或保存文件失败，请检查网络、磁盘空间和目录权限后重试"
    return "下载未完成，请稍后重试或打开发布页下载"


class _DownloadSignals(QObject):
    progress = pyqtSignal(int, object, object)
    finished = pyqtSignal(int, object)
    failed = pyqtSignal(int, str)


class UpdateDownloader(QObject):
    progress = pyqtSignal(object, object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)
    downloading_changed = pyqtSignal(bool)

    def __init__(self, parent=None, *, proxy_resolver=None, opener_factory=None) -> None:
        super().__init__(parent)
        self._proxy_resolver = proxy_resolver
        self._opener_factory = opener_factory
        self._signals = _DownloadSignals(self)
        self._signals.progress.connect(self._progress)
        self._signals.finished.connect(self._finished)
        self._signals.failed.connect(self._failed)
        self._thread = None
        self._cancelled = None
        self._generation = 0
        self._active = False
        self._closed = False
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(DOWNLOAD_TIMEOUT_MS)
        self._deadline.timeout.connect(self._timed_out)

    @property
    def downloading(self) -> bool:
        return self._active

    def start(self, asset: ReleaseAsset, destination_dir: Path) -> bool:
        if self._closed or self.downloading:
            return False
        if self._thread is not None and self._thread.is_alive():
            self.failed.emit("上次下载仍在结束，请稍后重试")
            return False
        try:
            _validate_asset(asset)
            destination = Path(destination_dir)
        except (DownloadError, ValueError, TypeError) as exc:
            self.failed.emit(str(exc) if isinstance(exc, DownloadError) else "下载保存目录无效")
            return False
        self._generation += 1
        token = self._generation
        self._cancelled = cancelled = Event()
        deadline = time.monotonic() + DOWNLOAD_TIMEOUT_MS / 1000
        self._active = True
        self._deadline.start()
        self.downloading_changed.emit(True)
        if not self.downloading or self._closed or token != self._generation:
            return False
        signals, proxy_resolver, opener_factory = self._signals, self._proxy_resolver, self._opener_factory

        def emit(signal, *args):
            try:
                signal.emit(token, *args)
            except RuntimeError:
                pass  # Window was destroyed; the daemon still cleans up its .part.

        def run():
            try:
                path = _download(asset, destination, cancelled, deadline,
                                 lambda received, total: emit(signals.progress, received, total),
                                 proxy_resolver, opener_factory)
                if not cancelled.is_set():
                    emit(signals.finished, path)
            except _Cancelled:
                pass
            except Exception as exc:
                if not cancelled.is_set():
                    emit(signals.failed, _error_message(exc))

        self._thread = Thread(target=run, name="software-update-download", daemon=True)
        try:
            self._thread.start()
        except RuntimeError:
            self._detach()
            self.failed.emit("无法启动更新下载，请稍后重试")
            return False
        return True

    def _progress(self, token, received, total) -> None:
        if token == self._generation and self.downloading and not self._closed:
            self.progress.emit(received, total)

    def _finished(self, token, path) -> None:
        if token == self._generation and self.downloading and not self._closed:
            self._detach()
            self.finished.emit(path)

    def _failed(self, token, message) -> None:
        if token == self._generation and self.downloading and not self._closed:
            self._detach()
            self.failed.emit(message)

    def _detach(self) -> None:
        active, self._active = self._active, False
        self._generation += 1
        self._deadline.stop()
        if self._cancelled is not None:
            self._cancelled.set()
        if active:
            self.downloading_changed.emit(False)

    def _timed_out(self) -> None:
        if self.downloading:
            self._detach()
            self.failed.emit("下载超时（10 分钟），请稍后重试")

    def cancel(self) -> None:
        self._detach()

    def close(self) -> None:
        self._closed = True
        self.cancel()
