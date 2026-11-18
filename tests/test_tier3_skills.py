"""Tier 3 / item 10 - the modular "standalone skills" layer (skills/).

TestSkillsRegistry      - skills/registry.py's name -> skill dispatch:
                          known names run, unknown names return a message
                          instead of raising, and a skill whose run()
                          raises is caught (registry.run never propagates
                          an exception - see skills/registry.py's docstring).
TestDailyBriefing       - skills/daily_briefing.py combines three
                          independent lookups; each is mocked here so the
                          test is about the COMBINING logic (skip empty
                          sections, don't let one failure blank the
                          others), not about web/web_ops.py or
                          automation/calendar_manager.py themselves, which
                          have (or should have) their own tests.
TestSelfDiagnostics     - skills/self_diagnostics.py's four checks, each
                          forced to a known outcome via mocking so the
                          pass/fail wording and the "problems" header logic
                          are what's actually under test.

Run: python -m unittest discover -s tests -p "test_tier3_*.py"
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: F401

# web/web_ops.py (imported transitively through skills/daily_briefing.py)
# needs pywhatkit, which isn't installed in this test environment - same
# stub-before-import pattern test_phase4_brain.py and friends already use.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                       hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                       screenshot=lambda *a, **k: None)
S.install_fake_module("pywhatkit")

from skills import registry as standalone_skills
from skills.base import StandaloneSkill
import skills.daily_briefing as daily_briefing_mod
import skills.self_diagnostics as self_diag_mod
from brain import llm_provider as real_llm_provider


class TestSkillsRegistry(unittest.TestCase):

    def test_known_skills_are_listed(self):
        names = [name for name, _ in standalone_skills.list_skills()]
        self.assertIn("daily_briefing", names)
        self.assertIn("self_diagnostics", names)

    def test_unknown_skill_name_returns_message_not_raise(self):
        result = standalone_skills.run("does_not_exist")
        self.assertIn("does_not_exist", result)
        self.assertIn("don't have", result)

    def test_run_dispatches_to_the_right_skill(self):
        with mock.patch.object(daily_briefing_mod, "run_daily_briefing", return_value="briefing text"):
            # registry binds run=run_daily_briefing at import time, so patch
            # the registry entry itself to prove dispatch-by-name works.
            fake = StandaloneSkill("daily_briefing", "fake", lambda: "patched result")
            with mock.patch.dict(standalone_skills._SKILLS, {"daily_briefing": fake}):
                self.assertEqual(standalone_skills.run("daily_briefing"), "patched result")

    def test_a_skill_that_raises_is_caught_not_propagated(self):
        def boom():
            raise RuntimeError("kaboom")
        fake = StandaloneSkill("self_diagnostics", "fake", boom)
        with mock.patch.dict(standalone_skills._SKILLS, {"self_diagnostics": fake}):
            result = standalone_skills.run("self_diagnostics")  # must not raise
        self.assertIn("self diagnostics", result.lower())


class TestDailyBriefing(unittest.TestCase):

    def test_all_three_sections_present(self):
        with mock.patch.object(daily_briefing_mod, "_weather_line", return_value="It's sunny +25C."), \
             mock.patch.object(daily_briefing_mod, "_calendar_line", return_value="You have a meeting at 3pm."), \
             mock.patch.object(daily_briefing_mod, "_reminders_line", return_value="You have one reminder today: call mom."):
            result = daily_briefing_mod.run_daily_briefing()
        self.assertIn("sunny", result)
        self.assertIn("meeting", result)
        self.assertIn("call mom", result)

    def test_one_failing_section_does_not_blank_the_others(self):
        with mock.patch.object(daily_briefing_mod, "_weather_line", return_value=""), \
             mock.patch.object(daily_briefing_mod, "_calendar_line", return_value="You have a meeting at 3pm."), \
             mock.patch.object(daily_briefing_mod, "_reminders_line", return_value=""):
            result = daily_briefing_mod.run_daily_briefing()
        self.assertIn("meeting", result)

    def test_all_sections_empty_gives_a_clear_fallback(self):
        with mock.patch.object(daily_briefing_mod, "_weather_line", return_value=""), \
             mock.patch.object(daily_briefing_mod, "_calendar_line", return_value=""), \
             mock.patch.object(daily_briefing_mod, "_reminders_line", return_value=""):
            result = daily_briefing_mod.run_daily_briefing()
        self.assertIn("don't have anything to brief", result)

    def test_weather_line_ignores_failure_strings(self):
        with mock.patch.object(daily_briefing_mod.WebManager, "get_weather", return_value="Weather data unavailable right now."):
            self.assertEqual(daily_briefing_mod._weather_line(), "")

    def test_weather_line_passes_through_real_reading(self):
        with mock.patch.object(daily_briefing_mod.WebManager, "get_weather", return_value="Partly cloudy +29C"):
            self.assertIn("Partly cloudy", daily_briefing_mod._weather_line())

    def test_reminders_line_only_counts_todays_reminders(self):
        import datetime
        today = datetime.datetime.now()
        tomorrow = today + datetime.timedelta(days=1)
        rows = [
            (1, "call mom", today.isoformat(), None),
            (2, "team offsite", tomorrow.isoformat(), None),
        ]
        with mock.patch.object(daily_briefing_mod.db, "get_upcoming_reminders", return_value=rows):
            line = daily_briefing_mod._reminders_line()
        self.assertIn("call mom", line)
        self.assertNotIn("team offsite", line)

    def test_calendar_section_errors_are_swallowed(self):
        with mock.patch.object(daily_briefing_mod.CalendarManager, "list_events", side_effect=RuntimeError("boom")):
            self.assertEqual(daily_briefing_mod._calendar_line(), "")  # must not raise


class TestSelfDiagnostics(unittest.TestCase):

    def _patch_llm_provider(self, ok=True, name="groq", model="llama-3.3", latency=42, message="Connected."):
        """Patches the three brain.llm_provider functions
        _check_llm_provider() calls, on the real module object - NOT via
        sys.modules swapping. self_diag_mod does `from brain import
        llm_provider` inside the function body each call; once any earlier
        test (or this file's own module-level import above) has done that
        once, `brain`'s package __dict__ already holds a direct reference
        to the real module object, so a sys.modules-only patch silently
        stops taking effect - this is what actually resolves it correctly
        across the whole test suite, not just in isolation."""
        return (
            mock.patch.object(real_llm_provider, "resolve_provider",
                               return_value=types.SimpleNamespace(name=name, model=model)),
            mock.patch.object(real_llm_provider, "build_client", return_value=object()),
            mock.patch.object(real_llm_provider, "check_connection", return_value=(ok, message, latency)),
        )

    def test_llm_provider_ok(self):
        patches = self._patch_llm_provider(ok=True)
        with patches[0], patches[1], patches[2]:
            result = self_diag_mod._check_llm_provider()
        self.assertTrue(result.startswith("OK"))
        self.assertIn("groq", result)
        self.assertIn("42ms", result)

    def test_llm_provider_problem(self):
        patches = self._patch_llm_provider(ok=False, message="No provider configured.")
        with patches[0], patches[1], patches[2]:
            result = self_diag_mod._check_llm_provider()
        self.assertTrue(result.startswith("PROBLEM"))
        self.assertIn("No provider configured", result)

    def test_llm_provider_check_itself_raising_is_caught(self):
        with mock.patch.object(real_llm_provider, "resolve_provider", side_effect=RuntimeError("network down")):
            result = self_diag_mod._check_llm_provider()
        self.assertTrue(result.startswith("PROBLEM"))

    def test_database_ok(self):
        with mock.patch.object(self_diag_mod.db, "list_workflows", return_value=[]):
            self.assertTrue(self_diag_mod._check_database().startswith("OK"))

    def test_database_problem(self):
        with mock.patch.object(self_diag_mod.db, "list_workflows", side_effect=RuntimeError("locked")):
            self.assertTrue(self_diag_mod._check_database().startswith("PROBLEM"))

    def test_disk_low_space_is_flagged(self):
        fake_usage = types.SimpleNamespace(free=500 * 1024 * 1024, total=0, used=0)  # 0.5 GB
        with mock.patch.object(self_diag_mod.shutil, "disk_usage", return_value=fake_usage):
            self.assertTrue(self_diag_mod._check_disk().startswith("LOW"))

    def test_disk_plenty_of_space_is_ok(self):
        fake_usage = types.SimpleNamespace(free=50 * 1024 ** 3, total=0, used=0)  # 50 GB
        with mock.patch.object(self_diag_mod.shutil, "disk_usage", return_value=fake_usage):
            self.assertTrue(self_diag_mod._check_disk().startswith("OK"))

    def test_run_self_diagnostics_reports_no_problems_when_all_ok(self):
        with mock.patch.object(self_diag_mod, "_check_llm_provider", return_value="OK - fine"), \
             mock.patch.object(self_diag_mod, "_check_database", return_value="OK - fine"), \
             mock.patch.object(self_diag_mod, "_check_disk", return_value="OK - fine"), \
             mock.patch.object(self_diag_mod, "_check_voice_stack", return_value="fine"):
            result = self_diag_mod.run_self_diagnostics()
        self.assertTrue(result.startswith("All systems checked out fine."))

    def test_run_self_diagnostics_names_the_broken_ones(self):
        with mock.patch.object(self_diag_mod, "_check_llm_provider", return_value="PROBLEM - unreachable"), \
             mock.patch.object(self_diag_mod, "_check_database", return_value="OK - fine"), \
             mock.patch.object(self_diag_mod, "_check_disk", return_value="LOW - 0.2 GB free"), \
             mock.patch.object(self_diag_mod, "_check_voice_stack", return_value="fine"):
            result = self_diag_mod.run_self_diagnostics()
        self.assertIn("LLM provider", result)
        self.assertIn("Disk space", result)
        self.assertNotIn("Database,", result)  # only broken ones named in the header


if __name__ == "__main__":
    unittest.main()
