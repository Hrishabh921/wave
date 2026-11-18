"""SARA-roadmap Tier 7 - "make Piper the default, not opt-in": tests for
voice/tts.py's _download_default_piper_voice() and the auto-download
branch inside _get_piper_voice(). Mocks subprocess.run entirely - no real
network call or piper-tts install is needed or wanted here.

    python -m unittest discover -s tests -p "test_piper_auto_download.py"
"""
import subprocess
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)


def _install_fakes():
    fake_mixer = types.SimpleNamespace(
        init=lambda **kw: None, get_init=lambda: (24000, 1),
        music=types.SimpleNamespace(unload=lambda: None, stop=lambda: None, set_volume=lambda v: None),
    )
    fake_pygame = types.ModuleType("pygame")
    fake_pygame.mixer = fake_mixer
    fake_pygame.time = types.SimpleNamespace(wait=lambda ms: None)
    fake_pygame.sndarray = types.SimpleNamespace(array=lambda s: None, make_sound=lambda a: None)
    sys.modules["pygame"] = fake_pygame

    fake_edge_tts = types.ModuleType("edge_tts")

    class _FC:
        def __init__(self, *a, **k):
            pass

        async def save(self, out_path):
            with open(out_path, "wb") as f:
                f.write(b"FAKE")

    fake_edge_tts.Communicate = _FC
    sys.modules["edge_tts"] = fake_edge_tts
    sys.modules.pop("sounddevice", None)


class PiperAutoDownloadTests(unittest.TestCase):
    def setUp(self):
        _install_fakes()
        for mod in ("voice.tts", "voice.tts_cache", "voice.aec"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.tmpdir = self._make_tmpdir()
        self.addCleanup(self._cleanup_modules)

    def _make_tmpdir(self):
        import shutil
        import tempfile

        d = tempfile.mkdtemp(prefix="wave_piper_test_")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def _cleanup_modules(self):
        for mod in ("pygame", "edge_tts", "voice.tts", "voice.tts_cache", "voice.aec"):
            sys.modules.pop(mod, None)

    def _cfg(self, **overrides):
        """A context manager patching settings.config.config with Tier-7
        Piper-download defaults, overridable per test."""
        values = {
            "TTS_LOCAL_AUTO_DOWNLOAD": True,
            "TTS_LOCAL_DEFAULT_VOICE": "en_US-lessac-medium",
            "TTS_LOCAL_VOICES_DIR": self.tmpdir,
            "TTS_LOCAL_DOWNLOAD_TIMEOUT_S": 45.0,
        }
        values.update(overrides)
        return mock.patch.multiple(self.ttsmod.config, **values)

    # ---- _download_default_piper_voice() ---------------------------------

    def test_disabled_by_config_returns_none_without_calling_subprocess(self):
        with self._cfg(TTS_LOCAL_AUTO_DOWNLOAD=False), \
             mock.patch.object(subprocess, "run") as m_run:
            self.assertIsNone(self.engine._download_default_piper_voice())
            m_run.assert_not_called()

    def test_missing_voice_or_dir_returns_none(self):
        with self._cfg(TTS_LOCAL_DEFAULT_VOICE=""):
            self.assertIsNone(self.engine._download_default_piper_voice())
        with self._cfg(TTS_LOCAL_VOICES_DIR=""):
            self.assertIsNone(self.engine._download_default_piper_voice())

    def test_already_downloaded_voice_is_reused_without_calling_subprocess(self):
        import os

        onnx_path = os.path.join(self.tmpdir, "en_US-lessac-medium.onnx")
        with open(onnx_path, "wb") as f:
            f.write(b"existing model")
        with self._cfg(), mock.patch.object(subprocess, "run") as m_run:
            self.assertEqual(self.engine._download_default_piper_voice(), onnx_path)
            m_run.assert_not_called()

    def test_successful_download_returns_the_onnx_path(self):
        import os

        onnx_path = os.path.join(self.tmpdir, "en_US-lessac-medium.onnx")

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            # Simulate the CLI actually writing the model file.
            with open(onnx_path, "wb") as f:
                f.write(b"downloaded model")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self._cfg(), mock.patch.object(subprocess, "run", side_effect=_fake_run) as m_run:
            result = self.engine._download_default_piper_voice()
            self.assertEqual(result, onnx_path)
            args, kwargs = m_run.call_args
            self.assertEqual(args[0], [sys.executable, "-m", "piper.download_voices", "en_US-lessac-medium"])
            self.assertEqual(kwargs.get("cwd"), self.tmpdir)

    def test_nonzero_exit_returns_none(self):
        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network unreachable")

        with self._cfg(), mock.patch.object(subprocess, "run", side_effect=_fake_run):
            self.assertIsNone(self.engine._download_default_piper_voice())

    def test_timeout_returns_none_without_raising(self):
        with self._cfg(), mock.patch.object(
            subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="piper", timeout=45.0)
        ):
            self.assertIsNone(self.engine._download_default_piper_voice())

    def test_unexpected_error_returns_none_without_raising(self):
        with self._cfg(), mock.patch.object(subprocess, "run", side_effect=OSError("no such file")):
            self.assertIsNone(self.engine._download_default_piper_voice())

    # ---- _get_piper_voice()'s auto-download branch ------------------------

    def _fake_piper_voice_class(self):
        loaded = {}

        class _FakePiperVoice:
            @staticmethod
            def load(path):
                loaded["path"] = path
                return _FakePiperVoice()

        return _FakePiperVoice, loaded

    def test_get_piper_voice_downloads_when_nothing_is_configured(self):
        fake_cls, loaded = self._fake_piper_voice_class()
        with self._cfg(TTS_LOCAL_MODEL_PATH="", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.ttsmod, "PiperVoice", fake_cls), \
             mock.patch.object(self.engine, "_download_default_piper_voice", return_value="/tmp/fake.onnx") as m_dl:
            voice = self.engine._get_piper_voice()
            self.assertIsNotNone(voice)
            m_dl.assert_called_once()
            self.assertEqual(loaded["path"], "/tmp/fake.onnx")

    def test_get_piper_voice_does_not_download_when_model_path_is_set_but_missing(self):
        """A user-set-but-broken path must fail as before, not silently
        substitute a downloaded default voice they didn't ask for."""
        with self._cfg(TTS_LOCAL_MODEL_PATH="/does/not/exist.onnx", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.engine, "_download_default_piper_voice") as m_dl:
            self.assertIsNone(self.engine._get_piper_voice())
            m_dl.assert_not_called()
            self.assertTrue(self.engine._piper_load_failed)

    def test_get_piper_voice_failed_download_marks_load_failed(self):
        with self._cfg(TTS_LOCAL_MODEL_PATH="", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.engine, "_download_default_piper_voice", return_value=None):
            self.assertIsNone(self.engine._get_piper_voice())
            self.assertTrue(self.engine._piper_load_failed)
