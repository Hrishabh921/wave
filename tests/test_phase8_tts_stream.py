"""Phase 8 / Tier 2 item 5 - progressive TTS (voice/tts.py's speak_stream()).

Reuses test_phase5_tts_fallback.py's _install_fakes() to get a real,
constructible VoiceEngine without touching actual audio hardware or the
network - but rather than re-faking the whole pygame/edge-tts playback
stack again, the per-sentence playback call is mocked here. That's the
right boundary: one utterance's own success/failure behavior is already
pinned down by test_phase5_tts_fallback.py; what these tests exercise is
the buffering/sentence-splitting/stop-handling ABOVE it.

Tier 11 turned speak_stream() into a pipeline (the text source is read on
the calling thread while a speaker thread plays, and every sentence is
prefetched as soon as it's cut). The mocked boundary moved with it, from
speak_and_wait() to _speak_stream_piece(), and the tests that depended on
the old strictly-serial timing now wait on events instead of assuming
"spoken before the generator resumes". Every assertion about WHAT gets
spoken - splitting, order, nothing lost or duplicated, failure
propagation, stop semantics - is unchanged.
"""
import sys
import threading
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401
from test_phase5_tts_fallback import _install_fakes


class SpeakStreamTests(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod
        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        # Prefetching is exercised by its own tests below; keep it out of
        # the way of the splitting/ordering tests.
        p = mock.patch.object(self.engine, "prefetch", return_value=False)
        self.prefetch = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in
                                 ("voice.tts", "voice.aec", "voice.language")])

    def mock_speak(self, side_effect=None):
        """Patches the per-sentence playback call to record calls instead of
        synthesizing/playing anything - returns the Mock so tests can
        inspect call_args_list."""
        return mock.patch.object(self.engine, "_speak_stream_piece",
                                 side_effect=side_effect or (lambda text: True))

    # ---------- basic buffering / sentence boundaries ----------
    def test_empty_stream_speaks_nothing_and_reports_success(self):
        with self.mock_speak() as m:
            self.assertTrue(self.engine.speak_stream(iter([])))
            m.assert_not_called()

    def test_whitespace_and_falsy_chunks_are_skipped(self):
        with self.mock_speak() as m:
            self.assertTrue(self.engine.speak_stream(iter(["", None, "   ", ""])))
            m.assert_not_called()

    def test_one_sentence_arriving_across_several_chunks_is_spoken_once_complete(self):
        with self.mock_speak() as m:
            ok = self.engine.speak_stream(iter(["Hello", " there", "."]))
            self.assertTrue(ok)
            m.assert_called_once_with("Hello there.")

    def test_each_complete_sentence_is_spoken_before_the_stream_ends(self):
        """The progressive property: the first sentence starts playing
        while the text source is still producing the rest - speak_stream
        never waits for the whole reply."""
        first_spoken = threading.Event()
        seen_mid_stream = []

        def recording_speak(text):
            if text == "First sentence.":
                first_spoken.set()
            return True

        def chunks():
            yield "First sentence. "
            seen_mid_stream.append(first_spoken.wait(2.0))
            yield "Second sentence."

        with self.mock_speak(side_effect=recording_speak) as m:
            self.engine.speak_stream(chunks())
        self.assertEqual(seen_mid_stream, [True], "first sentence wasn't spoken until the stream ended")
        self.assertEqual(m.call_args_list, [mock.call("First sentence."), mock.call("Second sentence.")])

    def test_trailing_sentence_with_no_closing_punctuation_is_still_spoken(self):
        with self.mock_speak() as m:
            self.engine.speak_stream(iter(["Hello there, no period"]))
        m.assert_called_once_with("Hello there, no period")

    def test_hindi_danda_is_recognized_as_a_sentence_boundary(self):
        with self.mock_speak() as m:
            self.engine.speak_stream(iter(["Namaste।", " Aap kaise hain?"]))
        self.assertEqual(m.call_args_list, [mock.call("Namaste।"), mock.call("Aap kaise hain?")])

    def test_long_run_on_with_no_punctuation_is_force_split_at_a_word_boundary(self):
        """Soft-cap check (_STREAM_MAX_BUFFER_CHARS): a run-on with no
        sentence-ending punctuation must still start talking instead of
        buffering forever, and the forced split must land between words,
        never inside one."""
        long_text = "lorem " * 60   # 360 chars, no punctuation at all
        with self.mock_speak() as m:
            self.engine.speak_stream(iter([long_text]))
        self.assertGreaterEqual(m.call_count, 2, "should have been force-split at least once")
        for call in m.call_args_list:
            spoken = call.args[0]
            self.assertTrue(spoken)
            for word in spoken.split():
                self.assertEqual(word, "lorem", f"a word got cut in half: {word!r}")
        # Nothing lost or duplicated in the split.
        self.assertEqual(" ".join(c.args[0] for c in m.call_args_list).split(), ["lorem"] * 60)

    def test_long_run_on_with_commas_is_force_split_at_the_last_clause_boundary_in_the_window(self):
        """Tier 7: when a run-on with no sentence-ending punctuation DOES
        contain comma(s) within the buffered _STREAM_MAX_BUFFER_CHARS
        window, the forced split prefers the LAST clause boundary in
        that window over the plain word-boundary cut (more natural
        spoken as two pieces), and keeps the comma attached to the piece
        that gets spoken rather than the leftover buffer. Two commas are
        placed here - an early one ("alpha,") and a later one ("beta,")
        still inside the window - specifically so this also proves the
        LAST one in the window wins, not just any comma."""
        long_text = "alpha, " + "lorem " * 30 + "beta, " + "dolor " * 20
        with self.mock_speak() as m:
            self.engine.speak_stream(iter([long_text]))
        self.assertGreaterEqual(m.call_count, 2, "should have been force-split at least once")
        first_piece = m.call_args_list[0].args[0]
        self.assertTrue(
            first_piece.endswith("beta,"),
            f"expected the cut at the LAST comma in the window ('beta,'), got: {first_piece!r}",
        )
        # Nothing lost or duplicated across the whole split.
        rebuilt = " ".join(c.args[0] for c in m.call_args_list).replace(",", "")
        self.assertEqual(rebuilt.split(), long_text.replace(",", "").split())

    def test_long_run_on_with_a_semicolon_is_also_treated_as_a_clause_boundary(self):
        """Same as the comma case above, but for ';' - the other
        punctuation mark the splitter treats as a clause boundary."""
        long_text = "lorem " * 20 + "ipsum; " + "dolor " * 40
        with self.mock_speak() as m:
            self.engine.speak_stream(iter([long_text]))
        self.assertGreaterEqual(m.call_count, 2, "should have been force-split at least once")
        first_piece = m.call_args_list[0].args[0]
        self.assertTrue(
            first_piece.endswith("ipsum;"),
            f"expected the cut right after the semicolon, got: {first_piece!r}",
        )
        rebuilt = " ".join(c.args[0] for c in m.call_args_list).replace(";", "")
        self.assertEqual(rebuilt.split(), long_text.replace(";", "").split())

    def test_run_on_with_only_an_early_comma_still_falls_back_sanely(self):
        """A single comma sitting right near the very start of the window
        (cut index 0) must not produce a degenerate empty-or-near-empty
        first piece - cut > 0 is required before the clause-boundary path
        is preferred, same guard the pre-existing word-boundary cut
        already used."""
        long_text = ", " + "lorem " * 60   # comma is literally the first character
        with self.mock_speak() as m:
            self.engine.speak_stream(iter([long_text]))
        for call in m.call_args_list:
            self.assertTrue(call.args[0].strip(), "must never speak an empty/whitespace-only piece")
        rebuilt = " ".join(c.args[0] for c in m.call_args_list).replace(",", "")
        self.assertEqual(rebuilt.split(), long_text.replace(",", "").split())

    # ---------- interruption ----------
    def test_stop_before_the_first_sentence_arrives_speaks_nothing(self):
        def chunks():
            self.engine.stop()          # e.g. "stop" said while Wave was still thinking
            yield "Hello there."

        with self.mock_speak() as m:
            ok = self.engine.speak_stream(chunks())
        self.assertTrue(ok)
        m.assert_not_called()

    def test_a_stop_left_over_from_an_earlier_utterance_does_not_silence_a_new_stream(self):
        """Tier 11 fix: stop_event used to stay set after an interruption,
        and the next streamed reply then broke out before speaking a
        single word."""
        self.engine.stop()
        with self.mock_speak() as m:
            ok = self.engine.speak_stream(iter(["Hello there."]))
        self.assertTrue(ok)
        m.assert_called_once_with("Hello there.")

    def test_stop_partway_through_stops_pulling_and_speaking_further_chunks(self):
        first_spoken = threading.Event()
        pulled_after_stop = []

        def speak(text):
            first_spoken.set()
            return True

        def chunks():
            yield "First sentence. "
            first_spoken.wait(2.0)
            self.engine.stop()           # simulated external interruption
            yield "Second sentence. "
            pulled_after_stop.append("third")
            yield "Third sentence."

        with self.mock_speak(side_effect=speak) as m:
            ok = self.engine.speak_stream(chunks())
        self.assertTrue(ok)   # an interruption is not a playback failure
        m.assert_called_once_with("First sentence.")
        self.assertEqual(pulled_after_stop, [], "kept reading the stream after stop()")

    def test_stop_while_a_sentence_plays_cancels_the_queued_ones(self):
        """A stop() during sentence 1 must not let sentences 2 and 3 - already
        queued, even already prefetched - start afterwards."""
        playing = threading.Event()

        def speak(text):
            if text == "One.":
                playing.set()
                self.engine.stop()       # the user barges in mid-sentence
            return True

        def chunks():
            yield "One. Two. Three. "
            playing.wait(2.0)
            yield "Four."

        with self.mock_speak(side_effect=speak) as m:
            self.engine.speak_stream(chunks())
        self.assertEqual(m.call_args_list, [mock.call("One.")])

    # ---------- failure propagation ----------
    def test_one_failed_sentence_makes_the_overall_result_false_but_others_still_play(self):
        results = iter([True, False, True])
        with self.mock_speak(side_effect=lambda text: next(results)) as m:
            ok = self.engine.speak_stream(iter(["One. ", "Two. ", "Three."]))
        self.assertFalse(ok)
        self.assertEqual(m.call_count, 3)

    def test_all_sentences_succeeding_reports_true(self):
        with self.mock_speak() as m:
            ok = self.engine.speak_stream(iter(["One. ", "Two."]))
        self.assertTrue(ok)
        self.assertEqual(m.call_count, 2)

    def test_a_piece_that_raises_counts_as_a_failure_and_the_rest_still_play(self):
        def speak(text):
            if text == "Two.":
                raise RuntimeError("audio device vanished")
            return True

        with self.mock_speak(side_effect=speak) as m:
            ok = self.engine.speak_stream(iter(["One. Two. Three."]))
        self.assertFalse(ok)
        self.assertEqual([c.args[0] for c in m.call_args_list], ["One.", "Two.", "Three."])


