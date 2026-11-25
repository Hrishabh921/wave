import speech_recognition as sr
import logging
import os
import threading
import time
from array import array
import pyaudio
from voice.tts import tts_engine
from voice.aec import aec_processor
from voice.buffers import get_vad
from voice import transcript_filter
from voice import endpoint_cues
# Tier 13: `import voice.language as language`, not `from voice import
# language` - the latter resolves via hasattr() on the already-cached
# `voice` package object, which skips re-importing the submodule even
# after a test pops "voice.language" from sys.modules, leaving every
# test sharing one stale LanguageTracker. This form's dotted name is
# looked up in sys.modules directly (same as voice.aec/voice.buffers
# below), so popping "voice.language" actually gets a fresh tracker.
import voice.language as language
import voice.speaker_id as speaker_id
from voice.endpoint import EndpointConfig
from settings.config import config

try:
    from voice.capture import CaptureEngine, HAS_SOUNDDEVICE as HAS_CAPTURE_STREAM
except Exception:   # pragma: no cover - capture.py only needs the stdlib + optional sounddevice
    CaptureEngine = None
    HAS_CAPTURE_STREAM = False

logger = logging.getLogger("STT")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from faster_whisper import WhisperModel
    HAS_FASTER_WHISPER = True
except ImportError:
    HAS_FASTER_WHISPER = False



class Transcript(str):
    """Tier 11: what listen() returns when something was heard - a plain
    `str` (so every existing caller's `if not text`, `.lower()`, `== ""`
    keeps working) carrying two extra facts the mic worker needs:

      wake_detected - an acoustic wake-word model (voice/wakeword.py) fired
                      on this audio, so it was addressed to Wave even if
                      the transcript doesn't contain the wake phrase.
      engine        - "whisper", "google", or "" if nothing was recognised.
      speech_end    - time.monotonic() estimate of when the speaker stopped
                      talking: capture end minus the recognizer's silence
                      timeout (it only knows you stopped once that much
                      silence has passed). None if unknown.
      captured_at   - time.monotonic() when capture ended.
      recognized_at - time.monotonic() when recognition finished.
      wake_score    - the wake-word model's score for this audio (0.0 if no
                      model ran).
      confidence    - Tier 12: Whisper's confidence 0-1 (None for Google).
      snr_db        - Tier 12: how far the speech stood above the room's
                      noise floor (continuous stream only, else None).
      tts_overlap   - Tier 12: Wave was talking while this was said.
      truncated     - Tier 12: the safety cap (STT_MAX_UTTERANCE_S) cut it.
      speaker_score - Tier 32: how much the voice sounded like the owner's
                      (voice/speaker_id.py), None if it wasn't checked.
    These timestamps feed telemetry/ttfa.py's time-to-first-audio numbers.
    """
    confidence = None
    snr_db = None
    tts_overlap = False
    truncated = False
    rejected_text = None      # Tier 30: what Whisper heard but scored too low ("" transcripts only)
    speaker_score = None

    def __new__(cls, text: str, wake_detected: bool = False, engine: str = "",
                speech_end: float = None, captured_at: float = None, recognized_at: float = None,
                wake_score: float = 0.0):
        obj = super().__new__(cls, text)
        obj.wake_detected = bool(wake_detected)
        obj.engine = engine
        obj.speech_end = speech_end
        obj.captured_at = captured_at
        obj.recognized_at = recognized_at
        obj.wake_score = wake_score
        return obj


