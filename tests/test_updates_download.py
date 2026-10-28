import hashlib
import io
import os
from pathlib import Path
import socket
import ssl
import tempfile
from threading import Event, get_ident
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QNetworkProxy
from PyQt6.QtWidgets import QApplication

from rigol_control.updates import ReleaseAsset
from rigol_control.updates_download import (
    CHUNK_BYTES, DOWNLOAD_TIMEOUT_MS, DownloadError, UpdateDownloader,
    _SafeRedirectHandler, _SystemProxyHandler, _make_opener, _publish_without_overwrite,
)


class FakeResponse(io.BytesIO):
    def __init__(self, data, *, declared_size=None, final_url=None, error=None, entered=None, release=None):
        super().__init__(data)
        self.headers = {} if declared_size is None else {"Content-Length": str(declared_size)}
        self.final_url = final_url or "https://release-assets.githubusercontent.com/asset?secret=signed"
        self.error = error
        self.entered, self.release = entered, release
        self.read_threads = []
        self.read_sizes = []

    def geturl(self):
        return self.final_url

    def getcode(self):
        return 200

    def read(self, size=-1):
        self.read_threads.append(get_ident())
        self.read_sizes.append(size)
        if self.entered is not None:
            self.entered.set()
            self.release.wait(2)
        if self.error is not None:
            raise self.error
        return super().read(size)

    def read1(self, size=-1):
        return self.read(size)


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.requests = []
        self.threads = []

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        self.threads.append(get_ident())
        if self.error is not None:
            raise self.error
        return self.response


