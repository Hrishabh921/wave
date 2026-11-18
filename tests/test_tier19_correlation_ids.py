"""Tier 19 item: per-turn correlation IDs in every log line
(STT -> router -> LLM -> tools -> TTS), reusing telemetry's turn id.

Covers:
  * telemetry/trace.py   - current_turn_id()/correlate()/begin_turn()'s new
                            id-adoption and log-tagging behavior
  * settings/logging_config.py - the handler-level _TurnIdFilter and the
                            extended LOG_FORMAT
  * ui/workers.py         - AIStreamWorker/TTSWorker/ContinuousMicWorker
                            actually tagging their own thread with the
                            right id at runtime (not just that trace.py's
                            primitives work in isolation)

No display, no audio device, no network - same headless Qt stand-in as
test_tier11_voice_loop.py (reused directly: _FakeBrain already drives
trace.begin_turn()/end_turn() exactly like the real brain does).
"""
import logging
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401
from telemetry import trace
from settings import logging_config as lc
from settings.config import config

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False


def _clean_thread_local():
    trace._local.turn = None
    trace._local.begin_turn_id = None
    trace._local.log_stack = []


# =====================================================================
# telemetry/trace.py - current_turn_id() / correlate() / begin_turn()
# =====================================================================
class CorrelationPrimitivesTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        _clean_thread_local()

    def tearDown(self):
        config.TELEMETRY_ENABLED = self._enabled
        _clean_thread_local()

    def test_no_active_turn_means_no_id(self):
        self.assertIsNone(trace.current_turn_id())

    def test_begin_turn_tags_this_thread_and_end_turn_clears_it(self):
        turn_id = trace.begin_turn(source="unit")
        self.assertIsNotNone(turn_id)
        self.assertEqual(trace.current_turn_id(), turn_id)
        trace.end_turn(outcome="ok")
        self.assertIsNone(trace.current_turn_id())

    def test_begin_turn_disabled_telemetry_still_returns_none_but_still_tags_the_thread(self):
        """The existing, tested contract (begin_turn() -> None when
        TELEMETRY_ENABLED is False) must not change - log-line correlation
        is a separate, always-on concern layered on top of it, not a
        replacement for it."""
        config.TELEMETRY_ENABLED = False
        turn_id = trace.begin_turn(source="unit")
        self.assertIsNone(turn_id)
        self.assertIsNotNone(trace.current_turn_id())  # still tagged for log lines
        self.assertIsNone(trace.end_turn(outcome="ok"))  # end_turn's own contract unchanged
        self.assertIsNone(trace.current_turn_id())       # but the log tag is cleared too

    def test_double_begin_turn_without_an_outer_correlate_mints_two_different_ids(self):
        first = trace.begin_turn(source="first")
        second = trace.begin_turn(source="second")
        self.assertNotEqual(first, second)
        self.assertEqual(trace.current_turn_id(), second)
        trace.end_turn(outcome="ok")

    def test_correlate_tags_the_thread_for_the_block_and_restores_after(self):
        with trace.correlate("abc123"):
            self.assertEqual(trace.current_turn_id(), "abc123")
        self.assertIsNone(trace.current_turn_id())

    def test_correlate_nests_correctly(self):
        with trace.correlate("outer"):
            self.assertEqual(trace.current_turn_id(), "outer")
            with trace.correlate("inner"):
                self.assertEqual(trace.current_turn_id(), "inner")
            self.assertEqual(trace.current_turn_id(), "outer")
        self.assertIsNone(trace.current_turn_id())

    def test_correlate_with_a_falsy_id_is_a_no_op(self):
        with trace.correlate(None):
            self.assertIsNone(trace.current_turn_id())
        with trace.correlate(""):
            self.assertIsNone(trace.current_turn_id())

    def test_begin_turn_inside_an_outer_correlate_adopts_its_id_instead_of_minting_a_new_one(self):
        """This is the whole point: AIStreamWorker.run() wraps think_stream()
        in trace.correlate(self.ttfa_turn) - think_stream()'s own
        begin_turn(source=...) call (unchanged, no turn_id kwarg) must end
        up using THAT SAME id, not a second, unrelated one."""
        with trace.correlate("ttfa-turn-xyz"):
            turn_id = trace.begin_turn(source="think_stream")
            self.assertEqual(turn_id, "ttfa-turn-xyz")
            self.assertEqual(trace.current_turn_id(), "ttfa-turn-xyz")
            trace.end_turn(outcome="ok")
            # Falls back to the outer correlate()'s tag once the inner
            # begin_turn/end_turn pair is done - covers e.g. speak_stream()
            # running right after think_stream() on the same thread.
            self.assertEqual(trace.current_turn_id(), "ttfa-turn-xyz")

    def test_explicit_turn_id_kwarg_wins_over_an_outer_correlate(self):
        with trace.correlate("outer-id"):
            turn_id = trace.begin_turn(source="unit", turn_id="explicit-id")
            self.assertEqual(turn_id, "explicit-id")
            trace.end_turn(outcome="ok")

    def test_correlate_does_not_leak_across_threads(self):
        seen = {}

        def other_thread():
            seen["id"] = trace.current_turn_id()

        with trace.correlate("main-thread-id"):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join()
        self.assertIsNone(seen["id"])
        self.assertEqual(trace.current_turn_id(), None)

    def test_correlate_pop_only_removes_its_own_entry(self):
        """A defensive guard: if something else already popped past this
        context's own push (shouldn't happen in practice, but must never
        pop someone ELSE's tag), correlate()'s own finally is a no-op."""
        stack = trace._log_stack()
        with trace.correlate("a"):
            stack.pop()  # simulate an external imbalance
            stack.append("b")
            self.assertEqual(trace.current_turn_id(), "b")
        # "a" was already gone - correlate()'s finally must not pop "b" too.
        self.assertEqual(trace.current_turn_id(), "b")
        stack.pop()


