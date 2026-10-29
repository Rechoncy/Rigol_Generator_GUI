import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dg_readback_ui as fixture
from rigol_control.repository import DeviceParameterRepository
from rigol_control.ui.device_windows import DG4000Window


class DgReadbackUnitFollowTests(unittest.TestCase):
    setUpClass = classmethod(fixture.DgReadbackUiTests.setUpClass.__func__)
    setUp = fixture.DgReadbackUiTests.setUp
    tearDown = fixture.DgReadbackUiTests.tearDown
    wait = fixture.DgReadbackUiTests.wait
    refresh = fixture.DgReadbackUiTests.refresh
    assert_unknown = fixture.DgReadbackUiTests.assert_unknown

    def test_channel_unit_change_reformats_only_that_channel_without_io(self):
        self.refresh()
        before = self.generator.snapshot().channels
        editor = self.window.channel_controls[2]["amplitude"]
        base_value = editor.value()
        self.generator.commands.clear()
        editor.set_unit("mVpp")
        self.assertEqual(self.window.output_readback_units[2].currentText(), "mVpp")
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "125 mVpp")
        self.assertEqual(self.window.output_readback_values[1]["amplitude"].text(), "2.5 Vpp")
        self.assertEqual(editor.value(), base_value)
        self.assertEqual(self.generator.snapshot().channels, before)
        self.assertEqual(list(self.generator.commands), [])

    def test_restored_mvpp_editor_is_used_for_first_read_without_construction_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DeviceParameterRepository(Path(directory))
            self.window.parameter_repository = repository
            self.window.channel_controls[1]["amplitude"].set_unit("mVpp")
            self.window._save_device_parameters(force=True)
            restored = None
            try:
                restored = DG4000Window(self.record, [], self.manager, parameter_repository=repository)
                self.assertTrue(self.wait(lambda: not restored._call_tasks))
                self.assertEqual(restored.channel_controls[1]["amplitude"].current_unit(), "mVpp")
                self.assertEqual(restored.output_readback_units[1].currentText(), "mVpp")
                editors = restored._device_parameter_snapshot()
                restored._refresh_output_readback()
                self.assertTrue(self.wait(lambda: not restored._call_tasks))
                self.assertEqual(restored.output_readback_values[1]["amplitude"].text(), "2500 mVpp")
                self.assertEqual(restored._device_parameter_snapshot(), editors)
                for explicit in (False, True):
                    controls = restored._persistent_controls(use_explicit_keys=explicit).values()
                    self.assertTrue(all(combo not in controls for combo in restored.output_readback_units.values()))
            finally:
                if restored is not None:
                    restored.close()
                self.window.parameter_repository = None

    def test_manual_readback_override_survives_refresh_until_editor_unit_changes(self):
        editor = self.window.channel_controls[2]["amplitude"]
        editor.set_unit("mVpp")
        self.refresh()
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "125 mVpp")
        self.window.output_readback_units[2].setCurrentText("Vpp")
        self.refresh()
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "0.125 Vpp")
        self.assertEqual(editor.current_unit(), "mVpp")
        editor.set_unit("Vpp")
        editor.set_unit("mVpp")
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "125 mVpp")

    def test_editor_peak_unit_does_not_convert_native_dbm_or_vrms(self):
        self.generator.amplitude_units.update({1: "DBM", 2: "VRMS"})
        original = self.window.driver.session.query

        def native(command, **kwargs):
            if command == ":SOUR1:VOLT?":
                return "-37.25"
            if command == ":SOUR2:VOLT?":
                return "0.0125"
            return original(command, **kwargs)

        with patch.object(self.window.driver.session, "query", side_effect=native):
            self.refresh()
        self.generator.commands.clear()
        for channel in (1, 2):
            self.window.channel_controls[channel]["amplitude"].set_unit("mVpp")
            self.assertFalse(self.window.output_readback_units[channel].isEnabled())
        self.assertEqual(self.window.output_readback_values[1]["amplitude"].text(), "-37.25 dBm")
        self.assertEqual(self.window.output_readback_values[2]["amplitude"].text(), "0.0125 Vrms")
        self.assertEqual(list(self.generator.commands), [])

    def test_following_editor_unit_cannot_restore_cache_after_disconnect_or_write(self):
        for change in ("disconnect", "write"):
            with self.subTest(change=change):
                if not self.window.driver.is_connected:
                    self.manager.connect(self.record)
                self.refresh()
                self.window.auto_readback.timer.stop()
                if change == "disconnect":
                    self.manager.disconnect(self.record)
                else:
                    self.manager.note_output_change(self.record)
                self.generator.commands.clear()
                editor = self.window.channel_controls[2]["amplitude"]
                editor.set_unit("Vpp" if editor.current_unit() == "mVpp" else "mVpp")
                self.assert_unknown()
                self.assertEqual(list(self.generator.commands), [])

    def test_level_span_follows_mvpp_editor_and_keeps_unknown_native_unit_label(self):
        self.window.channel_controls[2]["amplitude"].set_unit("mVpp")
        original = self.window.driver.session.query
        with patch.object(self.window.driver.session, "query", side_effect=lambda command, **kwargs:
                          "" if command.endswith(":VOLT:UNIT?") else original(command, **kwargs)):
            self.refresh()
        label = self.window.output_readback_values[2]["amplitude"]
        self.assertEqual(label.text(), "125 mVpp（电平差）")
        self.assertIn("单位未知", label.toolTip())


if __name__ == "__main__":
    unittest.main()
