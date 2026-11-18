"""Phase 8 / Tier 2 item 5 - LLM provider streaming + WaveBrain.think_stream().

StreamingClientTests exercises brain/llm_provider.py's OpenAICompatClient
in isolation (no WaveBrain) - same style as test_phase4_provider.py's
ClientTests, just with stream=True and the sse_* helpers instead of
completion()/tool_call().

ThinkStreamTests then exercises WaveBrain.think_stream() end to end
through BrainBase's real brain + fake server, the way
test_phase6_governance.py tests the non-streaming tool-calling loop.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: F401
from _fake_openai import FakeOpenAI, Reply, completion, error_body, tool_call, sse_body, sse_chunk, sse_tool_call_delta

from brain.llm_provider import LLMError, OpenAICompatClient, ProviderConfig
from database.db_manager import db
from test_phase4_brain import BrainBase


def cfg_for(srv, **over):
    base = dict(name="custom", base_url=srv.url, api_key="sk-secret-1234567890", model="m1",
                vision_model="m1", timeout=5.0, max_retries=2, needs_key=True, key_var="LLM_API_KEY")
    base.update(over)
    return ProviderConfig(**base)


class StreamingClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

    def client(self, **over):
        return OpenAICompatClient(cfg_for(self.srv, **over))

    def create(self, client=None, **kw):
        client = client or self.client()
        kw.setdefault("messages", [{"role": "user", "content": "hi"}])
        return client.chat.completions.create(**kw)

    def test_content_streams_in_order_and_last_chunk_carries_finish_reason(self):
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant"}),
            sse_chunk(delta={"content": "Hello"}),
            sse_chunk(delta={"content": ", "}),
            sse_chunk(delta={"content": "world."}),
            sse_chunk(delta={}, finish_reason="stop"),
        ))]
        chunks = list(self.create(stream=True))
        text = "".join(c.choices[0].delta.content or "" for c in chunks)
        self.assertEqual(text, "Hello, world.")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")
        self.assertIsNone(chunks[-1].choices[0].delta.content)

    def test_tool_call_deltas_pass_through_without_being_aggregated_here(self):
        """Aggregating split id/name/arguments across chunks is
        think_stream()'s job (see ThinkStreamTests below) - this layer
        must hand each chunk's piece through exactly as given."""
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant", "tool_calls": [
                sse_tool_call_delta(0, id="call_1", name="lock_pc", arguments=""),
            ]}),
            sse_chunk(delta={"tool_calls": [sse_tool_call_delta(0, arguments="{}")]}),
            sse_chunk(delta={}, finish_reason="tool_calls"),
        ))]
        chunks = list(self.create(stream=True))
        tc0 = chunks[0].choices[0].delta.tool_calls[0]
        self.assertEqual((tc0.index, tc0.id, tc0.function.name, tc0.function.arguments), (0, "call_1", "lock_pc", ""))
        tc1 = chunks[1].choices[0].delta.tool_calls[0]
        self.assertEqual((tc1.index, tc1.id, tc1.function.name, tc1.function.arguments), (0, None, None, "{}"))
        self.assertEqual(chunks[2].choices[0].finish_reason, "tool_calls")

    def test_negotiation_retries_opening_the_stream_then_succeeds(self):
        self.srv.script = [
            Reply(400, error_body("Unsupported parameter: 'temperature'.", "temperature")),
            Reply(200, sse_body(sse_chunk(delta={"content": "ok"}), sse_chunk(delta={}, finish_reason="stop"))),
        ]
        chunks = list(self.create(stream=True, temperature=0.4))
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "ok")
        self.assertEqual(len(self.srv.posts()), 2)
        self.assertNotIn("temperature", self.srv.posts()[1].json)

    def test_unfixable_400_raises_immediately_not_a_generator(self):
        self.srv.script = [Reply(400, error_body("Completely broken request."))]
        with self.assertRaises(LLMError) as cm:
            self.create(stream=True)
        self.assertEqual(cm.exception.kind, "bad_request")
        self.assertEqual(len(self.srv.posts()), 1)

    def test_auth_error_on_open_raises_before_any_chunk(self):
        self.srv.script = [Reply(401, error_body("Invalid API key."))]
        with self.assertRaises(LLMError) as cm:
            self.create(stream=True)
        self.assertEqual(cm.exception.kind, "auth")

    def test_mid_stream_error_event_raises_once_iterated(self):
        from _fake_openai import sse_error
        self.srv.script = [Reply(200, "".join([
            sse_chunk(delta={"content": "partial"}),
            sse_error("the model crashed"),
        ]) + "data: [DONE]\n\n")]
        gen = self.create(stream=True)
        first = next(gen)
        self.assertEqual(first.choices[0].delta.content, "partial")
        with self.assertRaises(LLMError):
            next(gen)

    def test_done_terminates_cleanly_with_no_extra_chunk(self):
        self.srv.script = [Reply(200, sse_body(sse_chunk(delta={"content": "x"}, finish_reason="stop")))]
        chunks = list(self.create(stream=True))
        self.assertEqual(len(chunks), 1)

    def test_stray_non_json_and_keepalive_lines_do_not_break_the_stream(self):
        body = (
            ": keep-alive\n\n"
            + sse_chunk(delta={"content": "a"})
            + "data: {not valid json\n\n"
            + "\n"
            + sse_chunk(delta={"content": "b"}, finish_reason="stop")
            + "data: [DONE]\n\n"
        )
        self.srv.script = [Reply(200, body)]
        chunks = list(self.create(stream=True))
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "ab")


