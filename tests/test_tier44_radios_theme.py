"""
tests/test_tier44_radios_theme.py — Tier 44: Wi-Fi, Bluetooth and the theme, switched for real.

SARA turns Wi-Fi / Bluetooth on and off and switches light/dark mode itself.
Wave only opened the Settings page - and its "toggle" pressed Space on that
page blindly, so "wifi on karo" turned an already-on Wi-Fi OFF. Now the
radios go through Windows' radio API (winsdk) and the theme through the
"Choose your mode" registry values, with undo; turning a radio off asks first.
Nothing real is switched here - a stand-in backend answers.

    python -m unittest discover -s tests -p "test_tier44_radios_theme.py"
"""
import sys
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from automation import app_control  # noqa: E402
from automation.app_control import AppController  # noqa: E402
from automation.backends.base import NotSupported  # noqa: E402
from brain import intent_router, tiers  # noqa: E402
from brain import confirm_gate  # noqa: E402


class _Backend:
    def __init__(self, wifi=True, bluetooth=False, dark=False, fail=False):
        self.state = {"wifi": wifi, "bluetooth": bluetooth}
        self.dark = dark
        self.fail = fail
        self.calls = []

    def get_radio(self, kind):
        return self.state[kind]

    def set_radio(self, kind, on):
        if self.fail:
            raise NotSupported("no radio here")
        self.calls.append((kind, on))
        was, self.state[kind] = self.state[kind], on
        label = "Wi-Fi" if kind == "wifi" else "Bluetooth"
        return f"{label} is already {'on' if on else 'off'}." if was == on else f"{label} is now {'on' if on else 'off'}."

    def get_dark_mode(self):
        return self.dark

    def set_dark_mode(self, on):
        self.calls.append(("dark", on))
        self.dark = on
        return f"Switched to {'dark' if on else 'light'} mode."


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()
        p = mock.patch.object(AppController, "_backend", classmethod(lambda cls: self.backend))
        p.start()
        self.addCleanup(p.stop)
        from settings.config import config  # noqa: PLC0415
        p3 = mock.patch.object(config, "SYSTEM_TOGGLES", True)    # the stand-in backend, never a real radio
        p3.start()
        self.addCleanup(p3.stop)
        self.undo = []
        audit = app_control._audit()
        original = audit.action

        def action(tool, args):
            ctx = original(tool, args)
            real_set_undo = ctx.set_undo

            def set_undo(kind, payload, label):
                self.undo.append((kind, payload))
                return real_set_undo(kind, payload, label)
            ctx.set_undo = set_undo
            return ctx
        p2 = mock.patch.object(audit, "action", side_effect=action)
        p2.start()
        self.addCleanup(p2.stop)

    def test_turning_a_radio_on_and_off_is_real_and_undoable(self):
        self.assertEqual(AppController.set_radio("bluetooth", True), "Bluetooth is now on.")
        self.assertEqual(self.undo[-1], ("restore_radio", {"kind": "bluetooth", "on": False}))
        self.assertEqual(AppController.set_radio("wifi", False), "Wi-Fi is now off.")
        self.assertEqual(self.undo[-1], ("restore_radio", {"kind": "wifi", "on": True}))

    def test_on_when_already_on_stays_on(self):
        """The old blind toggle turned an already-on Wi-Fi off."""
        self.assertEqual(AppController.set_radio("wifi", True), "Wi-Fi is already on.")
        self.assertTrue(self.backend.state["wifi"])
        self.assertEqual(self.undo, [])

    def test_the_theme(self):
        self.assertEqual(AppController.set_dark_mode(True), "Switched to dark mode.")
        self.assertEqual(self.undo[-1], ("restore_theme", {"dark": False}))
        self.assertEqual(AppController.set_dark_mode(True), "It's already in dark mode.")

    def test_a_machine_without_the_radio_says_so(self):
        self.backend.fail = True
        self.assertIn("no radio here", AppController.set_radio("wifi", True))

    def test_undo_puts_the_radio_and_theme_back(self):
        with mock.patch.object(app_control, "get_backend", return_value=self.backend):
            ok, _ = app_control._undo_restore_radio({"kind": "wifi", "on": True})
            self.assertTrue(ok)
            ok, _ = app_control._undo_restore_theme({"dark": True})
            self.assertTrue(ok)
        self.assertIn(("wifi", True), self.backend.calls)
        self.assertIn(("dark", True), self.backend.calls)

    def test_switched_off_means_nothing_is_switched(self):
        from settings.config import config  # noqa: PLC0415
        with mock.patch.object(config, "SYSTEM_TOGGLES", False):
            self.assertIn("SYSTEM_TOGGLES=false", AppController.set_radio("wifi", False))
        self.assertTrue(self.backend.state["wifi"])

    def test_toggle_hardware_never_blind_toggles_a_radio_any_more(self):
        self.assertEqual(AppController.toggle_hardware("turn on bluetooth"), "Bluetooth is now on.")
        message = AppController.toggle_hardware("turn off wifi")
        self.assertIn("I'll check with you first", message)
        self.assertTrue(self.backend.state["wifi"])                  # not switched from here


