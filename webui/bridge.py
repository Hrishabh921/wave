"""WaveWebBridge - the one seam between the pywebview page and Wave's
real backend (Tier 24).

Every public method here is meant to be called from JS as
`window.pywebview.api.<method>(...)` and returns a JSON-serializable
value (pywebview marshals the return value automatically). No method
raises outward: each one catches its own exceptions and returns an
honest `{"ok": False, "error": "..."}` rather than letting pywebview's
own generic error surface replace a useful message - the same
`{"ok": ..., ...}` shape every method uses on success, so the frontend
never has to guess which shape it got back.

Streaming events (chat chunks, mic level, reminders firing, etc.) can't
be RETURNED from a call that already finished, so they go the other
way: this module holds a reference to the pywebview `Window` once
`set_window()` is called (from webui/app.py, right after the window is
created) and pushes events into the page with
`window.evaluate_js("window.Wave.dispatch('<event>', <json>)")`. The
page's own js/bridge.js listens for those calls; see that file's
docstring-equivalent header comment for the receiving side.

This module imports NOTHING from PyQt6, and nothing in `ui/` imports
this module - the two UIs are siblings, not layered on each other.

Tier 25b adds one more sibling-reuse: the avatar companion. Wave's
PyQt6 desktop build gave it its own always-on-top floating window
(avatar/window.py) - for the web UI, per the user's own explicit
direction, it lives INSIDE the chat page instead (see js/app.js's
renderChat()), not as a second window layered on top of pywebview's
own. Only the Qt-free halves of the Tier 25 avatar work are reused
here (avatar/rig.py, avatar/state.py) - avatar/window.py itself is
PyQt6 widget code and plays no part in this; the frame-selection
logic it draws from (avatar/animation.py's frame_for_state()) is
ported to JS instead (js/avatar.js) rather than round-tripped through
this bridge on every ~66ms animation tick, which would be far too
chatty for a local, 15fps animation loop.
"""
import base64
import json
import logging
import os
import threading
import time

from settings.config import config
from database.db_manager import db
from automation.app_control import AppController
from automation.quick_notes import QuickNotesManager
from automation.todo_manager import TodoManager
from automation.calendar_manager import CalendarManager
from automation.workflow_scheduler import WorkflowScheduler
from automation.file_manager import FileManager
from automation import security_log
from automation.audit import audit as audit_singleton
from automation.undo import undo_manager, trash_enabled
from telemetry import usage_stats
from brain.groq_core import WaveBrain
from brain.skill_loader import UserSkillLoader

from webui.voice_worker import WebContinuousMicWorker, WebAIStreamWorker, WebReminderWorker
from webui.bridge_parity import ParityMixin
from monitoring.sys_monitor import SystemMonitor
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr as ocr_engine
from voice.tts import tts_engine
from voice.speaker_id import speaker_verifier
from avatar import rig as avatar_rig
from avatar import live as avatar_live
from avatar.state import AvatarStateMachine

logger = logging.getLogger("WebUI")

SETTINGS_FILE = os.path.join(config.DATA_DIR, "wave_settings.json")
# Same directory ui/main_window.py's AVATAR_RIG_DIR points at (see that
# module's own constant, right next to its SETTINGS_FILE) - a rig built
# from the desktop Settings page is immediately usable here with zero
# extra syncing, same as wave_settings.json itself already is.
AVATAR_RIG_DIR = os.path.join(config.DATA_DIR, "avatar_rig")


def _ok(**kwargs):
    kwargs["ok"] = True
    return kwargs


def _err(message):
    return {"ok": False, "error": str(message)}


def _safe(fn):
    """Decorator: catch anything a bridge method raises and turn it into
    the same {"ok": False, "error": ...} shape every method returns on
    failure, instead of letting pywebview's own opaque JS-side error
    replace a message that could actually help."""
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.error(f"{fn.__name__} failed: {e}", exc_info=True)
            return _err(str(e))
    wrapper.__name__ = fn.__name__
    return wrapper


