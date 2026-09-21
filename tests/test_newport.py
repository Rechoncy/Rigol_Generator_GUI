import csv
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from rigol_control.analog import AnalogAcquisitionController
from rigol_control.connections import DeviceBusyError, DeviceConnectionManager
from rigol_control.drivers.newport_smc100 import (
    MotionError, MotionStatus, Smc100Device, VIRTUAL_SMC100_RESOURCE, parse_resource,
)
from rigol_control.drivers.ni_analog import AnalogInputConfig
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.rotation_scan import RotationScanConfig, RotationScanController
from rigol_control.services import DeviceDiscoveryService, DeviceProbeService, ProbeState
from rigol_control.virtual_lab import VIRTUAL_NI_RESOURCE


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class FakeSerial:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.is_open = True
        self.writes = []
        self.replies = {"VE": "SMC100CC V3.0", "ID?": "PR50CC", "TS": "000033", "TP": "12.5",
                        "SL?": "-360", "SR?": "360", "VA?": "20", "TE": "@"}
        self.raw_reply = None

    def reset_input_buffer(self):
        pass

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def read_until(self, terminator, size):
        if self.raw_reply is not None:
            return self.raw_reply
        command = self.writes[-1].decode().strip()[1:]
        return f"1{command[:2]}{self.replies[command]}\r\n".encode()

    def close(self):
        self.is_open = False


