"""Bounded, asynchronous checks for this application's public GitHub releases."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from threading import Thread

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkProxy,
    QNetworkProxyFactory,
    QNetworkProxyQuery,
    QNetworkReply,
    QNetworkRequest,
)

from . import __version__


REPOSITORY_URL = "https://github.com/Rechoncy/Rigol_Generator_GUI"
LATEST_RELEASE_URL = "https://api.github.com/repos/Rechoncy/Rigol_Generator_GUI/releases/latest"
CHECK_TIMEOUT_MS = 15_000
MAX_RESPONSE_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
_VERSION = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})", re.ASCII)


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag: str
    url: str
    assets: tuple[ReleaseAsset, ...] = ()


def _version_parts(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("版本号格式无效，需为 x.y.z 或 vx.y.z 的正式版本")
    return tuple(int(part) for part in match.groups())


def _release_assets(payload: dict, tag: str, version: str) -> tuple[ReleaseAsset, ...]:
    entries = payload.get("assets")
    if not isinstance(entries, list):
        return ()
    allowed = {f"Lab-Control-Center-{version}-windows-x64-portable.zip",
               f"Lab-Control-Center-{version}-windows-x64.exe"}
    assets: dict[str, ReleaseAsset] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or name not in allowed:
            continue
        url = f"{REPOSITORY_URL}/releases/download/{tag}/{name}"
        size, digest = entry.get("size"), entry.get("digest")
        if (entry.get("state") != "uploaded" or entry.get("browser_download_url") != url
                or type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest, re.ASCII) is None):
            continue
        assets[name] = ReleaseAsset(name, url, size, digest[7:].lower())
    return tuple(assets[name] for name in sorted(assets))


def _release_info(payload: object) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise ValueError("GitHub 版本信息格式无效")
    if payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ValueError("GitHub 未返回可用的正式发行版")
    tag = payload.get("tag_name")
    parts = _version_parts(tag)
    # Only display a URL derived from the validated tag and fixed repository.
    # The response's html_url/body/assets are never opened or rendered as HTML.
    version = ".".join(map(str, parts))
    return ReleaseInfo(version, tag, f"{REPOSITORY_URL}/releases/tag/{tag}",
                       _release_assets(payload, tag, version))


def parse_release(payload: object, current_version: str = __version__) -> ReleaseInfo | None:
    """Return a newer stable release, rejecting invalid/preview release data."""
    release = _release_info(payload)
    return release if _version_parts(release.version) > _version_parts(current_version) else None


def _system_https_proxy(url: str = LATEST_RELEASE_URL):
    proxies = QNetworkProxyFactory.systemProxyForQuery(QNetworkProxyQuery(QUrl(url)))
    # Match Qt's HTTPS selection: the first transparent proxy (including DIRECT).
    # Returning a value avoids a Python virtual factory callback during Qt I/O.
    for proxy in proxies:
        if proxy.isTransparentProxy():
            return QNetworkProxy(proxy)
    raise RuntimeError("No suitable HTTPS proxy")


class _ProxyLookupSignals(QObject):
    finished = pyqtSignal(int, object, bool)


class UpdateChecker(QObject):
    update_available = pyqtSignal(object)
    up_to_date = pyqtSignal(str)
    failed = pyqtSignal(str)
    checking_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None, *, manager=None,
                 current_version: str = __version__, proxy_resolver=None) -> None:
        super().__init__(parent)
        _version_parts(current_version)
        self.current_version = current_version
        self._manager = manager if manager is not None else QNetworkAccessManager(self)
        self._proxy_resolver = proxy_resolver if proxy_resolver is not None else (
            _system_https_proxy if manager is None else None)
        self._proxy_signals = _ProxyLookupSignals(self)
        self._proxy_signals.finished.connect(self._proxy_resolved)
        self._proxy_thread = None
        self._generation = 0
        self._active = False
        self._reply = None
        self._body = bytearray()
        self._closed = False
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(CHECK_TIMEOUT_MS)
        self._deadline.timeout.connect(self._timed_out)

    @property
    def checking(self) -> bool:
        return self._active

    def check(self) -> bool:
        """Start one nonblocking request; duplicate requests are coalesced."""
        if self._closed or self.checking:
            return False
        if self._proxy_thread is not None and self._proxy_thread.is_alive():
            self.failed.emit("系统代理检测仍在进行，请稍后重试")
            return False
        self._generation += 1
        token = self._generation
        self._active = True
        self._body.clear()
        self._deadline.start()
        self.checking_changed.emit(True)
        if token != self._generation or not self.checking or self._closed:
            return False
        if self._proxy_resolver is None:
            self._send_request(token)
        else:
            # WinHTTP PAC/WPAD discovery is synchronous even for Qt's async
            # HTTP API. Resolve it outside the GUI and instrument worker pool.
            resolver, signals = self._proxy_resolver, self._proxy_signals

            def resolve():
                try:
                    proxy, success = resolver(), True
                except Exception:
                    proxy, success = None, False
                try:
                    signals.finished.emit(token, proxy, success)
                except RuntimeError:
                    pass  # Parent was destroyed while native lookup was pending.

            self._proxy_thread = Thread(target=resolve, name="software-update-proxy", daemon=True)
            try:
                self._proxy_thread.start()
            except RuntimeError:
                self._fail("无法启动系统代理检测，请稍后重试")
                return False
        return self.checking

    def _proxy_resolved(self, token: int, proxy, success: bool) -> None:
        if token != self._generation or not self.checking or self._closed:
            return
        if not success:
            self._fail("无法读取系统代理设置，请检查网络后重试")
            return
        # A concrete proxy affects only this manager; no application/global
        # proxy is changed and Qt cannot repeat PAC lookup on the GUI thread.
        self._manager.setProxy(proxy)
        self._send_request(token)

    def _send_request(self, token: int) -> None:
        if token != self._generation or not self.checking or self._closed:
            return
        request = QNetworkRequest(QUrl(LATEST_RELEASE_URL))
        request.setRawHeader(b"Accept", b"application/vnd.github+json")
        request.setRawHeader(b"User-Agent", f"Lab-Control-Center/{self.current_version}".encode("ascii"))
        request.setRawHeader(b"X-GitHub-Api-Version", b"2026-03-10")
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setAttribute(QNetworkRequest.Attribute.CacheLoadControlAttribute,
                             QNetworkRequest.CacheLoadControl.AlwaysNetwork)
        request.setTransferTimeout(CHECK_TIMEOUT_MS)
        try:
            reply = self._manager.get(request)
        except Exception:
            self._fail("无法启动更新检查，请稍后重试")
            return
        self._reply = reply
        reply.setReadBufferSize(MAX_RESPONSE_BYTES + 1)
        reply.readyRead.connect(lambda: self._read_available(reply))
        reply.finished.connect(lambda: self._finished(reply))

    def _read_available(self, reply) -> None:
        if self._reply is not reply:
            return
        remaining = MAX_RESPONSE_BYTES - len(self._body)
        chunk = reply.read(remaining + 1)
        # PyQt's QIODevice.read(max) returns None at EOF on real replies.
        # finished() can follow readyRead() after that slot consumed all data.
        if chunk:
            self._body.extend(bytes(chunk))
        if len(self._body) > MAX_RESPONSE_BYTES:
            self._fail("GitHub 版本响应过大，已停止检查")

    def _finished(self, reply) -> None:
        if self._reply is not reply:
            return
        self._read_available(reply)
        if self._reply is not reply:
            return
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if status in (403, 429):
            self._fail("GitHub 暂时限制请求，请稍后重试")
            return
        if status == 404:
            self._fail("GitHub 暂无可用发行版，或版本信息暂时不可访问")
            return
        if isinstance(status, int) and 300 <= status < 400:
            self._fail("GitHub 返回重定向，已停止检查")
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._fail("无法连接 GitHub，请检查网络或系统代理后重试")
            return
        if status != 200:
            self._fail("GitHub 版本服务暂时不可用，请稍后重试")
            return
        try:
            payload = json.loads(bytes(self._body).decode("utf-8"))
            release = _release_info(payload)
            newer = parse_release(payload, self.current_version)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._fail("GitHub 版本信息无效，无法确认是否有更新")
            return
        self._detach()
        if newer is not None:
            self.update_available.emit(newer)
        else:
            self.up_to_date.emit(release.version)

    def _detach(self, *, abort: bool = False) -> None:
        # Clear identity before abort: abort() may emit finished synchronously.
        active, self._active = self._active, False
        self._generation += 1
        reply, self._reply = self._reply, None
        self._deadline.stop()
        self._body.clear()
        if reply is not None:
            if abort:
                reply.abort()
            reply.deleteLater()
        if active:
            self.checking_changed.emit(False)

    def _fail(self, message: str) -> None:
        self._detach(abort=True)
        self.failed.emit(message)

    def _timed_out(self) -> None:
        if self.checking:
            self._fail("检查更新超时（15 秒），请稍后重试")

    def cancel(self) -> None:
        """Cancel quietly, retaining the ability to start a later manual check."""
        self._detach(abort=True)

    def close(self) -> None:
        self._closed = True
        self.cancel()
