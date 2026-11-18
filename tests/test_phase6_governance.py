"""Phase 6 - the governed tool-calling loop (Tier 3 gap #6).

Same fake-OpenAI-server harness as test_phase4_brain.py (see that file /
_fake_openai.py for how it works) - run with:
    python -m unittest discover -s tests -p "test_phase6_*.py"
"""
import os
import time
import unittest
from unittest import mock

import _phase4_support as S
from _fake_openai import FakeOpenAI, Reply, completion, tool_call

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                      # noqa: E402
from automation import undo as undo_mod                        # noqa: E402
from automation.audit import Audit, DbStore                    # noqa: E402
from automation.undo import TrashStore, UndoManager             # noqa: E402
from database.db_manager import db                              # noqa: E402
from settings.config import config                               # noqa: E402


class GovernanceBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

        old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        os.makedirs(os.path.join(self.tmp, "Desktop"))
        self.audit = Audit(store=DbStore(db), max_entries=100)
        self.trash = TrashStore(os.path.join(self.tmp, "trash"), 7, 10 * 1024 * 1024)
        self.um = UndoManager(self.audit, self.trash)
        saved = (audit_mod.audit, undo_mod.trash, undo_mod.undo_manager)
        audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = self.audit, self.trash, self.um
        import brain.groq_core as gc
        self.gc = gc
        gc_saved = (gc.audit, gc.undo_manager)
        gc.audit, gc.undo_manager = self.audit, self.um
        # Tier 4: list_recent_actions/undo_last_action moved to
        # brain/handlers/automation.py, which imports audit/undo_manager
        # independently of groq_core.py - patching gc.audit/gc.undo_manager
        # alone no longer reaches the code that actually runs for those two
        # tools, so this same swap needs to happen there too.
        import brain.handlers.automation as gc_automation
        gc_automation_saved = (gc_automation.audit, gc_automation.undo_manager)
        gc_automation.audit, gc_automation.undo_manager = self.audit, self.um

        # Small, fast-to-test governance bounds - overridden per-test where needed.
        cfg_saved = (config.TOOL_LOOP_MAX_STEPS, config.TOOL_LOOP_TIMEOUT_S)
        config.TOOL_LOOP_MAX_STEPS, config.TOOL_LOOP_TIMEOUT_S = 4, 45.0
        # Tier 2: these tests are specifically about the governed LLM
        # tool-calling loop - several of them use everyday phrasing
        # ("what's in my clipboard", "check my pc info...") that
        # brain/intent_router.py's fast path would now catch before any
        # of this class's scripted server replies are ever consulted.
        # Disabled here so this file keeps exercising exactly the loop it
        # was written for; see test_phase8_intent_router.py for the
        # router's own tests.
        router_saved = config.INTENT_ROUTER_ENABLED
        config.INTENT_ROUTER_ENABLED = False

        env = {"LLM_PROVIDER": "custom", "LLM_BASE_URL": self.srv.url, "LLM_MODEL": "test-model",
               "LLM_API_KEY": "sk-test-1234567890", "HOME": self.tmp, "USERPROFILE": self.tmp}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()

        def cleanup():
            patcher.stop()
            db.db_path = old_path
            audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = saved
            gc.audit, gc.undo_manager = gc_saved
            gc_automation.audit, gc_automation.undo_manager = gc_automation_saved
            config.TOOL_LOOP_MAX_STEPS, config.TOOL_LOOP_TIMEOUT_S = cfg_saved
            config.INTENT_ROUTER_ENABLED = router_saved
        self.addCleanup(cleanup)
        self.brain = gc.WaveBrain()


class MultiRoundLoopTests(GovernanceBase):
    def test_second_round_sees_first_rounds_result_and_can_call_another_tool(self):
        """The whole point of Phase 6: a request needing two DEPENDENT tool
        calls (the second one only decidable after seeing the first's
        result) used to be impossible - the old code forced tool_choice to
        'none' after exactly one round. Script: round 1 calls get_pc_info,
        round 2 (now allowed to happen) calls set_reminder, round 3 gives
        the final wording."""
        self.srv.script = [
            Reply(200, completion(None, [tool_call("get_pc_info", {})])),
            Reply(200, completion(None, [tool_call("set_reminder", {"message": "check battery", "when": "in 1 hour"})])),
            Reply(200, completion("Checked your PC info and set that reminder for you.")),
        ]
        reply = self.brain.think("check my pc info, then remind me to check battery in an hour")
        self.assertEqual(reply, "Checked your PC info and set that reminder for you.")
        # 3 separate round-trips, not 1 - and no SEPARATE compose-pass call was
        # needed, since round 3 itself came back with no more tool_calls and its
        # own content became the reply directly (tool_choice stays "auto" the
        # whole time the main loop is running; "none" is reserved for the
        # fallback compose pass used only when the governor cuts the loop off
        # mid-tool-call - see the StepLimitTests below).
        self.assertEqual(len(self.srv.posts()), 3)
        self.assertEqual(self.srv.posts()[2].json.get("tool_choice"), "auto")

        # round 2's request must carry round 1's tool result in its transcript
        round2_messages = self.srv.posts()[1].json["messages"]
        tool_msgs_r2 = [m for m in round2_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs_r2), 1)

        # round 3's request must carry BOTH prior rounds' results
        round3_messages = self.srv.posts()[2].json["messages"]
        tool_msgs_r3 = [m for m in round3_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs_r3), 2)

    def test_single_round_still_works_exactly_as_before(self):
        """No regression for the common case: one tool call, one compose pass."""
        self.srv.script = [
            Reply(200, completion(None, [tool_call("get_clipboard", {})])),
            Reply(200, completion("Your clipboard has 'hello world' in it.")),
        ]
        reply = self.brain.think("what's in my clipboard")
        self.assertEqual(reply, "Your clipboard has 'hello world' in it.")
        self.assertEqual(len(self.srv.posts()), 2)

    def test_no_tool_calls_at_all_still_answers_directly(self):
        self.srv.script = [Reply(200, completion("Hi there!"))]
        reply = self.brain.think("hello")
        self.assertEqual(reply, "Hi there!")
        self.assertEqual(len(self.srv.posts()), 1)


