"""
tests/test_tier33_voice_picker.py — Tier 33: the cloned voice in Settings -> Voice.

The cloned voice (XTTS-v2, from the user's voice sample "r1") wasn't one of
the voices in the picker at all: it simply took over whenever a sample was
set, and the picker's Neerja/Prabhat/... only chose the voice that filled in
until it had loaded. Now it is the picker's first entry - named after its
sample, "r1 — Cloned voice" - and the default; picking an edge-tts voice
turns it off. The setting is saved as a voice id ("clone",
"en-US-AriaNeural") instead of the label; a label saved before keeps its
old meaning (it only chose the fill-in voice).

    python -m unittest discover -s tests -p "test_tier33_voice_picker.py"
"""
import json
import os
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)
import test_tier25_voice_clone as t25  # noqa: E402

CLONE = "clone"
R1_LABEL = "r1 — Cloned voice"


# --------------------------------------------------------------------------
# voice/tts.py - the engine's side
# --------------------------------------------------------------------------
class EngineVoiceChoiceTests(t25._BaseCloneTestCase):
    def _on(self, **overrides):
        """Cloning set up: coqui-tts installed, a sample that exists."""
        return self._cfg(**overrides), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", True)

    def test_the_cloned_voice_is_the_default_once_a_sample_is_set(self):
        cfg, coqui = self._on()
        with cfg, coqui:
            self.assertEqual(self.ttsmod.CLONE_VOICE_ID, CLONE)
            self.assertTrue(self.engine.use_clone)
            self.assertEqual(self.engine.voice_choice, CLONE)

    def test_without_a_sample_the_picker_shows_the_voice_actually_speaking(self):
        cfg, coqui = self._on(TTS_CLONE_REFERENCE_PATH="")
        with cfg, coqui:
            self.assertEqual(self.engine.voice_choice, "en-IN-NeerjaNeural")

    def test_picking_an_edge_voice_turns_the_clone_off_even_with_its_process_up(self):
        cfg, coqui = self._on()
        with cfg, coqui:
            self.engine._clone_engine = types.SimpleNamespace(alive=True, reference_path=self.reference_path)
            self.assertIsNotNone(self.engine._clone_engine_if_ready())
            self.engine.set_voice("en-US-AriaNeural")
            self.assertFalse(self.engine.use_clone)
            self.assertEqual(self.engine.voice, "en-US-AriaNeural")
            self.assertEqual(self.engine.voice_choice, "en-US-AriaNeural")
            self.assertFalse(self.engine._clone_reference_ready())
            self.assertIsNone(self.engine._clone_engine_if_ready())
            with mock.patch.object(self.engine, "_clone_take") as take:
                self.assertFalse(self.engine._speak_via_clone("Hello there.", self.ttsmod.language.HINGLISH))
            take.assert_not_called()
            self.assertIsNone(self.engine.warm_up_clone())

    def test_picking_the_cloned_voice_loads_it_and_gives_it_a_fresh_try(self):
        cfg, coqui = self._on()
        with cfg, coqui:
            self.engine.set_voice("en-US-GuyNeural")
            self.engine._clone_load_failed = True
            with mock.patch.object(self.engine, "warm_up_clone") as warm:
                self.engine.set_voice(CLONE)
            warm.assert_called_once_with()
            self.assertTrue(self.engine.use_clone)
            self.assertFalse(self.engine._clone_load_failed)
            # The edge-tts voice picked before fills in until the clone is ready.
            self.assertEqual(self.engine.voice, "en-US-GuyNeural")
            self.assertEqual(self.engine.voice_choice, CLONE)

    def test_a_label_saved_before_tier_33_only_sets_the_fill_in_voice(self):
        cfg, coqui = self._on()
        with cfg, coqui:
            self.engine.set_fallback_voice("en-US-AriaNeural")
            self.assertTrue(self.engine.use_clone)
            self.assertEqual(self.engine.voice, "en-US-AriaNeural")
            self.assertEqual(self.engine.voice_choice, CLONE)

    def test_the_cloned_voice_is_named_after_its_sample(self):
        label = self.ttsmod.VoiceEngine.clone_voice_label
        with self._cfg(TTS_CLONE_REFERENCE_PATH=r"D:\wave_project\avatar_rig\r1.wav"):
            self.assertEqual(label(), R1_LABEL)
        with self._cfg(TTS_CLONE_REFERENCE_PATH="/home/me/voices/r1.ogg"):
            self.assertEqual(label(), R1_LABEL)
        with self._cfg(TTS_CLONE_REFERENCE_PATH=r"C:\Users\x\Downloads\WhatsApp Ptt 2025-01-01 at 10.00.00.ogg"):
            self.assertEqual(label(), "WhatsApp Ptt 2025-01-01 at… — Cloned voice")
        with self._cfg(TTS_CLONE_REFERENCE_PATH=""):
            self.assertEqual(label(), "Cloned voice (needs a voice sample)")

    def test_the_status_line_says_where_the_cloned_voice_stands(self):
        e = self.engine
        cfg, coqui = self._on()
        with cfg, coqui:
            self.assertEqual(e.clone_state(), "idle")
            e._clone_warm = types.SimpleNamespace(done=lambda: False)
            self.assertEqual(e.clone_state(), "loading")
            self.assertIn("is loading", e.clone_status())
            self.assertIn("Neerja", e.clone_status())
            e._clone_engine = types.SimpleNamespace(alive=True, reference_path=self.reference_path,
                                                    info={"device": "cuda"})
            self.assertEqual(e.clone_state(), "ready")
            self.assertIn("ready (on the GPU)", e.clone_status())
            e._clone_engine, e._clone_warm, e._clone_load_failed = None, None, True
            self.assertEqual(e.clone_state(), "failed")
            self.assertIn("wave_voice_clone.log", e.clone_status())
            e.set_voice("en-IN-PrabhatNeural")
            self.assertEqual(e.clone_state(), "not_picked")
            self.assertIn("my_voice — Cloned voice", e.clone_status())
        cfg, coqui = self._on(TTS_CLONE_REFERENCE_PATH="")
        with cfg, coqui:
            self.assertEqual(e.clone_state(), "no_sample")
        cfg, coqui = self._on(TTS_CLONE_ENABLED=False)
        with cfg, coqui:
            self.assertEqual(e.clone_state(), "disabled")
        with self._cfg(), mock.patch.object(self.ttsmod, "HAS_COQUI_TTS", False):
            self.assertEqual(e.clone_state(), "unavailable")
        for state in ("unavailable", "disabled", "no_sample", "not_picked", "failed", "loading", "idle"):
            with mock.patch.object(e, "clone_state", return_value=state):
                self.assertTrue(e.clone_status())


