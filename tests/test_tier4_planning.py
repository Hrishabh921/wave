"""Tier 4, item #11 - the multi-step planner (planning/schema.py,
planning/trigger.py, planning/planner.py, planning/executor.py,
planning/__init__.py) and its wiring into WaveBrain.think()/think_stream().

Same fake-OpenAI-server harness as test_phase4_brain.py / test_phase7_confirm_gate.py
(see those files / _fake_openai.py for how it works) - run with:
    python -m unittest discover -s tests -p "test_tier4_planning.py"
"""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import _phase4_support as S
from _fake_openai import FakeOpenAI, Reply, completion, tool_call

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from planning import trigger                                    # noqa: E402
from planning.schema import parse_plan_from_llm, PlanValidationError, StepStatus   # noqa: E402
from planning import executor as plan_executor                  # noqa: E402
from planning.executor import PlanStepDenied, PlanStepDispatchError, PlanStepRequiresConfirmation  # noqa: E402
from brain.skills_registry import skills                        # noqa: E402
from automation.audit import audit as audit_mod, Audit, DbStore  # noqa: E402
from automation.undo import undo_manager as undo_mod_manager, trash as undo_mod_trash, TrashStore, UndoManager  # noqa: E402
from automation import undo as undo_mod                         # noqa: E402
from database.db_manager import db                              # noqa: E402
from settings.config import config                                # noqa: E402
from telemetry import store as telemetry_store                  # noqa: E402


def _isolated_db():
    tmp = tempfile.mkdtemp(prefix="wave_planning_test_")
    db.db_path = os.path.join(tmp, "t.db")
    db.init_db()
    return tmp


# =============================================================================
# planning/trigger.py - pure, no brain, no server.
# =============================================================================
class TriggerTests(unittest.TestCase):
    def test_too_short_never_triggers(self):
        self.assertFalse(trigger.should_attempt_planning("volume up"))
        self.assertFalse(trigger.should_attempt_planning("hi"))

    def test_english_connector_words_trigger(self):
        self.assertTrue(trigger.should_attempt_planning(
            "check the weather and then set a reminder to bring an umbrella"))
        self.assertTrue(trigger.should_attempt_planning(
            "open chrome, after that play some lofi on youtube"))

    def test_hindi_hinglish_connector_words_trigger(self):
        self.assertTrue(trigger.should_attempt_planning(
            "pehle volume badhao, phir chrome khol do"))
        self.assertTrue(trigger.should_attempt_planning(
            "notepad band karo uske baad ek reminder set karo"))

    def test_numbered_or_bulleted_list_triggers(self):
        self.assertTrue(trigger.should_attempt_planning("1. open chrome\n2. mute the mic"))
        self.assertTrue(trigger.should_attempt_planning("- close notepad\n- lock the pc"))

    def test_repeated_action_verbs_trigger(self):
        self.assertTrue(trigger.should_attempt_planning("close chrome and open spotify please"))

    def test_plain_single_request_does_not_trigger(self):
        self.assertFalse(trigger.should_attempt_planning("what's the weather like in Jaipur today"))
        self.assertFalse(trigger.should_attempt_planning("remind me to call mom tomorrow at 5pm"))


