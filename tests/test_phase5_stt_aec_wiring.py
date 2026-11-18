"""Phase 5 - the stt.py side of real AEC: SpeechReceiver._apply_aec().

voice/aec.py's own frame-handling logic is covered in test_phase5_aec.py;
this file only pins down the thin integration contract at the stt.py
boundary: passthrough when AEC is disabled/unavailable, correct
AudioData round-tripping when it's enabled, and fail-safe passthrough on
any error - using minimal fakes for speech_recognition/pyaudio/pygame/
edge_tts (none of which need to be genuinely installed for this).

    python -m unittest discover -s tests -p "test_phase5_*.py"
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)


class _FakeAudioData:
    def __init__(self, raw, rate, width):
        self.raw, self.rate, self.width = raw, rate, width

    def get_raw_data(self, convert_rate=None, convert_width=None):
        return self.raw

    def __eq__(self, other):
        return isinstance(other, _FakeAudioData) and (self.raw, self.rate, self.width) == (
            other.raw, other.rate, other.width,
        )


class _FakeRecognizer:
    def __init__(self):
        self.energy_threshold = 300
        self.pause_threshold = 0.8
        self.non_speaking_duration = 0.3
        self.dynamic_energy_threshold = True

    def adjust_for_ambient_noise(self, source, duration=1):
        pass


class _FakeMicrophone:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fakes(testcase):
    """speech_recognition/pyaudio aren't genuinely needed by these tests'
    own logic (only sr.Recognizer/sr.Microphone/sr.AudioData's *shape*
    matters, which a whole-module stand-in gives cleanly), so those two
    stay full sys.modules swaps - restored to whatever was there before
    (a real module, if installed, or nothing) via testcase.addCleanup
    rather than a blind pop, so a real install elsewhere in the suite
    isn't permanently hidden afterward.

    pygame/edge_tts are different: pygame IS genuinely installed here, and
    swapping its sys.modules entry out for a from-scratch stand-in (even if
    "restored" to the exact prior object afterward) leaves a window where
    some OTHER test file's own module-eviction/reimport dance can reach
    `import pygame` while the swap is live, producing a "partially
    initialized module 'pygame' has no attribute 'color'" circular-import
    AttributeError in a totally unrelated test file. So instead of ever
    touching sys.modules["pygame"]/["edge_tts"], this patches the REAL,
    already-imported modules' mixer/time/sndarray/Communicate attributes
    in place via mock.patch.object (self-restoring through
    testcase.addCleanup) - sys.modules["pygame"] stays the one genuine,
    fully-initialized package object for the whole process, so nothing
    anywhere ever has a reason to re-import or partially-rebuild it.
    """
    for name, real in (("speech_recognition", sys.modules.get("speech_recognition")),
                        ("pyaudio", sys.modules.get("pyaudio"))):
        testcase.addCleanup(
            (lambda n, r: (lambda: sys.modules.__setitem__(n, r) if r is not None else sys.modules.pop(n, None)))(
                name, real
            )
        )

    fake_sr = types.ModuleType("speech_recognition")
    fake_sr.AudioData = _FakeAudioData
    fake_sr.Recognizer = _FakeRecognizer
    fake_sr.Microphone = _FakeMicrophone

    class WaitTimeoutError(Exception):
        pass

    class UnknownValueError(Exception):
        pass

    class RequestError(Exception):
        pass

    fake_sr.WaitTimeoutError = WaitTimeoutError
    fake_sr.UnknownValueError = UnknownValueError
    fake_sr.RequestError = RequestError
    sys.modules["speech_recognition"] = fake_sr

    sys.modules["pyaudio"] = types.ModuleType("pyaudio")

    import pygame  # noqa: PLC0415  (the REAL, installed package)
    import edge_tts  # noqa: PLC0415  (likewise real)

    fake_mixer = types.SimpleNamespace(
        init=lambda **kw: None, get_init=lambda: None, stop=lambda: None,
        music=types.SimpleNamespace(
            load=lambda p: None, set_volume=lambda v: None, play=lambda: None,
            get_busy=lambda: False, stop=lambda: None, unload=lambda: None,
        ),
    )
    fake_time = types.SimpleNamespace(wait=lambda ms: None)
    fake_sndarray = types.SimpleNamespace(array=lambda s: None, make_sound=lambda a: None)
    for attr, fake in (("mixer", fake_mixer), ("time", fake_time), ("sndarray", fake_sndarray)):
        patcher = mock.patch.object(pygame, attr, fake)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    class _FC:
        def __init__(self, *a, **k):
            pass

        async def save(self, path):
            pass

    comm_patcher = mock.patch.object(edge_tts, "Communicate", _FC)
    comm_patcher.start()
    testcase.addCleanup(comm_patcher.stop)

    for extra in ("sounddevice", "aec_audio_processing"):
        real_extra = sys.modules.pop(extra, None)
        if real_extra is not None:
            testcase.addCleanup(sys.modules.__setitem__, extra, real_extra)


class ApplyAECTests(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod  # noqa: PLC0415

        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)

    def test_passthrough_when_aec_disabled(self):
        audio = _FakeAudioData(b"original-bytes", 16000, 2)
        with mock.patch.object(self.sttmod.aec_processor, "enabled", False):
            result = self.receiver._apply_aec(audio)
        self.assertIs(result, audio)

    def test_cleans_audio_and_rewraps_when_enabled(self):
        audio = _FakeAudioData(b"raw-mic-bytes", 48000, 2)
        with mock.patch.object(self.sttmod.aec_processor, "enabled", True), \
             mock.patch.object(self.sttmod.aec_processor, "sample_rate", 16000), \
             mock.patch.object(
                 self.sttmod.aec_processor, "process_near_end", return_value=b"cleaned-bytes"
             ) as m_process:
            result = self.sttmod.sr.AudioData  # sanity the fake class is wired
            self.assertIs(result, _FakeAudioData)
            out = self.receiver._apply_aec(audio)
        m_process.assert_called_once_with(b"raw-mic-bytes")
        self.assertIsInstance(out, _FakeAudioData)
        self.assertEqual(out.raw, b"cleaned-bytes")
        self.assertEqual(out.rate, 16000)

    def test_fails_safe_and_returns_original_audio_on_error(self):
        audio = _FakeAudioData(b"raw-mic-bytes", 16000, 2)
        with mock.patch.object(self.sttmod.aec_processor, "enabled", True), \
             mock.patch.object(
                 self.sttmod.aec_processor, "process_near_end", side_effect=RuntimeError("boom")
             ):
            out = self.receiver._apply_aec(audio)
        self.assertIs(out, audio)


# =============================================================================
# Tier 7: SpeechReceiver._passes_vad() - the voice/buffers.py integration
# point. Same fake-module harness as ApplyAECTests above; this only pins
# down the thin contract at the stt.py boundary (voice/buffers.py's own
# VAD-backend logic is covered by test_voice_buffers.py).
# =============================================================================
class PassesVadTests(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod  # noqa: PLC0415

        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)

    def test_precheck_disabled_always_passes_without_even_asking_the_vad(self):
        audio = _FakeAudioData(b"irrelevant", 16000, 2)
        with mock.patch.object(self.sttmod.config, "VAD_PRECHECK_ENABLED", False), \
             mock.patch.object(self.receiver, "_vad") as m_vad:
            self.assertTrue(self.receiver._passes_vad(audio))
        m_vad.contains_speech.assert_not_called()

    def test_vad_says_speech_present_passes(self):
        audio = _FakeAudioData(b"some-bytes", 16000, 2)
        with mock.patch.object(self.sttmod.config, "VAD_PRECHECK_ENABLED", True), \
             mock.patch.object(self.receiver._vad, "contains_speech", return_value=True) as m:
            self.assertTrue(self.receiver._passes_vad(audio))
        m.assert_called_once_with(b"some-bytes", 16000, 2)

    def test_vad_says_no_speech_fails(self):
        audio = _FakeAudioData(b"silence-bytes", 16000, 2)
        with mock.patch.object(self.sttmod.config, "VAD_PRECHECK_ENABLED", True), \
             mock.patch.object(self.receiver._vad, "contains_speech", return_value=False):
            self.assertFalse(self.receiver._passes_vad(audio))

    def test_vad_raising_fails_safe_to_true(self):
        """A VAD bug must never silence something that might be real
        speech - worst case is one wasted recognition call, same as
        today, never a dropped utterance."""
        audio = _FakeAudioData(b"some-bytes", 16000, 2)
        with mock.patch.object(self.sttmod.config, "VAD_PRECHECK_ENABLED", True), \
             mock.patch.object(self.receiver._vad, "contains_speech", side_effect=RuntimeError("boom")):
            self.assertTrue(self.receiver._passes_vad(audio))


# =============================================================================
# Tier 7: the full listen() wiring - proves _passes_vad() returning False
# actually makes listen() return None (the existing "nothing said" bucket,
# not "" / recognition-failed), and that recognition is never even
# attempted in that case. Extends _FakeRecognizer with a working listen()
# locally (monkeypatched on the instance) since the shared fixture above
# deliberately doesn't stub it - every other test in this file tests
# _apply_aec() directly rather than the full capture loop; this is the one
# place a full-loop proof is worth the extra setup.
# =============================================================================
class ListenVadIntegrationTests(unittest.TestCase):
    def setUp(self):
        _install_fakes(self)
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.stt as sttmod  # noqa: PLC0415

        self.sttmod = sttmod
        self.receiver = sttmod.SpeechReceiver()
        self._scripted_audio = _FakeAudioData(b"captured-bytes", 16000, 2)
        self.receiver.recognizer.listen = lambda source, timeout=1, phrase_time_limit=8: (
            self._scripted_audio
        )
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for mod in ("voice.stt", "voice.tts", "voice.aec", "voice.buffers", "voice.language"):
            sys.modules.pop(mod, None)

    def test_vad_rejected_capture_returns_none_without_attempting_recognition(self):
        # _FakeRecognizer doesn't define recognize_google at all (the
        # shared fixture above never needs it - every other test in this
        # file stops at _apply_aec()) - a plain Mock assigned directly to
        # the instance stands in for it, and existing-only, since
        # mock.patch.object requires the attribute to already exist.
        self.receiver.recognizer.recognize_google = mock.Mock()
        with mock.patch.object(self.receiver, "_passes_vad", return_value=False), \
             mock.patch.object(self.receiver, "_recognize_with_whisper") as m_whisper:
            result = self.receiver.listen()
        self.assertIsNone(result)
        self.receiver.recognizer.recognize_google.assert_not_called()
        m_whisper.assert_not_called()

    def test_vad_accepted_capture_proceeds_to_recognition_as_before(self):
        self.receiver.recognizer.recognize_google = mock.Mock(return_value="hello")
        with mock.patch.object(self.receiver, "_passes_vad", return_value=True), \
             mock.patch.object(self.receiver, "_recognize_with_whisper", return_value=None):
            result = self.receiver.listen()
        self.assertEqual(result, "hello")


if __name__ == "__main__":
    unittest.main()
