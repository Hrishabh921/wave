"""
QThread worker classes used across Wave's UI.

Each of these runs a blocking or long-lived operation off the main GUI
thread (an API call, the continuous mic loop, OCR, clipboard polling, etc.)
so the window never freezes. Signals carry results back to the main thread
for MainWindow to handle.

Extracted from ui/main_window.py as part of the Phase 1 cleanup - no
behavior was changed, only the location of this code.
"""
import logging
import threading
import time
from datetime import datetime, timedelta

from PyQt6.QtCore import QThread, pyqtSignal

from voice.tts import tts_engine
from voice.ducking import ducker
from voice.stt import stt_engine
from voice import commands as voice_commands
from voice import speech_filter
from voice.commands import decide
from voice.wakeword import get_wake_gate
from voice.aec import aec_processor
from telemetry import trace, ttfa
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr
from monitoring.media_info import get_now_playing, seek_to
from database.db_manager import db
from settings.config import config
from web.web_ops import WebManager
from reminders import context as ctx
from reminders.composer import composer as reminder_composer

try:
    import pyperclip
except ImportError:
    pyperclip = None

logger = logging.getLogger("GUI")


class WeatherWorker(QThread):
    """One-shot fetch (.start() runs run() once, then the thread ends - this
    was never a polling loop and still isn't) via WebManager.get_weather(),
    which replaces the old bare requests.get() with the shared retrying
    session, response caching, and SSRF-safe URL validation from
    web/web_ops.py (🔧 Tier 3 / item 7). location defaults to
    config.WEATHER_DEFAULT_LOCATION, which is blank unless the user has set
    one - meaning wttr.in's own IP-based auto-detection, unchanged from the
    pre-Tier-3 behavior."""
    weather_ready = pyqtSignal(str)

    def __init__(self, location: str = None):
        super().__init__()
        self.location = location

    def run(self):
        location = self.location if self.location is not None else config.WEATHER_DEFAULT_LOCATION
        self.weather_ready.emit(WebManager.get_weather(location or None))


class TTSWorker(QThread):
    # 🔧 FIX: used to be pyqtSignal() with no payload, so a playback that
    # silently failed end-to-end (sounddevice AND pygame both unable to
    # produce audio) looked identical to a normal spoken reply - nothing
    # downstream could tell them apart. Now carries whether audio was
    # actually heard (see voice/tts.py's last_playback_ok); MainWindow's
    # on_speech_finished(success) surfaces a failure instead of staying
    # silent about it.
    finished_speaking = pyqtSignal(bool)

    def __init__(self, text, turn_id: str = None):
        super().__init__()
        self.text = text
        # Tier 19: this is the separate-thread TTS path (speak a finished
        # string, used for reminders/proactive nudges and anything else
        # that isn't AIStreamWorker's streaming reply) - optional because
        # not every caller has a turn to correlate with (e.g. a reminder
        # that never went through think()); trace.correlate(None) is a
        # no-op, so log lines simply show no turn id in that case.
        self.turn_id = turn_id

    def run(self):
        with trace.correlate(self.turn_id):
            success = True
            try:
                success = tts_engine.speak_and_wait(self.text)
            except Exception as e:
                logger.error(f"TTS Error: {e}")
                success = False
            finally:
                self.finished_speaking.emit(success)


