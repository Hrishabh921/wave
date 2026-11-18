"""Tier 13 - the optional Kokoro bilingual local neural voice.

Piper (tests/test_tier13_piper_download.py) is English-only. Handed
Hindi text, its espeak-ng phonemizer doesn't raise - it just
mispronounces the Devanagari and still reports success, which would
silently stop voice/tts.py's _speak_offline_fallback() before Kokoro
(https://github.com/thewh1teagle/kokoro-onnx) ever got a turn. So this
tier's routing fix tries Kokoro FIRST for lang="hi", and Piper/SAPI
(still English) only as the fallback if Kokoro itself isn't usable -
see _speak_offline_fallback()'s own docstring for the full reasoning.

Covers:
  - _download_one_kokoro_file() / _download_kokoro_files() /
    _confirm_kokoro_download() / _verify_kokoro_file_checksum() - mirrors
    test_tier13_piper_download.py's structure for Piper's equivalents,
    adapted for TWO fixed files and a plain `requests` GET instead of a
    download CLI (kokoro-onnx, unlike piper-tts, ships none of its own).
  - _get_kokoro_voice()'s lazy-load/auto-download/cache-the-failure
    branches.
  - _speak_via_kokoro()'s voice/lang selection and float32->int16 PCM
    conversion.
  - _speak_offline_fallback()'s Kokoro-before-Piper-for-Hindi routing,
    and the English-vs-Hindi text_prep split between the two engines.

requests.get and the Kokoro class are mocked throughout - no real
network call or kokoro-onnx model load happens here.

    python -m unittest discover -s tests -p "test_tier13_kokoro.py"
"""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from test_phase5_tts_fallback import _install_fakes


class _FakeKokoroResponse:
    """Stands in for a `requests.Response` used as `with requests.get(...)
    as resp:` - the only two members _download_one_kokoro_file() touches."""

    def __init__(self, content: bytes, status_ok: bool = True):
        self._content = content
        self._status_ok = status_ok

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self):
        if not self._status_ok:
            raise RuntimeError("simulated: HTTP error status")

    def iter_content(self, chunk_size=1):
        content = self._content
        for i in range(0, len(content), chunk_size):
            yield content[i:i + chunk_size]


def _make_fake_get(content_by_filename: dict, fail_for: tuple = ()):
    """Returns a fake `requests.get(url, stream=..., timeout=...)` that
    dispatches on the URL's trailing filename - content_by_filename maps
    filename -> bytes to "serve"; fail_for lists filenames whose request
    raises a simulated connection error instead."""

    def _fake_get(url, stream=None, timeout=None):
        for filename in fail_for:
            if url.endswith(filename):
                raise ConnectionError(f"simulated: couldn't reach {url}")
        for filename, content in content_by_filename.items():
            if url.endswith(filename):
                return _FakeKokoroResponse(content)
        raise AssertionError(f"unexpected download URL in test: {url}")

    return _fake_get


class _BaseKokoroTestCase(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.tmpdir = tempfile.mkdtemp(prefix="wave_kokoro_test_")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)

    def _cfg(self, **overrides):
        values = {
            "TTS_KOKORO_ENABLED": True,
            "TTS_KOKORO_AUTO_DOWNLOAD": True,
            "TTS_KOKORO_MODEL_DIR": self.tmpdir,
            "TTS_KOKORO_VOICE_EN": "af_heart",
            "TTS_KOKORO_VOICE_HI": "hf_alpha",
            "TTS_KOKORO_DOWNLOAD_TIMEOUT_S": 60.0,
        }
        values.update(overrides)
        return mock.patch.multiple(self.ttsmod.config, **values)

    def _model_path(self, base_dir=None):
        return os.path.join(base_dir or self.tmpdir, "kokoro-v1.0.int8.onnx")

    def _voices_path(self, base_dir=None):
        return os.path.join(base_dir or self.tmpdir, "voices-v1.0.bin")

    @staticmethod
    def _checksummed(content: bytes):
        """(content, matching sha256 hex) - small fake bytes are plenty;
        the real pinned checksums/sizes have their own dedicated sanity
        test below rather than every test writing real-sized (~92MB/
        ~28MB) files."""
        return content, hashlib.sha256(content).hexdigest()