# --------------------------------------------------------------------------
# ui/settings_panel.py - the desktop picker
# --------------------------------------------------------------------------
def _import_settings_panel():
    try:
        import PyQt6  # noqa: F401, PLC0415
    except ImportError:
        import test_phase4_ui_logic as P4  # noqa: PLC0415
        import test_tier9_settings_panel as T9  # noqa: PLC0415
        P4.install_stub()
        return T9.import_with_fakes("ui.settings_panel")
    import ui.settings_panel as sp  # noqa: PLC0415
    return sp


class DesktopPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sp = _import_settings_panel()

    def _panel(self, current_text=""):
        cls = self.sp.SettingsPanel
        try:
            panel = object.__new__(cls)   # the headless stand-in for Qt
        except TypeError:
            panel = cls.__new__(cls)      # real PyQt6: a bare, uninitialised panel
        panel._clone_label = R1_LABEL
        panel._clone_label = R1_LABEL
        panel.tts_clone_status_lbl = None   # the status line isn't built here
        panel.tts_combo = mock.Mock()
        panel.tts_combo.currentText.return_value = current_text
        return panel

    def _tts(self):
        tts = mock.patch.object(self.sp, "tts_engine")
        engine = tts.start()
        self.addCleanup(tts.stop)
        engine.clone_voice_label.return_value = R1_LABEL
        return engine

    def test_picking_the_cloned_voice(self):
        tts = self._tts()
        self._panel()._on_tts_voice_change(R1_LABEL)
        tts.set_voice.assert_called_once_with(CLONE)

    def test_picking_an_edge_tts_voice(self):
        tts = self._tts()
        self._panel()._on_tts_voice_change("Aria — English US (Female)")
        tts.set_voice.assert_called_once_with("en-US-AriaNeural")

    def test_the_setting_is_saved_as_the_voice_id(self):
        self._tts()
        self.assertEqual(self._panel(R1_LABEL).current_tts_voice_id(), CLONE)
        self.assertEqual(self._panel("Guy — English US (Male)").current_tts_voice_id(), "en-US-GuyNeural")

    def test_a_saved_voice_id_is_picked_again(self):
        self._tts()
        panel = self._panel()
        panel.select_tts_voice(CLONE)
        panel.select_tts_voice("en-US-GuyNeural")
        panel.select_tts_voice("not-a-voice")
        self.assertEqual([c.args[0] for c in panel.tts_combo.setCurrentText.call_args_list],
                         [R1_LABEL, "Guy — English US (Male)"])

    def test_a_label_saved_before_tier_33_keeps_the_cloned_voice(self):
        tts = self._tts()
        tts.voice_choice = CLONE
        panel = self._panel()
        panel.select_tts_voice("Aria — English US (Female)")
        tts.set_fallback_voice.assert_called_once_with("en-US-AriaNeural")
        tts.set_voice.assert_not_called()
        panel.tts_combo.setItemText.assert_called_once_with(0, R1_LABEL)
        panel.tts_combo.setCurrentText.assert_called_with(R1_LABEL)