class WaveWebBridge(ParityMixin):
    """See this module's docstring. The methods that give the web UI full
    parity with the PyQt6 desktop UI (files, music, system monitor, the
    Tier 21 pages, settings, onboarding, background services) live in
    webui/bridge_parity.py's ParityMixin."""

    def __init__(self, brain=None):
        self.brain = brain or WaveBrain()
        self._parity_init()
        self._window = None
        self._reply_worker = None
        self._mic_worker = None
        # Tier 25b: one state machine for the whole bridge (not per-worker) -
        # the avatar panel in the chat page reflects "what is Wave doing
        # right now" across however many reply/mic workers come and go,
        # exactly like ui/main_window.py's single self.avatar_state_machine.
        self._avatar_state = AvatarStateMachine()
        # Tier 27: lifelike clips (avatar/live.py) - the render job and the
        # local file server the Dodo page loads them from, both on demand.
        self._live_job = None
        self._live_server = None
        self._speech_pending = False      # Tier 29: reply handed to TTS, audio not started yet
        self._wake_phrases = ["hello wave", "wake up", "hey wave"]
        settings = self._load_settings_dict()
        # Tier 30: start with the saved wake words (Settings -> Voice), so the
        # mic listens for them from its first second, before the page loads.
        saved = settings.get("wake_phrases")
        if isinstance(saved, list) and any(isinstance(p, str) and p.strip() for p in saved):
            self._wake_phrases = [p.strip().lower() for p in saved if isinstance(p, str) and p.strip()]
        self._restore_voice_reference(settings)
        speaker_verifier.warm_up()   # Tier 32: the owner's voiceprint (their voice training) before the mic starts
        self._skills = UserSkillLoader(os.path.join(config.DATA_DIR, "user_skills"))
        self._remote = None  # lazy: webui/bridge.py's own Tier-23 reuse, see _get_remote()
        # Mirrors MainWindow._start_reminder_worker(): a reminder set via
        # chat is only as real as something that actually polls for it
        # coming due - without this, "set reminder for 2pm" wrote a row
        # to the DB that nothing ever checked, and silently never fired.
        # Starts right away (not lazily on first mic toggle) because
        # reminders fire independent of voice - same as desktop, where
        # the worker starts in MainWindow.__init__, not on mic-on.
        self._reminder_worker = WebReminderWorker(
            on_due=self._on_reminder_due,
            on_upcoming=self._on_reminder_upcoming,
        )
        self._reminder_worker.start()

    def _on_reminder_due(self, message: str, reminder_id: int):
        """Mirrors MainWindow.on_reminder_due: toast + chat bubble (when the
        Chat page has been opened at least once) + spoken aloud. No
        winsound beep - that's a Windows QSound call tied to the desktop
        widget tree; the toast + speech already carry the "something just
        fired" signal here."""
        self._push("reminder_due", {"message": message, "reminder_id": reminder_id})
        try:
            tts_engine.speak(message)
            self._follow_speech()
        except Exception:
            logger.debug("reminder_due: speak failed", exc_info=True)

    def _on_reminder_upcoming(self, message: str, reminder_id: int, minutes_until: int):
        """Mirrors MainWindow.on_reminder_upcoming - same lighter treatment
        (no urgency styling), just toast + chat bubble + speech."""
        self._push("reminder_upcoming", {"message": message, "reminder_id": reminder_id,
                                          "minutes_until": minutes_until})
        try:
            tts_engine.speak(message)
            self._follow_speech()
        except Exception:
            logger.debug("reminder_upcoming: speak failed", exc_info=True)

    def _follow_speech(self):
        """tts_engine.speak() returns at once and plays in the background;
        tell the page when it starts and (from a small watcher thread) when
        it stops, so the visualizer/status line track it the way
        MainWindow.start_speaking()/on_speech_finished() do on the desktop."""
        if self._window is None:
            return
        self._push("tts_started", {})

        def watch():
            deadline = time.monotonic() + 120
            time.sleep(0.3)
            while getattr(tts_engine, "is_speaking", False) and time.monotonic() < deadline:
                time.sleep(0.1)
            self._push("tts_finished", {"success": True})
        threading.Thread(target=watch, name="WebSpeechWatch", daemon=True).start()

    # ---- wiring -----------------------------------------------------

    def set_window(self, window):
        """Called once, by webui/app.py, right after pywebview.create_window()
        (and with None as the window closes)."""
        self._window = window
        # Tier 27: the Dodo page's mouth follows the audio actually playing.
        # Tier 29: and she only starts "speaking" once that audio starts.
        if window is not None:
            tts_engine.on_playback_pcm = self._on_tts_pcm
            tts_engine.on_playback_start = self._on_tts_audio_start
        else:
            if getattr(tts_engine, "on_playback_pcm", None) == self._on_tts_pcm:
                tts_engine.on_playback_pcm = None
            if getattr(tts_engine, "on_playback_start", None) == self._on_tts_audio_start:
                tts_engine.on_playback_start = None

    def _push(self, event, payload=None):
        """Fire-and-forget a browser-side event. Never raises - a push
        racing the window closing is routine, not a bug to surface."""
        win = self._window
        if win is None:
            return
        try:
            win.evaluate_js(f"window.Wave && window.Wave.dispatch({json.dumps(event)}, {json.dumps(payload)})")
        except Exception:
            logger.debug(f"push({event}) failed - window likely closing", exc_info=True)

    # ---- bootstrap / settings ----------------------------------------

    @_safe
    def get_bootstrap(self):
        """Everything the page needs before it builds itself: settings, the
        optional-dependency flags that change what the desktop shows (winsdk
        seek buttons, pycaw volume slider, ...), custom themes, and whether
        this is a first run (no settings file -> onboarding wizard)."""
        settings = self._load_settings_dict()
        caps = self.get_capabilities()
        caps.pop("ok", None)
        try:
            brief = "{provider} ({model})".format(**self.brain.provider_info())
        except Exception:
            brief = ""
        manifest = None
        try:
            manifest = avatar_rig.load_manifest(AVATAR_RIG_DIR)
        except Exception:
            manifest = None
        return _ok(settings=settings, assistant_name=getattr(config, "APP_NAME", "Wave"),
                   version=getattr(config, "VERSION", ""), chat_history=self.get_chat_history()["messages"],
                   capabilities=caps, custom_themes=self._custom_themes(),
                   last_theme=settings.get("last_theme", "Black Glass"), first_run=not os.path.exists(SETTINGS_FILE),
                   default_path=FileManager.get_default_path(), local_ip=self._local_ip(),
                   provider_brief=brief, has_avatar_rig=bool(manifest))

    def _restore_voice_reference(self, settings: dict):
        """MainWindow._init_avatar()'s Tier 25 restore, which the web UI
        never had: the voice sample "Build avatar" saved is what makes Wave
        speak in the user's own cloned voice, and without this every web
        session quietly spoke with edge-tts instead."""
        # Tier 33: the voice picked in Settings -> Voice decides whether the
        # cloned voice loads at all, so it's applied before the warm-up (the
        # page applies the other settings later). A label - saved before
        # Tier 33 - only chose the voice that fills in for the clone; the
        # page handles that one.
        saved_voice = str(settings.get("tts_voice") or "").strip()
        if saved_voice and " — " not in saved_voice:
            tts_engine.set_voice(saved_voice)
        saved = (settings.get("avatar_voice_reference_path") or "").strip()
        path = avatar_rig.find_voice_reference(saved, AVATAR_RIG_DIR)
        if path:
            config.TTS_CLONE_REFERENCE_PATH = path
            if path != saved:
                # The project folder moved; remember where the clip is now.
                self.save_settings({"avatar_voice_reference_path": path})
            logger.info("Tier 25: restored voice-clone reference from a previous session.")
            tts_engine.warm_up_clone()   # Tier 31: load the model before the first reply
        elif saved:
            logger.warning(f"Tier 25: previously-set voice-clone reference '{saved}' is gone - "
                           "falling back to edge-tts/Piper/Kokoro until 'Build avatar' is run again.")

    def _load_settings_dict(self):
        if not os.path.exists(SETTINGS_FILE):
            return {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"load settings failed: {e}")
            return {}

    @_safe
    def get_settings(self):
        return _ok(settings=self._load_settings_dict())

    @_safe
    def save_settings(self, patch: dict):
        """Merges `patch` into the existing flat settings dict and writes
        it back - the same file (and same flat-JSON shape) ui/main_window.py's
        save_settings()/load_settings() already use, so either UI can be
        launched against the same wave_settings.json without losing or
        forking the other's choices."""
        data = self._load_settings_dict()
        data.update(patch or {})
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return _ok(settings=data)

    @_safe
    def check_provider(self):
        ok, message, ms = self.brain.check_provider()
        return _ok(provider_ok=ok, message=message, ms=ms)

    # ---- chat ---------------------------------------------------------

    @_safe
    def get_chat_history(self, limit: int = 50):
        rows = db.get_chat_history(limit=limit)
        messages = [{"role": r[0], "text": r[1]} for r in (rows or [])]
        return _ok(messages=messages)

    @_safe
    def clear_chat(self):
        """Like MainWindow.clear_chat(): the visible conversation is cleared
        page-side and any reply in flight is cancelled. It used to call
        db.clear_all(), which wipes far more than the chat (memory,
        reminders, to-dos, ...) - the desktop's "Clear chat" never did that."""
        self.cancel_reply()
        return _ok()

    @_safe
    def send_message(self, text: str, attachment: dict = None, from_voice: bool = False, turn=None):
        """Starts the reply in a background thread and returns immediately
        - chat_chunk/chat_done events carry the actual reply back to the
        page. Any reply already in flight is cancelled first, exactly as
        a new typed message supersedes the one in flight on desktop.

        Tier 25b: also drives the avatar companion through the same
        lifecycle ui/main_window.py's _send_text()/_on_reply_done()/
        _on_reply_speech_finished() wire the desktop AvatarWindow through
        - THINKING the moment the reply starts, SPEAKING once TTS audio
        actually begins, then back to LISTENING/IDLE once it stops (or
        once the reply finishes with nothing ever having been spoken)."""
        self.cancel_reply()
        self._avatar_state.on_reply_started()
        self._push_avatar_state()
        # Time-to-first-audio, same as MainWindow.send_message(): a voice
        # command reuses the turn the mic thread opened, a typed one opens
        # its own.
        ttfa_turn = None
        try:
            from telemetry import ttfa
            ttfa_turn = ttfa.tracker.active_id() if from_voice else None
            if ttfa_turn is None:
                ttfa_turn = ttfa.tracker.begin("typed", heard=text)
            ttfa.tracker.mark("dispatched", turn_id=ttfa_turn)
        except Exception:
            ttfa_turn = None

        def finished_speaking(success=True):
            self._speech_pending = False
            self._avatar_on_speech_stopped(success)
            self._push("reply_speech_finished", {"success": bool(success)})
            self._note_mic_activity()
            try:
                if ttfa_turn is not None:
                    from telemetry import ttfa
                    ttfa.tracker.finish("interrupted" if worker.cancelled else ("ok" if success else "no_audio"),
                                        turn_id=ttfa_turn)
            except Exception:
                pass

        worker = WebAIStreamWorker(
            self.brain, text, attachment=attachment, speak_reply=True, ttfa_turn=ttfa_turn,
            # Tier 29: the page's turn number goes back on every event, so
            # a cancelled turn that finishes late can't end the next one.
            on_text_chunk=lambda piece: self._push("chat_chunk", {"piece": piece, "turn": turn}),
            on_response_ready=lambda full_text: self._on_reply_ready(full_text, turn),
            # Tier 29: not SPEAKING yet - the reply hasn't even been written.
            # The avatar switches when the audio actually starts (_on_tts_audio_start).
            on_speech_started=self._avatar_speech_pending,
            on_finished_speaking=finished_speaking,
        )
        self._reply_worker = worker
        worker.start()
        return _ok(started=True)

    @_safe
    def cancel_reply(self):
        worker, self._reply_worker = self._reply_worker, None
        if worker is None:
            return _ok(cancelled=False)
        worker.cancel()
        return _ok(cancelled=True)

    # ---- avatar companion (Tier 25b) -------------------------------------
    # Lives inside the chat page (js/app.js's renderChat() + js/avatar.js),
    # not a separate window - see this module's own docstring above for
    # why. These helpers are the bridge-side half of that: the state
    # machine itself is Qt-free avatar/state.py, reused as-is; only the
    # wiring below (which real Wave events feed it) is new.

    def _push_avatar_state(self):
        self._push("avatar_state", {"state": self._avatar_state.state.value})

    def _avatar_on_speech_started(self):
        self._avatar_state.on_speech_started()
        self._push_avatar_state()

    def _avatar_speech_pending(self):
        """Tier 29: the reply worker is about to hand its text to TTS - but
        the model may not have written a word yet (and on a busy cloud that
        took many seconds), so the avatar stays THINKING until the audio
        really starts (_on_tts_audio_start). Before, it switched to SPEAKING
        here and the Dodo page moved her lips through all that silence."""
        self._speech_pending = True

    def _on_tts_audio_start(self):
        """voice/tts.py's on_playback_start: an utterance's audio has just
        been handed to the speakers (every audio path, Windows SAPI too)."""
        try:
            from telemetry import ttfa
            ttfa.tracker.on_audio_start()
        except Exception:
            pass
        if getattr(self, "_speech_pending", False):
            self._speech_pending = False
            self._avatar_on_speech_started()

    def _note_mic_activity(self):
        worker = getattr(self, "_mic_worker", None)
        note = getattr(worker, "note_activity", None)
        if callable(note):
            note()

    def _avatar_on_speech_stopped(self, success: bool = True):
        self._avatar_state.on_speech_stopped()
        self._push_avatar_state()

    def _on_reply_ready(self, full_text: str, turn=None):
        self._push("chat_done", {"text": full_text, "turn": turn})
        self._note_mic_activity()
        # A no-op unless this turn's state is still THINKING - the normal
        # speak_reply=True path already settled it via
        # _avatar_on_speech_stopped() above, well before this callback
        # fires (see webui/voice_worker.py's WebAIStreamWorker._run_inner()
        # for the exact emission order). This only matters when a reply
        # produces nothing speakable at all (TTS failed, or speak_reply
        # were ever False) - without it the avatar would stay stuck
        # THINKING forever, mirroring ui/main_window.py's own
        # _avatar_on_reply_done_without_speech() call in _on_reply_done().
        self._avatar_state.on_reply_done_without_speech()
        self._push_avatar_state()

    @_safe
    def get_avatar_rig(self):
        """Base64-encodes each avatar/rig.py frame as a data: URI so the
        chat page can show them as plain <img> tags - sidesteps any
        file://-access restrictions a given pywebview platform backend
        might impose (a real concern on Windows' WebView2 depending on its
        security config), and is fetched once when the avatar panel is
        built, not per animation frame. `width`/`height` are the original
        photo's real dimensions (avatar/rig.py's build_rig() manifest) -
        js/avatar.js sizes the panel from these so the image fits its
        container at the correct aspect ratio instead of being stretched
        or cropped, which is the actual bug this tier exists to fix.

        has_rig: False (not an error) means no photo has been set up yet -
        the page shows its own "set up your avatar" placeholder instead of
        treating this like a failure."""
        manifest = avatar_rig.load_manifest(AVATAR_RIG_DIR)
        if not manifest:
            return _ok(has_rig=False)
        frames = {}
        for name, filename in manifest.get("frames", {}).items():
            try:
                with open(os.path.join(AVATAR_RIG_DIR, filename), "rb") as f:
                    frames[name] = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
            except Exception as e:
                logger.warning(f"get_avatar_rig: couldn't read frame '{filename}': {e}")
        if not frames:
            return _ok(has_rig=False)
        return _ok(has_rig=True, frames=frames, width=manifest.get("width"), height=manifest.get("height"))

    # ---- Tier 27: lifelike avatar (avatar/live.py, avatar/live_render.py) ----

    def _ensure_live_server(self):
        if self._live_server is None:
            self._live_server = avatar_live.LiveAssetServer(avatar_live.live_dir(AVATAR_RIG_DIR))
        return self._live_server

    def _live_status(self):
        job = self._live_job
        rendering = bool(job is not None and job.is_alive())
        problem = avatar_live.renderer_problem()
        return dict(configured=not problem, problem=problem, rendering=rendering,
                    done=job.done if rendering else 0, total=job.total if job is not None else 0,
                    last_error=(job.error if job is not None and not rendering and not job.ok else ""))

    @_safe
    def get_avatar_live(self):
        """The Dodo page's lifelike clips: available=True with where to load
        them from (a token URL on 127.0.0.1) and the manifest saying where
        every frame sits - or available=False, and whether a render can be
        made (Settings -> Avatar) or is running right now."""
        manifest = avatar_live.load_manifest(AVATAR_RIG_DIR)
        status = self._live_status()
        if not manifest:
            return _ok(available=False, **status)
        server = self._ensure_live_server()
        server.set_allowed(avatar_live.asset_names(manifest))
        return _ok(available=True, base_url=server.base_url, manifest=manifest, **status)

    @_safe
    def render_avatar_live(self):
        """Starts rendering the lifelike clips for the current avatar photo
        in the LivePortrait environment (a few minutes on the GPU). Progress
        arrives as avatar_live_progress events, the end as avatar_live_done."""
        problem = avatar_live.renderer_problem()
        if problem:
            return _err(problem)
        if not avatar_rig.load_manifest(AVATAR_RIG_DIR):
            return _err("Set up the avatar first: Settings → Avatar → pick a photo → Build avatar.")
        if self._live_job is not None and self._live_job.is_alive():
            return _ok(started=False, rendering=True)
        job = avatar_live.LiveRenderJob(
            AVATAR_RIG_DIR,
            on_progress=lambda done, total, clip: self._push("avatar_live_progress",
                                                             {"done": done, "total": total, "clip": clip}),
            on_done=lambda ok, error: self._push("avatar_live_done", {"ok": bool(ok), "error": error or ""}))
        self._live_job = job
        job.start()
        return _ok(started=True, total=job.total)

    def _stop_live(self):
        """Window closing: stop a render in progress (the old clips stay) and the file server."""
        job, server = self._live_job, self._live_server
        if job is not None and job.is_alive():
            job.cancel()
        if server is not None:
            server.close()
            self._live_server = None

    def _on_tts_pcm(self, pcm, sample_rate):
        """voice/tts.py's on_playback_pcm: one utterance is about to play -
        send the page its mouth-opening curve. Pushed from a thread of its
        own so the audio never waits on the window."""
        if self._window is None:
            return
        try:
            env = avatar_live.speech_envelope(pcm, int(sample_rate))
        except Exception:
            logger.debug("speech envelope failed", exc_info=True)
            return
        if env:
            threading.Thread(target=self._push, args=("avatar_speech", {"env": env, "fps": avatar_live.FPS}),
                             daemon=True).start()

    @_safe
    def get_avatar_state(self):
        """One-shot pull of the avatar's current state - used when the
        chat page (re)builds its panel, so it doesn't sit on a stale
        "idle" guess until the next real transition happens to push an
        avatar_state event."""
        return _ok(state=self._avatar_state.state.value)

    # ---- voice ----------------------------------------------------------

    @_safe
    def toggle_mic(self):
        if self._mic_worker is not None and self._mic_worker.is_alive():
            self._mic_worker.stop()
            self._mic_worker = None
            # Tier 25b: the desktop build never calls this either (its
            # AvatarWindow only ever sees SLEEPING/AWAKE from
            # handle_wake_word_mode, never a true "off"), but the web UI's
            # mic can be switched fully off, and the user's own ask was
            # explicit that the character should step back the moment the
            # mic goes off, not stay LISTENING forever on a worker that no
            # longer exists - so this is a deliberate small improvement
            # over the desktop build, not a drift from it.
            self._avatar_state.on_mic_off()
            self._push_avatar_state()
            return _ok(listening=False)
        self._start_mic_worker()
        return _ok(listening=True)

    def _start_mic_worker(self):
        """The continuous mic worker (ContinuousMicWorker on the desktop).
        The page normally keeps it running for the whole session via
        turn_on_mic() and flips AWAKE/SLEEPING with toggle_listening(),
        exactly like MainWindow.turn_on_mic()/toggle_mic()."""
        worker = WebContinuousMicWorker(
            wake_phrases=self._wake_phrases,
            on_text_ready=lambda text: self._push("voice_command", {"text": text}),
            on_mode_changed=self._on_voice_mode_changed,
            on_interruption=self._on_barge_in,
            on_stop_requested=self._on_voice_stop,
            on_recognition_failed=lambda: self._push("voice_recognition_failed", {}),
            on_audio_level=lambda level: self._push("voice_level", {"level": level}),
            on_partial_text=lambda text: self._push("voice_partial", {"text": text}),
            on_utterance_done=lambda: self._push("voice_utterance_done", {}),
        )
        self._mic_worker = worker
        worker.start()
        return worker

    def _on_voice_mode_changed(self, mode: str, announce: bool):
        """Mirrors MainWindow.handle_wake_word_mode: pushes the real
        SLEEPING/AWAKE state to the sidebar (it used to show "LISTENING"
        the instant the mic turned on, regardless of whether the worker
        was actually awake or still waiting for the wake phrase - see
        app.js's voice_mode handler) and, same as desktop, speaks the
        wake/sleep announcement itself rather than leaving the mode
        change silent.

        Tier 25b: also feeds the avatar state machine, same as
        ui/main_window.py's handle_wake_word_mode() -> _avatar_on_mic_mode_changed()
        - SLEEPING -> IDLE, AWAKE -> LISTENING (deferred if a reply is
        mid-flight; see avatar/state.py's own on_mic_mode_changed()
        docstring)."""
        self._avatar_state.on_mic_mode_changed(mode)
        self._push_avatar_state()
        self._push("voice_mode", {"mode": mode, "announce": announce})
        if not announce:
            return
        try:
            tts_engine.speak("Systems active." if mode == "AWAKE" else "Standing by.")
            self._follow_speech()
        except Exception:
            logger.debug("voice_mode announce: speak failed", exc_info=True)

    @_safe
    def mic_status(self):
        worker = self._mic_worker
        return _ok(listening=bool(worker and worker.is_alive()),
                    mode=(worker.mode if worker else "OFF"))

    # ---- memory ---------------------------------------------------------

    @_safe
    def get_memory(self):
        # get_all_memory() only gives (key, value); get_memory_provenance()
        # has the Tier 20 source/updated_at for one key - the fact count is
        # always small (facts, not chat turns), so an N+1 lookup here costs
        # nothing noticeable and is simpler than duplicating its query.
        rows = db.get_all_memory()
        items = []
        for key, value in (rows or []):
            prov = db.get_memory_provenance(key) or {}
            items.append({"key": key, "value": value, "source": prov.get("source"),
                           "updated_at": prov.get("updated_at")})
        return _ok(items=items)

    @_safe
    def delete_memory(self, key: str):
        ok = db.delete_memory(key)
        return _ok(deleted=bool(ok))

    # ---- notes & to-dos ---------------------------------------------------

    @_safe
    def get_notes(self):
        rows = db.list_quick_notes()
        return _ok(notes=[{"id": r[0], "content": r[1], "created_at": r[2]} for r in (rows or [])])

    @_safe
    def add_note(self, content: str):
        return _ok(message=QuickNotesManager.take(content))

    @_safe
    def clear_notes(self):
        return _ok(message=QuickNotesManager.clear())

    @_safe
    def get_todos(self, include_completed: bool = False):
        rows = db.list_todos(include_completed=include_completed)
        return _ok(todos=[{"id": r[0], "text": r[1], "due_text": r[2], "completed": bool(r[3]),
                            "created_at": r[4]} for r in (rows or [])])

    @_safe
    def add_todo(self, text: str, due_text: str = None):
        return _ok(message=TodoManager.add(text, due_text))

    @_safe
    def complete_todo(self, query: str):
        return _ok(message=TodoManager.complete(query))

    @_safe
    def delete_todo(self, query: str):
        return _ok(message=TodoManager.delete(query))

    # ---- reminders -----------------------------------------------------

    @_safe
    def get_reminders(self):
        rows = db.get_upcoming_reminders()  # (id, message, due_at, recurrence)
        return _ok(reminders=[{"id": r[0], "message": r[1], "due_at": r[2], "recurrence": r[3]}
                               for r in (rows or [])])

    @_safe
    def cancel_reminder(self, query: str):
        count = db.cancel_reminder(query)
        return _ok(cancelled=count)

    # ---- calendar --------------------------------------------------------

    @_safe
    def get_calendar_events(self, when_text: str = ""):
        return _ok(message=CalendarManager.list_events(when_text))

    @_safe
    def add_calendar_event(self, title: str, when_text: str, duration_minutes=None,
                            location: str = "", recurrence: str = ""):
        return _ok(message=CalendarManager.create_event(title, when_text, duration_minutes,
                                                          location, recurrence))

    @_safe
    def cancel_calendar_event(self, query: str):
        return _ok(message=CalendarManager.cancel_event(query))

    # ---- workflows -------------------------------------------------------

    @_safe
    def get_workflows(self):
        rows = db.list_workflows_full()  # [(name, [action_str, ...]), ...]
        return _ok(workflows=[{"name": name, "actions": actions} for name, actions in (rows or [])])

    @_safe
    def run_workflow(self, name: str):
        """Mirrors brain/handlers/automation.py's run_workflow skill exactly
        (same brain.process_actions() loop, same stop-on-pending-confirm
        rule) rather than re-executing the saved action strings some other
        way - one executor for "run this command", whether it arrived by
        voice, typed chat, or a saved workflow."""
        actions = db.get_workflow(name)
        if not actions:
            return _err(f"I don't have a workflow called '{name}'.")
        results = []
        for a in actions:
            res = self.brain.process_actions(a)
            if res:
                results.append(res)
            if self.brain.pending_action is not None:
                break
        return _ok(results=results)

    @_safe
    def delete_workflow(self, name: str):
        return _ok(deleted=db.delete_workflow(name))

    # ---- skills -----------------------------------------------------------

    @_safe
    def get_skills(self):
        self._skills.scan_once()
        disabled = self._skills.disabled_skills()
        files = []
        directory = self._skills.directory if hasattr(self._skills, "directory") else None
        try:
            for fn in sorted(os.listdir(directory)) if directory and os.path.isdir(directory) else []:
                if fn.endswith(".py"):
                    files.append({"filename": fn, "disabled": fn in disabled})
        except Exception:
            pass
        return _ok(skills=files)

    @_safe
    def set_skill_disabled(self, filename: str, disabled: bool):
        self._skills.set_disabled(filename, disabled)
        return _ok()

    # ---- performance -----------------------------------------------------

    @_safe
    def get_performance(self, days: int = 7):
        if not usage_stats.enabled():
            return _ok(enabled=False)
        return _ok(enabled=True, stats=usage_stats.summarize(days=days))

    # ---- security ----------------------------------------------------------

    @_safe
    def get_security_log(self, limit: int = 50):
        events = security_log.recent(limit=limit)
        return _ok(events=[{"kind": e.kind, "reason": e.reason, "tool": e.tool,
                             "time": e.time_text} for e in (events or [])])

    @_safe
    def get_security_mode(self):
        return _ok(mode=security_log.effective_mode())

    @_safe
    def set_security_mode(self, mode: str):
        return _ok(mode=security_log.set_mode(mode))

    # ---- activity / undo ----------------------------------------------------

    @_safe
    def get_recent_activity(self, limit: int = 30):
        records = audit_singleton.recent(limit=limit)
        return _ok(actions=[{"id": r.id, "tool": r.tool, "summary": r.summary(),
                              "status": r.status, "reversible": r.reversible,
                              "time": r.time_text} for r in (records or [])])

    @_safe
    def undo_action(self, action_id: int):
        result = undo_manager.undo(action_id, "web")
        return _ok(success=result.ok, message=result.message)

    @_safe
    def undo_last(self):
        rec = audit_singleton.last_reversible()
        if rec is None:
            return _ok(success=False, message="There's nothing to undo right now.")
        result = undo_manager.undo(rec.id, "web")
        return _ok(success=result.ok, message=result.message)

    @_safe
    def trash_enabled(self):
        return _ok(enabled=trash_enabled())

    # ---- system quick actions -------------------------------------------

    @_safe
    def get_volume(self):
        return _ok(volume=AppController.get_volume())

    @_safe
    def set_volume(self, percent: int):
        return _ok(message=AppController.set_volume(percent))

    @_safe
    def get_brightness(self):
        return _ok(message=AppController.get_brightness_status())

    @_safe
    def set_brightness(self, percent: int):
        return _ok(message=AppController.set_brightness(percent))

    @_safe
    def toggle_focus_mode(self, enabled: bool):
        if enabled:
            return _ok(message=AppController.enable_mode("focus"))
        return _ok(message=AppController.disable_mode())

    @_safe
    def take_screenshot(self):
        return _ok(message=AppController.take_screenshot())

    @_safe
    def power_action(self, action: str):
        fn = getattr(AppController, action, None)
        if action not in {"shutdown_pc", "restart_pc", "sleep_pc", "hibernate_pc",
                           "log_off", "cancel_shutdown", "lock_pc"} or not callable(fn):
            return _err(f"Unknown or disallowed power action: {action}")
        return _ok(message=fn())

    @_safe
    def open_app(self, app_name: str):
        return _ok(message=AppController.open_app(app_name))

    @_safe
    def close_app(self, app_name: str):
        return _ok(message=AppController.close_app(app_name))

    # ---- system monitor -----------------------------------------------------

    @_safe
    def get_system_stats(self):
        return _ok(stats=SystemMonitor.get_stats())

    # ---- music / media -------------------------------------------------------
    # Mirrors ui/main_window.py's Music page: whatever player is already
    # active on the PC (Spotify, YouTube in a tab, the OS player) is driven
    # with the OS media keys, exactly like brain/handlers/media.py's
    # media_play_pause/next/previous skills - one control surface, not two.

    @_safe
    def media_play_pause(self):
        import pyautogui
        pyautogui.press("playpause")
        return _ok(message="Toggled play/pause.")

    @_safe
    def media_next_track(self):
        import pyautogui
        pyautogui.press("nexttrack")
        return _ok(message="Skipped to the next track.")

    @_safe
    def media_previous_track(self):
        import pyautogui
        pyautogui.press("prevtrack")
        return _ok(message="Went to the previous track.")

    # ---- vision ---------------------------------------------------------------

    @_safe
    def vision_read_screen(self):
        """Capture the screen and OCR it - same path as ui/workers.py's OCRWorker."""
        path = VisionCapture.take_screenshot()
        if not path:
            return _err("Failed to capture the screen.")
        text = ocr_engine.extract_text(path)
        return _ok(text=text)

    @_safe
    def vision_take_photo(self):
        return _ok(message=VisionCapture.take_photo())

    # ---- clipboard ---------------------------------------------------------

    @_safe
    def get_clipboard(self):
        try:
            import pyperclip
            return _ok(text=pyperclip.paste())
        except Exception as e:
            return _err(f"Clipboard unavailable: {e}")

    @_safe
    def set_clipboard(self, text: str):
        return _ok(message=AppController.set_clipboard(text))

    # ---- files ------------------------------------------------------------

    @_safe
    def list_files(self, path: str = None):
        target = path or FileManager.get_default_path()
        entries = []
        for name in sorted(os.listdir(target)):
            full = os.path.join(target, name)
            entries.append({"name": name, "is_dir": os.path.isdir(full),
                             "size": os.path.getsize(full) if os.path.isfile(full) else None})
        return _ok(path=target, entries=entries)

    @_safe
    def create_file(self, file_name: str, content: str = "", path: str = None):
        return _ok(message=FileManager.create_file(file_name, content, path))

    @_safe
    def create_folder(self, folder_name: str, path: str = None):
        return _ok(message=FileManager.create_folder(folder_name, path))

    @_safe
    def delete_file(self, file_name: str, path: str = None):
        return _ok(message=FileManager.delete_file(file_name, path))

    # ---- remote dashboard (Tier 23 reuse) --------------------------------

    def _get_remote(self):
        if self._remote is None:
            from remote.runner import RemoteDashboardServer
            self._remote = RemoteDashboardServer(self.brain, assistant_name=getattr(config, "APP_NAME", "Wave"))
        return self._remote

    @_safe
    def start_remote_dashboard(self, port: int = None):
        from remote.runner import DEFAULT_PORT
        ok, message = self._get_remote().start(port=port or DEFAULT_PORT)
        return _ok(started=ok, message=message)

    @_safe
    def stop_remote_dashboard(self):
        self._get_remote().stop()
        return _ok()

    @_safe
    def remote_pairing_info(self):
        info = self._get_remote().pairing_info()
        if info is None:
            return _ok(running=False)
        # Settings -> Remote shows the pairing QR code, same as the desktop
        # page (ui/settings_panel.py's refresh_remote_pairing_display()).
        qr = None
        try:
            from remote.qr import HAS_QRCODE, build_pairing_qr_png
            if HAS_QRCODE:
                qr = "data:image/png;base64," + base64.b64encode(build_pairing_qr_png(info.url)).decode("ascii")
        except Exception:
            logger.debug("pairing QR render failed", exc_info=True)
        return _ok(running=True, url=info.url, code=info.code, lan_ip=info.lan_ip, port=info.port, qr=qr)

    @_safe
    def regenerate_remote_pairing(self):
        info = self._get_remote().regenerate_pairing()
        if info is None:
            return _err("The dashboard isn't running.")
        return _ok(url=info.url, code=info.code, lan_ip=info.lan_ip, port=info.port)
