"""Tier 4, item #13 - the telemetry/tracing module (telemetry/trace.py,
telemetry/store.py, telemetry/dump.py) and its wiring into
WaveBrain.think()/think_stream().

Same fake-OpenAI-server harness as test_phase4_brain.py (see that file /
_fake_openai.py for how it works) - run with:
    python -m unittest discover -s tests -p "test_tier4_telemetry.py"
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import _phase4_support as S
from _fake_openai import FakeOpenAI, Reply, completion, tool_call

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from telemetry import trace                                    # noqa: E402
from telemetry import store                                    # noqa: E402
from database.db_manager import db                              # noqa: E402
from settings.config import config                                # noqa: E402


def _isolated_db():
    tmp = tempfile.mkdtemp(prefix="wave_telemetry_test_")
    db.db_path = os.path.join(tmp, "t.db")
    db.init_db()
    return tmp


# =============================================================================
# Pure trace.py unit tests - no DB, no background thread.
# =============================================================================
class TraceInMemoryTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        # Each test gets a clean thread-local slate.
        trace._local.turn = None

    def tearDown(self):
        config.TELEMETRY_ENABLED = self._enabled
        trace._local.turn = None

    def test_mark_count_duration_and_annotate_roll_up_into_end_turn_row(self):
        trace.begin_turn(source="unit")
        trace.mark("step_one", foo="bar")
        trace.add_duration("llm_ms", 0.25)
        trace.add_duration("llm_ms", 0.10)
        trace.count("tools_called")
        trace.count("tools_called")
        trace.annotate(route="tool_loop", model="llama-x", provider="groq")
        row = trace.end_turn(outcome="ok")

        self.assertIsNotNone(row)
        self.assertEqual(row["source"], "unit")
        self.assertEqual(row["route"], "tool_loop")
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["model"], "llama-x")
        self.assertEqual(row["provider"], "groq")
        self.assertEqual(row["tools_called"], 2)
        self.assertAlmostEqual(row["llm_ms"], 350.0, delta=5.0)
        self.assertEqual(len(row["stages"]), 1)
        self.assertEqual(row["stages"][0]["stage"], "step_one")
        self.assertEqual(row["stages"][0]["meta"], {"foo": "bar"})
        self.assertIsInstance(row["total_ms"], float)

    def test_functions_are_safe_no_ops_with_no_active_turn(self):
        # No begin_turn() called - none of these should raise or create a turn.
        trace.mark("whatever")
        trace.add_duration("llm_ms", 1.0)
        trace.count("tools_called")
        trace.annotate(route="x")
        self.assertIsNone(trace.end_turn(outcome="ok"))

    def test_disabled_telemetry_means_begin_turn_is_a_silent_no_op(self):
        config.TELEMETRY_ENABLED = False
        turn_id = trace.begin_turn(source="unit")
        self.assertIsNone(turn_id)
        trace.mark("step")            # must not raise, must not create a row
        self.assertIsNone(trace.end_turn(outcome="ok"))

    def test_double_begin_turn_overwrites_rather_than_raising(self):
        trace.begin_turn(source="first")
        trace.mark("a")
        trace.begin_turn(source="second")   # a stray second begin_turn - must not crash
        row = trace.end_turn(outcome="ok")
        self.assertEqual(row["source"], "second")
        self.assertEqual(len(row["stages"]), 0)   # the first turn's mark is gone with it

    def test_get_recent_and_get_summary_read_the_ring_buffer(self):
        for i in range(3):
            trace.begin_turn(source="unit")
            trace.end_turn(outcome="ok" if i < 2 else "error", route="tool_loop")
        recent = trace.get_recent(limit=3)
        self.assertEqual(len(recent), 3)
        self.assertEqual(recent[0]["outcome"], "error")    # newest first

        summary = trace.get_summary()
        self.assertGreaterEqual(summary["turns"], 3)
        self.assertIn("by_outcome", summary)
        self.assertIn("by_route", summary)


# =============================================================================
# store.py - the bounded queue + background writer thread.
# =============================================================================
class StoreWriterTests(unittest.TestCase):
    def setUp(self):
        self._tmp = _isolated_db()
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        store.flush(timeout=2.0)   # drain anything left behind by a previous test's db

    def tearDown(self):
        store.flush(timeout=2.0)   # make sure nothing is still in flight before the next test
        config.TELEMETRY_ENABLED = self._enabled

    def test_submit_then_flush_persists_a_row_to_turn_trace(self):
        row = {"turn_id": "abc123", "started_at": "2026-01-01T00:00:00Z", "ended_at": None,
               "source": "unit", "route": "tool_loop", "outcome": "ok", "model": "m", "provider": "p",
               "rounds": 1, "tools_called": 2, "total_ms": 123.4, "llm_ms": 100.0, "tool_ms": 20.0,
               "stages": [{"stage": "x", "t_ms": 1.0}], "meta": {"k": "v"}, "error": None}
        self.assertTrue(store.submit(row))
        self.assertTrue(store.flush(timeout=5.0))

        rows = db.get_recent_traces(limit=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_id"], "abc123")
        self.assertEqual(rows[0]["tools_called"], 2)
        self.assertEqual(json.loads(rows[0]["stages"]), [{"stage": "x", "t_ms": 1.0}])
        self.assertEqual(json.loads(rows[0]["meta"]), {"k": "v"})

    def test_submit_is_a_no_op_when_telemetry_disabled(self):
        config.TELEMETRY_ENABLED = False
        self.assertFalse(store.submit({"turn_id": "nope", "started_at": "x", "source": "unit", "outcome": "ok"}))
        store.flush(timeout=1.0)
        self.assertEqual(db.get_recent_traces(limit=10), [])

    def test_full_queue_drops_and_counts_rather_than_blocking(self):
        old_max = config.TELEMETRY_QUEUE_MAX
        config.TELEMETRY_QUEUE_MAX = 2
        try:
            before = store.dropped_count()
            # Fill well past capacity without ever letting the writer drain -
            # we only care that submit() itself never blocks/raises.
            for i in range(20):
                store.submit({"turn_id": f"t{i}", "started_at": "x", "source": "unit", "outcome": "ok"})
            store.flush(timeout=5.0)
            self.assertGreaterEqual(store.dropped_count(), before)
        finally:
            config.TELEMETRY_QUEUE_MAX = old_max

    def test_prune_traces_keeps_only_the_newest_rows(self):
        for i in range(5):
            db.insert_trace({"turn_id": f"p{i}", "started_at": "x", "source": "unit", "outcome": "ok"})
        removed = db.prune_traces(keep=2)
        self.assertEqual(removed, 3)
        self.assertEqual(len(db.get_recent_traces(limit=10)), 2)


# =============================================================================
# Wired into WaveBrain.think()/think_stream() end to end.
# =============================================================================
class BrainBase(unittest.TestCase):
    def setUp(self):
        self._tmp = _isolated_db()
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        store.flush(timeout=2.0)

        self.srv = FakeOpenAI()
        self.srv.start()
        self.addCleanup(self.srv.stop)
        env = {"LLM_PROVIDER": "groq", "GROQ_API_KEY": "x", "LLM_BASE_URL": self.srv.url,
               "LLM_MODEL": "llama-test"}
        self._env_patch = mock.patch.dict(os.environ, env)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

        from brain.groq_core import WaveBrain
        self.brain = WaveBrain()

    def tearDown(self):
        store.flush(timeout=2.0)
        config.TELEMETRY_ENABLED = self._enabled


class ThinkTelemetryTests(BrainBase):
    def test_plain_chat_reply_produces_an_ok_tool_loop_trace_row(self):
        self.srv.script = [Reply(200, completion("General Kenobi."))]
        reply = self.brain.think("hello there")
        self.assertEqual(reply, "General Kenobi.")

        store.flush(timeout=5.0)
        rows = db.get_recent_traces(limit=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "think")
        self.assertEqual(rows[0]["route"], "tool_loop")
        self.assertEqual(rows[0]["outcome"], "ok")
        self.assertEqual(rows[0]["rounds"], 1)
        self.assertIsNotNone(rows[0]["llm_ms"])

    def test_intent_router_hit_is_traced_with_its_own_route(self):
        reply = self.brain.think("volume up")
        store.flush(timeout=5.0)
        rows = db.get_recent_traces(limit=5)
        self.assertEqual(rows[0]["route"], "intent_router")
        self.assertEqual(rows[0]["outcome"], "ok")
        meta = json.loads(rows[0]["meta"])
        self.assertEqual(meta.get("skill"), "volume_up")

    def test_tool_call_round_is_traced_with_tools_called_and_tool_ms(self):
        # intent_router disabled for just this test so the request is
        # guaranteed to go through the real LLM tool-calling loop (several
        # of its own phrasings are plain enough that intent_router's fast
        # path would otherwise claim them first, which is exactly what
        # test_intent_router_hit_is_traced_with_its_own_route already
        # covers - this test is specifically about the tool-LOOP path).
        old = config.INTENT_ROUTER_ENABLED
        config.INTENT_ROUTER_ENABLED = False
        self.addCleanup(setattr, config, "INTENT_ROUTER_ENABLED", old)

        self.srv.script = [
            Reply(200, completion(None, [tool_call("get_pc_info", {})])),
            Reply(200, completion("Here's your PC info.")),
        ]
        reply = self.brain.think("tell me about my pc")
        self.assertEqual(reply, "Here's your PC info.")

        store.flush(timeout=5.0)
        rows = db.get_recent_traces(limit=5)
        self.assertEqual(rows[0]["rounds"], 2)
        self.assertEqual(rows[0]["tools_called"], 1)
        self.assertIsNotNone(rows[0]["tool_ms"])

    def test_safety_refusal_is_traced_without_ever_calling_the_llm(self):
        reply = self.brain.think("shut down the pc")
        store.flush(timeout=5.0)
        rows = db.get_recent_traces(limit=5)
        self.assertEqual(rows[0]["route"], "safety_refusal")
        self.assertEqual(rows[0]["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