class DownloadTransportTests(unittest.TestCase):
    def test_redirects_only_use_known_https_hosts_without_credentials(self):
        request = Request("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/update.exe")
        handler = _SafeRedirectHandler()
        for target in ("http://release-assets.githubusercontent.com/file", "https://evil.example/file",
                       "https://github.com.evil.example/file", "https://user:pass@github.com/file",
                       "https://release-assets.githubusercontent.com:8443/file"):
            with self.subTest(target=target), self.assertRaises(DownloadError):
                handler.redirect_request(request, None, 302, "Found", {}, target)
        handler = _SafeRedirectHandler()
        for _ in range(5):
            redirected = handler.redirect_request(request, None, 302, "Found", {},
                                                   "https://release-assets.githubusercontent.com/file?signature=secret")
            self.assertEqual(redirected.host, "release-assets.githubusercontent.com")
        with self.assertRaises(DownloadError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://github.com/file")

    def test_opener_keeps_tls_validation_and_proxy_local(self):
        import urllib.request
        global_opener = urllib.request._opener
        with patch("rigol_control.updates_download.build_opener") as build:
            _make_opener(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        handlers = build.call_args.args
        proxy_handler, https_handler = handlers[:2]
        self.assertEqual(proxy_handler.proxies, {})
        self.assertTrue(https_handler._context.check_hostname)
        self.assertEqual(https_handler._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIs(urllib.request._opener, global_opener)

    def test_http_proxy_supported_and_socks_has_clear_fallback(self):
        with patch("rigol_control.updates_download.build_opener") as build:
            _make_opener(QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", 7897))
        self.assertEqual(build.call_args.args[0].proxies["https"], "http://127.0.0.1:7897")
        with self.assertRaisesRegex(DownloadError, "SOCKS"):
            _make_opener(QNetworkProxy(QNetworkProxy.ProxyType.Socks5Proxy, "127.0.0.1", 7897))

    def test_pac_route_is_recomputed_for_github_and_asset_redirect(self):
        original = Request("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/file.exe")
        target = "https://release-assets.githubusercontent.com/file?signature=signed"
        looked_up = []

        def resolve(url):
            looked_up.append(url)
            if url == target:
                return QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "asset-proxy.example", 7897)
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        with patch("urllib.request.getproxies", side_effect=AssertionError("Do not read environment proxies")):
            handler = _SystemProxyHandler(resolve)
        handler.https_open(original)
        self.assertEqual(original.host, "github.com")
        redirected = _SafeRedirectHandler().redirect_request(original, None, 302, "Found", {}, target)
        with patch("urllib.request.proxy_bypass", return_value=True):
            handler.https_open(redirected)
        self.assertEqual(redirected.host, "asset-proxy.example:7897")
        self.assertEqual(redirected._tunnel_host, "release-assets.githubusercontent.com")
        self.assertEqual(looked_up, [original.full_url, target])

    def test_direct_asset_redirect_does_not_inherit_github_proxy(self):
        original = Request("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/file.exe")
        target = "https://release-assets.githubusercontent.com/file"
        handler = _SystemProxyHandler(lambda url: QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
                                      if url == target else
                                      QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "github-proxy.example", 7897))
        handler.https_open(original)
        self.assertEqual(original.host, "github-proxy.example:7897")
        redirected = _SafeRedirectHandler().redirect_request(original, None, 302, "Found", {}, target)
        handler.https_open(redirected)
        self.assertEqual(redirected.host, "release-assets.githubusercontent.com")
        self.assertIsNone(redirected._tunnel_host)

    def test_unapproved_target_is_rejected_before_any_pac_lookup(self):
        resolver = Mock()
        handler = _SystemProxyHandler(resolver)
        with self.assertRaises(DownloadError):
            handler.https_open(Request("https://example.com/file"))
        resolver.assert_not_called()


class UpdateDownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["download-tests"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "updates"
        self.data = b"verified portable content\x00" * 200
        name = "Lab-Control-Center-0.7.22-windows-x64-portable.zip"
        self.asset = ReleaseAsset(name,
                                 f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/{name}",
                                 len(self.data), hashlib.sha256(self.data).hexdigest())
        self.response = FakeResponse(self.data, declared_size=len(self.data))
        self.opener = FakeOpener(self.response)
        self.proxy = Mock(return_value=QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        self.downloader = UpdateDownloader(proxy_resolver=self.proxy, opener_factory=lambda proxy: self.opener)
        self.done, self.errors, self.progress, self.busy = [], [], [], []
        self.downloader.finished.connect(self.done.append)
        self.downloader.failed.connect(self.errors.append)
        self.downloader.progress.connect(lambda received, total: self.progress.append((received, total)))
        self.downloader.downloading_changed.connect(self.busy.append)
        self.release_events = []

    def tearDown(self):
        self.downloader.close()
        for event in self.release_events:
            event.set()
        self.assertTrue(self.wait(lambda: self.downloader._thread is None or not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.temporary.cleanup()

    def wait(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.001)
        return predicate()

    def download(self):
        self.assertTrue(self.downloader.start(self.asset, self.root))
        self.assertTrue(self.wait(lambda: not self.downloader.downloading))

    def assert_no_partial_or_final(self):
        self.assertFalse((self.root / self.asset.name).exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_success_validates_bytes_hash_and_writes_only_on_worker_thread(self):
        self.download()
        final = self.root / self.asset.name
        self.assertEqual(self.done, [final])
        self.assertEqual(final.read_bytes(), self.data)
        self.assertFalse(self.errors)
        self.assertEqual(self.busy, [True, False])
        self.assertEqual(self.progress[0], (0, len(self.data)))
        self.assertEqual(self.progress[-1], (len(self.data), len(self.data)))
        self.assertTrue(all(thread != get_ident() for thread in self.opener.threads + self.response.read_threads))
        self.assertTrue(all(size <= CHUNK_BYTES for size in self.response.read_sizes))
        request, timeout = self.opener.requests[0]
        self.assertEqual(request.full_url, self.asset.url)
        self.assertEqual(timeout, 30)
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(self.downloader._deadline.interval(), DOWNLOAD_TIMEOUT_MS)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_existing_verified_asset_reused_without_network(self):
        self.root.mkdir()
        final = self.root / self.asset.name
        final.write_bytes(self.data)
        self.download()
        self.assertEqual(self.done, [final])
        self.assertFalse(self.opener.requests)
        self.proxy.assert_not_called()
        self.assertFalse(self.errors)

    def test_existing_different_file_is_preserved_without_network(self):
        self.root.mkdir()
        final = self.root / self.asset.name
        final.write_bytes(b"user data")
        self.download()
        self.assertEqual(final.read_bytes(), b"user data")
        self.assertFalse(self.done)
        self.assertIn("同名", self.errors[0])
        self.assertFalse(self.opener.requests)

    def test_same_size_existing_file_with_wrong_hash_is_preserved(self):
        self.root.mkdir()
        final = self.root / self.asset.name
        final.write_bytes(b"x" * self.asset.size)
        self.download()
        self.assertEqual(final.read_bytes(), b"x" * self.asset.size)
        self.assertFalse(self.done)
        self.assertIn("校验", self.errors[0])
        self.assertFalse(self.opener.requests)

    def test_bad_stream_lengths_and_hashes_never_publish_an_update(self):
        for data in (self.data[:-1], self.data + b"overflow", b"x" * self.asset.size):
            with self.subTest(length=len(data)):
                self.opener.response = FakeResponse(data)
                self.download()
                self.assert_no_partial_or_final()
                self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.assertEqual(len(self.errors), 3)
        self.assertFalse(self.done)

    def test_declared_size_mismatch_fails_before_file_creation(self):
        self.response.headers["Content-Length"] = "1"
        self.download()
        self.assertIn("长度", self.errors[0])
        self.assert_no_partial_or_final()

    def test_unapproved_final_response_host_is_rejected(self):
        self.response.final_url = "https://example.com/signed?token=secret"
        self.download()
        self.assertIn("跳转", self.errors[0])
        self.assertNotIn("secret", self.errors[0])
        self.assert_no_partial_or_final()

    def test_http_network_and_timeout_errors_are_sanitized(self):
        cases = [HTTPError("https://release-assets.githubusercontent.com/?secret=token", 403, "forbidden", {}, None),
                 HTTPError(self.asset.url, 404, "missing", {}, None),
                 HTTPError(self.asset.url, 500, "server", {}, None),
                 URLError("https://release-assets.githubusercontent.com/?secret=token"), socket.timeout()]
        for error in cases:
            self.opener.error = error
            self.download()
            self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.assertEqual(len(self.errors), len(cases))
        self.assertTrue(all("secret" not in message and "token" not in message for message in self.errors))
        self.assert_no_partial_or_final()

    def test_cancellation_during_read_is_immediate_and_keeps_worker_single(self):
        entered, release = Event(), Event()
        self.release_events.append(release)
        self.opener.response = FakeResponse(self.data, entered=entered, release=release)
        self.assertTrue(self.downloader.start(self.asset, self.root))
        self.assertTrue(self.wait(entered.is_set))
        self.assertFalse(self.downloader.start(self.asset, self.root))
        self.downloader.cancel()
        self.assertFalse(self.downloader.downloading)
        self.assertTrue(self.downloader._thread.is_alive())
        self.assertFalse(self.downloader.start(self.asset, self.root))
        self.assertIn("仍在结束", self.errors[-1])
        self.assertEqual(len(self.opener.requests), 1)
        release.set()
        self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.assertFalse(self.done)
        self.assert_no_partial_or_final()

    def test_proxy_lookup_deadline_does_not_block_gui_or_start_late_network(self):
        entered, release = Event(), Event()
        self.release_events.append(release)

        def lookup():
            entered.set()
            release.wait(2)
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        self.downloader._proxy_resolver = lookup
        self.downloader._deadline.setInterval(25)
        self.assertTrue(self.downloader.start(self.asset, self.root))
        self.assertTrue(self.wait(entered.is_set))
        ticks = []
        QTimer.singleShot(1, lambda: ticks.append(True))
        self.assertTrue(self.wait(lambda: bool(self.errors)))
        self.assertEqual(ticks, [True])
        self.assertIn("超时", self.errors[0])
        self.assertFalse(self.downloader.downloading)
        release.set()
        self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.assertFalse(self.opener.requests)
        self.assertFalse(self.done)
        self.assert_no_partial_or_final()

    def test_close_during_download_does_not_wait_or_allow_future_start(self):
        entered, release = Event(), Event()
        self.release_events.append(release)
        self.opener.response = FakeResponse(self.data, entered=entered, release=release)
        self.downloader.start(self.asset, self.root)
        self.assertTrue(self.wait(entered.is_set))
        self.downloader.close()
        self.assertTrue(self.downloader._thread.is_alive())
        self.assertFalse(self.downloader.start(self.asset, self.root))
        release.set()
        self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.assertFalse(self.done)
        self.assertFalse(self.errors)
        self.assert_no_partial_or_final()

    def test_invalid_asset_or_unverified_source_is_rejected_before_io(self):
        bad = [ReleaseAsset("../escape.exe", self.asset.url, 1, "a" * 64),
               ReleaseAsset(self.asset.name, "https://evil.example/update.zip", 1, "a" * 64),
               ReleaseAsset(self.asset.name, self.asset.url, self.asset.size, ""),
               ReleaseAsset(self.asset.name, self.asset.url, -1, "a" * 64)]
        for asset in bad:
            self.assertFalse(self.downloader.start(asset, self.root))
        self.assertFalse(self.root.exists())
        self.assertFalse(self.opener.requests)
        self.assertFalse(self.downloader.downloading)

    def test_file_appearing_at_publish_time_is_never_overwritten(self):
        final = self.root / self.asset.name

        def collide(temporary, target):
            target.write_bytes(b"other process")
            _publish_without_overwrite(temporary, target)

        with patch("rigol_control.updates_download._publish_without_overwrite", side_effect=collide):
            self.download()
        self.assertEqual(final.read_bytes(), b"other process")
        self.assertIn("同名", self.errors[0])
        self.assertFalse(self.done)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_cancel_before_atomic_publish_does_not_leave_a_ready_file(self):
        entered, release = Event(), Event()
        self.release_events.append(release)

        def sync(_):
            entered.set()
            release.wait(2)

        with patch("rigol_control.updates_download.os.fsync", side_effect=sync):
            self.downloader.start(self.asset, self.root)
            self.assertTrue(self.wait(entered.is_set))
            self.downloader.cancel()
            release.set()
            self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.assertFalse(self.done)
        self.assert_no_partial_or_final()

    def test_thread_start_failure_can_be_retried(self):
        with patch("rigol_control.updates_download.Thread.start", side_effect=RuntimeError("thread failure")):
            self.assertFalse(self.downloader.start(self.asset, self.root))
        self.assertFalse(self.downloader.downloading)
        self.assertFalse(self.root.exists())
        self.download()
        self.assertEqual(len(self.done), 1)

    def test_default_transport_resolves_each_real_download_url_on_worker(self):
        target = self.response.final_url
        lookup_urls, lookup_threads = [], []

        def lookup(url):
            lookup_urls.append(url)
            lookup_threads.append(get_ident())
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        def build_transport(handler):
            def open(request, *, timeout):
                handler.https_open(request)
                redirected = _SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)
                handler.https_open(redirected)
                return self.response

            return Mock(open=open)

        self.downloader._proxy_resolver = None
        self.downloader._opener_factory = None
        with patch("rigol_control.updates_download._system_https_proxy", side_effect=lookup), \
                patch("rigol_control.updates_download._build_https_opener", side_effect=build_transport):
            self.download()
        self.assertEqual(lookup_urls, [self.asset.url, target])
        self.assertTrue(all(thread != get_ident() for thread in lookup_threads))
        self.assertEqual(self.done, [self.root / self.asset.name])
        self.assertFalse(self.errors)

    def test_cancel_during_redirect_pac_does_not_open_the_redirect_connection(self):
        entered, release = Event(), Event()
        self.release_events.append(release)
        target = self.response.final_url
        connected = []

        def lookup(url):
            if url == target:
                entered.set()
                release.wait(2)
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        def build_transport(handler):
            def open(request, *, timeout):
                handler.https_open(request)
                connected.append(request.full_url)
                redirected = _SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)
                handler.https_open(redirected)
                connected.append(redirected.full_url)
                return self.response

            return Mock(open=open)

        self.downloader._proxy_resolver = None
        self.downloader._opener_factory = None
        with patch("rigol_control.updates_download._system_https_proxy", side_effect=lookup), \
                patch("rigol_control.updates_download._build_https_opener", side_effect=build_transport):
            self.downloader.start(self.asset, self.root)
            self.assertTrue(self.wait(entered.is_set))
            self.downloader.cancel()
            release.set()
            self.assertTrue(self.wait(lambda: not self.downloader._thread.is_alive()))
        self.app.processEvents()
        self.assertEqual(connected, [self.asset.url])
        self.assertFalse(self.done)
        self.assertFalse(self.errors)
        self.assert_no_partial_or_final()


if __name__ == "__main__":
    unittest.main()
