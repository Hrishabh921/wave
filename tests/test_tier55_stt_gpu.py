"""tests/test_tier55_stt_gpu.py - Tier 55: Whisper "medium" on the GPU, and
spoken commands that keep Whisper's punctuation.

Measured 2026-11-27 (320 spoken commands, see CHANGELOG): medium on the GPU
halves the word errors of base on the CPU at the same speed, and Whisper's
sentence-final "." / "।" kept 9-17% of commands away from the fast path.
Nothing here loads a real model, touches the GPU or loads a real DLL.
"""
import os
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

from brain import intent_router  # noqa: E402
from settings.config import config  # noqa: E402
from voice import whisper_device as wd  # noqa: E402


def _route(text):
    r = intent_router.route(text)
    return (r.skill, r.args) if r else None


class PunctuationTests(unittest.TestCase):
    def test_a_number_command_with_whispers_full_stop(self):
        self.assertEqual(_route("Set the volume to 5."), ("set_volume", {"percent": 5}))
        self.assertEqual(_route("Brightness 75%."), ("set_brightness", {"percent": 75}))
        self.assertEqual(_route("Set the volume to 5, please."), ("set_volume", {"percent": 5}))
        self.assertEqual(_route("Volume 50% kar do."), ("set_volume", {"percent": 50}))

    def test_a_devanagari_command_with_the_danda(self):
        self.assertEqual(_route("पेंट खोलो।"), ("open_app", {"app_name": "पेंट"}))
        self.assertEqual(_route("यह पूर्ववत करो।")[0], "undo_last_action")

    def test_names_lose_the_full_stop(self):
        self.assertEqual(_route("Open Chrome."), ("open_app", {"app_name": "chrome"}))     # was "chrome."
        self.assertEqual(_route("Close Spotify.")[1], {"app_name": "spotify"})
        self.assertEqual(_route("Take a note buy milk."), ("take_note", {"content": "buy milk"}))

    def test_a_question_mark_still_makes_a_question(self):
        self.assertIsNone(_route("dark mode on?"))
        self.assertEqual(_route("Dark mode on."), ("switch_to_dark_mode", {}))

    def test_the_older_guards_still_hold(self):
        self.assertIsNone(_route("Volume 2 of the book."))            # Tier 50
        self.assertIsNone(_route("..."))


class _FakeModel:
    def __init__(self, name, device, compute_type, decode_error=None):
        self.name, self.device, self.compute_type = name, device, compute_type
        self.decode_error, self.decodes = decode_error, 0

    def transcribe(self, audio, **kw):
        self.decodes += 1
        if self.decode_error:
            raise self.decode_error
        return iter([]), None


class _FakeWhisper:
    """Stands in for faster_whisper.WhisperModel and records what got built."""

    def __init__(self, gpu_build_error=None, gpu_decode_error=None, cpu_build_error=None):
        self.built, self.errors = [], {"cuda": gpu_build_error, "cpu": cpu_build_error}
        self.gpu_decode_error = gpu_decode_error

    def __call__(self, name, device, compute_type):
        self.built.append((name, device, compute_type))
        if self.errors[device]:
            raise self.errors[device]
        return _FakeModel(name, device, compute_type, self.gpu_decode_error if device == "cuda" else None)


class _np:
    float32 = "float32"

    @staticmethod
    def zeros(n, dtype=None):
        return [0.0] * n