class StreamPipelineTests(unittest.TestCase):
    """Tier 11: what makes speak_stream() a pipeline rather than a loop."""

    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod
        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in
                                 ("voice.tts", "voice.aec", "voice.language")])

    def test_text_keeps_being_read_while_a_sentence_is_still_playing(self):
        """With the old serial loop this deadlocks: sentence 1 waits for
        the stream to finish, and the stream waits for sentence 1."""
        everything_read = threading.Event()
        waited_ok = []

        def slow_speak(text):
            if text == "One.":
                waited_ok.append(everything_read.wait(2.0))
            return True

        def chunks():
            yield "One. "
            yield "Two. "
            yield "Three."
            everything_read.set()

        with mock.patch.object(self.engine, "prefetch", return_value=False), \
                mock.patch.object(self.engine, "_speak_stream_piece", side_effect=slow_speak):
            self.engine.speak_stream(chunks())
        self.assertEqual(waited_ok, [True])

    def test_every_sentence_is_prefetched_as_soon_as_it_is_cut(self):
        prefetched_while_one_played = []
        prefetched = []

        def speak(text):
            if text == "One.":
                deadline = time.monotonic() + 2.0
                while "Two." not in prefetched and time.monotonic() < deadline:
                    time.sleep(0.01)
                prefetched_while_one_played.append(list(prefetched))
            return True

        def chunks():
            yield "One. "
            yield "Two."

        with mock.patch.object(self.engine, "prefetch", side_effect=lambda t: prefetched.append(t) or True), \
                mock.patch.object(self.engine, "_speak_stream_piece", side_effect=speak):
            self.engine.speak_stream(chunks())
        # (the speaker thread also tops up prefetches - repeats are no-ops)
        self.assertEqual(list(dict.fromkeys(prefetched)), ["One.", "Two."])
        self.assertEqual(list(dict.fromkeys(prefetched_while_one_played[0])), ["One.", "Two."],
                         "sentence two wasn't being synthesized while sentence one played")

    def test_sentences_beyond_the_prefetch_cap_are_topped_up_while_earlier_ones_play(self):
        """Seven sentences arrive at once; only _PREFETCH_MAX_PENDING can be
        queued for synthesis then - the rest are prefetched by the speaker
        thread just before they're needed, never synthesized cold."""
        cold = []

        def synth(key, gen):
            return b"audio"

        def play(piece):
            key = (piece, self.engine.voice, self.engine.rate)
            if self.engine._take_prefetched(key) is None:
                cold.append(piece)
            return True

        text = "One. Two. Three. Four. Five. Six. Seven."
        with mock.patch.object(self.engine, "_synthesize_to_bytes", side_effect=synth), \
                mock.patch.object(self.engine, "_speak_stream_piece", side_effect=play):
            self.engine.speak_stream(iter([text]))
        self.assertEqual(cold, [], f"synthesized at playback time: {cold}")

    def test_the_pipeline_hides_synthesis_behind_playback(self):
        """The point of Tier 11's pipeline, measured: with synthesis taking S
        and playback P per sentence, the old loop took N x (S + P); now
        only the first synthesis is on the clock: about S + N x P."""
        S_, P_, N = 0.15, 0.15, 4

        def synth(key, gen):
            time.sleep(S_)
            return b"audio"

        def play(piece):
            self.engine._take_prefetched((piece, self.engine.voice, self.engine.rate))
            time.sleep(P_)
            return True

        with mock.patch.object(self.engine, "_synthesize_to_bytes", side_effect=synth), \
                mock.patch.object(self.engine, "_speak_stream_piece", side_effect=play):
            t0 = time.monotonic()
            self.engine.speak_stream(iter(["One. Two. Three. Four."]))
            elapsed = time.monotonic() - t0
        serial = N * (S_ + P_)
        self.assertLess(elapsed, serial - 0.25, f"{elapsed:.2f}s vs {serial:.2f}s for the old serial loop")

    def test_is_speaking_stays_true_between_queued_sentences_and_false_after(self):
        seen = []

        def speak(text):
            seen.append(self.engine.is_speaking)
            return True

        with mock.patch.object(self.engine, "prefetch", return_value=False), \
                mock.patch.object(self.engine, "_speak_stream_piece", side_effect=speak):
            self.engine.speak_stream(iter(["One. Two. Three."]))
        self.assertEqual(seen, [True, True, True])
        self.assertFalse(self.engine.is_speaking)

    def test_prefetched_audio_is_played_without_a_second_synthesis(self):
        from voice.tts_cache import phrase_cache
        phrase_cache.clear()
        self.addCleanup(phrase_cache.clear)
        fake_comm = sys.modules["edge_tts"].Communicate
        fake_comm.call_count = 0
        self.assertTrue(self.engine.prefetch("Hello there."))
        key = ("Hello there.", self.engine.voice, self.engine.rate)
        self.engine._prefetch_futures[key].result(timeout=5)
        self.assertEqual(fake_comm.call_count, 1)
        self.assertTrue(self.engine.speak_and_wait("Hello there."))
        self.assertEqual(fake_comm.call_count, 1, "synthesized again instead of using the prefetch")

    def test_prefetch_is_skipped_for_the_local_voice_and_for_cached_phrases(self):
        from voice.tts_cache import phrase_cache
        phrase_cache.clear()
        self.addCleanup(phrase_cache.clear)
        with mock.patch.object(self.ttsmod.config, "TTS_PREFER_LOCAL_VOICE", True):
            self.assertFalse(self.engine.prefetch("Hello."))
        phrase_cache.put(("Cached.", self.engine.voice, self.engine.rate), b"x")
        self.assertFalse(self.engine.prefetch("Cached."))
        with mock.patch.object(self.ttsmod.config, "TTS_PREFETCH_ENABLED", False):
            self.assertFalse(self.engine.prefetch("Hello."))
        self.assertEqual(self.engine._prefetch_futures, {})

    def test_stop_drops_pending_prefetches(self):
        gate = threading.Event()
        with mock.patch.object(self.engine, "_synthesize_to_bytes", side_effect=lambda k, g: gate.wait(2) and None):
            self.engine.prefetch("One.")
            self.engine.prefetch("Two.")
            self.assertEqual(len(self.engine._prefetch_futures), 2)
            self.engine.stop()
            self.assertEqual(self.engine._prefetch_futures, {})
            gate.set()

    def test_warm_up_fills_the_phrase_cache_once_and_gives_up_quietly_offline(self):
        from voice.tts_cache import phrase_cache
        phrase_cache.clear()
        self.addCleanup(phrase_cache.clear)
        fake_comm = sys.modules["edge_tts"].Communicate
        fake_comm.call_count = 0
        self.engine.warm_up(["Systems active.", "Standing by."]).join(5)
        self.assertTrue(phrase_cache.contains(("Systems active.", self.engine.voice, self.engine.rate)))
        self.assertTrue(phrase_cache.contains(("Standing by.", self.engine.voice, self.engine.rate)))
        self.assertEqual(fake_comm.call_count, 2)
        self.engine.warm_up(["Systems active.", "Standing by."]).join(5)
        self.assertEqual(fake_comm.call_count, 2, "already-cached phrases were synthesized again")
        phrase_cache.clear()
        fake_comm.call_count = 0
        fake_comm.should_fail = True
        self.addCleanup(setattr, fake_comm, "should_fail", False)
        self.engine.warm_up(["One.", "Two.", "Three."]).join(5)
        self.assertEqual(fake_comm.call_count, 1, "kept retrying while offline")

    def test_playback_start_listener_fires_once_per_utterance(self):
        fired = []
        self.engine.on_playback_start = lambda: fired.append(time.monotonic())
        self.assertTrue(self.engine.speak_and_wait("Hello there."))
        self.assertEqual(len(fired), 1)
        self.assertTrue(self.engine.speak_and_wait("Second one."))
        self.assertEqual(len(fired), 2)

    def test_a_broken_playback_listener_never_breaks_speech(self):
        def boom():
            raise RuntimeError("listener bug")
        self.engine.on_playback_start = boom
        self.assertTrue(self.engine.speak_and_wait("Hello there."))


if __name__ == "__main__":
    unittest.main()
