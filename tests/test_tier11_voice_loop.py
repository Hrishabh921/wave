"""Tier 11 - the voice loop core, minus the wake-word model itself (that
has its own file, test_tier11_wakeword.py):

  * voice/speech_filter.py - what of a streamed reply gets spoken
  * telemetry/ttfa.py     - time-to-first-audio bookkeeping
  * WaveBrain.think_stream - cancel_event, GeneratorExit, single save for
                             images, cancel_pending_confirmation()
  * ui/workers.py         - AIStreamWorker (cancel, speech filter, keep
                            streaming after the speech stops, TTFA marks,
                            route) and ContinuousMicWorker's TTFA hand-off
  * ui/main_window.py     - send_message() no longer bypasses the brain,
                            voice stop / barge-in handling, the live reply
                            bubble, mic level -> waveform
  * ui/widgets_basic.py   - AudioVisualizer.set_level
  * brain/intent_router.py - the rules that replaced the UI's keyword layer
  * voice/stt.py          - Transcript timestamps

No display, no audio device, no network: Qt is the stand-in from
test_phase4_ui_logic.py, the LLM is _fake_openai.FakeOpenAI.
"""
import json
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False


# =====================================================================
# voice/speech_filter.py
# =====================================================================
class SpeechFilterTests(unittest.TestCase):
    def setUp(self):
        from voice import speech_filter
        self.sf = speech_filter

    def feed_all(self, chunks):
        f = self.sf.SpeechFilter()
        out = [f.feed(c) for c in chunks]
        out.append(f.flush())
        return out

    def test_plain_text_passes_through_immediately_chunk_by_chunk(self):
        out = self.feed_all(["Hello ", "there, how ", "are you?"])
        self.assertEqual(out[:3], ["Hello ", "there, how ", "are you?"])
        self.assertEqual("".join(out), "Hello there, how are you?")

    def test_a_code_block_is_spoken_as_one_short_note_even_when_split_across_chunks(self):
        chunks = ["Here you go:\n`", "``py", "thon\nprint('hi')\nx = 1\n`", "``\nThat prints hi."]
        spoken = "".join(self.feed_all(chunks))
        self.assertNotIn("print('hi')", spoken)
        self.assertNotIn("x = 1", spoken)
        self.assertNotIn("python", spoken)
        self.assertEqual(spoken.count("Code block omitted."), 1)
        self.assertIn("Here you go:", spoken)
        self.assertIn("That prints hi.", spoken)

    def test_an_unterminated_code_block_at_the_end_is_still_swallowed(self):
        spoken = "".join(self.feed_all(["Try this:\n```bash\nrm -rf build\n"]))
        self.assertNotIn("rm -rf", spoken)
        self.assertIn("Code block omitted.", spoken)

    def test_table_rows_become_comma_lists_and_separator_rows_vanish(self):
        spoken = "".join(self.feed_all(["| Name | Age |\n|---|:--:|\n| Asha | 30 |\n", "Done."]))
        self.assertIn("Name, Age", spoken)
        self.assertIn("Asha, 30", spoken)
        self.assertNotIn("|", spoken)
        self.assertNotIn("---", spoken)

    def test_inline_code_at_the_start_of_a_line_is_text_not_a_fence(self):
        spoken = "".join(self.feed_all(["`pip install x` is the command"]))
        self.assertEqual(spoken, "`pip install x` is the command")   # backticks go in clean_for_speech

    def test_one_or_two_backticks_are_held_until_decided(self):
        f = self.sf.SpeechFilter()
        self.assertEqual(f.feed("`"), "")
        self.assertEqual(f.feed("`"), "")
        self.assertEqual(f.feed("x"), "``x")

    def test_filter_stream_and_for_speech_agree(self):
        text = "Intro.\n```\ncode\n```\n| a | b |\n|---|---|\n| 1 | 2 |\nOutro."
        streamed = "".join(self.sf.filter_stream(iter([text[:7], text[7:20], text[20:]])))
        self.assertEqual(streamed, self.sf.for_speech(text))

    def test_main_window_speech_helper_uses_the_same_rules(self):
        """_plain_text_for_speech used to have its own regexes - it now
        delegates, so a replayed reply and a streamed one sound the same."""
        from voice import speech_filter
        self.assertEqual(speech_filter.for_speech("a\n```\nb\n```\nc"), "a\n Code block omitted. c")