class LoadModelTests(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(config, "FASTER_WHISPER_MODEL", "base"),
                   mock.patch.object(config, "FASTER_WHISPER_COMPUTE_TYPE", "int8"),
                   mock.patch.object(config, "FASTER_WHISPER_GPU_MODEL", "medium"),
                   mock.patch.object(config, "FASTER_WHISPER_GPU_COMPUTE_TYPE", "int8_float16"),
                   mock.patch.object(config, "WHISPER_MIN_CONFIDENCE", 0.35),
                   mock.patch.object(config, "WHISPER_GPU_MIN_CONFIDENCE", 0.25),
                   mock.patch.object(wd, "preload_cuda_dlls", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _load(self, device, **errors):
        fake = _FakeWhisper(**errors)
        with mock.patch.object(config, "FASTER_WHISPER_DEVICE", device):
            return wd.load_whisper_model(fake, _np), fake

    def test_cpu_setting_never_tries_the_gpu(self):
        loaded, fake = self._load("cpu")
        self.assertEqual(fake.built, [("base", "cpu", "int8")])
        self.assertEqual((loaded.device, loaded.name, loaded.min_confidence), ("cpu", "base", 0.35))
        wd.preload_cuda_dlls.assert_not_called()

    def test_auto_uses_the_gpu_model_once_a_decode_proves_it(self):
        loaded, fake = self._load("auto")
        self.assertEqual(fake.built, [("medium", "cuda", "int8_float16")])
        self.assertEqual((loaded.device, loaded.compute_type, loaded.min_confidence), ("cuda", "int8_float16", 0.25))
        self.assertEqual(loaded.model.decodes, 1)                # the proof decode
        wd.preload_cuda_dlls.assert_called_once()

    def test_a_gpu_that_loads_but_cannot_decode_falls_back_to_the_cpu(self):
        # exactly what this PC did before: the model loaded, the first decode said
        # "Library cublas64_12.dll is not found"
        loaded, fake = self._load("auto", gpu_decode_error=RuntimeError("Library cublas64_12.dll is not found"))
        self.assertEqual(fake.built, [("medium", "cuda", "int8_float16"), ("base", "cpu", "int8")])
        self.assertEqual((loaded.device, loaded.name), ("cpu", "base"))

    def test_no_gpu_at_all_falls_back_too_even_when_cuda_is_asked_for(self):
        loaded, _fake = self._load("cuda", gpu_build_error=RuntimeError("CUDA driver version is insufficient"))
        self.assertEqual(loaded.device, "cpu")

    def test_only_a_broken_cpu_model_raises(self):
        with self.assertRaises(OSError):
            self._load("auto", gpu_build_error=RuntimeError("no gpu"), cpu_build_error=OSError("no weights"))


class CudaDllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)

    def _dir(self, *parts, dlls=()):
        d = os.path.join(self.tmp, *parts)
        os.makedirs(d, exist_ok=True)
        for name in dlls:
            open(os.path.join(d, name), "wb").close()
        return d

    def test_finds_the_configured_folder_and_the_livePortrait_torch_libs(self):
        own = self._dir("own", dlls=("cublas64_12.dll",))
        empty = self._dir("empty")
        lp_libs = self._dir("lp", "env", "Lib", "site-packages", "torch", "lib", dlls=("cudnn64_9.dll",))
        lp_python = os.path.join(self.tmp, "lp", "env", "Scripts", "python.exe")
        with mock.patch.object(config, "CUDA_DLL_DIR", os.pathsep.join([own, empty, own])), \
                mock.patch.object(config, "AVATAR_LIVE_PYTHON", lp_python), \
                mock.patch.object(wd, "_site_packages", return_value=[]), \
                mock.patch.dict(os.environ, {"CUDA_PATH": ""}):
            self.assertEqual(wd.find_cuda_dll_dirs(), [own, lp_libs])

    def test_preload_loads_each_dll_once_by_full_path(self):
        a = self._dir("a", dlls=("cublasLt64_12.dll", "cublas64_12.dll"))
        b = self._dir("b", dlls=("cublas64_12.dll", "cudnn64_9.dll"))
        loader = mock.Mock(side_effect=lambda path: types.SimpleNamespace(path=path))
        with mock.patch.object(wd, "_IS_WINDOWS", True), mock.patch.dict(wd._loaded, clear=True):
            first = wd.preload_cuda_dlls([a, b], loader=loader)
            second = wd.preload_cuda_dlls([a, b], loader=loader)
        self.assertEqual(first, [os.path.join(a, "cublasLt64_12.dll"), os.path.join(a, "cublas64_12.dll"),
                                 os.path.join(b, "cudnn64_9.dll")])
        self.assertEqual(second, [])
        self.assertEqual(loader.call_count, 3)

    def test_nothing_to_preload_off_windows(self):
        with mock.patch.object(wd, "_IS_WINDOWS", False):
            self.assertEqual(wd.preload_cuda_dlls(["anything"], loader=mock.Mock()), [])


class SpeechReceiverTests(unittest.TestCase):
    def setUp(self):
        import voice.stt as stt_mod
        self.stt_mod = stt_mod
        self.receiver = stt_mod.SpeechReceiver()

    def test_the_loaded_models_floor_is_used(self):
        gpu = wd.LoadedWhisper(object(), "cuda", "medium", "int8_float16", 0.25)
        with mock.patch.object(self.stt_mod, "HAS_FASTER_WHISPER", True), \
                mock.patch.object(config, "FASTER_WHISPER_ENABLED", True), \
                mock.patch.object(self.stt_mod.whisper_device, "load_whisper_model", return_value=gpu):
            self.assertIs(self.receiver._get_whisper_model(), gpu.model)
        self.assertEqual(self.receiver._min_confidence(), 0.25)
        self.receiver._whisper_model = object()            # swapped from outside (as tests do): config's floor
        with mock.patch.object(config, "WHISPER_MIN_CONFIDENCE", 0.35):
            self.assertEqual(self.receiver._min_confidence(), 0.35)

    def test_preload_loads_once_in_the_background(self):
        calls = []
        with mock.patch.object(self.stt_mod, "HAS_FASTER_WHISPER", True), \
                mock.patch.object(config, "FASTER_WHISPER_ENABLED", True), \
                mock.patch.object(config, "FASTER_WHISPER_PRELOAD", True), \
                mock.patch.object(self.receiver, "_get_whisper_model", side_effect=lambda: calls.append(1)), \
                mock.patch.object(self.stt_mod.threading, "Thread") as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs["target"]()
            self.receiver.preload_whisper()
            self.receiver.preload_whisper()
        self.assertEqual(len(calls), 1)
        self.assertEqual(thread.call_args.kwargs["name"], "WaveWhisperPreload")

    def test_preload_is_off_in_tests_and_when_switched_off(self):
        with mock.patch.object(self.stt_mod, "HAS_FASTER_WHISPER", True), \
                mock.patch.object(self.stt_mod.threading, "Thread") as thread:
            self.receiver.preload_whisper()                  # tests/_phase4_support switches it off
        thread.assert_not_called()

    def test_both_mic_workers_preload_when_they_start(self):
        for path in ("webui/voice_worker.py", "ui/workers.py"):
            with open(os.path.join(S.ROOT, *path.split("/")), encoding="utf-8") as f:
                self.assertIn('getattr(stt_engine, "preload_whisper", None)', f.read(), path)


if __name__ == "__main__":
    unittest.main()