# --------------------------------------------------------------------------
# webui/ - the web picker's Python side
# --------------------------------------------------------------------------
class WebPickerTests(unittest.TestCase):
    def setUp(self):
        import webui.bridge as bridge_module  # noqa: PLC0415
        import webui.bridge_parity as parity  # noqa: PLC0415
        self.parity = parity
        self.bridge = object.__new__(bridge_module.WaveWebBridge)
        tts = mock.patch.object(parity, "tts_engine")
        self.tts = tts.start()
        self.addCleanup(tts.stop)

    def test_apply_setting_picks_the_cloned_voice_or_only_the_fill_in_voice(self):
        self.assertTrue(self.bridge.apply_setting("tts_voice", CLONE)["ok"])
        self.tts.set_voice.assert_called_once_with(CLONE)
        self.assertTrue(self.bridge.apply_setting("tts_fallback_voice", "en-US-AriaNeural")["ok"])
        self.tts.set_fallback_voice.assert_called_once_with("en-US-AriaNeural")

    def test_the_clone_status_for_the_page(self):
        self.tts.clone_status.return_value = "The cloned voice is loading (about 20 s) - ..."
        self.tts.clone_state.return_value = "loading"
        self.tts.voice_choice = CLONE
        self.tts.clone_voice_label.return_value = R1_LABEL
        r = self.bridge.get_clone_status()
        self.assertEqual((r["ok"], r["loading"], r["voice"], r["clone_label"]), (True, True, CLONE, R1_LABEL))
        self.assertIn("loading", r["text"])

    def test_voice_settings_carry_the_cloned_voice(self):
        self.tts.voice_choice = CLONE
        self.tts.clone_voice_label.return_value = R1_LABEL
        self.tts.clone_status.return_value = "The cloned voice is ready (on the CPU)."
        self.tts.clone_state.return_value = "ready"
        self.tts.rate = "+8%"
        self.tts.volume_level = 1.0
        with mock.patch.object(type(self.bridge), "_wake_status_text", return_value=""):
            r = self.bridge.get_voice_settings()
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["voice"], r["clone_label"], r["clone_loading"]), (CLONE, R1_LABEL, False))
        self.assertEqual(r["clone_status"], "The cloned voice is ready (on the CPU).")


class WebStartupVoiceTests(unittest.TestCase):
    """The web bridge applies the saved voice before the cloned voice's
    warm-up, so an edge-tts voice picked last time doesn't load XTTS-v2 for
    nothing (the page applies the rest of the settings later)."""

    def _start(self, settings):
        import webui.bridge as bridge_module  # noqa: PLC0415
        from database.db_manager import db  # noqa: PLC0415
        tmp = S.make_tmpdir(self)
        path = os.path.join(tmp, "wave_settings.json")
        rig = os.path.join(tmp, "avatar_rig")
        os.makedirs(rig)
        clip = os.path.join(rig, "r1.wav")
        with open(clip, "wb") as f:
            f.write(b"RIFF")
        settings = dict(settings, avatar_voice_reference_path=clip)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f)
        old_db = db.db_path
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_db)
        cfg = bridge_module.config
        self.addCleanup(setattr, cfg, "TTS_CLONE_REFERENCE_PATH", cfg.TTS_CLONE_REFERENCE_PATH)
        cfg.TTS_CLONE_REFERENCE_PATH = ""
        engine = bridge_module.tts_engine
        self.addCleanup(setattr, engine, "use_clone", engine.use_clone)
        self.addCleanup(setattr, engine, "voice", engine.voice)
        with mock.patch.object(bridge_module, "SETTINGS_FILE", path), \
                mock.patch.object(bridge_module, "AVATAR_RIG_DIR", rig), \
                mock.patch.object(bridge_module.speaker_verifier, "warm_up"), \
                mock.patch.object(engine, "warm_up_clone"):
            bridge = bridge_module.WaveWebBridge(brain=object())
        self.addCleanup(bridge._reminder_worker.stop)
        return engine

    def test_an_edge_voice_saved_last_time_keeps_the_clone_from_loading(self):
        engine = self._start({"tts_voice": "en-US-AriaNeural"})
        self.assertFalse(engine.use_clone)
        self.assertEqual(engine.voice, "en-US-AriaNeural")
        self.assertFalse(engine._clone_reference_ready())

    def test_the_cloned_voice_saved_last_time_stays_on(self):
        engine = self._start({"tts_voice": CLONE})
        self.assertTrue(engine.use_clone)

    def test_a_label_saved_before_tier_33_is_left_to_the_page(self):
        engine = self._start({"tts_voice": "Aria — English US (Female)"})
        self.assertTrue(engine.use_clone)


if __name__ == "__main__":
    unittest.main()
