"""
planning/schema.py — Tier 4 item #11: the shapes a proposed plan must fit,
and the validation that stands between a model-proposed plan and
WaveBrain's real dispatch path.
================================================================================
Modelled on SARA's sara/core/planning/schema.py, with one deliberate scope
cut: SARA's version also re-implements URL/app-target hardening
(validate_url/validate_app_target) because SARA's planning executor calls
straight into raw tool functions. Wave's executor instead calls back through
WaveBrain._validate_tool_args()/._guard() (see planning/executor.py) - the
SAME required-argument checks and confirm-gate every AI-chosen or
intent_router-routed call already goes through - so re-checking argument
shape here would just be a second, divergence-prone copy of logic that
already exists and is already tested. What stays here is the part that
really is new: is this a real, currently-registered tool at all, is the
plan's overall SHAPE sane (a list of steps, not too many of them), and the
small, pure dataclasses the planner/executor pass between each other.

Tier 6 revisited this same cut, on a roadmap item asking directly for
SARA's validate_url()/validate_app_target()/validate_tool_arguments(). The
call stands: duplicating argument-shape validation here still isn't worth
the drift risk, for the exact reason above. What Tier 6 did instead was
look for the same CONCERN those functions serve - is a tool about to act
on a URL or an app/process name that it shouldn't - somewhere that isn't
already covered by _validate_tool_args()/._guard(), and found two genuine,
previously-unguarded gaps in automation/app_control.py, fixed there rather
than here: open_website() was handing a "URL" to the backend with nothing
past a startswith("http://"/"https://") string check (see
_is_safe_http_url in that file), and close_app() had no protection against
its one eligible candidate (see that function's own comment for why
there's only ever one) BEING a critical system/shell process (see
_CRITICAL_PROCESS_NAMES in that file). Fixed in the shared backend, not
here, so every call route benefits - planner, AI tool-calling, and
intent_router alike - consistent with why validate_url/validate_app_target
were cut from this module in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from brain.skills_registry import skills

# The JSON Schema for one element of the `steps` array the model must
# produce via the propose_plan tool call (see planning/planner.py). Defined
# once, here, and imported by planner.py when it builds that tool's
# function spec - so the prompt-facing shape and parse_plan_from_llm()'s
# parsing can never silently drift apart from each other.
PLAN_STEP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "tool": {"type": "string", "description": "The exact name of one already-registered tool."},
        "arguments": {"type": "object", "description": "This tool's arguments, by name, as it normally expects them."},
        "depends_on_previous": {
            "type": "boolean",
            "description": "True if this step only makes sense after the PREVIOUS step succeeded "
                           "(skip this step rather than run it if that one failed).",
        },
        "note": {"type": "string", "description": "One short phrase: why this step, in plain words."},
    },
    "required": ["tool"],
}

PLAN_STEPS_PARAMETER_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "items": PLAN_STEP_SCHEMA,
    "description": "An ordered list of tool calls that together satisfy the request.",
}


class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass
class PlanStep:
    tool: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    depends_on_previous: bool = False
    note: str = ""


@dataclass
class Plan:
    steps: List[PlanStep]
    raw_goal: str = ""
    dropped: int = 0   # how many raw steps parse_plan_from_llm() had to discard - diagnostics only


@dataclass
class StepResult:
    step: PlanStep
    status: StepStatus
    result: Optional[str] = None
    error: Optional[str] = None
    retried: bool = False
    duration_ms: float = 0.0


@dataclass
class PlanOutcome:
    plan: Plan
    results: List[StepResult]
    final_message: str
    pending_confirmation: bool = False


class PlanValidationError(Exception):
    """Raised by parse_plan_from_llm when nothing usable survives validation -
    the caller's response to this is always to fall back to the normal
    one-tool-at-a-time loop, never to show this message to the person."""


def _coerce_arguments(raw: Any) -> Dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


def parse_plan_from_llm(raw_args: Any, *, max_steps: int, raw_goal: str = "") -> Plan:
    """Turns the model's raw propose_plan(...) JSON arguments into a
    validated Plan, or raises PlanValidationError if nothing usable
    survives. Per-step handling, in order:
      - not a dict, or no usable 'tool' string -> step dropped
      - 'tool' isn't a name skills.get_handler() actually knows about
        right now (could be stale from the model's training, a typo, or a
        skill the model imagined) -> step dropped
      - 'arguments' isn't a dict -> coerced to {} (the executor's own
        required-argument check will catch a genuinely missing value and
        trigger the one self-correction retry - see planning/executor.py)
      - an exact repeat of the immediately preceding (tool, arguments)
        pair -> dropped (a cheap guard against a model looping on one step)
      - anything past `max_steps` surviving steps -> truncated, not dropped
        one at a time, so one oversized plan doesn't spam individually
    Never raises for a single bad step - only when EVERY step was bad, or
    the top-level shape wasn't even a {"steps": [...]} object, does this
    raise (steps=[] would silently "succeed" and report nothing done,
    which is worse than falling back to the normal loop)."""
    if not isinstance(raw_args, dict) or not isinstance(raw_args.get("steps"), list):
        raise PlanValidationError("propose_plan's arguments weren't a {'steps': [...]} object.")

    raw_steps = raw_args["steps"]
    if not raw_steps:
        raise PlanValidationError("propose_plan proposed zero steps.")

    parsed: List[PlanStep] = []
    dropped = 0
    last_key: Optional[tuple] = None
    for raw_step in raw_steps:
        if not isinstance(raw_step, dict):
            dropped += 1
            continue
        tool = raw_step.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            dropped += 1
            continue
        tool = tool.strip()
        if skills.get_handler(tool) is None:
            dropped += 1
            continue
        arguments = _coerce_arguments(raw_step.get("arguments"))
        key = (tool, tuple(sorted(arguments.items())))
        if key == last_key:
            dropped += 1
            continue
        last_key = key
        parsed.append(PlanStep(
            tool=tool,
            arguments=arguments,
            depends_on_previous=bool(raw_step.get("depends_on_previous", False)),
            note=str(raw_step.get("note") or "")[:200],
        ))

    if not parsed:
        raise PlanValidationError(f"None of the {len(raw_steps)} proposed step(s) named a real, "
                                  f"currently-registered tool.")

    truncated = len(parsed) > max_steps
    if truncated:
        dropped += len(parsed) - max_steps
        parsed = parsed[:max_steps]

    return Plan(steps=parsed, raw_goal=raw_goal, dropped=dropped)
