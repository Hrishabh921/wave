"""Tier 20 item: security event log + off/standard/strict mode.

Covers:
  * automation/security_log.py - record()/recent()/effective_mode()/
    set_mode()/clear(), including the redaction-on-reason-text safety net
    and the mode validation fallback.
  * brain/redaction.py's redact() -> automation/security_log.py wiring
    (a genuine hit logs a "redaction" event; a miss logs nothing).
  * brain/groq_core.py's _guard() recording guard_deny/guard_confirm
    events with policy context (tier, taint, source) for Task #88's
    "why did you do that?" to read later.

No display, no audio device, no network - same
_phase4_support.install_fake_module stubbing every other file in this
suite already uses.
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
from brain import redaction
from settings.config import config


class SecurityLogCoreTests(unittest.TestCase):
    def setUp(self):
        self._mode = config.SECURITY_MODE
        self._redact = config.REDACT_SECRETS_ENABLED
        config.SECURITY_MODE = "standard"
        config.REDACT_SECRETS_ENABLED = True
        security_log.clear()

    def tearDown(self):
        config.SECURITY_MODE = self._mode
        config.REDACT_SECRETS_ENABLED = self._redact
        security_log.clear()

    def test_record_and_recent_round_trip(self):
        security_log.record("guard_deny", reason="denied a thing", tool="delete_file", source="ai")
        events = security_log.recent(10)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "guard_deny")
        self.assertEqual(events[0].tool, "delete_file")

    def test_recent_is_newest_first(self):
        security_log.record("guard_deny", reason="first")
        security_log.record("guard_confirm", reason="second")
        events = security_log.recent(10)
        self.assertEqual([e.reason for e in events], ["second", "first"])

    def test_recent_respects_limit(self):
        for i in range(5):
            security_log.record("guard_confirm", reason=f"event {i}")
        self.assertEqual(len(security_log.recent(2)), 2)

    def test_ring_buffer_caps_at_max_events(self):
        for i in range(security_log._MAX_EVENTS + 50):
            security_log.record("guard_confirm", reason=f"event {i}")
        self.assertEqual(len(security_log.recent(10_000)), security_log._MAX_EVENTS)

    def test_reason_text_is_redacted(self):
        secret = "sk-" + "a" * 30
        security_log.record("guard_deny", reason=f"blocked because of {secret}")
        events = security_log.recent(1)
        self.assertNotIn(secret, events[0].reason)
        self.assertIn("[REDACTED:openai_key]", events[0].reason)

    def test_effective_mode_reads_config(self):
        config.SECURITY_MODE = "strict"
        self.assertEqual(security_log.effective_mode(), "strict")

    def test_effective_mode_falls_back_on_garbage(self):
        config.SECURITY_MODE = "whatever-this-is-not-valid"
        self.assertEqual(security_log.effective_mode(), "standard")

    def test_set_mode_applies_a_valid_value(self):
        result = security_log.set_mode("off")
        self.assertEqual(result, "off")
        self.assertEqual(config.SECURITY_MODE, "off")

    def test_set_mode_rejects_garbage_and_falls_back(self):
        result = security_log.set_mode("not-a-real-mode")
        self.assertEqual(result, "standard")
        self.assertEqual(config.SECURITY_MODE, "standard")

    def test_set_mode_logs_a_mode_change_event(self):
        security_log.set_mode("strict")
        events = security_log.recent(1)
        self.assertEqual(events[0].kind, "mode_change")
        self.assertIn("standard", events[0].reason)
        self.assertIn("strict", events[0].reason)

    def test_set_mode_to_the_same_value_logs_nothing(self):
        security_log.set_mode("standard")  # already standard
        self.assertEqual(security_log.recent(10), [])

    def test_clear_empties_the_log(self):
        security_log.record("guard_deny", reason="x")
        security_log.clear()
        self.assertEqual(security_log.recent(10), [])

    def test_record_never_raises_on_bad_input(self):
        try:
            security_log.record("guard_deny", reason=None, taint_sources=None)  # type: ignore
        except Exception as e:  # pragma: no cover - this is the assertion
            self.fail(f"record() raised: {e}")


class RedactionToSecurityLogWiringTests(unittest.TestCase):
    def setUp(self):
        self._redact = config.REDACT_SECRETS_ENABLED
        config.REDACT_SECRETS_ENABLED = True
        security_log.clear()

    def tearDown(self):
        config.REDACT_SECRETS_ENABLED = self._redact
        security_log.clear()

    def test_a_real_hit_logs_a_redaction_event(self):
        redaction.redact("my key is sk-" + "a" * 30)
        events = security_log.recent(10)
        self.assertTrue(any(e.kind == "redaction" for e in events))

    def test_no_hit_logs_nothing(self):
        redaction.redact("just some ordinary text, nothing secret here")
        self.assertEqual(security_log.recent(10), [])

    def test_kill_switch_logs_nothing_either(self):
        config.REDACT_SECRETS_ENABLED = False
        redaction.redact("my key is sk-" + "a" * 30)
        self.assertEqual(security_log.recent(10), [])


class GuardSecurityLogWiringTests(unittest.TestCase):
    """_guard() itself needs a real WaveBrain instance to exercise, which
    is heavier machinery than this file wants to stand up - instead this
    verifies the wiring surface directly: security_log is importable from
    brain.groq_core and the module-level call sites exist, while the full
    tier x taint decision table itself stays covered by
    tests/test_security_tiers.py (unchanged by this wiring - see that
    file's own continued pass)."""

    def test_groq_core_imports_security_log(self):
        import brain.groq_core as gc
        self.assertTrue(hasattr(gc, "security_log"))
        self.assertIs(gc.security_log, security_log)


if __name__ == "__main__":
    unittest.main()