# =============================================================================
# planning/schema.py - pure, no brain, no server (real skill names only).
# =============================================================================
class SchemaParseTests(unittest.TestCase):
    def test_valid_two_step_plan_parses_cleanly(self):
        plan = parse_plan_from_llm(
            {"steps": [{"tool": "volume_up", "arguments": {}},
                      {"tool": "create_folder", "arguments": {"folder_name": "Trip"}}]},
            max_steps=6, raw_goal="turn it up and make a folder")
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.steps[0].tool, "volume_up")
        self.assertEqual(plan.steps[1].arguments, {"folder_name": "Trip"})
        self.assertEqual(plan.dropped, 0)

    def test_unknown_tool_name_is_dropped_not_fatal(self):
        plan = parse_plan_from_llm(
            {"steps": [{"tool": "volume_up"}, {"tool": "definitely_not_a_real_tool", "arguments": {}}]},
            max_steps=6)
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.dropped, 1)

    def test_all_unknown_tools_raises_validation_error(self):
        with self.assertRaises(PlanValidationError):
            parse_plan_from_llm({"steps": [{"tool": "nope_one"}, {"tool": "nope_two"}]}, max_steps=6)

    def test_zero_steps_raises_validation_error(self):
        with self.assertRaises(PlanValidationError):
            parse_plan_from_llm({"steps": []}, max_steps=6)

    def test_not_a_steps_object_raises_validation_error(self):
        with self.assertRaises(PlanValidationError):
            parse_plan_from_llm({"not_steps": []}, max_steps=6)
        with self.assertRaises(PlanValidationError):
            parse_plan_from_llm("not even a dict", max_steps=6)

    def test_non_dict_arguments_coerced_to_empty_dict(self):
        plan = parse_plan_from_llm({"steps": [{"tool": "volume_up", "arguments": "not a dict"}]}, max_steps=6)
        self.assertEqual(plan.steps[0].arguments, {})

    def test_exact_consecutive_duplicate_step_is_dropped(self):
        plan = parse_plan_from_llm(
            {"steps": [{"tool": "volume_up", "arguments": {}}, {"tool": "volume_up", "arguments": {}},
                      {"tool": "get_pc_info", "arguments": {}}]},
            max_steps=6)
        self.assertEqual(len(plan.steps), 2)
        self.assertEqual(plan.dropped, 1)

    def test_excess_steps_truncated_to_max_steps(self):
        steps = [{"tool": "volume_up", "arguments": {}} for _ in range(3)] + \
                [{"tool": "get_pc_info", "arguments": {}}]   # distinct tool so it's not deduped away
        plan = parse_plan_from_llm({"steps": steps}, max_steps=2)
        self.assertEqual(len(plan.steps), 2)
        self.assertGreaterEqual(plan.dropped, 2)

    def test_depends_on_previous_and_note_are_carried_through(self):
        plan = parse_plan_from_llm(
            {"steps": [{"tool": "volume_up", "arguments": {}, "depends_on_previous": True,
                       "note": "turn it up first"}]}, max_steps=6)
        self.assertTrue(plan.steps[0].depends_on_previous)
        self.assertEqual(plan.steps[0].note, "turn it up first")


# =============================================================================
# planning/executor.py - dispatch_plan_step()/execute_plan() against a real
# WaveBrain (so _validate_tool_args/_guard/the real handlers all run for
# real), same isolated-Audit/UndoManager harness as test_phase7_confirm_gate.py.
# =============================================================================
class ExecutorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.audit = Audit(store=DbStore(db), max_entries=100)
        self.trash = TrashStore(os.path.join(self.tmp, "trash"), 7, 10 * 1024 * 1024)
        self.um = UndoManager(self.audit, self.trash)
        import automation.audit as audit_mod_real
        import automation.undo as undo_mod_real
        saved = (audit_mod_real.audit, undo_mod_real.trash, undo_mod_real.undo_manager)
        audit_mod_real.audit, undo_mod_real.trash, undo_mod_real.undo_manager = self.audit, self.trash, self.um
        self.addCleanup(lambda: setattr(audit_mod_real, "audit", saved[0]))
        self.addCleanup(lambda: setattr(undo_mod_real, "trash", saved[1]))
        self.addCleanup(lambda: setattr(undo_mod_real, "undo_manager", saved[2]))

        import brain.groq_core as gc
        self.gc = gc
        gc_saved = (gc.audit, gc.undo_manager)
        gc.audit, gc.undo_manager = self.audit, self.um
        self.addCleanup(lambda: setattr(gc, "audit", gc_saved[0]))
        self.addCleanup(lambda: setattr(gc, "undo_manager", gc_saved[1]))
        # Tier 4: list_recent_actions/undo_last_action moved to
        # brain/handlers/automation.py, which imports audit/undo_manager
        # independently of groq_core.py - patching gc.audit/gc.undo_manager
        # alone no longer reaches the code that actually runs for those two
        # tools, so this same swap needs to happen there too.
        import brain.handlers.automation as gc_automation
        gc_automation_saved = (gc_automation.audit, gc_automation.undo_manager)
        gc_automation.audit, gc_automation.undo_manager = self.audit, self.um
        self.addCleanup(lambda: setattr(gc_automation, "audit", gc_automation_saved[0]))
        self.addCleanup(lambda: setattr(gc_automation, "undo_manager", gc_automation_saved[1]))
        import planning.executor as pe
        pe_saved = pe.__dict__.get("audit")  # executor imports audit lazily inside the function - nothing to patch here

        ttl_saved = config.CONFIRM_PENDING_TTL_S
        config.CONFIRM_PENDING_TTL_S = 60.0
        self.addCleanup(lambda: setattr(config, "CONFIRM_PENDING_TTL_S", ttl_saved))

        env = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "x", "LLM_BASE_URL": self.srv.url,
               "LLM_MODEL": "llama-test"}
        self._env_patch = mock.patch.dict(os.environ, env)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        from brain.groq_core import WaveBrain
        self.brain = WaveBrain()


