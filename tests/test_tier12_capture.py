"""Tier 12 - the capture pipeline rebuild: streaming endpointing
(voice/endpoint.py), the continuous mic stream with its watchdog
(voice/capture.py, against a fake sounddevice), Whisper hygiene
(voice/transcript_filter.py) and SpeechReceiver's stream path
(voice/stt.py).

No microphone needed: audio is synthesized (tones for "speech", gaussian
noise for room tone) and the fake sounddevice calls the stream callback
from its own thread like PortAudio does.
"""
import math
import random
import struct
import sys
import threading
import time
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401

from voice.endpoint import EndpointConfig, Endpointer, frame_dbfs
from voice import transcript_filter as tf

SR = 16000
FRAME = 320                      # 20 ms


def tone(ms, amp=6000, f=220.0, start=0):
    n = SR * ms // 1000
    return struct.pack(f"<{n}h", *[int(amp * math.sin(2 * math.pi * f * (i + start) / SR)) for i in range(n)])


def noise(ms, amp=30, seed=None):
    rnd = random.Random(seed)
    n = SR * ms // 1000
    return struct.pack(f"<{n}h", *[max(-32768, min(32767, int(rnd.gauss(0, amp)))) for _ in range(n)])


def frames_of(pcm):
    fb = FRAME * 2
    return [pcm[i:i + fb] for i in range(0, len(pcm) - fb + 1, fb)]


def run(ep, pcm, t0=0.0):
    out, t = [], t0
    for f in frames_of(pcm):
        t += 0.02
        u = ep.feed(f, t)
        if u:
            out.append(u)
    return out, t


class EndpointerTests(unittest.TestCase):
    def ep(self, **kw):
        return Endpointer(EndpointConfig(**kw), vad=None)

    def test_one_utterance_with_exact_speech_end_and_pre_roll(self):
        utts, _ = run(self.ep(), noise(1000) + tone(1200) + noise(1000))
        self.assertEqual(len(utts), 1)
        u = utts[0]
        self.assertAlmostEqual(u.speech_start, 1.0, delta=0.07)
        self.assertAlmostEqual(u.speech_end, 2.2, delta=0.03)
        self.assertAlmostEqual(u.ended_at - u.speech_end, 0.46, delta=0.05)   # end_silence_ms
        # pre-roll (300 ms) + speech + a short tail, not the whole silence after
        self.assertAlmostEqual(u.duration_s, 0.3 + 1.2 + 0.12, delta=0.08)
        self.assertGreater(u.snr_db, 30)

    def test_a_pause_shorter_than_the_end_silence_keeps_one_utterance(self):
        utts, _ = run(self.ep(), noise(500) + tone(600) + noise(300) + tone(600) + noise(800))
        self.assertEqual(len(utts), 1)
        self.assertAlmostEqual(utts[0].speech_ms, 1200, delta=60)

    def test_a_longer_pause_splits_it(self):
        utts, _ = run(self.ep(), noise(500) + tone(600) + noise(800) + tone(600) + noise(800))
        self.assertEqual(len(utts), 2)

    def test_no_fixed_cap_a_thirty_second_command_is_one_utterance(self):
        speech = b"".join(tone(900, start=i) + noise(200) for i in range(30))    # pauses of 200 ms
        utts, _ = run(self.ep(), noise(500) + speech + noise(800))
        self.assertEqual(len(utts), 1)
        self.assertGreater(utts[0].duration_s, 30)
        self.assertFalse(utts[0].truncated)

    def test_the_safety_cap_still_ends_a_runaway_one(self):
        talk = b"".join(tone(220, start=i) + noise(100) for i in range(30))   # ~9.6 s, syllable-like
        utts, _ = run(self.ep(max_s=5.0), noise(500) + talk)
        self.assertTrue(utts and utts[0].truncated)
        self.assertLessEqual(utts[0].duration_s, 5.5)

    def test_a_steady_noise_that_never_dips_is_not_one_endless_utterance(self):
        """A fan or AC switching on: the floor rises to it within a few
        seconds and capture goes quiet again (speech_recognition would sit
        'listening' to it until the phrase cap)."""
        ep = self.ep()
        utts, _ = run(ep, noise(1000) + tone(12000, amp=900, f=120.0))
        self.assertFalse(ep.in_speech)
        self.assertTrue(all(u.duration_s < 6 for u in utts))

    def test_clicks_and_coughs_shorter_than_min_speech_are_dropped(self):
        utts, _ = run(self.ep(), noise(800) + tone(100) + noise(800))
        self.assertEqual(utts, [])

    def test_the_noise_floor_follows_the_room(self):
        ep = self.ep()
        run(ep, noise(3000, amp=30))
        quiet = ep.floor_dbfs
        run(ep, noise(6000, amp=600))
        self.assertGreater(ep.floor_dbfs, quiet + 15)
        # speech that would have counted in the quiet room doesn't in the loud one
        utts, _ = run(ep, tone(800, amp=700) + noise(1000, amp=600))
        self.assertEqual(utts, [])

    def test_a_vad_veto_keeps_loud_non_speech_out(self):
        ep = Endpointer(EndpointConfig(), vad=lambda frame, sr: False)
        utts, _ = run(ep, noise(500) + tone(1500) + noise(800))
        self.assertEqual(utts, [])

    def test_flush_ends_an_utterance_in_progress(self):
        ep = self.ep()
        _, t = run(ep, noise(500) + tone(800))
        self.assertTrue(ep.in_speech)
        u = ep.flush(t)
        self.assertIsNotNone(u)
        self.assertFalse(ep.in_speech)

    def test_the_gain_floor_keeps_faint_room_sounds_away_from_whisper(self):
        ep = Endpointer(EndpointConfig(), vad=None)
        very_quiet_room = noise(4000, amp=2, seed=7)          # about -84 dBFS
        faint = tone(800, amp=30)                             # about -64 dBFS: 20 dB over that floor
        utts, _ = run(ep, very_quiet_room + faint + very_quiet_room[:32000])
        self.assertEqual(utts, [])
        utts, _ = run(ep, tone(800) + very_quiet_room[:32000])  # normal speech still gets through
        self.assertEqual(len(utts), 1)

    def test_frame_dbfs(self):
        self.assertLess(frame_dbfs(b"\x00\x00" * 320), -90)
        self.assertAlmostEqual(frame_dbfs(tone(20, amp=32767)), -3.0, delta=0.5)


