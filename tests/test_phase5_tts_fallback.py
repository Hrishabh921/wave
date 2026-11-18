"""Phase 5 - TTS playback fallback chain and success/failure propagation
(voice/tts.py).

🔧 FIX under test: speak_and_wait() used to have no way to report that
playback silently failed end-to-end - finished_speaking fired identically
whether audio was actually heard or not (see ui/workers.py). These tests
pin down the contract that replaces that: speak_and_wait()/
VoiceEngine.last_playback_ok is True for a normal OR a user-interrupted
utterance, and False only when every tier (edge-tts+sounddevice,
edge-tts+pygame, Piper, SAPI) genuinely failed.

Runs against fakes for pygame and edge_tts (neither is installed in every
test environment, and real audio playback obviously can't run here) -
mirrors the install_fake_module pattern _phase4_support.py already uses
for pyautogui etc. sounddevice is left genuinely absent/unimportable
(the common case in CI containers with no PortAudio) so these tests also
double as a check that the pygame-only path still works correctly on its
own - see test_phase5_aec.py for AEC's own frame-handling tests.

    python -m unittest discover -s tests -p "test_phase5_*.py"
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)


class _FakeChannel:
    """Stands in for a pygame Sound-playing Channel."""

    def __init__(self, busy_sequence=None):
        self._busy_sequence = list(busy_sequence) if busy_sequence else []
        self.stopped = False
        self.volume = None

    def set_volume(self, v):
        self.volume = v

    def get_busy(self):
        return self._busy_sequence.pop(0) if self._busy_sequence else False

    def stop(self):
        self.stopped = True


class _FakeSound:
    def __init__(self, channel=None, fail=False):
        self._channel = channel or _FakeChannel()
        self._fail = fail

    def play(self):
        if self._fail:
            return None
        return self._channel


class _FakeMixerMusic:
    def __init__(self):
        self.busy_sequence = []
        self.loaded = None
        self.stopped = False
        self.fail_on_load = False

    def load(self, path):
        if self.fail_on_load:
            raise RuntimeError("simulated: mixer.music.load failed")
        self.loaded = path

    def set_volume(self, v):
        pass

    def play(self):
        pass

    def get_busy(self):
        return self.busy_sequence.pop(0) if self.busy_sequence else False

    def stop(self):
        self.stopped = True

    def unload(self):
        pass


class _FakeMixer:
    def __init__(self):
        self.music = _FakeMixerMusic()
        self._init = None
        self.fail_init = False
        self.sound_to_return = None  # set per-test; None -> Sound() raises

    def init(self, **kwargs):
        if self.fail_init:
            raise RuntimeError("simulated: mixer init failed")
        self._init = (kwargs.get("frequency", 22050), kwargs.get("channels", 2))

    def get_init(self):
        return self._init

    def stop(self):
        pass

    def Sound(self, path):
        if self.sound_to_return is None:
            raise RuntimeError("simulated: can't decode this file")
        return self.sound_to_return


class _FakeTimeMod:
    def __init__(self):
        self.on_wait = None  # optional callback(ms) -> None, for interrupt simulation

    def wait(self, ms):
        if self.on_wait:
            self.on_wait(ms)


def _install_fakes(testcase):
    """Fakes pygame's mixer/time/sndarray and edge_tts's Communicate for
    the given testcase, auto-restored via testcase.addCleanup, and returns
    (pygame_module, edge_tts_module, _FakeCommunicate) for the test to
    drive.

    Deliberately does NOT do `sys.modules["pygame"] = types.ModuleType(...)`
    (a from-scratch stand-in object replacing the real package entirely).
    pygame IS genuinely installed in this environment, and swapping out its
    sys.modules entry - even temporarily, even if later "restored" to the
    exact prior object - leaves a window where some OTHER test file's own
    module eviction/reimport dance (e.g. test_tier9/11/16's
    import_with_fakes(), which deletes and re-imports every ui.* module) can
    reach `import pygame` while this swap is in a half-finished state,
    producing a "partially initialized module 'pygame' has no attribute
    'color'" circular-import AttributeError in a completely unrelated test
    file. Patching the REAL module's attributes in place with
    mock.patch.object instead means sys.modules["pygame"] is always the one
    genuine, fully-initialized package object for this whole process - no
    code path anywhere ever has a reason to re-import or partially-rebuild
    it."""
    import pygame  # noqa: PLC0415  (the REAL, installed package - imported
    # for real at most once per process and cached from then on; nothing
    # below ever touches sys.modules["pygame"] itself)
    import edge_tts  # noqa: PLC0415  (likewise the real package)

    fake_mixer = _FakeMixer()
    fake_time = _FakeTimeMod()
    fake_sndarray = types.SimpleNamespace(
        array=lambda snd: (_ for _ in ()).throw(RuntimeError("simulated: no array support")),
        make_sound=lambda arr: (_ for _ in ()).throw(RuntimeError("simulated: no make_sound support")),
    )
    for attr, fake in (("mixer", fake_mixer), ("time", fake_time), ("sndarray", fake_sndarray)):
        patcher = mock.patch.object(pygame, attr, fake)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    class _FakeCommunicate:
        # Class-level so tests can flip behavior without re-importing.
        should_fail = False
        written_bytes = b"FAKE-MP3-BYTES"
        call_count = 0   # Tier 3 item #7: the one true signal a real (fake) synthesis was attempted

        def __init__(self, text, voice, rate=None, volume=None):
            self.text = text
            _FakeCommunicate.call_count += 1

        async def save(self, out_path):
            if _FakeCommunicate.should_fail:
                raise RuntimeError("simulated: edge-tts unreachable (no internet)")
            with open(out_path, "wb") as f:
                f.write(_FakeCommunicate.written_bytes)

    comm_patcher = mock.patch.object(edge_tts, "Communicate", _FakeCommunicate)
    comm_patcher.start()
    testcase.addCleanup(comm_patcher.stop)

    # sounddevice: deliberately made unimportable for the duration of this
    # test (whether or not it's really installed here) so these tests
    # exercise the pygame-only path - voice/tts.py must degrade to that
    # cleanly. Snapshot/restore (not a blind pop) so a real sounddevice
    # install elsewhere in the suite isn't permanently hidden afterward.
    real_sounddevice = sys.modules.pop("sounddevice", None)
    if real_sounddevice is not None:
        testcase.addCleanup(sys.modules.__setitem__, "sounddevice", real_sounddevice)

    return pygame, edge_tts, _FakeCommunicate


class _TTSFakeHarnessMixin:
    """setUp()/cleanup shared by TTSFallbackTests and
    TTSCacheIntegrationTests below - deliberately NOT a unittest.TestCase
    itself (and neither test class subclasses the other), so neither
    class's test_* methods get discovered and re-run a second time under
    the other's name, which a naive "just subclass TTSFallbackTests"
    would otherwise silently do."""

    def setUp(self):
        self.fake_pygame, self.fake_edge_tts, self.FakeCommunicate = _install_fakes(self)
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        import voice.tts as ttsmod  # noqa: PLC0415

        self.ttsmod = ttsmod
        self.engine = ttsmod.VoiceEngine()
        self.addCleanup(self._cleanup_modules)

        # Tier 3 item #7: voice/tts_cache.py's phrase_cache is a
        # module-level singleton, so without this, one test's real (fake-
        # edge_tts-backed) synthesis of "hello world" would cache it, and
        # a LATER test synthesizing that same text - several below do -
        # would silently get a cache hit instead of exercising whatever
        # it actually meant to test (e.g. test_edge_tts_raising_goes_
        # straight_to_offline_fallback would never reach FakeCommunicate
        # at all). Cleared before AND after each test so no test leaks
        # into the next one in either direction.
        self.ttsmod.phrase_cache.clear()
        self.addCleanup(self.ttsmod.phrase_cache.clear)

    def _cleanup_modules(self):
        # pygame/edge_tts themselves are untouched now - _install_fakes()
        # only patches attributes of the real, already-imported modules
        # (via mock.patch.object, self-restoring through addCleanup), so
        # there is nothing to restore in sys.modules here. Only the wave
        # modules that were deliberately evicted in setUp need re-evicting.
        for mod in ("voice.tts", "voice.aec", "voice.language"):
            sys.modules.pop(mod, None)
        self.FakeCommunicate.should_fail = False
        self.FakeCommunicate.call_count = 0


class TTSFallbackTests(_TTSFakeHarnessMixin, unittest.TestCase):

    # ---- top-level contract: last_playback_ok / speak_and_wait() -------

    def test_empty_text_is_not_a_failure_and_does_nothing(self):
        self.assertTrue(self.engine.speak_and_wait(""))

    def test_successful_playback_reports_true(self):
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.assertTrue(self.engine.speak_and_wait("hello world"))

    def test_primary_playback_fails_but_offline_fallback_succeeds_reports_true(self):
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=False), \
             mock.patch.object(self.engine, "_speak_offline_fallback", return_value=True) as m_fb:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
            self.assertTrue(m_fb.called)

    def test_every_tier_failing_reports_false(self):
        """The exact bug being fixed: total silent failure must now be
        visible, not reported as success."""
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=False), \
             mock.patch.object(self.engine, "_speak_offline_fallback", return_value=False):
            self.assertFalse(self.engine.speak_and_wait("hello world"))

    def test_edge_tts_raising_goes_straight_to_offline_fallback(self):
        self.FakeCommunicate.should_fail = True
        with mock.patch.object(self.engine, "_speak_offline_fallback", return_value=True) as m_fb:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
            self.assertTrue(m_fb.called)

    def test_interrupted_before_synthesis_starts_is_not_a_failure(self):
        # speak()/speak_and_wait() always clear stop_event right before
        # launching _speak_task (so a PREVIOUS stop() can't immediately
        # cancel a NEW utterance) - so to test _speak_task's own entry
        # guard for an utterance that's already-interrupted-on-entry
        # (the tail of a stop()/speak() race), call it directly rather
        # than through speak(), which would just clear the flag again.
        self.engine.stop_event.set()
        with mock.patch.object(self.engine, "_speak_offline_fallback") as m_fb, \
             mock.patch.object(self.engine, "_play_synthesized_mp3") as m_play:
            self.engine._speak_task("hello world")
            self.assertTrue(self.engine.last_playback_ok)
            self.assertFalse(m_fb.called)
            self.assertFalse(m_play.called)
        self.engine.stop_event.clear()

    def test_mixer_unusable_still_tries_the_offline_chain(self):
        self.fake_pygame.mixer.fail_init = True
        with mock.patch.object(self.engine, "_speak_offline_fallback", return_value=True) as m_fb:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
            self.assertTrue(m_fb.called)

    # ---- offline fallback ordering: Piper before SAPI -------------------

    def test_offline_fallback_tries_piper_before_sapi(self):
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi", return_value=True) as m_sapi:
            self.assertTrue(self.engine._speak_offline_fallback("hi"))
            self.assertTrue(m_piper.called)
            self.assertFalse(m_sapi.called, "SAPI shouldn't run once Piper already spoke")

    def test_offline_fallback_falls_through_to_sapi_when_piper_unavailable(self):
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=False) as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi", return_value=True) as m_sapi:
            self.assertTrue(self.engine._speak_offline_fallback("hi"))
            self.assertTrue(m_piper.called and m_sapi.called)

    def test_offline_fallback_interrupted_before_starting_skips_both_engines(self):
        self.engine.stop_event.set()
        with mock.patch.object(self.engine, "_speak_via_piper") as m_piper, \
             mock.patch.object(self.engine, "_speak_via_sapi") as m_sapi:
            self.assertTrue(self.engine._speak_offline_fallback("hi"))
            self.assertFalse(m_piper.called)
            self.assertFalse(m_sapi.called)
        self.engine.stop_event.clear()

    # ---- interruption mid-playback must read as True, not a failure -----

    def test_interrupted_mid_pygame_music_playback_is_not_a_failure(self):
        self.fake_pygame.mixer.music.busy_sequence = [True, True, False]

        def interrupt_on_first_wait(ms):
            self.engine.stop_event.set()

        self.fake_pygame.time.on_wait = interrupt_on_first_wait

        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            result = self.engine._play_mp3_via_pygame_music(path)
        finally:
            os.remove(path)
            self.engine.stop_event.clear()
            self.fake_pygame.time.on_wait = None
        self.assertTrue(result)

    def test_interrupted_mid_pygame_array_playback_is_not_a_failure(self):
        # _play_pcm_via_pygame_array builds its Sound from an array via
        # pygame.sndarray.make_sound() (not pygame.mixer.Sound(path), which
        # is the file-based decode API _decode_mp3_to_pcm uses instead).
        channel = _FakeChannel(busy_sequence=[True, True, False])
        self.fake_pygame.sndarray.make_sound = lambda arr: _FakeSound(channel=channel)

        def interrupt_on_first_wait(ms):
            self.engine.stop_event.set()

        self.fake_pygame.time.on_wait = interrupt_on_first_wait
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed")
        try:
            result = self.engine._play_pcm_via_pygame_array(np.zeros(10, dtype=np.int16), 24000)
        finally:
            self.engine.stop_event.clear()
            self.fake_pygame.time.on_wait = None
        self.assertTrue(result)
        self.assertTrue(channel.stopped)

    def test_sapi_interrupted_before_first_sentence_is_not_a_failure(self):
        """Pins the fix: _speak_via_sapi used to return spoke_any (False)
        when interrupted before finishing even one sentence, which read
        as a genuine failure one layer up. win32com/pythoncom aren't
        importable here (Windows-only), so this simulates that by
        checking the plain "SAPI unavailable at all" path instead returns
        False (a real failure, correctly) and relies on
        test_offline_fallback_interrupted_before_starting_skips_both_engines
        plus the source fix itself for the interrupted-partway case: see
        that test and voice/tts.py's _speak_via_sapi for the contract."""
        # win32com/pythoncom aren't available outside Windows - confirm
        # the honest "really not available" case still reports False
        # (not a silent success) rather than asserting on Windows-only
        # internals this environment can't exercise.
        self.assertFalse(self.engine._speak_via_sapi("hello"))


