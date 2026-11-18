"""
tests/test_tier15_taint_guard.py — Tier 15's actual "Done when" bar: every
keyboard tool (press_key/type_text/copy_text/paste_text/select_all_text/
undo_keystroke/redo_keystroke) is denied outright - not merely asked to
confirm - the moment the current turn already read something untrusted.

brain/tiers.py's TAINT_DENY_TOOLS is the mechanism (checked in
brain/groq_core.py's _guard() before the normal T0-T3xtaint branch - see
that file's own comments for the full reasoning): these seven tools stay
T1 (free to run on an ordinary turn, exactly like every other low-risk
tool), but get T3-strength refusal specifically when tainted, since the
keyboard surface sends real input to whatever currently has focus, which
Wave can't identify.

Reuses ConfirmGateBase/_taint() from test_phase7_confirm_gate.py (the same
fake-OpenAI-server WaveBrain harness GuardTierEscalationTests there
already uses for the ordinary T0-T3xtaint matrix) rather than duplicating
that setup - this file is the Tier 15 extension of exactly that matrix,
for the one new exception to it.

Run: python -m unittest discover -s tests -p "test_tier15_taint_guard.py"
(pytest will also collect it.)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_phase7_confirm_gate import ConfirmGateBase  # noqa: E402  (installs pyautogui/pywhatkit fakes too)

from brain import tiers                                # noqa: E402
from settings.config import config                      # noqa: E402

# One harmless args dict per keyboard tool - just enough to satisfy each
# skill's required-argument schema; which exact key/text is irrelevant to
# every test here, only whether the call runs, confirms, or is denied.
_KEYBOARD_TOOL_ARGS = {
    "press_key": {"key_combo": "a"},
    "type_text": {"text": "hello"},
    "copy_text": {},
    "paste_text": {},
    "select_all_text": {},
    "undo_keystroke": {},
    "redo_keystroke": {},
}


class TaintDenyTableTests(unittest.TestCase):
    """Pure/offline - no brain, no server. Table-vs-reality cross-check,
    same spirit as tests/test_security_tiers.py: this set is the whole
    point of the roadmap item, so it can't be allowed to silently drift
    from what _guard() actually consults."""

    def test_taint_deny_tools_is_exactly_the_seven_keyboard_tools(self):
        self.assertEqual(tiers.TAINT_DENY_TOOLS, frozenset(_KEYBOARD_TOOL_ARGS))

    def test_every_taint_deny_tool_is_in_the_tier_table_as_t1(self):
        """TAINT_DENY_TOOLS is an ADDITION on top of the T0-T3 table, not a
        replacement for it - every one of these must still have an
        ordinary baseline tier, and it must be T1 (see brain/tiers.py's
        own comment on why T1, not a flat T2/T3)."""
        for name in tiers.TAINT_DENY_TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(tiers.get_tier(name), tiers.T1)


class KeyboardTaintDenyTests(ConfirmGateBase):
    def setUp(self):
        super().setUp()
        self.taint = self.gc.taint
        self.addCleanup(self.taint.clear_turn)

    def _taint(self):
        with mock.patch.object(self.gc.WebManager, "ask_internet", return_value="Some web page body text."):
            self.brain._dispatch_tool("get_live_information", {"query": "anything"})
        self.assertTrue(self.taint.turn_tainted())      # sanity-check the setup itself before testing on it

    def test_every_keyboard_tool_runs_freely_on_an_ordinary_untainted_turn(self):
        """Baseline that TAINT_DENY_TOOLS must NOT break: ordinary,
        everyday keyboard use (the overwhelming majority of calls) still
        needs zero confirmation, exactly like any other T1 tool."""
        self.assertFalse(self.taint.turn_tainted())
        for tool, args in _KEYBOARD_TOOL_ARGS.items():
            with self.subTest(tool=tool):
                self.brain.pending_action = None
                out = self.brain._dispatch_tool(tool, args)
                self.assertNotIn("confirm", out.lower(), f"{tool} should not ask to confirm when untainted")
                self.assertNotIn("won't", out.lower(), f"{tool} should not be denied when untainted")
                self.assertIsNone(self.brain.pending_action)

    def test_every_keyboard_tool_is_denied_outright_once_the_turn_is_tainted(self):
        """The actual point of this whole file: a flat refusal, not merely
        a pending confirmation - contrast with an ordinary T1 tool like
        create_folder, which only escalates to ASKING first (see
        GuardTierEscalationTests.test_t1_tool_escalates_to_confirm_when_turn_is_tainted
        in test_phase7_confirm_gate.py) rather than refusing outright."""
        for tool, args in _KEYBOARD_TOOL_ARGS.items():
            with self.subTest(tool=tool):
                self.brain.pending_action = None
                self.taint.clear_turn()
                self._taint()
                out = self.brain._dispatch_tool(tool, args)
                self.assertIn("won't", out.lower(), f"{tool} should be denied, got: {out!r}")
                self.assertIsNone(self.brain.pending_action)   # nothing armed - a deny, not a pending confirm

    def test_denied_message_names_the_source_same_as_any_other_denial(self):
        self._taint()
        out = self.brain._dispatch_tool("press_key", {"key_combo": "a"})
        self.assertIn("web search", out.lower())

    def test_guard_keyword_path_also_denies_a_tainted_keyboard_tool(self):
        """_guard_keyword() (the deterministic/keyword dispatch path)
        shares _guard() with _dispatch_tool() (the AI path) - same other-
        caller coverage test_guard_keyword_path_also_denies_a_tainted_t3_tool
        gives the pre-existing T3 case in test_phase7_confirm_gate.py."""
        self._taint()
        gate = self.brain._guard_keyword("copy_text", {})
        self.assertIsNotNone(gate)
        self.assertIn("won't", gate.lower())
        self.assertIsNone(self.brain.pending_action)

    def test_a_plain_yes_after_a_deny_replays_nothing(self):
        """Confirms the deny branch truly never arms self.pending_action,
        same shape as the pre-existing T3 version of this test."""
        self._taint()
        self.brain._dispatch_tool("undo_keystroke", {})
        self.assertIsNone(self.brain.pending_action)

        from _fake_openai import Reply, completion
        self.srv.script = [Reply(200, completion("Sure - what would you like to do?"))]
        reply = self.brain.think("yes")
        self.assertEqual(reply, "Sure - what would you like to do?")   # treated as a brand new message

    def test_a_non_keyboard_t1_tool_is_unaffected_by_taint_deny(self):
        """TAINT_DENY_TOOLS is scoped to tool IDENTITY only - an ordinary
        T1 tool not in that set still gets the NORMAL T1xtainted behaviour
        (ask first), never a flat deny, proving this doesn't leak into the
        rest of the T0-T3 matrix."""
        self._taint()
        out = self.brain._dispatch_tool("create_folder", {"folder_name": "StillAsksFirst"})
        self.assertIn("confirm", out.lower())
        self.assertNotIn("won't", out.lower())
        self.assertIsNotNone(self.brain.pending_action)

    def test_kill_switch_restores_flat_behaviour_for_keyboard_tools_too(self):
        """RISK_TIER_GUARD_ENABLED=false must bypass TAINT_DENY_TOOLS the
        same way it bypasses tiers.py entirely (see
        GuardTierEscalationTests.test_kill_switch_restores_flat_confirm_only_behaviour_ignoring_taint) -
        the new check only runs inside the tier-guard path. None of these
        seven ever had a legacy confirm=True, so with the switch off they
        should run even on an already-tainted turn, never deny."""
        old = config.RISK_TIER_GUARD_ENABLED
        self.addCleanup(setattr, config, "RISK_TIER_GUARD_ENABLED", old)
        self._taint()
        config.RISK_TIER_GUARD_ENABLED = False
        out = self.brain._dispatch_tool("copy_text", {})
        self.assertNotIn("won't", out.lower())
        self.assertNotIn("confirm", out.lower())
        self.assertIsNone(self.brain.pending_action)

    def test_end_to_end_ai_tool_calling_denies_press_key_after_reading_the_web(self):
        """Same end-to-end shape as
        test_end_to_end_ai_tool_calling_denies_a_destructive_step_after_reading_the_web
        in test_phase7_confirm_gate.py, but for the NEW keyboard exception
        rather than a T3 tool: round 1 the model reads the web (tainting
        the turn), round 2 it tries press_key, round 3 it only gets to
        paraphrase a refusal - press_key never actually sends anything."""
        from _fake_openai import Reply, completion, tool_call
        self.srv.script = [
            Reply(200, completion(None, [tool_call("get_live_information", {"query": "anything"})])),
            Reply(200, completion(None, [tool_call("press_key", {"key_combo": "enter"})])),
            Reply(200, completion("I won't do that without you asking me directly.")),
        ]
        with mock.patch.object(self.gc.WebManager, "ask_internet", return_value="Some web page body text."), \
             mock.patch("automation.app_control.pyautogui.press") as press, \
             mock.patch("automation.app_control.pyautogui.hotkey") as hotkey:
            reply = self.brain.think("look something up, then press enter")
        self.assertEqual(reply, "I won't do that without you asking me directly.")
        press.assert_not_called()
        hotkey.assert_not_called()
        self.assertIsNone(self.brain.pending_action)
        sent = self.srv.posts()[-1].json["messages"]
        tool_results = [m.get("content") or "" for m in sent if m.get("role") == "tool"]
        self.assertTrue(any("won't" in c.lower() for c in tool_results))


if __name__ == "__main__":
    unittest.main()
