"""Bounded, asynchronous checks for this application's public GitHub releases."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import inspect
import json
import math
import re
from threading import Thread
import time
from urllib.parse import urljoin, urlsplit

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
LATEST_MANIFEST_URL = REPOSITORY_URL + "/releases/latest/download/release-manifest.json"
CHECK_TIMEOUT_MS = 15_000
MAX_RESPONSE_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
SUCCESS_CACHE_SECONDS = 60
FAILURE_COOLDOWN_SECONDS = 30
MAX_MANIFEST_REDIRECTS = 5
MAX_RATE_WAIT_SECONDS = 24 * 60 * 60
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
               f"Lab-Control-Center-{version}-windows-x64.exe",
               f"Lab-Control-Center-{version}-windows-x64-setup.exe"}
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


def _manifest_release_info(payload: object, tag: str) -> ReleaseInfo:
    """Read the publisher's existing manifest schema, bound to its observed tag.

    Older published manifests have no schema_version field; an explicit future
    incompatible schema is refused. Only fixed project artifact names become
    downloadable; paths and URLs from response data are never used.
    """
    if not isinstance(payload, dict):
        raise ValueError("发行清单格式无效")
    schema = payload.get("schema_version", 1)
    if type(schema) is not int or schema != 1:
        raise ValueError("不支持的发行清单格式")
    version = payload.get("version")
    parts = _version_parts(version)
    if (version != ".".join(map(str, parts)) or _version_parts(tag) != parts
            or payload.get("platform") != "windows-x64" or payload.get("source_dirty") is not False
            or not isinstance(payload.get("source_commit"), str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", payload["source_commit"], re.ASCII) is None):
        raise ValueError("发行清单版本或构建身份无效")
    application_names = {f"Lab-Control-Center-{version}-windows-x64{suffix}"
                         for suffix in ("-portable.zip", ".exe", "-setup.exe")}
    allowed_names = application_names | {f"Lab-Control-Center-{version}-windows-x64-source.zip",
                                         "RELEASE-NOTES.md", "release-manifest.json", "SHA256SUMS.txt"}
    entries = payload.get("artifacts")
    if not isinstance(entries, list) or not 0 < len(entries) <= len(allowed_names):
        raise ValueError("发行清单附件无效")
    names, assets = set(), []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("发行清单附件格式无效")
        name, size, digest = entry.get("file"), entry.get("bytes"), entry.get("sha256")
        if (not isinstance(name, str) or name not in allowed_names or name in names
                or type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest, re.ASCII) is None):
            raise ValueError("发行清单附件名称、大小或 SHA-256 无效")
        names.add(name)
        if name in application_names:
            assets.append(ReleaseAsset(name, f"{REPOSITORY_URL}/releases/download/{tag}/{name}", size, digest.lower()))
    if not assets:
        raise ValueError("发行清单没有可验证的应用附件")
    return ReleaseInfo(version, tag, f"{REPOSITORY_URL}/releases/tag/{tag}",
                       tuple(sorted(assets, key=lambda asset: asset.name)))


def _manifest_target(url: str, tag: str | None, *, initial: bool = False) -> str | None:
    """Validate every hop before issuing it, returning the bound stable tag."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("无效的发行清单地址") from None
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or port not in (None, 443) or parts.fragment
            or any(ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError("发行清单跳转不在允许的 HTTPS 地址内")
    if parts.hostname == "github.com":
        if initial and url == LATEST_MANIFEST_URL and tag is None:
            return None
        prefix = urlsplit(REPOSITORY_URL).path + "/releases/download/"
        match = re.fullmatch(re.escape(prefix) + r"([^/]+)/release-manifest\.json", parts.path, re.ASCII)
        if parts.query or match is None:
            raise ValueError("发行清单跳转不属于本项目")
        observed = match.group(1)
        _version_parts(observed)
        if tag is not None and tag != observed:
            raise ValueError("发行清单跳转版本发生变化")
        return observed
    if (parts.hostname == "release-assets.githubusercontent.com" and tag is not None
            and parts.path.startswith("/github-production-release-asset/")
            and ".." not in parts.path.split("/")):
        return tag
    raise ValueError("发行清单跳转不在允许的 GitHub 地址内")


def _header(reply, name: bytes) -> str:
    return bytes(reply.rawHeader(name)).decode("ascii", errors="replace").strip()


def _retry_delay(value: str, now: float) -> float | None:
    if re.fullmatch(r"[0-9]{1,10}", value, re.ASCII):
        return min(float(value), MAX_RATE_WAIT_SECONDS)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        return min(max(0, date.timestamp() - now), MAX_RATE_WAIT_SECONDS)
    except (ValueError, TypeError, OverflowError):
        return None


def _rate_limit_delay(reply, status: int, body: bytes, now: float) -> float | None:
    if status not in (403, 429):
        return None
    retry = _retry_delay(_header(reply, b"Retry-After"), now)
    remaining = _header(reply, b"X-RateLimit-Remaining")
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            message = payload["message"].lower()
    except (ValueError, UnicodeError, RecursionError):
        pass
    if status != 429 and remaining != "0" and retry is None and "rate limit" not in message and "rate-limit" not in message:
        return None
    waits = [60.0]
    if retry is not None:
        waits.append(retry)
    reset = _header(reply, b"X-RateLimit-Reset")
    if re.fullmatch(r"[0-9]{1,12}", reset, re.ASCII):
        waits.append(min(max(0, float(reset) - now), MAX_RATE_WAIT_SECONDS))
    return max(waits)


def _json_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("重复的版本信息字段")
        result[name] = value
    return result


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
                 current_version: str = __version__, proxy_resolver=None,
                 clock=None, wall_clock=None) -> None:
        super().__init__(parent)
        _version_parts(current_version)
        self.current_version = current_version
        self._manager = manager if manager is not None else QNetworkAccessManager(self)
        self._proxy_resolver = proxy_resolver if proxy_resolver is not None else (
            _system_https_proxy if manager is None else None)
        # Preserve the earlier no-argument injection hook while allowing tests
        # and real PAC decisions to select a proxy for the exact URL of each hop.
        self._proxy_accepts_url = True
        if self._proxy_resolver is not None:
            try:
                inspect.signature(self._proxy_resolver).bind(LATEST_RELEASE_URL)
            except (ValueError, TypeError):
                self._proxy_accepts_url = False
        self._clock = clock if clock is not None else time.monotonic
        self._wall_clock = wall_clock if wall_clock is not None else time.time
        self._proxy_signals = _ProxyLookupSignals(self)
        self._proxy_signals.finished.connect(self._proxy_resolved)
        self._proxy_thread = None
        self._generation = 0
        self._active = False
        self._reply = None
        self._body = bytearray()
        self._closed = False
        self._url = LATEST_RELEASE_URL
        self._mode = "api"
        self._manifest_tag = None
        self._redirects = 0
        self._api_retry_after = 0.0
        self._failure_until = 0.0
        self._last_failure = ""
        self._cache = None
        self._cache_until = 0.0
        self.last_source = ""
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(CHECK_TIMEOUT_MS)
        self._deadline.timeout.connect(self._timed_out)

    @property
    def checking(self) -> bool:
        return self._active

    def check(self) -> bool:
        """Coalesce active checks and bound repeated clicks without blocking Qt."""
        if self._closed or self.checking:
            return False
        if self._proxy_thread is not None and self._proxy_thread.is_alive():
            self.failed.emit("系统代理检测仍在进行，请稍后重试")
            return False
        now = self._clock()
        if now < self._failure_until:
            seconds = math.ceil(self._failure_until - now)
            self.failed.emit(f"{self._last_failure}（请在 {seconds} 秒后重试）")
            return False
        self._generation += 1
        token = self._generation
        self._active = True
        self._body.clear()
        self._deadline.start()
        self.checking_changed.emit(True)
        if token != self._generation or not self.checking or self._closed:
            return False
        if self._cache is not None and now < self._cache_until:
            # Defer delivery so cancellation/close can invalidate a cached
            # result just as they invalidate an in-flight network reply.
            QTimer.singleShot(0, lambda: self._cached_result(token))
            return True
        self._mode = "manifest" if now < self._api_retry_after else "api"
        self._manifest_tag = None
        self._redirects = 0
        self._start_url(LATEST_MANIFEST_URL if self._mode == "manifest" else LATEST_RELEASE_URL, token)
        return self.checking

    def _cached_result(self, token: int) -> None:
        if token == self._generation and self.checking and not self._closed:
            self._complete(self._cache, "cache", refresh_cache=False)

    def _start_url(self, url: str, token: int) -> None:
        if token != self._generation or not self.checking or self._closed:
            return
        self._url = url
        self._body.clear()
        if self._proxy_resolver is None:
            self._send_request(token)
        else:
            # WinHTTP PAC/WPAD discovery is synchronous even for Qt's async
            # HTTP API. Resolve it outside the GUI and instrument worker pool.
            resolver, signals = self._proxy_resolver, self._proxy_signals
            accepts_url = self._proxy_accepts_url

            def resolve():
                try:
                    proxy, success = (resolver(url) if accepts_url else resolver()), True
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
                return

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
        request = QNetworkRequest(QUrl(self._url))
        request.setRawHeader(b"Accept", b"application/vnd.github+json" if self._mode == "api" else b"application/json")
        request.setRawHeader(b"User-Agent", f"Lab-Control-Center/{self.current_version}".encode("ascii"))
        if self._mode == "api":
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
        rate_delay = _rate_limit_delay(reply, status, bytes(self._body), self._wall_clock())
        if rate_delay is not None:
            if self._mode == "api":
                self._api_retry_after = self._clock() + rate_delay
                self._mode = "manifest"
                self._manifest_tag = None
                self._redirects = 0
                self._discard_reply()
                # Keep the original single deadline and busy state across API,
                # PAC lookups, and every validated manifest redirect.
                self._start_url(LATEST_MANIFEST_URL, self._generation)
            else:
                self._fail("GitHub 发布清单暂时限制请求，已暂停重试", retry_seconds=rate_delay)
            return
        if status == 403:
            self._fail("更新访问被拒绝（HTTP 403），请检查网络或系统代理")
            return
        if status == 404:
            self._fail("GitHub 暂无可用发行版，或版本信息暂时不可访问")
            return
        if isinstance(status, int) and 300 <= status < 400:
            if self._mode == "api":
                self._fail("GitHub 返回重定向，已停止检查")
                return
            self._redirects += 1
            if status not in (301, 302, 303, 307, 308) or self._redirects > MAX_MANIFEST_REDIRECTS:
                self._fail("发行清单跳转次数过多或类型无效，已停止检查")
                return
            target = reply.attribute(QNetworkRequest.Attribute.RedirectionTargetAttribute)
            try:
                location = target.toString() if isinstance(target, QUrl) else ""
                if not location:
                    raise ValueError("发行清单跳转缺少地址")
                url = urljoin(self._url, location)
                self._manifest_tag = _manifest_target(url, self._manifest_tag)
            except ValueError:
                self._fail("发行清单跳转地址或版本不可信，已停止检查")
                return
            self._discard_reply()
            self._start_url(url, self._generation)
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._fail("无法连接 GitHub，请检查网络或系统代理后重试")
            return
        if status != 200:
            self._fail("GitHub 版本服务暂时不可用，请稍后重试")
            return
        try:
            payload = json.loads(bytes(self._body).decode("utf-8"), object_pairs_hook=_json_object)
            if self._mode == "manifest":
                # A CDN response is accepted only after observing a canonical
                # project tag URL. The manifest must match that exact version.
                if self._manifest_tag is None:
                    raise ValueError("发行清单缺少可验证的版本跳转")
                release = _manifest_release_info(payload, self._manifest_tag)
            else:
                release = _release_info(payload)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._fail("GitHub 版本信息无效，无法确认是否有更新")
            return
        self._complete(release, self._mode)

    def _complete(self, release: ReleaseInfo, source: str, *, refresh_cache: bool = True) -> None:
        if refresh_cache:
            self._cache, self._cache_until = release, self._clock() + SUCCESS_CACHE_SECONDS
        self.last_source = source
        self._failure_until = 0.0
        generation = self._detach()
        # checking_changed(False) can synchronously close the window or start
        # another request. An old result must never schedule a later download.
        if self._closed or generation != self._generation:
            return
        if _version_parts(release.version) > _version_parts(self.current_version):
            self.update_available.emit(release)
        else:
            self.up_to_date.emit(release.version)

    def _discard_reply(self, *, abort: bool = False) -> None:
        # Clear identity before abort: abort() may emit finished synchronously.
        reply, self._reply = self._reply, None
        self._body.clear()
        if reply is not None:
            if abort:
                reply.abort()
            reply.deleteLater()

    def _detach(self, *, abort: bool = False) -> int:
        active, self._active = self._active, False
        self._generation += 1
        generation = self._generation
        self._deadline.stop()
        self._discard_reply(abort=abort)
        if active:
            self.checking_changed.emit(False)
        return generation

    def _fail(self, message: str, *, retry_seconds: float = FAILURE_COOLDOWN_SECONDS) -> None:
        self._last_failure = message
        self._failure_until = self._clock() + max(FAILURE_COOLDOWN_SECONDS, retry_seconds)
        generation = self._detach(abort=True)
        if not self._closed and generation == self._generation:
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
