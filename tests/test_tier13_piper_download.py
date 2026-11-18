"""Tier 13 - "Local neural voice by default when installed... download the
voice model on first use, with consent and a checksum": voice/tts.py's
_download_default_piper_voice(), _confirm_piper_download(),
_verify_piper_voice_checksum() and the auto-download branch inside
_get_piper_voice().

Brings back an Oct 18-26 effort's auto-download mechanism (same
`python -m piper.download_voices <voice>` CLI call, same shape - see the
removed tests/test_piper_auto_download.py this is modeled on) with the
two things this tier's roadmap entry specifically asks for on top:
explicit consent and a checksum. subprocess.run is mocked throughout -
no real network call or piper-tts install is needed or wanted here.

    python -m unittest discover -s tests -p "test_tier13_piper_download.py"
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from test_phase5_tts_fallback import _install_fakes


class _BasePiperDownloadTestCase(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.tmpdir = tempfile.mkdtemp(prefix="wave_piper_dl_test_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)

    def _cfg(self, **overrides):
        values = {
            "TTS_LOCAL_AUTO_DOWNLOAD": True,
            "TTS_LOCAL_DEFAULT_VOICE": "en_US-lessac-medium",
            "TTS_LOCAL_VOICES_DIR": self.tmpdir,
            "TTS_LOCAL_DOWNLOAD_TIMEOUT_S": 45.0,
        }
        values.update(overrides)
        return mock.patch.multiple(self.ttsmod.config, **values)

    def _onnx_path(self, voice="en_US-lessac-medium"):
        return os.path.join(self.tmpdir, f"{voice}.onnx")


class DownloadDefaultPiperVoiceTests(_BasePiperDownloadTestCase):
    # ---- gating: off by default, nothing configured, already there -----

    def test_disabled_by_config_returns_none_without_calling_subprocess(self):
        with self._cfg(TTS_LOCAL_AUTO_DOWNLOAD=False), mock.patch.object(subprocess, "run") as m_run:
            self.assertIsNone(self.engine._download_default_piper_voice())
        m_run.assert_not_called()

    def test_off_by_default_when_nothing_overrides_the_setting(self):
        """TTS_LOCAL_AUTO_DOWNLOAD's real default (not this test file's
        _cfg() convenience override) is false - the deliberate opt-in the
        roadmap and settings/config.py's own comment call for."""
        self.assertFalse(self.ttsmod.config.TTS_LOCAL_AUTO_DOWNLOAD)

    def test_missing_voice_name_returns_none(self):
        with self._cfg(TTS_LOCAL_DEFAULT_VOICE=""), mock.patch.object(subprocess, "run") as m_run:
            self.assertIsNone(self.engine._download_default_piper_voice())
        m_run.assert_not_called()

    def test_blank_voices_dir_falls_back_to_a_data_dir_default_instead_of_giving_up(self):
        """Improvement over the Oct 18-26 behavior: a blank directory is
        treated as "use the sensible default" (matching how
        voice/wakeword.py's own downloaded files default under
        config.DATA_DIR), not "nothing configured, give up"."""
        with self._cfg(TTS_LOCAL_VOICES_DIR=""):
            self.assertIn("voice_models", self.engine._default_piper_voices_dir())
            self.assertIn("piper", self.engine._default_piper_voices_dir())

    def test_already_downloaded_voice_is_reused_without_calling_subprocess(self):
        onnx_path = self._onnx_path()
        with open(onnx_path, "wb") as f:
            f.write(b"existing model")
        with self._cfg(), mock.patch.object(subprocess, "run") as m_run:
            self.assertEqual(self.engine._download_default_piper_voice(), onnx_path)
        m_run.assert_not_called()

    # ---- consent (new this tier) ----------------------------------------

    def test_consent_hook_declining_skips_the_download_entirely(self):
        with self._cfg(), mock.patch.object(subprocess, "run") as m_run:
            self.engine.on_piper_download_consent = lambda voice: False
            self.assertIsNone(self.engine._download_default_piper_voice())
        m_run.assert_not_called()

    def test_consent_hook_is_asked_with_the_voice_name(self):
        seen = []
        with self._cfg(), mock.patch.object(subprocess, "run") as m_run:
            m_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            self.engine.on_piper_download_consent = lambda voice: seen.append(voice) or True
            self.engine._download_default_piper_voice()  # no file written -> fails later, fine
        self.assertEqual(seen, ["en_US-lessac-medium"])

    def test_no_consent_hook_set_means_the_auto_download_flag_alone_is_consent(self):
        """The default (no hook wired up by any UI yet): the config flag
        being on is this project's whole consent story."""
        self.assertIsNone(self.engine.on_piper_download_consent)
        self.assertTrue(self.engine._confirm_piper_download("en_US-lessac-medium"))

    def test_a_raising_consent_hook_does_not_block_a_download_the_flag_already_allowed(self):
        with self._cfg(), mock.patch.object(subprocess, "run") as m_run:
            m_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            self.engine.on_piper_download_consent = mock.Mock(side_effect=RuntimeError("UI not wired up"))
            self.engine._download_default_piper_voice()
        m_run.assert_called_once()

    # ---- the download itself --------------------------------------------

    def test_successful_download_returns_the_onnx_path(self):
        onnx_path = self._onnx_path()

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            with open(onnx_path, "wb") as f:
                f.write(b"x" * 63201294)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        real_sha = hashlib.sha256(b"x" * 63201294).hexdigest()
        with self._cfg(), \
             mock.patch.object(subprocess, "run", side_effect=_fake_run) as m_run, \
             mock.patch.dict(self.ttsmod.PIPER_VOICE_CHECKSUMS,
                             {"en_US-lessac-medium": {"sha256": real_sha, "size": 63201294}}):
            result = self.engine._download_default_piper_voice()
        self.assertEqual(result, onnx_path)
        args, kwargs = m_run.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "piper.download_voices", "en_US-lessac-medium"])
        self.assertEqual(kwargs.get("cwd"), self.tmpdir)

    def test_voices_dir_is_created_if_missing(self):
        nested = os.path.join(self.tmpdir, "not_yet_created")

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            with open(os.path.join(nested, "en_US-lessac-medium.onnx"), "wb") as f:
                f.write(b"x" * 63201294)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        real_sha = hashlib.sha256(b"x" * 63201294).hexdigest()
        with self._cfg(TTS_LOCAL_VOICES_DIR=nested), \
             mock.patch.object(subprocess, "run", side_effect=_fake_run), \
             mock.patch.dict(self.ttsmod.PIPER_VOICE_CHECKSUMS,
                             {"en_US-lessac-medium": {"sha256": real_sha, "size": 63201294}}):
            self.assertIsNotNone(self.engine._download_default_piper_voice())

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

    def test_reported_success_but_no_file_returns_none(self):
        with self._cfg(), mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ):
            self.assertIsNone(self.engine._download_default_piper_voice())

    # ---- checksum (new this tier) ----------------------------------------

    def test_a_corrupted_download_is_discarded_and_rejected(self):
        onnx_path = self._onnx_path()

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            with open(onnx_path, "wb") as f:
                f.write(b"not the real model bytes at all")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self._cfg(), mock.patch.object(subprocess, "run", side_effect=_fake_run):
            # Uses the REAL pinned checksum for en_US-lessac-medium - these
            # fake bytes must not match it.
            self.assertIsNone(self.engine._download_default_piper_voice())
        self.assertFalse(os.path.exists(onnx_path), "a failed-checksum file must be removed, not left behind")

    def test_wrong_size_is_rejected_without_hashing_the_whole_file(self):
        onnx_path = self._onnx_path()

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            with open(onnx_path, "wb") as f:
                f.write(b"too short")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self._cfg(), mock.patch.object(subprocess, "run", side_effect=_fake_run):
            self.assertIsNone(self.engine._download_default_piper_voice())
        self.assertFalse(os.path.exists(onnx_path))

    def test_a_voice_with_no_pinned_checksum_still_downloads_with_a_warning(self):
        """Choosing a voice other than the shipped default is itself a
        deliberate, informed choice - see _verify_piper_voice_checksum()'s
        docstring. This must not silently brick auto-download for anyone
        who set TTS_LOCAL_DEFAULT_VOICE to something else."""
        onnx_path = os.path.join(self.tmpdir, "hi_IN-pratham-medium.onnx")

        def _fake_run(cmd, cwd=None, timeout=None, capture_output=None, text=None):
            with open(onnx_path, "wb") as f:
                f.write(b"whatever this voice's real bytes are")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with self._cfg(TTS_LOCAL_DEFAULT_VOICE="hi_IN-pratham-medium"), \
             mock.patch.object(subprocess, "run", side_effect=_fake_run):
            result = self.engine._download_default_piper_voice()
        self.assertEqual(result, onnx_path)
        self.assertTrue(os.path.exists(onnx_path), "an unpinned voice is NOT discarded, just unverified")

    def test_the_real_pinned_checksum_constant_is_a_64_char_lowercase_hex_string(self):
        """A sanity check on the manifest itself, not just the logic
        around it - a typo'd hash here would reject every real download."""
        for voice, info in self.ttsmod.PIPER_VOICE_CHECKSUMS.items():
            sha = info["sha256"]
            self.assertEqual(len(sha), 64, voice)
            self.assertEqual(sha, sha.lower(), voice)
            int(sha, 16)  # raises ValueError if it isn't valid hex
            self.assertGreater(info["size"], 0, voice)


