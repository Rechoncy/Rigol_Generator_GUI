import csv
import io
import threading
import time
import unittest
from unittest.mock import patch

from rigol_control.analog import (
    AnalogAcquisitionController,
    AnalogAcquisitionState,
    ContinuousAnalogTextWriter,
)
from rigol_control.drivers.ni_analog import AnalogInputBlock


BLOCK = AnalogInputBlock(("ai0",), 1000.0, 0, ((0.1, 0.2),))


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class FlushFailureStream(io.StringIO):
    def __init__(self, *, close_failures=0):
        super().__init__()
        self.close_attempts = 0
        self.close_failures = close_failures

    def flush(self):
        raise OSError("disk full")

    def close(self):
        self.close_attempts += 1
        if self.close_attempts <= self.close_failures:
            raise OSError("close blocked")
        super().close()


def install_stream(writer, stream):
    writer._stream = stream
    writer._writer = csv.writer(stream)
    writer._is_open = True


class AnalogRecordingFailureTests(unittest.TestCase):
    def test_failed_flush_is_not_counted_and_close_is_still_attempted(self):
        stream = FlushFailureStream()
        writer = ContinuousAnalogTextWriter("unused.csv")
        install_stream(writer, stream)
        with self.assertRaisesRegex(OSError, "disk full"):
            writer.write_block(BLOCK)
        self.assertEqual(writer.saved_samples, 0)
        with self.assertRaisesRegex(OSError, "disk full"):
            writer.close()
        self.assertTrue(stream.closed)
        self.assertEqual(stream.close_attempts, 1)
        self.assertFalse(writer.is_open)

    def test_disk_failure_publishes_error_and_stops_reader(self):
        class Reader:
            stop_calls = 0

            def start(self):
                pass

            def read(self):
                return BLOCK

            def stop(self):
                self.stop_calls += 1

        reader = Reader()
        controller = AnalogAcquisitionController(reader, stream_path="unused.csv")
        stream = FlushFailureStream()
        with patch.object(controller.stream_writer, "open", lambda: install_stream(controller.stream_writer, stream)):
            controller.start()
        self.assertTrue(wait_until(lambda: controller.snapshot().state is AnalogAcquisitionState.ERROR))
        self.assertTrue(wait_until(lambda: not controller.is_running))
        snapshot = controller.snapshot()
        self.assertIn("disk full", snapshot.error)
        self.assertEqual(snapshot.saved_samples, 0)
        self.assertTrue(stream.closed)
        self.assertGreaterEqual(reader.stop_calls, 1)

    def test_failed_close_retains_handle_until_an_explicit_retry(self):
        stream = FlushFailureStream(close_failures=1)
        writer = ContinuousAnalogTextWriter("unused.csv")
        install_stream(writer, stream)
        with self.assertRaisesRegex(OSError, "disk full.*close blocked"):
            writer.close()
        self.assertTrue(writer.is_open)
        self.assertFalse(stream.closed)
        with self.assertRaisesRegex(OSError, "disk full"):
            writer.close()
        self.assertFalse(writer.is_open)
        self.assertTrue(stream.closed)
        self.assertEqual(stream.close_attempts, 2)

    def test_one_failed_flush_after_stop_request_is_still_reported(self):
        flush_entered = threading.Event()
        release_flush = threading.Event()

        class OneFailureStream(io.StringIO):
            flush_calls = 0

            def flush(self):
                self.flush_calls += 1
                if self.flush_calls == 1:
                    flush_entered.set()
                    if not release_flush.wait(2):
                        raise RuntimeError("test flush was not released")
                    raise OSError("one disk failure")
                return super().flush()

        reader = BlockingReader(first_block=BLOCK)
        controller = AnalogAcquisitionController(reader, stream_path="unused.csv")
        stream = OneFailureStream()
        try:
            with patch.object(controller.stream_writer, "open", lambda: install_stream(controller.stream_writer, stream)):
                controller.start()
            self.assertTrue(flush_entered.wait(1))
            controller.request_stop()
            release_flush.set()
            self.assertTrue(wait_until(lambda: controller.is_quiescent))
            snapshot = controller.snapshot()
            self.assertEqual(snapshot.state, AnalogAcquisitionState.ERROR)
            self.assertIn("one disk failure", snapshot.error)
            self.assertEqual(snapshot.saved_samples, 0)
            self.assertEqual(snapshot.total_samples, 2)
            self.assertTrue(stream.closed)
        finally:
            release_flush.set()
            reader.release_read.set()
            controller.stop()


