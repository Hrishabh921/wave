"""
tests/test_tier15_keyboard_browser.py — Tier 15: system control surface II,
part 1 - keyboard (press_key/type_text/copy_text/paste_text/
select_all_text/undo_keystroke/redo_keystroke), browser control (new_tab/
next_tab/previous_tab/reload_page/zoom_in/zoom_out/zoom_reset/scroll_up/
scroll_down/scroll_to_top/scroll_to_bottom), and clipboard write
(set_clipboard).

Tests AppController-level logic only - the TAINT_DENY_TOOLS/_guard()
interaction (the actual reason this tier's keyboard group has its own
risk-tier exception) lives in test_tier15_taint_guard.py instead, against
a real WaveBrain, the same split VolBrightTestBase/GuardTierEscalationTests
already draw for Tier 14's volume/brightness tools.

Run: python -m unittest discover -s tests -p "test_tier15_keyboard_browser.py"
(pytest will also collect it.)
"""
import sys
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                        # noqa: E402
from automation import backends                                   # noqa: E402
from automation.audit import Audit, MemoryStore                   # noqa: E402
from automation.app_control import AppController, HAS_PYPERCLIP    # noqa: E402
from automation.backends.base import NotSupported, PlatformBackend  # noqa: E402


class _FakeBackend(PlatformBackend):
    """Minimal stand-in - only `name` (for _modifier_key()) and
    get_active_window_title() (for press_key's alt+f4-on-a-system-window
    check) are ever consulted by anything in this file; every other
    keyboard/browser/clipboard tool talks to pyautogui/pyperclip directly,
    never the backend at all (see app_control.py's own Tier 15 section
    comment on why no new backend method was needed for most of these)."""
    display_name = "FakeOS"

    def __init__(self, name="linux", active_title=""):
        self.name = name
        self._active_title = active_title

    def get_active_window_title(self) -> str:
        return self._active_title


class KeyboardBrowserTestBase(unittest.TestCase):
    def setUp(self):
        self.audit = Audit(store=MemoryStore())
        self.saved_audit = audit_mod.audit
        audit_mod.audit = self.audit
        self.addCleanup(self._restore)

    def _restore(self):
        audit_mod.audit = self.saved_audit
        backends.set_backend(None)

    def _use(self, backend):
        backends.set_backend(backend)
        return backend

    def _last(self):
        return self.audit.recent(1)[0]


