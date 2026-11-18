"""
tests/test_doubles.py — Tier 6 item (roadmap: "Add a test_doubles.py...
so planner/executor tests don't need a live LLM").

Wave's planner/executor tests already don't touch a live LLM: every one of
them runs against tests/_fake_openai.py's FakeOpenAI, a real local HTTP
server with a scripted queue of replies (see that file, and
test_tier4_planning.py for how it's wired up via ExecutorBase). That's
deliberate, not a gap this file exists to close - testing through the real
openai-SDK-over-HTTP transport catches bugs a pure in-process fake can't
(a malformed request body, brain.client's own parsing of a response shape,
a timeout). This file is a narrower, complementary thing: a lightweight,
pure in-process stand-in for brain.client itself - no socket, no server
thread, no JSON round-trip - for a test that only cares what WaveBrain/the
planner DOES with a given LLM reply, not whether the SDK can parse one off
the wire. Use FakeOpenAI (tests/_fake_openai.py) when a test cares about
that transport layer; use FakeLLMClient here when it doesn't, and the
lighter weight is worth it - see test_tier4_planning.py's
TestDoublesIntegrationTests for this double standing in for FakeOpenAI on
one real planner behavior (the self-correction retry), as a working proof
rather than an assertion that it's interchangeable.

The one thing this double does NOT try to shortcut: a real tool call's
.function.arguments is a JSON STRING, not a dict (that's the actual SDK
shape) - production code (planning/executor.py's _request_correction)
does its own json.loads() on it. fake_tool_call() below keeps that same
shape rather than handing back an already-parsed dict, because skipping
that step would hide a real json.loads()-related bug in the code under
test, which defeats the point of a double that's supposed to stand in for
the real thing.
"""
from __future__ import annotations

import json
import types
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import unittest


def fake_tool_call(name: str, arguments: Dict[str, Any], call_id: str = "call_abc"):
    """One entry of response.choices[0].message.tool_calls, matching the
    real SDK's shape: call.function.name is the tool name, call.function.
    arguments is `arguments` JSON-ENCODED (see the module docstring for
    why this isn't just handed back as a dict)."""
    return types.SimpleNamespace(
        id=call_id, type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def fake_completion(content: Optional[str] = None, tool_calls: Optional[List[Any]] = None):
    """Mimics the response object brain.client.chat.completions.create(...)
    returns - only the attribute paths Wave's own code actually reads
    (response.choices[0].message.{content,tool_calls}; .finish_reason and
    .usage are included too since a few call sites log them, but nothing
    here enforces that production code only reads these fields - same as
    the real SDK's response object, this is just a plain container)."""
    message = types.SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls or None)
    choice = types.SimpleNamespace(index=0, message=message,
                                    finish_reason="tool_calls" if tool_calls else "stop")
    return types.SimpleNamespace(id="cmpl-fake", model="fake-model", choices=[choice],
                                  usage=types.SimpleNamespace(total_tokens=7))


@dataclass
class RecordedCall:
    """One chat.completions.create(...) invocation, exactly as the double
    received it - plain Python objects/kwargs, nothing to re-parse out of
    a request body the way a FakeOpenAI-backed test has to."""
    model: Any
    messages: List[Dict[str, Any]]
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = None
    kwargs: Dict[str, Any] = field(default_factory=dict)


class FakeChatCompletions:
    def __init__(self, outer: "FakeLLMClient"):
        self._outer = outer

    def create(self, *, model=None, messages=None, tools=None, tool_choice=None, **kwargs):
        self._outer.calls.append(RecordedCall(model=model, messages=messages or [], tools=tools,
                                               tool_choice=tool_choice, kwargs=kwargs))
        if not self._outer.script:
            if self._outer.default is None:
                raise AssertionError(
                    "FakeLLMClient: script exhausted and no default set - the code under test made "
                    "more LLM call(s) than this test scripted. Either script one more reply, or pass "
                    "default=... to FakeLLMClient(...) if running dry should be a normal case here.")
            return self._outer.default
        next_reply = self._outer.script.pop(0)
        if isinstance(next_reply, BaseException):
            raise next_reply
        return next_reply