class BlockingReader:
    def __init__(self, *, stop_failures=0, first_block=None):
        self.read_entered = threading.Event()
        self.release_read = threading.Event()
        self.first_block = first_block
        self.stop_failures = stop_failures
        self.stop_calls = 0
        self.read_calls = 0

    def start(self):
        pass

    def read(self):
        self.read_calls += 1
        self.read_entered.set()
        if self.first_block is not None:
            block, self.first_block = self.first_block, None
            return block
        if not self.release_read.wait(3):
            raise RuntimeError("test read was not released")
        raise OSError("read aborted")

    def stop(self):
        self.stop_calls += 1
        if self.stop_calls <= self.stop_failures:
            raise OSError("NI close failed")
        self.release_read.set()


class AnalogLifecycleTests(unittest.TestCase):
    def test_failed_reader_cleanup_keeps_lease_and_requires_explicit_retry(self):
        reader = BlockingReader(stop_failures=1)
        controller = AnalogAcquisitionController(reader)
        controller.start()
        self.assertTrue(reader.read_entered.wait(1))
        try:
            with self.assertRaisesRegex(RuntimeError, "NI close failed"):
                controller.stop(timeout_seconds=0.2)
            failed = controller.snapshot()
            self.assertEqual(failed.state, AnalogAcquisitionState.ERROR)
            self.assertTrue(failed.cleanup_pending)
            self.assertTrue(controller.is_running)
            self.assertFalse(controller.is_quiescent)
            with self.assertRaisesRegex(RuntimeError, "停止和清理"):
                controller.start()
            with self.assertRaisesRegex(RuntimeError, "停止和清理"):
                controller.clear()
            self.assertEqual(reader.stop_calls, 1)
            # Even when the cancelled read completes, failed NI cleanup is not
            # retried by the read worker behind the user's back.
            reader.release_read.set()
            self.assertTrue(wait_until(lambda: not controller._read_active))
            self.assertEqual(reader.stop_calls, 1)
            controller.stop()
            self.assertEqual(reader.stop_calls, 2)
            self.assertTrue(controller.is_quiescent)
            self.assertFalse(controller.snapshot().cleanup_pending)
            self.assertIn("NI close failed", controller.snapshot().error)
        finally:
            reader.release_read.set()
            controller.stop()

    def test_stop_timeout_keeps_cleanup_and_queries_do_not_wait_for_close(self):
        close_entered = threading.Event()
        release_close = threading.Event()

        class SlowCloseReader(BlockingReader):
            def stop(self):
                self.stop_calls += 1
                close_entered.set()
                if not release_close.wait(3):
                    raise RuntimeError("test close was not released")
                self.release_read.set()

        reader = SlowCloseReader()
        controller = AnalogAcquisitionController(reader)
        controller.start()
        self.assertTrue(reader.read_entered.wait(1))
        try:
            with self.assertRaisesRegex(RuntimeError, "停止尚未完成"):
                controller.stop(timeout_seconds=0.02)
            self.assertTrue(close_entered.is_set())
            query_done = threading.Event()

            def query():
                controller.snapshot()
                controller.is_running
                controller.is_quiescent
                query_done.set()

            query_thread = threading.Thread(target=query)
            query_thread.start()
            self.assertTrue(query_done.wait(0.3), "status query waited on NI close")
            query_thread.join(1)
            self.assertTrue(controller.snapshot().cleanup_pending)
            self.assertTrue(controller.is_running)
            with self.assertRaises(RuntimeError):
                controller.start()
            release_close.set()
            self.assertTrue(wait_until(lambda: controller.is_quiescent))
            controller.stop()
            self.assertEqual(reader.stop_calls, 1)
        finally:
            release_close.set()
            reader.release_read.set()
            controller.stop()

    def test_stop_during_start_waits_and_does_not_start_reading(self):
        start_entered = threading.Event()
        release_start = threading.Event()

        class SlowStartReader(BlockingReader):
            def start(self):
                start_entered.set()
                if not release_start.wait(3):
                    raise RuntimeError("test start was not released")

        reader = SlowStartReader()
        controller = AnalogAcquisitionController(reader)
        startup = threading.Thread(target=controller.start)
        startup.start()
        try:
            self.assertTrue(start_entered.wait(1))
            with self.assertRaisesRegex(RuntimeError, "停止尚未完成"):
                controller.stop(timeout_seconds=0.02)
            self.assertEqual(reader.stop_calls, 0)
            self.assertTrue(controller.is_running)
            self.assertTrue(controller.snapshot().cleanup_pending)
            release_start.set()
            startup.join(1)
            self.assertFalse(startup.is_alive())
            self.assertTrue(wait_until(lambda: controller.is_quiescent))
            self.assertEqual(reader.read_calls, 0)
            self.assertEqual(reader.stop_calls, 1)
        finally:
            release_start.set()
            startup.join(2)
            reader.release_read.set()
            controller.stop()

    def test_partial_start_and_cleanup_errors_are_both_preserved(self):
        class BrokenStartReader(BlockingReader):
            def start(self):
                raise OSError("start failed")

        reader = BrokenStartReader(stop_failures=1)
        controller = AnalogAcquisitionController(reader)
        with self.assertRaisesRegex(OSError, "start failed"):
            controller.start()
        self.assertTrue(wait_until(lambda: not controller._cleanup_in_progress))
        self.assertTrue(controller.is_running)
        snapshot = controller.snapshot()
        self.assertIn("start failed", snapshot.error)
        self.assertIn("NI close failed", snapshot.error)
        self.assertTrue(snapshot.cleanup_pending)
        self.assertEqual(reader.stop_calls, 1)
        controller.stop()
        self.assertTrue(controller.is_quiescent)
        self.assertEqual(reader.stop_calls, 2)

    def test_slow_file_flush_does_not_block_status_or_close_file_early(self):
        flush_entered = threading.Event()
        release_flush = threading.Event()

        class SlowFlushStream(io.StringIO):
            flush_calls = 0

            def flush(self):
                self.flush_calls += 1
                if self.flush_calls == 1:
                    flush_entered.set()
                    if not release_flush.wait(3):
                        raise RuntimeError("test flush was not released")
                return super().flush()

        stream = SlowFlushStream()
        reader = BlockingReader(first_block=BLOCK)
        controller = AnalogAcquisitionController(reader, stream_path="unused.csv")
        try:
            with patch.object(controller.stream_writer, "open", lambda: install_stream(controller.stream_writer, stream)):
                controller.start()
            self.assertTrue(flush_entered.wait(1))
            query_done = threading.Event()
            query_result = []

            def query():
                query_result.append(controller.snapshot())
                query_result.append(controller.is_running)
                query_done.set()

            query_thread = threading.Thread(target=query)
            query_thread.start()
            self.assertTrue(query_done.wait(0.3), "status query waited on file flush")
            query_thread.join(1)
            self.assertEqual(query_result[0].saved_samples, 0)
            self.assertEqual(query_result[0].total_samples, 2)
            with self.assertRaisesRegex(RuntimeError, "停止尚未完成"):
                controller.stop(timeout_seconds=0.02)
            self.assertFalse(stream.closed)
            self.assertTrue(controller.snapshot().cleanup_pending)
            release_flush.set()
            self.assertTrue(wait_until(lambda: controller.is_quiescent))
            self.assertTrue(stream.closed)
            self.assertEqual(controller.snapshot().saved_samples, 2)
            self.assertEqual(controller.snapshot().total_samples, 2)
        finally:
            release_flush.set()
            reader.release_read.set()
            controller.stop()

    def test_cancelled_native_read_is_not_an_acquisition_error(self):
        reader = BlockingReader()
        controller = AnalogAcquisitionController(reader)
        controller.start()
        self.assertTrue(reader.read_entered.wait(1))
        controller.stop()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.state, AnalogAcquisitionState.STOPPED)
        self.assertIsNone(snapshot.error)
        self.assertFalse(snapshot.cleanup_pending)
        self.assertEqual(snapshot.total_samples, 0)

    def test_read_failure_survives_failed_cleanup_and_retry(self):
        class BrokenReader(BlockingReader):
            def read(self):
                raise OSError("ADC failed")

        reader = BrokenReader(stop_failures=1)
        controller = AnalogAcquisitionController(reader)
        controller.start()
        self.assertTrue(wait_until(lambda: not controller._cleanup_in_progress and reader.stop_calls == 1))
        snapshot = controller.snapshot()
        self.assertIn("ADC failed", snapshot.error)
        self.assertIn("NI close failed", snapshot.error)
        self.assertTrue(snapshot.cleanup_pending)
        controller.stop()
        self.assertEqual(reader.stop_calls, 2)
        self.assertFalse(controller.is_running)
        self.assertIn("ADC failed", controller.snapshot().error)

    def test_repeated_automatic_cleanup_request_does_not_retry_failed_close(self):
        class BrokenReader(BlockingReader):
            def read(self):
                raise OSError("ADC failed")

        reader = BrokenReader(stop_failures=1)
        controller = AnalogAcquisitionController(reader)
        controller.start()
        self.assertTrue(wait_until(
            lambda: reader.stop_calls == 1 and not controller._cleanup_in_progress
        ))
        # Startup and read-loop finalizers may both request the same cleanup.
        controller._ensure_cleanup()
        self.assertEqual(reader.stop_calls, 1)
        self.assertTrue(controller.is_running)
        controller.stop()
        self.assertEqual(reader.stop_calls, 2)
        self.assertTrue(controller.is_quiescent)

    def test_partial_file_open_failure_is_closed_before_becoming_quiescent(self):
        stream = io.StringIO()
        reader = BlockingReader()
        controller = AnalogAcquisitionController(reader, stream_path="unused.csv")
        with patch("pathlib.Path.open", return_value=stream), patch(
            "rigol_control.analog.csv.writer", side_effect=RuntimeError("CSV init failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "CSV init failed"):
                controller.start()
        self.assertTrue(wait_until(lambda: controller.is_quiescent))
        self.assertTrue(stream.closed)
        self.assertEqual(reader.stop_calls, 0)
        self.assertIn("CSV init failed", controller.snapshot().error)

    def test_file_close_failure_remains_pending_until_explicit_stop(self):
        class CloseFailureStream(io.StringIO):
            close_attempts = 0

            def close(self):
                self.close_attempts += 1
                if self.close_attempts == 1:
                    raise OSError("file close failed")
                return super().close()

        stream = CloseFailureStream()
        reader = BlockingReader(first_block=BLOCK)
        controller = AnalogAcquisitionController(reader, stream_path="unused.csv")
        with patch.object(controller.stream_writer, "open", lambda: install_stream(controller.stream_writer, stream)):
            controller.start()
        self.assertTrue(wait_until(lambda: controller.snapshot().saved_samples == 2))
        with self.assertRaisesRegex(RuntimeError, "file close failed"):
            controller.stop()
        self.assertTrue(controller.snapshot().cleanup_pending)
        self.assertTrue(controller.is_running)
        self.assertFalse(stream.closed)
        self.assertEqual(stream.close_attempts, 1)
        controller.stop()
        self.assertTrue(stream.closed)
        self.assertEqual(stream.close_attempts, 2)
        self.assertFalse(controller.is_running)

    def test_failing_observer_cannot_strand_start_or_cleanup(self):
        reader = BlockingReader()
        controller = AnalogAcquisitionController(reader)
        healthy_updates = []

        def observer(_update):
            raise RuntimeError("detached window")

        controller.add_callback(lambda update: healthy_updates.append(update.snapshot))
        controller.add_callback(observer)
        with self.assertLogs("rigol_control.analog", level="ERROR"):
            controller.start()
            self.assertTrue(wait_until(lambda: controller.is_quiescent))
            controller.stop()
        self.assertTrue(controller.is_quiescent)
        snapshot = controller.snapshot()
        self.assertEqual(snapshot.state, AnalogAcquisitionState.ERROR)
        self.assertIn("detached window", snapshot.error)
        self.assertEqual(healthy_updates[-1].state, AnalogAcquisitionState.ERROR)
        self.assertEqual(reader.read_calls, 0)

    def test_snapshot_revisions_increase_across_stop(self):
        reader = BlockingReader()
        controller = AnalogAcquisitionController(reader)
        updates = []
        controller.add_callback(lambda update: updates.append(update.snapshot))
        controller.start()
        self.assertTrue(reader.read_entered.wait(1))
        initial = controller.snapshot()
        controller.stop()
        final = controller.snapshot()
        self.assertGreater(final.revision, initial.revision)
        self.assertGreater(final.revision, max(update.revision for update in updates))


if __name__ == "__main__":
    unittest.main()