class ContinuousMicWorker(QThread):
    """Listens in a loop and turns what it hears into actions.

    Tier 11: every decision goes through voice/commands.py's decide() -
    wake + command in one breath, the barge-in gate (unaddressed speech no
    longer cuts Wave off mid-reply), voice stop commands that cancel the
    turn in flight, and "new question: ...". While asleep the mic hands
    voice/wakeword.py's gate to stt_engine.listen(), so with a wake-word
    model installed nothing said near the mic is transcribed until the wake
    word is heard.

    Signals:
      text_ready(str)        - a command for the brain
      mode_update(str)       - mode changed ("AWAKE"/"SLEEPING"); kept for
                               existing listeners
      mode_changed(str, bool)- mode changed + whether to announce it ("Systems
                               active.") - False when a command came in the
                               same breath as the wake word
      interruption_signal()  - stop Wave's speech / current turn first
      stop_requested()       - a voice stop command: cancel the turn in flight
      recognition_failed()   - heard audio but couldn't make it out
      audio_level(float)     - live mic loudness 0-1, ~15/s while capturing
      partial_text(str)      - Tier 12: a rough live caption of what is being
                               said right now (AWAKE only; local Whisper +
                               the continuous stream only)
      utterance_done()       - Tier 12: one listen() came back (heard, failed
                               or ignored) - the UI clears its live caption

    Tier 12: the microphone stays open while this runs (voice/capture.py)
    and is released when it stops; loud, clear speech over Wave's reply can
    interrupt it without "Wave," (decide()'s `loud_barge_in`).
    """
    text_ready = pyqtSignal(str)
    mode_update = pyqtSignal(str)
    mode_changed = pyqtSignal(str, bool)
    interruption_signal = pyqtSignal()
    stop_requested = pyqtSignal()
    recognition_failed = pyqtSignal()
    audio_level = pyqtSignal(float)
    partial_text = pyqtSignal(str)
    utterance_done = pyqtSignal()

    def __init__(self, wake_phrases=None):
        super().__init__()
        self.is_running = True
        self.mode = "SLEEPING"
        self.wake_phrases = wake_phrases or ["hello wave", "wake up", "hey wave"]
        self.wake_gate = get_wake_gate()

    def set_wake_phrases(self, phrases):
        """Update wake phrases live - no restart needed."""
        self.wake_phrases = phrases

    def _gate_in_use(self):
        """The acoustic gate, if it should run while asleep: wanted by the
        config AND listening for the phrase the user picked in Settings. A
        "Hey Wave" model must not stand between the user and a wake phrase
        it was never trained on ("Okay Wave", a custom one) - those stay
        transcript-based. WAKE_WORD_ENGINE=openwakeword forces the gate."""
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

    def handle_text(self, text):
        """Acts on one listen() result. Split out of run() so the policy can
        be exercised without a microphone or a thread."""
        if text is None:
            return  # plain silence - nothing to do
        wake_detected = bool(getattr(text, "wake_detected", False))
        if text == "" and not wake_detected:
            # Audio was captured but couldn't be recognized (unclear
            # speech, or the recognition service failed) - only worth
            # telling the user about this while it's actively listening
            # for a command, not during silent standby.
            if self.mode == "AWAKE":
                self.recognition_failed.emit()
            return

        decision = decide(text, self.mode, self._all_wake_phrases(),
                          tts_active=bool(getattr(tts_engine, "is_speaking", False)),
                          wake_detected=wake_detected,
                          loud_barge_in=self._loud_barge_in(text))
        turn_id = self._begin_ttfa(decision, text)
        # Tier 19: tags THIS thread's log lines for however long handling
        # this one utterance takes (decide()'s outcome already computed
        # above - the raw recognition itself happens earlier in run(),
        # before any turn id exists yet to tag it with; see _begin_ttfa).
        # Whatever thread text_ready's connected slot actually runs on
        # (typically the GUI thread, via a queued connection) gets its OWN
        # tag from the same id downstream - this block only covers what
        # runs synchronously on the mic thread itself.
        with trace.correlate(turn_id):
            if decision.interrupt:
                self.interruption_signal.emit()

            action = decision.action
            if action == voice_commands.WAKE:
                self.set_mode("AWAKE")
            elif action == voice_commands.WAKE_AND_COMMAND:
                self.set_mode("AWAKE", announce=False)
                self.text_ready.emit(decision.text)
            elif action == voice_commands.SLEEP:
                self.set_mode("SLEEPING")
            elif action == voice_commands.STOP:
                self.stop_requested.emit()
            elif action == voice_commands.STOP_AND_COMMAND:
                self.stop_requested.emit()
                self.text_ready.emit(decision.text)
            elif action == voice_commands.COMMAND:
                self.text_ready.emit(decision.text)
            # IGNORE: nothing

    @staticmethod
    def _loud_barge_in(text) -> bool:
        """Tier 12: speech counts as a loud, deliberate barge-in only when it
        stood BARGE_IN_SNR_DB above the noise floor AND real echo
        cancellation was running - without AEC, Wave's own voice through the
        speakers could pass that test."""
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

    def _on_partial(self, text):
        """Called from stt's caption thread."""
        if self.is_running and self.mode == "AWAKE" and text:
            self.partial_text.emit(text)

    def _sync_captions(self):
        """Live captions only while AWAKE: asleep, nothing said near the mic
        is transcribed (the Tier 11 privacy rule), captions included."""
        try:
            stt_engine.set_partial_callback(self._on_partial if self.mode == "AWAKE" else None)
        except Exception:
            logger.debug("set_partial_callback failed", exc_info=True)

    _TTFA_SOURCE = {
        voice_commands.WAKE: "wake",
        voice_commands.WAKE_AND_COMMAND: "voice",
        voice_commands.COMMAND: "voice",
        voice_commands.STOP_AND_COMMAND: "voice",
        voice_commands.STOP: "stop",
    }

    def _begin_ttfa(self, decision, text):
        """Starts the time-to-first-audio clock (telemetry/ttfa.py) for a
        heard utterance that Wave will answer, from the moment the speaker
        stopped talking - the Transcript's timestamps are only available
        here, on the mic thread (a Qt str signal drops them). Returns the
        new turn id (Tier 19: also the id handle_text() tags this thread's
        log lines with - see trace.correlate()), or None when this
        decision doesn't start a turn (SLEEP/IGNORE) or ttfa is disabled."""
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

    def run(self):
        self.mode_update.emit(self.mode)
        self.mode_changed.emit(self.mode, False)
        stt_engine.set_level_callback(self.audio_level.emit)
        try:
            while self.is_running:
                self._sync_captions()
                gate = self._gate_in_use() if self.mode == "SLEEPING" else None
                text = stt_engine.listen(wake_gate=gate)
                if not self.is_running:
                    break
                if text is not None:
                    self.utterance_done.emit()
                self.handle_text(text)
        finally:
            stt_engine.set_level_callback(None)
            set_partial = getattr(stt_engine, "set_partial_callback", None)
            if callable(set_partial):
                set_partial(None)
            close = getattr(stt_engine, "close", None)
            if callable(close):
                try:
                    close()          # Tier 12: release the microphone
                except Exception:
                    logger.debug("stt close failed", exc_info=True)

    def set_mode(self, new_mode, announce: bool = True):
        self.mode = new_mode
        ducker.set_awake(new_mode == "AWAKE")   # Tier 39: other apps down while AWAKE
        self._sync_captions()
        self.mode_update.emit(self.mode)
        self.mode_changed.emit(self.mode, announce)

    def stop(self):
        self.is_running = False
        ducker.set_awake(False)                 # Tier 39: their volume back