class SmcProtocolTests(unittest.TestCase):
    def test_firmware_revision_formats_and_stage_identification(self):
        for revision in ("SMC100CC V3.0", " SMC - Controller-driver version 1.00r",
                         "SMC100 V3.0", "SMC_CC - Controller-driver version 3.1.2",
                         "SMC_CC - Controller-driver version 3. 1. 2",
                         " smc_cc - Controller-driver version 3.1.2 "):
            with self.subTest(revision=revision):
                serial = FakeSerial()
                serial.replies.update({"VE": revision, "ID?": " PR50CC "})
                device = Smc100Device("COM3::1", serial_factory=lambda **kw: serial)
                try:
                    self.assertIn("PR50CC", device.connect())
                    self.assertEqual(device.stage_model, "PR50CC")
                    self.assertEqual(device.firmware_revision, revision.strip())
                    self.assertEqual(serial.writes, [b"1VE\r\n", b"1ID?\r\n"])
                finally:
                    device.disconnect()

    def test_wrong_controller_or_stage_still_rejected_without_motion(self):
        for revision, stage, error in (
            ("SMC100PP V3.0", "PR50CC", "VE 回包"),
            ("SMC_PP - Controller-driver version 3.1.2", "PR50CC", "VE 回包"),
            ("SMC_CCX - Controller-driver version 3.1.2", "PR50CC", "VE 回包"),
            ("SMC100CCX V3.0", "PR50CC", "VE 回包"),
            ("XSMC_CC - Controller-driver version 3.1.2", "PR50CC", "VE 回包"),
            ("CONEX V3.0", "PR50CC", "VE 回包"),
            ("SMC - Controller-driver version 1.00r", "PR50PP", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "PR50PP", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "PR50PP_PN:B2210116", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "PR50CCX_PN:B2210116", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "OTHER_PR50CC", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "_PN:PR50CC", "ID\\?"),
            ("SMC_CC - Controller-driver version 3.1.2", "PR50CC/PR50PP", "ID\\?"),
        ):
            with self.subTest(revision=revision, stage=stage):
                serial = FakeSerial()
                serial.replies.update({"VE": revision, "ID?": stage})
                device = Smc100Device("COM3::1", serial_factory=lambda **kw: serial)
                with self.assertRaisesRegex(MotionError, error):
                    device.connect()
                self.assertFalse(device.is_connected)
                self.assertEqual(device.identity, "")
                self.assertTrue(all(w in (b"1VE\r\n", b"1ID?\r\n") for w in serial.writes))

    def test_stage_identifier_metadata_is_preserved_separately_from_model(self):
        for identifier in (
            "PR50CC_PN:B2210116_UD:01/10/2021",
            "PR50CC_PN:B2210116",
            "PR50CC_UD:01/10/2021_PN:B2210116",
            " pr50cc_PN:B2210116_UD:01/10/2021 ",
            "PR50CC",
        ):
            with self.subTest(identifier=identifier):
                serial = FakeSerial()
                serial.replies.update({"VE": "SMC_CC - Controller-driver version 3. 1. 2",
                                       "ID?": identifier})
                device = Smc100Device("COM3::1", serial_factory=lambda **kw: serial)
                try:
                    identity = device.connect()
                    self.assertEqual(device.stage_model, "PR50CC")
                    self.assertEqual(device.stage_identifier, identifier.strip())
                    self.assertEqual(identity, f"SMC_CC - Controller-driver version 3. 1. 2 / {identifier.strip()}")
                    self.assertEqual(serial.writes, [b"1VE\r\n", b"1ID?\r\n"])
                finally:
                    device.disconnect()
                self.assertEqual(device.stage_identifier, "")
                self.assertEqual(device.stage_model, "")

    def test_screenshot_smc_cc_reply_connects_and_reads_stage_without_motion(self):
        class TranscriptSerial(FakeSerial):
            responses = iter((
                b"1VESMC_CC - Controller-driver version 3.1.2\r\n",
                b"1IDPR50CC_PN:B2210116_UD:01/10/2021\r\n",
                b"1TS000033\r\n",
                b"1TP-5.2\r\n",
            ))

            def read_until(self, terminator, size):
                return next(self.responses)

        serial = TranscriptSerial()
        device = Smc100Device("COM3::1", serial_factory=lambda **kw: serial)
        try:
            self.assertEqual(device.connect(), "SMC_CC - Controller-driver version 3.1.2 / PR50CC_PN:B2210116_UD:01/10/2021")
            self.assertTrue(device.is_connected)
            self.assertTrue(device.status().ready)
            self.assertAlmostEqual(device.position(), -5.2)
            self.assertEqual(serial.writes, [b"1VE\r\n", b"1ID?\r\n", b"1TS\r\n", b"1TP\r\n"])
        finally:
            device.disconnect()

    def setUp(self):
        self.serial = FakeSerial()
        def factory(**kwargs):
            self.serial.options = kwargs
            return self.serial
        self.device = Smc100Device("COM3::1", serial_factory=factory)
        self.device.connect()
        self.addCleanup(self.device.disconnect)

    def test_protocol_parameters_and_motion(self):
        self.assertEqual(self.serial.options["baudrate"], 57600)
        self.assertTrue(self.serial.options["xonxoff"])
        self.assertEqual(self.device.position(), 12.5)
        self.device.move_absolute(-0.2)
        self.assertIn(b"1PA-0.200000000\r\n", self.serial.writes)
        self.assertEqual(self.serial.writes[-1], b"1TE\r\n")
        self.device.stop()
        self.assertIn(b"1ST\r\n", self.serial.writes)

    def test_all_ready_states_and_error_consumption(self):
        for code in (0x32, 0x33, 0x34, 0x35):
            self.assertTrue(MotionStatus(0, code).ready)
        self.assertFalse(MotionStatus(0, 0x28).ready)
        self.serial.replies["TS"] = "002033"
        with self.assertRaisesRegex(MotionError, "0x0020"):
            self.device.move_absolute(1)
        self.assertFalse(any(b"PA" in w for w in self.serial.writes))

    def test_limits_nonfinite_and_not_referenced(self):
        for value in (math.nan, math.inf, 361):
            with self.assertRaises((ValueError, MotionError)):
                self.device.move_absolute(value)
        self.serial.replies["TS"] = "00000A"
        with self.assertRaises(MotionError):
            self.device.move_absolute(1)
        self.device.home()
        self.assertIn(b"1OR\r\n", self.serial.writes)

    def test_incomplete_reply_disconnects_without_retry(self):
        self.serial.raw_reply = b"1TP12"
        before = len(self.serial.writes)
        with self.assertRaises(MotionError):
            self.device.position()
        self.assertFalse(self.device.is_connected)
        self.assertEqual(len(self.serial.writes), before + 1)

    def test_wrong_address_and_command_error(self):
        self.serial.replies["TE"] = "G"
        with self.assertRaisesRegex(MotionError, "命令错误 G"):
            self.device.move_absolute(1)
        self.assertEqual(sum(b"PA" in w for w in self.serial.writes), 1)
        self.serial.raw_reply = b"2TP1\r\n"
        with self.assertRaises(MotionError):
            self.device.position()
        self.assertFalse(self.device.is_connected)

    def test_resource_and_velocity_validation(self):
        self.assertEqual(parse_resource("com9::31"), ("COM9", 31))
        for resource in ("COM3", "COM3::0", "COM3::32", "COM3::1\r\n1OR"):
            with self.assertRaises(ValueError):
                parse_resource(resource)
        for value in (0, -1, 21, math.nan):
            with self.assertRaises(ValueError):
                self.device.set_velocity(value)


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.manager = DeviceConnectionManager()
        self.addCleanup(self.manager.close_all)
        self.stage = DeviceRecord.create(kind=DeviceKind.NEWPORT_SMC100, name="PR50CC",
                                         resource=VIRTUAL_SMC100_RESOURCE)
        self.ni = DeviceRecord.create(kind=DeviceKind.NI_DAQ, name="NI", resource=VIRTUAL_NI_RESOURCE)
        self.manager.connect(self.stage)
        self.manager.connect(self.ni)
        self.device = self.manager.newport_device_for(self.stage)

    def make_controller(self, config=None, analog=None, directory=None):
        controller = RotationScanController(self.device, self.manager.counter_for(self.ni),
            config or RotationScanConfig(step_deg=0.2, point_count=3, settle_seconds=0.01),
            manager=self.manager, device_ids=[self.stage.id, self.ni.id], analog=analog,
            run_directory=directory or Path(self.tmp.name) / "run")
        self.addCleanup(controller.stop)
        return controller

    def arm(self, controller):
        controller.start()
        self.assertTrue(wait_until(lambda: controller.snapshot().phase == "holding"), controller.snapshot())

    def edge(self, count=1):
        self.manager.virtual_lab.emit_trigger(self.ni.resource, count)

    def test_start_keeps_p0_first_edge_p1_and_repeated_wrap(self):
        self.device.move_absolute(12)
        self.assertTrue(wait_until(lambda: self.device.status().ready))
        self.device.move_history.clear()
        c = self.make_controller()
        self.arm(c)
        self.assertEqual(self.device.move_history, [])
        self.assertEqual(c.snapshot().target_deg, 12)
        for k in range(1, 8):
            self.edge()
            self.assertTrue(wait_until(lambda: c.snapshot().completed == k), c.snapshot())
            self.assertAlmostEqual(self.device.position(), 12 + (k % 3) * 0.2)
        c.stop()
        self.assertEqual(c.snapshot().received, 7)
        self.assertEqual(c.snapshot().completed, 7)
        self.assertAlmostEqual(self.device.move_history[2], 12)
        self.assertAlmostEqual(self.device.move_history[5], 12)
        with (Path(self.tmp.name) / "run/rotation.csv").open(encoding="utf-8-sig") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(sum(r["event"] == "settled" for r in rows), 7)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))
        config = json.loads((Path(self.tmp.name) / "run/config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["points_deg"][0], 12)
        self.assertIsNone(config["config"]["initial_position_deg"])
        self.assertEqual(config["initial_position_mode"], "current")

    def test_selected_p0_rejects_unpositioned_or_out_of_limits_without_arming(self):
        for initial in (3.0, -361.0, 359.9):
            with self.subTest(initial=initial):
                analog = Mock()
                c = self.make_controller(
                    RotationScanConfig(point_count=3, initial_position_deg=initial), analog=analog,
                    directory=Path(self.tmp.name) / str(initial))
                c.counter.start = Mock(wraps=c.counter.start)
                c.counter.stop = Mock(wraps=c.counter.stop)
                stop_count = self.device.stop_count
                c.start()
                self.assertTrue(wait_until(lambda: not c.is_running))
                self.assertIn("先移动到初始位置" if initial == 3.0 else "限位", c.snapshot().error)
                self.assertEqual(self.device.move_history, [])
                self.assertEqual(self.device.stop_count, stop_count)
                c.counter.start.assert_not_called()
                c.counter.stop.assert_not_called()
                analog.start.assert_not_called()
                analog.stop.assert_not_called()
                self.assertIsNone(self.manager.lease_owner(self.stage.id))
                self.assertIsNone(self.manager.lease_owner(self.ni.id))

    def test_selected_p0_preserves_grid_with_actual_offset_and_wraps_to_selected_start(self):
        self.device.move_absolute(12.005)
        self.assertTrue(wait_until(lambda: self.device.status().ready))
        self.device.move_history.clear()
        c = self.make_controller(RotationScanConfig(
            point_count=3, settle_seconds=0.02, initial_position_deg=12.0))
        self.arm(c)
        self.assertEqual(self.device.move_history, [])
        self.assertEqual(c.points, (12.0, 12.2, 12.4))
        self.assertEqual(c.snapshot().target_deg, 12.0)
        self.assertAlmostEqual(c.snapshot().actual_deg, 12.005)
        for k, angle in enumerate((12.2, 12.4, 12.0), 1):
            self.edge()
            self.assertTrue(wait_until(lambda: c.snapshot().completed == k), c.snapshot())
            self.assertAlmostEqual(self.device.position(), angle)
        c.stop()
        self.assertEqual(self.device.move_history, [12.2, 12.4, 12.0])
        config = json.loads((Path(self.tmp.name) / "run/config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["config"]["initial_position_deg"], 12.0)
        self.assertEqual(config["initial_position_mode"], "specified")
        self.assertEqual(config["points_deg"], [12.0, 12.2, 12.4])
        self.assertAlmostEqual(config["measured_initial_position_deg"], 12.005)
        self.assertAlmostEqual(config["armed_position_deg"], 12.005)

    def test_selected_p0_needs_continuous_ready_and_stable_interval_before_arming(self):
        position = [0.0]
        state = [MotionStatus(0, 0x33)]
        self.device.position = lambda: position[0]
        self.device.status = lambda: state[0]
        c = self.make_controller(RotationScanConfig(
            initial_position_deg=0, settle_seconds=0.08, motion_timeout_seconds=1))
        c.counter.start = Mock(wraps=c.counter.start)
        c.start()
        self.assertTrue(wait_until(lambda: c.snapshot().elapsed_ms > 0))
        position[0] = 0.02
        time.sleep(0.1)
        c.counter.start.assert_not_called()
        position[0] = 0
        state[0] = MotionStatus(0, 0x28)
        time.sleep(0.1)
        c.counter.start.assert_not_called()
        resumed = time.monotonic()
        state[0] = MotionStatus(0, 0x33)
        self.assertTrue(wait_until(lambda: c.snapshot().phase == "holding"), c.snapshot())
        self.assertGreaterEqual(time.monotonic() - resumed, 0.075)
        c.counter.start.assert_called_once()
        self.assertEqual(self.device.move_history, [])

    def test_selected_p0_settling_can_be_cancelled_without_arming_or_stop_command(self):
        analog = Mock()
        c = self.make_controller(RotationScanConfig(
            initial_position_deg=0, settle_seconds=0.5), analog=analog)
        c.counter.start = Mock(wraps=c.counter.start)
        stop_count = self.device.stop_count
        c.start()
        self.assertTrue(wait_until(lambda: c.snapshot().elapsed_ms > 0))
        c.stop()
        self.assertEqual(c.snapshot().phase, "stopped")
        c.counter.start.assert_not_called()
        analog.start.assert_not_called()
        self.assertEqual(self.device.move_history, [])
        self.assertEqual(self.device.stop_count, stop_count)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_selected_p0_drift_timeout_does_not_arm_or_command_motion(self):
        position_reads = 0
        def drifting_position():
            nonlocal position_reads
            position_reads += 1
            return 0 if position_reads == 1 else 0.02
        self.device.status = lambda: MotionStatus(0, 0x33)
        self.device.position = drifting_position
        analog = Mock()
        c = self.make_controller(RotationScanConfig(
            initial_position_deg=0, settle_seconds=0.02, motion_timeout_seconds=0.07), analog=analog)
        c.counter.start = Mock(wraps=c.counter.start)
        stop_count = self.device.stop_count
        c.start()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("初始位置 P0 稳定等待超时", c.snapshot().error)
        c.counter.start.assert_not_called()
        analog.start.assert_not_called()
        self.assertEqual(self.device.move_history, [])
        self.assertEqual(self.device.stop_count, stop_count)

    def test_negative_scan_and_no_trigger_holds(self):
        c = self.make_controller(RotationScanConfig(step_deg=-0.1, point_count=2, settle_seconds=0))
        self.arm(c)
        time.sleep(0.05)
        self.assertEqual(self.device.move_history, [])
        for k, angle in enumerate((-0.1, 0, -0.1), 1):
            self.edge()
            self.assertTrue(wait_until(lambda: c.snapshot().completed == k))
            self.assertAlmostEqual(self.device.position(), angle)

    def test_3hz_cycles_include_flyback(self):
        c = self.make_controller(RotationScanConfig(step_deg=0.2, point_count=4))
        self.arm(c)
        next_edge = time.monotonic()
        for k in range(1, 9):
            time.sleep(max(0, next_edge - time.monotonic()))
            self.edge()
            next_edge += 1 / 3
        self.assertTrue(wait_until(lambda: c.snapshot().completed == 8), c.snapshot())
        self.assertEqual(c.snapshot().phase, "holding")
        self.assertEqual(c.snapshot().cycle, 2)
        self.assertEqual(self.device.position(), 0)

    def test_edge_during_counter_start_is_first_move_not_baseline(self):
        c = self.make_controller()
        start = c.counter.start
        def start_with_edge():
            start()
            self.edge()
        c.counter.start = start_with_edge
        c.start()
        self.assertTrue(wait_until(lambda: c.snapshot().completed == 1), c.snapshot())
        self.assertAlmostEqual(self.device.move_history[0], 0.2)

    def test_counter_rollback_stops(self):
        c = self.make_controller()
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: c.snapshot().completed == 1))
        c.counter.read_count = lambda: 0
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("回退", c.snapshot().error)

    def test_write_failure_stops_motion_and_releases(self):
        c = self.make_controller()
        record = c._record
        def fail_on_move(event, block=None):
            if event == "move_sent":
                raise OSError("disk full")
            record(event, block)
        c._record = fail_on_move
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("disk full", c.snapshot().error)
        self.assertEqual(c.snapshot().completed, 0)
        self.assertFalse(self.device.status().moving)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_disconnect_during_motion_is_reported_unknown(self):
        self.device.set_velocity(0.1)
        c = self.make_controller()
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: self.device.status().moving))
        self.device.disconnect()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("状态未知", c.snapshot().error)
        self.assertFalse(self.manager.is_connected(self.stage))

    def test_analog_read_error_stops_scan(self):
        class BrokenReader:
            def start(self):
                pass
            def read(self):
                raise RuntimeError("AI disconnected")
            def stop(self):
                pass
        analog = AnalogAcquisitionController(BrokenReader())
        c = self.make_controller(analog=analog)
        c.start()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("AI disconnected", c.snapshot().error)
        self.assertEqual(c.snapshot().completed, 0)
        self.assertIsNone(self.manager.lease_owner(self.ni.id))

    def test_busy_edge_stops_without_replaying(self):
        self.device.set_velocity(0.2)
        c = self.make_controller()
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: len(self.device.move_history) == 1))
        self.edge()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertEqual(c.snapshot().phase, "error")
        self.assertIn("触发过快", c.snapshot().error)
        self.assertEqual(c.snapshot().received, 2)
        self.assertEqual(c.snapshot().completed, 0)
        self.assertEqual(len(self.device.move_history), 1)
        self.assertGreater(self.device.stop_count, 0)
        self.assertIsNone(self.manager.lease_owner(self.ni.id))

    def test_settling_edge_is_not_a_valid_next_step(self):
        c = self.make_controller(RotationScanConfig(settle_seconds=0.5))
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: c.snapshot().phase == "settling"))
        self.edge()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertEqual(c.snapshot().completed, 0)
        self.assertEqual(len(self.device.move_history), 1)

    def test_jump_is_not_replayed(self):
        c = self.make_controller()
        self.arm(c)
        self.edge(3)
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertEqual(c.snapshot().received, 3)
        self.assertEqual(self.device.move_history, [])

    def test_long_flyback_detects_busy_edge(self):
        c = self.make_controller(RotationScanConfig(step_deg=0.5, point_count=4, settle_seconds=0))
        self.arm(c)
        for k in range(1, 4):
            self.edge()
            self.assertTrue(wait_until(lambda: c.snapshot().completed == k))
        self.device.set_velocity(1)
        self.edge()
        self.assertTrue(wait_until(lambda: len(self.device.move_history) == 4))
        time.sleep(0.34)
        self.edge()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertEqual(c.snapshot().completed, 3)
        self.assertEqual(self.device.move_history[-1], 0)
        self.assertIn("触发过快", c.snapshot().error)

    def test_timeout_and_stop_release_resources(self):
        self.device.set_velocity(0.1)
        c = self.make_controller(RotationScanConfig(motion_timeout_seconds=0.08, settle_seconds=0))
        self.arm(c)
        with self.assertRaises(DeviceBusyError):
            self.manager.disconnect(self.stage)
        self.edge()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("超时", c.snapshot().error)
        self.assertFalse(self.device.status().moving)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_cancel_moving_does_not_complete_point(self):
        self.device.set_velocity(0.1)
        c = self.make_controller()
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: self.device.status().moving))
        c.stop()
        self.assertEqual(c.snapshot().completed, 0)
        self.assertFalse(self.device.status().moving)

    def test_preflight_limits_and_atomic_lease_failure_never_move(self):
        c = self.make_controller(RotationScanConfig(step_deg=0.9, point_count=1000))
        c.start()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("限位", c.snapshot().error)
        self.assertEqual(self.device.move_history, [])
        self.manager.acquire_devices("other", [self.ni.id])
        self.addCleanup(lambda: self.manager.release_devices("other"))
        other = self.make_controller(directory=Path(self.tmp.name) / "other")
        other.start()
        self.assertTrue(wait_until(lambda: not other.is_running))
        self.assertEqual(other.snapshot().phase, "error")
        self.assertIsNone(self.manager.lease_owner(self.stage.id))
        self.assertEqual(self.manager.lease_owner(self.ni.id), "other")

    def test_simultaneous_continuous_ai_and_rotation(self):
        reader = self.manager.analog_input_for(self.ni,
            AnalogInputConfig((f"{self.ni.resource}/ai0", f"{self.ni.resource}/ai1"), 1000, 20))
        analog_path = Path(self.tmp.name) / "analog.csv"
        analog = AnalogAcquisitionController(reader, maximum_retained_samples=1000, stream_path=analog_path)
        c = self.make_controller(analog=analog)
        self.arm(c)
        self.edge()
        self.assertTrue(wait_until(lambda: c.snapshot().completed == 1 and analog.snapshot().saved_samples >= 100))
        c.stop()
        with analog_path.open(encoding="utf-8-sig") as f:
            data = [r for r in csv.reader(f) if r and not r[0].startswith("#")]
        self.assertEqual(len(data[0]), 4)
        self.assertEqual([int(r[0]) for r in data[1:]], list(range(len(data)-1)))
        self.assertIsNone(self.manager.lease_owner(self.ni.id))

    def test_failed_counter_start_stops_ai_and_releases(self):
        class BadCounter:
            def start(self):
                raise RuntimeError("route conflict")
        c = self.make_controller()
        c.counter = BadCounter()
        c.start()
        self.assertTrue(wait_until(lambda: not c.is_running))
        self.assertIn("route conflict", c.snapshot().error)
        self.assertIsNone(self.manager.lease_owner(self.stage.id))

    def test_config_validation_and_discovery(self):
        for kwargs in ({"step_deg": 0}, {"step_deg": 1}, {"step_deg": math.nan},
                       {"point_count": 1}, {"point_count": True}, {"tolerance_deg": 0.1},
                       {"settle_seconds": -1}, {"initial_position_deg": math.nan},
                       {"initial_position_deg": math.inf}, {"initial_position_deg": -math.inf}):
            with self.assertRaises(ValueError):
                RotationScanConfig(**kwargs).validate()
        resources = DeviceDiscoveryService().discover(DeviceKind.NEWPORT_SMC100)
        self.assertTrue(any(r.resource == VIRTUAL_SMC100_RESOURCE for r in resources))
        self.assertEqual(DeviceProbeService().probe(self.stage).state, ProbeState.ONLINE)
        real = self.stage.updated(resource="COM3::1")
        self.assertEqual(DeviceProbeService().probe(real).state, ProbeState.UNCHECKED)
        self.assertIs(self.device, self.manager.newport_device_for(self.stage))
        with self.assertRaises(ValueError):
            self.manager.session_for(self.stage)

    def test_points_use_selected_p0_and_legacy_positional_config_stays_compatible(self):
        legacy = RotationScanConfig(-0.2, 3, 0.01, 0.02, 10.0, 0.01)
        self.assertIsNone(legacy.initial_position_deg)
        self.assertEqual(legacy.points(5, (-360, 360)), (5, 4.8, 4.6))
        specified = RotationScanConfig(step_deg=-0.2, point_count=3, initial_position_deg=5)
        self.assertEqual(specified.points(5.005, (-360, 360)), (5, 4.8, 4.6))
        with self.assertRaisesRegex(ValueError, "限位"):
            RotationScanConfig(step_deg=-0.2, initial_position_deg=-360).points(0, (-360, 360))


if __name__ == "__main__":
    unittest.main()
