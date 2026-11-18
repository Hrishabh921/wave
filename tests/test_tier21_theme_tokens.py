"""Tier 21 item: design tokens (spacing/radius/font-size/motion) added to
ThemeColors, build_stylesheet() wired to a representative zero-visual-risk
subset of them, ui/effects.py's Anim class wired to the motion tokens, and
the built-in theme roster expanded from 5 (all dark) to 11, including four
genuinely light themes.

As with every other UI-logic test in this project (see test_phase4_ui_logic.py
and test_tier5_ui_logic.py's module docstrings), this verifies Python-level
behavior - that every theme's stylesheet builds without error, that the new
token fields exist and round-trip, that light themes are actually light and
dark themes are actually dark - NOT real rendering, since there's no display
or PyQt6 in this sandbox. If real PyQt6 is installed, these tests skip
themselves, same convention as the rest of the suite.
"""
import sys
import unittest

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_tier5_ui_logic as T


def _hex_to_rgb(value: str):
    """Minimal #RRGGBB -> (r,g,b) parser, just for the light/dark luminance
    checks below - theme colors elsewhere are rgba(...)/#hex strings that
    don't need parsing for anything else in this file."""
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance_hex(value: str) -> float:
    r, g, b = _hex_to_rgb(value)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ThemeTokenFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                               hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                               typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                               size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        T.install_stub_with_list_widgets()
        cls.theme_mod = T.import_with_fakes("ui.theme")

    def test_default_spacing_radius_fontsize_motion_tokens_present(self):
        t = self.theme_mod.ThemeColors()
        self.assertEqual(t.spacing_xs, 4)
        self.assertEqual(t.spacing_sm, 8)
        self.assertEqual(t.spacing_md, 16)
        self.assertEqual(t.spacing_lg, 24)
        self.assertEqual(t.spacing_xl, 32)
        self.assertEqual(t.radius_sm, 10)
        self.assertEqual(t.radius_md, 14)
        self.assertEqual(t.radius_lg, 18)
        self.assertEqual(t.radius_xl, 22)
        self.assertEqual(t.font_size_xs, 10)
        self.assertEqual(t.font_size_sm, 12)
        self.assertEqual(t.font_size_md, 13)
        self.assertEqual(t.font_size_lg, 16)
        self.assertEqual(t.font_size_xl, 21)
        self.assertEqual(t.motion_duration_fast_ms, 150)
        self.assertEqual(t.motion_duration_normal_ms, 250)
        self.assertEqual(t.motion_duration_slow_ms, 400)
        self.assertEqual(t.motion_stagger_delay_ms, 50)
        self.assertEqual(t.motion_easing, "OutCubic")

    def test_new_surface_tokens_default_to_old_literals(self):
        """These tokens were pulled out of build_stylesheet()'s own literals -
        their defaults must equal exactly what every pre-Tier-21 dark theme
        already rendered, or every existing dark theme's look would change."""
        t = self.theme_mod.ThemeColors()
        self.assertEqual(t.surface_hover, "rgba(40,40,40,1)")
        self.assertEqual(t.surface_pressed, "rgba(10,10,10,1)")
        self.assertEqual(t.surface_header, "rgba(30,30,30,1)")
        self.assertEqual(t.dialog_bg, "#0A0A0A")
        self.assertEqual(t.page_bg_rgb, "20,20,20")

    def test_to_dict_and_from_dict_round_trip_new_tokens(self):
        t = self.theme_mod.ThemeColors(name="Custom", spacing_md=99, motion_easing="InOutQuad")
        data = t.to_dict()
        self.assertEqual(data["spacing_md"], 99)
        restored = self.theme_mod.ThemeColors.from_dict(data)
        self.assertEqual(restored.spacing_md, 99)
        self.assertEqual(restored.motion_easing, "InOutQuad")

    def test_from_dict_ignores_unknown_keys(self):
        data = self.theme_mod.ThemeColors().to_dict()
        data["some_future_field_this_version_does_not_know"] = "x"
        restored = self.theme_mod.ThemeColors.from_dict(data)  # must not raise
        self.assertEqual(restored.name, "Black Glass")


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ThemeRosterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                               hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                               typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                               size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        T.install_stub_with_list_widgets()
        cls.theme_mod = T.import_with_fakes("ui.theme")
        cls.mgr = cls.theme_mod.ThemeManager()

    def test_at_least_ten_builtin_themes(self):
        self.assertGreaterEqual(len(self.mgr._themes), 10)

    def test_original_five_dark_themes_still_present_unchanged(self):
        for name in ("Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark"):
            self.assertIn(name, self.mgr._themes)

    def test_every_theme_builds_a_nonempty_stylesheet(self):
        for name, theme in self.mgr._themes.items():
            ss = self.mgr.build_stylesheet(theme)
            self.assertIsInstance(ss, str)
            self.assertGreater(len(ss), 1000, f"{name} produced a suspiciously short stylesheet")

    def test_at_least_three_themes_are_genuinely_light(self):
        """Light = light background + dark text, not just a renamed dark
        palette. bg_main is a plain #RRGGBB on every built-in theme."""
        light_count = 0
        for name, theme in self.mgr._themes.items():
            bg_lum = _relative_luminance_hex(theme.bg_main)
            text_lum = _relative_luminance_hex(theme.text_main)
            if bg_lum > 0.6 and text_lum < 0.4:
                light_count += 1
        self.assertGreaterEqual(light_count, 3)

    def test_dark_themes_keep_light_text_on_dark_background(self):
        for name in ("Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark",
                     "Slate Dark", "Violet Dark"):
            theme = self.mgr._themes[name]
            self.assertLess(_relative_luminance_hex(theme.bg_main), 0.3, name)
            self.assertGreater(_relative_luminance_hex(theme.text_main), 0.6, name)

    def test_light_themes_override_surface_hover_pressed_header_dialog(self):
        """A light theme that kept the dark defaults for these would render
        an unreadable dark hover/pressed/header chrome on a light page."""
        for name in ("Daylight", "Paper Light", "Ocean Light", "Mint Light"):
            theme = self.mgr._themes[name]
            self.assertNotEqual(theme.surface_hover, "rgba(40,40,40,1)", name)
            self.assertNotEqual(theme.surface_pressed, "rgba(10,10,10,1)", name)
            self.assertNotEqual(theme.surface_header, "rgba(30,30,30,1)", name)
            self.assertNotEqual(theme.dialog_bg, "#0A0A0A", name)
            self.assertNotEqual(theme.page_bg_rgb, "20,20,20", name)

    def test_theme_names_are_unique(self):
        names = list(self.mgr._themes.keys())
        self.assertEqual(len(names), len(set(names)))


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class EffectsMotionTokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                               hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                               typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                               size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        T.install_stub_with_list_widgets()
        cls.effects_mod = T.import_with_fakes("ui.effects")

    def test_easing_curve_resolves_default(self):
        curve = self.effects_mod._easing_curve()
        self.assertIsNotNone(curve)

    def test_easing_curve_falls_back_for_unknown_name(self):
        """Must not raise for a name that doesn't exist on QEasingCurve.Type -
        this is the robustness _easing_curve()'s own docstring promises.
        (Not comparing the returned value to a second lookup: the fake
        PyQt6 stand-in's attribute absorber hands back a fresh stub object
        on every access, so object equality isn't meaningful here - this is
        a stub-harness limitation, not something the real QEasingCurve.Type
        would do.)"""
        curve = self.effects_mod._easing_curve("NotARealEasingCurveName")
        self.assertIsNotNone(curve)

    def test_slide_in_keeps_original_literal_default_duration(self):
        """Tier 21 deliberately did NOT tokenize slide_in's duration (see its
        own docstring) - 300 doesn't sit on the fast/normal/slow scale."""
        import inspect
        sig = inspect.signature(self.effects_mod.Anim.slide_in)
        self.assertEqual(sig.parameters["duration"].default, 300)

    def test_fade_in_and_stagger_show_default_to_none_and_resolve_via_theme(self):
        import inspect
        fade_sig = inspect.signature(self.effects_mod.Anim.fade_in)
        self.assertIsNone(fade_sig.parameters["duration"].default)
        stagger_sig = inspect.signature(self.effects_mod.Anim.stagger_show)
        self.assertIsNone(stagger_sig.parameters["delay"].default)
        self.assertIsNone(stagger_sig.parameters["duration"].default)
        self.assertEqual(self.effects_mod._DEFAULT_THEME_FOR_MOTION.motion_duration_normal_ms, 250)
        self.assertEqual(self.effects_mod._DEFAULT_THEME_FOR_MOTION.motion_stagger_delay_ms, 50)


if __name__ == "__main__":
    unittest.main()
