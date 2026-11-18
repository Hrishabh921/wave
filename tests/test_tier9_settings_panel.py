"""Tier 9 - logic tests for ui/settings_panel.py's handler methods, WITHOUT
a display or PyQt6.

test_phase4_ui_logic.py already covers _provider_text()/_provider_brief()
and the SUCCESS branch of _on_provider_checked() (see its
test_provider_status_text_and_connection_check_result). Everything else in
this ~625-line, 30-method file - theme/accent/blur/opacity handlers, the
five voice-settings handlers, the three privacy toggles, and the
confirm-then-clear-everything flow - has never been touched. Those are the
methods this file covers, reusing the exact object.__new__(SettingsPanel)
pattern test_phase4_ui_logic.py already established for this same class
(construct the bare instance, set only the attributes the one method under
test reads, call it, assert).

Reuses test_phase4_ui_logic.py's stand-in rather than duplicating it.
"""
import dataclasses
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_phase4_ui_logic as P4


def import_with_fakes(module_name):
    for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
        del sys.modules[name]
    for _ in range(30):
        try:
            return __import__(module_name, fromlist=["_"])
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if not missing or missing in sys.modules:
                raise
            sys.modules[missing] = P4._QtModule(missing)


class ImmediatePool:
    """Runs the job synchronously at submit() time and remembers the
    outcome, then replays it to whichever signal gets connected.

    test_phase4_ui_logic.SyncPool only runs the job the moment .result is
    connected, and dispatches failures to .error's slots *at that same
    instant* - so if the real caller connects .result before .error (as
    every caller in this codebase does: see _test_provider_clicked and
    OnboardingWizard._test_ai_provider), a failing job's error is dispatched
    to a still-empty .error.slots list and silently dropped. That ordering
    is exactly what the real Worker/WorkerPool in ui/worker_pool.py does NOT
    care about - a real background thread finishes whenever it finishes,
    after both signals are already connected. This fake preserves that
    order-independence instead of replicating the ordering trap."""

    def submit(self, fn, *a, **k):
        try:
            outcome = ("ok", fn(*a, **k))
        except Exception as e:
            outcome = ("error", str(e))

        result_sig, error_sig = P4._BoundSignal(), P4._BoundSignal()
        signals = types.SimpleNamespace(result=result_sig, error=error_sig)

        def result_connect(slot):
            result_sig.slots.append(slot)
            if outcome[0] == "ok":
                slot(outcome[1])

        def error_connect(slot):
            error_sig.slots.append(slot)
            if outcome[0] == "error":
                slot(outcome[1])

        signals.result = types.SimpleNamespace(connect=result_connect)
        signals.error = types.SimpleNamespace(connect=error_connect)
        return types.SimpleNamespace(signals=signals)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class SettingsPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.sp = import_with_fakes("ui.settings_panel")

    def _panel(self):
        return object.__new__(self.sp.SettingsPanel)

    def _theme(self, **overrides):
        fields = dict(
            name="Test Theme", bg_main="#000", bg_panel="#111", bg_surface="#222", bg_surface2="#333",
            border="#444", border_soft="#555", text_main="#fff", text_muted="#ccc", text_faint="#999",
            accent_1="#a1", accent_2="#a2", accent_3="#a3", success="#0f0", warning="#ff0", danger="#f00",
            bubble_sent="#b1", bubble_recv="#b2", page_bg_alpha=0.9, panel_top_alpha=0.8, panel_bottom_alpha=0.7,
        )
        fields.update(overrides)
        return self.sp.ThemeColors(**fields)

    # ---------- page switching ----------
    def test_switch_page_sets_the_stacked_widgets_current_index(self):
        panel = self._panel()
        panel.pages = mock.Mock()
        panel._switch_page(3)
        panel.pages.setCurrentIndex.assert_called_once_with(3)

    # ---------- theme change ----------
    def test_on_theme_change_updates_theme_reference_and_refreshes_controls(self):
        panel = self._panel()
        new_theme = self._theme(name="Midnight Blue")
        panel._main = types.SimpleNamespace(
            theme_manager=types.SimpleNamespace(set_theme=mock.Mock(), get_current=lambda: new_theme),
            apply_theme=mock.Mock(),
        )
        panel._refresh_appearance_controls = mock.Mock()
        panel._on_theme_change("Midnight Blue")
        panel._main.theme_manager.set_theme.assert_called_once_with("Midnight Blue")
        panel._main.apply_theme.assert_called_once_with("Midnight Blue")
        self.assertIs(panel._theme, new_theme)
        panel._refresh_appearance_controls.assert_called_once()

    def test_refresh_appearance_controls_pulls_slider_values_from_theme_and_backdrop(self):
        panel = self._panel()
        panel._theme = self._theme(accent_1="#111111", accent_2="#222222", accent_3="#333333", page_bg_alpha=0.75)
        panel._main = types.SimpleNamespace(backdrop=types.SimpleNamespace(_blur_radius=18.0))
        panel.accent1_btn, panel.accent2_btn, panel.accent3_btn = mock.Mock(), mock.Mock(), mock.Mock()
        panel.blur_slider, panel.opacity_slider = mock.Mock(), mock.Mock()
        panel._refresh_appearance_controls()
        panel.blur_slider.setValue.assert_called_once_with(18)
        panel.opacity_slider.setValue.assert_called_once_with(75)
        self.assertIn("#111111", panel.accent1_btn.setStyleSheet.call_args.args[0])

    # ---------- accent color picking ----------
    def test_pick_accent_with_a_valid_color_updates_the_theme_and_reapplies(self):
        panel = self._panel()
        panel._theme = self._theme(name="Picked Theme", accent_2="#OLD")
        panel._main = types.SimpleNamespace(apply_theme=mock.Mock())
        panel._refresh_appearance_controls = mock.Mock()
        fake_color = types.SimpleNamespace(isValid=lambda: True, name=lambda: "#NEWCOLOR")
        with mock.patch.object(self.sp, "QColorDialog") as mock_dialog:
            mock_dialog.getColor.return_value = fake_color
            panel._pick_accent(2)
        self.assertEqual(panel._theme.accent_2, "#NEWCOLOR")
        panel._refresh_appearance_controls.assert_called_once()
        panel._main.apply_theme.assert_called_once_with("Picked Theme")

    def test_pick_accent_cancelled_leaves_the_theme_untouched(self):
        panel = self._panel()
        panel._theme = self._theme(accent_1="#UNCHANGED")
        panel._main = types.SimpleNamespace(apply_theme=mock.Mock())
        panel._refresh_appearance_controls = mock.Mock()
        fake_color = types.SimpleNamespace(isValid=lambda: False)
        with mock.patch.object(self.sp, "QColorDialog") as mock_dialog:
            mock_dialog.getColor.return_value = fake_color
            panel._pick_accent(1)
        self.assertEqual(panel._theme.accent_1, "#UNCHANGED")
        panel._refresh_appearance_controls.assert_not_called()
        panel._main.apply_theme.assert_not_called()

    # ---------- blur / opacity ----------
    def test_on_blur_change_forwards_to_the_backdrop(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(backdrop=mock.Mock())
        panel._on_blur_change(32)
        panel._main.backdrop.set_blur_radius.assert_called_once_with(32)

    def test_on_opacity_change_converts_percent_to_a_fraction_and_reapplies(self):
        panel = self._panel()
        panel._theme = self._theme(name="OpacityTheme")
        panel._main = types.SimpleNamespace(apply_theme=mock.Mock())
        panel._on_opacity_change(70)
        self.assertAlmostEqual(panel._theme.page_bg_alpha, 0.70)
        panel._main.apply_theme.assert_called_once_with("OpacityTheme")

    # ---------- save as custom theme ----------
    def test_save_custom_theme_copies_every_field_and_registers_it(self):
        panel = self._panel()
        panel._theme = self._theme(name="irrelevant - overwritten by the chosen filename")
        panel._main = types.SimpleNamespace(theme_manager=types.SimpleNamespace(save_custom_theme=mock.Mock()))
        panel.theme_combo = mock.Mock()
        with mock.patch.object(self.sp, "QFileDialog") as mock_dialog, \
             mock.patch.object(self.sp.os.path, "basename", return_value="exported.json"):
            mock_dialog.getSaveFileName.return_value = ("/anywhere/exported.json", True)
            panel._save_custom_theme()
        saved = panel._main.theme_manager.save_custom_theme.call_args.args[0]
        self.assertEqual(saved, dataclasses.replace(panel._theme, name="exported"))
        panel.theme_combo.addItem.assert_called_once_with("exported")
        panel.theme_combo.setCurrentText.assert_called_once_with("exported")

    def test_save_custom_theme_cancelled_dialog_saves_nothing(self):
        panel = self._panel()
        panel._theme = self._theme()
        panel._main = types.SimpleNamespace(theme_manager=types.SimpleNamespace(save_custom_theme=mock.Mock()))
        panel.theme_combo = mock.Mock()
        with mock.patch.object(self.sp, "QFileDialog") as mock_dialog:
            mock_dialog.getSaveFileName.return_value = ("", False)
            panel._save_custom_theme()
        panel._main.theme_manager.save_custom_theme.assert_not_called()
        panel.theme_combo.addItem.assert_not_called()

    # ---------- voice settings ----------
    def test_on_tts_voice_change_sets_the_matching_voice_id(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "tts_engine") as mock_tts:
            panel._on_tts_voice_change("Aria — English US (Female)")
        mock_tts.set_voice.assert_called_once_with("en-US-AriaNeural")

    def test_on_tts_voice_change_with_an_unknown_label_does_nothing(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "tts_engine") as mock_tts:
            panel._on_tts_voice_change("Not A Real Voice")
        mock_tts.set_voice.assert_not_called()

    def test_on_rate_change_formats_values_above_below_and_at_the_midpoint(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "tts_engine") as mock_tts:
            panel._on_rate_change(100)
            panel._on_rate_change(50)
            panel._on_rate_change(200)
        mock_tts.set_rate.assert_has_calls([mock.call("+0%"), mock.call("-50%"), mock.call("+100%")])

    def test_on_tts_volume_change_converts_percent_to_a_fraction(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "tts_engine") as mock_tts:
            panel._on_tts_volume_change(45)
        mock_tts.set_volume.assert_called_once_with(0.45)

    def test_on_stt_lang_change_forwards_the_language_code(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "stt_engine") as mock_stt:
            panel._on_stt_lang_change("hi-IN")
        mock_stt.set_language.assert_called_once_with("hi-IN")

    # ---------- wake word ----------
    def test_wake_word_preset_always_appends_the_wake_up_fallback(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(wake_phrases=None, mic_worker=None)
        panel._on_wake_word_change("Hello Wave")
        self.assertEqual(panel._main.wake_phrases, ["hello wave", "wake up"])

    def test_wake_word_preset_also_updates_a_live_mic_worker(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(wake_phrases=None, mic_worker=mock.Mock())
        panel._on_wake_word_change("Okay Wave")
        panel._main.mic_worker.set_wake_phrases.assert_called_once_with(["okay wave", "ok wave", "wake up"])

    def test_wake_word_custom_with_text_lowercases_and_strips_it(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(wake_phrases=None, mic_worker=None)
        with mock.patch.object(self.sp, "QInputDialog") as mock_dialog:
            mock_dialog.getText.return_value = ("  Jarvis  ", True)
            panel._on_wake_word_change("Custom...")
        self.assertEqual(panel._main.wake_phrases, ["jarvis", "wake up"])

    def test_wake_word_custom_cancelled_falls_back_to_hey_wave(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(wake_phrases=None, mic_worker=None)
        with mock.patch.object(self.sp, "QInputDialog") as mock_dialog:
            mock_dialog.getText.return_value = ("Jarvis", False)
            panel._on_wake_word_change("Custom...")
        self.assertEqual(panel._main.wake_phrases, ["hey wave", "wake up"])

    def test_wake_word_custom_with_blank_text_falls_back_to_hey_wave(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(wake_phrases=None, mic_worker=None)
        with mock.patch.object(self.sp, "QInputDialog") as mock_dialog:
            mock_dialog.getText.return_value = ("   ", True)
            panel._on_wake_word_change("Custom...")
        self.assertEqual(panel._main.wake_phrases, ["hey wave", "wake up"])

    # ---------- privacy toggles ----------
    def test_clipboard_toggle_updates_a_live_worker(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(clip_worker=types.SimpleNamespace(monitoring_enabled=True))
        panel._on_clipboard_toggle(False)
        self.assertFalse(panel._main.clip_worker.monitoring_enabled)

    def test_clipboard_toggle_without_a_live_worker_does_not_crash(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(clip_worker=None)
        panel._on_clipboard_toggle(False)   # must not raise

    def test_screen_capture_toggle_sets_the_vision_capture_class_flag(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "VisionCapture") as mock_vc:
            panel._on_screen_capture_toggle(False)
        self.assertEqual(mock_vc.capture_enabled, False)

    def test_chat_history_toggle_sets_the_db_flag(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "db") as mock_db:
            panel._on_chat_history_toggle(False)
        self.assertEqual(mock_db.history_enabled, False)

    # ---------- clear all data ----------
    def _mock_messagebox(self, question_result):
        mock_box = mock.Mock()
        mock_box.StandardButton.Yes, mock_box.StandardButton.No = 1, 2
        mock_box.question.return_value = question_result
        return mock_box

    def test_clear_all_data_declined_does_nothing(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "QMessageBox", self._mock_messagebox(2)), \
             mock.patch.object(self.sp, "db") as mock_db, \
             mock.patch.object(self.sp, "undo_mod") as mock_undo, \
             mock.patch.object(self.sp, "audit_mod") as mock_audit, \
             mock.patch.object(self.sp.os.path, "exists", return_value=True), \
             mock.patch.object(self.sp.os, "remove") as mock_remove:
            panel._clear_all_data()
        mock_remove.assert_not_called()
        mock_undo.trash.empty.assert_not_called()
        mock_audit.audit.clear.assert_not_called()
        mock_db.clear_all.assert_not_called()

    def test_clear_all_data_confirmed_removes_settings_and_reports_full_success(self):
        panel = self._panel()
        mock_box = self._mock_messagebox(1)
        with mock.patch.object(self.sp, "QMessageBox", mock_box), \
             mock.patch.object(self.sp, "db") as mock_db, \
             mock.patch.object(self.sp, "undo_mod") as mock_undo, \
             mock.patch.object(self.sp, "audit_mod") as mock_audit, \
             mock.patch.object(self.sp.os.path, "exists", return_value=True), \
             mock.patch.object(self.sp.os, "remove") as mock_remove:
            mock_db.clear_all.return_value = True
            panel._clear_all_data()
        mock_remove.assert_called_once_with(self.sp.SETTINGS_FILE)
        mock_undo.trash.empty.assert_called_once()
        mock_audit.audit.clear.assert_called_once()
        mock_box.information.assert_called_once()
        mock_box.warning.assert_not_called()

    def test_clear_all_data_confirmed_but_db_wipe_fails_reports_partial_failure(self):
        panel = self._panel()
        mock_box = self._mock_messagebox(1)
        with mock.patch.object(self.sp, "QMessageBox", mock_box), \
             mock.patch.object(self.sp, "db") as mock_db, \
             mock.patch.object(self.sp, "undo_mod") as mock_undo, \
             mock.patch.object(self.sp, "audit_mod") as mock_audit, \
             mock.patch.object(self.sp.os.path, "exists", return_value=False), \
             mock.patch.object(self.sp.os, "remove") as mock_remove:
            mock_db.clear_all.return_value = False
            panel._clear_all_data()
        mock_remove.assert_not_called()           # file didn't exist - never attempted
        mock_box.warning.assert_called_once()
        mock_box.information.assert_not_called()

    def test_clear_all_data_still_wipes_the_db_even_if_trash_or_audit_clearing_raises(self):
        panel = self._panel()
        mock_box = self._mock_messagebox(1)
        with mock.patch.object(self.sp, "QMessageBox", mock_box), \
             mock.patch.object(self.sp, "db") as mock_db, \
             mock.patch.object(self.sp, "undo_mod") as mock_undo, \
             mock.patch.object(self.sp, "audit_mod") as mock_audit, \
             mock.patch.object(self.sp.os.path, "exists", return_value=False):
            mock_undo.trash.empty.side_effect = RuntimeError("trash is on fire")
            mock_db.clear_all.return_value = True
            panel._clear_all_data()   # must not raise, and must still report success
        mock_db.clear_all.assert_called_once()
        mock_box.information.assert_called_once()

    # ---------- Tier 20: Security page ----------
    def test_security_mode_changed_applies_immediately(self):
        panel = self._panel()
        panel.security_mode_combo = mock.Mock()
        panel.security_events_list = mock.Mock()
        with mock.patch.object(self.sp, "security_log") as mock_sl:
            mock_sl.set_mode.return_value = "strict"
            mock_sl.recent.return_value = []
            panel._on_security_mode_changed("strict")
        mock_sl.set_mode.assert_called_once_with("strict")
        panel.security_mode_combo.blockSignals.assert_not_called()

    def test_security_mode_changed_corrects_the_combo_when_the_value_was_rejected(self):
        panel = self._panel()
        panel.security_mode_combo = mock.Mock()
        panel.security_events_list = mock.Mock()
        with mock.patch.object(self.sp, "security_log") as mock_sl:
            mock_sl.set_mode.return_value = "standard"
            mock_sl.recent.return_value = []
            panel._on_security_mode_changed("garbage")
        panel.security_mode_combo.setCurrentText.assert_called_once_with("standard")
        self.assertEqual(panel.security_mode_combo.blockSignals.call_args_list,
                          [mock.call(True), mock.call(False)])

    def test_redact_toggle_sets_the_config_flag(self):
        panel = self._panel()
        with mock.patch.object(self.sp, "config") as mock_config:
            panel._on_redact_toggle(False)
        self.assertEqual(mock_config.REDACT_SECRETS_ENABLED, False)

    def test_refresh_security_events_with_no_events_shows_a_placeholder(self):
        panel = self._panel()
        panel.security_events_list = mock.Mock()
        with mock.patch.object(self.sp, "security_log") as mock_sl:
            mock_sl.recent.return_value = []
            panel._refresh_security_events()
        panel.security_events_list.clear.assert_called_once()
        panel.security_events_list.addItem.assert_called_once_with("No security events yet.")

    def test_refresh_security_events_lists_recent_events(self):
        panel = self._panel()
        panel.security_events_list = mock.Mock()
        fake_event = types.SimpleNamespace(time_text="2:30 PM", kind="guard_deny",
                                            tool="delete_file", reason="denied: tainted T3 tool")
        with mock.patch.object(self.sp, "security_log") as mock_sl:
            mock_sl.recent.return_value = [fake_event]
            panel._refresh_security_events()
        panel.security_events_list.addItem.assert_called_once_with(
            "[2:30 PM] guard_deny · delete_file — denied: tainted T3 tool")

    def test_clear_all_data_also_clears_the_security_log(self):
        panel = self._panel()
        mock_box = self._mock_messagebox(1)
        with mock.patch.object(self.sp, "QMessageBox", mock_box), \
             mock.patch.object(self.sp, "db") as mock_db, \
             mock.patch.object(self.sp, "undo_mod") as mock_undo, \
             mock.patch.object(self.sp, "audit_mod") as mock_audit, \
             mock.patch.object(self.sp, "security_log") as mock_sl, \
             mock.patch.object(self.sp.os.path, "exists", return_value=False):
            mock_db.clear_all.return_value = True
            panel._clear_all_data()
        mock_sl.clear.assert_called_once()

    # ---------- provider test round trip ----------
    def test_test_provider_clicked_success_round_trips_through_on_provider_checked(self):
        panel = self._panel()
        panel.btn_test_provider = mock.Mock()
        panel.provider_status = P4.QLabel()
        panel._main = types.SimpleNamespace(
            worker_pool=P4.SyncPool(),
            brain=types.SimpleNamespace(check_provider=lambda: (True, "Connected.", 17)),
        )
        panel._test_provider_clicked()
        panel.btn_test_provider.setEnabled.assert_any_call(False)
        self.assertEqual(panel.provider_status.text(), "OK: Connected. (17 ms)")
        panel.btn_test_provider.setEnabled.assert_any_call(True)

    def test_test_provider_clicked_exception_is_reported_as_a_problem(self):
        panel = self._panel()
        panel.btn_test_provider = mock.Mock()
        panel.provider_status = P4.QLabel()

        def boom():
            raise ConnectionError("no route to host")

        panel._main = types.SimpleNamespace(
            worker_pool=ImmediatePool(),
            brain=types.SimpleNamespace(check_provider=boom),
        )
        panel._test_provider_clicked()
        self.assertEqual(panel.provider_status.text(), "Problem: no route to host")


if __name__ == "__main__":
    unittest.main()