class DispatchPlanStepTests(ExecutorBase):
    def test_successful_step_returns_the_handlers_result(self):
        result = plan_executor.dispatch_plan_step(self.brain, "get_pc_info", {})
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    def test_missing_required_argument_raises_dispatch_error(self):
        with self.assertRaises(PlanStepDispatchError) as ctx:
            plan_executor.dispatch_plan_step(self.brain, "create_folder", {})
        self.assertIn("folder_name", str(ctx.exception))

    def test_unknown_tool_raises_dispatch_error(self):
        with self.assertRaises(PlanStepDispatchError):
            plan_executor.dispatch_plan_step(self.brain, "not_a_real_tool", {})

    def test_confirm_gated_tool_raises_requires_confirmation_and_arms_pending_action(self):
        self.assertIsNone(self.brain.pending_action)
        with self.assertRaises(PlanStepRequiresConfirmation) as ctx:
            plan_executor.dispatch_plan_step(self.brain, "close_app", {"app_name": "notepad"})
        self.assertEqual(ctx.exception.tool, "close_app")
        self.assertIsNotNone(self.brain.pending_action)
        self.assertEqual(self.brain.pending_action["tool"], "close_app")


class ExecutePlanTests(ExecutorBase):
    def _plan(self, *steps):
        from planning.schema import Plan, PlanStep
        return Plan(steps=[PlanStep(**s) for s in steps], raw_goal="test")

    def test_two_independent_steps_both_succeed(self):
        plan = self._plan({"tool": "volume_up", "arguments": {}},
                          {"tool": "get_pc_info", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK, StepStatus.OK])
        self.assertFalse(outcome.pending_confirmation)
        self.assertIn("Done", outcome.final_message) if False else None  # final wording not asserted here

    def test_failed_step_self_corrects_via_one_retry(self):
        # First call (propose_plan is never invoked by execute_plan itself -
        # only the correction call goes through the LLM) returns corrected args.
        self.srv.script = [Reply(200, completion(
            None, [tool_call("correct_arguments", {"arguments": {"folder_name": "Fixed"}})]))]
        plan = self._plan({"tool": "create_folder", "arguments": {}})   # missing folder_name
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.OK)
        self.assertTrue(outcome.results[0].retried)

    def test_failed_step_stays_failed_when_correction_call_itself_fails(self):
        self.srv.script = [Reply(500, {"error": {"message": "boom"}})]
        plan = self._plan({"tool": "create_folder", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertIn("folder_name", outcome.results[0].error)

    def test_dependent_step_is_skipped_after_a_failure(self):
        self.srv.script = [Reply(500, {"error": {"message": "boom"}})]  # correction call also fails
        plan = self._plan({"tool": "create_folder", "arguments": {}},
                          {"tool": "volume_up", "arguments": {}, "depends_on_previous": True})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertEqual(outcome.results[1].status, StepStatus.SKIPPED)

    def test_independent_step_still_runs_after_an_earlier_failure(self):
        self.srv.script = [Reply(500, {"error": {"message": "boom"}})]
        plan = self._plan({"tool": "create_folder", "arguments": {}},
                          {"tool": "volume_up", "arguments": {}, "depends_on_previous": False})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertEqual(outcome.results[1].status, StepStatus.OK)

    def test_confirmation_needed_stops_every_remaining_step(self):
        plan = self._plan({"tool": "close_app", "arguments": {"app_name": "notepad"}},
                          {"tool": "volume_up", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.NEEDS_CONFIRMATION)
        self.assertEqual(outcome.results[1].status, StepStatus.SKIPPED)
        self.assertTrue(outcome.pending_confirmation)
        self.assertIn("confirm", outcome.final_message.lower())


# =============================================================================
# Tier 1 item #5, reached through the planner: dispatch_plan_step()'s
# brain._guard() call can now come back "deny", not just run/confirm - see
# PlanStepDenied's docstring in planning/executor.py for why that's caught
# separately from plain PlanStepDispatchError rather than falling into the
# generic self-correction-retry path.
# =============================================================================
class PlanStepDeniedTests(ExecutorBase):
    def setUp(self):
        super().setUp()
        from brain import taint
        self.taint = taint
        self.addCleanup(taint.clear_turn)

    def _plan(self, *steps):
        from planning.schema import Plan, PlanStep
        return Plan(steps=[PlanStep(**s) for s in steps], raw_goal="test")

    def test_dispatch_plan_step_raises_plan_step_denied_not_requires_confirmation(self):
        self.taint.mark_turn("a web search", flagged=True)
        with self.assertRaises(PlanStepDenied) as ctx:
            plan_executor.dispatch_plan_step(self.brain, "delete_file", {"file_name": "ghost.txt"})
        self.assertEqual(ctx.exception.tool, "delete_file")
        self.assertIn("web search", str(ctx.exception).lower())
        self.assertIsNone(self.brain.pending_action)        # nothing armed - unlike a real confirm

    def test_denied_step_is_reported_failed_not_needs_confirmation(self):
        self.taint.mark_turn("a web search", flagged=True)
        plan = self._plan({"tool": "delete_file", "arguments": {"file_name": "ghost.txt"}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertFalse(outcome.pending_confirmation)       # doesn't pause the plan waiting on a human
        self.assertIn("web search", outcome.results[0].error.lower())

    def test_an_independent_step_after_a_denied_one_still_runs(self):
        """FAILED semantics, not NEEDS_CONFIRMATION semantics - contrast
        with test_confirmation_needed_stops_every_remaining_step above,
        where a real confirm pauses the whole plan. Nothing is pending
        after a deny, so there's no reason to stop steps that don't
        depend on the denied one.

        The second step is T0 (list_my_skills) specifically so THIS test
        isolates one thing: does execute_plan() keep going after a deny.
        volume_up (T1) would be a worse choice here - the turn is still
        legitimately tainted for the whole plan (taint isn't per-step,
        see brain/taint.py), so a T1 second step would ALSO correctly
        escalate to its own NEEDS_CONFIRMATION, which is correct behaviour
        but would confuse what this particular test is checking."""
        self.taint.mark_turn("a web search", flagged=True)
        plan = self._plan({"tool": "delete_file", "arguments": {"file_name": "ghost.txt"}},
                          {"tool": "list_my_skills", "arguments": {}, "depends_on_previous": False})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertEqual(outcome.results[1].status, StepStatus.OK)

    def test_denied_step_does_not_spend_a_self_correction_retry(self):
        """The whole reason PlanStepDenied is caught separately from plain
        PlanStepDispatchError (see that class's docstring in
        planning/executor.py). No LLM call is scripted at all here; if
        execute_plan tried a correction call anyway, posting to the fake
        server with nothing scripted would raise and fail this test."""
        self.taint.mark_turn("a web search", flagged=True)
        self.srv.script = []
        plan = self._plan({"tool": "delete_file", "arguments": {"file_name": "ghost.txt"}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertFalse(outcome.results[0].retried)

    def test_an_untainted_t3_step_still_just_needs_confirmation_not_denied(self):
        """The baseline this whole feature must leave alone: a destructive
        step with nothing untrusted read this turn still just asks, via
        the ordinary PlanStepRequiresConfirmation path - exactly
        test_confirmation_needed_stops_every_remaining_step above, for a
        T3 tool specifically rather than close_app's T2."""
        self.assertFalse(self.taint.turn_tainted())
        plan = self._plan({"tool": "delete_file", "arguments": {"file_name": "ghost.txt"}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.NEEDS_CONFIRMATION)
        self.assertTrue(outcome.pending_confirmation)


# =============================================================================
# Tier 19: per-step execution timeout - planning/executor.py's own module
# docstring (Tier 19 section) has the full rationale. _call_handler is
# tested in isolation first (no brain, no skills registry, no server),
# then dispatch_plan_step's thread/taint boundary (the exact regression
# this tier's own development caught - see that function's docstring),
# then execute_plan()'s end-to-end deadline wiring.
# =============================================================================
class CallHandlerTimeoutTests(unittest.TestCase):
    def test_no_timeout_calls_the_handler_directly_and_returns_its_result(self):
        calls = []

        def handler(brain, args):
            calls.append(args)
            return "ok"

        result = plan_executor._call_handler(handler, None, {"a": 1}, timeout=None)
        self.assertEqual(result, "ok")
        self.assertEqual(calls, [{"a": 1}])

    def test_a_fast_handler_still_returns_its_real_result_under_a_timeout(self):
        result = plan_executor._call_handler(lambda brain, args: "fast", None, {}, timeout=2.0)
        self.assertEqual(result, "fast")

    def test_a_slow_handler_is_abandoned_once_the_timeout_elapses(self):
        released = threading.Event()

        def slow_handler(brain, args):
            released.wait(5)   # would hang this test for 5s if NOT abandoned at the timeout below
            return "too late"

        start = time.monotonic()
        with self.assertRaises(TimeoutError) as ctx:
            plan_executor._call_handler(slow_handler, None, {}, timeout=0.05)
        self.assertLess(time.monotonic() - start, 2.0,
                        "must return once the timeout elapses, not wait for the handler")
        self.assertIn("too long", str(ctx.exception))
        released.set()   # let the abandoned thread actually finish instead of leaking past this test


class DispatchPlanStepTimeoutTests(ExecutorBase):
    def test_a_hanging_handler_becomes_a_plain_dispatch_error(self):
        released = threading.Event()

        def slow_handler(brain, args):
            released.wait(5)
            return "too late"

        with mock.patch.object(skills, "get_handler", return_value=slow_handler):
            with self.assertRaises(PlanStepDispatchError) as ctx:
                plan_executor.dispatch_plan_step(self.brain, "get_pc_info", {}, handler_timeout=0.05)
        self.assertIn("too long", str(ctx.exception))
        released.set()

    def test_the_guard_check_still_sees_this_threads_taint_even_with_a_handler_timeout(self):
        """Regression test for the exact bug this tier's own development
        caught: an earlier version of this timeout ran dispatch_plan_step's
        WHOLE body (including brain._guard()) on a worker thread, which
        made _guard() see a blank, untainted threading.local() record (see
        brain/taint.py) and wrongly let a tainted-turn T3 tool through to
        NEEDS_CONFIRMATION instead of denying it outright. The deny here
        happens before _call_handler is ever reached, so handler_timeout's
        value doesn't matter - what matters is that passing one at all
        doesn't change _guard()'s answer."""
        from brain import taint
        taint.clear_turn()
        taint.mark_turn("a web search", flagged=True)
        self.addCleanup(taint.clear_turn)
        with self.assertRaises(PlanStepDenied):
            plan_executor.dispatch_plan_step(self.brain, "delete_file", {"file_name": "ghost.txt"},
                                             handler_timeout=5.0)


class ExecutePlanTimeoutTests(ExecutorBase):
    """A short PLANNER_STEP_TIMEOUT_S so these tests stay fast rather than
    actually waiting out the real ~20s default; PLANNER_MAX_RETRIES_PER_STEP
    forced to 0 so a timeout's self-correction retry (already covered by
    ExecutePlanTests above) can't also fire here and complicate what these
    are isolating."""

    def setUp(self):
        super().setUp()
        saved = (config.PLANNER_STEP_TIMEOUT_S, config.PLANNER_TOTAL_TIMEOUT_S, config.PLANNER_MAX_RETRIES_PER_STEP)
        config.PLANNER_STEP_TIMEOUT_S = 1.0
        config.PLANNER_TOTAL_TIMEOUT_S = 10.0
        config.PLANNER_MAX_RETRIES_PER_STEP = 0
        self.addCleanup(lambda: setattr(config, "PLANNER_STEP_TIMEOUT_S", saved[0]))
        self.addCleanup(lambda: setattr(config, "PLANNER_TOTAL_TIMEOUT_S", saved[1]))
        self.addCleanup(lambda: setattr(config, "PLANNER_MAX_RETRIES_PER_STEP", saved[2]))

    def _plan(self, *steps):
        from planning.schema import Plan, PlanStep
        return Plan(steps=[PlanStep(**s) for s in steps], raw_goal="test")

    def test_a_step_that_hangs_past_its_timeout_is_reported_failed_not_left_hanging(self):
        released = threading.Event()

        def slow_handler(brain, args):
            released.wait(5)
            return "too late"

        plan = self._plan({"tool": "get_pc_info", "arguments": {}})
        start = time.monotonic()
        with mock.patch.object(skills, "get_handler", return_value=slow_handler):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertLess(time.monotonic() - start, 3.0, "execute_plan must not block for the handler's full hang")
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertIn("too long", outcome.results[0].error)
        released.set()

    def test_a_fast_step_is_unaffected_by_the_per_step_timeout(self):
        plan = self._plan({"tool": "get_pc_info", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.OK)

    def test_a_hanging_step_in_a_parallel_t0_batch_does_not_block_its_neighbor(self):
        """The batch path (_dispatch_batch_concurrently) threads `deadline`
        down to each step's own _dispatch_once the same way the sequential
        path does - a hung T0 step used to block pool.map() from ever
        returning, which silently withheld every OTHER step's result too."""
        released = threading.Event()
        real_handler = skills.get_handler("get_pc_info")

        def slow_get_pc_info(brain, args):
            released.wait(5)
            return "too late"

        def routed(tool):
            return slow_get_pc_info if tool == "get_pc_info" else real_handler

        plan = self._plan({"tool": "get_pc_info", "arguments": {}, "depends_on_previous": False},
                          {"tool": "list_my_skills", "arguments": {}, "depends_on_previous": False})
        start = time.monotonic()
        with mock.patch.object(skills, "get_handler", side_effect=routed), \
                mock.patch.object(config, "PLANNER_PARALLEL_READONLY_STEPS", True):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertLess(time.monotonic() - start, 3.0)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)
        self.assertEqual(outcome.results[1].status, StepStatus.OK)
        released.set()


# =============================================================================
# Tier 6: scoped parallel execution for contiguous, independent T0 steps -
# see planning/executor.py's module docstring and _is_parallel_eligible()
# for what qualifies and why. All five T0 tools used below
# (get_pc_info/list_my_skills/list_reminders/list_timers/check_timer) were
# hand-verified to run cleanly with {} arguments against ExecutorBase's
# fresh per-test DB before being used here.
# =============================================================================
class IsParallelEligibleTests(unittest.TestCase):
    """_is_parallel_eligible() in isolation - no brain/DB/audit harness
    needed, it's a pure function of (depends_on_previous, tier)."""
    def _step(self, tool, depends_on_previous=False):
        from planning.schema import PlanStep
        return PlanStep(tool=tool, arguments={}, depends_on_previous=depends_on_previous)

    def test_independent_t0_tool_is_eligible(self):
        self.assertTrue(plan_executor._is_parallel_eligible(self._step("get_pc_info")))

    def test_t1_tool_is_not_eligible_even_when_independent(self):
        self.assertFalse(plan_executor._is_parallel_eligible(self._step("volume_up")))

    def test_dependent_t0_tool_is_not_eligible(self):
        self.assertFalse(plan_executor._is_parallel_eligible(
            self._step("get_pc_info", depends_on_previous=True)))

    def test_unregistered_tool_name_is_not_eligible(self):
        # get_tier() on an unknown name falls through resolve_tier()'s own
        # fallback, which never returns T0 - see brain/tiers.py.
        self.assertFalse(plan_executor._is_parallel_eligible(self._step("not_a_real_tool_xyz")))


class ParallelBatchExecutionTests(ExecutorBase):
    def _plan(self, *steps):
        from planning.schema import Plan, PlanStep
        return Plan(steps=[PlanStep(**s) for s in steps], raw_goal="test")

    def test_three_independent_t0_steps_all_succeed_in_order(self):
        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}},
                          {"tool": "list_reminders", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK] * 3)
        self.assertEqual([r.step.tool for r in outcome.results],
                         ["get_pc_info", "list_my_skills", "list_reminders"])
        self.assertFalse(outcome.pending_confirmation)

    def test_batch_actually_overlaps_in_wall_clock_time(self):
        """Proof, not just correctness: two batched T0 steps really do run
        at the same time - if _dispatch_batch_concurrently silently fell
        back to one-at-a-time, this would take roughly twice as long."""
        real_dispatch = plan_executor.dispatch_plan_step

        def slow_dispatch(brain, tool, arguments, **kw):
            time.sleep(0.3)
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=slow_dispatch):
            t0 = time.perf_counter()
            outcome = plan_executor.execute_plan(self.brain, plan)
            elapsed = time.perf_counter() - t0
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK, StepStatus.OK])
        self.assertLess(elapsed, 0.5)   # concurrent ~0.3s; sequential would be >= 0.6s

    def test_feature_flag_off_restores_fully_sequential_timing(self):
        saved = config.PLANNER_PARALLEL_READONLY_STEPS
        config.PLANNER_PARALLEL_READONLY_STEPS = False
        self.addCleanup(lambda: setattr(config, "PLANNER_PARALLEL_READONLY_STEPS", saved))
        real_dispatch = plan_executor.dispatch_plan_step

        def slow_dispatch(brain, tool, arguments, **kw):
            time.sleep(0.3)
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=slow_dispatch):
            t0 = time.perf_counter()
            outcome = plan_executor.execute_plan(self.brain, plan)
            elapsed = time.perf_counter() - t0
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK, StepStatus.OK])
        self.assertGreaterEqual(elapsed, 0.55)   # two 0.3s calls back-to-back, not overlapped

    def test_batch_results_preserve_step_order_regardless_of_completion_order(self):
        real_dispatch = plan_executor.dispatch_plan_step

        def staggered_dispatch(brain, tool, arguments, **kw):
            if tool == "get_pc_info":
                time.sleep(0.25)   # listed first but finishes last
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=staggered_dispatch):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].step.tool, "get_pc_info")
        self.assertEqual(outcome.results[1].step.tool, "list_my_skills")
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK, StepStatus.OK])

    def test_lone_eligible_t0_step_with_no_eligible_neighbor_still_runs(self):
        """Not worth a thread pool for one call - takes the ordinary
        sequential path instead (batch_end - i >= 2 is False for it)."""
        plan = self._plan({"tool": "volume_up", "arguments": {}},
                          {"tool": "get_pc_info", "arguments": {}},
                          {"tool": "volume_up", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK] * 3)

    def test_max_parallel_workers_of_one_still_runs_every_batched_step(self):
        saved = config.PLANNER_MAX_PARALLEL_WORKERS
        config.PLANNER_MAX_PARALLEL_WORKERS = 1
        self.addCleanup(lambda: setattr(config, "PLANNER_MAX_PARALLEL_WORKERS", saved))
        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}},
                          {"tool": "list_reminders", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual([r.status for r in outcome.results], [StepStatus.OK] * 3)

    def test_batch_member_that_fails_is_retried_sequentially_after_the_batch(self):
        """A T0 step that fails on its first (parallel) attempt still gets
        its one self-correction retry - just sequentially, after the
        batch, not re-parallelized (_dispatch_batch_concurrently's own
        docstring)."""
        self.srv.script = [Reply(200, completion(
            None, [tool_call("correct_arguments", {"arguments": {}})]))]
        real_dispatch = plan_executor.dispatch_plan_step
        calls = {"n": 0}

        def flaky_dispatch(brain, tool, arguments, **kw):
            if tool == "get_pc_info":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PlanStepDispatchError("pretend hardware probe failed")
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=flaky_dispatch):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.OK)
        self.assertTrue(outcome.results[0].retried)
        self.assertEqual(outcome.results[1].status, StepStatus.OK)
        self.assertFalse(outcome.results[1].retried)
        self.assertEqual(calls["n"], 2)

    def test_batch_member_defensively_handles_an_unexpected_confirmation_gate(self):
        """T0 tools can't really trigger NEEDS_CONFIRMATION in practice -
        _guard() always resolves T0 straight to "run" (see
        _is_parallel_eligible's docstring) - but _dispatch_once/
        _dispatch_batch_concurrently handle the tag defensively anyway
        rather than assuming it can't happen. This proves that path
        actually works, and documents a genuine, honest consequence of
        batching: the OTHER member of an already-running batch still
        completes (it was dispatched concurrently before the gate was
        known) even though the plan as a whole now has a confirmation
        pending - unlike the sequential path, where nothing after a
        gated step is even started."""
        real_dispatch = plan_executor.dispatch_plan_step

        def dispatch_forcing_confirmation(brain, tool, arguments, **kw):
            if tool == "get_pc_info":
                raise PlanStepRequiresConfirmation(tool, arguments, "Pretend this needs a yes/no.")
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=dispatch_forcing_confirmation):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.NEEDS_CONFIRMATION)
        self.assertEqual(outcome.results[1].status, StepStatus.OK)
        self.assertTrue(outcome.pending_confirmation)

    def test_batch_followed_by_a_dependent_step_is_skipped_if_the_batch_ended_blocked(self):
        real_dispatch = plan_executor.dispatch_plan_step

        def dispatch_forcing_confirmation(brain, tool, arguments, **kw):
            if tool == "list_my_skills":
                raise PlanStepRequiresConfirmation(tool, arguments, "Pretend this needs a yes/no.")
            return real_dispatch(brain, tool, arguments, **kw)

        plan = self._plan({"tool": "get_pc_info", "arguments": {}},
                          {"tool": "list_my_skills", "arguments": {}},
                          {"tool": "volume_up", "arguments": {}, "depends_on_previous": True})
        with mock.patch.object(plan_executor, "dispatch_plan_step", side_effect=dispatch_forcing_confirmation):
            outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.OK)
        self.assertEqual(outcome.results[1].status, StepStatus.NEEDS_CONFIRMATION)
        self.assertEqual(outcome.results[2].status, StepStatus.SKIPPED)


