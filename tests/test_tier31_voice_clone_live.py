"""
tests/test_tier31_voice_clone_live.py — Tier 31: the user's cloned voice, for real.

Tier 25 wired XTTS-v2 in, but on the user's Windows PC Wave never once spoke
in their voice:
  - the saved sample path was a Linux build-sandbox path (the project had
    moved), and the web UI never restored the sample at all;
  - torchaudio.load() needs FFmpeg's DLLs since torchaudio 2.9, so the
    model's own loader failed on every sentence;
  - every sentence worked the voice latents out again and waited for its own
    synthesis (~1.8x the sentence's length on the laptop's CPU);
  - Windows' power throttling made that maths ~15x slower a second in;
  - and inside Wave's process, Python's GIL made it 100x slower whenever any
    other thread was busy (and starved Wave's audio threads in turn), so
    XTTS-v2 now runs in a process of its own (voice/clone_worker.py).

    python -m unittest discover -s tests -p "test_tier31_voice_clone_live.py"
"""
import concurrent.futures
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)
import test_tier25_voice_clone as t25  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FindVoiceReferenceTests(unittest.TestCase):
    def setUp(self):
        from avatar import rig  # noqa: PLC0415
        self.rig = rig
        self.tmp = tempfile.mkdtemp(prefix="wave_voice_ref_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rig_dir = os.path.join(self.tmp, "avatar_rig")
        os.makedirs(self.rig_dir)
        self.clip = os.path.join(self.rig_dir, "voice_reference.wav")
        with open(self.clip, "wb") as f:
            f.write(b"RIFF")

    def test_a_path_that_exists_is_kept(self):
        self.assertEqual(self.rig.find_voice_reference(self.clip, os.path.join(self.tmp, "elsewhere")), self.clip)

    def test_a_moved_project_finds_its_clip_in_this_rig_folder(self):
        for saved in ("/home/claude/wave/avatar_rig/voice_reference.wav",
                      r"E:\old\wave\avatar_rig\voice_reference.wav"):
            with self.subTest(saved=saved):
                self.assertEqual(self.rig.find_voice_reference(saved, self.rig_dir), self.clip)

    def test_a_sample_kept_anywhere_else_is_not_guessed_at(self):
        with open(os.path.join(self.rig_dir, "my_voice.wav"), "wb") as f:
            f.write(b"RIFF")
        self.assertEqual(self.rig.find_voice_reference(r"C:\Users\x\Downloads\my_voice.wav", self.rig_dir), "")

    def test_nothing_saved_or_nothing_found_is_empty(self):
        self.assertEqual(self.rig.find_voice_reference("", self.rig_dir), "")
        self.assertEqual(self.rig.find_voice_reference("/home/claude/wave/avatar_rig/gone.wav", self.rig_dir), "")


class WebRestoreTests(unittest.TestCase):
    """WaveWebBridge restores the sample at start-up, like the desktop's
    MainWindow._init_avatar() - the web UI never did."""

    def _start(self, settings):
        import webui.bridge as bridge_module  # noqa: PLC0415
        from database.db_manager import db  # noqa: PLC0415
        tmp = S.make_tmpdir(self)
        path = os.path.join(tmp, "wave_settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        rig = os.path.join(tmp, "avatar_rig")
        os.makedirs(rig)
        clip = os.path.join(rig, "voice_reference.wav")
        with open(clip, "wb") as f:
            f.write(b"RIFF")
        old_db = db.db_path
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_db)
        cfg = bridge_module.config
        self.addCleanup(setattr, cfg, "TTS_CLONE_REFERENCE_PATH", cfg.TTS_CLONE_REFERENCE_PATH)
        cfg.TTS_CLONE_REFERENCE_PATH = ""
        with mock.patch.object(bridge_module, "SETTINGS_FILE", path), \
                mock.patch.object(bridge_module, "AVATAR_RIG_DIR", rig), \
                mock.patch.object(bridge_module.speaker_verifier, "warm_up"), \
                mock.patch.object(bridge_module.tts_engine, "warm_up_clone") as warm:
            bridge = bridge_module.WaveWebBridge(brain=object())
        self.addCleanup(bridge._reminder_worker.stop)
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
        return cfg.TTS_CLONE_REFERENCE_PATH, saved, warm, clip

    def test_a_sample_saved_on_another_machine_is_found_and_the_setting_fixed(self):
        ref, saved, warm, clip = self._start(
            {"avatar_voice_reference_path": "/home/claude/wave/avatar_rig/voice_reference.wav"})
        self.assertEqual(ref, clip)
        self.assertEqual(saved["avatar_voice_reference_path"], clip)
        warm.assert_called_once_with()

    def test_no_saved_sample_leaves_cloning_off(self):
        ref, saved, warm, _ = self._start({"wake_phrases": ["baby"]})
        self.assertEqual(ref, "")
        self.assertNotIn("avatar_voice_reference_path", saved)
        warm.assert_not_called()

    def test_a_sample_that_is_gone_everywhere_leaves_cloning_off(self):
        ref, saved, warm, _ = self._start({"avatar_voice_reference_path": r"C:\Users\x\Downloads\voice.ogg"})
        self.assertEqual(ref, "")
        self.assertEqual(saved["avatar_voice_reference_path"], r"C:\Users\x\Downloads\voice.ogg")
        warm.assert_not_called()


# --------------------------------------------------------------------------
# voice/clone_xtts.py - XTTS-v2 with the user's voice (inside the worker)
# --------------------------------------------------------------------------
class _FakeXtts:
    """Stands in for XTTS-v2's model: counts the latents work and records
    every inference() call."""

    def __init__(self):
        self.config = types.SimpleNamespace(max_ref_len=30, sound_norm_refs=False, gpt_cond_len=30,
                                            gpt_cond_chunk_len=4, temperature=0.75, length_penalty=1.0,
                                            repetition_penalty=5.0, top_k=50, top_p=0.85)
        self.latent_calls = 0
        self.audio_lengths = []
        self.inference_calls = []

    def get_speaker_embedding(self, audio, sample_rate):
        self.latent_calls += 1
        self.audio_lengths.append(audio.shape[1])
        return "speaker"

    def get_gpt_cond_latents(self, audio, sample_rate, length, chunk_length):
        return ("gpt", length, chunk_length)

    def inference(self, text, language, gpt, speaker, **kw):
        self.inference_calls.append((text, language, gpt, speaker, kw))
        return {"wav": np.array([0.0, 0.25, -0.25], dtype=np.float32)}


class XttsVoiceTests(unittest.TestCase):
    def setUp(self):
        from voice import clone_xtts  # noqa: PLC0415
        self.clone_xtts = clone_xtts
        self.tmp = tempfile.mkdtemp(prefix="wave_xtts_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sample = os.path.join(self.tmp, "voice.wav")
        with open(self.sample, "wb") as f:
            f.write(b"RIFF")
        self.loads = []
        self.model = _FakeXtts()
        self.voice = clone_xtts.XttsVoice(self.model, 24000, "cpu", loader=self._loader)

    def _loader(self, path, sample_rate):
        self.loads.append((path, sample_rate))
        return np.zeros((1, 22050 * 40), dtype=np.float32)

    def test_the_voice_latents_are_worked_out_once_per_sample_file(self):
        self.assertTrue(self.voice.set_reference(self.sample))
        self.assertFalse(self.voice.set_reference(self.sample))
        self.voice.synthesize("one", "en")
        self.voice.synthesize("two", "en")
        self.assertEqual(self.model.latent_calls, 1)
        self.assertEqual([c[0] for c in self.model.inference_calls], ["one", "two"])

    def test_a_changed_sample_file_gets_new_latents(self):
        self.voice.set_reference(self.sample)
        stamp = os.path.getmtime(self.sample) + 10
        os.utime(self.sample, (stamp, stamp))
        self.assertTrue(self.voice.set_reference(self.sample))
        self.assertEqual(self.model.latent_calls, 2)

    def test_inference_uses_the_models_own_settings_and_splits_long_text(self):
        self.voice.set_reference(self.sample)
        samples = self.voice.synthesize("Kal milte hain", "en")
        text, lang, gpt, speaker, kw = self.model.inference_calls[0]
        self.assertEqual((text, lang, gpt, speaker), ("Kal milte hain", "en", ("gpt", 30, 4), "speaker"))
        self.assertEqual(kw, {"enable_text_splitting": True, "temperature": 0.75, "length_penalty": 1.0,
                              "repetition_penalty": 5.0, "top_k": 50, "top_p": 0.85})
        self.assertEqual(samples.dtype, np.float32)
        self.assertEqual(len(samples), 3)

    def test_the_sample_is_read_at_the_conditioning_rate_and_cut_to_max_ref_len(self):
        self.voice.set_reference(self.sample)
        self.assertEqual(self.loads, [(self.sample, 22050)])
        self.assertEqual(self.model.audio_lengths, [22050 * 30])

    def test_speaking_before_a_sample_is_set_is_an_error(self):
        with self.assertRaises(RuntimeError):
            self.voice.synthesize("hi", "en")

    def test_anything_but_an_xtts_model_is_refused(self):
        with self.assertRaises(TypeError):
            self.clone_xtts.XttsVoice(object())

    def test_the_device_is_the_gpu_only_when_asked_for_and_available(self):
        resolve = self.clone_xtts.resolve_device
        self.assertEqual(resolve("auto", True), "cuda")
        self.assertEqual(resolve("auto", False), "cpu")
        self.assertEqual(resolve("cpu", True), "cpu")
        self.assertEqual(resolve("cuda", False), "cpu")
        self.assertEqual(resolve("", True), "cuda")

    def test_load_xtts_moves_the_model_to_the_gpu(self):
        moved = []
        model = _FakeXtts()

        class _FakeTTS:
            def __init__(self, model_name):
                self.model_name = model_name
                self.synthesizer = types.SimpleNamespace(tts_model=model, output_sample_rate=24000)

            def to(self, device):
                moved.append(device)
                return self

        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
        fake_api = types.ModuleType("TTS.api")
        fake_api.TTS = _FakeTTS
        with mock.patch.dict(sys.modules, {"torch": fake_torch, "TTS": types.ModuleType("TTS"), "TTS.api": fake_api}):
            voice = self.clone_xtts.load_xtts("tts_models/multilingual/multi-dataset/xtts_v2", "auto")
        self.assertEqual(moved, ["cuda"])
        self.assertEqual((voice.device, voice.sample_rate), ("cuda", 24000))
        self.assertIs(voice.model, model)


# --------------------------------------------------------------------------
# voice/clone_worker.py - the protocol, run in-process with fake streams
# --------------------------------------------------------------------------
class _FakeVoice:
    device = "cpu"
    sample_rate = 24000

    def __init__(self):
        self.references = []
        self.said = []

    def set_reference(self, path):
        self.references.append(path)

    def synthesize(self, text, lang):
        self.said.append((text, lang))
        if text == "boom":
            raise RuntimeError("simulated: inference failed")
        return np.array([0.5, -0.5, 0.25], dtype=np.float32)


def _parse_replies(data):
    stream, replies = io.BytesIO(data), []
    for line in iter(stream.readline, b""):
        header = json.loads(line)
        samples = None
        if header.get("n"):
            samples = np.frombuffer(stream.read(4 * header["n"]), dtype="<f4")
        replies.append((header, samples))
    return replies


class WorkerProtocolTests(unittest.TestCase):
    def _run(self, messages, voice=None, load_error=None):
        from voice import clone_worker  # noqa: PLC0415
        lines = b"".join(m if isinstance(m, bytes) else (json.dumps(m) + "\n").encode("ascii") for m in messages)
        out = io.BytesIO()

        def load(model_name, device):
            if load_error is not None:
                raise load_error
            self.loaded = (model_name, device)
            return voice

        code = clone_worker.main(["--model", "xtts", "--reference", "voice.wav", "--device", "cpu"],
                                 stdin=io.BytesIO(lines), stdout=out, load=load)
        return code, _parse_replies(out.getvalue())

    def test_it_says_ready_then_answers_each_sentence_with_samples(self):
        voice = _FakeVoice()
        code, replies = self._run([{"cmd": "say", "id": 1, "text": "Kal milte hain", "lang": "en"},
                                   {"cmd": "say", "id": 2, "text": "नमस्ते", "lang": "hi"}], voice)
        self.assertEqual(code, 0)
        self.assertEqual(self.loaded, ("xtts", "cpu"))
        self.assertEqual(voice.references, ["voice.wav"])
        ready = replies[0][0]
        self.assertTrue(ready["ready"])
        self.assertEqual((ready["device"], ready["rate"]), ("cpu", 24000))
        self.assertEqual([(h["id"], h["ok"], h["n"]) for h, _ in replies[1:]], [(1, True, 3), (2, True, 3)])
        np.testing.assert_allclose(replies[1][1], [0.5, -0.5, 0.25])
        self.assertEqual(voice.said, [("Kal milte hain", "en"), ("नमस्ते", "hi")])

    def test_a_failed_sentence_is_reported_and_the_next_still_answered(self):
        code, replies = self._run([{"cmd": "say", "id": 1, "text": "boom", "lang": "en"},
                                   {"cmd": "say", "id": 2, "text": "ok", "lang": "en"}], _FakeVoice())
        self.assertEqual((replies[1][0]["id"], replies[1][0]["ok"]), (1, False))
        self.assertIn("simulated: inference failed", replies[1][0]["error"])
        self.assertEqual((replies[2][0]["id"], replies[2][0]["ok"]), (2, True))

    def test_junk_and_unknown_commands_are_ignored(self):
        code, replies = self._run([b"not json\n", {"cmd": "dance", "id": 9},
                                   {"cmd": "say", "id": 3, "text": "hi", "lang": "en"}], _FakeVoice())
        self.assertEqual([h.get("id") for h, _ in replies[1:]], [3])

    def test_a_model_that_wont_load_is_reported_instead_of_ready(self):
        code, replies = self._run([], load_error=RuntimeError("simulated: no model"))
        self.assertEqual(code, 1)
        self.assertFalse(replies[0][0]["ready"])
        self.assertIn("simulated: no model", replies[0][0]["error"])


# --------------------------------------------------------------------------
# voice/clone_client.py - a real child process, speaking the same protocol
# --------------------------------------------------------------------------
_STAND_IN_WORKER = r'''
import json, struct, sys, time
out = sys.stdout.buffer
def send(header, payload=b""):
    out.write((json.dumps(header) + "\n").encode("ascii")); out.write(payload); out.flush()
mode = sys.argv[1]
if mode == "fail-start":
    send({"ready": False, "error": "simulated: no model"}); sys.exit(1)
if mode == "die-at-start":
    sys.exit(3)
sys.stderr.write("INFO stand-in worker up\n"); sys.stderr.flush()
send({"ready": True, "device": "cpu", "rate": 16000, "load_s": 0.1, "latents_s": 0.0})
for raw in sys.stdin.buffer:
    msg = json.loads(raw)
    text = msg["text"]
    if text == "hang":
        time.sleep(60)
        continue
    if text == "die":
        sys.exit(4)
    if text == "fail":
        send({"id": msg["id"], "ok": False, "error": "simulated: synthesis failed"})
        continue
    n = len(text)
    send({"id": msg["id"], "ok": True, "rate": 16000, "n": n}, struct.pack("<%df" % n, *([0.25] * n)))
'''


class ClientProcessTests(unittest.TestCase):
    def setUp(self):
        from voice import clone_client  # noqa: PLC0415
        self.clone_client = clone_client
        self.tmp = tempfile.mkdtemp(prefix="wave_clone_proc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.script = os.path.join(self.tmp, "stand_in_worker.py")
        with open(self.script, "w", encoding="utf-8") as f:
            f.write(_STAND_IN_WORKER)

    def _process(self, mode="ok"):
        proc = self.clone_client.CloneVoiceProcess("xtts", "voice.wav", command=[sys.executable, self.script, mode])
        self.addCleanup(proc.close)
        return proc

    def test_it_starts_then_turns_text_into_samples(self):
        proc = self._process()
        info = proc.start(timeout=30)
        self.assertEqual(info["device"], "cpu")
        samples, rate = proc.synthesize("hello", "en")
        self.assertEqual(rate, 16000)
        np.testing.assert_allclose(samples, [0.25] * 5)
        proc.close()
        self.assertFalse(proc.alive)

    def test_a_failed_sentence_raises_but_the_process_keeps_going(self):
        proc = self._process()
        proc.start(timeout=30)
        with self.assertRaises(self.clone_client.CloneProcessError):
            proc.synthesize("fail", "en")
        self.assertTrue(proc.alive)
        self.assertEqual(len(proc.synthesize("ok", "en")[0]), 2)

    def test_a_process_that_stops_answering_is_stopped(self):
        proc = self._process()
        proc.start(timeout=30)
        started = time.monotonic()
        with self.assertRaises(self.clone_client.CloneProcessError):
            proc.synthesize("hang", "en", timeout=1.0)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(proc.alive)

    def test_a_process_that_exits_mid_sentence_is_reported(self):
        proc = self._process()
        proc.start(timeout=30)
        with self.assertRaises(self.clone_client.CloneProcessError):
            proc.synthesize("die", "en", timeout=20)
        self.assertFalse(proc.alive)

    def test_a_process_that_cannot_load_the_model_fails_to_start(self):
        for mode in ("fail-start", "die-at-start"):
            with self.subTest(mode=mode):
                proc = self._process(mode)
                with self.assertRaises(self.clone_client.CloneProcessError):
                    proc.start(timeout=30)
                self.assertFalse(proc.alive)

    def test_the_real_worker_is_started_as_a_module_from_the_repo(self):
        proc = self.clone_client.CloneVoiceProcess("xtts_v2", r"C:\voice.wav", device="cuda")
        with mock.patch.object(self.clone_client.subprocess, "Popen", side_effect=OSError("simulated")) as popen:
            with self.assertRaises(self.clone_client.CloneProcessError):
                proc.start(timeout=1)
        command = popen.call_args[0][0]
        self.assertEqual(command[1:], ["-m", "voice.clone_worker", "--model", "xtts_v2", "--device", "cuda",
                                       "--reference", r"C:\voice.wav"])
        self.assertEqual(popen.call_args[1]["cwd"], REPO)


# --------------------------------------------------------------------------
# voice/tts.py - the pipeline around the clone process
# --------------------------------------------------------------------------
class PipelineTests(t25._BaseCloneTestCase):
    """Sentences are handed to the clone process from one worker thread,
    ahead of playback."""

    def _up(self, engine=None):
        """The clone process is up for the configured sample."""
        self.engine._clone_engine = engine or types.SimpleNamespace(alive=True, reference_path=self.reference_path)
        return self.engine._clone_engine

    def test_a_prefetched_sentence_is_rendered_once_then_played(self):
        rendered, played = [], []

        def render(text, lang, gen):
            rendered.append(text)
            return np.array([1, 2, 3], dtype=np.int16), 24000

        clean = self.ttsmod.clean_for_speech("Kal milte hain.")
        self._up()
        with self._cfg(), \
                mock.patch.object(self.engine, "_clone_render", side_effect=render), \
                mock.patch.object(self.engine, "_play_pcm", side_effect=lambda pcm, rate: played.append((len(pcm), rate)) or True):
            self.assertTrue(self.engine.prefetch("Kal milte hain."))
            self.assertTrue(self.engine._speak_via_clone(clean, self.ttsmod.language.HINGLISH))
        self.assertEqual(rendered, [clean])
        self.assertEqual(played, [(3, 24000)])
        self.assertEqual(self.engine._clone_futures, {})

    def test_sentences_render_in_order_on_one_worker(self):
        order = []

        def render(text, lang, gen):
            order.append((text, threading.current_thread().name))
            time.sleep(0.02)

        self._up()
        with self._cfg(), mock.patch.object(self.engine, "_clone_render", side_effect=render):
            for text in ("One.", "Two.", "Three."):
                self.assertTrue(self.engine.prefetch(text))
            for future in list(self.engine._clone_futures.values()):
                future.result(timeout=5)
        self.assertEqual([t for t, _ in order], [self.ttsmod.clean_for_speech(t) for t in ("One.", "Two.", "Three.")])
        self.assertEqual(len({name for _, name in order}), 1)
        self.assertTrue(order[0][1].startswith("TTS-Clone"))

    def test_stop_drops_the_renderings_still_queued(self):
        release = threading.Event()
        self._up()
        with self._cfg(), \
                mock.patch.object(self.engine, "_clone_render", side_effect=lambda t, lang, gen: release.wait(5)):
            self.engine.prefetch("One.")
            self.engine.prefetch("Two.")
            queued = self.engine._clone_futures[self.ttsmod.clean_for_speech("Two.")]
            self.engine.stop()
            self.assertEqual(self.engine._clone_futures, {})
            self.assertTrue(queued.cancelled())
            release.set()

    def test_waiting_gives_up_as_soon_as_playback_is_stopped(self):
        self.engine.stop_event.set()
        started = time.monotonic()
        self.assertIsNone(self.engine._clone_wait(concurrent.futures.Future()))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_rendering_that_failed_with_a_timeout_does_not_hang_the_wait(self):
        future = concurrent.futures.Future()
        future.set_exception(TimeoutError("simulated: inside the synthesis"))
        started = time.monotonic()
        self.assertIsNone(self.engine._clone_wait(future))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_rendering_queued_before_a_stop_is_skipped(self):
        gen = self.engine._interrupt_gen
        self.engine.stop()
        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine") as get_engine:
            self.assertIsNone(self.engine._clone_render("hi", self.ttsmod.language.EN, gen))
        get_engine.assert_not_called()

    def test_a_clone_process_that_died_switches_the_session_to_edge_tts(self):
        clone_error = self.ttsmod.CloneProcessError

        class _Dead:
            alive = False

            def synthesize(self, text, lang):
                raise clone_error("the voice-clone process exited")

        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine", return_value=_Dead()):
            self.assertIsNone(self.engine._clone_render("hi", self.ttsmod.language.EN, self.engine._interrupt_gen))
        self.assertTrue(self.engine._clone_load_failed)

    def test_a_single_failed_sentence_does_not(self):
        clone_error = self.ttsmod.CloneProcessError

        class _Alive:
            alive = True

            def synthesize(self, text, lang):
                raise clone_error("synthesis failed")

        with self._cfg(), mock.patch.object(self.engine, "_get_clone_engine", return_value=_Alive()):
            self.assertIsNone(self.engine._clone_render("hi", self.ttsmod.language.EN, self.engine._interrupt_gen))
        self.assertFalse(self.engine._clone_load_failed)


class WatchdogTests(t25._BaseCloneTestCase):
    def test_without_cloning_the_limit_is_unchanged(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""):
            self.assertEqual(self.engine._watchdog_seconds("a b c"), 20.0)
            self.assertAlmostEqual(self.engine._watchdog_seconds(" ".join(["w"] * 50)), 30.0)

    def test_a_cloned_sentence_gets_time_to_be_synthesized(self):
        ten_words = " ".join(["w"] * 10)
        self.engine._clone_engine = types.SimpleNamespace(alive=True, reference_path=self.reference_path)
        with self._cfg():
            self.assertAlmostEqual(self.engine._watchdog_seconds(ten_words), 35.0)

    def test_nothing_waits_for_a_clone_process_that_is_still_starting(self):
        self.engine._clone_engine = None
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True):
            self.assertEqual(self.engine._watchdog_seconds("a b c"), 20.0)


class WarmUpTests(t25._BaseCloneTestCase):
    def test_nothing_to_warm_up_without_a_sample(self):
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""):
            self.assertIsNone(self.engine.warm_up_clone())

    def test_the_clone_process_is_started_before_any_sentence(self):
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
                mock.patch.object(self.engine, "_get_clone_engine", return_value=object()) as get_engine:
            self.assertTrue(self.engine.warm_up_clone().result(timeout=5))
        get_engine.assert_called_once_with()

    def test_a_start_already_under_way_is_not_queued_twice(self):
        release = threading.Event()
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True), \
                mock.patch.object(self.engine, "_get_clone_engine", side_effect=lambda: release.wait(5)) as get_engine:
            first = self.engine.warm_up_clone()
            second = self.engine.warm_up_clone()
            release.set()
            first.result(timeout=5)
        self.assertIs(first, second)
        get_engine.assert_called_once_with()