class ThinkStreamTests(BrainBase):
    """WaveBrain.think_stream() - same governed round loop as think(), just
    streamed. INTENT_ROUTER_ENABLED is already False via BrainBase.setUp()
    (see test_phase4_brain.py), which is what these tests want anyway:
    every phrase below should reach the scripted server, not a fast-path
    skill call."""

    def last_saved_reply(self):
        history = db.get_chat_history(limit=50)
        assistant_msgs = [msg for role, msg in history if role == "assistant"]
        return assistant_msgs[-1] if assistant_msgs else None

    def test_single_round_streams_text_matching_what_think_would_return(self):
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant", "content": "Hi "}),
            sse_chunk(delta={"content": "there!"}),
            sse_chunk(delta={}, finish_reason="stop"),
        ))]
        chunks = list(self.brain.think_stream("hello"))
        self.assertEqual("".join(chunks), "Hi there!")
        self.assertEqual(len(self.srv.posts()), 1)
        self.assertEqual(self.last_saved_reply(), "Hi there!")

    def test_tool_call_round_then_final_round_dispatches_once_and_streams_the_final_text(self):
        self.srv.script = [
            Reply(200, sse_body(
                sse_chunk(delta={"role": "assistant", "tool_calls": [
                    sse_tool_call_delta(0, id="call_1", name="get_clipboard", arguments=""),
                ]}),
                sse_chunk(delta={"tool_calls": [sse_tool_call_delta(0, arguments="{}")]}),
                sse_chunk(delta={}, finish_reason="tool_calls"),
            )),
            Reply(200, sse_body(
                sse_chunk(delta={"role": "assistant", "content": "Checked your clipboard for you."}),
                sse_chunk(delta={}, finish_reason="stop"),
            )),
        ]
        chunks = list(self.brain.think_stream("what's in my clipboard"))
        self.assertEqual("".join(chunks), "Checked your clipboard for you.")
        self.assertEqual(len(self.srv.posts()), 2)
        round2_messages = self.srv.posts()[1].json["messages"]
        tool_msgs = [m for m in round2_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(self.last_saved_reply(), "Checked your clipboard for you.")

    def test_content_alongside_a_tool_call_is_streamed_but_not_saved_as_the_final_reply(self):
        """A round that says a few words before calling a tool has that
        content streamed live (good UX - see think_stream()'s docstring)
        but it is NOT part of the saved/returned reply - only the round
        with no tool_calls is. Tier 11: the two rounds' text is separated
        by a blank line instead of being glued together."""
        self.srv.script = [
            Reply(200, sse_body(
                sse_chunk(delta={"role": "assistant", "content": "Let me check that... "}),
                sse_chunk(delta={"tool_calls": [
                    sse_tool_call_delta(0, id="call_1", name="get_clipboard", arguments="{}"),
                ]}),
                sse_chunk(delta={}, finish_reason="tool_calls"),
            )),
            Reply(200, sse_body(sse_chunk(delta={"content": "Done."}, finish_reason="stop"))),
        ]
        chunks = list(self.brain.think_stream("what's in my clipboard"))
        self.assertIn("Let me check that... ", chunks)
        self.assertEqual("".join(chunks), "Let me check that... \n\nDone.")
        # ...but only round 2's content is the actual saved/returned reply.
        self.assertEqual(self.last_saved_reply(), "Done.")

    def test_pending_confirmation_replay_through_think_stream_needs_no_llm_call(self):
        open(os.path.join(self.desktop, "secret.txt"), "w").close()
        self.srv.script = [
            Reply(200, completion(None, [tool_call("delete_file", {"file_name": "secret.txt"})])),
            Reply(200, completion("Delete secret.txt - you sure?")),
        ]
        self.brain.think("delete secret.txt")          # arms pending_action (non-streaming is fine here)
        self.assertIsNotNone(self.brain.pending_action)
        self.srv.requests.clear()

        chunks = list(self.brain.think_stream("yes"))
        self.assertEqual(len(chunks), 1)
        self.assertIn("Deleted", chunks[0])
        self.assertIsNone(self.brain.pending_action)
        self.assertEqual(len(self.srv.posts()), 0)
        self.assertFalse(os.path.exists(os.path.join(self.desktop, "secret.txt")))

    def test_unrecoverable_provider_error_falls_back_gracefully_instead_of_raising(self):
        # _chat_completion_stream retries once without reasoning_effort on
        # ANY exception (same dance _chat_completion already does) - two
        # identical unfixable 400s make sure BOTH attempts genuinely fail,
        # so this exercises think_stream()'s own except Exception fallback
        # rather than a lucky second attempt.
        self.srv.script = [Reply(400, error_body("Completely broken request.")),
                           Reply(400, error_body("Completely broken request."))]
        chunks = list(self.brain.think_stream("hello"))
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0])                      # some fallback string, not an exception/crash
        self.assertEqual(self.last_saved_reply(), chunks[0])

    def test_a_round_with_zero_content_and_no_tool_calls_still_yields_something(self):
        """Hardening check: a round that ends with neither more tool calls
        nor any streamed text (final_text == "") must not leave this
        generator yielding nothing at all - see the streamed_anything
        tracking in think_stream()."""
        self.srv.script = [Reply(200, sse_body(sse_chunk(delta={}, finish_reason="stop")))]
        chunks = list(self.brain.think_stream("hello"))
        self.assertEqual(len(chunks), 1)
        self.assertTrue(chunks[0])
        self.assertEqual(self.last_saved_reply(), chunks[0])


if __name__ == "__main__":
    unittest.main()