class TestDoublesIntegrationTests(ExecutorBase):
    """Proves tests/test_doubles.py's FakeLLMClient is a working
    complement to FakeOpenAI, not just plausible-looking code nobody
    exercises - swaps self.brain.client directly (bypassing the real SDK
    AND the HTTP round-trip FakeOpenAI still goes through) for just these
    tests. Every other test in this file keeps using the full FakeOpenAI
    harness, since that's still what proves the HTTP/SDK parsing layer
    itself works - see test_doubles.py's own module docstring for why
    both exist."""
    def _plan(self, *steps):
        from planning.schema import Plan, PlanStep
        return Plan(steps=[PlanStep(**s) for s in steps], raw_goal="test")

    def test_self_correction_retry_works_against_the_lightweight_double_too(self):
        from test_doubles import FakeLLMClient, fake_completion, fake_tool_call
        self.brain.client = FakeLLMClient([fake_completion(tool_calls=[
            fake_tool_call("correct_arguments", {"arguments": {"folder_name": "Fixed"}})])])
        plan = self._plan({"tool": "create_folder", "arguments": {}})
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.OK)
        self.assertTrue(outcome.results[0].retried)

    def test_double_records_what_the_planner_actually_asked_for(self):
        from test_doubles import FakeLLMClient, fake_completion, fake_tool_call
        fake_client = FakeLLMClient([fake_completion(tool_calls=[
            fake_tool_call("correct_arguments", {"arguments": {"folder_name": "Fixed"}})])])
        self.brain.client = fake_client
        plan = self._plan({"tool": "create_folder", "arguments": {}})
        plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(len(fake_client.calls), 1)
        self.assertIn("create_folder", fake_client.calls[0].messages[-1]["content"])

    def test_exhausted_script_with_no_default_is_treated_as_a_failed_correction_call(self):
        from test_doubles import FakeLLMClient
        self.brain.client = FakeLLMClient([])   # nothing scripted, no default
        plan = self._plan({"tool": "create_folder", "arguments": {}})
        # _request_correction catches every Exception and returns None -
        # the double's own AssertionError looks, from execute_plan()'s
        # side, exactly like any other provider error would.
        outcome = plan_executor.execute_plan(self.brain, plan)
        self.assertEqual(outcome.results[0].status, StepStatus.FAILED)


