import threading
import unittest

from rigol_control.drivers.scpi import ScpiSession


class ScpiStatusTests(unittest.TestCase):
    def test_status_does_not_wait_for_instrument_query(self):
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        class Backend:
            def query(self, command):
                if command != '*IDN?':
                    entered.set()
                    release.wait(3)
                return 'SIGLENT,SSG3021X,TEST,1.0'
            def close(self):
                pass
        session = ScpiSession('TEST', backend_factory=lambda *_: Backend())
        identity = session.connect()
        worker = threading.Thread(target=lambda: session.query(':SWEep:CURRent:FREQuency?'))
        result = []
        def observe():
            result.append((session.is_connected, session.identity))
            observed.set()
        reader = threading.Thread(target=observe)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            reader.start()
            self.assertTrue(observed.wait(.3), 'Connection status blocked on instrument I/O')
            self.assertEqual(result, [(True, identity)])
        finally:
            release.set()
            worker.join(3)
            if reader.ident is not None:
                reader.join(3)
            session.disconnect()

    def test_status_is_disconnected_during_open_and_close(self):
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        class Backend:
            def query(self, command):
                entered.set()
                release.wait(3)
                return 'SIGLENT,SSG3021X,TEST,1.0'
            def close(self):
                entered.set()
                release.wait(3)
        session = ScpiSession('TEST', backend_factory=lambda *_: Backend())
        for operation in (session.connect, session.disconnect):
            entered.clear()
            release.clear()
            observed.clear()
            result = []
            def observe():
                result.append((session.is_connected, session.identity))
                observed.set()
            worker, reader = threading.Thread(target=operation), threading.Thread(target=observe)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                reader.start()
                self.assertTrue(observed.wait(.3), 'Status waited for open/close I/O')
                self.assertEqual(result, [(False, '')])
            finally:
                release.set()
                worker.join(3)
                if reader.ident is not None:
                    reader.join(3)