class TranscriptFilterTests(unittest.TestCase):
    def seg(self, text, lp=-0.2, nsp=0.05, cr=1.1, dur=1.0):
        return tf.SegmentInfo(text, lp, nsp, cr, dur)

    def test_known_hallucinations_on_short_or_quiet_audio_are_rejected(self):
        for text in ("Thank you.", "Thanks for watching!", "you", "Subtitles by the Amara.org community", "धन्यवाद"):
            v = tf.judge(text, [self.seg(text, lp=-0.5, nsp=0.3, dur=0.6)], audio_s=0.6, snr_db=8)
            self.assertEqual(v.text, "", text)

    def test_but_a_clearly_spoken_thank_you_survives(self):
        v = tf.judge("Thank you.", [self.seg("Thank you.", lp=-0.1, nsp=0.01, dur=1.8)], audio_s=1.8, snr_db=25)
        self.assertEqual(v.text, "Thank you.")

    def test_bracketed_tags_and_empty_text_are_rejected(self):
        for text in ("[Music]", "(applause)", "♪ ♪", "", "  ...  "):
            self.assertEqual(tf.judge(text, []).text, "", text)

    def test_low_confidence_is_rejected_and_confidence_is_reported(self):
        v = tf.judge("what is the weather", [self.seg("what is the weather", lp=-1.8, nsp=0.7)])
        self.assertEqual(v.text, "")
        v = tf.judge("what is the weather", [self.seg("what is the weather", lp=-0.15, nsp=0.02)])
        self.assertEqual(v.text, "what is the weather")
        self.assertGreater(v.confidence, 0.8)

    def test_degenerate_repetition_is_rejected_and_loops_are_collapsed(self):
        self.assertEqual(tf.judge("go go go go go go", [self.seg("go go go go go go", cr=3.1)]).text, "")
        loop = "I'm going to go. I'm going to go. I'm going to go. I'm going to go."
        self.assertEqual(tf.collapse_repetitions(loop), "I'm going to go.")
        self.assertEqual(tf.collapse_repetitions("open open chrome"), "open open chrome")

    def test_a_transcript_without_whisper_numbers_is_trusted(self):
        v = tf.judge("open chrome", None)
        self.assertEqual((v.text, v.confidence), ("open chrome", 1.0))