class AIWorker(QThread):
    response_ready = pyqtSignal(str)

    def __init__(self, brain, text, attachment=None, turn_id: str = None):
        super().__init__()
        self.brain = brain
        self.text = text
        self.attachment = attachment
        # Tier 19: optional - this worker isn't constructed anywhere in the
        # live app today (AIStreamWorker is the real reply path), but kept
        # consistent with it so a future non-streaming call site can
        # correlate its log lines the same way.
        self.turn_id = turn_id

    def run(self):
        with trace.correlate(self.turn_id):
            reply = self.brain.think(self.text, attachment=self.attachment)
            self.response_ready.emit(reply)


class AIStreamWorker(QThread):
    """Tier 2 / Tier 11: one reply, streamed. Calls brain.think_stream()
    and (when speak_reply is True, the default) speaks each sentence as
    it's produced via voice/tts.py's speak_stream() instead of waiting for
    the whole reply before saying anything.

    Tier 11 made this THE reply path - MainWindow.send_message() uses it
    for typed and spoken messages alike (AIWorker + TTSWorker remain for
    anything that wants a finished string) - and added:

      * cancel(): ends the turn, not just the audio. The brain gets the
        same event as think_stream(cancel_event=...), so no tool runs for a
        turn the user called off, and this worker stops reading/showing
        further text. The partial reply is saved by the brain with its
        "(interrupted)" marker.
      * speech filtering: the voice gets voice/speech_filter.py's view of
        the text (no code blocks, no table pipes) while the bubble gets the
        raw markdown.
      * if the SPEECH is stopped without the turn being cancelled (say a
        reminder interrupts), the text still streams to the end - you just
        stop hearing it.
      * time-to-first-audio marks (telemetry/ttfa.py) for the turn whose id
        it was given.

    Signals: text_chunk(str) per piece as it arrives; response_ready(str)
    once at the end with everything shown (always fires); started_speaking()
    right before the first TTS call for this turn (only if speak_reply and
    not cancelled - Tier 25's avatar window listens for this to switch to
    its SPEAKING state); finished_speaking(bool) when speech is over (only
    if speak_reply); `route` (the brain's trace route for this turn) and
    `cancelled` are readable afterwards."""
    text_chunk = pyqtSignal(str)
    response_ready = pyqtSignal(str)
    started_speaking = pyqtSignal()
    finished_speaking = pyqtSignal(bool)

    def __init__(self, brain, text, attachment=None, speak_reply: bool = True, ttfa_turn: str = None):
        super().__init__()
        self.brain = brain
        self.text = text
        self.attachment = attachment
        self.speak_reply = speak_reply
        self.ttfa_turn = ttfa_turn
        self._cancel = threading.Event()
        self.route = None

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self):
        """Callable from any thread. Takes effect at the next piece of text
        (or before the next tool/LLM round, inside the brain)."""
        self._cancel.set()

    def _think_stream(self):
        try:
            return self.brain.think_stream(self.text, attachment=self.attachment, cancel_event=self._cancel)
        except TypeError:
            # A brain (or test double) without Tier 11's cancel_event.
            return self.brain.think_stream(self.text, attachment=self.attachment)

    def run(self):
        # Tier 19: one correlate() call covers this ENTIRE thread for this
        # turn - think_stream() below (router -> LLM -> tools, via its own
        # begin_turn()/end_turn() pair) adopts this same id automatically
        # (see trace._correlated_id()), and so does speak_stream() right
        # after it, since both run synchronously on this one thread. This
        # is what makes "TTS" show up correlated with the rest of the turn
        # for Wave's main reply path (AIWorker/TTSWorker's separate-thread
        # TTS is tagged independently - see TTSWorker.run()).
        with trace.correlate(self.ttfa_turn):
            self._run_inner()

    def _run_inner(self):
        parts = []
        inner = None
        failure = []

        def tee():
            # Pulled one piece at a time (by speak_stream's reader, or the
            # drain loop below) - each piece is recorded and shown live
            # before being handed onward, so text_chunk/response_ready
            # reflect exactly what was shown, in order. A failure in the
            # BRAIN is kept apart from a failure to play audio: it ends the
            # text cleanly here instead of surfacing as a playback error.
            nonlocal inner
            try:
                inner = self._think_stream()
                for piece in inner:
                    if self._cancel.is_set():
                        break
                    if not parts and self.ttfa_turn is not None:
                        ttfa.tracker.mark("first_token", turn_id=self.ttfa_turn)
                    parts.append(piece)
                    self.text_chunk.emit(piece)
                    yield piece
            except Exception as e:
                logger.error(f"AIStreamWorker: the reply failed: {e}")
                failure.append(e)
            finally:
                close = getattr(inner, "close", None)
                if callable(close):
                    close()

        stream = tee()
        try:
            if self.speak_reply and not self._cancel.is_set():
                success = True
                self.started_speaking.emit()
                try:
                    success = tts_engine.speak_stream(speech_filter.filter_stream(stream))
                except Exception as e:
                    logger.error(f"Streaming TTS Error: {e}")
                    success = False
                finally:
                    # Always - the UI resets its "Speaking..." state on it.
                    self.finished_speaking.emit(bool(success))
            # Whatever wasn't read yet (speech finished early, was stopped,
            # or there was no speech at all) is still shown, unless the turn
            # itself was cancelled - tee() stops on that by itself.
            for _ in stream:
                pass
        except Exception as e:
            logger.error(f"AIStreamWorker error: {e}")
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
        self.response_ready.emit("".join(parts))


