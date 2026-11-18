"""
avatar/state.py — Tier 25: the avatar's IDLE/LISTENING/THINKING/SPEAKING
state machine.

Deliberately Qt-free and dependency-free, the same way voice/commands.py's
decide() is pure logic kept separate from the QThread/Thread that calls
it - independently testable, and reusable from either UI's worker shape
(ui/workers.py's QThread callbacks or webui/voice_worker.py's plain-thread
ones) without caring which one is driving it.

The avatar doesn't run its own voice/brain loop - it only tracks which
state the REAL events happening elsewhere in Wave imply:

  mic mode SLEEPING/AWAKE  -> IDLE/LISTENING        (on_mic_mode_changed)
  a reply starting          -> THINKING             (on_reply_started)
  TTS audio actually playing -> SPEAKING            (on_speech_started)
  TTS audio actually stopping -> back to LISTENING/IDLE (on_speech_stopped)

on_speech_started()/on_speech_stopped() are driven by the TTS engine's
OWN playback start/stop - ui/workers.py's AIStreamWorker.started_speaking
signal and webui/voice_worker.py's WebAIStreamWorker on_speech_started
callback, both fired right before their speak_stream() call, plus
ui/main_window.py's start_speaking()/on_speech_finished() for the
separate (non-reply) TTSWorker path reminders/proactive nudges and wake/
sleep announcements use - not by the first text chunk arriving: text
streams and gets buffered into sentences well before any audio actually
starts playing (voice/tts.py's own _PREFETCH_MAX_PENDING docstring
explains why), so using a text event here would make the avatar start
"talking" before any sound does.
"""
import enum
import time


class AvatarState(enum.Enum):
    IDLE = "idle"              # mic sleeping (or off) - breathing/blinking only
    LISTENING = "listening"    # mic awake, waiting for / receiving speech
    THINKING = "thinking"      # a reply is being generated, nothing to say yet
    SPEAKING = "speaking"      # TTS audio is actually playing right now


# Mic-mode strings this module understands - exactly the two values
# WebContinuousMicWorker/ContinuousMicWorker's own on_mode_changed callback
# already uses, so callers can wire this straight off that same callback
# with zero translation.
_AWAKE = "AWAKE"
_SLEEPING = "SLEEPING"


class AvatarStateMachine:
    """One instance per running avatar window. Call the on_*() methods as
    the corresponding real event happens; read .state at render time.
    entered_at lets window.py vary animation by how long the avatar's
    been in its current state (e.g. only start the idle head-sway after
    a couple of idle seconds) without this class knowing anything about
    rendering."""

    def __init__(self):
        self.state = AvatarState.IDLE
        self.entered_at = time.monotonic()

    def _set(self, state: AvatarState):
        if state is self.state:
            return
        self.state = state
        self.entered_at = time.monotonic()

    def seconds_in_state(self) -> float:
        return time.monotonic() - self.entered_at

    # ---- mic mode -----------------------------------------------------
    def on_mic_mode_changed(self, mode: str):
        """Ignored mid-turn (THINKING/SPEAKING) - a mic mode push landing
        while a reply is in flight shouldn't visibly interrupt it; the
        mic's own real mode is still tracked internally via
        _pending_mic_mode and applied the moment the turn ends (see
        on_speech_stopped/on_reply_done_without_speech), so nothing is
        lost, just deferred."""
        if mode not in (_AWAKE, _SLEEPING):
            return  # unrecognized value - fail closed, don't guess
        self._pending_mic_mode = mode
        if self.state in (AvatarState.THINKING, AvatarState.SPEAKING):
            return
        self._set(AvatarState.LISTENING if mode == _AWAKE else AvatarState.IDLE)

    def on_mic_off(self):
        self._pending_mic_mode = None
        if self.state in (AvatarState.THINKING, AvatarState.SPEAKING):
            return
        self._set(AvatarState.IDLE)

    # ---- reply lifecycle ------------------------------------------------
    def on_reply_started(self):
        """A new reply always interrupts whatever the avatar was doing -
        mirrors send_message()/cancel_reply() superseding any reply
        already in flight."""
        self._set(AvatarState.THINKING)

    def on_speech_started(self):
        self._set(AvatarState.SPEAKING)

    def on_speech_stopped(self):
        """TTS audio finished (or there was none to play) - settle back
        to whatever the mic's real mode says it should be, falling back
        to IDLE if no mic mode was ever reported (web UI with mic never
        toggled on, say)."""
        mode = getattr(self, "_pending_mic_mode", None)
        self._set(AvatarState.LISTENING if mode == _AWAKE else AvatarState.IDLE)

    def on_reply_done_without_speech(self):
        """A reply finished (chat_done) and no speech ever started for
        it - e.g. speak_reply was False, or TTS failed outright. Without
        this, a THINKING avatar whose turn never produces audio would
        stay stuck THINKING forever, since only on_speech_stopped()
        otherwise resolves that state."""
        if self.state is AvatarState.THINKING:
            self.on_speech_stopped()
