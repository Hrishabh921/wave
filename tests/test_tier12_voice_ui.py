"""
Tier 12 - the capture pipeline as the rest of Wave sees it:

  * voice/commands.py decide(): loud, clear speech over Wave's reply
    interrupts even without "Wave," - but only two words or more, never a
    filler, never while asleep.
  * ui/workers.py ContinuousMicWorker: when that counts as "loud" (SNR over
    BARGE_IN_SNR_DB AND real echo cancellation running), live captions only
    while AWAKE, utterance_done after each listen(), the microphone released
    when the worker stops.
  * ui/main_window.py: the live caption bubble.

Run with:  python -m pytest tests/test_tier12_voice_ui.py
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6.QtWidgets  # noqa: F401
    REAL_QT = not getattr(sys.modules.get("PyQt6"), "_wave_stub", False)
except Exception:
    REAL_QT = False

from voice import commands as vc  # noqa: E402

WAKE = ("hey wave", "hello wave")


class LoudBargeInDecisionTests(unittest.TestCase):
    def test_loud_unaddressed_speech_interrupts_and_becomes_the_command(self):
        d = vc.decide("what about tomorrow", "AWAKE", WAKE, tts_active=True, loud_barge_in=True)
        self.assertEqual((d.action, d.text, d.interrupt), (vc.COMMAND, "what about tomorrow", True))

    def test_without_the_loud_flag_the_tier11_gate_still_drops_it(self):
        d = vc.decide("what about tomorrow", "AWAKE", WAKE, tts_active=True)
        self.assertEqual((d.action, d.interrupt), (vc.IGNORE, False))

    def test_a_single_loud_word_or_a_filler_does_not_interrupt(self):
        for text in ("what", "umm", "hmm okay"):
            d = vc.decide(text, "AWAKE", WAKE, tts_active=True, loud_barge_in=True)
            self.assertEqual(d.action, vc.IGNORE, text)

    def test_asleep_nothing_changes(self):
        d = vc.decide("what about tomorrow", "SLEEPING", WAKE, tts_active=True, loud_barge_in=True)
        self.assertEqual(d.action, vc.IGNORE)

    def test_when_wave_is_quiet_the_flag_is_irrelevant(self):
        d = vc.decide("what about tomorrow", "AWAKE", WAKE, tts_active=False, loud_barge_in=True)
        self.assertEqual((d.action, d.interrupt), (vc.COMMAND, False))

    def test_stop_commands_still_win(self):
        d = vc.decide("stop", "AWAKE", WAKE, tts_active=True, loud_barge_in=True)
        self.assertEqual(d.action, vc.STOP)


def _install_ui_fakes():
    S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                          hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                          typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
    S.install_fake_module("pywhatkit")


class _T(str):
    """A stand-in for voice/stt.py's Transcript."""

    def __new__(cls, text, **attrs):
        obj = super().__new__(cls, text)
        obj.__dict__.update(attrs)
        return obj


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MicWorkerTier12Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        import test_tier5_ui_logic as T5
        _install_ui_fakes()
        P4.install_stub()
        cls.workers = T5.import_with_fakes("ui.workers")

    def setUp(self):
        from telemetry import ttfa
        p = mock.patch.object(ttfa, "tracker", ttfa.TTFATracker(persist=False))
        p.start()
        self.addCleanup(p.stop)
        self.worker = self.workers.ContinuousMicWorker(wake_phrases=["hey wave"])
        self.worker.wake_gate = None
        self.texts, self.interrupts = [], []
        self.worker.text_ready.connect(self.texts.append)
        self.worker.interruption_signal.connect(lambda: self.interrupts.append(1))

    def aec(self, enabled):
        return mock.patch.object(self.workers, "aec_processor", types.SimpleNamespace(enabled=enabled))

    def speaking(self, flag):
        return mock.patch.multiple(self.workers.tts_engine, _utterance_active=flag, _stream_active=False)

    def test_loud_speech_with_real_aec_interrupts_wave(self):
        self.worker.mode = "AWAKE"
        with self.aec(True), self.speaking(True), \
                mock.patch.object(self.workers.config, "BARGE_IN_SNR_DB", 18.0, create=True):
            self.worker.handle_text(_T("what about tomorrow", snr_db=26.0, tts_overlap=True))
        self.assertEqual((self.interrupts, self.texts), ([1], ["what about tomorrow"]))

    def test_without_real_echo_cancellation_loudness_proves_nothing(self):
        self.worker.mode = "AWAKE"
        with self.aec(False), self.speaking(True), \
                mock.patch.object(self.workers.config, "BARGE_IN_SNR_DB", 18.0, create=True):
            self.worker.handle_text(_T("what about tomorrow", snr_db=40.0, tts_overlap=True))
        self.assertEqual((self.interrupts, self.texts), ([], []))

    def test_quiet_speech_and_the_off_switch(self):
        with self.aec(True):
            with mock.patch.object(self.workers.config, "BARGE_IN_SNR_DB", 18.0, create=True):
                self.assertFalse(self.worker._loud_barge_in(_T("x", snr_db=12.0)))
                self.assertFalse(self.worker._loud_barge_in(_T("x")))          # legacy path: no SNR
                self.assertTrue(self.worker._loud_barge_in(_T("x", snr_db=18.0)))
            with mock.patch.object(self.workers.config, "BARGE_IN_SNR_DB", 0.0, create=True):
                self.assertFalse(self.worker._loud_barge_in(_T("x", snr_db=50.0)))

    def test_captions_only_while_awake(self):
        calls = []
        with mock.patch.object(self.workers.stt_engine, "set_partial_callback", calls.append, create=True):
            self.worker.set_mode("AWAKE")
            self.worker.set_mode("SLEEPING")
        self.assertTrue(callable(calls[0]))
        self.assertIsNone(calls[1], "asleep, nothing said near the mic may be transcribed - captions included")

    def test_partials_are_forwarded_only_while_awake_and_running(self):
        seen = []
        self.worker.partial_text.connect(seen.append)
        self.worker.mode = "AWAKE"
        self.worker._on_partial("open chr")
        self.worker.mode = "SLEEPING"
        self.worker._on_partial("secret")
        self.worker.mode = "AWAKE"
        self.worker.is_running = False
        self.worker._on_partial("late")
        self.assertEqual(seen, ["open chr"])

    def test_run_signals_each_utterance_and_releases_the_mic_when_it_stops(self):
        results = iter([None, _T("", engine="whisper"), _T("hey wave")])
        done, closed = [], []

        def listen(wake_gate=None):
            try:
                return next(results)
            except StopIteration:
                self.worker.is_running = False
                return None
        self.worker.utterance_done.connect(lambda: done.append(1))
        with mock.patch.object(self.workers.stt_engine, "listen", listen), \
                mock.patch.object(self.workers.stt_engine, "set_level_callback", lambda cb: None), \
                mock.patch.object(self.workers.stt_engine, "set_partial_callback", lambda cb: None, create=True), \
                mock.patch.object(self.workers.stt_engine, "close", lambda: closed.append(1), create=True), \
                self.speaking(False):
            self.worker.run()
        self.assertEqual(len(done), 2, "one per listen() that heard something (not for silence)")
        self.assertEqual(closed, [1])
        self.assertEqual(self.worker.mode, "AWAKE")


