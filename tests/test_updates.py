import json
import hashlib
import os
import time
import unittest
from threading import Event, get_ident
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QTimer, QUrl, pyqtSignal
from PyQt6.QtNetwork import QNetworkProxy, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QApplication

from rigol_control.updates import (
    LATEST_RELEASE_URL,
    LATEST_MANIFEST_URL,
    MAX_RESPONSE_BYTES,
    MAX_DOWNLOAD_BYTES,
    ReleaseAsset,
    ReleaseInfo,
    UpdateChecker,
    _system_https_proxy,
    _manifest_release_info,
    _manifest_target,
    parse_release,
)


def payload(tag="v0.7.22", **values):
    return dict(tag_name=tag, draft=False, prerelease=False, **values)


def manifest_payload(version="0.7.24", **values):
    artifacts = [dict(file=f"Lab-Control-Center-{version}-windows-x64{suffix}", bytes=size, sha256="a" * 64)
                 for suffix, size in (("-portable.zip", 123), (".exe", 234), ("-setup.exe", 345))]
    return dict(version=version, platform="windows-x64", source_commit="a" * 40,
                source_dirty=False, artifacts=artifacts, **values)


class FakeClock:
    def __init__(self, value=1000.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeReply(QObject):
    readyRead = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.data = bytearray()
        self.status = 200
        self.network_error = QNetworkReply.NetworkError.NoError
        self.aborted = False
        self.deleted = False
        self.read_limits = []
        self.buffer_limit = None
        self.eof_returns_none = False
        self.headers = {}
        self.location = QUrl()

    def setReadBufferSize(self, limit):
        self.buffer_limit = limit

    def read(self, limit):
        self.read_limits.append(limit)
        if not self.data and self.eof_returns_none:
            return None
        result = bytes(self.data[:limit])
        del self.data[:limit]
        return result

    def attribute(self, code):
        if code == QNetworkRequest.Attribute.RedirectionTargetAttribute:
            return self.location
        assert code == QNetworkRequest.Attribute.HttpStatusCodeAttribute
        return self.status

    def rawHeader(self, name):
        return self.headers.get(bytes(name).lower(), b"")

    def error(self):
        return self.network_error

    def abort(self):
        self.aborted = True
        # QNetworkReply may emit finished immediately on abort.
        self.network_error = QNetworkReply.NetworkError.OperationCanceledError
        self.finished.emit()

    def deleteLater(self):
        self.deleted = True

    def deliver(self, body, *, finished=True):
        self.data.extend(body)
        self.readyRead.emit()
        if finished:
            self.finished.emit()


class FakeManager:
    def __init__(self):
        self.requests = []
        self.replies = []
        self.raise_on_get = False
        self.proxy = None

    def setProxy(self, proxy):
        self.proxy = proxy

    def get(self, request):
        self.requests.append(request)
        if self.raise_on_get:
            raise RuntimeError("dispatch failed")
        reply = FakeReply()
        self.replies.append(reply)
        return reply


class ReleaseParsingTests(unittest.TestCase):
    def test_numeric_comparison_handles_two_digit_patch_minor_major(self):
        for current, remote in (("0.7.9", "v0.7.10"), ("0.9.99", "0.10.0"),
                                ("9.99.99", "v10.0.0")):
            with self.subTest(current=current, remote=remote):
                release = parse_release(payload(remote), current)
                self.assertEqual(release.version, remote.removeprefix("v"))

    def test_equal_and_older_do_not_offer_downgrade(self):
        self.assertIsNone(parse_release(payload("v0.7.21"), "0.7.21"))
        self.assertIsNone(parse_release(payload("v0.7.20"), "0.7.21"))

    def test_rejects_preview_draft_missing_flags_and_malformed_data(self):
        invalid = [None, [], {}, {"tag_name": "v1.0.0"},
                   {"tag_name": "v1.0.0", "draft": True, "prerelease": False},
                   {"tag_name": "v1.0.0", "draft": False, "prerelease": True},
                   {"tag_name": "v1.0.0", "draft": 0, "prerelease": False}]
        invalid += [payload(tag) for tag in (None, 123, "1.0", "01.0.0", "v1.0.0-rc1",
                                            "1.0.0+dev", "v1.0.0/path", "v１.0.0", " 1.0.0")]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_release(item, "0.7.21")

    def test_release_url_comes_only_from_fixed_repository_and_validated_tag(self):
        release = parse_release(payload(html_url="https://example.com/malicious", body="<script/>"), "0.7.21")
        self.assertEqual(release, ReleaseInfo("0.7.22", "v0.7.22",
                         "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v0.7.22"))

    def test_only_matching_verified_windows_release_assets_are_available(self):
        entries = []
        for suffix in ("-portable.zip", ".exe", "-source.zip"):
            name = f"Lab-Control-Center-0.7.22-windows-x64{suffix}"
            entries.append(dict(name=name, state="uploaded", size=123,
                                digest="sha256:" + "A" * 64,
                                browser_download_url=f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/{name}"))
        release = parse_release(payload(assets=entries), "0.7.21")
        self.assertEqual(len(release.assets), 2)
        self.assertTrue(all(isinstance(asset, ReleaseAsset) for asset in release.assets))
        self.assertTrue(all(asset.sha256 == "a" * 64 for asset in release.assets))
        self.assertEqual({asset.name for asset in release.assets}, {entry["name"] for entry in entries[:2]})

    def test_bad_or_missing_download_metadata_keeps_the_version_notice(self):
        name = "Lab-Control-Center-0.7.22-windows-x64.exe"
        valid = dict(name=name, state="uploaded", size=123, digest="sha256:" + "a" * 64,
                     browser_download_url=f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.22/{name}")
        for changed in ({"digest": None}, {"digest": "sha256:bad"}, {"size": True}, {"size": 0},
                        {"size": MAX_DOWNLOAD_BYTES + 1}, {"state": "new"},
                        {"browser_download_url": "https://example.com/update.exe"},
                        {"name": "Lab-Control-Center-0.7.21-windows-x64.exe"}):
            with self.subTest(changed=changed):
                release = parse_release(payload(assets=[dict(valid, **changed)]), "0.7.21")
                self.assertEqual(release.version, "0.7.22")
                self.assertEqual(release.assets, ())
        self.assertEqual(parse_release(payload(assets=None), "0.7.21").assets, ())

    def test_system_proxy_lookup_uses_the_requested_url(self):
        target = "https://release-assets.githubusercontent.com/asset?signature=signed"
        direct = QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
        with patch("rigol_control.updates.QNetworkProxyFactory.systemProxyForQuery", return_value=[direct]) as lookup:
            self.assertEqual(_system_https_proxy(target).type(), QNetworkProxy.ProxyType.NoProxy)
        self.assertEqual(lookup.call_args.args[0].url().toString(), target)

    def test_publisher_manifest_metadata_matches_api_assets_including_setup(self):
        manifest = manifest_payload()
        api_assets = [dict(name=entry["file"], state="uploaded", size=entry["bytes"],
                           digest="sha256:" + entry["sha256"], browser_download_url=
                           f'https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/{entry["file"]}')
                      for entry in manifest["artifacts"]]
        self.assertEqual(_manifest_release_info(manifest, "v0.7.24"),
                         parse_release(payload("v0.7.24", assets=api_assets), "0.7.21"))

    def test_manifest_identity_schema_and_artifact_corruption_is_rejected(self):
        invalid = [None, [], {}, dict(manifest_payload(), version="v0.7.24"),
                   dict(manifest_payload(), version="0.7.25"), dict(manifest_payload(), platform="linux-x64"),
                   dict(manifest_payload(), source_dirty=0), dict(manifest_payload(), source_dirty=True),
                   dict(manifest_payload(), source_commit="a" * 39), dict(manifest_payload(), source_commit=None),
                   dict(manifest_payload(), schema_version=2), dict(manifest_payload(), schema_version=True),
                   dict(manifest_payload(), artifacts=[]), dict(manifest_payload(), artifacts="bad")]
        for field, value in (("file", "../setup.exe"), ("file", "Lab-Control-Center-0.7.25-windows-x64-setup.exe"),
                             ("bytes", True), ("bytes", 0), ("bytes", MAX_DOWNLOAD_BYTES + 1),
                             ("sha256", "a" * 63), ("sha256", None)):
            item = manifest_payload()
            item["artifacts"][2][field] = value
            invalid.append(item)
        duplicate = manifest_payload()
        duplicate["artifacts"].append(dict(duplicate["artifacts"][0]))
        invalid.append(duplicate)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                _manifest_release_info(item, "v0.7.24")

    def test_manifest_redirect_urls_cannot_escape_project_tag_and_https(self):
        base = "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/release-manifest.json"
        self.assertEqual(_manifest_target(base, None), "v0.7.24")
        self.assertIsNone(_manifest_target(LATEST_MANIFEST_URL, None, initial=True))
        cdn = "https://release-assets.githubusercontent.com/github-production-release-asset/123/abc?signature=private"
        self.assertEqual(_manifest_target(cdn, "v0.7.24"), "v0.7.24")
        bad = [(base.replace("https:", "http:"), None), (base.replace("github.com", "github.com.evil.test"), None),
               (base.replace("Rechoncy", "another-owner"), None), (base.replace("v0.7.24", "v0.7.24-rc1"), None),
               (base.replace("/v0.7.24/", "/v0.7.25/"), "v0.7.24"),
               (base.replace("github.com", "user@github.com"), None), (base + "?url=evil", None),
               (base + "#fragment", None), (base.replace("github.com", "github.com:444"), None),
               (base.replace("/download/", "/download%2F"), None), (cdn, None),
               (cdn.replace("github-production-release-asset", "arbitrary-path"), "v0.7.24"),
               (LATEST_MANIFEST_URL, "v0.7.24")]
        for url, tag in bad:
            with self.subTest(url=url, tag=tag), self.assertRaises(ValueError):
                _manifest_target(url, tag)


class UpdateCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["update-tests"])

    def setUp(self):
        self.manager = FakeManager()
        self.clock = FakeClock()
        self.wall_clock = FakeClock(1_700_000_000)
        self.checker = UpdateChecker(manager=self.manager, current_version="0.7.21",
                                     clock=self.clock, wall_clock=self.wall_clock)
        self.updates, self.current, self.errors, self.busy = [], [], [], []
        self.checker.update_available.connect(self.updates.append)
        self.checker.up_to_date.connect(self.current.append)
        self.checker.failed.connect(self.errors.append)
        self.checker.checking_changed.connect(self.busy.append)

    def tearDown(self):
        self.checker.close()
        self.app.processEvents()

    def wait(self, condition, timeout=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.001)
        return condition()

    def begin_manifest(self, *, status=403, headers=None):
        self.assertTrue(self.checker.check())
        reply = self.manager.replies[-1]
        reply.status = status
        reply.headers = headers if headers is not None else {b"x-ratelimit-remaining": b"0"}
        reply.deliver(b'{"message":"API rate limit exceeded"}')
        self.assertEqual(self.manager.requests[-1].url().toString(), LATEST_MANIFEST_URL)
        return reply

    def finish_manifest(self, *, tag="v0.7.24", manifest=None, cdn=False):
        reply = self.manager.replies[-1]
        reply.status = 302
        reply.location = QUrl(f"https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/{tag}/release-manifest.json")
        reply.deliver(b"")
        if cdn:
            reply = self.manager.replies[-1]
            reply.status = 302
            reply.location = QUrl("https://release-assets.githubusercontent.com/github-production-release-asset/123/abc?signature=private")
            reply.deliver(b"")
        self.manager.replies[-1].deliver(json.dumps(manifest_payload() if manifest is None else manifest).encode())

    def test_request_is_public_fixed_https_bounded_and_does_not_follow_redirects(self):
        self.assertTrue(self.checker.check())
        request = self.manager.requests[0]
        self.assertEqual(request.url().toString(), LATEST_RELEASE_URL)
        self.assertEqual(bytes(request.rawHeader(b"Accept")), b"application/vnd.github+json")
        self.assertEqual(bytes(request.rawHeader(b"User-Agent")), b"Lab-Control-Center/0.7.21")
        self.assertFalse(request.hasRawHeader(b"Authorization"))
        self.assertEqual(request.transferTimeout(), 15_000)
        self.assertEqual(request.attribute(QNetworkRequest.Attribute.RedirectPolicyAttribute),
                         QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        self.assertEqual(self.checker._deadline.interval(), 15_000)
        self.assertEqual(self.manager.replies[0].buffer_limit, MAX_RESPONSE_BYTES + 1)

    def test_newer_release_delivered_once_after_chunked_read(self):
        self.assertTrue(self.checker.check())
        reply = self.manager.replies[0]
        body = json.dumps(payload()).encode()
        reply.deliver(body[:20], finished=False)
        self.assertTrue(self.checker.checking)
        self.assertFalse(self.updates)
        reply.deliver(body[20:])
        reply.finished.emit()
        self.assertEqual([item.version for item in self.updates], ["0.7.22"])
        self.assertEqual(self.busy, [True, False])
        self.assertFalse(self.errors)
        self.assertFalse(self.checker.checking)
        self.assertTrue(reply.deleted)

    def test_equal_and_older_response_report_actual_latest_without_update(self):
        for tag in ("v0.7.21", "v0.7.20"):
            self.clock.advance(61)
            self.checker.check()
            self.manager.replies[-1].deliver(json.dumps(payload(tag)).encode())
        self.assertEqual(self.current, ["0.7.21", "0.7.20"])
        self.assertFalse(self.updates)
        self.assertFalse(self.errors)

    def test_real_qiodevice_eof_none_does_not_escape_finished_slot(self):
        self.checker.check()
        reply = self.manager.replies[-1]
        reply.eof_returns_none = True
        reply.deliver(json.dumps(payload()).encode())
        self.assertEqual([item.version for item in self.updates], ["0.7.22"])
        self.assertFalse(self.checker.checking)
        self.assertFalse(self.errors)

    def test_repeated_checks_do_not_start_concurrent_request(self):
        self.assertTrue(self.checker.check())
        self.assertFalse(self.checker.check())
        self.assertEqual(len(self.manager.requests), 1)
        self.manager.replies[-1].deliver(json.dumps(payload()).encode())
        self.clock.advance(61)
        self.assertTrue(self.checker.check())
        self.assertEqual(len(self.manager.requests), 2)

    def test_http_failures_and_tls_errors_are_not_treated_as_up_to_date(self):
        for status, error in ((403, QNetworkReply.NetworkError.ContentAccessDenied),
                              (404, QNetworkReply.NetworkError.ContentNotFoundError),
                              (302, QNetworkReply.NetworkError.NoError),
                              (500, QNetworkReply.NetworkError.InternalServerError),
                              (None, QNetworkReply.NetworkError.SslHandshakeFailedError)):
            with self.subTest(status=status, error=error):
                self.clock.advance(61)
                self.checker.check()
                reply = self.manager.replies[-1]
                reply.status, reply.network_error = status, error
                reply.deliver(b"failure")
                self.assertFalse(self.checker.checking)
                self.assertTrue(reply.aborted)
        self.assertEqual(len(self.errors), 5)
        self.assertIn("403", self.errors[0])
        self.assertIn("重定向", self.errors[2])
        self.assertFalse(self.current)
        self.assertFalse(self.updates)

    def test_invalid_json_encoding_preview_and_bad_version_are_failures(self):
        for body in (b"not json", b"\xff", b"[]", json.dumps(payload("v1.0.0-rc1")).encode(),
                     b'{"tag_name":"v1.0.0","draft":false,"prerelease":true}'):
            self.clock.advance(31)
            self.checker.check()
            self.manager.replies[-1].deliver(body)
        self.assertEqual(len(self.errors), 5)
        self.assertFalse(self.current)
        self.assertFalse(self.updates)

    def test_oversized_stream_is_aborted_before_it_can_accumulate(self):
        self.checker.check()
        reply = self.manager.replies[-1]
        reply.deliver(b"x" * (MAX_RESPONSE_BYTES + 5000), finished=False)
        self.assertTrue(reply.aborted)
        self.assertFalse(self.checker.checking)
        self.assertEqual(reply.read_limits, [MAX_RESPONSE_BYTES + 1])
        self.assertEqual(self.checker._body, b"")
        self.assertEqual(len(self.errors), 1)
        self.assertIn("过大", self.errors[0])

    def test_deeply_nested_json_does_not_escape_qt_callback(self):
        self.checker.check()
        self.manager.replies[-1].deliver(b"[" * 2000 + b"]" * 2000)
        self.assertFalse(self.checker.checking)
        self.assertEqual(len(self.errors), 1)
        self.assertFalse(self.updates)

    def test_deadline_keeps_event_loop_responsive_and_allows_retry(self):
        self.checker._deadline.setInterval(25)
        self.checker.check()
        ticks = []
        QTimer.singleShot(1, lambda: ticks.append(True))
        self.assertTrue(self.wait(lambda: bool(self.errors)))
        self.assertEqual(ticks, [True])
        self.assertTrue(self.manager.replies[0].aborted)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("超时", self.errors[0])
        self.clock.advance(31)
        self.assertTrue(self.checker.check())

    def test_cancel_is_silent_and_late_reply_cannot_replace_a_fresh_check(self):
        self.checker.check()
        old = self.manager.replies[-1]
        self.checker.cancel()
        self.assertTrue(old.aborted)
        self.assertFalse(self.errors)
        self.assertTrue(self.checker.check())
        old.deliver(json.dumps(payload("v99.0.0")).encode())
        self.assertFalse(self.updates)
        self.assertTrue(self.checker.checking)
        self.manager.replies[-1].deliver(json.dumps(payload()).encode())
        self.assertEqual([item.version for item in self.updates], ["0.7.22"])

    def test_close_is_silent_and_prevents_future_requests(self):
        self.checker.check()
        self.checker.close()
        self.manager.replies[-1].deliver(json.dumps(payload()).encode())
        self.assertFalse(self.checker.check())
        self.assertFalse(self.errors)
        self.assertFalse(self.updates)
        self.assertEqual(self.busy, [True, False])

    def test_request_submission_failure_does_not_stick_or_prevent_retry(self):
        self.manager.raise_on_get = True
        self.assertFalse(self.checker.check())
        self.assertFalse(self.checker.checking)
        self.assertEqual(len(self.errors), 1)
        self.manager.raise_on_get = False
        self.clock.advance(31)
        self.assertTrue(self.checker.check())

    def test_fixed_system_proxy_is_local_to_manager_without_global_mutation(self):
        from PyQt6.QtNetwork import QNetworkProxyFactory
        previous = QNetworkProxyFactory.usesSystemConfiguration()
        proxy = QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", 7897)
        checker = UpdateChecker(manager=self.manager, proxy_resolver=lambda: proxy,
                                clock=self.clock, wall_clock=self.wall_clock)
        try:
            self.assertTrue(checker.check())
            self.assertTrue(self.wait(lambda: bool(self.manager.requests)))
            self.assertEqual(self.manager.proxy, proxy)
            self.assertEqual(QNetworkProxyFactory.usesSystemConfiguration(), previous)
        finally:
            checker.close()

    def test_blocked_proxy_lookup_keeps_gui_responsive_and_deadline_effective(self):
        entered, release = Event(), Event()
        lookup_threads = []

        def resolve():
            lookup_threads.append(get_ident())
            entered.set()
            release.wait(2)
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        checker = UpdateChecker(manager=self.manager, proxy_resolver=resolve,
                                clock=self.clock, wall_clock=self.wall_clock)
        errors, ticks = [], []
        checker.failed.connect(errors.append)
        checker._deadline.setInterval(25)
        try:
            self.assertTrue(checker.check())
            self.assertTrue(self.wait(entered.is_set))
            QTimer.singleShot(1, lambda: ticks.append(True))
            self.assertTrue(self.wait(lambda: bool(errors)))
            self.assertEqual(ticks, [True])
            self.assertNotEqual(lookup_threads, [get_ident()])
            self.assertIn("超时", errors[0])
            self.assertFalse(checker.checking)
            self.assertFalse(self.manager.requests)
            self.assertFalse(checker.check())
            self.assertIn("仍在进行", errors[-1])
            self.assertEqual(len(lookup_threads), 1, "Do not accumulate blocked lookup threads")
            release.set()
            self.assertTrue(self.wait(lambda: not checker._proxy_thread.is_alive()))
            self.app.processEvents()
            self.assertFalse(self.manager.requests, "Late proxy results must not launch a timed-out request")
            checker._deadline.setInterval(1000)
            self.clock.advance(31)
            self.assertTrue(checker.check())
            self.assertTrue(self.wait(lambda: bool(self.manager.requests)))
        finally:
            release.set()
            checker.close()

    def test_close_during_proxy_lookup_is_immediate_and_discards_late_result(self):
        entered, release = Event(), Event()

        def resolve():
            entered.set()
            release.wait(2)
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)

        checker = UpdateChecker(manager=self.manager, proxy_resolver=resolve,
                                clock=self.clock, wall_clock=self.wall_clock)
        errors = []
        checker.failed.connect(errors.append)
        try:
            checker.check()
            self.assertTrue(self.wait(entered.is_set))
            checker.close()
            self.assertTrue(checker._proxy_thread.is_alive(), "Closing must not wait for the native lookup")
            self.assertFalse(checker.checking)
            release.set()
            self.assertTrue(self.wait(lambda: not checker._proxy_thread.is_alive()))
            self.app.processEvents()
            self.assertFalse(self.manager.requests)
            self.assertFalse(errors)
        finally:
            release.set()
            checker.close()

    def test_proxy_failure_or_thread_start_failure_is_recoverable(self):
        def fail():
            raise OSError("lookup failure")

        checker = UpdateChecker(manager=self.manager, proxy_resolver=fail,
                                clock=self.clock, wall_clock=self.wall_clock)
        errors = []
        checker.failed.connect(errors.append)
        try:
            checker.check()
            self.assertTrue(self.wait(lambda: bool(errors)))
            self.assertFalse(checker.checking)
            self.assertFalse(self.manager.requests)
            checker._proxy_resolver = lambda: QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
            self.clock.advance(31)
            with patch("rigol_control.updates.Thread.start", side_effect=RuntimeError("thread failure")):
                self.assertFalse(checker.check())
            self.assertFalse(checker.checking)
            self.assertEqual(len(errors), 2)
            self.clock.advance(31)
            self.assertTrue(checker.check())
            self.assertTrue(self.wait(lambda: bool(self.manager.requests)))
        finally:
            checker.close()

    def test_confirmed_rate_limit_uses_official_manifest_with_setup_metadata(self):
        self.checker.check()
        reply = self.manager.replies[-1]
        reply.status = 403
        reply.headers = {b"x-ratelimit-remaining": b"0", b"retry-after": b"120"}
        reply.deliver(b'{"message":"API rate limit exceeded"}')
        self.assertEqual(len(self.manager.requests), 2)
        self.assertEqual(self.manager.requests[-1].url().toString(),
                         "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/latest/download/release-manifest.json")
        self.assertTrue(self.checker.checking)
        redirect = self.manager.replies[-1]
        redirect.status = 302
        redirect.location = QUrl("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/release-manifest.json")
        redirect.deliver(b"")
        self.manager.replies[-1].deliver(json.dumps(manifest_payload()).encode())
        self.assertEqual([x.version for x in self.updates], ["0.7.24"])
        self.assertEqual({x.name for x in self.updates[0].assets}, {
            "Lab-Control-Center-0.7.24-windows-x64-portable.zip",
            "Lab-Control-Center-0.7.24-windows-x64.exe",
            "Lab-Control-Center-0.7.24-windows-x64-setup.exe"})
        self.assertFalse(self.errors)
        self.assertEqual(self.busy, [True, False])

    def test_ordinary_forbidden_response_is_not_claimed_to_be_rate_limited(self):
        self.checker.check()
        self.manager.replies[-1].status = 403
        self.manager.replies[-1].deliver(b'{"message":"Access denied by organization proxy"}')
        self.assertEqual(len(self.manager.requests), 1)
        self.assertIn("403", self.errors[-1])
        self.assertNotIn("限制请求", self.errors[-1])

    def test_rapid_failure_retry_does_not_send_another_request(self):
        self.checker.check()
        self.manager.replies[-1].status = 500
        self.manager.replies[-1].deliver(b"unavailable")
        self.assertFalse(self.checker.check())
        self.assertEqual(len(self.manager.requests), 1)
        self.assertIn("秒", self.errors[-1])

    def test_successful_check_is_cached_for_rapid_manual_rechecks(self):
        self.checker.check()
        self.manager.replies[-1].deliver(json.dumps(payload("v0.7.24")).encode())
        self.assertTrue(self.checker.check())
        self.app.processEvents()
        self.assertEqual(len(self.manager.requests), 1)
        self.assertEqual([x.version for x in self.updates], ["0.7.24", "0.7.24"])

    def test_api_backoff_uses_manifest_until_reset_and_success_cache_expires(self):
        self.begin_manifest(headers={b"x-ratelimit-remaining": b"0", b"retry-after": b"120",
                                     b"x-ratelimit-reset": str(int(self.wall_clock() + 3600)).encode()})
        self.finish_manifest(cdn=True)
        self.assertEqual(self.checker.last_source, "manifest")
        self.clock.advance(61)
        self.assertTrue(self.checker.check())
        self.assertEqual(self.manager.requests[-1].url().toString(), LATEST_MANIFEST_URL)
        self.finish_manifest()
        self.clock.advance(3540)
        self.assertTrue(self.checker.check())
        self.assertEqual(self.manager.requests[-1].url().toString(), LATEST_RELEASE_URL)

    def test_rate_limit_signals_and_http_retry_date_are_honored(self):
        from email.utils import formatdate
        for status, headers, body, seconds in (
            (403, {b"retry-after": formatdate(self.wall_clock() + 180, usegmt=True).encode()}, b'{}', 180),
            (403, {}, b'{"message":"You have exceeded a secondary rate limit"}', 60),
            (429, {b"retry-after": b"90"}, b"not json", 90),
        ):
            with self.subTest(status=status, headers=headers):
                manager = FakeManager()
                checker = UpdateChecker(manager=manager, clock=self.clock, wall_clock=self.wall_clock)
                try:
                    checker.check()
                    reply = manager.replies[-1]
                    reply.status, reply.headers = status, headers
                    reply.deliver(body)
                    self.assertEqual(manager.requests[-1].url().toString(), LATEST_MANIFEST_URL)
                    self.assertEqual(checker._api_retry_after, self.clock() + seconds)
                finally:
                    checker.close()

    def test_equal_and_older_manifest_report_actual_version_without_downgrade(self):
        for version in ("0.7.21", "0.7.20"):
            self.clock.advance(61)
            if not self.manager.requests:
                self.begin_manifest(headers={b"retry-after": b"3600"})
            else:
                self.assertTrue(self.checker.check())
            self.finish_manifest(tag="v" + version, manifest=manifest_payload(version))
        self.assertEqual(self.current, ["0.7.21", "0.7.20"])
        self.assertFalse(self.updates)
        self.assertFalse(self.errors)

    def test_fallback_never_loops_if_manifest_is_also_rate_limited(self):
        self.begin_manifest(status=429)
        reply = self.manager.replies[-1]
        reply.status, reply.headers = 429, {b"retry-after": b"300"}
        reply.deliver(b"limited")
        self.assertEqual(len(self.manager.requests), 2)
        self.assertFalse(self.checker.checking)
        self.assertIn("发布清单", self.errors[-1])
        self.clock.advance(299)
        self.assertFalse(self.checker.check())
        self.assertEqual(len(self.manager.requests), 2)
        self.clock.advance(2)
        self.assertTrue(self.checker.check())
        # Only the API's own independent backoff has elapsed at this point.
        self.assertEqual(self.manager.requests[-1].url().toString(), LATEST_RELEASE_URL)

    def test_manifest_redirect_refuses_cross_repository_without_issuing_request(self):
        self.begin_manifest()
        reply = self.manager.replies[-1]
        reply.status = 302
        reply.location = QUrl("https://github.com/another/project/releases/download/v0.7.24/release-manifest.json")
        reply.deliver(b"")
        self.assertEqual(len(self.manager.requests), 2)
        self.assertFalse(self.updates)
        self.assertIn("不可信", self.errors[-1])

    def test_manifest_redirect_limit_keeps_one_total_deadline(self):
        self.begin_manifest()
        deadline_id = self.checker._deadline.timerId()
        target = QUrl("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/release-manifest.json")
        for _ in range(6):
            reply = self.manager.replies[-1]
            reply.status, reply.location = 302, target
            reply.deliver(b"")
            if self.checker.checking:
                self.assertEqual(self.checker._deadline.timerId(), deadline_id)
        self.assertFalse(self.checker.checking)
        self.assertEqual(len(self.manager.requests), 7)
        self.assertIn("跳转次数", self.errors[-1])

    def test_manifest_payload_must_match_observed_tag_and_not_repeat_json_fields(self):
        self.begin_manifest()
        self.finish_manifest(manifest=manifest_payload("0.7.25"))
        self.assertFalse(self.updates)
        self.assertIn("无效", self.errors[-1])
        self.clock.advance(31)
        self.checker.check()
        reply = self.manager.replies[-1]
        reply.status = 302
        reply.location = QUrl("https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/release-manifest.json")
        reply.deliver(b"")
        body = json.dumps(manifest_payload()).encode().replace(b'"version": "0.7.24"', b'"version":"0.7.24","version":"0.7.24"')
        self.manager.replies[-1].deliver(body)
        self.assertEqual(len(self.errors), 2)
        self.assertFalse(self.updates)

    def test_manifest_without_validated_tag_and_oversize_body_are_not_updates(self):
        self.begin_manifest()
        self.manager.replies[-1].deliver(json.dumps(manifest_payload()).encode())
        self.assertIn("无效", self.errors[-1])
        self.clock.advance(31)
        self.checker.check()
        self.manager.replies[-1].deliver(b"x" * (MAX_RESPONSE_BYTES + 1), finished=False)
        self.assertIn("过大", self.errors[-1])
        self.assertFalse(self.updates)

    def test_fallback_timeout_is_still_the_original_responsive_deadline(self):
        self.checker._deadline.setInterval(25)
        self.begin_manifest()
        ticks = []
        QTimer.singleShot(1, lambda: ticks.append(True))
        self.assertTrue(self.wait(lambda: bool(self.errors)))
        self.assertEqual(ticks, [True])
        self.assertIn("超时", self.errors[-1])
        self.assertFalse(self.updates)

    def test_cancel_or_close_fallback_rejects_late_reply_and_cached_delivery(self):
        old_api = self.begin_manifest()
        old_manifest = self.manager.replies[-1]
        self.checker.cancel()
        old_api.deliver(json.dumps(payload("v99.0.0")).encode())
        old_manifest.deliver(json.dumps(manifest_payload()).encode())
        self.assertFalse(self.updates)
        self.assertFalse(self.errors)
        self.assertTrue(self.checker.check())
        self.finish_manifest()
        self.assertEqual(len(self.updates), 1)
        self.assertTrue(self.checker.check())
        self.checker.cancel()
        self.app.processEvents()
        self.assertEqual(len(self.updates), 1)
        self.assertTrue(self.checker.check())
        self.checker.close()
        self.app.processEvents()
        self.assertEqual(len(self.updates), 1)

    def test_closing_or_starting_check_when_busy_clears_cannot_publish_old_release(self):
        self.checker.checking_changed.connect(lambda busy: None if busy else self.checker.close())
        self.checker.check()
        self.manager.replies[-1].deliver(json.dumps(payload("v99.0.0")).encode())
        self.assertFalse(self.updates)
        self.assertFalse(self.errors)
        # A new generation from checking_changed(False) also invalidates old
        # delivery, even if the replacement completes via the cached result.
        other = UpdateChecker(manager=self.manager, current_version="0.7.21", clock=self.clock)
        values, started = [], []
        other.update_available.connect(values.append)
        def restart(busy):
            if not busy and not started:
                started.append(True)
                other.check()
        other.checking_changed.connect(restart)
        try:
            other.check()
            self.manager.replies[-1].deliver(json.dumps(payload()).encode())
            self.assertEqual(values, [])
            self.app.processEvents()
            self.assertEqual([value.version for value in values], ["0.7.22"])
        finally:
            other.close()

    def test_fallback_resolves_each_real_url_off_gui_without_global_proxy_changes(self):
        calls = []
        def resolve(url):
            calls.append((url, get_ident()))
            return QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
        checker = UpdateChecker(manager=self.manager, proxy_resolver=resolve, clock=self.clock,
                                wall_clock=self.wall_clock, current_version="0.7.21")
        results = []
        checker.update_available.connect(results.append)
        try:
            checker.check()
            self.assertTrue(self.wait(lambda: len(self.manager.requests) == 1))
            reply = self.manager.replies[-1]
            reply.status, reply.headers = 403, {b"x-ratelimit-remaining": b"0"}
            reply.deliver(b"limited")
            self.assertTrue(self.wait(lambda: len(self.manager.requests) == 2))
            target = "https://github.com/Rechoncy/Rigol_Generator_GUI/releases/download/v0.7.24/release-manifest.json"
            reply = self.manager.replies[-1]
            reply.status, reply.location = 302, QUrl(target)
            reply.deliver(b"")
            self.assertTrue(self.wait(lambda: len(self.manager.requests) == 3))
            self.manager.replies[-1].deliver(json.dumps(manifest_payload()).encode())
            self.assertEqual([value.version for value in results], ["0.7.24"])
            self.assertEqual([url for url, _ in calls], [LATEST_RELEASE_URL, LATEST_MANIFEST_URL, target])
            self.assertTrue(all(thread != get_ident() for _, thread in calls))
        finally:
            checker.close()


if __name__ == "__main__":
    unittest.main()