class DownloadKokoroFilesTests(_BaseKokoroTestCase):
    # ---- gating: off by default, nothing configured, already there ----

    def test_disabled_by_config_returns_none_without_any_network_call(self):
        with self._cfg(TTS_KOKORO_AUTO_DOWNLOAD=False), mock.patch("requests.get") as m_get:
            self.assertIsNone(self.engine._download_kokoro_files())
        m_get.assert_not_called()

    def test_off_by_default_when_nothing_overrides_the_setting(self):
        """TTS_KOKORO_AUTO_DOWNLOAD's real default (not this test file's
        _cfg() convenience override) is false - its own deliberate
        opt-in, separate from Piper's TTS_LOCAL_AUTO_DOWNLOAD."""
        self.assertFalse(self.ttsmod.config.TTS_KOKORO_AUTO_DOWNLOAD)

    def test_both_files_already_present_are_reused_without_any_network_call(self):
        with open(self._model_path(), "wb") as f:
            f.write(b"existing model")
        with open(self._voices_path(), "wb") as f:
            f.write(b"existing voices")
        with self._cfg(), mock.patch("requests.get") as m_get:
            result = self.engine._download_kokoro_files()
        self.assertEqual(result, (self._model_path(), self._voices_path()))
        m_get.assert_not_called()

    def test_blank_model_dir_falls_back_to_a_data_dir_default_instead_of_giving_up(self):
        """Same improvement _default_piper_voices_dir() already has -
        matches voice/wakeword.py's own downloaded-file default shape."""
        with self._cfg(TTS_KOKORO_MODEL_DIR=""):
            self.assertIn("voice_models", self.engine._default_kokoro_model_dir())
            self.assertIn("kokoro", self.engine._default_kokoro_model_dir())

    # ---- consent -----------------------------------------------------

    def test_consent_hook_declining_skips_the_download_entirely(self):
        with self._cfg(), mock.patch("requests.get") as m_get:
            self.engine.on_kokoro_download_consent = lambda filename: False
            self.assertIsNone(self.engine._download_kokoro_files())
        m_get.assert_not_called()

    def test_consent_hook_is_asked_with_each_filename(self):
        seen = []
        self.engine.on_kokoro_download_consent = lambda filename: seen.append(filename) or False
        with self._cfg():
            self.engine._download_kokoro_files()
        self.assertEqual(seen, ["kokoro-v1.0.int8.onnx"])  # declines before ever reaching the second file

    def test_no_consent_hook_set_means_the_auto_download_flag_alone_is_consent(self):
        model_bytes, model_sha = self._checksummed(b"fake-model-bytes")
        voices_bytes, voices_sha = self._checksummed(b"fake-voices-bytes")
        fake_get = _make_fake_get({
            "kokoro-v1.0.int8.onnx": model_bytes,
            "voices-v1.0.bin": voices_bytes,
        })
        checks = {
            "kokoro-v1.0.int8.onnx": {"sha256": model_sha, "size": len(model_bytes)},
            "voices-v1.0.bin": {"sha256": voices_sha, "size": len(voices_bytes)},
        }
        self.assertIsNone(self.engine.on_kokoro_download_consent)
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get), \
             mock.patch.dict(self.ttsmod.KOKORO_FILE_CHECKSUMS, checks):
            result = self.engine._download_kokoro_files()
        self.assertEqual(result, (self._model_path(), self._voices_path()))

    def test_a_raising_consent_hook_does_not_block_a_download_the_flag_already_allowed(self):
        model_bytes, model_sha = self._checksummed(b"m")
        voices_bytes, voices_sha = self._checksummed(b"v")
        fake_get = _make_fake_get({
            "kokoro-v1.0.int8.onnx": model_bytes,
            "voices-v1.0.bin": voices_bytes,
        })
        checks = {
            "kokoro-v1.0.int8.onnx": {"sha256": model_sha, "size": len(model_bytes)},
            "voices-v1.0.bin": {"sha256": voices_sha, "size": len(voices_bytes)},
        }

        def _raising_hook(filename):
            raise RuntimeError("simulated: a broken Settings-UI hook")

        self.engine.on_kokoro_download_consent = _raising_hook
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get), \
             mock.patch.dict(self.ttsmod.KOKORO_FILE_CHECKSUMS, checks):
            result = self.engine._download_kokoro_files()
        self.assertIsNotNone(result)

    # ---- the actual download + checksum flow --------------------------

    def test_successful_download_of_both_files_returns_their_paths(self):
        model_bytes, model_sha = self._checksummed(b"the model file's bytes")
        voices_bytes, voices_sha = self._checksummed(b"the voices file's bytes")
        fake_get = _make_fake_get({
            "kokoro-v1.0.int8.onnx": model_bytes,
            "voices-v1.0.bin": voices_bytes,
        })
        checks = {
            "kokoro-v1.0.int8.onnx": {"sha256": model_sha, "size": len(model_bytes)},
            "voices-v1.0.bin": {"sha256": voices_sha, "size": len(voices_bytes)},
        }
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get) as m_get, \
             mock.patch.dict(self.ttsmod.KOKORO_FILE_CHECKSUMS, checks):
            result = self.engine._download_kokoro_files()
        self.assertEqual(result, (self._model_path(), self._voices_path()))
        with open(self._model_path(), "rb") as f:
            self.assertEqual(f.read(), model_bytes)
        with open(self._voices_path(), "rb") as f:
            self.assertEqual(f.read(), voices_bytes)
        self.assertEqual(m_get.call_count, 2)

    def test_model_dir_is_created_if_missing(self):
        nested = os.path.join(self.tmpdir, "not_yet_created")
        model_bytes, model_sha = self._checksummed(b"m")
        voices_bytes, voices_sha = self._checksummed(b"v")
        fake_get = _make_fake_get({
            "kokoro-v1.0.int8.onnx": model_bytes,
            "voices-v1.0.bin": voices_bytes,
        })
        checks = {
            "kokoro-v1.0.int8.onnx": {"sha256": model_sha, "size": len(model_bytes)},
            "voices-v1.0.bin": {"sha256": voices_sha, "size": len(voices_bytes)},
        }
        with self._cfg(TTS_KOKORO_MODEL_DIR=nested), mock.patch("requests.get", side_effect=fake_get), \
             mock.patch.dict(self.ttsmod.KOKORO_FILE_CHECKSUMS, checks):
            self.assertIsNotNone(self.engine._download_kokoro_files())

    def test_a_partial_download_resumes_only_the_missing_file(self):
        """The model file already succeeded on a previous attempt (or was
        placed by hand); only the voices file should be fetched now."""
        with open(self._model_path(), "wb") as f:
            f.write(b"already have this one")
        voices_bytes, voices_sha = self._checksummed(b"v")
        fake_get = _make_fake_get({"voices-v1.0.bin": voices_bytes})
        checks = {"voices-v1.0.bin": {"sha256": voices_sha, "size": len(voices_bytes)}}
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get) as m_get, \
             mock.patch.dict(self.ttsmod.KOKORO_FILE_CHECKSUMS, checks):
            result = self.engine._download_kokoro_files()
        self.assertEqual(result, (self._model_path(), self._voices_path()))
        self.assertEqual(m_get.call_count, 1)

    def test_a_network_error_returns_none_without_raising(self):
        fake_get = _make_fake_get({}, fail_for=("kokoro-v1.0.int8.onnx",))
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get):
            self.assertIsNone(self.engine._download_kokoro_files())

    def test_an_http_error_status_leaves_no_partial_file_behind(self):
        def _fake_get(url, stream=None, timeout=None):
            return _FakeKokoroResponse(b"", status_ok=False)

        with self._cfg(), mock.patch("requests.get", side_effect=_fake_get):
            self.assertIsNone(self.engine._download_kokoro_files())
        self.assertFalse(os.path.exists(self._model_path() + ".part"))
        self.assertFalse(os.path.exists(self._model_path()))

    # ---- checksum verification -----------------------------------------

    def test_a_corrupted_download_is_discarded_and_rejected(self):
        """Uses the REAL pinned checksum for kokoro-v1.0.int8.onnx - these
        fake bytes must not match it."""
        fake_get = _make_fake_get({"kokoro-v1.0.int8.onnx": b"not the real model bytes at all"})
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get):
            self.assertIsNone(self.engine._download_kokoro_files())
        self.assertFalse(os.path.exists(self._model_path()), "a failed-checksum file must not be left behind")

    def test_wrong_size_is_rejected_without_hashing_the_whole_file(self):
        fake_get = _make_fake_get({"kokoro-v1.0.int8.onnx": b"too short"})
        with self._cfg(), mock.patch("requests.get", side_effect=fake_get):
            self.assertIsNone(self.engine._download_kokoro_files())
        self.assertFalse(os.path.exists(self._model_path()))

    def test_an_unpinned_filename_still_downloads_with_a_warning(self):
        """Both of Kokoro's filenames are always pinned in normal
        operation, but _verify_kokoro_file_checksum() itself must not
        refuse an unrecognized name outright - see its docstring."""
        with open(self._voices_path(), "wb") as f:
            f.write(b"whatever bytes")
        self.assertTrue(self.engine._verify_kokoro_file_checksum("some-other-file.bin", self._voices_path()))

    def test_the_real_pinned_checksums_are_64_char_lowercase_hex_strings(self):
        """A sanity check on the manifest itself, not just the logic
        around it - a typo'd hash here would reject every real
        download."""
        self.assertEqual(len(self.ttsmod.KOKORO_FILE_CHECKSUMS), 2)
        for filename, info in self.ttsmod.KOKORO_FILE_CHECKSUMS.items():
            sha = info["sha256"]
            self.assertEqual(len(sha), 64, filename)
            self.assertEqual(sha, sha.lower(), filename)
            int(sha, 16)  # raises ValueError if it isn't valid hex
            self.assertGreater(info["size"], 0, filename)