class PressKeyTests(KeyboardBrowserTestBase):
    def test_ordinary_key_is_sent_and_audited_ok(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.press") as press:
            result = AppController.press_key("enter")
        press.assert_called_once_with("enter")
        self.assertEqual(result, "Sent enter.")
        self.assertEqual(self._last().status, "ok")

    def test_chord_is_sent_via_hotkey_not_press(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey, \
             mock.patch("automation.app_control.pyautogui.press") as press:
            result = AppController.press_key("ctrl+shift+t")
        hotkey.assert_called_once_with("ctrl", "shift", "t")
        press.assert_not_called()
        self.assertEqual(result, "Sent ctrl+shift+t.")

    def test_normalizes_spacing_casing_and_aliases_before_matching(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("Windows + R")
        hotkey.assert_not_called()   # blocked before ever reaching pyautogui
        self.assertIn("win+r", result)

    def test_empty_key_combo_asks_which_key_without_touching_pyautogui(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.press") as press, \
             mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("")
        self.assertEqual(result, "Which key?")
        press.assert_not_called()
        hotkey.assert_not_called()

    def test_win_r_is_always_blocked(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("win+r")
        hotkey.assert_not_called()
        self.assertIn("won't send", result.lower())
        self.assertEqual(self._last().status, "blocked")

    def test_ctrl_alt_delete_variants_are_always_blocked(self):
        self._use(_FakeBackend())
        for combo in ("ctrl+alt+del", "ctrl+alt+delete", "CTRL+ALT+DELETE"):
            with self.subTest(combo=combo):
                with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
                    result = AppController.press_key(combo)
                hotkey.assert_not_called()
                self.assertIn("won't send", result.lower())

    def test_alt_f4_is_sent_normally_when_active_window_is_an_ordinary_app(self):
        self._use(_FakeBackend(active_title="Untitled - Notepad"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("alt+f4")
        hotkey.assert_called_once_with("alt", "f4")
        self.assertEqual(result, "Sent alt+f4.")

    def test_alt_f4_is_sent_normally_when_the_active_title_cant_be_read(self):
        """get_active_window_title() returning '' means "can't tell" (see
        base.py's own contract) - honest uncertainty must never be treated
        as "looks dangerous"."""
        self._use(_FakeBackend(active_title=""))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("alt+f4")
        hotkey.assert_called_once_with("alt", "f4")
        self.assertEqual(result, "Sent alt+f4.")

    def test_alt_f4_is_blocked_on_a_system_looking_window(self):
        self._use(_FakeBackend(active_title="Task Manager"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("alt+f4")
        hotkey.assert_not_called()
        self.assertIn("won't send", result.lower())
        self.assertIn("close window", result.lower())   # points at the safer alternative

    def test_command_w_is_blocked_on_a_system_looking_window_on_macos(self):
        self._use(_FakeBackend(name="macos", active_title="System Settings"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("cmd+w")
        hotkey.assert_not_called()
        self.assertIn("won't send", result.lower())

    def test_get_active_window_title_raising_is_treated_as_cant_tell(self):
        backend = self._use(_FakeBackend(active_title="Task Manager"))
        with mock.patch.object(backend, "get_active_window_title", side_effect=RuntimeError("boom")), \
             mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.press_key("alt+f4")
        hotkey.assert_called_once_with("alt", "f4")
        self.assertEqual(result, "Sent alt+f4.")


class TypeTextTests(KeyboardBrowserTestBase):
    def test_types_the_given_text_and_reports_the_character_count(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.write") as write:
            result = AppController.type_text("hello world")
        write.assert_called_once_with("hello world", interval=0.02)
        self.assertEqual(result, "Typed 11 characters.")

    def test_singular_character_count_wording(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.write"):
            result = AppController.type_text("x")
        self.assertEqual(result, "Typed 1 character.")

    def test_empty_text_asks_what_to_type_without_touching_pyautogui(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.write") as write:
            result = AppController.type_text("")
        self.assertEqual(result, "What should I type?")
        write.assert_not_called()

    def test_audited_by_length_never_by_the_actual_text(self):
        """🔧 Privacy: a password or anything else typed through this tool
        must never sit in plain text in the audit log / list_recent_actions."""
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.write"):
            AppController.type_text("super-secret-password")
        row = self._last()
        self.assertEqual(row.args, {"length": len("super-secret-password")})
        self.assertNotIn("super-secret-password", str(row.args))


class ModifierKeyTests(KeyboardBrowserTestBase):
    def test_is_command_on_macos(self):
        self._use(_FakeBackend(name="macos"))
        self.assertEqual(AppController._modifier_key(), "command")

    def test_is_ctrl_on_windows_and_linux(self):
        for os_name in ("windows", "linux"):
            with self.subTest(os_name=os_name):
                self._use(_FakeBackend(name=os_name))
                self.assertEqual(AppController._modifier_key(), "ctrl")


class KeyboardChordToolsTests(KeyboardBrowserTestBase):
    """copy_text/paste_text/select_all_text/undo_keystroke/redo_keystroke -
    each just a _send_hotkey() wrapper, so one parametrized shape per tool
    plus redo's own mac-specific chord."""

    def test_copy_paste_select_all_undo_use_ctrl_on_windows_linux(self):
        self._use(_FakeBackend(name="linux"))
        expectations = {
            AppController.copy_text: ("ctrl", "c"),
            AppController.paste_text: ("ctrl", "v"),
            AppController.select_all_text: ("ctrl", "a"),
            AppController.undo_keystroke: ("ctrl", "z"),
        }
        for method, keys in expectations.items():
            with self.subTest(method=method.__name__):
                with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
                    method()
                hotkey.assert_called_once_with(*keys)

    def test_copy_paste_select_all_undo_use_command_on_macos(self):
        self._use(_FakeBackend(name="macos"))
        expectations = {
            AppController.copy_text: ("command", "c"),
            AppController.paste_text: ("command", "v"),
            AppController.select_all_text: ("command", "a"),
            AppController.undo_keystroke: ("command", "z"),
        }
        for method, keys in expectations.items():
            with self.subTest(method=method.__name__):
                with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
                    method()
                hotkey.assert_called_once_with(*keys)

    def test_redo_uses_ctrl_y_on_windows_linux(self):
        self._use(_FakeBackend(name="windows"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            AppController.redo_keystroke()
        hotkey.assert_called_once_with("ctrl", "y")

    def test_redo_uses_command_shift_z_on_macos(self):
        self._use(_FakeBackend(name="macos"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            AppController.redo_keystroke()
        hotkey.assert_called_once_with("command", "shift", "z")

    def test_each_is_audited_under_its_own_tool_name_and_ok(self):
        self._use(_FakeBackend())
        for method, tool in ((AppController.copy_text, "copy_text"), (AppController.paste_text, "paste_text"),
                              (AppController.select_all_text, "select_all_text"),
                              (AppController.undo_keystroke, "undo_keystroke"),
                              (AppController.redo_keystroke, "redo_keystroke")):
            with self.subTest(tool=tool):
                with mock.patch("automation.app_control.pyautogui.hotkey"):
                    method()
                row = self._last()
                self.assertEqual((row.tool, row.status), (tool, "ok"))

    def test_hotkey_failure_is_reported_and_audited_as_failed(self):
        self._use(_FakeBackend())
        with mock.patch("automation.app_control.pyautogui.hotkey", side_effect=RuntimeError("no display")):
            result = AppController.copy_text()
        self.assertEqual(result, "Couldn't copy.")
        self.assertEqual(self._last().status, "failed")


class BrowserControlTests(KeyboardBrowserTestBase):
    def test_new_tab_uses_the_platform_modifier(self):
        self._use(_FakeBackend(name="linux"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            result = AppController.new_tab()
        hotkey.assert_called_once_with("ctrl", "t")
        self.assertEqual(result, "Opened a new tab.")

        self._use(_FakeBackend(name="macos"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            AppController.new_tab()
        hotkey.assert_called_once_with("command", "t")

    def test_next_and_previous_tab_always_use_plain_ctrl_even_on_macos(self):
        """Ctrl+Tab/Ctrl+Shift+Tab are the browser's OWN shortcut on every
        platform including macOS - not the OS app-switcher (Cmd+Tab), so
        these two deliberately don't call _modifier_key() at all."""
        self._use(_FakeBackend(name="macos"))
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            AppController.next_tab()
        hotkey.assert_called_once_with("ctrl", "tab")
        with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            AppController.previous_tab()
        hotkey.assert_called_once_with("ctrl", "shift", "tab")

    def test_reload_zoom_use_the_platform_modifier(self):
        self._use(_FakeBackend(name="macos"))
        cases = {
            AppController.reload_page: ("command", "r"),
            AppController.zoom_in: ("command", "+"),
            AppController.zoom_out: ("command", "-"),
            AppController.zoom_reset: ("command", "0"),
        }
        for method, keys in cases.items():
            with self.subTest(method=method.__name__):
                with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
                    method()
                hotkey.assert_called_once_with(*keys)

    def test_scroll_tools_send_plain_named_keys(self):
        self._use(_FakeBackend())
        cases = {
            AppController.scroll_up: ("pageup",), AppController.scroll_down: ("pagedown",),
            AppController.scroll_to_top: ("home",), AppController.scroll_to_bottom: ("end",),
        }
        for method, keys in cases.items():
            with self.subTest(method=method.__name__):
                with mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
                    result = method()
                hotkey.assert_called_once_with(*keys)
                self.assertNotIn("couldn't", result.lower())

    def test_every_browser_tool_is_audited_under_its_own_name(self):
        self._use(_FakeBackend())
        tools = {
            "new_tab": AppController.new_tab, "next_tab": AppController.next_tab,
            "previous_tab": AppController.previous_tab, "reload_page": AppController.reload_page,
            "zoom_in": AppController.zoom_in, "zoom_out": AppController.zoom_out,
            "zoom_reset": AppController.zoom_reset, "scroll_up": AppController.scroll_up,
            "scroll_down": AppController.scroll_down, "scroll_to_top": AppController.scroll_to_top,
            "scroll_to_bottom": AppController.scroll_to_bottom,
        }
        for name, method in tools.items():
            with self.subTest(tool=name):
                with mock.patch("automation.app_control.pyautogui.hotkey"):
                    method()
                self.assertEqual(self._last().tool, name)


@unittest.skipUnless(HAS_PYPERCLIP, "pyperclip not installed")
class SetClipboardTests(KeyboardBrowserTestBase):
    def test_sets_the_clipboard_and_confirms(self):
        with mock.patch("automation.app_control.pyperclip.copy") as copy:
            result = AppController.set_clipboard("hello there")
        copy.assert_called_once_with("hello there")
        self.assertEqual(result, "Copied that to the clipboard.")

    def test_audited_by_length_never_by_the_actual_text(self):
        with mock.patch("automation.app_control.pyperclip.copy"):
            AppController.set_clipboard("my-bank-pin-is-1234")
        row = self._last()
        self.assertEqual(row.args, {"length": len("my-bank-pin-is-1234")})
        self.assertNotIn("1234", str(row.args))

    def test_none_text_is_treated_as_empty_not_a_crash(self):
        with mock.patch("automation.app_control.pyperclip.copy") as copy:
            result = AppController.set_clipboard(None)
        copy.assert_called_once_with("")
        self.assertEqual(result, "Copied that to the clipboard.")

    def test_copy_failure_is_reported_and_audited_as_failed(self):
        with mock.patch("automation.app_control.pyperclip.copy", side_effect=RuntimeError("no clipboard")):
            result = AppController.set_clipboard("x")
        self.assertEqual(result, "Couldn't set the clipboard.")
        self.assertEqual(self._last().status, "failed")


class PyperclipMissingTests(unittest.TestCase):
    """Separate from KeyboardBrowserTestBase - patches the HAS_PYPERCLIP
    flag itself rather than needing the real package absent, same
    dependency-missing style brain/handlers/system.py's own
    get_clipboard already uses/tests for the read side."""

    def test_set_clipboard_reports_the_missing_dependency_without_raising(self):
        with mock.patch("automation.app_control.HAS_PYPERCLIP", False):
            result = AppController.set_clipboard("hello")
        self.assertIn("pyperclip", result.lower())


if __name__ == "__main__":
    unittest.main()
