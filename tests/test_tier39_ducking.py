"""
tests/test_tier39_ducking.py — Tier 39: other apps down while Wave listens or talks.

SARA lowers other applications' volume while it listens (so the mic hears the
user, not the song) and puts it back after. voice/ducking.py does that for
Wave while it's AWAKE or speaking - against a fake mixer here, never the
real one.

    python -m unittest discover -s tests -p "test_tier39_ducking.py"
"""
import json
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

from settings.config import config  # noqa: E402
from voice import ducking  # noqa: E402


class _Vol:
    def __init__(self, level, muted=False):
        self.level, self.is_muted = level, muted


class _Mixer:
    """Stands in for pycaw: sessions are (key, pid, name, _Vol)."""

    def __init__(self, sessions):
        self.list = sessions

    def sessions(self):
        return list(self.list)

    @staticmethod
    def get(v):
        return v.level

    @staticmethod
    def muted(v):
        return v.is_muted

    @staticmethod
    def set(v, level):
        v.level = level


class DuckingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        p = mock.patch.object(config, "AUDIO_DUCKING", True)
        p.start()
        self.addCleanup(p.stop)
        self.spotify, self.chrome, self.muted = _Vol(0.8), _Vol(0.5), _Vol(0.6, muted=True)
        self.mine = _Vol(1.0)
        self.mixer = _Mixer([("spotify", 101, "Spotify.exe", self.spotify), ("chrome", 102, "chrome.exe", self.chrome),
                             ("zoom", 103, "Zoom.exe", self.muted), ("wave", os.getpid(), "python.exe", self.mine)])
        self.now = [0.0]
        self.speaking = [False]
        self.ducker = self._ducker()

    def _ducker(self):
        return ducking.Ducker(backend=self.mixer, state_path=os.path.join(self.tmp, "audio_ducking.json"),
                              clock=lambda: self.now[0], speaking=lambda: self.speaking[0])

    def test_awake_turns_other_apps_down_but_never_wave_itself(self):
        self.ducker._awake = True
        self.ducker.tick()
        self.assertAlmostEqual(self.spotify.level, 0.24)
        self.assertAlmostEqual(self.chrome.level, 0.15)
        self.assertEqual(self.muted.level, 0.6)          # muted: left alone
        self.assertEqual(self.mine.level, 1.0)           # Wave's own process

    def test_volumes_come_back_a_moment_after_wave_is_idle(self):
        self.ducker._awake = True
        self.ducker.tick()
        self.ducker._awake = False
        self.ducker.tick()
        self.assertAlmostEqual(self.spotify.level, 0.24)  # not straight away (no pumping between sentences)
        self.now[0] += ducking.RELEASE_S + 0.1
        self.ducker.tick()
        self.assertEqual((self.spotify.level, self.chrome.level), (0.8, 0.5))

    def test_wave_speaking_ducks_too(self):
        self.speaking[0] = True
        self.ducker.tick()
        self.assertAlmostEqual(self.spotify.level, 0.24)

    def test_a_volume_the_user_changed_meanwhile_is_left_alone(self):
        self.ducker._awake = True
        self.ducker.tick()
        self.spotify.level = 0.9                          # the user turned Spotify up
        self.ducker.restore()
        self.assertEqual(self.spotify.level, 0.9)
        self.assertEqual(self.chrome.level, 0.5)

    def test_an_app_that_starts_playing_mid_conversation_is_turned_down_too(self):
        self.ducker._awake = True
        self.ducker.tick()
        vlc = _Vol(1.0)
        self.mixer.list.append(("vlc", 104, "vlc.exe", vlc))
        self.ducker.tick()
        self.assertAlmostEqual(vlc.level, 0.3)
        self.assertAlmostEqual(self.spotify.level, 0.24)  # not turned down twice

    def test_volumes_a_crashed_run_left_down_come_back_at_the_next_start(self):
        self.ducker._awake = True
        self.ducker.tick()                                # ... and Wave crashes here
        with open(self.ducker.state_path(), encoding="utf-8") as f:
            self.assertIn("spotify", json.load(f))
        self._ducker().restore_leftovers()
        self.assertEqual((self.spotify.level, self.chrome.level), (0.8, 0.5))

    def test_off_means_off(self):
        with mock.patch.object(config, "AUDIO_DUCKING", False):
            self.ducker._awake = True
            self.ducker.tick()
        self.assertEqual(self.spotify.level, 0.8)

    def test_the_level_setting(self):
        with mock.patch.object(config, "AUDIO_DUCK_LEVEL", 0.5):
            self.ducker._awake = True
            self.ducker.tick()
        self.assertAlmostEqual(self.spotify.level, 0.4)

    def test_the_mic_workers_report_awake_and_stop(self):
        with mock.patch.object(ducking.ducker, "set_awake") as set_awake:
            from webui.voice_worker import WebContinuousMicWorker  # noqa: PLC0415
            worker = WebContinuousMicWorker.__new__(WebContinuousMicWorker)
            worker._on_mode_changed = lambda mode, announce: None
            worker._sync_captions = lambda: None
            worker.set_mode("AWAKE")
            worker.set_mode("SLEEPING")
            worker.stop()
        self.assertEqual([c.args[0] for c in set_awake.call_args_list], [True, False, False])


if __name__ == "__main__":
    unittest.main()