class GetKokoroVoiceTests(_BaseKokoroTestCase):
    """_get_kokoro_voice()'s own lazy-load/cache-the-failure branches -
    mirrors GetPiperVoiceAutoDownloadTests in
    test_tier13_piper_download.py."""

    def _fake_kokoro_class(self):
        constructed = {}

        class _FakeKokoro:
            def __init__(self, model_path, voices_path):
                constructed["model_path"] = model_path
                constructed["voices_path"] = voices_path

        return _FakeKokoro, constructed

    def test_disabled_by_default_returns_none_even_with_files_present(self):
        with open(self._model_path(), "wb") as f:
            f.write(b"m")
        with open(self._voices_path(), "wb") as f:
            f.write(b"v")
        fake_cls, _ = self._fake_kokoro_class()
        with self._cfg(TTS_KOKORO_ENABLED=False), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.ttsmod, "Kokoro", fake_cls):
            self.assertIsNone(self.engine._get_kokoro_voice())

    def test_not_installed_returns_none_even_when_enabled(self):
        with self._cfg(TTS_KOKORO_ENABLED=True), mock.patch.object(self.ttsmod, "HAS_KOKORO", False):
            self.assertIsNone(self.engine._get_kokoro_voice())

    def test_downloads_when_files_are_missing(self):
        fake_cls, constructed = self._fake_kokoro_class()
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.ttsmod, "Kokoro", fake_cls), \
             mock.patch.object(self.engine, "_download_kokoro_files",
                               return_value=("/tmp/fake-model.onnx", "/tmp/fake-voices.bin")) as m_dl:
            voice = self.engine._get_kokoro_voice()
        self.assertIsNotNone(voice)
        m_dl.assert_called_once()
        self.assertEqual(constructed["model_path"], "/tmp/fake-model.onnx")
        self.assertEqual(constructed["voices_path"], "/tmp/fake-voices.bin")

    def test_both_files_already_present_never_calls_the_download_helper(self):
        with open(self._model_path(), "wb") as f:
            f.write(b"m")
        with open(self._voices_path(), "wb") as f:
            f.write(b"v")
        fake_cls, constructed = self._fake_kokoro_class()
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.ttsmod, "Kokoro", fake_cls), \
             mock.patch.object(self.engine, "_download_kokoro_files") as m_dl:
            self.engine._get_kokoro_voice()
        m_dl.assert_not_called()
        self.assertEqual(constructed["model_path"], self._model_path())

    def test_failed_download_marks_load_failed_and_does_not_retry(self):
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.engine, "_download_kokoro_files", return_value=None) as m_dl:
            self.assertIsNone(self.engine._get_kokoro_voice())
            self.assertIsNone(self.engine._get_kokoro_voice())
        self.assertTrue(self.engine._kokoro_load_failed)
        m_dl.assert_called_once()  # second call short-circuited on the cached failure

    def test_a_working_voice_is_cached_across_calls(self):
        fake_cls, constructed = self._fake_kokoro_class()
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.ttsmod, "Kokoro", fake_cls), \
             mock.patch.object(self.engine, "_download_kokoro_files",
                               return_value=(self._model_path(), self._voices_path())) as m_dl:
            first = self.engine._get_kokoro_voice()
            second = self.engine._get_kokoro_voice()
        self.assertIs(first, second)
        m_dl.assert_called_once()

    def test_a_load_exception_is_caught_and_marks_load_failed(self):
        class _RaisingKokoro:
            def __init__(self, model_path, voices_path):
                raise RuntimeError("simulated: onnxruntime couldn't load this model")

        with open(self._model_path(), "wb") as f:
            f.write(b"m")
        with open(self._voices_path(), "wb") as f:
            f.write(b"v")
        with self._cfg(), \
             mock.patch.object(self.ttsmod, "HAS_KOKORO", True), \
             mock.patch.object(self.ttsmod, "Kokoro", _RaisingKokoro):
            self.assertIsNone(self.engine._get_kokoro_voice())
        self.assertTrue(self.engine._kokoro_load_failed)


