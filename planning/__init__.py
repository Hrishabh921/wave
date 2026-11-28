"""
planning — Tier 4 item #11: a multi-step planner for requests that
genuinely need more than one tool call, with schema validation and one
self-correction retry per failed step.
================================================================================
One public entry point:

    from planning import try_plan_and_execute
    outcome = try_plan_and_execute(brain, user_text)
    if outcome is not None:
        reply = outcome.final_message
    # else: fall through to the normal one-tool-at-a-time loop, unchanged.

Called from WaveBrain.think()/think_stream(), from inside the existing
try block, right after brain/intent_router.py's fast path has already had
its chance and found no match - never instead of either of those, only
between them and the normal multi-round tool-calling loop. Returns None
(never raises) whenever planning isn't worth it or didn't pan out:
PLANNER_ENABLED is off, planning/trigger.py's cheap heuristic doesn't
think this needs more than one step, the model's proposed plan didn't
validate into 2+ real steps, or anything along the way raised
unexpectedly - in every one of those cases the caller proceeds exactly as
if this package didn't exist, so a planning bug can make Wave slightly
slower on a compound request, never broken on any request.

See planning/trigger.py (the cheap gate), planning/planner.py (the single
bounded propose_plan LLM call), planning/executor.py (runs the validated
plan through WaveBrain's real dispatch path), and planning/schema.py (the
shapes passed between all three) for the rest.
"""
from __future__ import annotations

import logging
from typing import Optional

from planning import trigger, planner, executor
from planning.schema import PlanOutcome
from settings.config import config

logger = logging.getLogger("planning")

__all__ = ["try_plan_and_execute", "PlanOutcome"]


def try_plan_and_execute(brain, user_text: str) -> Optional[PlanOutcome]:
    """See module docstring. `brain` is the live WaveBrain instance (needs
    .client, .model, ._validate_tool_args, ._guard, .pending_action - the
    same real dispatch surface _dispatch_tool() itself uses)."""
    if not config.PLANNER_ENABLED:
        return None
    try:
        if not trigger.should_attempt_planning(user_text):
            return None
        # Tier 57: a proposal is one more full model request. While the
        # model's per-minute budget is tight (a 429 within ~30 s) it would
        # fail or starve the turn itself - the normal loop can call several
        # tools on its own anyway.
        tight = getattr(brain, "llm_budget_tight", None)
        if callable(tight) and tight():
            logger.info("planning: skipped - the model's per-minute budget is tight right now.")
            return None

        plan = planner.propose_plan(brain, user_text)
        if plan is None or len(plan.steps) < 2:
            # Zero or one step adds nothing over the normal loop, which
            # already handles a single tool call perfectly well - let it.
            return None

        logger.info("planning: executing a %d-step plan for this turn.", len(plan.steps))
        return executor.execute_plan(brain, plan)
    except Exception:
        # Never let a planning bug break a turn that the normal loop would
        # have handled just fine on its own.
        logger.warning("planning.try_plan_and_execute: unexpected failure, falling back to the normal loop",
                       exc_info=True)
        return None
