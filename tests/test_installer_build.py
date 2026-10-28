"""Installer boundary tests run without installing into a user's Windows profile."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid5


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer_builder", ROOT / "tools/build_installer.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def x64_executable() -> bytes:
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", data, 0x84, 0x8664)
    struct.pack_into("<H", data, 0x98, 0x20B)
    return bytes(data)


class InstallerBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.payload = self.root / "portable O'Brien 中文"
        self.payload.mkdir()
        (self.payload / "Lab Control Center.exe").write_bytes(x64_executable())
        internal = self.payload / "_internal"
        internal.mkdir()
        for name in ("base_library.zip", "python3.dll", "python313.dll"):
            (internal / name).write_bytes(b"bundled test input")
        (self.payload / "source.zip").write_bytes(b"source fixture")
        (self.payload / "build-info.json").write_text(json.dumps({
            "version": "0.7.23", "platform": "windows-x64"}), encoding="utf-8")
        self.compiler = self.root / "compiler" / "ISCC.exe"
        self.compiler.parent.mkdir()
        self.compiler.write_bytes(b"test compiler placeholder")
        self.output = self.root / "output"

    def tearDown(self):
        self.temporary.cleanup()

    def fake_compile(self, arguments, **kwargs):
        self.compiler_arguments = arguments
        stage = Path(next(value.removeprefix("/DOutputDir=") for value in arguments
                          if value.startswith("/DOutputDir=")))
        name = next(value.removeprefix("/DOutputName=") for value in arguments
                    if value.startswith("/DOutputName="))
        (stage / (name + ".exe")).write_bytes(b"MZverified fixture")

    def build(self, **kwargs):
        with patch.object(builder.subprocess, "run", side_effect=self.fake_compile):
            return builder.build_installer(self.payload, "0.7.23", self.output,
                                           iscc=self.compiler, **kwargs)

    def test_accepts_complete_x64_portable_tree(self):
        self.assertEqual(builder.validate_payload(self.payload, "0.7.23"), self.payload.resolve())

    def test_rejects_invalid_versions_and_mismatched_build(self):
        for version in ("v0.7.23", "0.7", "00.7.23", "0.7.23-preview", "0.7.24"):
            with self.subTest(version=version), self.assertRaises((ValueError, RuntimeError)):
                builder.validate_payload(self.payload, version)

    def test_requires_real_x64_executable_and_bundled_python(self):
        executable = self.payload / "Lab Control Center.exe"
        original = executable.read_bytes()
        for content in (b"not executable", original[:65], original[:0x84] + b"L\x01" + original[0x86:]):
            executable.write_bytes(content)
            with self.subTest(content=content[:10]), self.assertRaises(RuntimeError):
                builder.validate_payload(self.payload, "0.7.23")
        executable.write_bytes(original)
        (self.payload / "_internal/python313.dll").unlink()
        with self.assertRaisesRegex(RuntimeError, "Python"):
            builder.validate_payload(self.payload, "0.7.23")

    def test_refuses_packaging_user_data_or_previous_installation(self):
        for name in ("data", "runtime-data", "logs", "updates", "installed-mode.json", "unins000.exe"):
            unwanted = self.payload / name
            unwanted.write_text("private user state", encoding="utf-8")
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "user files"):
                builder.validate_payload(self.payload, "0.7.23")
            unwanted.unlink()

    def test_existing_installer_is_never_overwritten(self):
        self.output.mkdir()
        target = self.output / "Lab-Control-Center-0.7.23-windows-x64-setup.exe"
        target.write_bytes(b"keep earlier installer")
        with self.assertRaises(FileExistsError):
            self.build()
        self.assertEqual(target.read_bytes(), b"keep earlier installer")

    def test_isolated_test_builder_has_separate_registry_mutex_and_asset_name(self):
        target = self.build(test_id="upgrade-acceptance")
        self.assertIn("-setup-test-upgrade-acceptance.exe", target.name)
        expected_id = str(uuid5(UUID(builder.APP_ID), "upgrade-acceptance")).upper()
        self.assertIn(f"/DInstallerId={expected_id}", self.compiler_arguments)
        self.assertIn("/DInstanceSuffix=-Test-upgrade-acceptance", self.compiler_arguments)
        self.assertNotEqual(expected_id, builder.APP_ID)
        self.assertEqual(target.read_bytes(), b"MZverified fixture")

    def test_compiler_arguments_preserve_unicode_and_apostrophe_without_shell(self):
        target = self.build()
        self.assertTrue(target.is_file())
        self.assertIn(f"/DPayloadDir={self.payload.resolve()}", self.compiler_arguments)
        self.assertIn(f"/DInstallerId={builder.APP_ID}", self.compiler_arguments)
        self.assertIn("/DInstanceSuffix=", self.compiler_arguments)
        self.assertFalse(any(self.output.parent.glob("installer-*")))

    def test_invalid_test_id_cannot_inject_script_defines(self):
        for test_id in ("../production", "a b", "a\"", "", "a" * 41):
            with self.subTest(test_id=test_id), self.assertRaises(ValueError):
                self.build(test_id=test_id)

    def test_compiler_failure_does_not_publish_partial_installer(self):
        with patch.object(builder.subprocess, "run", side_effect=RuntimeError("compiler failure")):
            with self.assertRaisesRegex(RuntimeError, "compiler failure"):
                builder.build_installer(self.payload, "0.7.23", self.output, iscc=self.compiler)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(self.output.parent.glob("installer-*")))

    def test_iscc_explicit_location_does_not_fall_back_on_missing_path(self):
        with self.assertRaisesRegex(RuntimeError, "compiler not found"):
            builder.find_iscc(self.root / "missing/ISCC.exe")


if __name__ == "__main__":
    unittest.main()