# =====================================================================
# settings/logging_config.py - the handler-level turn-id filter
# =====================================================================
class TurnIdFilterTests(unittest.TestCase):
    def setUp(self):
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        _clean_thread_local()
        self.tmp = tempfile.mkdtemp(prefix="wave_turnid_test_")
        root = logging.getLogger()
        self._saved_handlers = list(root.handlers)
        self._saved_level = root.level
        self._saved_configured = lc._configured
        for h in list(root.handlers):
            root.removeHandler(h)

        def restore():
            root = logging.getLogger()
            for h in list(root.handlers):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
            for h in self._saved_handlers:
                root.addHandler(h)
            root.setLevel(self._saved_level)
            lc._configured = self._saved_configured
        self.addCleanup(restore)

    def tearDown(self):
        config.TELEMETRY_ENABLED = self._enabled
        _clean_thread_local()

    def log_path(self):
        return os.path.join(self.tmp, lc.LOG_FILENAME)

    def test_log_format_includes_the_turn_id_field(self):
        self.assertIn("%(turn_id)s", lc.LOG_FORMAT)

    def test_a_record_with_no_active_turn_is_stamped_with_a_placeholder(self):
        lc.setup(self.tmp, env={}, force=True)
        logging.getLogger("CorrelationSmokeTest").info("no turn active")
        with open(self.log_path(), encoding="utf-8") as f:
            line = f.read()
        self.assertIn("[-]", line)
        self.assertIn("no turn active", line)

    def test_a_record_emitted_during_an_active_turn_carries_its_id(self):
        lc.setup(self.tmp, env={}, force=True)
        with trace.correlate("corr-test-id-1"):
            logging.getLogger("CorrelationSmokeTest").info("mid turn")
        with open(self.log_path(), encoding="utf-8") as f:
            line = f.read()
        self.assertIn("[corr-test-id-1]", line)
        self.assertIn("mid turn", line)

    def test_a_record_from_a_child_logger_is_stamped_too(self):
        """The filter is attached per-HANDLER, not the root logger object -
        this is what makes it actually fire for the overwhelming majority
        of log lines, which come from per-module logging.getLogger("X")
        children, not the root logger directly (see the module's own
        reasoning in its docstring)."""
        lc.setup(self.tmp, env={}, force=True)
        with trace.correlate("corr-test-id-2"):
            logging.getLogger("brain.groq_core").warning("a child logger line")
        with open(self.log_path(), encoding="utf-8") as f:
            line = f.read()
        self.assertIn("[corr-test-id-2]", line)

    def test_filter_never_raises_even_if_trace_import_somehow_fails(self):
        f = lc._TurnIdFilter()
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
        with mock.patch.dict("sys.modules", {"telemetry.trace": None}):
            # Importing a module that's been replaced with None in
            # sys.modules raises ImportError - filter() must swallow it.
            self.assertTrue(f.filter(record))
        self.assertEqual(record.turn_id, "-")


