import threading
import unittest

from rigol_control.connections import DeviceConnectionManager
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.virtual_lab import VirtualGenerator, VirtualScpiSession


class BoundedManagerLock:
    """Turn the old lock inversion into a test failure, never a hung suite."""
    def __init__(self):
        self.lock = threading.RLock()

    def __enter__(self):
        if not self.lock.acquire(timeout=.5):
            raise RuntimeError("manager/session lock inversion")
        return self

    def __exit__(self, *_args):
        self.lock.release()


class VirtualSessionStatusTests(unittest.TestCase):
    def test_status_card_and_manual_transaction_cannot_deadlock_each_other(self):
        status_entered, io_held = threading.Event(), threading.Event()

        class ObservedSession(VirtualScpiSession):
            @property
            def is_connected(self):
                if threading.current_thread().name == "manager-status":
                    status_entered.set()
                return super().is_connected

        record = DeviceRecord.create(
            kind=DeviceKind.DG4000, name="Virtual DG", resource="USB0::FAKE::INSTR"
        )
        generator = VirtualGenerator(record.resource)
        generator.scpi_delay_ms = 0
        session = ObservedSession(generator)
        manager = DeviceConnectionManager(session_factory=lambda _resource: session)
        manager.connect(record)
        manager._lock = BoundedManagerLock()
        errors, observations = [], []

        def manual():
            try:
                with session.transaction():
                    io_held.set()
                    if not status_entered.wait(1):
                        raise RuntimeError("status reader did not enter")
                    manager.assert_manual_access(record)
            except Exception as exc:
                errors.append(exc)

        def status():
            try:
                if not io_held.wait(1):
                    raise RuntimeError("manual worker did not enter")
                observations.append(manager.info(record).connected)
            except Exception as exc:
                errors.append(exc)

        writer = threading.Thread(target=manual)
        reader = threading.Thread(target=status, name="manager-status")
        writer.start()
        reader.start()
        writer.join(2)
        reader.join(2)
        try:
            self.assertFalse(writer.is_alive())
            self.assertFalse(reader.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(observations, [True])
        finally:
            manager.close_all()

    def test_identity_and_connected_state_remain_readable_during_slow_query(self):
        generator = VirtualGenerator("SIM::DG4000::1")
        generator.scpi_delay_ms = 0
        session = VirtualScpiSession(generator)
        identity = session.connect()
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        original_query = generator.query

        def slow_query(command):
            if command != "*IDN?":
                entered.set()
                release.wait(2)
            return original_query(command)

        generator.query = slow_query
        result = []
        worker = threading.Thread(target=lambda: session.query(":SOUR2:FREQ?"))

        def read_status():
            result.append((session.is_connected, session.identity))
            observed.set()

        reader = threading.Thread(target=read_status)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(observed.wait(.5), "virtual status waited for the I/O lock")
            self.assertEqual(result, [(True, identity)])
        finally:
            release.set()
            worker.join(2)
            if reader.ident is not None:
                reader.join(2)
            session.disconnect()
        self.assertFalse(session.is_connected)
        self.assertEqual(session.identity, "")


if __name__ == "__main__":
    unittest.main()
