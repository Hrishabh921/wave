"""
tests/test_tier32_speaker_id.py — Tier 32: only the owner's voice counts.

In testing, a video's "Say something." was taken for a command and Wave began
talking on its own. voice/speaker_id.py compares every utterance's voice with
a voiceprint built from the owner's voice sample, and voice/stt.py drops other
voices before they're transcribed. These tests use a stand-in embedder (a
voice is a pitch here); one smoke test runs the real model when it's present.

    python -m unittest discover -s tests -p "test_tier32_speaker_id.py"
"""
import hashlib
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import voice.speaker_id as sid  # noqa: E402

RATE = 16000


def tone(freq, seconds, seed=0):
    """A 'voice': a pitch with a little noise."""
    t = np.arange(int(seconds * RATE)) / RATE
    rng = np.random.default_rng(seed)
    return (0.5 * np.sin(2 * np.pi * freq * t) + 0.01 * rng.standard_normal(len(t))).astype(np.float32)


def pcm(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


class PitchEmbedder:
    """Stand-in for the speaker model: one dimension per 100 Hz band of the
    loudest frequency, so the same pitch matches and different ones don't."""

    def __init__(self):
        self.calls = 0

    def __call__(self, samples):
        self.calls += 1
        spectrum = np.abs(np.fft.rfft(samples))
        peak_hz = np.argmax(spectrum) * RATE / len(samples)
        emb = np.full(64, 0.02, dtype=np.float32)
        emb[min(63, int(peak_hz // 100))] = 1.0
        return emb


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave_speaker_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sample = os.path.join(self.tmp, "voice_reference.wav")
        sf.write(self.sample, tone(220, 6.0), RATE)
        self.voiceprint = os.path.join(self.tmp, "voiceprint.npz")
        self.embedder = PitchEmbedder()
        patcher = mock.patch.multiple(sid.config, TTS_CLONE_REFERENCE_PATH=self.sample, SPEAKER_VERIFY="auto",
                                      SPEAKER_VERIFY_THRESHOLD=0.28)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.v = self._verifier()

    def _verifier(self):
        return sid.SpeakerVerifier(model_path=os.path.join(self.tmp, "none.onnx"),
                                   voiceprint_path=self.voiceprint, embedder=self.embedder)


class VoiceprintTests(_Base):
    def test_it_is_built_from_the_sample_in_three_conditions_and_saved(self):
        self.assertTrue(self.v.ready())
        self.assertEqual(self.v._centroids.shape[0], 3)
        self.assertTrue(os.path.isfile(self.voiceprint))

    def test_a_saved_voiceprint_is_reused_without_recomputing(self):
        self.v.ready()
        again = self._verifier()
        before = self.embedder.calls
        self.assertTrue(again.ready())
        self.assertEqual(self.embedder.calls, before)
        np.testing.assert_allclose(again._centroids, self.v._centroids)

    def test_a_changed_voice_sample_gets_a_new_voiceprint(self):
        self.v.ready()
        sf.write(self.sample, tone(880, 6.0), RATE)
        stamp = os.path.getmtime(self.sample) + 10
        os.utime(self.sample, (stamp, stamp))
        self.assertTrue(self.v.ready())
        self.assertTrue(self.v.check(pcm(tone(880, 2.0, seed=3))).is_owner)
        self.assertFalse(self.v.check(pcm(tone(220, 2.0, seed=4))).is_owner)


class CheckTests(_Base):
    def test_the_owners_voice_passes_and_another_voice_does_not(self):
        owner = self.v.check(pcm(tone(220, 2.0, seed=1)))
        other = self.v.check(pcm(tone(650, 2.0, seed=2)))
        self.assertTrue(owner.is_owner)
        self.assertGreater(owner.score, 0.9)
        self.assertFalse(other.is_owner)
        self.assertLess(other.score, 0.28)

    def test_under_a_second_is_not_judged(self):
        self.assertIsNone(self.v.check(pcm(tone(650, 0.8))))

    def test_other_sample_rates_are_resampled(self):
        t = np.arange(int(2.0 * 48000)) / 48000
        x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        self.assertTrue(self.v.check(pcm(x), 48000).is_owner)

    def test_nothing_to_compare_with_means_no_verdict(self):
        with mock.patch.object(sid.config, "TTS_CLONE_REFERENCE_PATH", ""):
            self.assertIsNone(self._verifier().check(pcm(tone(650, 2.0))))
        with mock.patch.object(sid.config, "SPEAKER_VERIFY", "off"):
            self.assertIsNone(self._verifier().check(pcm(tone(650, 2.0))))
        no_model = sid.SpeakerVerifier(model_path=os.path.join(self.tmp, "missing.onnx"),
                                       voiceprint_path=self.voiceprint)
        self.assertIsNone(no_model.check(pcm(tone(650, 2.0))))

    def test_a_model_file_that_isnt_the_pinned_one_is_refused(self):
        fake = os.path.join(self.tmp, "fake.onnx")
        with open(fake, "wb") as f:
            f.write(b"not the model")
        verifier = sid.SpeakerVerifier(model_path=fake, voiceprint_path=self.voiceprint)
        self.assertIsNone(verifier.check(pcm(tone(220, 2.0))))
        self.assertTrue(verifier._load_failed)

    def test_the_threshold_comes_from_the_config(self):
        with mock.patch.object(sid.config, "SPEAKER_VERIFY_THRESHOLD", 0.99):
            self.assertFalse(self.v.check(pcm(tone(330, 2.0))).is_owner)


class LearningTests(_Base):
    def test_a_clearly_owner_utterance_is_learned_and_saved(self):
        check = self.v.check(pcm(tone(220, 2.0, seed=5)))
        self.assertTrue(self.v.learn(check))
        self.assertEqual(len(self.v._learned), 1)
        again = self._verifier()
        again.ready()
        self.assertEqual(len(again._learned), 1)
        self.assertEqual(len(again._targets()), 4)   # three centroids + what was learned

    def test_a_doubtful_one_is_not(self):
        doubtful = sid.SpeakerCheck(sid.LEARN_SCORE - 0.01, True, np.ones(64, np.float32))
        self.v.ready()
        self.assertFalse(self.v.learn(doubtful))
        self.assertFalse(self.v.learn(None))
        self.assertEqual(self.v._learned, [])

    def test_only_the_newest_are_kept(self):
        self.v.ready()
        check = self.v.check(pcm(tone(220, 2.0, seed=6)))
        for _ in range(sid.MAX_LEARNED + 5):
            self.v.learn(check)
        self.assertEqual(len(self.v._learned), sid.MAX_LEARNED)


class TrainingTests(_Base):
    """Tier 32b: the owner's voice through this PC's mic (scripts/train_my_voice.py).
    In the real test their mic voice scored only 0.16-0.19 against the
    phone-recorded sample, and 0.71+ once trained."""

    def test_training_adds_the_mic_voice_and_is_saved(self):
        self.v.ready()
        mic_voice = tone(330, 10.0, seed=11)          # 'the owner, as this mic hears them'
        self.assertFalse(self.v.check(pcm(tone(330, 2.0, seed=12))).is_owner)
        self.v.set_training(self.v.window_embeddings(mic_voice))
        self.assertTrue(self.v.check(pcm(tone(330, 2.0, seed=12))).is_owner)
        again = self._verifier()
        self.assertTrue(again.check(pcm(tone(330, 2.0, seed=13))).is_owner)
        self.assertEqual(len(again._mic), len(self.v.window_embeddings(mic_voice)))

    def test_training_alone_is_enough_without_a_voice_sample(self):
        self.v.set_training(self.v.window_embeddings(tone(330, 10.0)))
        with mock.patch.object(sid.config, "TTS_CLONE_REFERENCE_PATH", ""):
            verifier = self._verifier()
            self.assertTrue(verifier.ready())
            self.assertTrue(verifier.check(pcm(tone(330, 2.0, seed=14))).is_owner)
            self.assertFalse(verifier.check(pcm(tone(220, 2.0, seed=15))).is_owner)

    def test_a_new_voice_sample_keeps_the_training(self):
        self.v.ready()
        self.v.set_training(self.v.window_embeddings(tone(330, 10.0)))
        sf.write(self.sample, tone(880, 6.0), RATE)
        stamp = os.path.getmtime(self.sample) + 10
        os.utime(self.sample, (stamp, stamp))
        self.assertTrue(self.v.ready())
        self.assertTrue(self.v.check(pcm(tone(880, 2.0, seed=16))).is_owner)
        self.assertTrue(self.v.check(pcm(tone(330, 2.0, seed=17))).is_owner)

    def test_sample_scores_compare_against_the_sample_alone(self):
        self.v.set_training(self.v.window_embeddings(tone(330, 10.0)))
        scores = self.v.sample_scores(self.v.window_embeddings(tone(330, 6.0)))
        self.assertTrue(scores and max(scores) < 0.28)
        with mock.patch.object(sid.config, "TTS_CLONE_REFERENCE_PATH", ""):
            self.assertIsNone(self._verifier().sample_scores([np.ones(64, np.float32)]))

    def test_the_training_script_finds_speech_and_can_forget_everything(self):
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("train_my_voice",
                                                      os.path.join(S.ROOT, "scripts", "train_my_voice.py"))
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        quiet = np.zeros(RATE, np.float32)
        x = np.concatenate([quiet, tone(330, 3.0), quiet, tone(330, 2.0), quiet])
        voiced, peak = script.speech_only(x)
        # the 5 s of speech plus ~150 ms kept either side of each stretch
        self.assertGreaterEqual(len(voiced) / RATE, 5.0)
        self.assertLessEqual(len(voiced) / RATE, 6.0)
        self.assertGreater(peak, 0.1)
        self.v.ready()
        self.assertTrue(os.path.isfile(self.voiceprint))
        with mock.patch.object(script.speaker_id, "speaker_verifier", self.v):
            self.assertEqual(script.main(["--reset"]), 0)
        self.assertFalse(os.path.exists(self.voiceprint))

    def test_video_only_measures_other_voices_without_training(self):
        """How the owner's laptop was tuned: a video's voices scored at most
        0.21 against the trained voiceprint (the owner 0.71+) - 0.35 in .env."""
        import contextlib  # noqa: PLC0415
        import importlib.util  # noqa: PLC0415
        import io  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location("train_my_voice",
                                                      os.path.join(S.ROOT, "scripts", "train_my_voice.py"))
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        self.v.set_training(self.v.window_embeddings(tone(330, 10.0)))
        trained = len(self.v._mic)
        quiet = np.zeros(RATE, np.float32)
        video = np.concatenate([quiet, tone(650, 3.0), quiet, tone(650, 3.0), quiet])   # someone else talking
        out = io.StringIO()
        with mock.patch.object(script.speaker_id, "speaker_verifier", self.v), \
                mock.patch.object(script, "record", return_value=video), \
                mock.patch.object(script, "beep"), mock.patch.object(script, "keep_full_speed"), \
                mock.patch.object(self.v, "_ready_model", return_value=True), contextlib.redirect_stdout(out):
            self.assertEqual(script.main(["--video-only", "--video-seconds", "6"]), 0)
        self.assertIn("other voices score: max", out.getvalue())
        self.assertEqual(len(self.v._mic), trained)


class _Audio:
    def __init__(self, x):
        self.x = x

    def get_raw_data(self, convert_rate=None, convert_width=None):
        return pcm(self.x)


class SttTests(unittest.TestCase):
    """voice/stt.py drops other voices before they're transcribed."""

    def setUp(self):
        from voice.stt import Transcript, stt_engine  # noqa: PLC0415
        self.Transcript, self.stt = Transcript, stt_engine

    def _run(self, verdict, tts_overlap=False, text="time kya hua hai"):
        recognized = self.Transcript(text, engine="whisper")
        with mock.patch.object(sid.speaker_verifier, "check", return_value=verdict), \
                mock.patch.object(sid.speaker_verifier, "learn") as learn, \
                mock.patch.object(self.stt, "_recognize", return_value=recognized) as recognize:
            out = self.stt._gate_and_recognize(_Audio(tone(220, 2.0)), None, tts_overlap, {})
        return out, recognize, learn

    def test_another_voice_is_dropped_before_transcription(self):
        out, recognize, learn = self._run(sid.SpeakerCheck(0.1, False))
        self.assertIsNone(out)
        recognize.assert_not_called()
        learn.assert_not_called()

    def test_the_owners_voice_is_transcribed_scored_and_learned_from(self):
        verdict = sid.SpeakerCheck(0.7, True, np.ones(4, np.float32))
        out, recognize, learn = self._run(verdict)
        self.assertEqual(str(out), "time kya hua hai")
        self.assertEqual(out.speaker_score, 0.7)
        learn.assert_called_once_with(verdict)

    def test_while_wave_talks_nothing_is_dropped_or_learned(self):
        out, recognize, learn = self._run(sid.SpeakerCheck(0.1, False), tts_overlap=True, text="ruk jao")
        self.assertEqual(str(out), "ruk jao")
        learn.assert_not_called()
        out, _, learn = self._run(sid.SpeakerCheck(0.7, True, np.ones(4, np.float32)), tts_overlap=True)
        learn.assert_not_called()

    def test_without_a_verdict_everything_works_as_before(self):
        out, recognize, learn = self._run(None)
        self.assertEqual(str(out), "time kya hua hai")
        self.assertIsNone(out.speaker_score)
        learn.assert_not_called()


class DownloadScriptTests(unittest.TestCase):
    def test_it_installs_only_a_file_with_the_pinned_checksum(self):
        import importlib.util  # noqa: PLC0415
        spec = importlib.util.spec_from_file_location(
            "download_speaker_model", os.path.join(S.ROOT, "scripts", "download_speaker_model.py"))
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        tmp = tempfile.mkdtemp(prefix="wave_speaker_dl_")
        self.addCleanup(shutil.rmtree, tmp, True)
        target = os.path.join(tmp, "model.onnx")
        good = b"the model bytes"

        def fake_download(url, path, payload=good):
            with open(path, "wb") as f:
                f.write(payload)

        with mock.patch.object(script.speaker_id, "default_model_path", return_value=target), \
                mock.patch.object(script.speaker_id, "MODEL_SHA256", hashlib.sha256(good).hexdigest()), \
                mock.patch.object(script.urllib.request, "urlretrieve", side_effect=fake_download):
            self.assertEqual(script.main(), 0)
            self.assertTrue(os.path.isfile(target))
        os.remove(target)
        with mock.patch.object(script.speaker_id, "default_model_path", return_value=target), \
                mock.patch.object(script.speaker_id, "MODEL_SHA256", "0" * 64), \
                mock.patch.object(script.urllib.request, "urlretrieve", side_effect=fake_download):
            self.assertEqual(script.main(), 1)
        self.assertFalse(os.path.exists(target))
        self.assertEqual([f for f in os.listdir(tmp) if f.endswith(".part")], [])


@unittest.skipUnless(os.path.isfile(sid.default_model_path()), "the speaker model isn't downloaded here")
class RealModelTests(unittest.TestCase):
    def test_the_real_model_gives_a_stable_192_d_voice_embedding_quickly(self):
        tmp = tempfile.mkdtemp(prefix="wave_speaker_real_")
        self.addCleanup(shutil.rmtree, tmp, True)
        verifier = sid.SpeakerVerifier(voiceprint_path=os.path.join(tmp, "vp.npz"))
        self.assertTrue(verifier._ready_model())
        x = tone(180, 2.0, seed=9)
        started = time.monotonic()
        a = verifier._embed(x)
        b = verifier._embed(x)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(a.shape, (192,))
        self.assertAlmostEqual(float(np.linalg.norm(a)), 1.0, places=4)
        np.testing.assert_allclose(a, b, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