class SpeakViaKokoroTests(_BaseKokoroTestCase):
    """_speak_via_kokoro()'s voice/lang selection and audio handoff."""

    def setUp(self):
        super().setUp()
        import numpy as np  # noqa: PLC0415
        self.np = np

    def _fake_kokoro_voice(self, samples=None, sample_rate=24000):
        if samples is None:
            samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=self.np.float32)
        calls = []

        class _FakeKokoroVoice:
            def create(self, text, voice, lang):
                calls.append({"text": text, "voice": voice, "lang": lang})
                return samples, sample_rate

        return _FakeKokoroVoice(), calls

    def test_hindi_lang_picks_the_hindi_voice_and_language_code(self):
        fake_voice, calls = self._fake_kokoro_voice()
        with self._cfg(TTS_KOKORO_VOICE_HI="hf_alpha"), \
             mock.patch.object(self.engine, "_get_kokoro_voice", return_value=fake_voice), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            result = self.engine._speak_via_kokoro("नमस्ते", self.ttsmod.language.HI)
        self.assertTrue(result)
        self.assertEqual(calls[0]["voice"], "hf_alpha")
        self.assertEqual(calls[0]["lang"], "hi")

    def test_non_hindi_lang_picks_the_english_voice_and_language_code(self):
        fake_voice, calls = self._fake_kokoro_voice()
        with self._cfg(TTS_KOKORO_VOICE_EN="af_heart"), \
             mock.patch.object(self.engine, "_get_kokoro_voice", return_value=fake_voice), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            result = self.engine._speak_via_kokoro("hello", self.ttsmod.language.EN)
        self.assertTrue(result)
        self.assertEqual(calls[0]["voice"], "af_heart")
        self.assertEqual(calls[0]["lang"], "en-us")

    def test_hinglish_lang_also_picks_the_english_voice(self):
        """Only language.HI routes to the Hindi voice - HINGLISH (and
        anything else) falls to English, same as _cloud_voice_and_text()'s
        own if-lang==HI branching."""
        fake_voice, calls = self._fake_kokoro_voice()
        with self._cfg(), \
             mock.patch.object(self.engine, "_get_kokoro_voice", return_value=fake_voice), \
             mock.patch.object(self.engine, "_play_pcm", return_value=True):
            self.engine._speak_via_kokoro("aaj mausam kaisa hai", self.ttsmod.language.HINGLISH)
        self.assertEqual(calls[0]["lang"], "en-us")

    def test_float_samples_are_converted_to_clipped_int16_pcm(self):
        samples = self.np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0], dtype=self.np.float32)
        fake_voice, _ = self._fake_kokoro_voice(samples=samples, sample_rate=22050)
        captured = {}

        def _capture_play_pcm(pcm, sample_rate):
            captured["pcm"] = pcm
            captured["sample_rate"] = sample_rate
            return True

        with self._cfg(), \
             mock.patch.object(self.engine, "_get_kokoro_voice", return_value=fake_voice), \
             mock.patch.object(self.engine, "_play_pcm", side_effect=_capture_play_pcm):
            self.assertTrue(self.engine._speak_via_kokoro("hi", self.ttsmod.language.EN))
        self.assertEqual(captured["sample_rate"], 22050)
        self.assertEqual(captured["pcm"].dtype, self.np.int16)
        # The two out-of-range values (2.0, -2.0) must be clipped to
        # full-scale, not wrapped/overflowed.
        self.assertEqual(captured["pcm"][-2], 32767)
        self.assertEqual(captured["pcm"][-1], -32767)

    def test_kokoro_unavailable_returns_false(self):
        with self._cfg(), mock.patch.object(self.engine, "_get_kokoro_voice", return_value=None):
            self.assertFalse(self.engine._speak_via_kokoro("hi", self.ttsmod.language.HI))

    def test_empty_samples_returns_false(self):
        fake_voice, _ = self._fake_kokoro_voice(samples=self.np.array([], dtype=self.np.float32))
        with self._cfg(), mock.patch.object(self.engine, "_get_kokoro_voice", return_value=fake_voice):
            self.assertFalse(self.engine._speak_via_kokoro("hi", self.ttsmod.language.HI))

    def test_synthesis_exception_is_caught_and_returns_false(self):
        class _RaisingVoice:
            def create(self, text, voice, lang):
                raise RuntimeError("simulated: onnxruntime inference failure")

        with self._cfg(), mock.patch.object(self.engine, "_get_kokoro_voice", return_value=_RaisingVoice()):
            self.assertFalse(self.engine._speak_via_kokoro("hi", self.ttsmod.language.HI))


