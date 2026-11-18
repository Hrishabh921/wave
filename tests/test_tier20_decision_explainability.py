"""Tier 20 item #88: "why did you do that?" / "why did you notify me?"

Covers:
  * automation/proactive.py's _emit() logging a "proactive_notify" event to
    automation/security_log.py for every nudge, with the real trigger
    reason (the nudge's own title+message) - not a generic "something
    fired" placeholder.
  * brain/handlers/system.py's new why_did_you_do_that/why_did_you_notify_me
    read-only skills: most-recent-wins, "nothing yet" honesty, and that
    they don't cross-answer each other's question (a guard event doesn't
    satisfy "why did you notify me" and vice versa).
  * Both are registered T0 in brain/tiers.py and show up in the
    "system" domain in tests/test_handlers_structure.py's own mapping
    (checked indirectly here by importing both and calling them directly -
    the domain-mapping exactness itself is that other test's job).

No display, no audio device, no network - same _phase4_support stubbing
every other file in this suite already uses.
"""
import unittest
from unittest import mock

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                       hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                       typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                       size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from automation import security_log
from automation.proactive import ProactiveEngine, ProactiveNudge
from settings.config import config


class ProactiveEmitSecurityLogTests(unittest.TestCase):
    def setUp(self):
        self._reword = config.PROACTIVE_LLM_REWORD_ENABLED
        config.PROACTIVE_LLM_REWORD_ENABLED = False  # keep nudge.message as the plain template
        security_log.clear()
        self.engine = ProactiveEngine()

    def tearDown(self):
        config.PROACTIVE_LLM_REWORD_ENABLED = self._reword
        security_log.clear()

    def test_emit_logs_a_proactive_notify_event_with_the_real_reason(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                                message="Battery is at 12% and not charging.")
        self.engine._emit(nudge)
        events = [e for e in security_log.recent(10) if e.kind == "proactive_notify"]
        self.assertEqual(len(events), 1)
        self.assertIn("Battery is at 12%", events[0].reason)
        self.assertIn("Battery low", events[0].reason)
        self.assertEqual(events[0].tier, "battery")
        self.assertEqual(events[0].source, "proactive")

    def test_emit_still_reaches_listeners_even_if_security_log_raises(self):
        """Fail-open: a broken security_log must never stop a nudge from
        actually reaching its listeners (or crash _emit() outright) - same
        philosophy as every other Tier 20 module."""
        seen = []
        self.engine.add_listener(lambda n: seen.append(n))
        with mock.patch.object(security_log, "record", side_effect=RuntimeError("boom")):
            nudge = ProactiveNudge(kind="download", title="Download finished",
                                    message="myfile.zip finished downloading.")
            self.engine._emit(nudge)
        self.assertEqual(len(seen), 1)

    def test_each_nudge_kind_gets_its_own_distinguishable_reason(self):
        for kind, title, message in [
            ("calendar", "Upcoming event", "Standup starts in 5 minutes."),
            ("low_disk", "Low disk space", "C: is down to 4% free space."),
            ("streak", "Streak milestone", "7-day streak on morning pages!"),
        ]:
            security_log.clear()
            self.engine._emit(ProactiveNudge(kind=kind, title=title, message=message))
            events = [e for e in security_log.recent(10) if e.kind == "proactive_notify"]
            self.assertEqual(len(events), 1)
            self.assertIn(message, events[0].reason)
            self.assertEqual(events[0].tier, kind)


class WhyDidYouDoThatSkillTests(unittest.TestCase):
    def setUp(self):
        security_log.clear()
        import brain.handlers.system as sysmod
        self.sysmod = sysmod

    def tearDown(self):
        security_log.clear()

    def test_explains_the_most_recent_guard_deny(self):
        security_log.record("guard_confirm", reason="older confirm, should be shadowed", tool="open_app")
        security_log.record("guard_deny", reason="denied delete_file - this turn read something untrusted.",
                             tool="delete_file")
        out = self.sysmod._skill_why_did_you_do_that(None, {})
        self.assertIn("denied delete_file", out)

    def test_explains_the_most_recent_guard_confirm_when_no_deny_is_newer(self):
        security_log.record("guard_deny", reason="an older denial")
        security_log.record("guard_confirm", reason="Asked before running 'close_app' - it's a sensitive/destructive action.")
        out = self.sysmod._skill_why_did_you_do_that(None, {})
        self.assertIn("close_app", out)

    def test_ignores_non_guard_events(self):
        security_log.record("redaction", reason="scrubbed a secret")
        security_log.record("mode_change", reason="mode changed to strict")
        security_log.record("proactive_notify", reason="Battery low: Battery is at 12%.")
        out = self.sysmod._skill_why_did_you_do_that(None, {})
        self.assertIn("haven't had to ask", out)

    def test_nothing_yet_is_honest_not_a_crash(self):
        out = self.sysmod._skill_why_did_you_do_that(None, {})
        self.assertIn("haven't had to ask", out)


class WhyDidYouNotifyMeSkillTests(unittest.TestCase):
    def setUp(self):
        security_log.clear()
        import brain.handlers.system as sysmod
        self.sysmod = sysmod

    def tearDown(self):
        security_log.clear()

    def test_explains_the_most_recent_nudge(self):
        security_log.record("proactive_notify", reason="Morning briefing: here's your day.", tier="morning_briefing")
        security_log.record("proactive_notify", reason="Low disk space: C: is down to 4% free space.", tier="low_disk")
        out = self.sysmod._skill_why_did_you_notify_me(None, {})
        self.assertIn("Low disk space", out)
        self.assertNotIn("Morning briefing", out)

    def test_ignores_guard_events(self):
        security_log.record("guard_deny", reason="denied something")
        out = self.sysmod._skill_why_did_you_notify_me(None, {})
        self.assertIn("haven't notified you", out)

    def test_nothing_yet_is_honest_not_a_crash(self):
        out = self.sysmod._skill_why_did_you_notify_me(None, {})
        self.assertIn("haven't notified you", out)


class RegistrationTests(unittest.TestCase):
    def test_both_tools_are_t0(self):
        from brain import tiers
        self.assertEqual(tiers.TIERS["why_did_you_do_that"], tiers.T0)
        self.assertEqual(tiers.TIERS["why_did_you_notify_me"], tiers.T0)

    def test_both_tools_are_registered_skills(self):
        from brain.skills_registry import skills
        self.assertIn("why_did_you_do_that", skills.names())
        self.assertIn("why_did_you_notify_me", skills.names())


if __name__ == "__main__":
    unittest.main()
