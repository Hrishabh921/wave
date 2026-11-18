"""
tests/test_taint.py — Tier 1 item #4: brain/taint.py's per-turn taint
record, in isolation.
================================================================================
Pure unit tests for the thread-local record itself - no brain, no fake
server, no filesystem. See tests/test_phase7_confirm_gate.py's
GuardTierEscalationTests for taint actually driving brain/groq_core.py's
_guard() end to end through a real WaveBrain.

    python -m unittest discover -s tests -p "test_taint.py"
"""
import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brain import taint                                         # noqa: E402
from settings.config import config                                # noqa: E402


class TaintTrackingTests(unittest.TestCase):
    def setUp(self):
        taint.clear_turn()
        self.addCleanup(taint.clear_turn)

    def test_a_fresh_turn_is_untainted(self):
        self.assertFalse(taint.turn_tainted())
        self.assertFalse(taint.turn_flagged())
        self.assertEqual(taint.turn_sources(), ())

    def test_marking_an_unflagged_source_still_taints_the_turn(self):
        """The whole point of item #4: "the model read something from the
        internet this turn" is the signal that matters, not "...and it
        looked suspicious" - a perfectly clean web result still counts,
        because the model reading it at all is what the rest of the turn
        needs to be more careful about."""
        taint.mark_turn("a web search", flagged=False)
        self.assertTrue(taint.turn_tainted())
        self.assertFalse(taint.turn_flagged())
        self.assertEqual(taint.turn_sources(), ("a web search",))

    def test_marking_a_flagged_source_sets_both_signals(self):
        taint.mark_turn("the screen (OCR)", flagged=True)
        self.assertTrue(taint.turn_tainted())
        self.assertTrue(taint.turn_flagged())

    def test_turn_sources_deduplicates_but_keeps_first_seen_order(self):
        taint.mark_turn("a web search", flagged=False)
        taint.mark_turn("an attached file (notes.txt)", flagged=False)
        taint.mark_turn("a web search", flagged=True)      # same source again, different flag
        self.assertEqual(taint.turn_sources(), ("a web search", "an attached file (notes.txt)"))
        self.assertTrue(taint.turn_flagged())              # the repeat's flagged=True still counts

    def test_clear_turn_resets_everything(self):
        taint.mark_turn("a web search", flagged=True)
        taint.clear_turn()
        self.assertFalse(taint.turn_tainted())
        self.assertFalse(taint.turn_flagged())
        self.assertEqual(taint.turn_sources(), ())

    def test_mark_turn_ignores_an_empty_source(self):
        taint.mark_turn("", flagged=True)
        self.assertFalse(taint.turn_tainted())

    def test_kill_switch_makes_mark_turn_a_no_op(self):
        old = config.RISK_TIER_GUARD_ENABLED
        config.RISK_TIER_GUARD_ENABLED = False
        try:
            taint.mark_turn("a web search", flagged=True)
            self.assertFalse(taint.turn_tainted())
        finally:
            config.RISK_TIER_GUARD_ENABLED = old

    def test_taint_is_thread_local(self):
        """Marking on this thread must never leak into another thread's
        turn - the entire reason this is threading.local() and not a
        single module-level flag (see the module docstring: Wave's worker
        threads can each have a turn in flight at once)."""
        taint.mark_turn("a web search", flagged=False)
        self.assertTrue(taint.turn_tainted())

        seen = {}

        def other_thread():
            seen["tainted_there"] = taint.turn_tainted()
            taint.mark_turn("a weather service", flagged=False)  # shouldn't touch this thread either

        t = threading.Thread(target=other_thread)
        t.start()
        t.join()

        self.assertFalse(seen["tainted_there"])
        self.assertEqual(taint.turn_sources(), ("a web search",))   # unaffected by the other thread's mark


if __name__ == "__main__":
    unittest.main()