class SpeakOfflineFallbackKokoroRoutingTests(_BaseKokoroTestCase):
    """_speak_offline_fallback()'s Tier 13 routing fix: for lang="hi"
    with Kokoro enabled, Kokoro is tried BEFORE Piper (not after) - see
    the method's own docstring for why trying Piper first would silently
    never reach Kokoro at all."""

    def test_hindi_text_tries_kokoro_before_piper_and_short_circuits_on_success(self):
        with self._cfg(), \
             mock.patch.object(self.engine, "_speak_via_kokoro", return_value=True) as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper") as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi") as m_sapi:
            result = self.engine._speak_offline_fallback("नमस्ते, आज मौसम अच्छा है", self.ttsmod.language.HI)
        self.assertTrue(result)
        m_kokoro.assert_called_once()
        m_piper.assert_not_called()
        m_sapi.assert_not_called()

    def test_hindi_text_falls_through_to_piper_when_kokoro_fails(self):
        with self._cfg(), \
             mock.patch.object(self.engine, "_speak_via_kokoro", return_value=False) as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi") as m_sapi:
            result = self.engine._speak_offline_fallback("नमस्ते", self.ttsmod.language.HI)
        self.assertTrue(result)
        m_kokoro.assert_called_once()
        m_piper.assert_called_once()
        m_sapi.assert_not_called()

    def test_hindi_text_reaches_sapi_only_if_both_kokoro_and_piper_fail(self):
        with self._cfg(), \
             mock.patch.object(self.engine, "_speak_via_kokoro", return_value=False), \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=False), \
             mock.patch.object(self.engine, "_speak_via_sapi", return_value=True) as m_sapi:
            result = self.engine._speak_offline_fallback("नमस्ते", self.ttsmod.language.HI)
        self.assertTrue(result)
        m_sapi.assert_called_once()

    def test_hindi_text_skips_kokoro_entirely_when_disabled(self):
        """TTS_KOKORO_ENABLED's real default is False - unchanged,
        Piper/SAPI-only legacy behavior for anyone who hasn't opted in."""
        with self._cfg(TTS_KOKORO_ENABLED=False), \
             mock.patch.object(self.engine, "_speak_via_kokoro") as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True):
            self.engine._speak_offline_fallback("नमस्ते", self.ttsmod.language.HI)
        m_kokoro.assert_not_called()

    def test_english_text_never_tries_kokoro_even_when_enabled(self):
        """Kokoro costs ~2x CPU latency vs Piper (see docs/VOICE_LOCAL.md)
        with no quality benefit for English, so it's deliberately never
        reached outside lang="hi"."""
        with self._cfg(TTS_KOKORO_ENABLED=True), \
             mock.patch.object(self.engine, "_speak_via_kokoro") as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True):
            self.engine._speak_offline_fallback("hello there", self.ttsmod.language.EN)
        m_kokoro.assert_not_called()

    def test_hinglish_text_never_tries_kokoro_either(self):
        with self._cfg(TTS_KOKORO_ENABLED=True), \
             mock.patch.object(self.engine, "_speak_via_kokoro") as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True):
            self.engine._speak_offline_fallback("aaj mausam kaisa hai", self.ttsmod.language.HINGLISH)
        m_kokoro.assert_not_called()

    def test_default_lang_argument_is_english_so_old_callers_are_unaffected(self):
        """No call site in this codebase omits `lang` any more, but the
        default itself is still part of the contract - old single-arg
        test calls elsewhere rely on it staying "en"."""
        with self._cfg(TTS_KOKORO_ENABLED=True), \
             mock.patch.object(self.engine, "_speak_via_kokoro") as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True):
            self.engine._speak_offline_fallback("hello there")
        m_kokoro.assert_not_called()

    def test_piper_and_sapi_always_get_english_prepared_text_even_for_hindi(self):
        """The exact bug this ordering fix exists to avoid: Piper/SAPI
        must never receive Hindi-expanded text, even when lang="hi" and
        Kokoro isn't usable. A number expands to different words per
        language, so this pins the actual text, not just which engine
        was called."""
        with self._cfg(TTS_KOKORO_ENABLED=False), \
             mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper:
            self.engine._speak_offline_fallback("I have 25 things", self.ttsmod.language.HI)
        piper_text = m_piper.call_args[0][0]
        self.assertIn("twenty-five", piper_text)
        self.assertNotIn("पच्चीस", piper_text)

    def test_kokoro_gets_hindi_prepared_text_for_hindi_lang(self):
        with self._cfg(), \
             mock.patch.object(self.engine, "_speak_via_kokoro", return_value=True) as m_kokoro, \
             mock.patch.object(self.engine, "_speak_via_piper") as m_piper:
            self.engine._speak_offline_fallback("I have 25 things", self.ttsmod.language.HI)
        kokoro_text = m_kokoro.call_args[0][0]
        self.assertIn("पच्चीस", kokoro_text)
        m_piper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
