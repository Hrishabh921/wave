"""Tier 11 - the acoustic wake word (voice/wakeword.py) and its two-stage
check, with a fake openwakeword.Model - plus, when the real pieces are
installed (openwakeword + onnxruntime + espeak-ng) and the bundled model is
present, a real end-to-end check of Wave's own "Hey Wave" model.

    python -m unittest discover -s tests -p "test_tier11_wakeword.py"
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S

import numpy as np

from voice import commands as vc
import voice.wakeword as ww

BUNDLED_MODEL = os.path.join(S.ROOT, "voice_models", "wakewords", "hey_wave.onnx")
BUNDLED_CARD = os.path.join(S.ROOT, "voice_models", "wakewords", "hey_wave.json")

# Checked once, here, before any test runs: install_stub() (this file and
# several others) leaves a fake "PyQt6" sitting in sys.modules for the rest
# of the process once anything has called it, which would make a *later*
# "try: import PyQt6" look like the real thing. Module import happens
# before any test executes, so this is the one point where the check is
# trustworthy.
try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False


class _FakeModel:
    """Stands in for openwakeword.Model: returns scripted scores, one per
    80 ms frame, and records how it was built."""
    built = []
    script = []

    def __init__(self, wakeword_models=None, inference_framework=None, **kwargs):
        _FakeModel.built.append({"models": list(wakeword_models or []), "framework": inference_framework, **kwargs})
        self.i = 0
        self.resets = 0

    def reset(self):
        self.resets += 1
        self.i = 0

    def predict(self, frame):
        assert len(frame) == ww.FRAME_SAMPLES
        score = _FakeModel.script[self.i] if self.i < len(_FakeModel.script) else 0.0
        self.i += 1
        return {"hey_wave": score}


class _Audio:
    def __init__(self, seconds=1.0):
        self.pcm = np.zeros(int(16000 * seconds), dtype=np.int16).tobytes()

    def get_raw_data(self, convert_rate=None, convert_width=None):
        return self.pcm


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        _FakeModel.built, _FakeModel.script = [], []
        for name, value in (("_OWWModel", _FakeModel), ("HAS_OPENWAKEWORD", True)):
            p = mock.patch.object(ww, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("feature_models_dir", lambda: os.path.join(self.tmp, "oww")),
                            ("_package_models_dir", lambda: None)):
            p = mock.patch.object(ww, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.model_path = os.path.join(self.tmp, "hey_wave.onnx")
        open(self.model_path, "wb").close()
        os.makedirs(os.path.join(self.tmp, "oww"))
        for f in ("melspectrogram.onnx", "embedding_model.onnx"):
            open(os.path.join(self.tmp, "oww", f), "wb").close()

    def gate(self, **kw):
        kw.setdefault("engine", "auto")
        kw.setdefault("model_path", self.model_path)
        kw.setdefault("auto_download", False)
        return ww.WakeWordGate(**kw)

    def test_score_is_the_max_over_frames_and_detect_uses_the_threshold(self):
        _FakeModel.script = [0.0] * 14 + [0.2, 0.71, 0.4]
        g = self.gate(threshold=0.6)
        self.assertAlmostEqual(g.score_pcm16(_Audio().pcm), 0.71, places=5)
        self.assertTrue(g.detect(_Audio()))
        g.threshold = 0.8
        self.assertFalse(g.detect(_Audio()))

    def test_the_feature_models_come_from_the_writable_data_folder(self):
        self.gate().available
        built = _FakeModel.built[-1]
        self.assertEqual(built["framework"], "onnx")
        self.assertEqual(built["melspec_model_path"], os.path.join(self.tmp, "oww", "melspectrogram.onnx"))
        self.assertEqual(built["embedding_model_path"], os.path.join(self.tmp, "oww", "embedding_model.onnx"))

    def test_missing_feature_models_are_downloaded_there_once(self):
        shutil.rmtree(os.path.join(self.tmp, "oww"))
        calls = []

        def fake_download(model_names, target_directory):
            calls.append((model_names, target_directory))
            os.makedirs(target_directory, exist_ok=True)
            for f in ("melspectrogram.onnx", "embedding_model.onnx"):
                open(os.path.join(target_directory, f), "wb").close()

        utils = types.ModuleType("openwakeword.utils")
        utils.download_models = fake_download
        with mock.patch.dict(sys.modules, {"openwakeword.utils": utils}):
            g = self.gate(auto_download=True)
            self.assertTrue(g.available)
            self.assertTrue(g.available)
        self.assertEqual(calls, [(["__feature_models_only__"], os.path.join(self.tmp, "oww"))])

    def test_feature_models_already_in_openwakewords_own_folder_are_reused(self):
        pkg = os.path.join(self.tmp, "pkg_models")
        os.makedirs(pkg)
        for f in ("melspectrogram.onnx", "embedding_model.onnx"):
            open(os.path.join(pkg, f), "wb").close()
        shutil.rmtree(os.path.join(self.tmp, "oww"))
        utils = types.ModuleType("openwakeword.utils")
        utils.download_models = mock.Mock()
        with mock.patch.object(ww, "_package_models_dir", lambda: pkg), \
                mock.patch.dict(sys.modules, {"openwakeword.utils": utils}):
            self.assertTrue(self.gate(auto_download=True).available)
        utils.download_models.assert_not_called()
        self.assertEqual(_FakeModel.built[-1]["melspec_model_path"], os.path.join(pkg, "melspectrogram.onnx"))

    def test_a_pretrained_name_is_resolved_to_the_downloaded_file(self):
        open(os.path.join(self.tmp, "oww", "hey_jarvis_v0.1.onnx"), "wb").close()
        with mock.patch.object(ww, "default_custom_model_path", lambda: os.path.join(self.tmp, "none.onnx")):
            g = ww.WakeWordGate(engine="auto", model_path="", model_name="hey_jarvis", auto_download=False)
            self.assertTrue(g.available)
            self.assertEqual(g.phrase, "hey jarvis")
        self.assertEqual(_FakeModel.built[-1]["models"], [os.path.join(self.tmp, "oww", "hey_jarvis_v0.1.onnx")])

    def test_a_load_failure_is_remembered(self):
        with mock.patch.object(ww, "_OWWModel", side_effect=RuntimeError("bad file")) as m:
            g = self.gate()
            self.assertFalse(g.available)
            self.assertFalse(g.available)
        self.assertEqual(m.call_count, 1)
        self.assertEqual(g.score_pcm16(_Audio().pcm), 0.0)

    def test_engine_transcript_never_wants_the_gate_and_auto_needs_a_model(self):
        self.assertFalse(self.gate(engine="transcript").wanted())
        self.assertTrue(self.gate(engine="auto").wanted())
        with mock.patch.object(ww, "default_custom_model_path", lambda: os.path.join(self.tmp, "none.onnx")):
            self.assertFalse(ww.WakeWordGate(engine="auto", model_path="", model_name="").wanted())

    def test_threshold_and_phrase_come_from_the_model_card_unless_set(self):
        with open(os.path.join(self.tmp, "hey_wave.json"), "w", encoding="utf-8") as f:
            json.dump({"recommended_threshold": 0.62, "phrase": "hey wave"}, f)
        with mock.patch.object(ww.config, "WAKE_WORD_THRESHOLD_SET", False):
            self.assertAlmostEqual(self.gate().threshold, 0.62)
            self.assertAlmostEqual(self.gate(threshold=0.4).threshold, 0.4)
        with mock.patch.object(ww.config, "WAKE_WORD_THRESHOLD_SET", True), \
                mock.patch.object(ww.config, "WAKE_WORD_THRESHOLD", 0.77):
            self.assertAlmostEqual(self.gate().threshold, 0.77)
        self.assertEqual(self.gate().phrase, "hey wave")

    def test_smoothing_needs_the_score_held_for_k_frames(self):
        self.assertAlmostEqual(ww.smoothed_peak([0.1, 0.9, 0.2, 0.6, 0.7, 0.3], 1), 0.9)
        self.assertAlmostEqual(ww.smoothed_peak([0.1, 0.9, 0.2, 0.6, 0.7, 0.3], 2), 0.6)
        self.assertEqual(ww.smoothed_peak([0.9], 2), 0.0)
        self.assertEqual(ww.smoothed_peak([], 1), 0.0)

    def test_the_card_sets_smoothing_and_a_prefilter_model_confirms_every_detection(self):
        with open(os.path.join(self.tmp, "hey_wave.json"), "w", encoding="utf-8") as f:
            json.dump({"recommended_threshold": 0.5, "smoothing_frames": 2, "mode": "prefilter",
                       "confirm_below": 1.0}, f)
        _FakeModel.script = [0.0] * 14 + [0.95, 0.2, 0.0, 0.62, 0.66, 0.1]
        with mock.patch.object(ww.config, "WAKE_WORD_CONFIRM_BELOW_SET", False):
            g = self.gate()
            self.assertEqual(g.smoothing_frames, 2)
            self.assertAlmostEqual(g.score_pcm16(_Audio(2.0).pcm), 0.62, places=5,
                                   msg="the one-frame 0.95 spike must not count")
            self.assertEqual(g.confirm_below, 1.0)
            self.assertFalse(g.confirm("hey dave", 0.97))
            self.assertTrue(g.confirm("hey wave, open chrome", 0.97))
        with mock.patch.object(ww.config, "WAKE_WORD_CONFIRM_BELOW_SET", True), \
                mock.patch.object(ww.config, "WAKE_WORD_CONFIRM_BELOW", 0.9):
            self.assertEqual(self.gate().confirm_below, 0.9, "an explicit .env value wins over the card")

    def test_without_a_card_nothing_changes(self):
        _FakeModel.script = [0.0] * 14 + [0.95, 0.2]
        with mock.patch.object(ww.config, "WAKE_WORD_CONFIRM_BELOW_SET", False), \
                mock.patch.object(ww.config, "WAKE_WORD_CONFIRM_BELOW", 0.85):
            g = self.gate()
            self.assertEqual((g.smoothing_frames, g.confirm_below), (1, 0.85))
            self.assertAlmostEqual(g.score_pcm16(_Audio().pcm), 0.95, places=5)

    def test_a_strong_detection_stands_alone_and_a_weak_one_needs_the_transcript(self):
        g = self.gate()
        g.confirm_below = 0.85
        self.assertTrue(g.confirm("", 0.93))
        self.assertTrue(g.confirm("hey way, open chrome", 0.55))
        self.assertFalse(g.confirm("hey dave wait for me", 0.55))
        self.assertFalse(g.confirm("", 0.55))
        g.confirm_below = 0.0
        self.assertTrue(g.confirm("anything at all", 0.01))


class HeardWakeTests(unittest.TestCase):
    def test_what_recognizers_write_for_hey_wave(self):
        for t in ("Hey Wave", "hey way, open chrome", "Hey wait, what time is it", "a wave", "hey vave",
                  "Okay wave.", "हे वेव", "हे वेव, क्रोम खोलो"):
            self.assertTrue(vc.looks_like_wake(t), t)
        for t in ("hey dave", "microwave the soup", "wavelength", "", "the weather today", "hey jarvis"):
            self.assertFalse(vc.looks_like_wake(t), t)

    def test_the_heard_wake_is_stripped_from_the_command(self):
        self.assertEqual(vc.strip_heard_wake("Hey way, open Chrome"), "open Chrome")
        self.assertEqual(vc.strip_heard_wake("हे वेव क्रोम खोलो"), "क्रोम खोलो")
        self.assertEqual(vc.strip_heard_wake("open chrome"), "open chrome")

    def test_decide_after_an_acoustic_wake(self):
        d = vc.decide("Hey way, open Chrome", "SLEEPING", ["hey wave"], False, wake_detected=True)
        self.assertEqual((d.action, d.text), (vc.WAKE_AND_COMMAND, "open Chrome"))
        d = vc.decide("a wave", "SLEEPING", ["hey wave"], False, wake_detected=True)
        self.assertEqual(d.action, vc.WAKE)
        d = vc.decide("", "SLEEPING", ["hey wave"], False, wake_detected=True)
        self.assertEqual(d.action, vc.WAKE)


class ListenWithGateTests(unittest.TestCase):
    """voice/stt.py's listen(wake_gate=...) - detection, then the
    transcript check."""

    def setUp(self):
        from test_phase5_stt_aec_wiring import _install_fakes, _FakeAudioData
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod
        self.sttmod = sttmod
        self.r = sttmod.SpeechReceiver()
        self.r.recognizer.listen = lambda source, timeout=1, phrase_time_limit=8: _FakeAudioData(b"x", 16000, 2)
        self.r.recognizer.recognize_google = mock.Mock(return_value="hey way open chrome")
        for name, value in (("_apply_aec", lambda a: a), ("_passes_vad", lambda a: True),
                            ("_recognize_with_whisper", lambda a, **kw: None), ("is_headphone_connected", lambda: False)):
            p = mock.patch.object(self.r, name, side_effect=value)
            p.start()
            self.addCleanup(p.stop)
        self.sttmod.tts_engine.is_speaking = False
        self.addCleanup(lambda: [sys.modules.pop(m, None) for m in (
            "voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language")])

    def gate(self, detected, score):
        g = types.SimpleNamespace(available=True, last_score=score)
        g.detect = lambda audio: detected
        g.confirm = lambda text, s: s >= 0.85 or vc.looks_like_wake(text)
        return g

    def test_no_wake_word_means_nothing_is_transcribed(self):
        self.assertIsNone(self.r.listen(wake_gate=self.gate(False, 0.1)))
        self.r.recognizer.recognize_google.assert_not_called()

    def test_a_confirmed_detection_returns_the_transcript_marked_as_a_wake(self):
        t = self.r.listen(wake_gate=self.gate(True, 0.6))
        self.assertEqual(t, "hey way open chrome")
        self.assertTrue(t.wake_detected)
        self.assertAlmostEqual(t.wake_score, 0.6)

    def test_a_weak_detection_the_transcript_doesnt_back_up_is_dropped(self):
        self.r.recognizer.recognize_google.return_value = "hey dave did you see the game"
        self.assertIsNone(self.r.listen(wake_gate=self.gate(True, 0.6)))

    def test_audio_captured_while_wave_itself_was_talking_is_never_wake_scored(self):
        g = self.gate(True, 0.99)
        g.detect = mock.Mock(return_value=True)
        self.sttmod.tts_engine.is_speaking = True
        self.addCleanup(setattr, self.sttmod.tts_engine, "is_speaking", False)
        self.assertIsNone(self.r.listen(wake_gate=g))
        g.detect.assert_not_called()
        self.r.recognizer.recognize_google.assert_not_called()

    def test_a_strong_detection_needs_no_transcript_support(self):
        self.r.recognizer.recognize_google.return_value = "hey dave did you see the game"
        self.assertEqual(self.r.listen(wake_gate=self.gate(True, 0.95)), "hey dave did you see the game")


class MicGateCouplingTests(unittest.TestCase):
    """ContinuousMicWorker._gate_in_use: a model for one phrase must never
    stand between the user and a different wake phrase they picked."""

    @classmethod
    def setUpClass(cls):
        if REAL_QT:
            raise unittest.SkipTest("real PyQt6 present - stub-based test")
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        P4.install_stub()
        cls.workers = T5.import_with_fakes("ui.workers")

    def worker(self, phrases, engine="auto", wanted=True, phrase="hey wave"):
        w = self.workers.ContinuousMicWorker(wake_phrases=phrases)
        w.wake_gate = types.SimpleNamespace(wanted=lambda: wanted, engine=engine, phrase=phrase)
        return w

    def test_used_when_the_picked_phrase_is_the_models(self):
        w = self.worker(["hey wave", "wake up"])
        self.assertIs(w._gate_in_use(), w.wake_gate)
        self.assertIn("hey wave", w._all_wake_phrases())

    def test_not_used_for_a_different_phrase_in_auto_mode(self):
        self.assertIsNone(self.worker(["okay wave", "ok wave", "wake up"])._gate_in_use())

    def test_forced_with_engine_openwakeword(self):
        w = self.worker(["okay wave"], engine="openwakeword")
        self.assertIs(w._gate_in_use(), w.wake_gate)

    def test_not_used_when_not_wanted(self):
        self.assertIsNone(self.worker(["hey wave"], wanted=False)._gate_in_use())


# ---------------------------------------------------------------------------
# The real thing - only where openwakeword, onnxruntime and espeak-ng exist.
# ---------------------------------------------------------------------------
def _real_stack_available():
    if not (ww.HAS_OPENWAKEWORD and ww.HAS_NUMPY and os.path.isfile(BUNDLED_MODEL)):
        return False
    return shutil.which("espeak-ng") is not None


@unittest.skipUnless(os.path.isfile(BUNDLED_MODEL), "bundled hey_wave.onnx not present")
class BundledModelCardTests(unittest.TestCase):
    def test_card_matches_the_file_and_is_honest_about_its_numbers(self):
        with open(BUNDLED_CARD, encoding="utf-8") as f:
            card = json.load(f)
        with open(BUNDLED_MODEL, "rb") as f:
            self.assertEqual(card["sha256"], hashlib.sha256(f.read()).hexdigest())
        self.assertEqual(card["phrase"], "hey wave")
        self.assertTrue(0.0 < card["recommended_threshold"] < 1.0)
        self.assertIn("synthetic", card["caveat"].lower())
        self.assertIn("MIT", card["license"])
        self.assertIn(card["mode"], ("standalone", "prefilter"))
        self.assertIn(card["smoothing_frames"], (1, 2))
        if card["mode"] == "prefilter":
            self.assertEqual(card["confirm_below"], 1.0, "a prefilter model must have every detection confirmed")
        rows = card["metrics_synthetic"]["by_smoothing"][str(card["smoothing_frames"])]
        point = [r for r in rows if abs(r["threshold"] - card["recommended_threshold"]) < 1e-6]
        self.assertEqual(len(point), 1, "the recommended threshold must be one that was measured")
        self.assertEqual(point[0], card["operating_point"])

    def test_the_model_has_the_shape_openwakeword_expects(self):
        try:
            import onnxruntime as ort
        except ImportError:
            self.skipTest("onnxruntime not installed")
        sess = ort.InferenceSession(BUNDLED_MODEL, providers=["CPUExecutionProvider"])
        self.assertEqual(sess.get_inputs()[0].shape[1:], [16, 96])
        self.assertEqual(sess.get_outputs()[0].shape[1], 1)
        out = sess.run(None, {sess.get_inputs()[0].name: np.zeros((1, 16, 96), np.float32)})[0]
        self.assertTrue(0.0 <= float(out[0][0]) <= 1.0)


def _espeak(text, voice="en-us", speed=160):
    from scipy.signal import resample_poly
    import io
    import wave
    out = subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "--stdout", text], capture_output=True).stdout
    with wave.open(io.BytesIO(out)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        sr = w.getframerate()
    return np.clip(resample_poly(pcm.astype(np.float32), 160, sr // 100), -32768, 32767).astype(np.int16)


@unittest.skipUnless(_real_stack_available(), "needs openwakeword + onnxruntime + espeak-ng + the bundled model")
class RealHeyWaveModelTests(unittest.TestCase):
    """End to end with Wave's shipped model. espeak-ng voices are robotic
    and partly held out of training, so this is a smoke test of the real
    pipeline, not a quality benchmark - docs/WAKE_WORD.md has those."""

    @classmethod
    def setUpClass(cls):
        cls.gate = ww.WakeWordGate(engine="auto", model_path=BUNDLED_MODEL, model_name="", auto_download=True)
        if not cls.gate.available:
            raise unittest.SkipTest("model could not load (feature models not downloadable here?)")

    def accepted(self, pcm, transcript):
        """Both stages, as voice/stt.py runs them: the acoustic detection,
        then confirm() against the (here: perfect) transcript."""
        return self.gate.detect_pcm16(pcm) and self.gate.confirm(transcript, self.gate.last_score)

    def test_hey_wave_is_detected(self):
        hits = 0
        cases = [("hey wave", "en-us", 160), ("hey wave", "en-gb", 150), ("hey, wave.", "en-us", 190),
                 ("hey wave", "en-us+f3", 170), ("हे वेव", "hi", 150)]
        for text, voice, speed in cases:
            if self.accepted(_espeak(text, voice, speed).tobytes(), text):
                hits += 1
        self.assertGreaterEqual(hits, 4, f"only {hits}/{len(cases)} detected")

    def test_other_speech_is_not(self):
        for text in ("what is the weather like today", "hey jarvis", "turn the volume down please",
                     "the quick brown fox jumps over the lazy dog"):
            self.assertFalse(self.accepted(_espeak(text).tobytes(), text), text)

    def test_silence_and_noise_are_not(self):
        rng = np.random.default_rng(0)
        self.assertFalse(self.gate.detect_pcm16(np.zeros(32000, np.int16).tobytes()))
        noise = (rng.standard_normal(48000) * 2000).astype(np.int16)
        self.assertFalse(self.gate.detect_pcm16(noise.tobytes()))


if __name__ == "__main__":
    unittest.main()
