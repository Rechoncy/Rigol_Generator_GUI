import time
import unittest

from rigol_control.drivers.newport_smc100 import (
    MotionError, MotionStateError, MotionStatus, Smc100Device, VirtualSmc100Device,
)


class StateSerial:
    def __init__(self, state=0x33):
        self.state = state
        self.error = 0
        self.is_open = True
        self.writes = []
        self.enable_changes_state = True
        self.fail_enable = False
        self.command_error = "@"

    def reset_input_buffer(self):
        pass

    def write(self, payload):
        self.writes.append(payload)
        if payload == b"7MM1\r\n":
            if self.fail_enable:
                raise OSError("USB_WRITE_UNCERTAIN")
            if self.enable_changes_state:
                self.state = 0x34
        return len(payload)

    def read_until(self, _terminator, _size):
        command = self.writes[-1].decode("ascii").strip()[1:]
        replies = {"VE": "SMC100CC V3.0", "ID?": "PR50CC", "TP": "12.5",
                   "VA?": "10", "SL?": "-360", "SR?": "360", "TE": self.command_error,
                   "TS": f"{self.error:04X}{self.state:02X}"}
        return f"7{command[:2]}{replies[command]}\r\n".encode("ascii")

    def close(self):
        self.is_open = False


class NewportStateTests(unittest.TestCase):
    def setUp(self):
        self.serial = StateSerial()
        self.device = Smc100Device("COM8::7", serial_factory=lambda **_: self.serial)
        self.device.connect()
        self.serial.writes.clear()
        self.addCleanup(self.device.disconnect)

    def test_ready_home_refusal_identifies_the_current_state_without_writes(self):
        with self.assertRaisesRegex(MotionStateError, "0x33") as caught:
            self.device.home()
        self.assertTrue(caught.exception.status.ready)
        self.assertIn("0°", str(caught.exception))
        self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_esp_stage_configuration_error_never_starts_homing(self):
        self.serial.state = 0x10
        with self.assertRaisesRegex(MotionError, "ESP"):
            self.device.home()
        self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_not_referenced_homing_sends_only_one_addressed_or(self):
        for code in (0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x11):
            with self.subTest(state=code):
                self.serial.state = code
                self.serial.writes.clear()
                self.device.home()
                self.assertEqual(self.serial.writes, [b"7TS\r\n", b"7OR\r\n", b"7TE\r\n"])

    def test_disable_recovery_is_addressed_once_and_confirmed_ready(self):
        for code in (0x3C, 0x3D, 0x3E):
            with self.subTest(state=code):
                self.serial.state = code
                self.serial.writes.clear()
                result = self.device.enable_motor()
                self.assertTrue(result.ready)
                self.assertEqual(self.serial.writes,
                                 [b"7TS\r\n", b"7MM1\r\n", b"7TE\r\n", b"7TS\r\n"])

    def test_ready_recovery_is_a_noop_without_motor_or_motion_writes(self):
        for code in (0x32, 0x33, 0x34, 0x35):
            with self.subTest(state=code):
                self.serial.state = code
                self.serial.writes.clear()
                self.assertEqual(self.device.enable_motor().state, code)
                self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_recovery_refuses_unreferenced_configuration_motion_and_unknown_states(self):
        for code in (0x0A, 0x10, 0x14, 0x1E, 0x1F, 0x28, 0x46, 0x47, 0x99):
            with self.subTest(state=code):
                self.serial.state = code
                self.serial.writes.clear()
                with self.assertRaises(MotionStateError) as caught:
                    self.device.enable_motor()
                self.assertEqual(caught.exception.status.state, code)
                self.assertIn(f"0x{code:02X}", str(caught.exception))
                self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_move_and_velocity_refusal_retain_state_without_settings(self):
        self.serial.state = 0x3C
        for operation in (lambda: self.device.move_absolute(1), lambda: self.device.set_velocity(1)):
            with self.subTest(operation=operation):
                self.serial.writes.clear()
                with self.assertRaises(MotionStateError) as caught:
                    operation()
                self.assertTrue(caught.exception.status.disabled)
                self.assertIn("恢复使能", str(caught.exception))
                self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_hardware_error_is_preserved_before_any_motion_or_recovery(self):
        self.serial.error = 0x0020
        for operation in (self.device.home, self.device.enable_motor, lambda: self.device.move_absolute(1)):
            with self.subTest(operation=operation):
                self.serial.writes.clear()
                with self.assertRaisesRegex(MotionError, "0x0020") as caught:
                    operation()
                self.assertNotIsInstance(caught.exception, MotionStateError)
                self.assertEqual(self.serial.writes, [b"7TS\r\n"])

    def test_uncertain_mm1_write_is_never_replayed_or_followed_by_reconnection(self):
        self.serial.state = 0x3C
        self.serial.fail_enable = True
        with self.assertRaisesRegex(MotionError, "USB_WRITE_UNCERTAIN"):
            self.device.enable_motor()
        self.assertEqual(self.serial.writes, [b"7TS\r\n", b"7MM1\r\n"])
        self.assertFalse(self.device.is_connected)

    def test_post_command_not_ready_is_not_misclassified_as_a_local_refusal(self):
        self.serial.state = 0x3C
        self.serial.enable_changes_state = False
        with self.assertRaisesRegex(MotionError, "未确认 READY") as caught:
            self.device.enable_motor()
        self.assertNotIsInstance(caught.exception, MotionStateError)
        self.assertEqual(self.serial.writes, [b"7TS\r\n", b"7MM1\r\n", b"7TE\r\n", b"7TS\r\n"])

    def test_rejected_mm1_has_no_reset_home_position_or_automatic_retry(self):
        self.serial.state = 0x3D
        self.serial.command_error = "D"
        with self.assertRaisesRegex(MotionError, "MM1.*D"):
            self.device.enable_motor()
        self.assertEqual(self.serial.writes, [b"7TS\r\n", b"7MM1\r\n", b"7TE\r\n"])

    def test_state_descriptions_distinguish_reference_ready_disable_and_fault(self):
        for code, phrase in ((0x0A, "NOT REFERENCED"), (0x33, "READY"), (0x3C, "DISABLE"),
                             (0x10, "ESP"), (0x14, "CONFIGURATION"), (0x1E, "HOMING"),
                             (0x28, "MOVING"), (0x46, "JOGGING"), (0x99, "未识别")):
            with self.subTest(state=code):
                state = MotionStatus(0, code)
                self.assertIn(phrase, state.description)
                self.assertIn(f"0x{code:02X}", state.description)
                self.assertTrue(state.guidance)
        error = MotionStatus(0x0020, 0x33)
        self.assertFalse(error.ready)
        self.assertIn("0x0020", error.description)
        self.assertIn("硬件错误", error.guidance)
        self.assertTrue(MotionStatus(0, 0x10).not_referenced)
        self.assertFalse(MotionStatus(0, 0x10).can_home)