class _FakeLayout:
    def __init__(self):
        self.items = []

    def addWidget(self, w, *a):
        self.items.append(w)

    def removeWidget(self, w):
        self.items.remove(w)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MainWindowCaptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_phase4_ui_logic as P4
        _install_ui_fakes()
        P4.install_stub()
        for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
            del sys.modules[name]
        for _ in range(30):
            try:
                import ui.main_window as mw
                break
            except ModuleNotFoundError as e:
                missing = (e.name or "").split(".")[0]
                if not missing or missing in sys.modules:
                    raise
                sys.modules[missing] = P4._QtModule(missing)
        cls.mw = mw

    def setUp(self):
        win = object.__new__(self.mw.MainWindow)
        win._caption_row = win._caption_label = None
        win._caption_seq, win._caption_closed_at = 0, -1e9
        win.chat_layout = _FakeLayout()
        win._scroll_chat_to_bottom = lambda delay_ms=80: None
        win._bubble_max_width = lambda text: 420
        self.win = win
        self.timers = []
        p = mock.patch.object(self.mw.QTimer, "singleShot", side_effect=lambda ms, fn: self.timers.append((ms, fn)))
        p.start()
        self.addCleanup(p.stop)

    def caption_text(self):
        return self.win._caption_label.text() if self.win._caption_label is not None else None

    def test_one_caption_bubble_grows_and_is_replaced_when_the_utterance_ends(self):
        self.win._on_partial_text("open")
        self.win._on_partial_text("open chrome and")
        self.assertEqual(len(self.win.chat_layout.items), 1, "one caption bubble, updated in place")
        self.assertIn("open chrome and", self.caption_text())
        self.win._clear_caption()                    # utterance_done
        self.assertEqual(self.win.chat_layout.items, [])
        self.assertIsNone(self.caption_text())

    def test_it_clears_itself_if_nothing_follows_but_not_while_still_updating(self):
        self.win._on_partial_text("what about")
        first_timer = self.timers[-1][1]
        self.win._on_partial_text("what about tomorrow")
        first_timer()                                # an older timer must not clear a newer caption
        self.assertIsNotNone(self.caption_text())
        self.timers[-1][1]()
        self.assertIsNone(self.caption_text())

    def test_a_straggler_right_after_the_utterance_is_ignored(self):
        self.win._on_partial_text("hello")
        self.win._clear_caption()
        self.win._on_partial_text("hello there")    # arrived just after utterance_done
        self.assertEqual(self.win.chat_layout.items, [])

    def test_turn_on_mic_wires_captions(self):
        import test_tier11_voice_loop as T11
        sig = T11._Sig
        fake = types.SimpleNamespace(text_ready=sig(), mode_update=sig(), mode_changed=sig(),
                                     interruption_signal=sig(), stop_requested=sig(), audio_level=sig(),
                                     recognition_failed=sig(), partial_text=sig(), utterance_done=sig(),
                                     start=lambda: None)
        with mock.patch.object(self.mw, "ContinuousMicWorker", lambda wake_phrases=None: fake):
            self.win.turn_on_mic()
        fake.partial_text.emit("live words")
        self.assertIn("live words", self.caption_text())
        fake.utterance_done.emit()
        self.assertIsNone(self.caption_text())


if __name__ == "__main__":
    unittest.main()
