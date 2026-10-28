import json
import os
import time
import unittest
from threading import Event, get_ident
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtNetwork import QNetworkProxy, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QApplication

from rigol_control.updates import (
    LATEST_RELEASE_URL,
    MAX_RESPONSE_BYTES,
    ReleaseInfo,
    UpdateChecker,
    parse_release,
)


def payload(tag="v0.7.22", **values):
    return dict(tag_name=tag, draft=False, prerelease=False, **values)


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
        assert code == QNetworkRequest.Attribute.HttpStatusCodeAttribute
        return self.status

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


class UpdateCheckerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["update-tests"])

    def setUp(self):
        self.manager = FakeManager()
        self.checker = UpdateChecker(manager=self.manager, current_version="0.7.21")
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
        self.assertTrue(self.checker.check())
        self.assertEqual(len(self.manager.requests), 2)

    def test_http_failures_and_tls_errors_are_not_treated_as_up_to_date(self):
        for status, error in ((403, QNetworkReply.NetworkError.ContentAccessDenied),
                              (429, QNetworkReply.NetworkError.UnknownContentError),
                              (404, QNetworkReply.NetworkError.ContentNotFoundError),
                              (302, QNetworkReply.NetworkError.NoError),
                              (500, QNetworkReply.NetworkError.InternalServerError),
                              (None, QNetworkReply.NetworkError.SslHandshakeFailedError)):
            with self.subTest(status=status, error=error):
                self.checker.check()
                reply = self.manager.replies[-1]
                reply.status, reply.network_error = status, error
                reply.deliver(b"failure")
                self.assertFalse(self.checker.checking)
                self.assertTrue(reply.aborted)
        self.assertEqual(len(self.errors), 6)
        self.assertIn("限制", self.errors[0])
        self.assertIn("重定向", self.errors[3])
        self.assertFalse(self.current)
        self.assertFalse(self.updates)

    def test_invalid_json_encoding_preview_and_bad_version_are_failures(self):
        for body in (b"not json", b"\xff", b"[]", json.dumps(payload("v1.0.0-rc1")).encode(),
                     b'{"tag_name":"v1.0.0","draft":false,"prerelease":true}'):
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
        self.assertTrue(self.checker.check())

    def test_fixed_system_proxy_is_local_to_manager_without_global_mutation(self):
        from PyQt6.QtNetwork import QNetworkProxyFactory
        previous = QNetworkProxyFactory.usesSystemConfiguration()
        proxy = QNetworkProxy(QNetworkProxy.ProxyType.HttpProxy, "127.0.0.1", 7897)
        checker = UpdateChecker(manager=self.manager, proxy_resolver=lambda: proxy)
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

        checker = UpdateChecker(manager=self.manager, proxy_resolver=resolve)
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

        checker = UpdateChecker(manager=self.manager, proxy_resolver=resolve)
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

        checker = UpdateChecker(manager=self.manager, proxy_resolver=fail)
        errors = []
        checker.failed.connect(errors.append)
        try:
            checker.check()
            self.assertTrue(self.wait(lambda: bool(errors)))
            self.assertFalse(checker.checking)
            self.assertFalse(self.manager.requests)
            checker._proxy_resolver = lambda: QNetworkProxy(QNetworkProxy.ProxyType.NoProxy)
            with patch("rigol_control.updates.Thread.start", side_effect=RuntimeError("thread failure")):
                self.assertFalse(checker.check())
            self.assertFalse(checker.checking)
            self.assertEqual(len(errors), 2)
            self.assertTrue(checker.check())
            self.assertTrue(self.wait(lambda: bool(self.manager.requests)))
        finally:
            checker.close()


if __name__ == "__main__":
    unittest.main()