class ClipboardWorker(QThread):
    clip_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.is_running = True
        # Settings > Privacy > "Enable clipboard monitoring" toggles this -
        # the loop below checks it before reading anything.
        self.monitoring_enabled = True

    def run(self):
        last_value = None
        while self.is_running:
            if self.monitoring_enabled:
                try:
                    current = pyperclip.paste()
                    if current and current != last_value:
                        last_value = current
                        self.clip_changed.emit(current)
                except Exception:
                    pass
            time.sleep(1)

    def stop(self):
        self.is_running = False


class ReminderWorker(QThread):
    """Polls the reminders table every config.REMINDER_CHECK_INTERVAL seconds
    for two things, in order, and emits one signal per reminder for each:

        reminder_due(text, reminder_id)
            - a reminder whose time has arrived. Unchanged signal shape from
              before Tier 3 - MainWindow's existing on_reminder_due handler
              needs no changes at all. `text` is no longer always the raw
              saved message, though - see _compose_all() below.
        reminder_upcoming(text, reminder_id, minutes_until)   [Tier 3 / item 8]
            - a quiet heads-up a few minutes before a reminder is due
              (config.REMINDER_UPCOMING_WINDOW_MIN), sent once per occurrence.

    🔧 Tier 18: the post-due follow-up check-in used to be a THIRD thing
    here (reminder_followup, _check_followups) - moved to
    automation/proactive.py's _check_reminder_followups() instead, and
    deleted from this class entirely (not kept as a thin wrapper). That
    module's own docstring draws the line explicitly: a reminder being
    due/upcoming is the user explicitly asking to be told something at a
    time THEY set, so it stays here; but the follow-up check-in is Wave
    noticing unprompted that some time has passed with no acknowledgment -
    exactly the "ambient, not asked-for" shape every other trigger in that
    engine already has. The due-ness check (db.get_due_followups()) and the
    composer.py-based wording are unchanged, just relocated.

    Every reminder is marked (triggered / notified_upcoming) in the DB
    itself, right before its signal is emitted, so a slow handler on the
    main thread can never cause the same one to fire twice - same guarantee
    the pre-Tier-3 version had.

    Wording comes from reminders/composer.py: an LLM reword with
    reminders/context.py's deterministic template as the fallback (or the
    only source, if config.CONTEXTUAL_REMINDER_LLM is off). composer.start()
    returns immediately and does the actual LLM call on its own background
    thread, so _compose_all() below starts every due reminder's composition
    before waiting on any of them - N reminders due in the same tick cost
    roughly one LLM round-trip's worth of wall-clock time, not N of them."""
    reminder_due = pyqtSignal(str, int)
    reminder_upcoming = pyqtSignal(str, int, int)

    def __init__(self, interval=None):
        super().__init__()
        self.is_running = True
        self.interval = interval or config.REMINDER_CHECK_INTERVAL

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
            self.reminder_due.emit(result.text, reminder_id)

    def _check_upcoming(self):
        due_soon = db.get_reminders_due_soon(config.REMINDER_UPCOMING_WINDOW_MIN)  # (id, message, due_at)
        if not due_soon:
            return

        def extra(item):
            return {"minutes_until": ctx.minutes_until(item[2])}

        for (reminder_id, message, due_at), result in self._compose_all(due_soon, "upcoming", extra):
            db.mark_upcoming_notified(reminder_id)
            self.reminder_upcoming.emit(result.text, reminder_id, ctx.minutes_until(due_at) or 0)

    # _check_followups moved to automation/proactive.py's
    # _check_reminder_followups() - see this class's own docstring above.


class ActionWorker(QThread):
    result_ready = pyqtSignal(str)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            result = self.func(*self.args)
            self.result_ready.emit(result if isinstance(result, str) and result else "Done.")
        except Exception as e:
            logger.error(f"ActionWorker error: {e}")
            self.result_ready.emit(f"Something went wrong: {e}")


class OCRWorker(QThread):
    result_ready = pyqtSignal(str)

    def run(self):
        try:
            path = VisionCapture.take_screenshot()
            if not path:
                self.result_ready.emit("Failed to capture the screen.")
                return
            text = ocr.extract_text(path)
            self.result_ready.emit(text)
        except Exception as e:
            logger.error(f"OCRWorker error: {e}")
            self.result_ready.emit(f"Error reading screen text: {e}")


class MediaInfoWorker(QThread):
    info_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.is_running = True

    def run(self):
        while self.is_running:
            info = get_now_playing() or {}
            self.info_ready.emit(info)
            self.msleep(1000)

    def stop(self):
        self.is_running = False


class MediaSeekWorker(QThread):
    def __init__(self, position_seconds: float):
        super().__init__()
        self.position_seconds = position_seconds

    def run(self):
        seek_to(self.position_seconds)