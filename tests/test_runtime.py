import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rigol_control import runtime
from rigol_control.drivers.vaunix_lms import VaunixLmsError, locate_vaunix_dll
from rigol_control.models import DeviceKind, DeviceRecord
from rigol_control.repository import DeviceParameterRepository, DeviceRepository, default_data_dir


class PortableRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.environment = patch.dict(os.environ, {})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name in ("RIGOL_GUI_DATA_DIR", "APPDATA", "VAUNIX_LMS_DLL",
                     "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
            os.environ.pop(name, None)

    def frozen(self, name="first", extraction="_internal"):
        launcher = self.root / name / "LabControl.exe"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        extracted = self.root / extraction
        extracted.mkdir(parents=True, exist_ok=True)
        for attribute, value in (("frozen", True), ("executable", str(launcher)),
                                 ("_MEIPASS", str(extracted))):
            context = patch.object(sys, attribute, value, create=True)
            context.start()
            self.addCleanup(context.stop)
        return launcher.parent, extracted

    def test_frozen_data_lives_beside_exe_not_onefile_extraction_or_working_dir(self):
        launcher, extraction = self.frozen(extraction="temporary-onefile")
        self.assertEqual(runtime.prepare_data_dir(), launcher / "data")
        self.assertEqual(runtime.bundle_root(), extraction)
        self.assertEqual(list((launcher / "data").iterdir()), [])
        self.assertFalse((extraction / "data").exists())

    def test_explicit_and_environment_data_dirs_override_portable_default(self):
        launcher, _ = self.frozen()
        os.environ["RIGOL_GUI_DATA_DIR"] = str(self.root / "environment")
        self.assertEqual(default_data_dir(), self.root / "environment")
        explicit = self.root / "chosen"
        self.assertEqual(runtime.prepare_data_dir(explicit), explicit)
        self.assertEqual(DeviceRepository(explicit).data_dir, explicit)
        self.assertFalse((launcher / "data").exists())

    def test_source_retains_appdata_and_config_home_defaults(self):
        with patch.object(sys, "frozen", False, create=True):
            os.environ["APPDATA"] = str(self.root / "roaming")
            self.assertEqual(default_data_dir(), self.root / "roaming" / "RigolGeneratorControl")
            del os.environ["APPDATA"]
            with patch.object(Path, "home", return_value=self.root):
                self.assertEqual(default_data_dir(), self.root / ".config" / "RigolGeneratorControl")

    def test_copying_portable_directory_keeps_devices_and_parameters(self):
        original, _ = self.frozen()
        data = runtime.prepare_data_dir()
        record = DeviceRecord.create(kind=DeviceKind.SIGLENT_SSG,
                                     name="迁移仪器", resource="TCPIP0::192.0.2.1::INSTR")
        DeviceRepository().save(record)
        DeviceParameterRepository().save(record.id, kind=record.kind.value,
                                        parameters={"controls": {"frequency": 1e9}})
        relocated = self.root / "另一台机器" / "portable"
        shutil.copytree(original, relocated)
        with patch.object(sys, "executable", str(relocated / "LabControl.exe")):
            self.assertEqual(default_data_dir(), relocated / "data")
            self.assertEqual(DeviceRepository().list(), [record])
            self.assertEqual(DeviceParameterRepository().load(record.id),
                             {"controls": {"frequency": 1e9}})
        self.assertTrue((data / "devices.json").is_file())

    def test_unwritable_directory_raises_without_falling_back(self):
        launcher, _ = self.frozen()
        with patch("rigol_control.runtime.tempfile.TemporaryFile",
                   side_effect=PermissionError("read-only location")):
            with self.assertRaisesRegex(RuntimeError, "数据目录不可写") as caught:
                runtime.prepare_data_dir()
        self.assertIn(str(launcher / "data"), str(caught.exception))
        self.assertIn("--data-dir", str(caught.exception))
        self.assertEqual(default_data_dir(), launcher / "data")

    def test_existing_file_cannot_be_used_as_data_directory(self):
        invalid = self.root / "file"
        invalid.write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "数据目录不可写"):
            runtime.prepare_data_dir(invalid)
        self.assertEqual(invalid.read_text(encoding="utf-8"), "keep me")

    def test_package_assets_follow_package_file_location(self):
        _, extracted = self.frozen()
        with patch.object(runtime, "__file__", str(extracted / "rigol_control" / "runtime.py")):
            self.assertEqual(runtime.package_resource("assets", "app_icon.ico"),
                             extracted / "rigol_control" / "assets" / "app_icon.ico")

    def test_frozen_qt_keeps_bundled_plugins_and_offscreen_test_setting(self):
        _, extracted = self.frozen()
        bundled = extracted / "PyQt6" / "Qt6" / "plugins"
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join((str(self.root / "other-python"), str(bundled)))
        os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(self.root / "other-qt")
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        runtime.prepare_qt_environment()
        self.assertEqual(os.environ["QT_PLUGIN_PATH"], str(bundled))
        self.assertNotIn("QT_QPA_PLATFORM_PLUGIN_PATH", os.environ)
        self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")

    def test_source_qt_plugin_configuration_is_unchanged(self):
        with patch.object(sys, "frozen", False, create=True):
            os.environ["QT_PLUGIN_PATH"] = "custom-source-qt"
            runtime.prepare_qt_environment()
            self.assertEqual(os.environ["QT_PLUGIN_PATH"], "custom-source-qt")

    def test_vaunix_frozen_search_prefers_environment_then_external_then_bundle(self):
        launcher, extracted = self.frozen()
        internal = extracted / "vendor" / "vaunix" / "vnx_fmsynth.dll"
        external = launcher / "vendor" / "vaunix" / "vnx_fmsynth.dll"
        for path in (internal, external):
            path.parent.mkdir(parents=True)
            path.write_bytes(b"fake test dll")
        self.assertEqual(locate_vaunix_dll(), external)
        configured = self.root / "configured.dll"
        configured.write_bytes(b"explicit test dll")
        os.environ["VAUNIX_LMS_DLL"] = str(configured)
        self.assertEqual(locate_vaunix_dll(), configured)
        del os.environ["VAUNIX_LMS_DLL"]
        external.unlink()
        self.assertEqual(locate_vaunix_dll(), internal)
        internal.unlink()
        with self.assertRaisesRegex(VaunixLmsError, "vendor/vaunix"):
            locate_vaunix_dll()

    def test_main_propagates_explicit_data_directory_to_recording_defaults(self):
        from rigol_control import app
        explicit = self.root / "chosen"
        application = Mock()
        application.exec.return_value = 0
        with patch.object(app, "create_application", return_value=application), \
             patch.object(app, "DeviceCenterWindow") as window:
            self.assertEqual(app.main(["--data-dir", str(explicit)]), 0)
        self.assertEqual(default_data_dir(), explicit)
        self.assertEqual(window.call_args.args[0].data_dir, explicit)

    def test_main_does_not_open_window_if_data_directory_is_unwritable(self):
        from rigol_control import app
        with patch.object(app, "prepare_data_dir", side_effect=RuntimeError("数据目录不可写")), \
             patch.object(app, "create_application") as application, \
             patch.object(app, "DeviceCenterWindow") as window:
            with self.assertRaisesRegex(RuntimeError, "数据目录不可写"):
                app.main([])
        application.assert_not_called()
        window.assert_not_called()


if __name__ == "__main__":
    unittest.main()
