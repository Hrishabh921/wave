"""Tier 21 item: fixing a real pre-existing bug in ui/onboarding.py.

OnboardingWizard's Wake Word step (self.wake_combo) and Permissions step
(self.perm_clipboard/perm_screen/perm_notifications) used to be read
nowhere - _go_next()'s last-step branch had a bare "# Save preferences"
comment with no code after it. Picking a wake phrase or toggling a
permission during first-run setup silently did nothing: the values
vanished the moment the dialog closed, because the REAL settings
persistence (ui/main_window.py's save_settings()/load_settings()) only
ever reads the Settings page's own separate widget instances
(settings_panel.wake_combo, settings_panel.cb_clipboard, ...) - entirely
different objects from the wizard's.

This file tests the fix: OnboardingWizard._save_preferences(), which
pushes the wizard's chosen values onto the Settings page's equivalent
widgets (firing the same change handlers the Settings page itself relies
on to apply each choice immediately) and then calls the real
save_settings() to persist them - rather than inventing a second,
parallel persistence path.

Reuses test_tier9_onboarding.py's stand-in / import machinery.
"""
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
from test_tier9_onboarding import import_with_fakes


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class SavePreferencesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.ob = import_with_fakes("ui.onboarding")

    def _wizard(self, parent):
        w = object.__new__(self.ob.OnboardingWizard)
        w._parent = parent
        w.parent = lambda: w._parent
        return w

    def _fake_settings_panel(self):
        sp = types.SimpleNamespace()
        sp.WAKE_WORD_PRESETS = {"Hey Wave": ["hey wave"], "Hello Wave": ["hello wave"]}
        sp.wake_combo = mock.Mock()
        sp.cb_clipboard = mock.Mock()
        sp.cb_screen = mock.Mock()
        sp.cb_notifications = mock.Mock()
        sp._refresh_wake_status = mock.Mock()
        return sp

    def test_missing_settings_panel_is_a_no_op_not_a_crash(self):
        w = self._wizard(parent=types.SimpleNamespace())   # no settings_panel attr
        w._save_preferences()   # must not raise

    def test_none_parent_is_a_no_op_not_a_crash(self):
        w = self._wizard(parent=None)
        w._save_preferences()   # must not raise

    def test_preset_wake_word_is_pushed_onto_the_settings_panel_combo(self):
        sp = self._fake_settings_panel()
        win = types.SimpleNamespace(settings_panel=sp, save_settings=mock.Mock())
        w = self._wizard(parent=win)
        w.wake_combo = types.SimpleNamespace(currentText=lambda: "Hello Wave")
        w._save_preferences()
        sp.wake_combo.setCurrentText.assert_called_once_with("Hello Wave")
        win.save_settings.assert_called_once()

    def test_custom_wake_word_prompts_and_applies_directly(self):
        sp = self._fake_settings_panel()
        win = types.SimpleNamespace(settings_panel=sp, save_settings=mock.Mock(), mic_worker=mock.Mock())
        w = self._wizard(parent=win)
        w.wake_combo = types.SimpleNamespace(currentText=lambda: "Custom...")
        with mock.patch.object(self.ob.QInputDialog, "getText", return_value=("Open Sesame", True)):
            w._save_preferences()
        self.assertEqual(win.wake_phrases, ["open sesame", "wake up"])
        win.mic_worker.set_wake_phrases.assert_called_once_with(["open sesame", "wake up"])
        sp._refresh_wake_status.assert_called_once()
        # Custom path is applied directly - never routed through
        # sp.wake_combo.setCurrentText (that would pop a SECOND dialog via
        # sp's own _on_wake_word_change).
        sp.wake_combo.setCurrentText.assert_not_called()
        win.save_settings.assert_called_once()

    def test_custom_wake_word_cancelled_falls_back_to_hey_wave(self):
        sp = self._fake_settings_panel()
        win = types.SimpleNamespace(settings_panel=sp, save_settings=mock.Mock(), mic_worker=None)
        w = self._wizard(parent=win)
        w.wake_combo = types.SimpleNamespace(currentText=lambda: "Custom...")
        with mock.patch.object(self.ob.QInputDialog, "getText", return_value=("", False)):
            w._save_preferences()
        self.assertEqual(win.wake_phrases, ["hey wave", "wake up"])

    def test_permission_checkboxes_are_pushed_onto_the_settings_panel(self):
        sp = self._fake_settings_panel()
        win = types.SimpleNamespace(settings_panel=sp, save_settings=mock.Mock())
        w = self._wizard(parent=win)
        w.perm_clipboard = types.SimpleNamespace(isChecked=lambda: False)
        w.perm_screen = types.SimpleNamespace(isChecked=lambda: True)
        w.perm_notifications = types.SimpleNamespace(isChecked=lambda: False)
        w._save_preferences()
        sp.cb_clipboard.setChecked.assert_called_once_with(False)
        sp.cb_screen.setChecked.assert_called_once_with(True)
        sp.cb_notifications.setChecked.assert_called_once_with(False)
        win.save_settings.assert_called_once()

    def test_go_next_on_finish_calls_save_preferences_before_accept(self):
        """Integration with _go_next: the fix must actually be wired in,
        not just exist as a dead method nobody calls."""
        w = self._wizard(parent=types.SimpleNamespace())
        w._current_step = 6
        w._steps = [("x", "y", "z")] * 7
        w._show_step = mock.Mock()
        w._save_preferences = mock.Mock()
        w._on_complete = mock.Mock()
        w.accept = mock.Mock()
        w._go_next()
        w._save_preferences.assert_called_once()
        w.accept.assert_called_once()
        w._on_complete.assert_called_once()


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class NotificationToggleGatesToastTests(unittest.TestCase):
    """The "Desktop notifications" checkbox (onboarding Permissions step /
    Settings > Privacy page) needs to actually gate something real. WAVE has
    no OS-level desktop notifications anywhere in the codebase, so it gates
    the one real notification surface that exists: ToastManager."""

    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        P4.install_stub()
        cls.toast_mod = import_with_fakes("ui.toast")

    def test_toast_manager_defaults_to_enabled(self):
        tm = self.toast_mod.ToastManager.__new__(self.toast_mod.ToastManager)
        tm._toasts = []
        tm.enabled = True
        self.assertTrue(tm.enabled)

    def test_disabled_toast_manager_shows_nothing(self):
        tm = self.toast_mod.ToastManager.__new__(self.toast_mod.ToastManager)
        tm._toasts = []
        tm.enabled = False
        result = tm.show("hello")
        self.assertIsNone(result)
        self.assertEqual(tm._toasts, [])


if __name__ == "__main__":
    unittest.main()