class _FakeInputStream:
    instances = []

    def __init__(self, samplerate, channels, dtype, blocksize, device, callback):
        self.callback, self.blocksize = callback, blocksize
        self.started = self.closed = False
        self.feed = []          # raw blocks to deliver
        self.alive = True
        _FakeInputStream.instances.append(self)

    def start(self):
        self.started = True
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        while self.started and not self.closed:
            if self.feed and self.alive:
                block = self.feed.pop(0)
                self.callback(block, len(block) // 2, None, None)
            else:
                time.sleep(0.002)

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


def _fake_sd(device_names):
    sd = types.SimpleNamespace()
    sd.InputStream = _FakeInputStream
    state = {"i": 0}

    def query_devices(kind=None):
        return {"name": device_names[min(state["i"], len(device_names) - 1)]}
    sd.query_devices = query_devices
    sd._state = state
    return sd


class CaptureEngineTests(unittest.TestCase):
    def setUp(self):
        _FakeInputStream.instances = []
        import voice.capture as cap
        self.cap = cap

    def engine(self, sd, **kw):
        e = self.cap.CaptureEngine(EndpointConfig(), aec=None, sd_module=sd, **kw)
        e._endpointer = Endpointer(EndpointConfig(), vad=None)
        self.addCleanup(e.stop)
        return e

    def test_utterances_come_out_of_the_live_stream(self):
        e = self.engine(_fake_sd(["Mic A"]))
        self.assertTrue(e.start())
        stream = _FakeInputStream.instances[-1]
        stream.feed.extend(frames_of(noise(500) + tone(800) + noise(900)))
        u = e.next_utterance(timeout=3.0)
        self.assertIsNotNone(u)
        self.assertAlmostEqual(u.speech_ms, 800, delta=60)

    def test_levels_are_reported_while_capturing(self):
        e = self.engine(_fake_sd(["Mic A"]))
        levels = []
        e.set_level_callback(levels.append)
        e.start()
        _FakeInputStream.instances[-1].feed.extend(frames_of(tone(600)))
        deadline = time.time() + 2
        while not levels and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(levels and all(0.0 <= x <= 1.0 for x in levels))

    def test_a_dead_stream_is_reopened(self):
        with mock.patch.object(self.cap, "WATCHDOG_S", 0.2):
            e = self.engine(_fake_sd(["Mic A"]))
            e.start()
            first = _FakeInputStream.instances[-1]
            first.alive = False                     # the device stops delivering audio
            deadline = time.time() + 3
            while e.restarts == 0 and time.time() < deadline:
                time.sleep(0.02)
        self.assertGreaterEqual(e.restarts, 1)
        self.assertTrue(first.closed)
        self.assertIsNot(_FakeInputStream.instances[-1], first)

    def test_a_new_default_microphone_is_followed(self):
        sd = _fake_sd(["Laptop Mic", "Headset Mic"])
        with mock.patch.object(self.cap, "DEVICE_POLL_S", 0.1):
            e = self.engine(sd)
            e.start()
            stream = _FakeInputStream.instances[-1]
            stream.feed.extend(frames_of(noise(3000)))
            sd._state["i"] = 1                      # the headset gets plugged in
            deadline = time.time() + 3
            while e.restarts == 0 and time.time() < deadline:
                stream.feed.extend(frames_of(noise(40)))
                time.sleep(0.02)
        self.assertGreaterEqual(e.restarts, 1)
        self.assertEqual(e._device_name, "Headset Mic")

    def test_no_sounddevice_means_not_available(self):
        e = self.cap.CaptureEngine(EndpointConfig(), sd_module=None)
        e._sd = None
        self.assertFalse(e.available)
        self.assertFalse(e.start())

    def test_a_stream_that_cannot_open_reports_why(self):
        sd = _fake_sd(["Mic"])
        sd.InputStream = mock.Mock(side_effect=OSError("device busy"))
        e = self.engine(sd)
        self.assertFalse(e.start())
        self.assertIn("device busy", e.last_error)


if __name__ == "__main__":
    unittest.main()


class HallucinationSetTests(unittest.TestCase):
    """tests/fixtures/whisper_hallucinations.jsonl - Whisper's known failure
    modes on silence, room tone and short noises, plus real commands
    (including a deliberately spoken "Thank you.") that must survive."""

    def test_every_case_in_the_fixture_set(self):
        import json
        import os
        path = os.path.join(os.path.dirname(__file__), "fixtures", "whisper_hallucinations.jsonl")
        wrong = []
        n = 0
        with open(path, encoding="utf-8") as f:
            for line in f:
                c = json.loads(line)
                n += 1
                segs = [tf.SegmentInfo(c["text"], lp, nsp, cr, dur) for lp, nsp, cr, dur in c["segs"]]
                v = tf.judge(c["text"], segs, audio_s=c["audio_s"], snr_db=c["snr_db"])
                if v.text != c["expect"]:
                    wrong.append((c["text"], v.text, v.reason))
        self.assertGreaterEqual(n, 25)
        self.assertEqual(wrong, [])


class _Seg:
    def __init__(self, text, avg_logprob=-0.2, no_speech_prob=0.05, compression_ratio=1.1, start=0.0, end=1.0):
        self.text, self.avg_logprob, self.no_speech_prob = text, avg_logprob, no_speech_prob
        self.compression_ratio, self.start, self.end = compression_ratio, start, end


class StreamListenTests(unittest.TestCase):
    """voice/stt.py's Tier 12 path, with a stand-in capture engine."""

    def setUp(self):
        from test_phase5_stt_aec_wiring import _install_fakes
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod
        self.sttmod = sttmod
        self.r = sttmod.SpeechReceiver()
        self.r.recognizer.recognize_google = mock.Mock(return_value="open chrome")
        # No local Whisper by default (instance attribute; a test can del it)
        self.r._recognize_with_whisper = lambda audio, audio_s=None, snr_db=None: None
        self.sttmod.tts_engine.is_speaking = False
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in (
            "voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language")])

    def utterance(self, **kw):
        from voice.endpoint import Utterance
        base = dict(pcm=tone(800), sample_rate=SR, started_at=10.0, speech_start=10.3, speech_end=11.1,
                    ended_at=11.56, speech_ms=800.0, peak_dbfs=-12.0, mean_speech_dbfs=-15.0, floor_dbfs=-55.0)
        base.update(kw)
        return Utterance(**base)

    def engine(self, utt):
        return types.SimpleNamespace(next_utterance=lambda timeout: utt, running=True)

    def test_a_stream_utterance_carries_its_exact_speech_end(self):
        utt = self.utterance()
        with mock.patch.object(self.r, "_stream_engine", return_value=self.engine(utt)):
            t = self.r.listen()
        self.assertEqual(t, "open chrome")
        self.assertEqual(t.speech_end, 11.1)
        self.assertEqual(t.captured_at, 11.56)
        self.assertAlmostEqual(t.snr_db, 40.0)
        self.assertFalse(t.tts_overlap)

    def test_no_utterance_in_the_window_is_silence(self):
        with mock.patch.object(self.r, "_stream_engine", return_value=self.engine(None)):
            self.assertIsNone(self.r.listen())

    def test_capture_engine_legacy_forces_the_old_path(self):
        with mock.patch.object(self.sttmod.config, "CAPTURE_ENGINE", "legacy", create=True):
            self.assertIsNone(self.r._stream_engine())

    def test_a_stream_that_wont_start_falls_back_and_is_retried_only_later(self):
        fake = mock.Mock()
        fake.running = False
        fake.start.return_value = False
        fake.last_error = "no device"
        with mock.patch.object(self.sttmod, "HAS_CAPTURE_STREAM", True), \
                mock.patch.object(self.sttmod, "CaptureEngine", return_value=fake), \
                mock.patch.object(self.sttmod.config, "CAPTURE_ENGINE", "auto", create=True):
            self.assertIsNone(self.r._stream_engine())
            self.assertIsNone(self.r._stream_engine())
        self.assertEqual(fake.start.call_count, 1, "retried immediately instead of backing off")

    def test_whisper_hallucinations_come_back_as_unclear_with_a_confidence(self):
        del self.r._recognize_with_whisper          # back to the real method
        model = mock.Mock()
        model.transcribe.return_value = ([_Seg(" Thank you.", avg_logprob=-0.6, no_speech_prob=0.5, end=0.6)], None)
        from test_phase5_stt_aec_wiring import _FakeAudioData
        with mock.patch.object(self.r, "_get_whisper_model", return_value=model), \
                mock.patch.object(self.sttmod, "HAS_NUMPY", True), \
                mock.patch.object(self.sttmod, "np", create=True) as fake_np:
            fake_np.frombuffer.return_value = mock.MagicMock()
            text = self.r._recognize_with_whisper(_FakeAudioData(b"\x00\x00" * 9600, SR, 2), audio_s=0.6, snr_db=5)
        self.assertEqual(text, "")
        self.assertIsNotNone(self.r.last_confidence)
        self.assertLess(self.r.last_confidence, 0.5)

    def test_live_captions_report_the_audio_so_far(self):
        seen = []
        self.r.set_partial_callback(seen.append)
        state = {"calls": 0}

        def in_speech():
            state["calls"] += 1
            if state["calls"] > 6:
                self.r._captions_stop.set()
            return True

        self.r._capture = types.SimpleNamespace(running=True, in_speech=in_speech, snapshot=lambda: tone(1500))
        with mock.patch.object(self.r, "captions_available", return_value=True), \
                mock.patch.object(self.r, "_transcribe_partial", return_value="open chr"):
            self.r._caption_loop()
        self.assertIn("open chr", seen)

    def test_no_captions_of_what_is_heard_while_wave_itself_is_talking(self):
        seen = []
        self.r.set_partial_callback(seen.append)
        state = {"calls": 0}

        def in_speech():
            state["calls"] += 1
            if state["calls"] > 6:
                self.r._captions_stop.set()
            return True

        self.r._capture = types.SimpleNamespace(running=True, in_speech=in_speech, snapshot=lambda: tone(1500))
        self.sttmod.tts_engine.is_speaking = True
        transcribe = mock.Mock(return_value="echo of wave")
        with mock.patch.object(self.r, "captions_available", return_value=True), \
                mock.patch.object(self.r, "_transcribe_partial", transcribe):
            self.r._caption_loop()
        self.assertEqual(seen, [])
        transcribe.assert_not_called()


