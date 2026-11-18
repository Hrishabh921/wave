"""Plain-thread ports of ui/workers.py's ContinuousMicWorker and
AIStreamWorker for the pywebview UI.

Both classes in ui/workers.py are QThread subclasses, but neither one's
run() logic actually depends on Qt - they call straight into
voice/stt.py, voice/commands.py, voice/wakeword.py, voice/tts.py,
voice/speech_filter.py and brain/groq_core.py, all of which are plain
Python, and communicate results back via pyqtSignal.emit(...) calls that
are really just "call this callback with these arguments" dressed up in
Qt's event-queue machinery. Swapping pyqtSignal.emit() for a direct
callback() call and QThread for threading.Thread changes nothing about
WHEN or WHY a wake word fires, a turn gets cancelled, or TTS plays - the
same decide()/think_stream()/speak_stream() calls run in the same order.

This is intentionally a close structural mirror of ui/workers.py, not an
independent reimplementation - two different voice pipelines would be a
real way to introduce a behavioral drift between the desktop and web UIs.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from voice.tts import tts_engine
from voice.stt import stt_engine
from voice import commands as voice_commands
from voice import speech_filter
from voice.commands import decide
from voice.wakeword import get_wake_gate
from voice.aec import aec_processor
from telemetry import trace, ttfa
from settings.config import config
from database.db_manager import db
from reminders import context as ctx
from reminders.composer import composer as reminder_composer

logger = logging.getLogger("WebUI")


class WebContinuousMicWorker(threading.Thread):
    """threading.Thread port of ui/workers.py's ContinuousMicWorker.

    Callbacks (all optional, called with the same arguments the Qt
    version's matching signal carried):
      on_text_ready(str)
      on_mode_changed(mode: str, announce: bool)
      on_interruption()
      on_stop_requested()
      on_recognition_failed()
      on_audio_level(float)
      on_partial_text(str)
      on_utterance_done()
    """

    def __init__(self, wake_phrases=None, on_text_ready=None, on_mode_changed=None,
                 on_interruption=None, on_stop_requested=None, on_recognition_failed=None,
                 on_audio_level=None, on_partial_text=None, on_utterance_done=None):
        super().__init__(daemon=True)
        self.is_running = True
        self.mode = "SLEEPING"
        self.wake_phrases = wake_phrases or ["hello wave", "wake up", "hey wave"]
        self.wake_gate = get_wake_gate()
        self._on_text_ready = on_text_ready or (lambda text: None)
        self._on_mode_changed = on_mode_changed or (lambda mode, announce: None)
        self._on_interruption = on_interruption or (lambda: None)
        self._on_stop_requested = on_stop_requested or (lambda: None)
        self._on_recognition_failed = on_recognition_failed or (lambda: None)
        self._on_audio_level = on_audio_level or (lambda level: None)
        self._on_partial_text = on_partial_text or (lambda text: None)
        self._on_utterance_done = on_utterance_done or (lambda: None)
        # Tier 29: when Wave last did something (woke, took a command,
        # spoke) - AWAKE falls back to SLEEPING after AWAKE_IDLE_SLEEP_S.
        self._last_activity = time.monotonic()

    def note_activity(self):
        """The bridge calls this when a reply finishes, so the idle timer
        counts from the end of Wave's answer, not from the question."""
        self._last_activity = time.monotonic()

    def _maybe_doze(self):
        """Tier 29 (web UI): AWAKE used to last forever, so in a room with a
        TV or other people every stray transcript became a command - each one
        a cloud request that used up the per-minute quota (and cut off the
        reply in progress). After AWAKE_IDLE_SLEEP_S quiet seconds Wave goes
        back to waiting for its wake word, silently."""
        idle = float(getattr(config, "AWAKE_IDLE_SLEEP_S", 60) or 0)
        if self.mode != "AWAKE" or idle <= 0:
            return
        now = time.monotonic()
        if getattr(tts_engine, "is_speaking", False):
            self._last_activity = now
        elif now - self._last_activity > idle:
            logger.info(f"Mic: {idle:.0f}s with nothing for Wave - back to waiting for the wake word")
            self.set_mode("SLEEPING", announce=False)

    def _unsure_noise(self, text, decision) -> bool:
        """Tier 29: a short, low-confidence, unaddressed transcript the intent
        router doesn't know ("Gatya", "Salas un tini", "You have eaten.") is
        almost always room noise misheard by Whisper - and it would go to the
        cloud model. Ignored; anything with the wake word, any command the
        router handles locally, and anything longer or clearer still goes
        through."""
        if decision.action != voice_commands.COMMAND or self.mode != "AWAKE":
            return False
        conf = getattr(text, "confidence", None)
        floor = float(getattr(config, "AWAKE_MIN_CONFIDENCE", 0.5) or 0)
        if conf is None or floor <= 0 or float(conf) >= floor:
            return False
        if len(voice_commands.normalize_utterance(decision.text or "").split()) > 3:
            return False
        addressed, _ = voice_commands.strip_wake_phrase(str(text), self._all_wake_phrases(), allow_bare_name=True)
        if addressed:
            return False
        try:
            from brain import intent_router
            if intent_router.route(decision.text or "") is not None:
                return False
        except Exception:
            pass
        logger.info(f"Mic: ignoring unsure speech '{decision.text}' (confidence {float(conf):.2f})")
        return True

    def _is_bare_wake(self, heard) -> bool:
        """True when `heard` is just a wake word (plus filler), nothing more."""
        if not heard:
            return False
        addressed, rest = voice_commands.strip_wake_phrase(heard, self._all_wake_phrases(), allow_bare_name=False)
        return addressed and voice_commands.is_filler_only(rest)

    def _wake_hint(self):
        """Tier 30: "Baby, Babu, Dodo, Suar, Hey Wave, Wake Up." - put at the
        front of Whisper's prompt while asleep, so it writes short wake words
        the way they're configured instead of guessing another language."""
        words = []
        for p in self._all_wake_phrases():
            n = voice_commands.normalize_utterance(p)
            if n and n not in words:
                words.append(n)
        return (", ".join(w.title() for w in words) + ".") if words else None

    def set_wake_phrases(self, phrases):
        self.wake_phrases = phrases

    def _gate_in_use(self):
        gate = self.wake_gate
        try:
            if gate is None or not gate.wanted():
                return None
            if getattr(gate, "engine", "auto") == "openwakeword":
                return gate
            wanted = {voice_commands.normalize_utterance(p) for p in (self.wake_phrases or [])}
            return gate if voice_commands.normalize_utterance(gate.phrase) in wanted else None
        except Exception:
            return None

    def _all_wake_phrases(self):
        phrases = list(self.wake_phrases or [])
        gate = self._gate_in_use()
        if gate is not None:
            try:
                phrases.append(gate.phrase)
            except Exception:
                pass
        return phrases

    @staticmethod
    def _loud_barge_in(text) -> bool:
        try:
            threshold = float(getattr(config, "BARGE_IN_SNR_DB", 18.0) or 0.0)
            snr = getattr(text, "snr_db", None)
            if threshold <= 0 or snr is None:
                return False
            if not getattr(aec_processor, "enabled", False):
                return False
            return float(snr) >= threshold
        except Exception:
            return False

    _TTFA_SOURCE = {
        voice_commands.WAKE: "wake",
        voice_commands.WAKE_AND_COMMAND: "voice",
        voice_commands.COMMAND: "voice",
        voice_commands.STOP_AND_COMMAND: "voice",
        voice_commands.STOP: "stop",
    }

    def _begin_ttfa(self, decision, text):
        source = self._TTFA_SOURCE.get(decision.action)
        if source is None:
            return None
        try:
            return ttfa.tracker.begin(
                source, speech_end=getattr(text, "speech_end", None),
                captured_at=getattr(text, "captured_at", None),
                recognized_at=getattr(text, "recognized_at", None),
                heard=decision.text or str(text), stt=getattr(text, "engine", "") or None,
                wake_word=bool(getattr(text, "wake_detected", False)) or None,
                barge_in=bool(decision.interrupt) or None)
        except Exception:
            logger.debug("ttfa begin failed", exc_info=True)
            return None

    def handle_text(self, text):
        if text is None:
            return
        wake_detected = bool(getattr(text, "wake_detected", False))
        self._maybe_doze()
        if text == "" and self.mode == "SLEEPING" and self._is_bare_wake(getattr(text, "rejected_text", None)):
            # Tier 30: a lone wake word ("Baby") often scores below Whisper's
            # confidence floor and used to be thrown away - just the wake
            # word on its own is enough to wake up.
            logger.info(f"Mic: '{text.rejected_text}' was unsure but is a wake word - waking up")
            self.set_mode("AWAKE")
            return
        if text == "" and not wake_detected:
            if self.mode == "AWAKE":
                self._on_recognition_failed()
            return

        decision = decide(text, self.mode, self._all_wake_phrases(),
                           tts_active=bool(getattr(tts_engine, "is_speaking", False)),
                           wake_detected=wake_detected,
                           loud_barge_in=self._loud_barge_in(text))
        if self._unsure_noise(text, decision):
            return
        if decision.action in (voice_commands.COMMAND, voice_commands.WAKE_AND_COMMAND,
                               voice_commands.STOP_AND_COMMAND, voice_commands.WAKE):
            self._last_activity = time.monotonic()
        turn_id = self._begin_ttfa(decision, text)
        with trace.correlate(turn_id):
            if decision.interrupt:
                self._on_interruption()

            action = decision.action
            if action == voice_commands.WAKE:
                self.set_mode("AWAKE")
            elif action == voice_commands.WAKE_AND_COMMAND:
                self.set_mode("AWAKE", announce=False)
                self._on_text_ready(decision.text)
            elif action == voice_commands.SLEEP:
                self.set_mode("SLEEPING")
            elif action == voice_commands.STOP:
                self._on_stop_requested()
            elif action == voice_commands.STOP_AND_COMMAND:
                self._on_stop_requested()
                self._on_text_ready(decision.text)
            elif action == voice_commands.COMMAND:
                self._on_text_ready(decision.text)

    def _on_partial(self, text):
        if self.is_running and self.mode == "AWAKE" and text:
            self._on_partial_text(text)

    def _sync_captions(self):
        try:
            stt_engine.set_partial_callback(self._on_partial if self.mode == "AWAKE" else None)
        except Exception:
            logger.debug("set_partial_callback failed", exc_info=True)

    def run(self):
        self._on_mode_changed(self.mode, False)
        stt_engine.set_level_callback(self._on_audio_level)
        try:
            while self.is_running:
                self._sync_captions()
                gate = self._gate_in_use() if self.mode == "SLEEPING" else None
                stt_engine.wake_hint = self._wake_hint() if self.mode == "SLEEPING" else None
                text = stt_engine.listen(wake_gate=gate)
                if not self.is_running:
                    break
                if text is not None:
                    self._on_utterance_done()
                self.handle_text(text)
        finally:
            stt_engine.wake_hint = None
            stt_engine.set_level_callback(None)
            set_partial = getattr(stt_engine, "set_partial_callback", None)
            if callable(set_partial):
                set_partial(None)
            close = getattr(stt_engine, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("stt close failed", exc_info=True)

    def set_mode(self, new_mode, announce: bool = True):
        self.mode = new_mode
        if new_mode == "AWAKE":
            self._last_activity = time.monotonic()
        self._sync_captions()
        self._on_mode_changed(self.mode, announce)

    def stop(self):
        self.is_running = False


class WebAIStreamWorker(threading.Thread):
    """threading.Thread port of ui/workers.py's AIStreamWorker.

    Callbacks: on_text_chunk(str), on_response_ready(str),
    on_speech_started() (right before the first TTS call for this turn -
    only if speak_reply and not cancelled; Tier 25's avatar window listens
    for this to switch to its SPEAKING state), on_finished_speaking(bool).
    `.route` and `.cancelled` are readable once the thread has finished,
    same as the Qt version.
    """

    def __init__(self, brain, text, attachment=None, speak_reply: bool = True,
                 ttfa_turn: str = None, on_text_chunk=None, on_response_ready=None,
                 on_speech_started=None, on_finished_speaking=None):
        super().__init__(daemon=True)
        self.brain = brain
        self.text = text
        self.attachment = attachment
        self.speak_reply = speak_reply
        self.ttfa_turn = ttfa_turn
        self._cancel = threading.Event()
        self.route = None
        self._on_text_chunk = on_text_chunk or (lambda piece: None)
        self._on_response_ready = on_response_ready or (lambda full: None)
        self._on_speech_started = on_speech_started or (lambda: None)
        self._on_finished_speaking = on_finished_speaking or (lambda ok: None)

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def _think_stream(self):
        try:
            return self.brain.think_stream(self.text, attachment=self.attachment, cancel_event=self._cancel)
        except TypeError:
            return self.brain.think_stream(self.text, attachment=self.attachment)

    def run(self):
        with trace.correlate(self.ttfa_turn):
            self._run_inner()

    def _run_inner(self):
        parts = []
        inner = None
        failure = []

        def tee():
            nonlocal inner
            try:
                inner = self._think_stream()
                for piece in inner:
                    if self._cancel.is_set():
                        break
                    if not parts and self.ttfa_turn is not None:
                        ttfa.tracker.mark("first_token", turn_id=self.ttfa_turn)
                    parts.append(piece)
                    self._on_text_chunk(piece)
                    yield piece
            except Exception as e:
                logger.error(f"WebAIStreamWorker: the reply failed: {e}")
                failure.append(e)
            finally:
                close = getattr(inner, "close", None)
                if callable(close):
                    close()

        stream = tee()
        try:
            if self.speak_reply and not self._cancel.is_set():
                success = True
                self._on_speech_started()
                try:
                    success = tts_engine.speak_stream(speech_filter.filter_stream(stream))
                except Exception as e:
                    logger.error(f"Streaming TTS Error: {e}")
                    success = False
                finally:
                    self._on_finished_speaking(bool(success))
            for _ in stream:
                pass
        except Exception as e:
            logger.error(f"WebAIStreamWorker error: {e}")
            failure.append(e)
        finally:
            stream.close()
        if failure and not parts:
            parts.append("Something went wrong while I was replying.")

        try:
            row = trace.last_finished()
            self.route = (row or {}).get("route")
        except Exception:
            self.route = None
        if self.ttfa_turn is not None:
            ttfa.tracker.mark("reply_done", turn_id=self.ttfa_turn)
            if self.route:
                ttfa.tracker.annotate(turn_id=self.ttfa_turn, route=self.route)
        self._on_response_ready("".join(parts))


class WebReminderWorker(threading.Thread):
    """threading.Thread port of ui/workers.py's ReminderWorker - see this
    module's own docstring above for why a port, not a reimplementation.

    Before this class existed, the web UI had no equivalent of
    MainWindow._start_reminder_worker() running at all: a reminder set via
    chat was written to the DB correctly (brain/handlers/reminders.py is
    shared code, same skill either UI calls), but nothing ever polled for
    it coming due, so it silently never fired - indistinguishable from
    "never got set" from the user's side, which is exactly what was
    reported.

    Polls every `interval` seconds (config.REMINDER_CHECK_INTERVAL) for
    reminders now due and reminders due soon, in that order, same as
    desktop. Each reminder is marked (triggered / notified_upcoming) in
    the DB right before its callback fires, so a slow callback can never
    cause the same one to fire twice.

    Callbacks (both optional):
      on_due(text: str, reminder_id: int)
      on_upcoming(text: str, reminder_id: int, minutes_until: int)
    """

    def __init__(self, interval=None, on_due=None, on_upcoming=None):
        super().__init__(daemon=True)
        self.is_running = True
        self.interval = interval or config.REMINDER_CHECK_INTERVAL
        self._on_due = on_due or (lambda text, reminder_id: None)
        self._on_upcoming = on_upcoming or (lambda text, reminder_id, minutes_until: None)

    def run(self):
        while self.is_running:
            try:
                self._check_due()
                self._check_upcoming()
            except Exception as e:
                logger.error(f"Reminder check failed: {e}")
            # Slept in 1s steps so stop() takes effect quickly instead of
            # waiting out the rest of a potentially long interval.
            for _ in range(self.interval):
                if not self.is_running:
                    break
                time.sleep(1)

    def stop(self):
        self.is_running = False

    # ------------------------------------------------------------------
    @staticmethod
    def _compose_all(items, stage, extra_for):
        """items: list of (reminder_id, message, ...). extra_for(item) ->
        dict of composer.start()'s stage-specific kwargs for that item.
        Starts every composition first, then waits on all of them, so
        several reminders due in the same tick compose in parallel rather
        than one after another - see the class docstring above."""
        started = [
            (item, reminder_composer.start(item[1], stage=stage, **extra_for(item)))
            for item in items
        ]
        return [(item, waiter()) for item, waiter in started]

    def _check_due(self):
        due = db.get_due_reminders()  # (id, message, recurrence)
        if not due:
            return
        for (reminder_id, message, recurrence), result in self._compose_all(due, "due", lambda _i: {}):
            intent = ctx.classify(message)
            policy = ctx.effective_policy(intent, ctx.time_bucket())
            followup_at_iso = None
            if policy.post_due_followup:
                followup_at_iso = (datetime.now() + timedelta(minutes=config.REMINDER_FOLLOWUP_DELAY_MIN)).isoformat()
            db.complete_or_reschedule_reminder(reminder_id, recurrence=recurrence, followup_at_iso=followup_at_iso)
            self._on_due(result.text, reminder_id)

    def _check_upcoming(self):
        due_soon = db.get_reminders_due_soon(config.REMINDER_UPCOMING_WINDOW_MIN)  # (id, message, due_at)
        if not due_soon:
            return

        def extra(item):
            return {"minutes_until": ctx.minutes_until(item[2])}

        for (reminder_id, message, due_at), result in self._compose_all(due_soon, "upcoming", extra):
            db.mark_upcoming_notified(reminder_id)
            self._on_upcoming(result.text, reminder_id, ctx.minutes_until(due_at) or 0)