class GetPiperVoiceAutoDownloadTests(_BasePiperDownloadTestCase):
    """_get_piper_voice()'s own branch: only reached when
    TTS_LOCAL_MODEL_PATH is blank, never silently overrides a user-set path."""

    def _fake_piper_voice_class(self):
        loaded = {}

        class _FakePiperVoice:
            @staticmethod
            def load(path):
                loaded["path"] = path
                return _FakePiperVoice()

        return _FakePiperVoice, loaded

    def test_downloads_when_nothing_is_configured(self):
        fake_cls, loaded = self._fake_piper_voice_class()
        with self._cfg(TTS_LOCAL_MODEL_PATH="", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.ttsmod, "PiperVoice", fake_cls), \
             mock.patch.object(self.engine, "_download_default_piper_voice", return_value="/tmp/fake.onnx") as m_dl:
            voice = self.engine._get_piper_voice()
        self.assertIsNotNone(voice)
        m_dl.assert_called_once()
        self.assertEqual(loaded["path"], "/tmp/fake.onnx")

    def test_does_not_download_when_model_path_is_set_but_missing(self):
        """A user-set-but-broken path fails as a real error, never
        silently substitutes a downloaded default voice they didn't ask
        for - unchanged from the Oct 18-26 contract."""
        with self._cfg(TTS_LOCAL_MODEL_PATH="/does/not/exist.onnx", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.engine, "_download_default_piper_voice") as m_dl:
            self.assertIsNone(self.engine._get_piper_voice())
        m_dl.assert_not_called()
        self.assertTrue(self.engine._piper_load_failed)

    def test_failed_download_marks_load_failed(self):
        with self._cfg(TTS_LOCAL_MODEL_PATH="", TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.engine, "_download_default_piper_voice", return_value=None):
            self.assertIsNone(self.engine._get_piper_voice())
        self.assertTrue(self.engine._piper_load_failed)

    def test_a_set_model_path_never_even_calls_the_download_helper(self):
        onnx_path = os.path.join(self.tmpdir, "real.onnx")
        with open(onnx_path, "wb") as f:
            f.write(b"x")
        fake_cls, loaded = self._fake_piper_voice_class()
        with self._cfg(TTS_LOCAL_MODEL_PATH=onnx_path, TTS_LOCAL_ENABLED=True), \
             mock.patch.object(self.ttsmod, "HAS_PIPER", True), \
             mock.patch.object(self.ttsmod, "PiperVoice", fake_cls), \
             mock.patch.object(self.engine, "_download_default_piper_voice") as m_dl:
            self.engine._get_piper_voice()
        m_dl.assert_not_called()
        self.assertEqual(loaded["path"], onnx_path)


if __name__ == "__main__":
    unittest.main()
