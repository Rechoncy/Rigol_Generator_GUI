import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.build_release import resolve_vaunix_dll
from rigol_control.drivers.vaunix_lms import VaunixLmsApi


class ReleaseVendorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def dll(self, path, *, machine=0x8664, magic=0x20b, characteristics=0x2000):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = bytearray(0x100)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 0x3c, 0x80)
        data[0x80:0x84] = b"PE\0\0"
        struct.pack_into("<H", data, 0x84, machine)
        struct.pack_into("<H", data, 0x96, characteristics)
        struct.pack_into("<H", data, 0x98, magic)
        path.write_bytes(data)
        return path

    def test_default_root_library_and_explicit_override(self):
        self.assertIsNone(resolve_vaunix_dll(None, self.root))
        default = self.dll(self.root / "vnx_fmsynth.dll")
        self.assertEqual(resolve_vaunix_dll(None, self.root), default.resolve())
        override = self.dll(self.root / "other SDK" / "vnx_fmsynth.dll")
        self.assertEqual(resolve_vaunix_dll(override, self.root), override.resolve())
        with self.assertRaises(FileNotFoundError):
            resolve_vaunix_dll(self.root / "missing.dll", self.root)

    def test_wrong_architecture_and_non_library_fail_before_packaging(self):
        for machine, magic, flags in ((0x14c, 0x10b, 0x2000), (0xaa64, 0x20b, 0x2000), (0x8664, 0x20b, 0)):
            with self.subTest(machine=machine, magic=magic, flags=flags):
                path = self.dll(self.root / "vnx_fmsynth.dll", machine=machine, magic=magic, characteristics=flags)
                with self.assertRaisesRegex(RuntimeError, "Windows x64 DLL"):
                    resolve_vaunix_dll(None, self.root)

    def test_sdk_load_check_binds_driver_without_accessing_hardware(self):
        dll = Mock()
        with patch("rigol_control.drivers.vaunix_lms.ctypes.CDLL", return_value=dll):
            self.assertIs(VaunixLmsApi.load_library(self.root / "vnx_fmsynth.dll"), dll)
        dll.fnLMS_SetTestMode.assert_not_called()
        dll.fnLMS_GetNumDevices.assert_not_called()
        dll.fnLMS_InitDevice.assert_not_called()
        self.assertEqual(dll.fnLMS_GetDLLVersion.argtypes, [])

    def test_corrupt_or_truncated_library_fails_before_packaging(self):
        path = self.root / "vnx_fmsynth.dll"
        for data in (b"", b"not a library", b"MZ" + bytes(62), b"MZ" + bytes(58) + struct.pack("<I", 0xffffffff)):
            with self.subTest(data=data):
                path.write_bytes(data)
                with self.assertRaisesRegex(RuntimeError, "Windows x64 DLL"):
                    resolve_vaunix_dll(None, self.root)