class _FakeOutputStream:
    """Calls the callback every block on its own thread, like PortAudio,
    and records everything it 'played'."""
    instances = []

    def __init__(self, samplerate, channels, dtype, blocksize, callback):
        import numpy as np
        self.np = np
        self.rate, self.blocksize, self.callback = samplerate, blocksize, callback
        self.played = []
        self.running = False
        self.closed = False
        self.frozen = False
        _FakeOutputStream.instances.append(self)

    def start(self):
        self.running = True
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.running:
            if not self.frozen:
                buf = self.np.zeros((self.blocksize, 1), dtype=self.np.int16)
                self.callback(buf, self.blocksize, None, None)
                self.played.append(buf[:, 0].copy())
            time.sleep(0.002)          # faster than real time

    def stop(self):
        self.running = False

    def close(self):
        self.closed = True


@unittest.skipUnless(__import__("importlib").util.find_spec("numpy"), "needs numpy")
class PersistentOutputTests(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        _FakeOutputStream.instances = []
        import voice.audio_out as ao
        self.ao = ao
        self.sd = types.SimpleNamespace(OutputStream=_FakeOutputStream)
        self.out = ao.PersistentOutput(self.sd, samplerate=24000)
        self.addCleanup(self.out.close)

    def pcm(self, n, value):
        return self.np.full(n, value, dtype=self.np.int16)

    def test_utterances_play_back_to_back_through_one_stream(self):
        stop = threading.Event()
        starts = []
        self.assertTrue(self.out.play(self.pcm(2400, 1000), 24000, stop, on_start=lambda: starts.append(1)))
        self.assertTrue(self.out.play(self.pcm(2400, 2000), 24000, stop, on_start=lambda: starts.append(2)))
        self.assertEqual(len(_FakeOutputStream.instances), 1, "a new stream per utterance")
        played = self.np.concatenate(_FakeOutputStream.instances[0].played)
        self.assertEqual(int((played == 1000).sum()), 2400)
        self.assertEqual(int((played == 2000).sum()), 2400)
        self.assertEqual(starts, [1, 2])

    def test_stop_cuts_the_current_utterance_short(self):
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        t0 = time.monotonic()
        self.assertTrue(self.out.play(self.pcm(24000 * 10, 500), 24000, stop))   # 10 s of audio
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_other_rates_are_resampled(self):
        out = self.ao.PersistentOutput(self.sd, samplerate=24000,
                                       resample=lambda pcm, a, b: self.np.repeat(pcm, 2)[: int(len(pcm) * b / a)])
        self.addCleanup(out.close)
        self.assertTrue(out.play(self.pcm(1200, 700), 12000, threading.Event()))
        played = self.np.concatenate(_FakeOutputStream.instances[-1].played)
        self.assertEqual(int((played == 700).sum()), 2400)

    def test_a_stalled_device_is_reported_so_playback_can_fall_back(self):
        with mock.patch.object(self.ao, "_STALL_S", 0.2):
            stop = threading.Event()
            self.assertTrue(self.out.play(self.pcm(240, 1), 24000, stop))
            _FakeOutputStream.instances[-1].frozen = True          # unplugged mid-reply
            self.assertFalse(self.out.play(self.pcm(24000, 1), 24000, stop))
        self.assertFalse(self.out.is_open)

    def test_the_echo_canceller_gets_a_continuous_reference_silence_included(self):
        fed = []
        aec = types.SimpleNamespace(enabled=True, feed_far_end=lambda block, rate: fed.append(len(block)))
        out = self.ao.PersistentOutput(self.sd, samplerate=24000, aec=aec)
        self.addCleanup(out.close)
        out.play(self.pcm(480, 3), 24000, threading.Event())
        deadline = time.time() + 1
        while len(fed) < 5 and time.time() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(len(fed), 5)

    def test_an_unopenable_device_returns_false_and_backs_off(self):
        sd = types.SimpleNamespace(OutputStream=mock.Mock(side_effect=OSError("no device")))
        out = self.ao.PersistentOutput(sd)
        self.assertFalse(out.play(self.pcm(10, 1), 24000, threading.Event()))
        self.assertFalse(out.play(self.pcm(10, 1), 24000, threading.Event()))
        self.assertEqual(sd.OutputStream.call_count, 1)

    def test_it_closes_after_being_idle(self):
        with mock.patch.object(self.ao, "IDLE_CLOSE_S", 0.15):
            self.out.play(self.pcm(240, 1), 24000, threading.Event())
            self.assertTrue(self.out.is_open)
            deadline = time.time() + 2
            while self.out.is_open and time.time() < deadline:
                time.sleep(0.02)
        self.assertFalse(self.out.is_open)


class CaptureRateFallbackTests(unittest.TestCase):
    def test_a_device_that_refuses_16khz_is_opened_at_its_own_rate_and_resampled(self):
        import voice.capture as cap
        opened = []

        class Picky(_FakeInputStream):
            def __init__(self, samplerate, channels, dtype, blocksize, device, callback):
                if samplerate != 48000:
                    raise OSError("Invalid sample rate")
                opened.append(samplerate)
                super().__init__(samplerate, channels, dtype, blocksize, device, callback)

        sd = types.SimpleNamespace(InputStream=Picky,
                                   query_devices=lambda kind=None: {"name": "USB Mic", "default_samplerate": 48000.0})
        e = cap.CaptureEngine(EndpointConfig(), aec=None, sd_module=sd)
        e._endpointer = Endpointer(EndpointConfig(), vad=None)
        self.addCleanup(e.stop)
        self.assertTrue(e.start())
        self.assertEqual(opened, [48000])
        # 48 kHz audio: 3x the samples per frame; utterance timing still right after resampling
        stream = _FakeInputStream.instances[-1]
        loud = tone(800)
        up = b"".join(loud[i:i + 2] * 3 for i in range(0, len(loud), 2))
        quiet = noise(1600)
        quiet_up = b"".join(quiet[i:i + 2] * 3 for i in range(0, len(quiet), 2))
        block = 960 * 2
        for chunk in (quiet_up[:len(quiet_up) // 2], up, quiet_up):
            stream.feed.extend(chunk[i:i + block] for i in range(0, len(chunk) - block + 1, block))
        u = e.next_utterance(timeout=3.0)
        self.assertIsNotNone(u)
        self.assertAlmostEqual(u.speech_ms, 800, delta=80)