# =====================================================================
# ui/workers.py - the actual cross-thread wiring at runtime
# =====================================================================
@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class WorkerCorrelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        import test_tier11_voice_loop as T11
        T11._install_ui_fakes()
        P4.install_stub()
        cls.workers = T5.import_with_fakes("ui.workers")
        cls._FakeBrain = T11._FakeBrain
        cls._T = T11._T

    def setUp(self):
        self._enabled = config.TELEMETRY_ENABLED
        config.TELEMETRY_ENABLED = True
        _clean_thread_local()
        from telemetry import ttfa
        self.ttfa = ttfa
        p = mock.patch.object(ttfa, "tracker", ttfa.TTFATracker(persist=False))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        config.TELEMETRY_ENABLED = self._enabled
        _clean_thread_local()

    def test_aistreamworker_tags_its_thread_for_the_whole_turn_llm_and_tts_alike(self):
        """think_stream() (router/LLM/tools, via _FakeBrain's own
        begin_turn()/end_turn() calls) and the synchronous speak_stream()
        call right after it must both see the SAME id - the whole point
        of wrapping AIStreamWorker.run() in one trace.correlate() call."""
        seen_during_llm = []
        seen_during_tts = []

        brain = self.__class__._FakeBrain(["One. ", "Two."],
                                          on_piece=lambda i: seen_during_llm.append(trace.current_turn_id()))
        w = self.workers.AIStreamWorker(brain, "q", ttfa_turn="worker-turn-id")

        def fake_speak_stream(chunks):
            for c in chunks:
                seen_during_tts.append(trace.current_turn_id())
            return True

        with mock.patch.object(self.workers.tts_engine, "speak_stream", side_effect=fake_speak_stream):
            w.run()

        self.assertEqual(seen_during_llm, ["worker-turn-id", "worker-turn-id"])
        self.assertEqual(seen_during_tts, ["worker-turn-id", "worker-turn-id"])
        # And the tag is gone once run() returns - no leak onto whatever
        # this (test) thread does next.
        self.assertIsNone(trace.current_turn_id())

    def test_aistreamworker_with_no_ttfa_turn_tags_nothing_but_still_works(self):
        brain = self.__class__._FakeBrain(["Hi."])
        w = self.workers.AIStreamWorker(brain, "q")  # ttfa_turn=None (default)
        with mock.patch.object(self.workers.tts_engine, "speak_stream", side_effect=lambda c: [x for x in c] and True):
            w.run()  # must not raise
        self.assertIsNone(trace.current_turn_id())

    def test_ttsworker_tags_its_own_thread_when_given_a_turn_id(self):
        seen = []
        w = self.workers.TTSWorker("hello", turn_id="reminder-turn-id")
        with mock.patch.object(self.workers.tts_engine, "speak_and_wait",
                                side_effect=lambda t: seen.append(trace.current_turn_id()) or True):
            w.run()
        self.assertEqual(seen, ["reminder-turn-id"])
        self.assertIsNone(trace.current_turn_id())

    def test_ttsworker_with_no_turn_id_is_a_harmless_no_op_correlate(self):
        seen = []
        w = self.workers.TTSWorker("hello")  # turn_id=None (default)
        with mock.patch.object(self.workers.tts_engine, "speak_and_wait",
                                side_effect=lambda t: seen.append(trace.current_turn_id()) or True):
            w.run()
        self.assertEqual(seen, [None])

    def test_mic_worker_tags_the_dispatch_with_the_ttfa_turn_it_just_began(self):
        worker = self.workers.ContinuousMicWorker(wake_phrases=["hey wave"])
        worker.wake_gate = None
        seen = []
        worker.text_ready.connect(
            lambda t: seen.append((t, trace.current_turn_id(), self.ttfa.tracker.active_id())))
        with mock.patch.multiple(self.workers.tts_engine, _utterance_active=False, _stream_active=False):
            worker.handle_text(self.__class__._T("Hey Wave, open Chrome", speech_end=time.monotonic(),
                                                   engine="whisper", wake_detected=False))
        self.assertEqual(len(seen), 1)
        text, tagged_id, ttfa_active_id = seen[0]
        self.assertEqual(text, "open Chrome")
        self.assertIsNotNone(tagged_id)
        # The id trace.correlate() tagged this dispatch with IS the ttfa
        # turn _begin_ttfa() just started - not a second, different one.
        self.assertEqual(tagged_id, ttfa_active_id)
        self.assertIsNone(trace.current_turn_id())  # cleared once handle_text() returns


if __name__ == "__main__":
    unittest.main()