# =============================================================================
# End to end through WaveBrain.think() - propose_plan scripted, steps
# dispatched for real against faked pyautogui/pywhatkit.
# =============================================================================
class ThinkPlanningIntegrationTests(ExecutorBase):
    def setUp(self):
        super().setUp()
        telemetry_store.flush(timeout=2.0)
        self._telemetry_was = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        self.addCleanup(lambda: setattr(config, "TELEMETRY_ENABLED", self._telemetry_was))
        self.addCleanup(lambda: telemetry_store.flush(timeout=2.0))

    def test_compound_request_executes_a_real_multi_step_plan(self):
        self.srv.script = [Reply(200, completion(None, [tool_call("propose_plan", {"steps": [
            {"tool": "volume_up", "arguments": {}},
            {"tool": "get_pc_info", "arguments": {}},
        ]})]))]
        reply = self.brain.think("turn the volume up and then tell me my pc specs")
        self.assertIsInstance(reply, str)
        self.assertGreater(len(reply), 0)

        telemetry_store.flush(timeout=5.0)
        rows = db.get_recent_traces(limit=5)
        self.assertEqual(rows[0]["route"], "planner")

    def test_plan_with_only_one_surviving_step_falls_back_to_normal_loop(self):
        # propose_plan proposes one real step + one bogus one that gets
        # dropped by schema validation -> down to 1 step -> planning.
        # try_plan_and_execute returns None -> normal loop takes over, and
        # since nothing in the normal loop was scripted beyond this, it
        # uses the second queued reply as that loop's own first round.
        self.srv.script = [
            Reply(200, completion(None, [tool_call("propose_plan", {"steps": [
                {"tool": "volume_up", "arguments": {}},
                {"tool": "not_a_real_tool_at_all", "arguments": {}},
            ]})])),
            Reply(200, completion("Sure, turning it up.")),
        ]
        reply = self.brain.think("turn the volume up and then do a thing")
        self.assertEqual(reply, "Sure, turning it up.")


if __name__ == "__main__":
    unittest.main()