# =====================================================================
# telemetry/ttfa.py
# =====================================================================
class TTFATrackerTests(unittest.TestCase):
    def setUp(self):
        from telemetry import ttfa
        self.ttfa = ttfa
        self.tmp = S.make_tmpdir(self)
        self.log = os.path.join(self.tmp, "ttfa.jsonl")
        self.tracker = ttfa.TTFATracker(log_path=self.log)

    def test_a_voice_turn_is_timed_from_speech_end(self):
        t0 = time.monotonic()
        tid = self.tracker.begin("voice", speech_end=t0, captured_at=t0 + 0.5, recognized_at=t0 + 0.8,
                                 heard="open chrome")
        self.tracker.mark("dispatched", t=t0 + 0.81)
        self.tracker.mark("first_token", t=t0 + 0.9)
        self.tracker.mark("first_audio", t=t0 + 1.2)
        row = self.tracker.finish("ok", route="intent_router")
        self.assertEqual(row["turn_id"], tid)
        self.assertAlmostEqual(row["ttfa_ms"], 1200.0, delta=0.5)
        self.assertAlmostEqual(row["stt_ms"], 300.0, delta=0.5)
        self.assertAlmostEqual(row["first_token_ms"], 900.0, delta=0.5)
        self.assertEqual(row["route"], "intent_router")
        self.assertEqual(row["heard"], "open chrome")
        self.assertEqual(list(row["marks_ms"]), ["capture_end", "recognized", "dispatched", "first_token", "first_audio"])

    def test_first_mark_wins(self):
        self.tracker.begin("typed")
        self.tracker.mark("first_audio", t=time.monotonic())
        first = self.tracker._active.marks["first_audio"]
        self.tracker.mark("first_audio", t=time.monotonic() + 5)
        self.assertEqual(self.tracker._active.marks["first_audio"], first)

    def test_marks_scoped_to_a_turn_id_never_land_on_a_newer_turn(self):
        old = self.tracker.begin("voice")
        new = self.tracker.begin("voice")
        self.tracker.mark("first_token", turn_id=old)
        self.assertNotIn("first_token", self.tracker._active.marks)
        self.assertIsNone(self.tracker.finish("ok", turn_id=old))
        self.assertEqual(self.tracker.finish("ok", turn_id=new)["turn_id"], new)

    def test_a_turn_left_open_is_recorded_as_superseded(self):
        self.tracker.begin("voice", heard="first")
        self.tracker.begin("voice", heard="second")
        rows = self.tracker.recent()
        self.assertEqual((rows[0]["heard"], rows[0]["outcome"]), ("first", "superseded"))

    def test_rows_are_appended_as_json_lines(self):
        self.tracker.begin("voice", heard="a")
        self.tracker.finish("ok")
        self.tracker.begin("typed", heard="b")
        self.tracker.finish("ok")
        with open(self.log, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([r["heard"] for r in rows], ["a", "b"])

    def test_the_log_rotates_instead_of_growing_forever(self):
        with mock.patch.object(self.ttfa, "_LOG_MAX_BYTES", 200):
            for i in range(10):
                self.tracker.begin("voice", heard=f"utterance number {i}")
                self.tracker.finish("ok")
        self.assertTrue(os.path.exists(self.log + ".1"))
        self.assertLess(os.path.getsize(self.log), 1000)

    def test_disabled_telemetry_makes_everything_a_no_op(self):
        with mock.patch.object(self.ttfa.config, "TELEMETRY_ENABLED", False):
            self.assertIsNone(self.tracker.begin("voice"))
            self.tracker.mark("first_audio")
            self.assertIsNone(self.tracker.finish("ok"))
        self.assertFalse(os.path.exists(self.log))

    def test_percentile_is_nearest_rank(self):
        p = self.ttfa.percentile
        vals = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        self.assertEqual(p(vals, 50), 500)
        self.assertEqual(p(vals, 90), 900)
        self.assertEqual(p([42], 50), 42)
        self.assertIsNone(p([], 50))
        self.assertEqual(p([None, 3, None], 50), 3)

    def test_the_tts_listener_marks_first_audio(self):
        self.tracker.begin("wake")
        self.tracker.on_audio_start()
        self.assertIn("first_audio", self.tracker._active.marks)


# =====================================================================
# WaveBrain.think_stream - cancellation, partial saves, single save
# =====================================================================
from _fake_openai import Reply, completion, sse_body, sse_chunk, sse_tool_call_delta, tool_call  # noqa: E402
from test_phase4_brain import BrainBase  # noqa: E402


class _FlipEvent:
    """is_set() is False for the first `after` checks, True from then on -
    lets a test cancel a turn at an exact checkpoint inside think_stream."""

    def __init__(self, after):
        self.after, self.calls = after, 0

    def is_set(self):
        self.calls += 1
        return self.calls > self.after


class ThinkStreamTier11Tests(BrainBase):
    def assistant_rows(self):
        from database.db_manager import db
        return [msg for role, msg in db.get_chat_history(limit=50) if role == "assistant"]

    def test_closing_the_stream_part_way_saves_the_partial_reply_marked_interrupted(self):
        from telemetry import trace
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant", "content": "Hello "}),
            sse_chunk(delta={"content": "there. "}),
            sse_chunk(delta={"content": "And a lot more after this."}),
            sse_chunk(delta={}, finish_reason="stop"),
        ))]
        gen = self.brain.think_stream("tell me something")
        self.assertEqual([next(gen), next(gen)], ["Hello ", "there. "])
        gen.close()
        self.assertEqual(self.assistant_rows()[-1], "Hello there." + self.gc.INTERRUPTED_MARKER)
        self.assertEqual(trace.get_recent(1)[0]["outcome"], "interrupted")

    def test_a_cancelled_turn_never_runs_the_tool_the_model_asked_for(self):
        self.srv.script = [Reply(200, sse_body(
            sse_chunk(delta={"role": "assistant", "tool_calls": [
                sse_tool_call_delta(0, id="call_1", name="lock_pc", arguments="{}"),
            ]}),
            sse_chunk(delta={}, finish_reason="tool_calls"),
        ))]
        with mock.patch.object(self.brain, "_dispatch_tool") as dispatch:
            chunks = list(self.brain.think_stream("lock my pc", cancel_event=_FlipEvent(after=1)))
        dispatch.assert_not_called()
        self.assertEqual(chunks, [])
        self.assertEqual(len(self.srv.posts()), 1)

    def test_cancel_before_the_first_round_makes_no_llm_request(self):
        ev = threading.Event()
        ev.set()
        self.assertEqual(list(self.brain.think_stream("hello", cancel_event=ev)), [])
        self.assertEqual(self.srv.posts(), [])

    def test_no_cancel_event_behaves_exactly_as_before(self):
        self.srv.script = [Reply(200, sse_body(sse_chunk(delta={"content": "Hi!"}, finish_reason="stop")))]
        self.assertEqual("".join(self.brain.think_stream("hello")), "Hi!")
        self.assertEqual(self.assistant_rows()[-1], "Hi!")

    def test_an_image_reply_is_saved_once_not_twice(self):
        img = os.path.join(self.tmp, "cat.png")
        with open(img, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        self.srv.script = [Reply(200, completion("A sleepy cat."))]
        before = len(self.assistant_rows())
        chunks = list(self.brain.think_stream("what is this", attachment={"kind": "image", "path": img}))
        self.assertEqual(chunks, ["A sleepy cat."])
        self.assertEqual(len(self.assistant_rows()) - before, 1)

    def test_a_fallback_after_a_mid_stream_failure_starts_a_new_paragraph(self):
        from _fake_openai import sse_error
        self.srv.script = [Reply(200, sse_chunk(delta={"content": "Partial"}) + sse_error("boom") + "data: [DONE]\n\n")]
        with mock.patch.object(self.brain, "_think_with_ollama", return_value="Fallback answer."):
            chunks = list(self.brain.think_stream("question"))
        self.assertEqual(chunks, ["Partial", "\n\nFallback answer."])


class CancelPendingConfirmationTests(BrainBase):
    def test_drops_an_armed_confirmation_and_audits_it(self):
        open(os.path.join(self.desktop, "secret.txt"), "w").close()
        self.srv.script = [
            Reply(200, completion(None, [tool_call("delete_file", {"file_name": "secret.txt"})])),
            Reply(200, completion("Delete secret.txt - you sure?")),
        ]
        self.brain.think("delete secret.txt")
        self.assertIsNotNone(self.brain.pending_action)
        self.assertTrue(self.brain.cancel_pending_confirmation())
        self.assertIsNone(self.brain.pending_action)
        self.assertTrue(os.path.exists(os.path.join(self.desktop, "secret.txt")))
        self.assertIn("User cancelled (stop).", [e.result for e in self.audit.recent()])
        self.assertFalse(self.brain.cancel_pending_confirmation())


# =====================================================================
# WaveBrain.think - Tier 19 cancel_event (mirrors think_stream's Tier 11
# cancel_event above, minus the streaming/partial-save mechanics think()
# never had to begin with)
# =====================================================================
class ThinkTier19CancelTests(BrainBase):
    def assistant_rows(self):
        from database.db_manager import db
        return [msg for role, msg in db.get_chat_history(limit=50) if role == "assistant"]

    def test_a_cancelled_turn_never_runs_the_tool_the_model_asked_for(self):
        self.srv.script = [Reply(200, completion(None, [tool_call("lock_pc", {})]))]
        with mock.patch.object(self.brain, "_dispatch_tool") as dispatch:
            reply = self.brain.think("lock my pc", cancel_event=_FlipEvent(after=1))
        dispatch.assert_not_called()
        self.assertEqual(reply, self.gc.INTERRUPTED_MARKER.strip())
        self.assertEqual(len(self.srv.posts()), 1)

    def test_cancel_before_the_first_round_makes_no_llm_request(self):
        ev = threading.Event()
        ev.set()
        reply = self.brain.think("hello", cancel_event=ev)
        self.assertEqual(reply, self.gc.INTERRUPTED_MARKER.strip())
        self.assertEqual(self.srv.posts(), [])

    def test_no_cancel_event_behaves_exactly_as_before(self):
        self.srv.script = [Reply(200, completion("Hi!"))]
        self.assertEqual(self.brain.think("hello"), "Hi!")

    def test_a_cancelled_turn_saves_nothing_to_history(self):
        before = len(self.assistant_rows())
        ev = threading.Event()
        ev.set()
        self.brain.think("hello", cancel_event=ev)
        self.assertEqual(len(self.assistant_rows()), before)

    def test_cancelling_marks_the_trace_row_interrupted(self):
        from telemetry import trace
        ev = threading.Event()
        ev.set()
        self.brain.think("hello", cancel_event=ev)
        self.assertEqual(trace.get_recent(1)[0]["outcome"], "interrupted")


# =====================================================================
# WaveBrain._think_with_ollama - Tier 19 cancel_event, same checkpoints
# (before a round, before each tool) as think()/think_stream() above.
# A minimal fake 'ollama' client - _get_ollama_client() hands back
# self._ollama_client directly once it's set, so the real 'ollama'
# package/import is never needed here.
# =====================================================================
class _OllamaMsg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _OllamaResp:
    def __init__(self, message):
        self.message = message


class _OllamaToolCall:
    def __init__(self, name, arguments):
        self.function = types.SimpleNamespace(name=name, arguments=arguments)


class _FakeOllamaClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        return self.replies.pop(0)


class OllamaFallbackCancelTests(BrainBase):
    def use_ollama(self, replies):
        from settings.config import config
        self.brain._ollama_client = _FakeOllamaClient(replies)
        p = mock.patch.object(config, "OLLAMA_ENABLED", True)
        p.start()
        self.addCleanup(p.stop)
        return self.brain._ollama_client

    def test_cancel_before_dispatching_the_tool_it_asked_for(self):
        client = self.use_ollama([_OllamaResp(_OllamaMsg(tool_calls=[_OllamaToolCall("lock_pc", "{}")]))])
        with mock.patch.object(self.brain, "_dispatch_tool") as dispatch:
            reply = self.brain._think_with_ollama("lock my pc", cancel_event=_FlipEvent(after=1))
        dispatch.assert_not_called()
        self.assertIsNone(reply)
        self.assertEqual(client.calls, 1)

    def test_cancel_before_the_first_round_makes_no_ollama_request(self):
        client = self.use_ollama([])
        ev = threading.Event()
        ev.set()
        reply = self.brain._think_with_ollama("hello", cancel_event=ev)
        self.assertIsNone(reply)
        self.assertEqual(client.calls, 0)

    def test_no_cancel_event_behaves_exactly_as_before(self):
        self.use_ollama([_OllamaResp(_OllamaMsg(content="Hi from ollama!"))])
        reply = self.brain._think_with_ollama("hello")
        self.assertEqual(reply, "Hi from ollama!")


class OllamaTimeoutWiringTests(BrainBase):
    """Tier 19: config.OLLAMA_TIMEOUT genuinely bounds every Ollama
    request - verified (not assumed) by installing the real ollama==0.6.3
    package and reading its source: Client.__init__ forwards timeout=
    straight into the httpx.Client it builds internally, which genuinely
    bounds every request made through it. That's an upstream-library fact,
    not something to re-derive here each run - what IS worth a regression
    test is the one line actually under this codebase's control: that
    WaveBrain really does pass config.OLLAMA_TIMEOUT through when building
    the client, so a future refactor can't silently drop it."""

    def test_ollama_client_is_constructed_with_the_configured_timeout(self):
        import ollama
        with mock.patch.object(self.gc.config, "OLLAMA_ENABLED", True), \
                mock.patch.object(self.gc.config, "OLLAMA_TIMEOUT", 7), \
                mock.patch.object(self.gc.config, "OLLAMA_HOST", "http://example-ollama:11434"), \
                mock.patch.object(ollama, "Client") as client_cls:
            self.brain._ollama_client = None   # force _get_ollama_client() to actually build one
            client = self.brain._get_ollama_client()
        client_cls.assert_called_once_with(host="http://example-ollama:11434", timeout=7)
        self.assertIs(client, client_cls.return_value)

    def test_a_disabled_feature_never_touches_the_ollama_client_at_all(self):
        import ollama
        with mock.patch.object(self.gc.config, "OLLAMA_ENABLED", False), \
                mock.patch.object(ollama, "Client") as client_cls:
            self.brain._ollama_client = None
            client = self.brain._get_ollama_client()
        self.assertIsNone(client)
        client_cls.assert_not_called()


# =====================================================================
# ui/workers.py - AIStreamWorker and ContinuousMicWorker (Qt stand-in)
# =====================================================================
def _install_ui_fakes():
    S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                          hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                          typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
    S.install_fake_module("pywhatkit")


class _FakeBrain:
    def __init__(self, pieces, on_piece=None, accepts_cancel=True, route="tool_loop"):
        self.pieces, self.on_piece, self.route = pieces, on_piece, route
        self.accepts_cancel = accepts_cancel
        self.closed = False
        self.cancel_event = None

    def think_stream(self, text, attachment=None, **kw):
        if kw and not self.accepts_cancel:
            raise TypeError("unexpected keyword argument 'cancel_event'")
        self.cancel_event = kw.get("cancel_event")
        return self._gen()

    def _gen(self):
        from telemetry import trace
        trace.begin_turn(source="think_stream")
        try:
            for i, p in enumerate(self.pieces):
                if self.on_piece:
                    self.on_piece(i)
                yield p
            trace.end_turn(outcome="ok", route=self.route)
        except GeneratorExit:
            self.closed = True
            trace.end_turn(outcome="interrupted", route=self.route)
            raise


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class AIStreamWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        _install_ui_fakes()
        P4.install_stub()
        cls.workers = T5.import_with_fakes("ui.workers")

    def setUp(self):
        from telemetry import ttfa
        self.ttfa = ttfa
        p = mock.patch.object(ttfa, "tracker", ttfa.TTFATracker(persist=False))
        p.start()
        self.addCleanup(p.stop)

    def collect(self, signal):
        seen = []
        signal.connect(lambda *a: seen.append(a))
        return seen

    def run_worker(self, brain, spoken=None, speak_limit=None, **kw):
        spoken = [] if spoken is None else spoken

        def fake_speak_stream(chunks):
            for i, c in enumerate(chunks):
                spoken.append(c)
                if speak_limit is not None and i + 1 >= speak_limit:
                    return True      # speech stopped early (e.g. a reminder) - turn NOT cancelled
            return True

        w = self.workers.AIStreamWorker(brain, "q", **kw)
        chunks, done, speech = self.collect(w.text_chunk), self.collect(w.response_ready), self.collect(w.finished_speaking)
        with mock.patch.object(self.workers.tts_engine, "speak_stream", side_effect=fake_speak_stream):
            w.run()
        return w, chunks, done, speech, spoken

    def test_the_bubble_gets_raw_markdown_and_the_voice_gets_filtered_text(self):
        brain = _FakeBrain(["Run:\n", "```bash\nls -la\n```\n", "Done."])
        w, chunks, done, speech, spoken = self.run_worker(brain)
        self.assertEqual(done, [("Run:\n```bash\nls -la\n```\nDone.",)])
        self.assertNotIn("ls -la", "".join(spoken))
        self.assertIn("Code block omitted.", "".join(spoken))
        self.assertEqual(speech, [(True,)])

    def test_cancel_ends_the_turn_and_closes_the_brains_stream(self):
        holder = {}
        brain = _FakeBrain(["One. ", "Two. ", "Three."], on_piece=lambda i: i == 1 and holder["w"].cancel())
        w = self.workers.AIStreamWorker(brain, "q")
        holder["w"] = w
        done = self.collect(w.response_ready)
        with mock.patch.object(self.workers.tts_engine, "speak_stream", side_effect=lambda c: [x for x in c] and True):
            w.run()
        self.assertTrue(w.cancelled)
        self.assertEqual(done, [("One. ",)])
        self.assertTrue(brain.closed)
        self.assertIs(brain.cancel_event, w._cancel)

    def test_text_keeps_streaming_when_only_the_speech_was_stopped(self):
        brain = _FakeBrain(["One. ", "Two. ", "Three."])
        w, chunks, done, speech, spoken = self.run_worker(brain, speak_limit=1)
        self.assertEqual(done, [("One. Two. Three.",)])
        self.assertEqual(len(chunks), 3)
        self.assertFalse(w.cancelled)

    def test_a_brain_without_cancel_support_still_works(self):
        brain = _FakeBrain(["Hi."], accepts_cancel=False)
        w, chunks, done, speech, spoken = self.run_worker(brain)
        self.assertEqual(done, [("Hi.",)])

    def test_route_and_ttfa_marks_are_recorded_for_its_own_turn(self):
        tid = self.ttfa.tracker.begin("voice", speech_end=time.monotonic())
        brain = _FakeBrain(["Hi ", "there."], route="intent_router")
        w, *_ = self.run_worker(brain, ttfa_turn=tid)
        self.assertEqual(w.route, "intent_router")
        marks = self.ttfa.tracker._active.marks
        self.assertIn("first_token", marks)
        self.assertIn("reply_done", marks)
        self.assertEqual(self.ttfa.tracker._active.meta["route"], "intent_router")

    def test_a_brain_failure_is_shown_as_text_not_reported_as_a_playback_failure(self):
        class Broken:
            def think_stream(self, text, attachment=None, **kw):
                raise RuntimeError("bug in the brain")
                yield  # pragma: no cover
        w, chunks, done, speech, spoken = self.run_worker(Broken())
        self.assertEqual(done, [("Something went wrong while I was replying.",)])
        self.assertEqual(speech, [(True,)], "the UI must still get finished_speaking to reset its state")

    def test_no_speech_means_no_finished_speaking_signal(self):
        brain = _FakeBrain(["Hi."])
        w, chunks, done, speech, spoken = self.run_worker(brain, speak_reply=False)
        self.assertEqual((done, speech, spoken), ([("Hi.",)], [], []))


class _T(str):
    """A stand-in for voice/stt.py's Transcript."""

    def __new__(cls, text, **attrs):
        obj = super().__new__(cls, text)
        obj.__dict__.update(attrs)
        return obj


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MicWorkerTier11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        _install_ui_fakes()
        P4.install_stub()
        cls.workers = T5.import_with_fakes("ui.workers")

    def setUp(self):
        from telemetry import ttfa
        self.ttfa = ttfa
        p = mock.patch.object(ttfa, "tracker", ttfa.TTFATracker(persist=False))
        p.start()
        self.addCleanup(p.stop)
        self.worker = self.workers.ContinuousMicWorker(wake_phrases=["hey wave"])
        self.worker.wake_gate = None
        self.texts, self.modes, self.stops, self.interrupts = [], [], [], []
        self.worker.text_ready.connect(lambda t: self.texts.append(t))
        self.worker.mode_changed.connect(lambda m, a: self.modes.append((m, a)))
        self.worker.stop_requested.connect(lambda: self.stops.append(1))
        self.worker.interruption_signal.connect(lambda: self.interrupts.append(1))

    def speaking(self, flag):
        return mock.patch.multiple(self.workers.tts_engine, _utterance_active=flag, _stream_active=False)

    def test_wake_and_command_in_one_breath_dispatches_without_announcing(self):
        t0 = time.monotonic()
        with self.speaking(False):
            self.worker.handle_text(_T("Hey Wave, open Chrome", speech_end=t0, captured_at=t0 + 0.6,
                                       recognized_at=t0 + 0.9, engine="whisper", wake_detected=False))
        self.assertEqual(self.texts, ["open Chrome"])
        self.assertEqual(self.modes, [("AWAKE", False)])
        turn = self.ttfa.tracker._active
        self.assertEqual(turn.source, "voice")
        self.assertEqual(turn.t0, t0)
        self.assertEqual(set(turn.marks), {"capture_end", "recognized"})
        self.assertEqual(turn.meta["heard"], "open Chrome")

    def test_a_plain_wake_announces_and_opens_a_wake_turn(self):
        with self.speaking(False):
            self.worker.handle_text(_T("hey wave", speech_end=time.monotonic()))
        self.assertEqual(self.modes, [("AWAKE", True)])
        self.assertEqual(self.ttfa.tracker.active_source(), "wake")

    def test_stop_while_speaking_interrupts_and_requests_a_stop(self):
        self.worker.mode = "AWAKE"
        with self.speaking(True):
            self.worker.handle_text(_T("ruk jao", speech_end=time.monotonic()))
        self.assertEqual((self.interrupts, self.stops, self.texts), ([1], [1], []))
        self.assertEqual(self.ttfa.tracker.active_source(), "stop")

    def test_unaddressed_speech_during_a_reply_is_dropped_by_the_barge_in_gate(self):
        self.worker.mode = "AWAKE"
        with self.speaking(True):
            self.worker.handle_text(_T("what's on tv tonight", speech_end=time.monotonic()))
        self.assertEqual((self.interrupts, self.stops, self.texts), ([], [], []))
        self.assertIsNone(self.ttfa.tracker.active_source())

    def test_new_question_interrupts_and_asks_the_new_thing(self):
        self.worker.mode = "AWAKE"
        with self.speaking(True):
            self.worker.handle_text(_T("new question: what time is it", speech_end=time.monotonic()))
        self.assertEqual((self.interrupts, self.stops, self.texts), ([1], [1], ["what time is it"]))


# =====================================================================
# ui/main_window.py (Qt stand-in, MainWindow built with object.__new__)
# =====================================================================
class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class _FakeStreamWorker:
    instances = []

    def __init__(self, brain, text, attachment=None, speak_reply=True, ttfa_turn=None):
        self.brain, self.text, self.attachment = brain, text, attachment
        self.speak_reply, self.ttfa_turn = speak_reply, ttfa_turn
        self.text_chunk, self.response_ready, self.finished_speaking = _Sig(), _Sig(), _Sig()
        # Tier 25: avatar/control.py's MainWindow._start_reply() connects to
        # this one too now - same fake-signal shape as the three above.
        self.started_speaking = _Sig()
        self.finished = _Sig()
        self.cancelled = False
        self.started = False
        _FakeStreamWorker.instances.append(self)

    def cancel(self):
        self.cancelled = True

    def start(self):
        self.started = True


class _FakeBubble:
    def __init__(self, text):
        self.texts = [text]

    def set_text(self, text, max_width=None):
        self.texts.append(text)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MainWindowVoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        cls.P4 = P4
        _install_ui_fakes()
        P4.install_stub()
        for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
            del sys.modules[name]
        for _ in range(30):
            try:
                import ui.main_window as mw
                break
            except ModuleNotFoundError as e:
                missing = (e.name or "").split(".")[0]
                if not missing or missing in sys.modules:
                    raise
                sys.modules[missing] = P4._QtModule(missing)
        cls.mw = mw

    def setUp(self):
        from telemetry import ttfa
        self.ttfa = ttfa
        p = mock.patch.object(ttfa, "tracker", ttfa.TTFATracker(persist=False))
        p.start()
        self.addCleanup(p.stop)
        _FakeStreamWorker.instances = []
        win = object.__new__(self.mw.MainWindow)
        win.theme = types.SimpleNamespace(warning="#fa0", accent_1="#0af", accent_2="#f0a", accent_3="#ff0",
                                          success="#0f0", text_muted="#888", bg_surface="#111", border="#333")
        win.toast = self.P4.FakeToast()
        win.brain = mock.Mock()
        win.brain.cancel_pending_confirmation.return_value = False
        win.mic_worker = types.SimpleNamespace(mode="AWAKE")
        win.visualizer = mock.Mock()
        win._pending_attachment = None
        win._reply_worker = None
        win._voice_ttfa_turn = None
        win._active_workers = []
        win.statuses = []
        win._set_status = lambda text, color: win.statuses.append(text)
        win.chat_input = mock.Mock()
        win.btn_send = mock.Mock()
        win.bubbles = []
        self.icon_calls = []
        p_icon = mock.patch.object(self.mw.Icon, "set_btn_icon",
                                    side_effect=lambda btn, name, *a, **k: self.icon_calls.append(name))
        p_icon.start()
        self.addCleanup(p_icon.stop)

        def add_bubble(text, is_user=True, attachment=None):
            b = _FakeBubble(text)
            win.bubbles.append((is_user, b))
            return b
        win.add_chat_bubble = add_bubble
        win._clear_attachment = lambda: None
        win._scroll_chat_to_bottom = lambda delay_ms=80: None
        win._bubble_max_width = lambda text: 420
        self.win = win
        for target, attr in ((self.mw, "AIStreamWorker"),):
            p = mock.patch.object(target, attr, _FakeStreamWorker)
            p.start()
            self.addCleanup(p.stop)
        self.tts_stop = mock.patch.object(self.mw.tts_engine, "stop").start()
        self.addCleanup(mock.patch.stopall)

    def send(self, text):
        self.win.chat_input.toPlainText.return_value = text
        self.win.send_message()
        return _FakeStreamWorker.instances[-1] if _FakeStreamWorker.instances else None

    def test_former_keyword_shortcuts_now_all_go_through_the_brain(self):
        """The old send_message() called AppController directly for these -
        skipping the confirm gate (WhatsApp is T3, close_app T2), the audit
        log and undo."""
        boom = mock.Mock(side_effect=AssertionError("UI called AppController directly"))
        with mock.patch.multiple(self.mw.AppController, send_whatsapp_message=boom, close_app=boom,
                                 open_app=boom, write_in_notepad=boom, take_screenshot=boom,
                                 toggle_hardware=boom, delete_latest_screenshot=boom):
            for text in ("send hi to mom on whatsapp", "close chrome", "kill notepad", "open spotify",
                         "write milk in notepad", "take a screenshot", "turn off wifi", "delete the screenshot"):
                worker = self.send(text)
                self.assertEqual(worker.text, text)
                self.assertTrue(worker.started)
        self.assertEqual(len(_FakeStreamWorker.instances), 8)

    def test_a_new_message_cancels_the_reply_still_streaming(self):
        first = self.send("tell me a long story")
        second = self.send("actually, what time is it")
        self.assertTrue(first.cancelled)
        self.assertFalse(second.cancelled)
        self.assertIs(self.win._reply_worker, second)

    def test_a_typed_message_opens_a_typed_ttfa_turn_and_a_voice_one_reuses_the_mic_turn(self):
        worker = self.send("hello")
        self.assertEqual(self.ttfa.tracker.active_source(), "typed")
        self.assertEqual(worker.ttfa_turn, self.ttfa.tracker.active_id())
        voice_turn = self.ttfa.tracker.begin("voice", speech_end=time.monotonic())
        self.win.chat_input.toPlainText.return_value = "open chrome"
        self.win.process_voice_input("open chrome")
        self.assertEqual(_FakeStreamWorker.instances[-1].ttfa_turn, voice_turn)
        self.assertIn("dispatched", self.ttfa.tracker._active.marks)

    def test_streamed_chunks_grow_the_bubble_and_done_finalizes_it(self):
        worker = self.send("hi")
        bubble = self.win.bubbles[-1][1]
        self.assertEqual(bubble.texts, [self.win.REPLY_PLACEHOLDER])
        scheduled = []
        with mock.patch.object(self.mw.QTimer, "singleShot", side_effect=lambda ms, fn: scheduled.append(fn)):
            self.win._on_reply_chunk(worker, "Hello ")
            self.win._on_reply_chunk(worker, "there.")
        self.assertEqual(len(scheduled), 1, "re-render should be throttled to one pending timer")
        scheduled[0]()
        self.assertEqual(bubble.texts[-1], "Hello there.")
        self.assertIn("Speaking...", self.win.statuses)
        self.win._on_reply_done(worker, "Hello there.")
        self.assertEqual(bubble.texts[-1], "Hello there.")
        self.assertIsNone(self.win._reply_worker)

    def test_a_stopped_reply_keeps_its_text_and_says_it_was_stopped(self):
        worker = self.send("hi")
        self.win._on_voice_stop()
        self.assertTrue(worker.cancelled)
        self.win._on_reply_done(worker, "Partial answer")
        bubble = self.win.bubbles[-1][1]
        self.assertTrue(bubble.texts[-1].startswith("Partial answer"))
        self.assertTrue(bubble.texts[-1].endswith(self.win.REPLY_STOPPED_MARK))

    def test_voice_stop_also_drops_a_pending_confirmation(self):
        self.win.brain.cancel_pending_confirmation.return_value = True
        self.win._on_voice_stop()
        self.win.brain.cancel_pending_confirmation.assert_called_once()
        self.tts_stop.assert_called()
        self.assertEqual(self.win.toast.shown[-1][0], "Okay, cancelled - I won't do that.")

    def test_a_stale_reply_cannot_reset_the_ui_of_the_current_one(self):
        old = self.send("one")
        new = self.send("two")
        self.win.statuses.clear()
        self.win._on_reply_chunk(old, "late text")
        self.win._on_reply_speech_finished(old, True)
        self.assertEqual(self.win.statuses, [])
        self.assertIs(self.win._reply_worker, new)

    def test_barge_in_cancels_the_reply_and_stops_the_voice(self):
        worker = self.send("hi")
        self.win._on_barge_in()
        self.assertTrue(worker.cancelled)
        self.tts_stop.assert_called()

    # -----------------------------------------------------------------
    # Tier 19: the send button doubles as Stop while a reply streams.
    # -----------------------------------------------------------------
    def test_send_button_flips_to_stop_while_a_reply_streams_and_back_when_done(self):
        worker = self.send("hi")
        self.assertEqual(self.icon_calls[-1], "stop")
        self.win._on_reply_done(worker, "Hello!")
        self.assertEqual(self.icon_calls[-1], "send")

    def test_send_button_flips_back_to_send_the_instant_a_reply_is_cancelled(self):
        self.send("hi")
        self.assertEqual(self.icon_calls[-1], "stop")
        self.win._on_voice_stop()
        self.assertEqual(self.icon_calls[-1], "send")

    def test_clicking_send_button_while_idle_sends(self):
        self.win.chat_input.toPlainText.return_value = "hello"
        self.win._on_send_btn_clicked()
        self.assertEqual(len(_FakeStreamWorker.instances), 1)
        self.assertEqual(_FakeStreamWorker.instances[-1].text, "hello")

    def test_clicking_send_button_while_a_reply_streams_stops_it_instead_of_sending(self):
        worker = self.send("tell me a long story")
        self.win.chat_input.toPlainText.return_value = "a second message"
        self.win._on_send_btn_clicked()
        self.assertTrue(worker.cancelled)
        self.assertEqual(len(_FakeStreamWorker.instances), 1,
                          "the click must have stopped the reply, not sent a second message")
        self.assertIsNone(self.win._reply_worker)

    def test_stop_button_click_cancels_and_shows_the_stopped_toast(self):
        worker = self.send("hi")
        self.win._on_stop_button_clicked()
        self.assertTrue(worker.cancelled)
        self.assertEqual(self.win.toast.shown[-1][0], "⏹ Stopped")

    def test_stop_button_click_while_idle_shows_no_toast(self):
        before = len(self.win.toast.shown)
        self.win._on_stop_button_clicked()
        self.assertEqual(len(self.win.toast.shown), before)

    def test_wake_announcement_is_skipped_when_a_command_came_with_it(self):
        win = self.win
        win.btn_mic_inline, win.mic_mini_lbl = mock.Mock(), mock.Mock()
        with mock.patch.object(self.mw, "add_shadow", lambda *a, **k: None), \
                mock.patch.object(type(win), "start_speaking") as speak:
            win.handle_wake_word_mode("AWAKE", False)
            speak.assert_not_called()
            win.handle_wake_word_mode("AWAKE", True)
            speak.assert_called_once_with("Systems active.")

    def test_turn_on_mic_wires_mode_changed_level_and_stop(self):
        fake = types.SimpleNamespace(text_ready=_Sig(), mode_update=_Sig(), mode_changed=_Sig(),
                                     interruption_signal=_Sig(), stop_requested=_Sig(), audio_level=_Sig(),
                                     recognition_failed=_Sig(), partial_text=_Sig(), utterance_done=_Sig(),
                                     start=lambda: None)
        with mock.patch.object(self.mw, "ContinuousMicWorker", lambda wake_phrases=None: fake):
            self.win.turn_on_mic()
        self.assertEqual(fake.mode_update.slots, [], "mode_update would announce 'Systems active.' twice")
        self.assertEqual(len(fake.mode_changed.slots), 1)
        fake.audio_level.emit(0.7)
        self.win.visualizer.set_level.assert_called_once_with(0.7)
        self.assertEqual(len(fake.stop_requested.slots), 1)

    def test_replayed_text_uses_the_shared_speech_filter(self):
        self.assertNotIn("secret_code", self.win._plain_text_for_speech("Look:\n```\nsecret_code()\n```\nok"))


class _FakeWorker:
    """A minimal QThread-like stand-in for emergency_stop()'s tests below:
    .cancel() flips .cancelled exactly the way AIStreamWorker's real one
    does (see ui/workers.py) - unlike a bare Mock, whose attributes don't
    react to its own method calls - and .wait()/.terminate() are spies
    these tests assert against directly."""

    def __init__(self, cancelled=False):
        self.cancelled = cancelled
        self.wait_calls = []
        self.terminate_calls = 0

    def cancel(self):
        self.cancelled = True

    def wait(self, ms=None):
        self.wait_calls.append(ms)
        return True

    def terminate(self):
        self.terminate_calls += 1


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class EmergencyStopTests(unittest.TestCase):
    """Tier 19: emergency_stop()'s cooperative grace-period loop, added in
    front of its pre-existing hard-terminate loop. That terminate loop had
    no tests at all before this - these cover both, since the two are now
    one continuous behavior (ask nicely, then force)."""

    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        cls.P4 = P4
        _install_ui_fakes()
        P4.install_stub()
        for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
            del sys.modules[name]
        for _ in range(30):
            try:
                import ui.main_window as mw
                break
            except ModuleNotFoundError as e:
                missing = (e.name or "").split(".")[0]
                if not missing or missing in sys.modules:
                    raise
                sys.modules[missing] = P4._QtModule(missing)
        cls.mw = mw

    def setUp(self):
        win = object.__new__(self.mw.MainWindow)
        win.theme = types.SimpleNamespace(warning="#fa0", accent_1="#0af", accent_2="#f0a", accent_3="#ff0",
                                          success="#0f0", text_muted="#888", bg_surface="#111", border="#333")
        win.toast = self.P4.FakeToast()
        win.mic_worker = mock.Mock()
        win.visualizer = mock.Mock()
        win.worker_pool = mock.Mock()
        win._reply_worker = None
        win._active_workers = []
        win.btn_send = mock.Mock()
        win.statuses = []
        win._set_status = lambda text, color: win.statuses.append(text)
        win.bubbles = []
        win.add_chat_bubble = lambda text, is_user=True, attachment=None: win.bubbles.append(text)
        self.win = win
        p_icon = mock.patch.object(self.mw.Icon, "set_btn_icon")
        p_icon.start()
        self.addCleanup(p_icon.stop)
        mock.patch.object(self.mw.tts_engine, "stop").start()
        self.addCleanup(mock.patch.stopall)

    def test_a_worker_cancelled_by_this_call_is_given_a_grace_wait_before_terminate(self):
        reply_worker = _FakeWorker()   # not cancelled yet - emergency_stop's own _cancel_reply() does that
        self.win._reply_worker = reply_worker
        self.win._active_workers = [reply_worker]
        self.win.emergency_stop()
        self.assertTrue(reply_worker.cancelled)
        self.assertIn(self.win.EMERGENCY_STOP_GRACE_MS, reply_worker.wait_calls)
        self.assertEqual(reply_worker.terminate_calls, 1)

    def test_a_worker_that_was_never_cancelled_gets_no_grace_wait(self):
        action_worker = _FakeWorker(cancelled=False)
        self.win._active_workers = [action_worker]
        self.win.emergency_stop()
        self.assertEqual(action_worker.wait_calls, [1000])   # only the terminate loop's own wait
        self.assertEqual(action_worker.terminate_calls, 1)

    def test_everything_is_still_hard_terminated_after_the_grace_period(self):
        """The grace period is an addition, not a replacement - a
        cooperatively-cancelled worker is still terminated if it hasn't
        actually exited once the grace window elapses."""
        reply_worker = _FakeWorker(cancelled=True)
        self.win._active_workers = [reply_worker]
        self.win.emergency_stop()
        self.assertEqual(reply_worker.terminate_calls, 1)

    def test_active_workers_are_cleared_and_the_mic_goes_to_sleep(self):
        self.win._active_workers = [_FakeWorker()]
        self.win.emergency_stop()
        self.assertEqual(self.win._active_workers, [])
        self.win.mic_worker.set_mode.assert_called_once_with("SLEEPING")
        self.win.worker_pool.shutdown.assert_called_once()

    def test_shows_a_toast_and_a_chat_bubble(self):
        self.win.emergency_stop()
        self.assertIn("halted", self.win.toast.shown[-1][0])
        self.assertTrue(any("Emergency stop" in b for b in self.win.bubbles))


# =====================================================================
# ui/widgets_basic.py - AudioVisualizer.set_level
# =====================================================================
@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class VisualizerLevelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        _install_ui_fakes()
        P4.install_stub()
        cls.wb = T5.import_with_fakes("ui.widgets_basic")

    def make(self):
        vis = object.__new__(self.wb.AudioVisualizer)
        self.wb.AudioVisualizer.__init__(vis, theme=types.SimpleNamespace())
        return vis

    def test_fresh_levels_are_used_and_stale_ones_are_not(self):
        vis = self.make()
        self.assertIsNone(vis._live_level())
        vis.set_level(0.8)
        self.assertEqual(vis._live_level(), 0.8)
        vis._level_at -= 5
        self.assertIsNone(vis._live_level())

    def test_out_of_range_and_junk_values_are_clamped_or_ignored(self):
        vis = self.make()
        vis.set_level(3.0)
        self.assertEqual(vis._level, 1.0)
        vis.set_level(-1)
        self.assertEqual(vis._level, 0.0)
        vis.set_level("loud")
        self.assertEqual(vis._level, 0.0)

    def test_the_newest_level_sits_in_the_middle_and_older_ones_ripple_out(self):
        vis = self.make()
        vis._level_targets(5, 30.0, 0.0)
        heights = vis._level_targets(5, 30.0, 1.0)
        self.assertEqual(heights[2], 30.0)                 # centre: newest (loud)
        self.assertEqual(heights[1], heights[3])           # mirrored
        self.assertEqual(heights[1], 3.0)                  # one step out: the previous (silent) reading


# =====================================================================
# brain/intent_router.py - what replaced the UI's keyword layer
# =====================================================================
class RouterTier11Tests(unittest.TestCase):
    def route(self, text):
        from brain import intent_router
        r = intent_router.route(text)
        return (r.skill, r.args) if r else None

    def test_settings_pages(self):
        self.assertEqual(self.route("open display settings"), ("open_system_setting", {"setting": "display"}))
        self.assertEqual(self.route("Open the Wi-Fi settings"), ("open_system_setting", {"setting": "wifi"}))
        self.assertEqual(self.route("open night light settings"), ("open_system_setting", {"setting": "nightlight"}))
        self.assertEqual(self.route("settings kholo"), ("open_system_setting", {"setting": "settings"}))
        self.assertEqual(self.route("सेटिंग्स खोलो"), ("open_system_setting", {"setting": "settings"}))
        self.assertEqual(self.route("open windows update settings"), ("open_system_setting", {"setting": "update"}))

    def test_an_apps_own_settings_are_not_a_system_page(self):
        self.assertEqual(self.route("open chrome settings"), ("open_app", {"app_name": "chrome settings"}))

    def test_notepad_text_keeps_its_casing(self):
        self.assertEqual(self.route("write Buy MILK at 5 in notepad"), ("write_notepad", {"text": "Buy MILK at 5"}))
        self.assertEqual(self.route("notepad mein Doodh lana likho"), ("write_notepad", {"text": "Doodh lana"}))
        self.assertEqual(self.route("नोटपैड में दूध लाना लिखो"), ("write_notepad", {"text": "दूध लाना"}))
        self.assertIsNone(self.route("write a poem about rain"))

    def test_kill_closes_through_the_confirm_gated_tool_but_quit_is_left_alone(self):
        self.assertEqual(self.route("kill chrome"), ("close_app", {"app_name": "chrome"}))
        self.assertIsNone(self.route("quit smoking tips"))

    def test_hardware_verbs_beyond_turn_on_off(self):
        self.assertEqual(self.route("disable wifi")[0], "toggle_hardware")
        self.assertEqual(self.route("enable bluetooth")[0], "toggle_hardware")
        self.assertEqual(self.route("wifi band karo"), ("toggle_hardware", {"instruction": "wifi band karo"}))
        self.assertEqual(self.route("ब्लूटूथ बंद करो"), ("toggle_hardware", {"instruction": "bluetooth बंद करो"}))

    def test_the_guards_still_apply(self):
        self.assertIsNone(self.route("don't open display settings"))
        self.assertIsNone(self.route("write hi in notepad and then close it"))


# =====================================================================
# voice/stt.py - Transcript timestamps
# =====================================================================
class TranscriptTimestampTests(unittest.TestCase):
    def setUp(self):
        from test_phase5_stt_aec_wiring import _install_fakes, _FakeAudioData
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod
        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()
        self.receiver.recognizer.listen = lambda source, timeout=1, phrase_time_limit=8: _FakeAudioData(b"x", 16000, 2)
        self.receiver.recognizer.recognize_google = mock.Mock(return_value="open chrome")
        for name in ("_apply_aec", "_passes_vad", "_recognize_with_whisper", "is_headphone_connected"):
            p = mock.patch.object(self.receiver, name)
            m = p.start()
            self.addCleanup(p.stop)
            setattr(self, name, m)
        self._apply_aec.side_effect = lambda a: a
        self._passes_vad.return_value = True
        self._recognize_with_whisper.return_value = None
        self.is_headphone_connected.return_value = False
        self.sttmod.tts_engine.is_speaking = False
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in (
            "voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language")])

    def test_speech_end_is_capture_end_minus_the_silence_timeout(self):
        before = time.monotonic()
        result = self.receiver.listen()
        after = time.monotonic()
        self.assertEqual(result, "open chrome")
        pause = self.receiver.recognizer.pause_threshold
        self.assertAlmostEqual(result.captured_at - result.speech_end, pause, places=6)
        self.assertTrue(before <= result.captured_at <= result.recognized_at <= after)
        self.assertEqual(result.engine, "google")


if __name__ == "__main__":
    unittest.main()