class StepLimitTests(GovernanceBase):
    def test_loop_stops_at_max_steps_instead_of_hanging_forever(self):
        """A model that NEVER stops wanting to call tools (confused, or a
        genuine infinite-loop bug upstream) used to be impossible to bound
        at all in the old single-shot code (not a problem then because there
        WAS no loop) - now that there is one, it must still stop."""
        config.TOOL_LOOP_MAX_STEPS = 3
        # Exactly as many tool-call replies as the step cap allows (the fake
        # server has no real concept of tool_choice="none" - it just returns
        # its script in order - so the 4th, compose-pass request must be the
        # one where the plain reply sits) plus one trailing plain reply for
        # that final compose pass.
        self.srv.script = [Reply(200, completion(None, [tool_call("get_pc_info", {}, id=f"call_{i}")]))
                           for i in range(3)] + [Reply(200, completion("Here's what I found."))]
        reply = self.brain.think("keep checking forever")
        # 3 tool-calling rounds + 1 compose pass = 4 requests, never more.
        self.assertEqual(len(self.srv.posts()), 4)
        self.assertEqual(self.srv.posts()[3].json.get("tool_choice"), "none")
        self.assertEqual(reply, "Here's what I found.")

    def test_step_limited_run_still_composes_a_reply_not_an_error(self):
        config.TOOL_LOOP_MAX_STEPS = 2
        self.srv.script = [Reply(200, completion(None, [tool_call("get_pc_info", {}, id=f"call_{i}")]))
                           for i in range(10)]  # NOTHING ever stops calling tools, no trailing plain reply either
        reply = self.brain.think("loop forever")
        # hits the step cap, falls back to composing from gathered results with
        # tool_choice="none" (which, per the fake server's default reply, has no
        # tool_calls) - so the user still gets real words back, not a crash.
        self.assertTrue(reply)
        self.assertNotIn("System Error", reply)


class TimeoutTests(GovernanceBase):
    def test_slow_server_cannot_hang_the_turn_past_llm_timeout(self):
        """A single round whose response takes longer than the governance
        deadline (and the provider's own LLM_TIMEOUT) must fail fast rather
        than hang indefinitely - think() falls back to its existing
        ollama/keyword chain exactly as it already did for any other
        request exception."""
        config.TOOL_LOOP_TIMEOUT_S = 1.0
        with mock.patch.dict(os.environ, {"LLM_TIMEOUT": "1"}):
            import importlib
            # provider config reads LLM_TIMEOUT at WaveBrain() construction time
            self.brain = self.gc.WaveBrain()
            self.srv.script = [Reply(200, completion("too slow"), delay=3.0)]
            t0 = time.monotonic()
            reply = self.brain.think("hello")
            elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 3.0)  # didn't wait out the full 3s server delay
        self.assertTrue(reply)  # fell back to SOMETHING rather than raising out of think()

    def test_timeout_kwarg_never_leaks_into_the_request_body(self):
        """The custom OpenAICompatClient's create() used to shove any unknown
        kwarg straight into the JSON request body (_build_payload's
        **extra handling) - a real API would then 400 on an unrecognized
        "timeout" field. Confirms `timeout` is consumed as a proper request
        option, never serialized into the body Wave actually sends."""
        self.srv.script = [Reply(200, completion("ok"))]
        self.brain.think("hello")
        body = self.srv.posts()[0].json
        self.assertNotIn("timeout", body)


class ValidationTests(GovernanceBase):
    def test_missing_required_argument_is_caught_before_the_handler_runs(self):
        """create_calendar_event requires `title` and `when`. Calling it with
        only `title` used to silently become when="" deep inside
        CalendarManager (which would then fail with a confusing dateparser
        message) - now it's caught immediately as a clear tool result the
        model can react to."""
        self.srv.script = [
            Reply(200, completion(None, [tool_call("create_calendar_event", {"title": "Standup"})])),
            Reply(200, completion("I need a bit more detail to add that event.")),
        ]
        reply = self.brain.think("add standup to my calendar")
        self.assertEqual(reply, "I need a bit more detail to add that event.")
        # the validation message (not a CalendarManager crash) is what the
        # second round's request transcript actually contains
        round2 = self.srv.posts()[1].json["messages"]
        tool_result = next(m["content"] for m in round2 if m.get("role") == "tool")
        self.assertIn("Missing required argument", tool_result)
        self.assertIn("when", tool_result)


if __name__ == "__main__":
    unittest.main()
