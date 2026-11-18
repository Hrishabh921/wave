"""
planning/executor.py — Tier 4 item #11: runs a validated Plan step by step
through WaveBrain's real dispatch path, with one self-correction retry per
failed step and a bounded total time budget. Tier 6 item adds scoped
parallel execution for read-only steps - see "Tier 6" below.
================================================================================
Simplified from SARA's sara/core/planning/executor.py in one deliberate
way, UNCHANGED since Tier 4: steps with real side effects still run
strictly in order, never batched for parallel execution. A large share of
Wave's tools touch shared, stateful things on the SAME machine the person
is sitting at (open/close apps and windows, write/delete files, toggle
hardware, send a WhatsApp message) where running two at once adds real
risk (races, confusing on-screen results) for a latency win that barely
matters at Wave's PLANNER_MAX_STEPS=6 ceiling. What's kept at full
fidelity from SARA's design - because the person explicitly asked for
these two - is schema validation (planning/schema.py, enforced again per
step via dispatch_plan_step below) and the retry: exactly one
self-correction attempt per failed step, never more.

Tier 6: scoped parallel execution for READ-ONLY steps
-------------------------------------------------------
The roadmap asked for exactly what the paragraph above says was deferred:
"steps without depends_on_previous can run concurrently via a thread
pool... same validated-plan contract." Implementing that literally -
every independent step, any tier - would reintroduce precisely the
shared-state races the paragraph above exists to avoid; it was deferred
for a reason, not an oversight, and that reason hasn't changed. What HAS
changed is that Tier 1 gave every tool a reviewed risk tier
(brain/tiers.py), which makes a safe middle ground possible: a step at
T0 (read-only, no side effect at all, by definition) carries none of the
race risk above, and brain/groq_core.py's _guard() already resolves T0
unconditionally to "run" - no gating, no taint check, regardless of
anything else happening in the turn. So a contiguous run of 2+
depends_on_previous=False steps that are ALL T0 now dispatches through a
small thread pool (_dispatch_batch_concurrently below); anything else -
any step with a real side effect (T1 and up), or one the model marked
dependent - still runs exactly as it always has, one at a time, in order.
A hot-loaded user skill can never qualify for this either: brain/tiers.py's
resolve_tier() (the ONLY path that can assign a tier to something outside
the committed table) never returns T0 for anything not explicitly reviewed
and placed there - see that module's own docstring - so only a tool a
human actually reviewed and classified T0 is ever eligible.
PLANNER_PARALLEL_READONLY_STEPS=false restores fully-sequential execution
for every step regardless of tier (the exact pre-Tier-6 behaviour).

Tier 19: a per-step execution timeout
--------------------------------------
Until now, execute_plan()'s own PLANNER_TOTAL_TIMEOUT_S deadline was only
ever CHECKED before starting a new step/batch - nothing bounded a step
once it was already dispatched. A single hanging handler (a skill with
its own unbounded network call, a bug in a tool) could therefore block
the rest of the plan, and the whole turn, forever - the deadline would
never get a chance to fire again because the loop was never going to come
back around to check it. _dispatch_once now also takes the plan's
absolute `deadline`, runs the actual dispatch in a one-off worker thread,
and bounds the wait to min(PLANNER_STEP_TIMEOUT_S, time left until
deadline) - turning a timeout into an ordinary "failed" tag, which
_retry_after_batch_failure/_run_step_with_retry already know how to
handle exactly like any other dispatch failure (including the one
self-correction retry, itself bounded the same way). Python has no safe
way to force a thread to stop, so a genuinely stuck handler's thread is
abandoned (pool.shutdown(wait=False)) rather than killed - no worse than
a hung network call anywhere else in this codebase, just bounded now from
the PLAN's perspective instead of silently eating the whole turn.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from brain.tiers import T0, get_tier
from planning.schema import Plan, PlanStep, PlanOutcome, StepResult, StepStatus
from settings.config import config
from telemetry import trace

logger = logging.getLogger("planning.executor")

_CORRECTION_TOOL_NAME = "correct_arguments"
_CORRECTION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": _CORRECTION_TOOL_NAME,
        "description": "Propose corrected arguments for the tool that just failed.",
        "parameters": {
            "type": "object",
            "properties": {"arguments": {"type": "object", "description": "The corrected arguments, by name."}},
            "required": ["arguments"],
        },
    },
}


class PlanStepRequiresConfirmation(Exception):
    """Raised by dispatch_plan_step when the step's tool is confirm-gated.
    brain._guard() has already armed brain.pending_action by the time this
    is raised (see dispatch_plan_step) - the plan stops here, and the
    person's next "yes"/"no" is picked up by the normal
    _check_pending_confirmation() flow at the top of their next think()
    call, exactly as if a single AI-chosen tool call had been gated."""
    def __init__(self, tool: str, arguments: Dict[str, Any], prompt: str):
        super().__init__(prompt)
        self.tool = tool
        self.arguments = arguments
        self.prompt = prompt


class PlanStepDispatchError(Exception):
    """Raised by dispatch_plan_step for every other kind of failure (unknown
    tool, a missing required argument, or the handler itself raising) -
    `str(this)` is the short, human-readable reason execute_plan hands to
    the one self-correction retry."""


class PlanStepDenied(PlanStepDispatchError):
    """Raised by dispatch_plan_step when brain._guard() refuses the step
    outright instead of asking (a "deny" GuardOutcome - Tier 1 item #5): a
    T3 destructive tool, with this turn having already read something
    untrusted (brain/taint.py). A PlanStepDispatchError subclass - so
    anything that only ever expected that type still catches this - but
    caught separately, and FIRST, in execute_plan() so the step becomes
    FAILED WITHOUT the usual self-correction retry: _request_correction()
    only changes arguments, and different arguments can't un-taint a turn,
    so retrying would just spend an LLM call reaching this exact same
    denial a second time."""
    def __init__(self, tool: str, arguments: Dict[str, Any], message: str):
        super().__init__(message)
        self.tool = tool
        self.arguments = arguments
        self.message = message


def dispatch_plan_step(brain, tool: str, arguments: Dict[str, Any], *, source: str = "planner",
                        handler_timeout: Optional[float] = None) -> str:
    """The planner's own dispatch chokepoint - mirrors
    WaveBrain._dispatch_tool() EXACTLY (same handler lookup, same
    brain._validate_tool_args(), same brain._guard(), same
    audit.action(..., adoptable=True) scope, same handler(brain, arguments)
    call) but signals outcome via exceptions instead of a result string, so
    execute_plan() below never has to string-sniff a result to know
    whether a step actually worked - it can just catch the right
    exception. Every check here calls into WaveBrain's real methods, never
    reimplements them, so a planner-dispatched step is validated, gated,
    and audited identically to an AI tool-calling or intent_router-routed
    call - only the signaling shape (raise vs. return-string) differs, and
    only because this needs a reliable decision point the normal string
    contract would be fragile to parse out of (see test_tier4_planning.py
    for exactly what "fragile" would mean here: a legitimate successful
    reply from a weather/notes tool can easily contain words like "cloudy"
    or "couldn't reach" as DATA, not as a failure signal).

    Tier 19: handler_timeout (seconds, optional) bounds ONLY the handler
    call itself, via _call_handler below - deliberately NOT this whole
    function. skills.get_handler, _validate_tool_args, and above all
    brain._guard() all stay on the CALLING thread, because _guard()
    depends on brain/taint.py's THREAD-LOCAL per-turn taint record (see
    that module's docstring - "a thread-local record means none of them
    can taint another's in-flight turn", which cuts both ways: run the
    gate check itself on a DIFFERENT thread and it sees a blank slate,
    silently approving or merely confirming a T3 tool a tainted turn
    should have denied outright). An earlier version of this timeout ran
    the ENTIRE function on a worker thread and broke exactly that -
    test_tier4_planning.py's PlanStepDeniedTests caught it. Only
    handler(brain, arguments) - the one call that can genuinely hang (a
    skill's own unbounded network call, a bug) - is ever bounded this
    way."""
    from brain.skills_registry import skills
    from automation.audit import audit

    handler = skills.get_handler(tool)
    if handler is None:
        raise PlanStepDispatchError(f"I don't have a '{tool}' tool available.")

    validation_error = brain._validate_tool_args(tool, arguments)
    if validation_error:
        raise PlanStepDispatchError(validation_error)

    with audit.action(tool, arguments, source=source, adoptable=True) as act:
        gate = brain._guard(tool, arguments, source=source)
        if gate.action == "deny":
            act.blocked(gate.message)
            raise PlanStepDenied(tool, dict(arguments), gate.message)
        if gate.action != "run":
            act.blocked(gate.message)
            raise PlanStepRequiresConfirmation(tool, dict(arguments), gate.message)
        try:
            result = _call_handler(handler, brain, arguments, timeout=handler_timeout)
            act.done(result)
            return result
        except (PlanStepRequiresConfirmation, PlanStepDispatchError):
            raise
        except Exception as e:
            act.fail(f"Something went wrong while trying to do that: {e}")
            raise PlanStepDispatchError(str(e)) from e


def _call_handler(handler, brain, arguments: Dict[str, Any], *, timeout: Optional[float]):
    """Tier 19: calls handler(brain, arguments) directly when timeout is
    None (every caller outside execute_plan()'s own bounded path, and
    every pre-Tier-19 test) - identical to how this always worked. With a
    timeout, runs the SAME call on a one-off worker thread instead and
    raises plain TimeoutError if it doesn't return in time - deliberately
    NOT caught here, so it reaches dispatch_plan_step's own
    `except Exception as e:` and becomes an ordinary PlanStepDispatchError,
    exactly like any other handler failure. No new tag anywhere
    downstream, and nothing above this one call ever touches a different
    thread - see dispatch_plan_step's docstring for why that boundary
    matters. The abandoned thread (pool.shutdown(wait=False)) is a known,
    accepted tradeoff: Python has no safe way to force a thread to stop,
    so a genuinely stuck handler's eventual real result, if any, is simply
    discarded rather than waited for."""
    if timeout is None:
        return handler(brain, arguments)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(handler, brain, arguments)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"That took too long (over {timeout:.0f}s) and was stopped.") from None
    finally:
        pool.shutdown(wait=False)


def _request_correction(brain, step: PlanStep, error_message: str) -> Optional[Dict[str, Any]]:
    """One bounded LLM call asking for corrected arguments for ONE failed
    step. Returns the corrected arguments dict, or None if the call
    itself fails or doesn't produce a usable dict - execute_plan() then
    marks the step FAILED rather than retrying with something unusable.
    Never raises."""
    if not getattr(brain, "client", None):
        return None
    try:
        from brain.skills_registry import skills
        spec = skills.get_spec(step.tool) or {}
        params = spec.get("function", {}).get("parameters", {})
        messages = [
            {"role": "system", "content": f"A call to the '{step.tool}' tool just failed. Propose corrected "
                                          f"arguments for the SAME tool by calling {_CORRECTION_TOOL_NAME}(...) "
                                          f"exactly once. If the failure means this step genuinely can't be "
                                          f"done (information that was never provided and still isn't "
                                          f"knowable), call it again with the arguments unchanged."},
            {"role": "user", "content": f"Tool: {step.tool}\nPrevious arguments: {json.dumps(step.arguments)}\n"
                                        f"Error: {error_message}\nTool's parameters: {json.dumps(params)}"},
        ]
        response = brain.client.chat.completions.create(
            model=brain.model, messages=messages, tools=[_CORRECTION_TOOL_SCHEMA],
            tool_choice={"type": "function", "function": {"name": _CORRECTION_TOOL_NAME}},
            max_tokens=500, timeout=config.PLANNER_STEP_TIMEOUT_S,
        )
        call = response.choices[0].message.tool_calls[0]
        if call.function.name != _CORRECTION_TOOL_NAME:
            return None
        payload = json.loads(call.function.arguments or "{}")
        corrected = payload.get("arguments")
        return corrected if isinstance(corrected, dict) else None
    except Exception:
        logger.debug("planning.executor: self-correction call for '%s' failed", step.tool, exc_info=True)
        return None


def _is_parallel_eligible(step: PlanStep) -> bool:
    """A step can be grouped into a concurrent batch with its neighbors
    only if it doesn't depend on the step before it AND is risk-tier T0
    (read-only, no side effect at all - brain/tiers.py). T0 is the one
    tier brain/groq_core.py's _guard() always resolves straight to "run" -
    no gating, no taint check, unconditionally - so a batched step can
    never genuinely come back NEEDS_CONFIRMATION or DENIED;
    _dispatch_batch_concurrently below still handles those tags
    defensively regardless, rather than assuming they can't happen.
    get_tier() (not resolve_tier()) is used deliberately: it never
    returns T0 for anything outside the committed TIERS table (see
    brain/tiers.py's own docstring), so a hot-loaded user skill - which
    can only ever resolve to T1/T2/UNKNOWN_TIER through resolve_tier()'s
    fallback - can never qualify for this either; only a built-in tool a
    human actually reviewed and placed in that table as T0 is eligible."""
    return (not step.depends_on_previous) and get_tier(step.tool) == T0


def _dispatch_once(brain, step: PlanStep, arguments: Dict[str, Any], *,
                    deadline: Optional[float] = None) -> Tuple[str, str]:
    """One dispatch attempt, no retry, no timing - the caller times the
    whole step (see _run_step_with_retry/_retry_after_batch_failure),
    since a retried step's reported duration includes the self-correction
    LLM call in between, not just its final dispatch attempt. Returns
    (tag, payload): tag is one of "ok"/"needs_confirmation"/"denied"/
    "failed"; payload is the result string, confirmation prompt, or error
    text, matching tag.

    Tier 19: deadline (an absolute time.monotonic() value, optional) is
    turned into a handler_timeout for dispatch_plan_step - min'd against
    config.PLANNER_STEP_TIMEOUT_S so neither a single step nor the plan's
    own remaining budget can be blown past by a handler that never
    returns. None (the default - also what every call site outside
    execute_plan() gets, e.g. a direct test) keeps the old, unbounded
    behavior exactly as it was. The gate check itself is never
    thread-moved or bounded this way - see dispatch_plan_step's own
    docstring for why that boundary has to be exactly where it is."""
    handler_timeout = None
    if deadline is not None:
        handler_timeout = max(0.1, min(config.PLANNER_STEP_TIMEOUT_S, deadline - time.monotonic()))
    try:
        return "ok", dispatch_plan_step(brain, step.tool, arguments, handler_timeout=handler_timeout)
    except PlanStepRequiresConfirmation as e:
        return "needs_confirmation", e.prompt
    except PlanStepDenied as e:
        return "denied", str(e)
    except PlanStepDispatchError as e:
        return "failed", str(e)


_STATUS_BY_TAG = {"ok": StepStatus.OK, "needs_confirmation": StepStatus.NEEDS_CONFIRMATION,
                  "denied": StepStatus.FAILED, "failed": StepStatus.FAILED}


def _finalize(step: PlanStep, tag: str, payload: str, t0: float, retried: bool) -> Tuple[StepResult, str]:
    """Builds the StepResult plus the matching plan_step_end trace-status
    string. The two aren't quite the same shape on purpose: a denial and a
    plain failure both become StepStatus.FAILED (PlanStepDenied's
    docstring - a refusal behaves like a failure to the rest of the plan),
    but stay distinguishable as "denied" vs "failed" in a trace."""
    status = _STATUS_BY_TAG[tag]
    duration_ms = round((time.perf_counter() - t0) * 1000, 1)
    result = StepResult(step=step, status=status, duration_ms=duration_ms, retried=retried,
                        result=payload if status in (StepStatus.OK, StepStatus.NEEDS_CONFIRMATION) else None,
                        error=payload if status == StepStatus.FAILED else None)
    trace_status = f"{tag}_after_retry" if retried and tag in ("ok", "needs_confirmation") else tag
    return result, trace_status


def _retry_after_batch_failure(brain, step: PlanStep, first_error: str, t0: float,
                                deadline: Optional[float] = None) -> Tuple[StepResult, str]:
    """The one self-correction retry, usable both after a plain sequential
    dispatch failure and after a batched step's parallel first attempt
    already failed - only for a plain dispatch error, never a denial (a
    confirmation-required or denied step is never "wrong arguments" -
    retrying either would just spend an LLM call reaching the exact same
    outcome again). `t0` is the ORIGINAL start of this step's first
    attempt, so the reported duration covers the whole thing including the
    correction call - for a step that was part of a parallel batch, that
    also honestly includes any time spent waiting for the rest of the
    batch to finish before this sequential retry phase could even start;
    accepted as a reasonable overcount for what should be a rare path
    (most T0 reads just succeed on the first try).

    Tier 19: `deadline` is forwarded to the retry's own _dispatch_once
    call (re-evaluated fresh, same as everywhere else - time has passed
    since `t0`, including a whole correction LLM call) so a retry can't
    hang unbounded either, same reasoning as the first attempt."""
    if config.PLANNER_MAX_RETRIES_PER_STEP <= 0:
        return _finalize(step, "failed", first_error, t0, retried=False)
    corrected_args = _request_correction(brain, step, first_error)
    if corrected_args is None:
        return _finalize(step, "failed", first_error, t0, retried=True)
    tag2, payload2 = _dispatch_once(brain, step, corrected_args, deadline=deadline)
    if tag2 == "denied":
        tag2 = "failed"   # PlanStepDenied IS-A PlanStepDispatchError - a retry's own denial reads as a plain failure
    return _finalize(step, tag2, payload2, t0, retried=True)


def _run_step_with_retry(brain, step: PlanStep, deadline: Optional[float] = None) -> Tuple[StepResult, str]:
    """The full single-step pipeline used by execute_plan()'s ordinary
    sequential path: one dispatch attempt, then (for a plain failure only)
    the one self-correction retry above. `deadline` (Tier 19) is forwarded
    to every dispatch attempt below - see _dispatch_once's docstring."""
    t0 = time.perf_counter()
    tag, payload = _dispatch_once(brain, step, step.arguments, deadline=deadline)
    if tag != "failed":
        return _finalize(step, tag, payload, t0, retried=False)
    return _retry_after_batch_failure(brain, step, payload, t0, deadline=deadline)


def _dispatch_batch_concurrently(brain, steps: List[PlanStep],
                                  deadline: Optional[float] = None) -> List[Tuple[StepResult, str]]:
    """Runs every step in `steps` (each already confirmed
    _is_parallel_eligible) at once in a small thread pool for its FIRST
    dispatch attempt only, then retries whichever ones failed sequentially,
    one at a time, right after - see the module docstring's Tier 6 section
    for why only the first attempt is parallelized: a correction LLM call
    is comparatively rare (most T0 reads just succeed), and keeping the
    retry itself sequential means it reuses _retry_after_batch_failure
    exactly as the ordinary path does, rather than a second, divergent
    concurrent-retry implementation. Deliberately does NOT call
    trace.mark() from inside the pool: telemetry/trace.py's tracer is one
    active turn per THREAD (threading.local()), so a mark made from a
    worker thread would silently land nowhere - the caller marks every
    step from the main thread instead, once this returns. Returns results
    in the SAME order as `steps` (concurrent.futures.Executor.map already
    guarantees this, regardless of completion order), each paired with its
    trace-status string.

    Tier 19: `deadline` is forwarded to every step's own _dispatch_once
    call, each of which now bounds ITS wait individually (via its own
    one-off worker thread - see that function's docstring) - so one
    hanging step in the batch no longer blocks pool.map() from ever
    returning, which used to block every other step's result right along
    with it since list(pool.map(...)) only yields once every item is
    done."""
    max_workers = max(1, min(len(steps), config.PLANNER_MAX_PARALLEL_WORKERS))

    def _first_attempt(step: PlanStep) -> Tuple[str, str, float]:
        t0 = time.perf_counter()
        tag, payload = _dispatch_once(brain, step, step.arguments, deadline=deadline)
        return tag, payload, t0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        first_pass = list(pool.map(_first_attempt, steps))

    finalized: List[Tuple[StepResult, str]] = []
    for step, (tag, payload, t0) in zip(steps, first_pass):
        if tag == "failed":
            finalized.append(_retry_after_batch_failure(brain, step, payload, t0, deadline=deadline))
        else:
            finalized.append(_finalize(step, tag, payload, t0, retried=False))
    return finalized


def _compose_final_message(results: List[StepResult]) -> str:
    """Deterministic, no extra LLM call - see the module docstring on why
    this trades a slightly plainer final message for one fewer point of
    failure/latency in an already multi-call turn."""
    needs_confirm = [r for r in results if r.status == StepStatus.NEEDS_CONFIRMATION]
    if needs_confirm:
        # The gate's own message already says what and asks yes/no - it
        # IS the right next thing to show, same as a single gated AI tool
        # call would show on its own.
        return needs_confirm[0].result or "I need you to confirm before I can continue."

    ok = [r for r in results if r.status == StepStatus.OK]
    failed = [r for r in results if r.status == StepStatus.FAILED]
    skipped = [r for r in results if r.status == StepStatus.SKIPPED]

    parts: List[str] = []
    if ok:
        parts.append(" ".join(str(r.result) for r in ok if r.result))
    if failed:
        detail = "; ".join(f"{r.step.tool} ({r.error})" for r in failed)
        parts.append(f"I couldn't finish everything - {detail}.")
    if skipped:
        parts.append(f"Skipped {len(skipped)} step(s) that depended on one of those.")
    return " ".join(p for p in parts if p).strip() or "Done."


def execute_plan(brain, plan: Plan) -> PlanOutcome:
    """Runs every step of `plan` in order against `brain`'s real dispatch
    path. A step whose depends_on_previous is True is SKIPPED (not run)
    if the step immediately before it failed or needs confirmation -
    everything else runs regardless of earlier failures, since most of a
    plan's steps are independent asks bundled into one request ("check the
    weather AND set a reminder"), not a strict pipeline. The whole plan
    stops accepting NEW work once a step comes back needing confirmation
    (every later step is SKIPPED, not attempted) - that gate exists to
    pause for a human either way, and racing further actions ahead of an
    unanswered confirmation would defeat the point of it. Bounded by
    PLANNER_TOTAL_TIMEOUT_S end to end; never raises - any step's own
    unexpected failure becomes a FAILED StepResult, not an exception out
    of this function.

    Tier 6: a maximal contiguous run of 2+ steps that are each
    _is_parallel_eligible (independent AND risk-tier T0) dispatches through
    _dispatch_batch_concurrently instead of one at a time - see the module
    docstring. A lone eligible step with no eligible neighbor still takes
    the ordinary sequential path; a thread pool isn't worth it for one
    call.

    Tier 19: `deadline` is checked, as before, to decide whether to START
    the next step/batch at all - but is now ALSO handed down to every
    individual dispatch attempt (_run_step_with_retry /
    _dispatch_batch_concurrently -> _dispatch_once), which bounds its OWN
    wait to min(PLANNER_STEP_TIMEOUT_S, time left until deadline). A step
    or batch already in flight is therefore no longer able to hang past
    both of those bounds - only a call that's already timed out and been
    abandoned keeps running, detached, in its own worker thread (see
    _dispatch_once's docstring for why that's accepted rather than solved
    outright - Python has no safe way to force a thread to stop)."""
    results: List[StepResult] = []
    deadline = time.monotonic() + config.PLANNER_TOTAL_TIMEOUT_S
    previous_blocked = False     # previous step/batch ended FAILED or NEEDS_CONFIRMATION
    confirmation_pending = False  # once True, every remaining step is SKIPPED

    steps = plan.steps
    n = len(steps)
    i = 0
    while i < n:
        step = steps[i]
        if confirmation_pending:
            results.append(StepResult(step=step, status=StepStatus.SKIPPED,
                                      error="an earlier step needs your confirmation first"))
            i += 1
            continue
        if time.monotonic() >= deadline:
            results.append(StepResult(step=step, status=StepStatus.SKIPPED, error="the plan ran out of time"))
            i += 1
            continue
        if step.depends_on_previous and previous_blocked:
            results.append(StepResult(step=step, status=StepStatus.SKIPPED,
                                      error="a previous step this depends on didn't succeed"))
            i += 1
            continue

        batch_end = i
        if config.PLANNER_PARALLEL_READONLY_STEPS and _is_parallel_eligible(step):
            batch_end = i + 1
            while batch_end < n and _is_parallel_eligible(steps[batch_end]):
                batch_end += 1

        if batch_end - i >= 2:
            batch = steps[i:batch_end]
            for s in batch:
                trace.mark("plan_step_start", tool=s.tool)
            batch_results: List[StepResult] = []
            for s, (result, trace_status) in zip(batch, _dispatch_batch_concurrently(brain, batch, deadline=deadline)):
                results.append(result)
                batch_results.append(result)
                trace.mark("plan_step_end", tool=s.tool, status=trace_status)
            previous_blocked = batch_results[-1].status != StepStatus.OK
            confirmation_pending = any(r.status == StepStatus.NEEDS_CONFIRMATION for r in batch_results)
            i = batch_end
            continue

        trace.mark("plan_step_start", tool=step.tool)
        result, trace_status = _run_step_with_retry(brain, step, deadline=deadline)
        results.append(result)
        trace.mark("plan_step_end", tool=step.tool, status=trace_status)
        previous_blocked = result.status != StepStatus.OK
        confirmation_pending = (result.status == StepStatus.NEEDS_CONFIRMATION)
        i += 1

    outcome = PlanOutcome(plan=plan, results=results, final_message=_compose_final_message(results),
                          pending_confirmation=confirmation_pending)
    return outcome
