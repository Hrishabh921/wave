"""Tier 9 - logic tests for ui/theme.py (ThemeColors, ThemeManager), WITHOUT
a display or PyQt6.

ui/theme.py was split out of the original ui/main_window.py (see
ui/common.py's own docstring) along with a dozen other focused files - but
unlike most of those, nothing in test_phase4_ui_logic.py or
test_tier5_ui_logic.py ever touches it. This closes that gap: ThemeColors'
dict round-trip and ThemeManager's theme switching / custom-theme
persistence / last-theme-remembered logic, all real Python logic that
doesn't depend on how anything paints.

Reuses test_phase4_ui_logic.py's stand-in (install_stub()) rather than
duplicating it - ui/theme.py only needs QObject + pyqtSignal out of that
stand-in, nothing list/line-edit-specific, so test_tier5_ui_logic.py's
extra widgets aren't needed here.

As with those two files: if real PyQt6 is installed, these tests skip
themselves (see REAL_QT below), since ThemeManager(QObject) would then be a
real QObject requiring a real Qt application.
"""
import json
import os
import sys
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

# Reuse the Phase 4 stand-in's building blocks instead of redefining them.
import test_phase4_ui_logic as P4


def import_with_fakes(module_name):
    """Same retry loop test_phase4_ui_logic.py/test_tier5_ui_logic.py use:
    import module_name, and for each ModuleNotFoundError bubbled up from a
    package that isn't installed here, install a generic stand-in for just
    that package and retry. Evicts cached ui.* modules first so this file's
    install_stub() (freshly called in setUpClass) is what ui.theme's
    `from ui.common import *` actually binds against, not a stale copy left
    over from another test file run earlier in the same process."""
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


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ThemeColorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.theme = import_with_fakes("ui.theme")

    def test_to_dict_contains_every_field_with_its_current_value(self):
        t = self.theme.ThemeColors(name="Custom", accent_1="#ABCDEF")
        d = t.to_dict()
        self.assertEqual(d["name"], "Custom")
        self.assertEqual(d["accent_1"], "#ABCDEF")
        self.assertEqual(set(d.keys()), set(t.__annotations__.keys()))

    def test_from_dict_ignores_unknown_keys_instead_of_raising(self):
        t = self.theme.ThemeColors.from_dict({"name": "X", "accent_1": "#111111", "totally_unknown_field": 42})
        self.assertEqual(t.name, "X")
        self.assertEqual(t.accent_1, "#111111")
        self.assertFalse(hasattr(t, "totally_unknown_field"))

    def test_from_dict_with_a_partial_dict_fills_the_rest_from_defaults(self):
        t = self.theme.ThemeColors.from_dict({"name": "Partial"})
        default = self.theme.ThemeColors()
        self.assertEqual(t.name, "Partial")
        self.assertEqual(t.accent_1, default.accent_1)          # untouched field keeps its dataclass default
        self.assertEqual(t.page_bg_alpha, default.page_bg_alpha)

    def test_to_dict_then_from_dict_round_trips_to_an_equal_theme(self):
        original = self.theme.ThemeColors(name="Round Trip", accent_2="#222222", page_bg_alpha=0.42)
        rebuilt = self.theme.ThemeColors.from_dict(original.to_dict())
        self.assertEqual(original, rebuilt)                     # dataclass __eq__ compares every field


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ThemeManagerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.theme = import_with_fakes("ui.theme")

    def setUp(self):
        tmp = S.make_tmpdir(self)
        self.themes_dir = os.path.join(tmp, "themes")
        self.settings_file = os.path.join(tmp, "wave_settings.json")
        self.dir_patch = mock.patch.object(self.theme, "THEMES_DIR", self.themes_dir)
        self.file_patch = mock.patch.object(self.theme, "SETTINGS_FILE", self.settings_file)
        self.dir_patch.start()
        self.file_patch.start()
        self.addCleanup(self.dir_patch.stop)
        self.addCleanup(self.file_patch.stop)

    # ---------- built-ins ----------
    def test_five_builtin_themes_are_registered_and_distinct(self):
        # Tier 21 expanded the built-in roster from 5 (all dark) to 11,
        # including 4 light themes - see test_tier21_theme_tokens.py for
        # dedicated coverage of the new roster (light/dark balance, token
        # overrides, etc). This test keeps its original name/intent: the
        # original five are still present, unchanged, and still distinct.
        mgr = self.theme.ThemeManager()
        names = mgr.get_theme_names()
        original_five = {"Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark"}
        self.assertTrue(original_five.issubset(set(names)))
        self.assertGreaterEqual(len(names), 10)
        # Not just names pointing at the same colors - each preset's accent differs.
        accents = {mgr.get_theme(n).accent_1 for n in names}
        self.assertEqual(len(accents), len(names))

    def test_current_theme_defaults_to_black_glass_before_any_set_theme_call(self):
        mgr = self.theme.ThemeManager()
        self.assertEqual(mgr.get_current().name, "Black Glass")

    # ---------- set_theme ----------
    def test_set_theme_switches_current_emits_signal_and_persists(self):
        mgr = self.theme.ThemeManager()
        seen = []
        mgr.theme_changed.connect(lambda t: seen.append(t.name))
        mgr.set_theme("Midnight Blue")
        self.assertEqual(mgr.get_current().name, "Midnight Blue")
        self.assertEqual(seen, ["Midnight Blue"])
        self.assertEqual(mgr.get_last_theme(), "Midnight Blue")

    def test_set_theme_with_an_unknown_name_is_a_silent_no_op(self):
        mgr = self.theme.ThemeManager()
        seen = []
        mgr.theme_changed.connect(lambda t: seen.append(t.name))
        mgr.set_theme("Not A Real Theme")
        self.assertEqual(mgr.get_current().name, "Black Glass")  # unchanged
        self.assertEqual(seen, [])                                # never emitted
        # Never persisted either - get_last_theme() falls back since nothing was ever saved.
        self.assertEqual(mgr.get_last_theme(), "Black Glass")

    # ---------- custom themes ----------
    def test_save_custom_theme_is_immediately_available_and_written_to_disk(self):
        mgr = self.theme.ThemeManager()
        custom = self.theme.ThemeColors(name="My Custom", accent_1="#123456")
        mgr.save_custom_theme(custom)
        self.assertIn("My Custom", mgr.get_theme_names())
        self.assertIs(mgr.get_theme("My Custom"), custom)
        path = os.path.join(self.themes_dir, "My Custom.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            on_disk = self.theme.ThemeColors.from_dict(json.load(f))
        self.assertEqual(on_disk, custom)

    def test_a_saved_custom_theme_is_loaded_back_by_a_fresh_manager(self):
        os.makedirs(self.themes_dir, exist_ok=True)
        saved = self.theme.ThemeColors(name="Reloaded", accent_1="#ABABAB")
        with open(os.path.join(self.themes_dir, "Reloaded.json"), "w") as f:
            json.dump(saved.to_dict(), f)
        mgr = self.theme.ThemeManager()                         # fresh instance -> _load_custom_themes() runs
        self.assertIn("Reloaded", mgr.get_theme_names())
        self.assertEqual(mgr.get_theme("Reloaded"), saved)

    def test_a_corrupt_custom_theme_file_is_skipped_without_crashing_construction(self):
        os.makedirs(self.themes_dir, exist_ok=True)
        with open(os.path.join(self.themes_dir, "broken.json"), "w") as f:
            f.write("{not valid json")
        mgr = self.theme.ThemeManager()                         # must not raise
        # Only the builtins made it in (11 since Tier 21 - see
        # test_tier21_theme_tokens.py) - the broken file was skipped, not
        # half-applied.
        self.assertEqual(len(mgr.get_theme_names()), 11)

    # ---------- last-theme persistence ----------
    def test_get_last_theme_with_no_settings_file_yet_falls_back_to_black_glass(self):
        mgr = self.theme.ThemeManager()
        self.assertEqual(mgr.get_last_theme(), "Black Glass")

    def test_get_last_theme_with_a_corrupt_settings_file_falls_back_too(self):
        with open(self.settings_file, "w") as f:
            f.write("not json at all")
        mgr = self.theme.ThemeManager()
        self.assertEqual(mgr.get_last_theme(), "Black Glass")

    def test_save_last_theme_merges_into_the_settings_file_rather_than_overwriting_it(self):
        with open(self.settings_file, "w") as f:
            json.dump({"cb_autostart": True}, f)
        mgr = self.theme.ThemeManager()
        mgr.save_last_theme("Emerald Dark")
        with open(self.settings_file) as f:
            data = json.load(f)
        self.assertEqual(data["cb_autostart"], True)             # pre-existing key survived
        self.assertEqual(data["last_theme"], "Emerald Dark")


if __name__ == "__main__":
    unittest.main()