class FakeChat:
    def __init__(self, outer: "FakeLLMClient"):
        self.completions = FakeChatCompletions(outer)


class FakeLLMClient:
    """A pure in-process stand-in for the real openai.OpenAI(...) client.
    Assign it straight to brain.client - WaveBrain and the planner only
    ever read brain.client.chat.completions.create(...) off it, nothing
    else - to script LLM replies with no real SDK instance and no real
    socket anywhere.

    script: a list consumed one reply per call (fake_completion(...) for a
        normal reply, or a raised exception instance to simulate a
        provider error - the SAME thing a real openai.APIError would do to
        calling code, since this re-raises it rather than wrapping it).
    default: served once `script` runs dry, instead of raising. Leave it
        None (the default) when a test wants to know if the code under
        test made more calls than expected - see FakeChatCompletions.create.
    calls: every call this double has received, in order, for assertions.
    """
    def __init__(self, script: Optional[List[Any]] = None, default: Optional[Any] = None):
        self.script: List[Any] = list(script or [])
        self.default = default
        self.calls: List[RecordedCall] = []
        self.chat = FakeChat(self)


# =============================================================================
# Self-tests for the double itself. Not wired into any planner/brain
# behavior - that proof lives in test_tier4_planning.py's
# TestDoublesIntegrationTests, which uses this double for a real retry
# path. These just pin down FakeLLMClient's own mechanics in isolation, so
# a future change to it fails loudly here first rather than as a
# confusing failure three layers away in a planner test.
# =============================================================================
class FakeLLMClientSelfTests(unittest.TestCase):
    def test_scripted_reply_is_served_and_recorded(self):
        reply = fake_completion("hi there")
        client = FakeLLMClient([reply])
        got = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hello"}])
        self.assertIs(got, reply)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0].model, "m")
        self.assertEqual(client.calls[0].messages, [{"role": "user", "content": "hello"}])

    def test_replies_are_served_in_order_one_per_call(self):
        first, second = fake_completion("one"), fake_completion("two")
        client = FakeLLMClient([first, second])
        self.assertIs(client.chat.completions.create(model="m", messages=[]), first)
        self.assertIs(client.chat.completions.create(model="m", messages=[]), second)

    def test_exhausted_script_with_no_default_raises_assertion_error(self):
        client = FakeLLMClient([])
        with self.assertRaises(AssertionError):
            client.chat.completions.create(model="m", messages=[])

    def test_exhausted_script_with_a_default_serves_it_indefinitely(self):
        fallback = fake_completion("fallback")
        client = FakeLLMClient([], default=fallback)
        self.assertIs(client.chat.completions.create(model="m", messages=[]), fallback)
        self.assertIs(client.chat.completions.create(model="m", messages=[]), fallback)

    def test_scripted_exception_is_raised_not_returned(self):
        client = FakeLLMClient([TimeoutError("provider timed out")])
        with self.assertRaises(TimeoutError):
            client.chat.completions.create(model="m", messages=[])

    def test_tool_call_arguments_are_a_json_string_not_a_dict(self):
        call = fake_tool_call("some_tool", {"x": 1})
        self.assertIsInstance(call.function.arguments, str)
        self.assertEqual(json.loads(call.function.arguments), {"x": 1})

    def test_fake_completion_with_tool_calls_sets_finish_reason(self):
        completion = fake_completion(tool_calls=[fake_tool_call("t", {})])
        self.assertEqual(completion.choices[0].finish_reason, "tool_calls")
        self.assertIsNone(completion.choices[0].message.content)

    def test_fake_completion_plain_text_sets_stop_finish_reason(self):
        completion = fake_completion("just text")
        self.assertEqual(completion.choices[0].finish_reason, "stop")
        self.assertIsNone(completion.choices[0].message.tool_calls)


if __name__ == "__main__":
    unittest.main()