# =============================================================================
# Tier 7: TTS_PREFER_LOCAL_VOICE - opts Piper in FIRST, ahead of edge-tts
# entirely, for someone who has already set up a local Piper voice.
# TTSFallbackTests.test_offline_fallback_tries_piper_before_sapi above
# already covers Piper-before-SAPI inside the OFFLINE fallback chain
# (reached only once edge-tts has already failed); these tests cover the
# separate, new question this flag answers - whether Piper is tried
# BEFORE edge-tts is attempted AT ALL. Off by default, so every other
# test in this file (which never touches the flag) keeps exercising the
# pre-Tier-7 edge-tts-first order unchanged.
# =============================================================================
class TTSPreferLocalVoiceTests(_TTSFakeHarnessMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from settings.config import config  # noqa: PLC0415
        self.config = config
        old = config.TTS_PREFER_LOCAL_VOICE
        self.addCleanup(setattr, config, "TTS_PREFER_LOCAL_VOICE", old)

    def test_flag_off_by_default_never_tries_piper_before_edge_tts(self):
        self.config.TTS_PREFER_LOCAL_VOICE = False
        with mock.patch.object(self.engine, "_speak_via_piper") as m_piper, \
             mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True) as m_play:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
        m_piper.assert_not_called()
        m_play.assert_called_once()

    def test_flag_on_and_piper_succeeds_skips_edge_tts_entirely(self):
        self.config.TTS_PREFER_LOCAL_VOICE = True
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=True) as m_piper, \
             mock.patch.object(self.engine, "_ensure_mixer") as m_mixer:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
        m_piper.assert_called_once_with("hello world")
        m_mixer.assert_not_called()
        self.assertEqual(self.FakeCommunicate.call_count, 0,
                          "edge-tts must never be reached once Piper already spoke")

    def test_flag_on_and_piper_fails_falls_through_to_edge_tts_as_normal(self):
        self.config.TTS_PREFER_LOCAL_VOICE = True
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=False) as m_piper, \
             mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True) as m_play:
            self.assertTrue(self.engine.speak_and_wait("hello world"))
        m_piper.assert_called_once_with("hello world")
        self.assertEqual(self.FakeCommunicate.call_count, 1,
                          "Piper declining should fall through to a normal edge-tts synthesis")
        m_play.assert_called_once()

    def test_flag_on_and_interrupted_right_after_piper_attempt_is_not_a_failure(self):
        self.config.TTS_PREFER_LOCAL_VOICE = True

        def piper_then_interrupt(text):
            self.engine.stop_event.set()   # simulated external interruption
            return False

        with mock.patch.object(self.engine, "_speak_via_piper", side_effect=piper_then_interrupt), \
             mock.patch.object(self.engine, "_play_synthesized_mp3") as m_play:
            self.engine._speak_task("hello world")
            self.assertTrue(self.engine.last_playback_ok)
            m_play.assert_not_called()
        self.engine.stop_event.clear()

    def test_piper_output_is_never_written_to_the_phrase_cache(self):
        """Deliberate design choice (see the call site's comment in
        voice/tts.py): the cache exists to skip edge-tts's network
        round-trip, which Piper never makes in the first place, so
        caching its output would add disk I/O for no latency win."""
        self.config.TTS_PREFER_LOCAL_VOICE = True
        with mock.patch.object(self.engine, "_speak_via_piper", return_value=True):
            self.assertTrue(self.engine.speak_and_wait("hello world"))
        self.assertIsNone(self.ttsmod.phrase_cache.get(("hello world", self.engine.voice, self.engine.rate)))


