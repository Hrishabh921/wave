"""
tests/test_tier25_avatar_skills.py - Tier 25: brain/handlers/avatar.py's
show_ui/hide_ui skills, plus avatar/registry.py's singleton they depend
on. Deliberately does NOT import avatar.control (the real, Qt-dependent
AvatarController) - this sandbox has no PyQt6 installed, and the whole
point of the registry.py split (see that module's own docstring, and
avatar/control.py's) is that brain/handlers/avatar.py never needs it to.
A fake duck-typed controller stands in here instead.

    python -m unittest discover -s tests -p "test_tier25_avatar_skills.py"
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S  # noqa: F401,E402  (puts the project root on sys.path, temp TTFA log)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from avatar.registry import set_active_controller, get_active_controller  # noqa: E402
from brain.handlers.avatar import _skill_show_ui, _skill_hide_ui  # noqa: E402


class _FakeController:
    """Anything with show_ui()/hide_ui() methods works - the real
    AvatarController (avatar/control.py) is duck-typed the same way,
    just backed by actual QWidgets instead of these counters."""

    def __init__(self):
        self.show_calls = 0
        self.hide_calls = 0

    def show_ui(self) -> str:
        self.show_calls += 1
        return "Dashboard khol diya."

    def hide_ui(self) -> str:
        self.hide_calls += 1
        return "Dashboard chhupa diya."


class AvatarRegistryTests(unittest.TestCase):
    def tearDown(self):
        set_active_controller(None)  # never leak a fake controller into another test file

    def test_defaults_to_none(self):
        set_active_controller(None)
        self.assertIsNone(get_active_controller())

    def test_set_then_get_returns_the_same_object(self):
        fake = _FakeController()
        set_active_controller(fake)
        self.assertIs(get_active_controller(), fake)

    def test_setting_none_clears_it(self):
        set_active_controller(_FakeController())
        set_active_controller(None)
        self.assertIsNone(get_active_controller())


class ShowHideUiSkillTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(set_active_controller, None)

    def test_show_ui_with_no_controller_registered_says_so_plainly(self):
        set_active_controller(None)
        result = _skill_show_ui(None, {})
        self.assertIn("nahi hai", result)

    def test_hide_ui_with_no_controller_registered_says_so_plainly(self):
        set_active_controller(None)
        result = _skill_hide_ui(None, {})
        self.assertIn("nahi hai", result)

    def test_show_ui_delegates_to_the_active_controller(self):
        fake = _FakeController()
        set_active_controller(fake)
        result = _skill_show_ui(None, {})
        self.assertEqual(result, "Dashboard khol diya.")
        self.assertEqual(fake.show_calls, 1)
        self.assertEqual(fake.hide_calls, 0)

    def test_hide_ui_delegates_to_the_active_controller(self):
        fake = _FakeController()
        set_active_controller(fake)
        result = _skill_hide_ui(None, {})
        self.assertEqual(result, "Dashboard chhupa diya.")
        self.assertEqual(fake.hide_calls, 1)
        self.assertEqual(fake.show_calls, 0)

    def test_args_dict_is_accepted_but_ignored(self):
        """Both skills take zero parameters (see their @skill(...)
        registration - no params/required given) - a stray/extra args
        dict (the brain always passes one, even if empty) must not
        raise."""
        fake = _FakeController()
        set_active_controller(fake)
        self.assertEqual(_skill_show_ui(None, {"unexpected": "value"}), "Dashboard khol diya.")

    def test_brain_argument_is_never_touched(self):
        """Neither handler reads anything off `brain` - passing a plain
        sentinel object (not a real WaveBrain) must work identically to
        passing None."""
        fake = _FakeController()
        set_active_controller(fake)
        sentinel = object()
        self.assertEqual(_skill_show_ui(sentinel, {}), "Dashboard khol diya.")
        self.assertEqual(_skill_hide_ui(sentinel, {}), "Dashboard chhupa diya.")


class AvatarSkillsAreRegisteredTests(unittest.TestCase):
    """Confirms show_ui/hide_ui are actually wired into the live skill
    registry via brain/handlers/__init__.py - importing brain.handlers.
    avatar directly (as the classes above do) proves the functions work,
    but not that WaveBrain's normal dispatch path would ever reach them."""

    def test_both_skills_are_in_the_live_registry(self):
        import brain.groq_core  # noqa: F401  (side effect: imports brain.handlers)
        from brain.skills_registry import skills
        self.assertIn("show_ui", skills)
        self.assertIn("hide_ui", skills)

    def test_registered_handlers_are_the_same_functions(self):
        import brain.groq_core  # noqa: F401
        from brain.skills_registry import skills
        self.assertIs(skills.get_handler("show_ui"), _skill_show_ui)
        self.assertIs(skills.get_handler("hide_ui"), _skill_hide_ui)


if __name__ == "__main__":
    unittest.main()