class VirtualNewportStateTests(unittest.TestCase):
    def setUp(self):
        self.device = VirtualSmc100Device()
        self.device.connect()

    def wait_ready(self):
        deadline = time.monotonic() + 2
        while not self.device.status().ready and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertTrue(self.device.status().ready)

    def test_disable_recovery_preserves_nonzero_position_without_homing_or_moving(self):
        self.device.move_absolute(1)
        self.wait_ready()
        history = list(self.device.move_history)
        self.device.set_virtual_state(0x3C)
        result = self.device.enable_motor()
        self.assertEqual(result.state, 0x34)
        self.assertEqual(self.device.position(), 1)
        self.assertEqual(self.device.move_history, history)
        self.assertEqual(self.device.home_count, 0)
        self.assertEqual(self.device.enable_count, 1)
        self.device.enable_motor()
        self.assertEqual(self.device.enable_count, 1)

    def test_homing_requires_reference_search_state_and_ends_in_ready_from_homing(self):
        with self.assertRaises(MotionStateError):
            self.device.home()
        self.device.set_virtual_state(0x0A)
        self.device.home()
        self.assertEqual(self.device.status().state, 0x1E)
        self.wait_ready()
        self.assertEqual(self.device.status().state, 0x32)
        self.assertEqual(self.device.home_count, 1)
        self.assertEqual(self.device.move_history, [])

    def test_virtual_fault_and_esp_state_cannot_be_hidden_by_recovery(self):
        self.device.set_virtual_state(0x3C, error=0x0020)
        with self.assertRaisesRegex(MotionError, "0x0020"):
            self.device.enable_motor()
        self.assertEqual(self.device.enable_count, 0)
        self.device.set_virtual_state(0x10)
        with self.assertRaisesRegex(MotionStateError, "ESP"):
            self.device.home()
        self.assertEqual(self.device.home_count, 0)

    def test_stop_rejects_unreferenced_configuration_and_jogging_without_changing_position(self):
        self.device.move_absolute(1)
        self.wait_ready()
        for code in (*range(0x0A, 0x12), 0x14, 0x46, 0x47):
            with self.subTest(state=code):
                self.device.set_virtual_state(code)
                with self.assertRaisesRegex(MotionError, "拒绝 ST") as caught:
                    self.device.stop()
                self.assertNotIsInstance(caught.exception, MotionStateError)
                self.assertEqual(self.device.position(), 1)
                self.assertEqual(self.device.state, code)
                self.assertEqual(self.device.stop_count, 0)
        self.assertIn("控制器面板", MotionStatus(0, 0x46).guidance)

    def test_stop_rejection_does_not_hide_hardware_fault(self):
        self.device.set_virtual_state(0x0A, error=0x0020)
        with self.assertRaisesRegex(MotionError, "0x0020"):
            self.device.stop()
        self.assertEqual(self.device.error, 0x0020)
        self.assertEqual(self.device.state, 0x0A)
        self.assertEqual(self.device.stop_count, 0)
        # A best-effort stop in an allowed moving state may stop the simulated
        # mechanics, but must leave the consumed-fault evidence available.
        self.device.set_virtual_state(0x28, error=0x0020)
        self.device.stop()
        self.assertEqual(self.device.stop_count, 1)
        with self.assertRaisesRegex(MotionError, "0x0020"):
            self.device.status()

    def test_stopping_reference_search_requires_homing_again(self):
        self.device.set_virtual_state(0x0A)
        self.device.home()
        self.device.stop()
        self.assertEqual(self.device.status().state, 0x0B)
        self.assertFalse(self.device.status().ready)
        self.assertEqual(self.device.stop_count, 1)


if __name__ == "__main__":
    unittest.main()