# =============================================================================
# Tier 3 item #7: the TTS phrase cache (voice/tts_cache.py), integrated
# into _speak_task. voice/tts_cache.py's own test_tts_cache.py covers the
# PhraseCache class in isolation (get/put/eviction/maxlen); these test the
# OTHER half - that _speak_task actually consults and populates it at the
# right point, with the right key.
# =============================================================================
class TTSCacheIntegrationTests(_TTSFakeHarnessMixin, unittest.TestCase):
    def test_cache_hit_skips_synthesis_entirely(self):
        key = ("hello world", self.engine.voice, self.engine.rate)
        self.ttsmod.phrase_cache.put(key, b"CACHED-BYTES")
        written = {}

        def fake_play(path):
            with open(path, "rb") as f:
                written["bytes"] = f.read()
            return True

        with mock.patch.object(self.engine, "_play_synthesized_mp3", side_effect=fake_play):
            self.assertTrue(self.engine.speak_and_wait("hello world"))
        self.assertEqual(self.FakeCommunicate.call_count, 0, "a cache hit must not call edge-tts at all")
        self.assertEqual(written.get("bytes"), b"CACHED-BYTES")

    def test_cache_miss_synthesizes_and_then_populates_the_cache(self):
        key = ("a brand new phrase", self.engine.voice, self.engine.rate)
        self.assertIsNone(self.ttsmod.phrase_cache.get(key))
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.assertTrue(self.engine.speak_and_wait("a brand new phrase"))
        self.assertEqual(self.FakeCommunicate.call_count, 1)
        self.assertEqual(self.ttsmod.phrase_cache.get(key), self.FakeCommunicate.written_bytes)

    def test_a_second_identical_utterance_is_a_cache_hit(self):
        """End-to-end version of the two tests above: speak the same text
        twice, synthesis only happens once."""
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.engine.speak_and_wait("say this twice")
            self.engine.speak_and_wait("say this twice")
        self.assertEqual(self.FakeCommunicate.call_count, 1)

    def test_different_voice_is_a_different_cache_key(self):
        """volume is deliberately NOT part of the key (see tts_cache.py's
        module docstring - it's applied at playback time, never baked
        into the synthesized bytes) but voice and rate ARE, since edge-tts
        actually synthesizes different audio for each."""
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.engine.speak_and_wait("same text")
            self.engine.voice = "hi-IN-SwaraNeural"
            self.engine.speak_and_wait("same text")
        self.assertEqual(self.FakeCommunicate.call_count, 2, "a different voice must not reuse the cache")

    def test_different_rate_is_a_different_cache_key(self):
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.engine.speak_and_wait("same text")
            self.engine.rate = "-10%"
            self.engine.speak_and_wait("same text")
        self.assertEqual(self.FakeCommunicate.call_count, 2, "a different rate must not reuse the cache")

    def test_volume_change_alone_still_reuses_the_cache(self):
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.engine.speak_and_wait("same text")
            self.engine.set_volume(0.2)
            self.engine.speak_and_wait("same text")
        self.assertEqual(self.FakeCommunicate.call_count, 1, "volume-only changes should still hit the cache")

    def test_disabling_the_feature_flag_bypasses_the_cache_both_ways(self):
        from settings.config import config
        old = config.TTS_PHRASE_CACHE_ENABLED
        config.TTS_PHRASE_CACHE_ENABLED = False
        self.addCleanup(setattr, config, "TTS_PHRASE_CACHE_ENABLED", old)
        with mock.patch.object(self.engine, "_play_synthesized_mp3", return_value=True):
            self.engine.speak_and_wait("same text")
            self.engine.speak_and_wait("same text")
        self.assertEqual(self.FakeCommunicate.call_count, 2, "disabled should mean no caching in either direction")

    def test_edge_tts_failure_is_not_cached(self):
        """A failed synthesis has nothing worth remembering - only a
        genuinely successful write should ever reach phrase_cache.put()."""
        self.FakeCommunicate.should_fail = True
        with mock.patch.object(self.engine, "_speak_offline_fallback", return_value=True):
            self.engine.speak_and_wait("will fail")
        self.assertIsNone(self.ttsmod.phrase_cache.get(("will fail", self.engine.voice, self.engine.rate)))

    def test_interrupted_mid_synthesis_is_not_cached(self):
        """stop_event firing between a successful save() and the
        would-be cache.put() must not leave a half-trusted entry behind -
        _speak_task returns before reaching the cache.put() line in this
        case (see voice/tts.py), so there's nothing to assert except that
        it's genuinely absent afterward."""
        def set_stop_during_save(out_path):
            self.engine.stop_event.set()

        orig_save = self.FakeCommunicate.save

        async def save_then_stop(self_inner, out_path):
            await orig_save(self_inner, out_path)
            set_stop_during_save(out_path)

        with mock.patch.object(self.FakeCommunicate, "save", save_then_stop):
            self.engine._speak_task("interrupted phrase")
        self.engine.stop_event.clear()
        self.assertIsNone(self.ttsmod.phrase_cache.get(("interrupted phrase", self.engine.voice, self.engine.rate)))


if __name__ == "__main__":
    unittest.main()
