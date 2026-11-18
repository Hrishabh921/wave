"""
planning/test_doubles.py — Tier 6 item: fakes for exercising planning/
planner.py and planning/executor.py's own orchestration logic (retry,
skip-on-dependency, skip-once-confirmation-is-pending, the total time
budget, the parallel batch path, the final message) without a real
WaveBrain, database, or LLM provider behind any of it.
================================================================================
Two different fakes for two different jobs:

- FakeBrain / FakeLLMClient stand in for the (brain.client, brain.model)
  pair planner.propose_plan() / executor._request_correction() actually
  read - just enough of the OpenAI-compatible response shape
  (.choices[0].message.tool_calls[0].function.{name,arguments}) for
  those two call sites, nothing else of a real client.
- The make_dispatch_*() factories stand in for dispatch_plan_step()
  itself when testing execute_plan(): each returns a plain
  (tool, arguments) -> str callable matching the `dispatch_fn` parameter
  execute_plan() accepts for exactly this purpose, so these tests can
  exercise retry/skip/timeout/parallel-batch logic without a real
  handler, skills registry, or audit log anywhere in the loop. Production
  code never sets dispatch_fn - see execute_plan()'s own docstring for
  why that parameter exists at all.

    from planning import test_doubles as td
    brain = td.FakeBrain(client=td.FakeLLMClient([td.make_plan_response()]))
    plan = planner.propose_plan(brain, "...")
    outcome = executor.execute_plan(td.FakeBrain(), plan, dispatch_fn=td.make_dispatch_success())
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from planning.executor import PlanStepDispatchError, PlanStepRequiresConfirmation, PlanStepDenied


# =============================================================================
# brain.client / brain.model fakes - for planner.propose_plan() and
# executor._request_correction()
# =============================================================================
class FakeBrain:
    """Just the two attributes planner.propose_plan()/
    executor._request_correction() actually read off a real WaveBrain."""
    def __init__(self, client: Optional["FakeLLMClient"] = None, model: str = "test-model"):
        self.client = client
        self.model = model


class FakeLLMClient:
    """Stands in for brain.client: only .chat.completions.create(...) is
    ever called on it (see planner.py/executor.py - nothing else of a
    real OpenAI-compatible client is needed). `responses` is popped one
    per call, in order; if `raises` is set, every call raises that
    instead (simulating a transport failure) and `responses` is never
    consulted. call_log records every call's kwargs, in order, for a test
    that wants to inspect what was actually sent (e.g. the system
    prompt)."""
    def __init__(self, responses: Optional[List[Any]] = None, raises: Optional[Exception] = None):
        self._responses = list(responses or [])
        self._raises = raises
        self.call_log: List[Dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.call_log.append(kwargs)
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            raise RuntimeError("FakeLLMClient: no more scripted responses for this call")
        return self._responses.pop(0)


def _tool_call(name: str, arguments: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


def _response(tool_calls: Optional[List[SimpleNamespace]] = None, content: Optional[str] = None) -> SimpleNamespace:
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_plan_response(steps: Optional[List[Dict[str, Any]]] = None) -> SimpleNamespace:
    """A response as if the model called propose_plan(steps=[...]) -
    defaults to a simple two-step plan when `steps` isn't given."""
    if steps is None:
        steps = [{"tool": "volume_up", "arguments": {}}, {"tool": "get_pc_info", "arguments": {}}]
    return _response(tool_calls=[_tool_call("propose_plan", {"steps": steps})])


def make_malformed_steps_response() -> SimpleNamespace:
    """Calls propose_plan(...) but with a "steps" value
    parse_plan_from_llm can't use (a string, not a list) -
    propose_plan() should return None, not raise."""
    return _response(tool_calls=[_tool_call("propose_plan", {"steps": "not-a-list"})])


def make_text_response(text: str) -> SimpleNamespace:
    """No tool call at all, just a plain reply - propose_plan() should
    read this as "the model didn't want to plan" and return None."""
    return _response(tool_calls=None, content=text)


def make_correction_response(arguments: Dict[str, Any]) -> SimpleNamespace:
    """A response as if the model called executor.py's own
    correct_arguments(arguments={...}) - what _request_correction()
    expects back for its one self-correction retry."""
    return _response(tool_calls=[_tool_call("correct_arguments", {"arguments": arguments})])


# =============================================================================
# dispatch_fn fakes - for execute_plan(brain, plan, dispatch_fn=...)
# =============================================================================
def make_dispatch_success() -> Callable[[str, Dict[str, Any]], str]:
    """Every step succeeds."""
    def _dispatch(tool, arguments):
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_always_fails(message: str = "simulated failure",
                               fail_tools: Optional[List[str]] = None
                               ) -> Callable[[str, Dict[str, Any]], str]:
    """Raises PlanStepDispatchError on every call, or only for tools
    named in `fail_tools` if given (every other tool succeeds) - for
    proving an independent step still runs after an earlier one fails."""
    def _dispatch(tool, arguments):
        if fail_tools is None or tool in fail_tools:
            raise PlanStepDispatchError(message)
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_fails_then_succeeds() -> Callable[[str, Dict[str, Any]], str]:
    """Each tool's FIRST call fails; every call after that for the SAME
    tool succeeds - exactly one failure to retry past, tracked per tool
    so a multi-step plan's other steps are never affected by it."""
    seen: Dict[str, int] = {}
    def _dispatch(tool, arguments):
        seen[tool] = seen.get(tool, 0) + 1
        if seen[tool] == 1:
            raise PlanStepDispatchError("simulated failure (first attempt)")
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_requires_confirmation(confirm_tools: List[str]) -> Callable[[str, Dict[str, Any]], str]:
    """Raises PlanStepRequiresConfirmation for tools named in
    `confirm_tools`; every other tool succeeds."""
    def _dispatch(tool, arguments):
        if tool in confirm_tools:
            prompt = f"Should I go ahead and {tool.replace('_', ' ')}?"
            raise PlanStepRequiresConfirmation(tool, dict(arguments), prompt)
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_denied(deny_tools: List[str]) -> Callable[[str, Dict[str, Any]], str]:
    """Raises PlanStepDenied for tools named in `deny_tools`; every other
    tool succeeds - roadmap item #5's outright-refusal outcome, as
    opposed to make_dispatch_requires_confirmation()'s ask-first one."""
    def _dispatch(tool, arguments):
        if tool in deny_tools:
            prompt = f"I won't {tool.replace('_', ' ')} on my own right now."
            raise PlanStepDenied(tool, dict(arguments), prompt)
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_slow(sleep_s: float = 0.3) -> Callable[[str, Dict[str, Any]], str]:
    """Succeeds, but only after sleeping `sleep_s` first - for timeout
    tests, and for proving the parallel batch path actually overlaps
    wall-clock time rather than just producing correct results in
    sequence."""
    def _dispatch(tool, arguments):
        time.sleep(sleep_s)
        return f"ok: {tool}"
    return _dispatch


def make_dispatch_selective(mapping: Dict[str, Callable[[Dict[str, Any]], str]]
                            ) -> Callable[[str, Dict[str, Any]], str]:
    """Looks `tool` up in `mapping` and calls mapping[tool](arguments) -
    the mapped callable either returns the result directly or raises
    PlanStepDispatchError/PlanStepRequiresConfirmation itself, for a test
    that wants a different, specific behavior per tool rather than one
    blanket rule. An unmapped tool's KeyError is deliberately left
    uncaught - a test using this should map every tool its own plan
    actually uses."""
    def _dispatch(tool, arguments):
        return mapping[tool](arguments)
    return _dispatch