class SafetyTests(unittest.TestCase):
    def test_turning_a_radio_off_asks_first(self):
        self.assertEqual(tiers.TIERS["turn_wifi_off"], tiers.T2)
        self.assertEqual(tiers.TIERS["turn_bluetooth_off"], tiers.T2)
        for tool in ("turn_wifi_on", "turn_bluetooth_on", "switch_to_dark_mode", "switch_to_light_mode"):
            self.assertEqual(tiers.TIERS[tool], tiers.T1, tool)
        self.assertIn("offline", confirm_gate.describe_action("turn_wifi_off", {})
                      if hasattr(confirm_gate, "describe_action") else confirm_gate._FRIENDLY_ACTIONS["turn_wifi_off"]({}))


class RouterTests(unittest.TestCase):
    def _route(self, text):
        r = intent_router.route(text)
        return (r.skill, r.args) if r else None

    def test_phrasings(self):
        cases = {
            "wifi band karo": ("turn_wifi_off", {}), "turn on wifi": ("turn_wifi_on", {}),
            "enable bluetooth": ("turn_bluetooth_on", {}), "ब्लूटूथ बंद करो": ("turn_bluetooth_off", {}),
            "वाईफाई चालू करो": ("turn_wifi_on", {}), "dark mode": ("switch_to_dark_mode", {}),
            "dark mode lagao": ("switch_to_dark_mode", {}), "dark mode band karo": ("switch_to_light_mode", {}),
            "switch to light mode": ("switch_to_light_mode", {}),
            "switch on the hotspot": ("toggle_hardware", {"instruction": "switch on the hotspot"}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self._route(text), expected)

    def test_mentions_and_both_radios_go_to_the_model(self):
        for text in ("what is dark mode", "is dark mode better for the eyes", "turn off wifi and bluetooth",
                     "wifi is great"):
            with self.subTest(text=text):
                self.assertIsNone(self._route(text))


@unittest.skipUnless(sys.platform == "win32", "the Windows backend")
class WindowsThemeTests(unittest.TestCase):
    def test_dark_mode_writes_both_registry_values(self):
        from automation.backends import windows  # noqa: PLC0415
        writes = []

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(windows.winreg, "OpenKey", return_value=_Key()), \
                mock.patch.object(windows.winreg, "SetValueEx", side_effect=lambda k, n, r, t, v: writes.append((n, v))), \
                mock.patch("ctypes.windll.user32.SendMessageTimeoutW") as broadcast:
            msg = windows.WindowsBackend().set_dark_mode(True)
        self.assertEqual(msg, "Switched to dark mode.")
        self.assertEqual(sorted(writes), [("AppsUseLightTheme", 0), ("SystemUsesLightTheme", 0)])
        broadcast.assert_called_once()

    def test_the_radio_api_answer_becomes_a_sentence(self):
        from automation.backends import windows  # noqa: PLC0415
        with mock.patch.object(windows, "_radio_call",
                               return_value={"found": True, "was_on": False, "now_on": True, "error": ""}):
            self.assertEqual(windows.WindowsBackend().set_radio("wifi", True), "Wi-Fi is now on.")
        with mock.patch.object(windows, "_radio_call",
                               return_value={"found": False, "was_on": None, "now_on": None,
                                             "error": "this PC has no Bluetooth radio"}):
            with self.assertRaises(NotSupported):
                windows.WindowsBackend().set_radio("bluetooth", True)


if __name__ == "__main__":
    unittest.main()