def _pcm16_level(data: bytes) -> float:
    """0.0-1.0 loudness of a chunk of 16-bit PCM - RMS, square-root scaled
    so normal speech lands mid-range instead of hugging the bottom."""
    if not data:
        return 0.0
    try:
        if HAS_NUMPY:
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        else:
            samples = array("h")
            samples.frombytes(data[: len(data) // 2 * 2])
            rms = (sum(x * x for x in samples) / len(samples)) ** 0.5 if samples else 0.0
    except Exception:
        return 0.0
    return max(0.0, min(1.0, (rms / 6000.0) ** 0.5))


class _LevelTapStream:
    """Wraps speech_recognition's mic stream so every chunk the recognizer
    reads also reports its loudness (throttled to ~15/s) - the hook that
    lets the UI's waveform follow the real microphone instead of drawing
    random bars. Everything else is passed straight through."""

    def __init__(self, stream, on_level, min_interval: float = 0.066):
        self._stream = stream
        self._on_level = on_level
        self._min_interval = min_interval
        self._last = 0.0

    def read(self, size, *args, **kwargs):
        data = self._stream.read(size, *args, **kwargs)
        now = time.monotonic()
        if now - self._last >= self._min_interval:
            self._last = now
            try:
                self._on_level(_pcm16_level(data))
            except Exception:
                pass
        return data

    def __getattr__(self, name):
        return getattr(self._stream, name)


class SpeechReceiver:
    """Auto-Detects Headphones vs Speakers for Dynamic Sensitivity.

    🔧 Real AEC: energy_threshold/pause_threshold below decide WHEN to
    capture a phrase (endpointing) - they were also the only defense
    against hearing W.A.V.E.'s own voice through laptop speakers, which
    is a guess (raise the threshold while TTS is speaking and hope real
    speech is still louder than the echo). listen() now also runs every
    captured utterance through the shared AECProcessor (voice/aec.py),
    which does real reference-signal-based echo cancellation instead of
    guessing. The two are complementary, not redundant: AEC removes the
    correlated echo from the signal itself; the thresholds below still
    decide when a phrase starts/ends and remain a useful second layer
    (e.g. while AEC is disabled/unavailable, or simply as defense in
    depth) - left untouched on purpose rather than ripped out in favor of
    AEC alone without real-hardware testing to back that up.
    """
    
    def __init__(self):
        self.recognizer = sr.Recognizer()
        self.recognizer.non_speaking_duration = 0.3
        self.recognizer.dynamic_energy_threshold = False
        self.language = "en-IN"  # change at runtime via set_language()

        # 🔧 Phase 3: local faster-whisper model - NOT loaded here. Loading
        # it (even the small "base" model) takes real time and, on first
        # run, downloads several hundred MB - doing that in the constructor
        # would make every app launch pay that cost even for someone who
        # never installed faster-whisper or has it disabled. _get_whisper_model()
        # below loads it lazily, on the first utterance that actually needs
        # it, and caches the result (or the failure) for the rest of the
        # session so a broken install doesn't retry on every single thing said.
        self._whisper_model = None
        self._whisper_load_failed = False

        # 🔧 Tier 7: a real speech/non-speech check (voice/buffers.py) run
        # over a captured utterance before recognition sees it - see
        # _passes_vad() below. Built once here, same lazy-but-cached shape
        # as the Whisper/Piper model loaders elsewhere in this project,
        # though this one is cheap enough it doesn't need failure-caching.
        self._vad = get_vad()

        # Tier 11: optional callback(level: float 0-1) fed from the live mic
        # stream while listen() captures - see set_level_callback().
        self._level_callback = None

        # Tier 12: the continuous capture pipeline (voice/capture.py),
        # started on the first listen(); live captions; Whisper hygiene.
        self._capture = None
        self._capture_failed_at = -1e9
        self._partial_callback = None
        self._caption_thread = None
        self._captions_stop = threading.Event()
        self._whisper_lock = threading.Lock()
        self.last_confidence = None
        self.last_rejected_text = None
        # Tier 30: "Baby, Dodo, Hey Wave." while the mic worker waits for a
        # wake word (it sets/clears this) - leads Whisper's decoding prompt.
        self.wake_hint = None
        # Tier 13: Whisper's own guess at what language it just heard, used
        # to feed voice/language.py's tracker - see _recognize_with_whisper().
        self.last_detected_language = None
        self.last_language_probability = None

        self._calibrate(initial=True)

    def _calibrate(self, duration: float = 1.0, initial: bool = False):
        """The ambient-noise calibration that used to be inline in
        __init__ - pulled out into its own method (Tier 21) so
        recalibrate() can re-run the exact same measurement later, not
        just once at startup. Still never raises: a bad reading must not
        stop the app starting (the original FIX comment below), and must
        not crash a wizard page either."""
        logger.info("Calibrating mic for accurate recognition..." if initial else "Recalibrating mic...")
        try:
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=duration)
            # 🔧 FIX: this calibration result used to be thrown away - listen()
            # overwrote energy_threshold with fixed numbers every single time, so
            # a noisy room and a silent room got identical sensitivity. Keep what
            # calibration measured so listen() can take this room into account.
            self.baseline_energy = float(self.recognizer.energy_threshold)
        except Exception as e:
            if initial:
                self.baseline_energy = 0.0
            logger.warning(f"Mic calibration {'at startup' if initial else 'recalibration'} failed: {e}")
        logger.info(
            f"Calibrated ambient level: {self.baseline_energy:.0f} -> idle threshold "
            f"{self._scaled_threshold(200, 1.0, 500):.0f}, headphone threshold "
            f"{self._scaled_threshold(400, 2.0, 1000):.0f}"
        )

    def recalibrate(self) -> dict:
        """Tier 21: re-measures ambient mic noise on demand (same 1-second
        adjust_for_ambient_noise() call __init__ makes) and returns the
        fresh numbers - for the setup wizard's "Wake Word Calibration" step
        and anyone else who wants a fresh reading without restarting Wave.
        Blocking (~1s) - callers from the GUI thread should run this on a
        worker, same as every other mic/network call in this project."""
        self._calibrate(duration=1.0, initial=False)
        return {
            "baseline_energy": self.baseline_energy,
            "idle_threshold": self._scaled_threshold(200, 1.0, 500),
            "headphone_threshold": self._scaled_threshold(400, 2.0, 1000),
        }

    def _scaled_threshold(self, default: int, ratio: float, ceiling: int) -> float:
        """The hand-tuned `default` stays the FLOOR (so a quiet room behaves
        exactly as it always did). It only rises when this room's calibrated
        noise level says `default` would sit at or below the background
        hum - and is capped at `ceiling`, so one noisy moment during startup
        calibration can never leave the mic effectively deaf."""
        return max(default, min(self.baseline_energy * ratio, ceiling))

    def set_language(self, lang_code: str):
        """lang_code is a Google Speech Recognition language tag,
        e.g. 'hi-IN' for Hindi, 'en-US' for US English."""
        self.language = lang_code

    def is_headphone_connected(self) -> bool:
        """🚨 NAYA: Checks Windows audio to see if headphones are plugged in 🚨"""
        try:
            p = pyaudio.PyAudio()
            default_host_api = p.get_default_host_api_info()
            default_output = p.get_device_info_by_index(default_host_api.get('defaultOutputDevice'))
            device_name = default_output.get('name', '').lower()
            p.terminate()
            
            # Agar system ke audio device ke naam mein ye words hain, matlab headphone laga hai
            keywords = ['headphone', 'bluetooth', 'ear', 'airpods', 'buds', 'hands-free']
            return any(kw in device_name for kw in keywords)
        except Exception as e:
            return False

    def _whisper_language_code(self) -> str:
        """faster-whisper wants a bare ISO-639-1 code ('en', 'hi'), not the
        Google-style tag self.language holds ('en-IN', 'hi-IN') - this just
        narrows it down at the point faster-whisper is actually called.
        self.language itself stays untouched since set_language() also
        drives the UI's language dropdown in its original Google-tag form."""
        return (self.language or "en").split("-")[0].lower()

    def _get_whisper_model(self):
        """Lazily builds and caches the local faster-whisper model. Returns
        None - never raises - if the package isn't installed, the feature
        is disabled in Settings/.env, or the model can't be loaded for any
        other reason (e.g. no internet on first run to fetch weights), and
        remembers that failure so every subsequent utterance doesn't pay
        the cost of retrying a load that's already failed once this
        session."""
        if not HAS_FASTER_WHISPER or not config.FASTER_WHISPER_ENABLED:
            return None
        if self._whisper_load_failed:
            return None
        if self._whisper_model is not None:
            return self._whisper_model
        try:
            self._whisper_model = WhisperModel(
                config.FASTER_WHISPER_MODEL,
                device=config.FASTER_WHISPER_DEVICE,
                compute_type=config.FASTER_WHISPER_COMPUTE_TYPE,
            )
            logger.info(
                f"Loaded local faster-whisper model '{config.FASTER_WHISPER_MODEL}' "
                f"({config.FASTER_WHISPER_DEVICE}/{config.FASTER_WHISPER_COMPUTE_TYPE})."
            )
            return self._whisper_model
        except Exception as e:
            logger.warning(f"faster-whisper model load failed, staying on Google STT: {e}")
            self._whisper_load_failed = True
            return None

    def _recognize_with_whisper(self, audio: sr.AudioData, audio_s: float = None, snr_db: float = None):
        """Returns the recognized text (possibly "" if Whisper ran but
        heard nothing worth transcribing), or None if local Whisper isn't
        available/usable at all for this utterance - the caller falls back
        to Google exactly as it always did in that case. Never raises.

        Tier 12: the result goes through voice/transcript_filter.py's
        judge() - known hallucinations on short/quiet audio ("Thank you.",
        "Thanks for watching!"), bracketed tags, degenerate repetition and
        low confidence come back as "" (heard, but nothing usable) - and
        the confidence is left in self.last_confidence.

        Tier 13: the language passed to Whisper (and the decoding hint
        that goes with it) now comes from voice/language.py's tracker -
        the sticky "what language is this conversation in right now" -
        instead of self.language/Settings. On a fresh conversation
        (nothing decisive heard yet) that means `language=None`, i.e.
        Whisper detects it itself; self._whisper_language_code() stays
        for Google's fallback only. Whisper's own guess (and how sure it
        was) is left in self.last_detected_language/last_language_probability
        for _recognize() to feed back into the tracker once a verdict on
        the text itself exists."""
        self.last_confidence = None
        self.last_detected_language = None
        self.last_language_probability = None
        self.last_rejected_text = None
        if not HAS_NUMPY:
            return None
        model = self._get_whisper_model()
        if model is None:
            return None
        try:
            # faster-whisper wants mono 16kHz float32 PCM in [-1, 1].
            # AudioData.get_raw_data can resample/requantize for us, so the
            # conversion itself is just an int16 -> float32 rescale.
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            audio_np = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            # Tier 30: while Wave waits for its wake word, the wake words lead
            # the decoding prompt - short Hinglish words ("Dodo", "Suar") were
            # otherwise heard as Japanese/Chinese ("ドードー", "做了") or "So".
            prompt = language.tracker.initial_prompt()
            hint = getattr(self, "wake_hint", None)
            if hint:
                prompt = f"{hint} {prompt}".strip() if prompt else hint
            with self._whisper_lock:
                segments, _info = model.transcribe(
                    audio_np,
                    language=language.tracker.whisper_language(),
                    initial_prompt=prompt,
                    beam_size=int(getattr(config, "FASTER_WHISPER_BEAM_SIZE", 5)),
                    vad_filter=True,
                )
                segments = list(segments)
            self.last_detected_language = getattr(_info, "language", None)
            self.last_language_probability = getattr(_info, "language_probability", None)
            infos = [transcript_filter.SegmentInfo(
                text=getattr(seg, "text", ""),
                avg_logprob=float(getattr(seg, "avg_logprob", 0.0) or 0.0),
                no_speech_prob=float(getattr(seg, "no_speech_prob", 0.0) or 0.0),
                compression_ratio=float(getattr(seg, "compression_ratio", 1.0) or 1.0),
                duration_s=max(0.0, float(getattr(seg, "end", 0.0) or 0.0) - float(getattr(seg, "start", 0.0) or 0.0)),
            ) for seg in segments]
            text = "".join(seg.text for seg in segments).strip()
            verdict = transcript_filter.judge(
                text, infos, audio_s=audio_s if audio_s is not None else len(raw) / 32000.0, snr_db=snr_db,
                min_confidence=float(getattr(config, "WHISPER_MIN_CONFIDENCE", 0.35)))
            self.last_confidence = round(verdict.confidence, 3)
            if verdict.reason:
                logger.info(f"Whisper transcript {text[:50]!r}: {verdict.reason}")
                # Tier 30: kept for the mic worker - a lone wake word ("Baby")
                # often scores under the confidence floor and was thrown away.
                if not verdict.text and text:
                    self.last_rejected_text = text
            return verdict.text
        except Exception as e:
            logger.warning(f"Local Whisper transcription failed, falling back to Google: {e}")
            return None

    def _apply_aec(self, audio: sr.AudioData) -> sr.AudioData:
        """Runs a captured utterance through the shared AECProcessor's
        real echo cancellation (voice/aec.py) before recognition sees it.
        Falls back to the original, unmodified audio on any failure, or
        whenever AEC itself is disabled/unavailable (no aec-audio-processing
        install, no numpy, AEC_ENABLED=false) - recognition always gets
        SOME audio either way, exactly as before this existed.

        Legacy path only (Tier 12): this processes the whole captured
        phrase in one go, right after speech_recognition's listen() returns
        it. The continuous stream (voice/capture.py) cancels echo frame by
        frame instead and never calls this. Still a correct causal ordering
        for WebRTC APM - the far-end reference was fed in real time by
        tts.py before this runs - see voice/aec.py.
        """
        if not aec_processor.enabled:
            return audio
        try:
            raw = audio.get_raw_data(convert_rate=aec_processor.sample_rate, convert_width=2)
            cleaned = aec_processor.process_near_end(raw)
            return sr.AudioData(cleaned, aec_processor.sample_rate, 2)
        except Exception as e:
            logger.warning(f"AEC near-end processing failed, recognizing raw audio instead: {e}")
            return audio

    def _passes_vad(self, audio: sr.AudioData) -> bool:
        """Tier 7: a real speech/non-speech check (voice/buffers.py) over
        the WHOLE captured utterance, run after AEC and before recognition.
        Distinct from energy_threshold/pause_threshold in listen() below,
        which only decide WHEN recognizer.listen() considers a phrase
        "done" - not whether it ever contained speech at all. Catches a
        capture that completed (wasn't a plain WaitTimeoutError) but
        turned out to be pure silence/noise, which today gets treated
        exactly like real speech and sent to Whisper/Google regardless.

        Returns True (meaning: proceed to recognition) whenever the check
        itself can't run or fails for any reason, or when VAD_PRECHECK_ENABLED
        is off - a VAD bug must never SILENCE something that might be real
        speech. Worst case of a false "True" here is one wasted recognition
        call, exactly today's behaviour; worst case of a wrongly-returned
        False would be a dropped utterance, which this is written to avoid
        by construction rather than by hoping the check is always right."""
        if not config.VAD_PRECHECK_ENABLED:
            return True
        try:
            raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
            return self._vad.contains_speech(raw, 16000, 2)
        except Exception as e:
            logger.debug(f"VAD precheck failed, proceeding to recognition anyway: {e}")
            return True

    def set_level_callback(self, callback) -> None:
        """Tier 11: `callback(level)` (level 0.0-1.0) is called ~15 times a
        second with the loudness of the audio the mic is delivering while
        listen() is capturing - for a waveform that follows the real
        microphone. None turns it off. Called from the listening thread."""
        self._level_callback = callback
        if self._capture is not None:
            self._capture.set_level_callback(callback)

    def _local_recognizer_available(self) -> bool:
        return HAS_NUMPY and self._get_whisper_model() is not None

    def listen(self, wake_gate=None):
        """Returns:
        - a non-empty string: recognized speech, ready to act on
        - "" (empty string): audio WAS captured, but recognition failed
          (couldn't understand it, or the recognition service errored out)
        - None: plain silence - nothing was said, nothing went wrong
          (either the mic timed out with nothing captured, or something
          WAS captured but held no speech; both are the same "nothing said"
          outcome from a caller's point of view)

        The string returned is a Transcript (see the class above).

        `wake_gate` - the mic worker passes voice/wakeword.py's gate while
        Wave is ASLEEP: a captured phrase is wake-scored first and only
        transcribed when the wake word is in it (Tier 11).

        Tier 12: when `sounddevice` works, audio comes from ONE continuous
        microphone stream (voice/capture.py) with per-frame echo
        cancellation and streaming endpointing (voice/endpoint.py) - no
        fixed phrase cap, an exact end-of-speech time, a mic that recovers
        from being unplugged. Otherwise (CAPTURE_ENGINE=legacy, no
        sounddevice, or the stream won't open) speech_recognition's
        listen() is used exactly as before.
        """
        engine = self._stream_engine()
        if engine is not None:
            return self._listen_stream(engine, wake_gate)
        return self._listen_legacy(wake_gate)

    # ------------------------------------------------------------------
    # Tier 12: the continuous stream
    # ------------------------------------------------------------------
    def _endpoint_config(self) -> EndpointConfig:
        return EndpointConfig(
            end_silence_ms=int(getattr(config, "STT_END_SILENCE_MS", 450)),
            preroll_ms=int(getattr(config, "STT_PREROLL_MS", 300)),
            max_s=float(getattr(config, "STT_MAX_UTTERANCE_S", 60.0)),
            vad_aggressiveness=int(getattr(config, "VAD_AGGRESSIVENESS", 2)),
        )

    def _stream_engine(self):
        """The running CaptureEngine, starting it on first use - or None to
        use the legacy path (retried at most once a minute after a failure)."""
        mode = (getattr(config, "CAPTURE_ENGINE", "auto") or "auto").strip().lower()
        if mode == "legacy" or not HAS_CAPTURE_STREAM:
            return None
        if self._capture is not None and self._capture.running:
            return self._capture
        now = time.monotonic()
        if now - self._capture_failed_at < 60.0:
            return None
        try:
            if self._capture is None:
                self._capture = CaptureEngine(self._endpoint_config(), aec=aec_processor)
            self._capture.set_level_callback(self._level_callback)
            if self._capture.start():
                logger.info("Continuous mic stream started (Tier 12 capture pipeline)")
                self._start_caption_thread()
                return self._capture
            logger.warning(f"Continuous mic stream unavailable ({self._capture.last_error}) - "
                           "using speech_recognition's listen() instead")
        except Exception as e:
            logger.warning(f"Continuous mic stream failed to start ({e}) - using speech_recognition's listen()")
        self._capture_failed_at = now
        return None

    def close(self):
        """Releases the microphone (the mic worker calls this when it stops)."""
        self._captions_stop.set()
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass

    def _listen_stream(self, engine, wake_gate):
        tts_was_speaking = bool(tts_engine.is_speaking)
        utt = engine.next_utterance(timeout=config.STT_LISTEN_TIMEOUT_S)
        if utt is None:
            return None
        audio = sr.AudioData(utt.pcm, utt.sample_rate, 2)
        timing = {"speech_end": utt.speech_end, "captured_at": utt.ended_at}
        # Echo was cancelled frame by frame and the endpointer only emits
        # speech, so neither _apply_aec() nor _passes_vad() runs again here.
        result = self._gate_and_recognize(audio, wake_gate, tts_was_speaking or tts_engine.is_speaking,
                                          timing, audio_s=utt.duration_s, snr_db=utt.snr_db)
        if isinstance(result, Transcript):
            result.snr_db = utt.snr_db
            result.tts_overlap = tts_was_speaking
            result.truncated = utt.truncated
            result = self._continue_unfinished(engine, result)
        return result

    def _continue_unfinished(self, engine, first):
        """Tier 36: SARA-style adaptive endpointing, one step later. A
        transcript that sounds cut off mid-sentence ("mujhe kal subah...",
        "remind me to", "...aur") waits up to STT_CONTINUATION_WAIT_S for
        the rest - longer while the speaker is still talking - and the two
        are joined into one command (up to 3 joins). Not while Wave is
        talking. What follows goes through the speaker check like anything
        else, and isn't wake-gated: it's the rest of a command that was."""
        if not getattr(config, "STT_CONTINUATION", True):
            return first
        merged = first
        for _ in range(3):
            if merged.tts_overlap or tts_engine.is_speaking or not endpoint_cues.sounds_unfinished(str(merged)):
                return merged
            utt = self._next_part(engine, float(getattr(config, "STT_CONTINUATION_WAIT_S", 1.6)))
            if utt is None:
                logger.info(f"'{merged}' sounded unfinished, but nothing followed - going with it")
                return merged
            audio = sr.AudioData(utt.pcm, utt.sample_rate, 2)
            timing = {"speech_end": utt.speech_end, "captured_at": utt.ended_at}
            more = self._gate_and_recognize(audio, None, bool(tts_engine.is_speaking), timing,
                                            audio_s=utt.duration_s, snr_db=utt.snr_db)
            if not isinstance(more, Transcript) or not str(more).strip():
                return merged
            joined = Transcript(endpoint_cues.join(str(merged), str(more)), wake_detected=merged.wake_detected,
                                engine=merged.engine or more.engine, speech_end=more.speech_end,
                                captured_at=more.captured_at, recognized_at=more.recognized_at,
                                wake_score=merged.wake_score)
            confidences = [c for c in (merged.confidence, more.confidence) if c is not None]
            joined.confidence = min(confidences) if confidences else None
            joined.snr_db = merged.snr_db
            joined.tts_overlap = merged.tts_overlap
            joined.truncated = bool(merged.truncated or utt.truncated)
            joined.speaker_score = merged.speaker_score if merged.speaker_score is not None else more.speaker_score
            logger.info(f"Joined an unfinished sentence: '{merged}' + '{more}'")
            merged = joined
        return merged

    @staticmethod
    def _next_part(engine, wait_s: float):
        """The next utterance within wait_s - or, if the speaker is still
        talking when that runs out, once they stop (12 s at most)."""
        deadline = time.monotonic() + wait_s
        hard_stop = time.monotonic() + 12.0
        while True:
            utt = engine.next_utterance(timeout=max(0.05, deadline - time.monotonic()))
            if utt is not None:
                return utt
            now = time.monotonic()
            if getattr(engine, "speaking", False) and now < hard_stop:
                deadline = now + 0.5        # still talking: wait for that utterance to end
                continue
            if now < deadline:
                continue
            # One last look: an utterance can end (and be queued) just as
            # the wait above runs out.
            return engine.next_utterance(timeout=0.1)

    # ------------------------------------------------------------------
    # The original path
    # ------------------------------------------------------------------
    def _listen_legacy(self, wake_gate=None):
        tts_was_speaking = bool(tts_engine.is_speaking)
        with sr.Microphone() as source:
            if self._level_callback is not None and getattr(source, "stream", None) is not None:
                source.stream = _LevelTapStream(source.stream, self._level_callback)
            # 🚨 DYNAMIC SMART SENSITIVITY LOGIC 🚨
            if self.is_headphone_connected():
                # =============== HEADPHONE MODE ===============
                # No speaker-echo risk here, so we stay sensitive even
                # while W.A.V.E. is talking - a shorter pause threshold
                # while it's speaking makes an interruption register faster.
                self.recognizer.energy_threshold = self._scaled_threshold(400, 2.0, 1000)
                self.recognizer.pause_threshold = 0.35 if tts_engine.is_speaking else 0.5
            else:
                # =============== LAPTOP SPEAKER MODE ===============
                if tts_engine.is_speaking:
                    # Raised (not deafened) - just enough that W.A.V.E.'s
                    # own voice leaking through the speakers doesn't
                    # self-trigger, while a normal-volume interruption
                    # still gets through without needing to shout.
                    self.recognizer.energy_threshold = 1800
                    # Shorter pause threshold here too, so an interrupt
                    # attempt is recognized as "done talking" quickly.
                    self.recognizer.pause_threshold = 0.35
                else:
                    # Normal aawaz sunne ke liye sensitive ho jao
                    self.recognizer.energy_threshold = self._scaled_threshold(200, 1.0, 500)
                    # Speaker mode mein delay thoda zyada rakha taaki echo catch na ho
                    self.recognizer.pause_threshold = 0.6

            try:
                # Background listening
                # Tier 10: both limits come from settings/config.py now -
                # the phrase limit used to be a hard-coded 8 seconds.
                audio = self.recognizer.listen(
                    source,
                    timeout=config.STT_LISTEN_TIMEOUT_S,
                    phrase_time_limit=config.STT_PHRASE_TIME_LIMIT_S,
                )
                captured_at = time.monotonic()
                try:
                    pause_s = float(self.recognizer.pause_threshold)
                except (TypeError, ValueError):
                    pause_s = 0.0
                speech_end = captured_at - max(0.0, pause_s)
            except sr.WaitTimeoutError:
                return None  # nothing said in this window - completely normal
            except Exception as e:
                logger.warning(f"Mic capture failed: {e}")
                return None
            finally:
                if self._level_callback is not None:
                    try:
                        self._level_callback(0.0)   # capture over - let the waveform settle
                    except Exception:
                        pass

        # 🔧 Real AEC: strip W.A.V.E.'s own voice out of the captured audio
        # using the actual reference signal instead of only the energy
        # threshold guess above - see _apply_aec()/voice/aec.py. A no-op
        # passthrough whenever AEC isn't enabled/available.
        audio = self._apply_aec(audio)

        # 🔧 Tier 7: a completed capture that's actually pure silence/noise
        # (see _passes_vad()'s own docstring) is "nothing said", not "heard
        # audio but couldn't understand it" - and skips a wasted Whisper/
        # Google call. Checked AFTER AEC (so a cancelled-out echo doesn't
        # itself get misread as speech) and BEFORE recognition.
        if not self._passes_vad(audio):
            logger.info("Captured audio but no speech detected in it (VAD) - treating as silence")
            return None
        timing = {"speech_end": speech_end, "captured_at": captured_at}
        return self._gate_and_recognize(audio, wake_gate, tts_was_speaking or tts_engine.is_speaking, timing)

    # ------------------------------------------------------------------
    # Shared by both paths
    # ------------------------------------------------------------------
    def _gate_and_recognize(self, audio, wake_gate, tts_overlap: bool, timing: dict,
                            audio_s: float = None, snr_db: float = None):
        """The asleep-mode wake gate (Tier 11), then recognition, then the
        wake word's transcript check."""
        wake_detected = False
        allow_cloud = True
        if wake_gate is not None:
            if tts_overlap:
                # Asleep and Wave itself was talking (a reminder, "Standing
                # by."): the acoustic model can't tell Wave's own voice from
                # yours, so that audio is dropped rather than scored.
                logger.debug("Asleep and Wave was speaking - captured audio dropped, not wake-scored")
                return None
            if wake_gate.available:
                if not wake_gate.detect(audio):
                    return None   # asleep and no wake word in it: never transcribed
                wake_detected = True
                logger.info(f"Wake word detected (score {getattr(wake_gate, 'last_score', 0.0):.2f})")
            elif not config.STT_CLOUD_WHILE_ASLEEP and not self._local_recognizer_available():
                # Asleep, no wake model, and the only recognizer left is the
                # cloud one - don't send what was said to it just to look for
                # a wake phrase. (Hotkey / mic button still wake Wave.)
                return None
            else:
                allow_cloud = bool(config.STT_CLOUD_WHILE_ASLEEP)
        # Tier 32: whose voice is it? Anyone but the owner (the TV, a video,
        # other people) is dropped here, before it's even transcribed. Not
        # while Wave itself is talking: its echo is in that audio, and the
        # owner's "ruk jao" must always get through.
        speaker = self._speaker_check(audio)
        if speaker is not None and not speaker.is_owner and not tts_overlap:
            logger.info(f"Not your voice (speaker score {speaker.score:.2f}) - ignored, not transcribed")
            return None
        result = self._recognize(audio, wake_detected=wake_detected, allow_cloud=allow_cloud,
                                 audio_s=audio_s, snr_db=snr_db)
        if isinstance(result, Transcript):
            result.speech_end = timing.get("speech_end")
            result.captured_at = timing.get("captured_at")
            result.recognized_at = time.monotonic()
            if speaker is not None:
                result.speaker_score = speaker.score
                logger.info(f"Speaker check: {speaker.score:.2f} ({'you' if speaker.is_owner else 'unsure'})")
                # Clearly the owner, through this PC's mic, and not Wave's own
                # (cloned) voice: let the voiceprint learn from it.
                if str(result).strip() and not tts_overlap:
                    speaker_id.speaker_verifier.learn(speaker)
            if wake_detected:
                # Second stage (voice/wakeword.py's confirm()): a weak
                # detection must also LOOK like the wake phrase in the
                # transcript, which most false accepts (TV, other people)
                # don't.
                score = float(getattr(wake_gate, "last_score", 0.0) or 0.0)
                result.wake_score = score
                confirm = getattr(wake_gate, "confirm", None)
                if callable(confirm) and not confirm(str(result), score):
                    logger.info(f"Wake word ({score:.2f}) not confirmed by the transcript "
                                f"{str(result)[:40]!r} - ignoring")
                    return None
        return result

    @staticmethod
    def _speaker_check(audio):
        """Tier 32: voice/speaker_id.py's verdict on this audio, or None when
        there's nothing to compare it with (no speaker model, no voice
        sample, SPEAKER_VERIFY=off, too short) - then nothing changes."""
        try:
            raw = audio.get_raw_data(convert_rate=speaker_id.SAMPLE_RATE, convert_width=2)
        except Exception:
            return None
        return speaker_id.speaker_verifier.check(raw, speaker_id.SAMPLE_RATE)

    def _recognize(self, audio, wake_detected: bool = False, allow_cloud: bool = True,
                   audio_s: float = None, snr_db: float = None):
        """Whisper first, Google as the fallback when `allow_cloud`. Same
        three-way contract as listen(). Tier 12: Whisper's output goes
        through voice/transcript_filter.py (hallucinations, repetition
        loops, low confidence) and carries its confidence.

        Tier 13: every non-empty result (Whisper or Google) is fed to
        voice/language.py's tracker - the single place that updates the
        sticky "what language is this" state that the NEXT utterance's
        Whisper/Google calls (and voice/tts.py's voice choice) read."""
        try:
            text = self._recognize_with_whisper(audio, audio_s=audio_s, snr_db=snr_db)
            used_whisper = text is not None
            confidence = self.last_confidence if used_whisper else None
            if not used_whisper:
                if not allow_cloud:
                    return None
                text = self.recognizer.recognize_google(
                    audio, language=language.tracker.google_language(self.language))
            text = (text or "").strip()
            engine = "whisper" if used_whisper else "google"
            if text:
                language.tracker.observe(
                    text,
                    detected=self.last_detected_language if used_whisper else None,
                    probability=self.last_language_probability if used_whisper else None,
                )
            if not text:
                logger.info("Heard audio but couldn't make out any words in it")
                t = Transcript("", wake_detected=wake_detected, engine=engine)
                t.confidence = confidence
                t.rejected_text = getattr(self, "last_rejected_text", None) if used_whisper else None
                return t
            logger.info(f"Recognized ({engine}{f', {confidence:.2f}' if confidence is not None else ''}): {text}")
            t = Transcript(text, wake_detected=wake_detected, engine=engine)
            t.confidence = confidence
            return t
        except sr.UnknownValueError:
            logger.info("Heard audio but couldn't make out any words in it")
            return Transcript("", wake_detected=wake_detected)
        except sr.RequestError as e:
            # This is the free Google Web Speech API - no internet, or the
            # service is temporarily unreachable/rate-limiting us, lands here.
            logger.warning(f"Speech recognition service unreachable: {e}")
            return Transcript("", wake_detected=wake_detected)
        except Exception as e:
            logger.warning(f"Speech recognition failed unexpectedly: {e}")
            return Transcript("", wake_detected=wake_detected)

    # ------------------------------------------------------------------
    # Tier 12: live captions
    # ------------------------------------------------------------------
    def set_partial_callback(self, callback) -> None:
        """`callback(text)` receives a rough transcript of what you are
        saying WHILE you're still saying it (about every 0.8 s), when the
        continuous stream and a local Whisper model are both available.
        None turns it off. Called from a background thread."""
        self._partial_callback = callback

    def captions_available(self) -> bool:
        mode = (getattr(config, "LIVE_CAPTIONS", "auto") or "auto").strip().lower()
        if mode in ("off", "false", "0", "no"):
            return False
        if self._capture is None or not self._capture.running:
            return False
        if not HAS_NUMPY or self._get_whisper_model() is None:
            return False
        return mode in ("on", "true", "1", "yes") or (os.cpu_count() or 1) >= 4

    def _start_caption_thread(self):
        if self._caption_thread is not None and self._caption_thread.is_alive():
            return
        self._captions_stop.clear()
        self._caption_thread = threading.Thread(target=self._caption_loop, daemon=True, name="LiveCaptions")
        self._caption_thread.start()

    def _caption_loop(self):
        last_len = 0
        while not self._captions_stop.is_set():
            self._captions_stop.wait(0.15)
            cb = self._partial_callback
            engine = self._capture
            if cb is None or engine is None or not engine.running:
                continue
            if not engine.in_speech():
                last_len = 0
                continue
            if tts_engine.is_speaking:
                continue                      # what's heard over Wave's own voice isn't captioned
            pcm = engine.snapshot()
            seconds = len(pcm) / 32000.0
            if seconds < 0.6 or len(pcm) - last_len < 16000 or seconds > 15.0:
                continue                      # too short, too little new audio, or too long to redo cheaply
            if not self.captions_available():
                continue
            text = self._transcribe_partial(pcm)
            last_len = len(pcm)
            if text and engine.in_speech():
                try:
                    cb(text)
                except Exception:
                    logger.debug("partial callback failed", exc_info=True)

    def _transcribe_partial(self, pcm: bytes) -> str:
        """A quick greedy decode of the audio so far. Skipped (returns "")
        when the final recognition holds the model.

        Tier 13: reads the language tracker (same as the final
        recognition) but never calls .observe() itself - this is an
        in-progress utterance, and the final _recognize() call for the
        SAME utterance already reports it once it's done; observing
        twice would double-weight one utterance in the tracker's short
        history for no benefit."""
        model = self._get_whisper_model()
        if model is None or not self._whisper_lock.acquire(blocking=False):
            return ""
        try:
            audio_np = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _info = model.transcribe(audio_np, language=language.tracker.whisper_language(), beam_size=1,
                                               vad_filter=False, without_timestamps=True,
                                               condition_on_previous_text=False)
            text = "".join(seg.text for seg in segments).strip()
            return transcript_filter.judge(text, None, audio_s=len(pcm) / 32000.0).text
        except Exception:
            return ""
        finally:
            self._whisper_lock.release()


stt_engine = SpeechReceiver()
