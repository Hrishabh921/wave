"""
planning/planner.py — Tier 4 item #11: asks the model to propose a short,
ordered sequence of already-registered tools for one request, via a single
bounded, forced tool call.
================================================================================
Deliberately NOT a re-use of WaveBrain._chat_completion(): that method
always passes tools=skills.tool_specs() (every one of Wave's ~44 tools, for
the normal "pick the next single tool, or answer in plain text" round) and
tool_choice="auto". This call needs the OPPOSITE shape - exactly one
function on offer (propose_plan) and tool_choice forcing the model to call
it - so it goes straight through brain.client.chat.completions.create()
itself, the same OpenAI-compatible client _chat_completion wraps (see
brain/llm_provider.py's OpenAICompatClient), just with its own tools list.

    from planning import planner
    plan = planner.propose_plan(brain, "check the weather, then set a
                                         reminder to bring an umbrella")
    if plan is not None:
        ...

Never raises - any failure (timeout, malformed tool-call JSON, a plan that
validates down to nothing) logs and returns None, which
planning/__init__.py treats as "skip planning, fall back to the normal
one-tool-at-a-time loop" exactly as if this had never been called.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from planning.schema import Plan, PlanValidationError, PLAN_STEPS_PARAMETER_SCHEMA, parse_plan_from_llm
from settings.config import config

logger = logging.getLogger("planning.planner")

_FUNCTION_NAME = "propose_plan"

_SYSTEM_PREAMBLE = (
    "You turn one request into a short, ordered list of tool calls. You are "
    "NOT answering the person and NOT performing any action yourself - you are "
    "only proposing which already-registered tools to run, in which order, with "
    "which arguments. Call {fn}(...) exactly once with your proposed steps.\n\n"
    "Rules:\n"
    "- Only use tool names from the list below, spelled exactly as given.\n"
    "- Keep it to the minimum steps that actually satisfy the request (often 2-4).\n"
    "- Set depends_on_previous=true on a step only when it genuinely only makes "
    "sense after the step right before it succeeded (e.g. \"reply to it\" after "
    "\"find the message\"). Leave it false for steps that stand on their own.\n"
    "- If the request is really just ONE action, propose a plan with exactly one step "
    "- do not invent extra steps to pad it out.\n"
    "- If the request doesn't actually need any of the tools below, propose a plan "
    "with zero steps rather than forcing an unrelated tool to fire."
).format(fn=_FUNCTION_NAME)


def _tool_catalog_lines() -> str:
    """name - description, one per line, straight from skills.describe() (the
    SAME registry _dispatch_tool()/skills.tool_specs() already use) - so this
    list can never list a tool that doesn't really exist, or describe one
    differently than the rest of the app does."""
    from brain.skills_registry import skills
    lines = [f"- {item['name']}: {item['description']}" for item in skills.describe()]
    return "\n".join(lines)


def _plan_tool_spec() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _FUNCTION_NAME,
            "description": "Propose an ordered list of tool calls that together satisfy the request.",
            "parameters": {
                "type": "object",
                "properties": {"steps": PLAN_STEPS_PARAMETER_SCHEMA},
                "required": ["steps"],
            },
        },
    }


def _extract_steps_argument(response: Any) -> Any:
    choice = response.choices[0]
    tool_calls = getattr(choice.message, "tool_calls", None)
    if not tool_calls:
        return None
    call = tool_calls[0]
    if call.function.name != _FUNCTION_NAME:
        return None
    try:
        return json.loads(call.function.arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return None


def propose_plan(brain, user_text: str) -> Optional[Plan]:
    """Makes one bounded, forced propose_plan(...) call through `brain`'s
    own LLM client and returns a validated Plan, or None on any failure
    (never raises). `brain` only needs .client and .model - passed as the
    live WaveBrain instance so this always uses whichever provider/model
    the person is actually configured for, with no separate credentials
    or client of its own to fall out of sync."""
    if not getattr(brain, "client", None):
        return None
    try:
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PREAMBLE + "\n\nAvailable tools:\n" + _tool_catalog_lines()},
            {"role": "user", "content": user_text},
        ]
        response = brain.client.chat.completions.create(
            model=brain.model,
            messages=messages,
            tools=[_plan_tool_spec()],
            tool_choice={"type": "function", "function": {"name": _FUNCTION_NAME}},
            max_tokens=1200,
            timeout=config.PLANNER_PROPOSE_TIMEOUT_S,
        )
    except Exception as e:
        logger.info("planning.propose_plan: LLM call failed (%s) - skipping planning for this turn.",
                   type(e).__name__)
        return None

    raw_args = _extract_steps_argument(response)
    if raw_args is None:
        logger.info("planning.propose_plan: model didn't call %s - skipping planning for this turn.",
                   _FUNCTION_NAME)
        return None

    try:
        plan = parse_plan_from_llm(raw_args, max_steps=config.PLANNER_MAX_STEPS, raw_goal=user_text)
    except PlanValidationError as e:
        logger.info("planning.propose_plan: proposed plan didn't validate (%s) - skipping planning.", e)
        return None

    if plan.dropped:
        logger.info("planning.propose_plan: dropped %d invalid/duplicate/excess step(s) from the "
                   "model's proposal; %d step(s) remain.", plan.dropped, len(plan.steps))
    return plan
