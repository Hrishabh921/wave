"""Tier 49 - everything the PC plays as the echo reference (voice/system_reference.py, voice/aec.py).

ExternalReferenceTests: the canceller takes the PC's own sound as its reference only while Wave is
quiet, and delays the live mic while it does (a fake WebRTC processor records what it gets).
LoopbackTests: the loopback device is picked and read with a fake `soundcard` module.
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402,F401

from settings.config import config  # noqa: E402
from voice import aec as aec_mod  # noqa: E402
from voice import system_reference as sr_mod  # noqa: E402


class _FakeAP:
    def __init__(self):
        self.reverse, self.near = [], []

    def process_reverse_stream(self, frame):
        self.reverse.append(frame)

    def process_stream(self, frame):
        self.near.append(frame)
        return frame


def _tone(n, amp=3000):
    return (np.sin(np.arange(n) / 5.0) * amp).astype(np.int16)


def _processor():
    p = aec_mod.AECProcessor.__new__(aec_mod.AECProcessor)
    aec_mod.AECProcessor.__init__(p)
    p.enabled = True
    p._ap = _FakeAP()
    p._reverse_method_name = "process_reverse_stream"
    return p


@unittest.skipUnless(aec_mod.HAS_NUMPY, "numpy is needed")
class ExternalReferenceTests(unittest.TestCase):
    def setUp(self):
        self.p = _processor()
        self.clock = [1000.0]
        patcher = mock.patch.object(aec_mod.time, "monotonic", side_effect=lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_pcs_sound_is_the_reference_while_wave_is_quiet(self):
        self.p.feed_external_reference(_tone(480))            # 30 ms of a video playing
        self.assertEqual(len(self.p._ap.reverse), 3)            # three 10 ms frames
        self.p.feed_far_end(np.zeros(160, np.int16), 16000)    # Wave's silent output: not a second timeline
        self.assertEqual(len(self.p._ap.reverse), 3)

    def test_wave_talking_is_the_reference_then(self):
        self.p.feed_external_reference(_tone(480))
        self.p.feed_far_end(_tone(160), 16000)                 # Wave speaks
        n = len(self.p._ap.reverse)
        self.p.feed_external_reference(_tone(480))             # the mix carries Wave's voice too: skipped
        self.assertEqual(len(self.p._ap.reverse), n)
        self.clock[0] += 0.5
        self.p.feed_external_reference(_tone(480))
        self.assertEqual(len(self.p._ap.reverse), n + 3)

    def test_silence_from_the_pc_changes_nothing(self):
        self.p.feed_external_reference(np.zeros(480, np.int16))
        self.assertEqual(self.p._ap.reverse, [])
        self.p.feed_far_end(np.zeros(160, np.int16), 16000)    # as before Tier 49
        self.assertEqual(len(self.p._ap.reverse), 1)

    def test_the_live_mic_is_delayed_while_the_pc_plays(self):
        frame = lambda v: bytes([v]) * 320                       # noqa: E731 - one 10 ms frame
        self.assertEqual(self.p.process_near_end(frame(1), live=True), frame(1))
        self.p.feed_external_reference(_tone(480))
        out = [self.p.process_near_end(frame(i), live=True) for i in range(2, 12)]
        self.assertEqual(out[:8], [b"\x00" * 320] * 8)          # 80 ms of delay engaged
        self.assertEqual(out[8:], [frame(2), frame(3)])
        self.assertEqual(self.p.delay_engagements, 1)
        # a whole captured utterance (the legacy path) is never shifted
        self.assertEqual(self.p.process_near_end(frame(7) * 3), frame(7) * 3)
        self.clock[0] += 2.0                                     # the video stopped
        self.assertEqual(self.p.process_near_end(frame(9), live=True), frame(9))


class _Mic:
    def __init__(self, name, loop=True):
        self.name, self.isloopback = name, loop
        self.reads = 0

    def recorder(self, samplerate, channels, blocksize):
        mic = self

        class _Rec:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def record(self, numframes):
                mic.reads += 1
                if mic.reads > 3:
                    raise RuntimeError("device gone")
                return np.full((numframes, 1), 0.25, np.float32)
        return _Rec()


class _SoundCard:
    def __init__(self, mics, default):
        self._mics, self._default = mics, default

    def all_microphones(self, include_loopback=False):
        return self._mics

    def default_speaker(self):
        return types.SimpleNamespace(name=self._default)


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        saved = config.AEC_SYSTEM_REFERENCE
        config.AEC_SYSTEM_REFERENCE = True
        self.addCleanup(setattr, config, "AEC_SYSTEM_REFERENCE", saved)

    def test_the_speakers_in_use_and_never_a_virtual_enhancer(self):
        mics = [_Mic("Microphone (Realtek)", loop=False), _Mic("FxSound Speakers"), _Mic("Speakers (Realtek)")]
        self.assertEqual(sr_mod.pick_loopback(_SoundCard(mics, "Speakers (Realtek)")).name, "Speakers (Realtek)")
        self.assertEqual(sr_mod.pick_loopback(_SoundCard(mics, "FxSound Speakers")).name, "Speakers (Realtek)")
        self.assertEqual(sr_mod.pick_loopback(_SoundCard(mics, "x"), hint="fxsound").name, "FxSound Speakers")
        self.assertIsNone(sr_mod.pick_loopback(_SoundCard([mics[0]], "x")))

    def test_blocks_go_to_the_canceller(self):
        aec = mock.Mock(enabled=True)
        mic = _Mic("Speakers (Realtek)")
        ref = sr_mod.SystemAudioReference(aec, loader=lambda: _SoundCard([mic], "Speakers (Realtek)"))
        self.assertTrue(ref.start())
        deadline = time.monotonic() + 3
        while aec.feed_external_reference.call_count < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        ref.stop()
        self.assertGreaterEqual(aec.feed_external_reference.call_count, 3)
        pcm = aec.feed_external_reference.call_args_list[0].args[0]
        self.assertEqual((pcm.dtype, len(pcm), int(pcm[0])), (np.int16, sr_mod.BLOCK, 8191))
        self.assertEqual(ref.device_name, "Speakers (Realtek)")

    def test_without_soundcard_or_switched_off_nothing_runs(self):
        aec = mock.Mock(enabled=True)

        def missing():
            raise ImportError("No module named 'soundcard'")
        self.assertFalse(sr_mod.SystemAudioReference(aec, loader=missing).start())
        config.AEC_SYSTEM_REFERENCE = False
        self.assertFalse(sr_mod.SystemAudioReference(aec, loader=lambda: _SoundCard([], "")).start())


if __name__ == "__main__":
    unittest.main()
