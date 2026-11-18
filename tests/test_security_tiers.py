"""
tests/test_security_tiers.py — Tier 1 item #3: brain/tiers.py's TIERS
table stays in sync with the live skill registry.
================================================================================
Mirrors SARA's own tests/test_security_tiers.py (same name, same job): a
tier table doesn't stay accurate on its own - what keeps it honest is a
test that fails CI the moment the table and the real registered tools
drift apart in either direction, so this can't quietly rot the way the
old flat, hand-maintained 7-tool confirm list could have.

    python -m unittest discover -s tests -p "test_security_tiers.py"
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import brain.groq_core  # noqa: E402,F401  (side effect: registers every @skill-decorated tool)
from brain import tiers                                         # noqa: E402
from brain.skills_registry import skills, BUILTIN_SOURCE         # noqa: E402

# The 7 tools that required confirmation before this table existed at all
# (brain/confirm_gate.py's _FRIENDLY_ACTIONS / the old skills.requires_confirm()
# binary flag) - see brain/tiers.py's own module docstring for why these
# specifically must land at T2 or higher.
_ORIGINALLY_GATED = (
    "delete_file", "delete_latest_screenshot", "forget_memory",
    "send_whatsapp_message", "close_app", "create_file", "take_photo",
)
# Of those, the ones genuinely destructive/irreversible enough to be the
# T3 tier _guard() escalates to an outright deny under taint - narrower
# than _ORIGINALLY_GATED, see brain/tiers.py's TIERS table comment.
_T3_EXPECTED = ("delete_file", "forget_memory", "send_whatsapp_message")


class SecurityTiersTests(unittest.TestCase):
    def test_every_builtin_tool_has_a_tier(self):
        """A built-in tool missing from TIERS silently falls back to
        UNKNOWN_TIER - a safe fail-safe, but that's not the same as
        someone having actually made a considered decision about it.
        This should always be deliberate (add it to TIERS explicitly),
        never an oversight this test lets slide."""
        untiered = sorted(set(skills.names_by_source(BUILTIN_SOURCE)) - set(tiers.TIERS))
        self.assertEqual(untiered, [],
                         f"built-in tool(s) missing from brain.tiers.TIERS: {untiered}")

    def test_no_stale_entries_in_tier_table(self):
        """The reverse check: an entry in TIERS for a tool that no longer
        exists (renamed or removed) is worse than dead weight - it's a
        false sense of having reviewed something that isn't there."""
        stale = sorted(set(tiers.TIERS) - set(skills.names_by_source(BUILTIN_SOURCE)))
        self.assertEqual(stale, [],
                         f"tiers.TIERS entries with no matching registered tool: {stale}")

    def test_every_tier_value_is_a_real_tier(self):
        valid = {tiers.T0, tiers.T1, tiers.T2, tiers.T3}
        bad = {name: tier for name, tier in tiers.TIERS.items() if tier not in valid}
        self.assertEqual(bad, {}, f"tool(s) with an out-of-range tier value: {bad}")

    def test_unknown_tool_gets_the_risky_fail_safe_not_the_safe_one(self):
        """UNKNOWN_TIER has to be risky-by-default, not safe-by-default -
        that's the entire point of a fail-safe default. Pinned to an
        explicit value (not just "assertNotEqual(T0)") so a future edit
        can't quietly weaken it to something less cautious."""
        self.assertEqual(tiers.UNKNOWN_TIER, tiers.T2)
        self.assertEqual(tiers.get_tier("a_tool_that_will_never_exist"), tiers.T2)

    def test_the_seven_originally_gated_tools_landed_at_t2_or_t3(self):
        """What keeps Tier 1 from being a silent behaviour change: every
        tool that already required confirmation before this table existed
        must land at T2 or T3 (never T0/T1), since those are the only
        tiers whose BASELINE (untainted-turn) outcome is "confirm" - see
        brain/groq_core.py's _guard()."""
        for name in _ORIGINALLY_GATED:
            self.assertIn(name, tiers.TIERS, f"{name} should be explicitly tiered, not fall back to UNKNOWN_TIER")
            self.assertGreaterEqual(tiers.get_tier(name), tiers.T2, name)

    def test_the_most_destructive_tools_are_specifically_t3(self):
        """Narrower than the check above: these three need to be T3
        specifically, not just "T2 or higher" - T3 is the only tier
        _guard() ever escalates to an outright deny (rather than merely
        confirm) when the turn already read something untrusted, and that
        escalation is the actual point of Tier 1 item #5 for exactly
        these tools."""
        for name in _T3_EXPECTED:
            self.assertEqual(tiers.get_tier(name), tiers.T3, name)

    def test_label_never_raises_even_for_a_bad_value(self):
        self.assertEqual(tiers.label(tiers.T0), "T0 read-only")
        self.assertEqual(tiers.label(99), "T99")  # out-of-range - describes itself, doesn't crash


if __name__ == "__main__":
    unittest.main()