class WaveProcessTests(unittest.TestCase):
    def test_wave_itself_never_imports_torch_or_coqui(self):
        """XTTS lives in the worker process only - Wave's own process just
        checks that the package is installed."""
        code = ("import sys; import voice.tts; "
                "print(int('torch' in sys.modules), int('TTS' in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "0 0", out.stderr[-2000:])


class PowerTests(unittest.TestCase):
    def test_other_platforms_are_left_alone(self):
        from monitoring import power  # noqa: PLC0415
        with mock.patch.object(power.sys, "platform", "linux"):
            self.assertFalse(power.keep_full_speed())

    @unittest.skipUnless(sys.platform == "win32", "a Windows API")
    def test_windows_accepts_the_opt_out(self):
        from monitoring import power  # noqa: PLC0415
        self.assertTrue(power.keep_full_speed())

    def test_both_launchers_and_the_voice_process_opt_out(self):
        for name in ("wave_start.pyw", "wave_web_start.pyw"):
            with self.subTest(launcher=name), open(os.path.join(REPO, name), encoding="utf-8") as f:
                src = f.read()
                self.assertIn("if config.POWER_THROTTLING_OPT_OUT:", src)
                self.assertIn("keep_full_speed()", src)
        with open(os.path.join(REPO, "voice", "clone_worker.py"), encoding="utf-8") as f:
            self.assertIn("keep_full_speed()", f.read())


if __name__ == "__main__":
    unittest.main()
