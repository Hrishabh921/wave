import logging
import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _dir_is_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".wave_write_test")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
        return True
    except OSError:
        return False


def resolve_dirs(frozen, meipass, executable, env, project_root, is_writable=_dir_is_writable):
    """(APP_DIR, DATA_DIR).

    APP_DIR  - where Wave's own code and bundled read-only resources live.
    DATA_DIR - where Wave writes: database, trash, screenshots, settings, themes,
               user_skills, the log. Must be user-writable.

    Running from source both are the project folder (nothing changes). In a
    PyInstaller build APP_DIR is the unpacked bundle (sys._MEIPASS - a temporary
    folder for --onefile, so writing there would lose everything on exit) and
    DATA_DIR is the folder holding the .exe if that's writable, otherwise
    %LOCALAPPDATA%\\WaveAI. WAVE_DATA_DIR overrides it in either case.
    """
    override = (env.get("WAVE_DATA_DIR") or "").strip()
    if not frozen:
        return project_root, (os.path.abspath(os.path.expanduser(override)) if override else project_root)
    exe_dir = os.path.dirname(os.path.abspath(executable))
    app_dir = meipass or exe_dir
    if override:
        return app_dir, os.path.abspath(os.path.expanduser(override))
    if is_writable(exe_dir):
        return app_dir, exe_dir
    base = env.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return app_dir, os.path.join(base, "WaveAI")


# -- Tier 5 / item 14: env-var validation ------------------------------------
# Every setting below used to be a bare int(os.getenv(...))/float(os.getenv(...))
# with no handling for a malformed value (a typo in .env crashed at import with
# a raw ValueError/TypeError) and no bounds or choice checking (a nonsense
# value like a negative timeout or an out-of-range percentage was accepted
# silently). These three helpers fix both: a bad raw value, or one outside the
# allowed range/choices, is recorded in _config_warnings and the documented
# default is used instead, so a typo in .env can never crash startup - it is
# reported once logging exists (see the bottom of this file) and Wave keeps
# running on safe defaults, the same "degrade, don't crash" philosophy already
# used for every optional dependency elsewhere in this file.
_config_warnings: list = []


def _int_env(name: str, default: str, *, min: int = None, max: int = None) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _config_warnings.append(f"{name}={raw!r} is not a whole number (expected one like {default!r})")
        return int(default)
    if min is not None and value < min:
        _config_warnings.append(f"{name}={value} is below the minimum of {min}")
        return int(default)
    if max is not None and value > max:
        _config_warnings.append(f"{name}={value} is above the maximum of {max}")
        return int(default)
    return value


def _float_env(name: str, default: str, *, min: float = None, max: float = None) -> float:
    raw = os.getenv(name, default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        _config_warnings.append(f"{name}={raw!r} is not a number (expected one like {default!r})")
        return float(default)
    if min is not None and value < min:
        _config_warnings.append(f"{name}={value} is below the minimum of {min}")
        return float(default)
    if max is not None and value > max:
        _config_warnings.append(f"{name}={value} is above the maximum of {max}")
        return float(default)
    return value


def _choice_env(name: str, default: str, choices: tuple) -> str:
    """Case-insensitive match against choices; returns the canonically-cased
    entry from `choices` (so callers can mix DEBUG/INFO-style or cpu/cuda-style
    choices and get that same casing back, regardless of how the user typed it)."""
    raw = os.getenv(name, default)
    for c in choices:
        if raw.strip().lower() == c.lower():
            return c
    _config_warnings.append(f"{name}={raw!r} is not one of {choices}")
    return default


class Config:
    """Central configuration for the Wave AI Assistant."""
    APP_NAME = "Wave AI"
    VERSION = "1.0.0"
    
    # API Keys
    GROQ_API_KEY = os.getenv("GROQ_API_KEY")

    # Local LLM fallback (Ollama) - used when Groq is unreachable, errors
    # out, or no GROQ_API_KEY is configured at all. Entirely optional: if
    # the 'ollama' package isn't installed or no local server is running,
    # Wave just skips straight to the existing deterministic fallback, same
    # as before this feature existed.
    OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1")
    OLLAMA_TIMEOUT = _int_env("OLLAMA_TIMEOUT", "20", min=1, max=3600)
    OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "5m")
    # Tier 19: lifecycle management for the above - starting `ollama serve`
    # itself if it isn't already running, and making sure OLLAMA_MODEL is
    # actually pulled, instead of silently failing every call until the user
    # notices and does both by hand (see brain/ollama_lifecycle.py). Entirely
    # a no-op, with nothing attempted at all, whenever the `ollama` binary
    # isn't on PATH - "not installed" stays exactly as free as it is today.
    OLLAMA_AUTOSTART = os.getenv("OLLAMA_AUTOSTART", "true").strip().lower() not in ("false", "0", "no")
    OLLAMA_AUTO_PULL_MODEL = os.getenv("OLLAMA_AUTO_PULL_MODEL", "true").strip().lower() not in ("false", "0", "no")
    OLLAMA_STARTUP_TIMEOUT_S = _float_env("OLLAMA_STARTUP_TIMEOUT_S", "15", min=1, max=120)
    OLLAMA_PULL_TIMEOUT_S = _float_env("OLLAMA_PULL_TIMEOUT_S", "1800", min=10, max=7200)

    # ── Tier 3 / item 7: web_ops hardening ──────────────────────────────
    # Shared requests.Session in web/web_ops.py - per-request timeout and
    # how many times urllib3's Retry will retry a 429/5xx before giving up
    # (with exponential backoff between attempts - see
    # web/web_ops.py's _build_session()).
    WEB_REQUEST_TIMEOUT_S = _float_env("WEB_REQUEST_TIMEOUT_S", "8", min=0.1, max=120)
    WEB_MAX_RETRIES = _int_env("WEB_MAX_RETRIES", "3", min=0, max=10)
    # Tier 19: per-host circuit breaker, ON TOP of the per-call retry
    # above - WEB_MAX_RETRIES/backoff governs ONE call's own attempts
    # against a host; this governs repeated CALLS (separate tool calls,
    # plan steps, turns) against the same host once it's already shown
    # itself to be down, so Wave stops paying a full retry+timeout cost
    # every single time. WEB_CIRCUIT_FAILURE_THRESHOLD consecutive
    # transport-level failures (a connection error, timeout, or a
    # 429/5xx response - never a 404 or other ordinary bad-URL response,
    # which says nothing about the HOST's health) opens the circuit for
    # that host for WEB_CIRCUIT_COOLDOWN_S seconds; a single success
    # anywhere in between resets the streak. See web/web_ops.py's
    # _CircuitBreaker.
    WEB_CIRCUIT_FAILURE_THRESHOLD = _int_env("WEB_CIRCUIT_FAILURE_THRESHOLD", "3", min=1, max=20)
    WEB_CIRCUIT_COOLDOWN_S = _float_env("WEB_CIRCUIT_COOLDOWN_S", "60", min=1, max=3600)
    # How long WebManager.get_weather()/.get_news() cache a successful
    # result (per distinct location/topic) before fetching again - see
    # web/web_ops.py's ttl_cache(). A failed fetch is never cached, so a
    # transient outage doesn't get "stuck" for the TTL window.
    WEATHER_CACHE_TTL_S = _float_env("WEATHER_CACHE_TTL_S", "600", min=0, max=86400)
    NEWS_CACHE_TTL_S = _float_env("NEWS_CACHE_TTL_S", "300", min=0, max=86400)
    # Optional home location ("Jaipur", "Mumbai, IN") for WeatherWorker when
    # no location is given per-call - leave blank to keep the pre-Tier-3
    # behavior of wttr.in's own IP-based auto-detection.
    WEATHER_DEFAULT_LOCATION = os.getenv("WEATHER_DEFAULT_LOCATION", "").strip()

    # ── Tier 16: summarize_page ──────────────────────────────────────────
    # Max characters of extracted page text WebManager.fetch_page_text()
    # returns before truncating. This text is handed to the LLM to
    # summarize (see brain/handlers/web.py's summarize_page), not shown to
    # the user raw, so it needs to be generous enough to carry a real
    # article but capped so one huge page can't blow out a prompt.
    WEB_SUMMARIZE_MAX_CHARS = _int_env("WEB_SUMMARIZE_MAX_CHARS", "6000", min=500, max=50000)

    # Reminders / alarms - how often (seconds) the background poller checks
    # for due reminders. Low enough to feel responsive, high enough not to
    # hammer SQLite for no reason.
    REMINDER_CHECK_INTERVAL = _int_env("REMINDER_CHECK_INTERVAL", "15", min=1, max=3600)

    # ── Tier 3 / item 8: reminders deepening ────────────────────────────
    # How long before a reminder is due to give a quiet heads-up (toast only,
    # no beep - see ui/workers.py's ReminderWorker and
    # ui/main_window.py's on_reminder_upcoming), and how long after a
    # reminder fires to gently check in (only for categories
    # reminders/context.py's effective_policy marks as follow-up-worthy -
    # most reminders never produce one of these).
    REMINDER_UPCOMING_WINDOW_MIN = _int_env("REMINDER_UPCOMING_WINDOW_MIN", "10", min=0, max=1440)
    REMINDER_FOLLOWUP_DELAY_MIN = _int_env("REMINDER_FOLLOWUP_DELAY_MIN", "20", min=0, max=1440)
    # Whether a due/upcoming/follow-up reminder gets its wording reworded by
    # the LLM (reminders/composer.py) before it's spoken/shown, with the
    # deterministic template from reminders/context.py as the fallback -
    # False = always use the template, no LLM call at all. Naming matches
    # the SARA reference project's CONTEXTUAL_REMINDER_* convention, since
    # reminders/context.py was modeled directly on it.
    CONTEXTUAL_REMINDER_LLM = os.getenv("CONTEXTUAL_REMINDER_LLM", "true").strip().lower() not in ("false", "0", "no")
    CONTEXTUAL_REMINDER_LLM_TIMEOUT_S = _float_env("CONTEXTUAL_REMINDER_LLM_TIMEOUT_S", "4.0", min=0.1, max=60)

    # Local speech recognition (faster-whisper) - used instead of the Google
    # Web Speech API when available, so voice commands keep working offline
    # and without sending audio to Google. Entirely optional: if the
    # 'faster-whisper' package isn't installed, this is disabled, or the
    # model fails to load (e.g. no internet on first run to fetch weights),
    # voice/stt.py falls straight back to the existing Google recognizer,
    # same as before this feature existed.
    FASTER_WHISPER_ENABLED = os.getenv("FASTER_WHISPER_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # "tiny"/"base"/"small"/"medium"/"large-v3" - bigger is more accurate and
    # slower. "base" is a reasonable default for a live voice assistant on
    # a CPU.
    FASTER_WHISPER_MODEL = os.getenv("FASTER_WHISPER_MODEL", "base")
    FASTER_WHISPER_DEVICE = _choice_env("FASTER_WHISPER_DEVICE", "cpu", ("cpu", "cuda", "auto"))
    # "int8" is the fast, low-memory choice for CPU. Use "float16" if
    # FASTER_WHISPER_DEVICE is "cuda".
    FASTER_WHISPER_COMPUTE_TYPE = os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "int8")

    # ── Tier 10: mic capture limits (voice/stt.py) ──────────────────────
    # STT_PHRASE_TIME_LIMIT_S: the longest a single spoken command may run
    # before capture stops. It used to be a hard-coded 8 seconds, which cut
    # longer requests ("remind me tomorrow at 9 to call the bank about the
    # card that...") off mid-sentence. STT_LISTEN_TIMEOUT_S: how long one
    # listen() call waits for speech to START before reporting "nothing
    # said" and looping again.
    STT_PHRASE_TIME_LIMIT_S = _float_env("STT_PHRASE_TIME_LIMIT_S", "15", min=2, max=120)
    STT_LISTEN_TIMEOUT_S = _float_env("STT_LISTEN_TIMEOUT_S", "1", min=0.2, max=30)

    # ── Tier 12: the continuous capture pipeline (voice/capture.py) ──────
    # One microphone stream that stays open, with echo cancellation on
    # every 20 ms frame, a tracked noise floor and exact end-of-speech
    # (voice/endpoint.py) - see docs/CAPTURE_PIPELINE.md. "auto" uses it
    # when 'sounddevice' works and falls back to speech_recognition's
    # listen() (the Tier 11 path, using the two limits above) otherwise;
    # "legacy" forces that old path.
    CAPTURE_ENGINE = _choice_env("CAPTURE_ENGINE", "auto", ("auto", "legacy"))
    # Silence that ends a phrase. Lower answers sooner but risks cutting you
    # off mid-thought; 450 ms is measured exactly (20 ms frames).
    STT_END_SILENCE_MS = _int_env("STT_END_SILENCE_MS", "450", min=200, max=3000)
    # Tier 36: a phrase that sounds unfinished ("...aur", "...ki", "remind me
    # to", a trailing "...") is held this long for the rest of the sentence,
    # and the two are joined (voice/endpoint_cues.py). false = off.
    STT_CONTINUATION = os.getenv("STT_CONTINUATION", "true").strip().lower() not in ("false", "0", "no")
    STT_CONTINUATION_WAIT_S = _float_env("STT_CONTINUATION_WAIT_S", "1.6", min=0.3, max=5)
    # Tier 37: names put in Whisper's prompt so it spells them right
    # (voice/vocabulary.py) - from Wave's memory and this list (comma-
    # separated: "Priya, Jaipur, Arijit Singh"). STT_VOCABULARY=off: neither.
    STT_VOCABULARY = _choice_env("STT_VOCABULARY", "auto", ("auto", "off"))
    STT_EXTRA_VOCABULARY = os.getenv("STT_EXTRA_VOCABULARY", "").strip()
    # Audio kept from just before speech was detected (no clipped first syllable).
    STT_PREROLL_MS = _int_env("STT_PREROLL_MS", "300", min=0, max=1000)
    # A safety net only - there is no fixed phrase cap any more.
    STT_MAX_UTTERANCE_S = _float_env("STT_MAX_UTTERANCE_S", "60", min=5, max=300)
    # A rough transcript in the chat while you are still talking. "auto" =
    # on with a local Whisper model and at least 4 CPU cores.
    LIVE_CAPTIONS = _choice_env("LIVE_CAPTIONS", "auto", ("auto", "on", "off"))
    # Talking over Wave WITHOUT saying "Wave" also interrupts it when the
    # speech is at least this far (dB) above the background - and only while
    # real echo cancellation is running, so Wave's own voice can't do it.
    # 0 = off (only "stop", "new question" and speech addressed to Wave do).
    BARGE_IN_SNR_DB = _float_env("BARGE_IN_SNR_DB", "18", min=0, max=60)
    # Whisper hygiene (voice/transcript_filter.py): below this average
    # confidence a transcript counts as "didn't catch that".
    WHISPER_MIN_CONFIDENCE = _float_env("WHISPER_MIN_CONFIDENCE", "0.35", min=0.0, max=1.0)
    # Tier 29 (web UI mic): while awake, a short (3 words or fewer) transcript
    # without the wake word, below this confidence, that the intent router
    # doesn't recognise is treated as room noise instead of a question for
    # the cloud model. 0 = off.
    AWAKE_MIN_CONFIDENCE = _float_env("AWAKE_MIN_CONFIDENCE", "0.5", min=0.0, max=1.0)
    # Tier 29 (web UI mic): after this many quiet seconds AWAKE quietly goes
    # back to waiting for the wake word, so a TV or a conversation in the
    # room can't keep firing commands. 0 = stay awake until told to sleep.
    AWAKE_IDLE_SLEEP_S = _float_env("AWAKE_IDLE_SLEEP_S", "60", min=0, max=3600)
    # 1 is faster, 5 (the default) slightly more accurate.
    FASTER_WHISPER_BEAM_SIZE = _int_env("FASTER_WHISPER_BEAM_SIZE", "5", min=1, max=10)

    # ── Tier 11: wake word (voice/wakeword.py) ────────────────────────────
    # An acoustic wake-word model, so a phrase captured while Wave is asleep
    # is checked by a small local model instead of being transcribed just to
    # search the text for "hey wave". "auto" uses openWakeWord when it's
    # installed (requirements-voice-local.txt) AND a model is configured or
    # present at voice_models/wakewords/hey_wave.onnx; otherwise the
    # transcript-based check is used, as before. See docs/WAKE_WORD.md.
    WAKE_WORD_ENGINE = _choice_env("WAKE_WORD_ENGINE", "auto", ("auto", "openwakeword", "transcript"))
    WAKE_WORD_MODEL_PATH = os.getenv("WAKE_WORD_MODEL_PATH", "").strip()
    # One of openWakeWord's pretrained phrases: hey_jarvis, alexa, hey_mycroft,
    # hey_rhasspy. Downloaded on first use; CC BY-NC-SA 4.0 (non-commercial).
    WAKE_WORD_MODEL_NAME = os.getenv("WAKE_WORD_MODEL_NAME", "").strip().lower()
    # Unset = use the model's own recommended threshold from its card
    # (voice_models/wakewords/hey_wave.json); 0.5 for a model without one.
    # Lower = wakes more easily (more false wakes), higher = stricter.
    WAKE_WORD_THRESHOLD = _float_env("WAKE_WORD_THRESHOLD", "0.5", min=0.05, max=0.99)
    WAKE_WORD_THRESHOLD_SET = bool(os.getenv("WAKE_WORD_THRESHOLD", "").strip())
    # Two-stage check: a detection scoring below this is only accepted if
    # the transcript also starts with something like the wake phrase. 0
    # turns the check off (every detection wakes Wave), 1 checks every one.
    # Unset = the model card's value (a "prefilter" model asks for 1.0),
    # else 0.85.
    WAKE_WORD_CONFIRM_BELOW = _float_env("WAKE_WORD_CONFIRM_BELOW", "0.85", min=0.0, max=1.0)
    WAKE_WORD_CONFIRM_BELOW_SET = bool(os.getenv("WAKE_WORD_CONFIRM_BELOW", "").strip())
    # The phrase the model listens for, stripped from the transcript before
    # the rest is treated as a command. Empty = derived from the model name.
    WAKE_WORD_PHRASE = os.getenv("WAKE_WORD_PHRASE", "").strip()
    WAKE_WORD_AUTO_DOWNLOAD = os.getenv(
        "WAKE_WORD_AUTO_DOWNLOAD", "true").strip().lower() not in ("false", "0", "no")
    # While Wave is ASLEEP and no local recognizer/wake model is available,
    # should a captured phrase be sent to the cloud (Google) recognizer just
    # to look for the wake phrase? Default no: asleep means nothing you say
    # leaves the machine. Wake Wave with the hotkey or the mic button then,
    # or install faster-whisper / a wake-word model (see docs/WAKE_WORD.md).
    STT_CLOUD_WHILE_ASLEEP = os.getenv(
        "STT_CLOUD_WHILE_ASLEEP", "false").strip().lower() not in ("false", "0", "no")

    # Semantic (embeddings-based) long-term memory - lets Wave surface
    # remembered facts that are conceptually related to what you just said,
    # not only ones that match by exact key. Entirely optional: if
    # 'sentence-transformers' isn't installed or fails to load, memory
    # falls back to the plain key/value behavior Wave always had.
    SEMANTIC_MEMORY_ENABLED = os.getenv("SEMANTIC_MEMORY_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # ── Phase 5: real Acoustic Echo Cancellation ────────────────────────
    # Replaces the old "just raise the mic's energy threshold while TTS is
    # speaking" guess with WebRTC's actual reference-signal-based echo
    # cancellation (see voice/aec.py). Entirely optional: if the
    # 'aec-audio-processing' package or numpy isn't installed, or this is
    # disabled, voice/stt.py falls straight back to the energy-threshold
    # heuristics alone, same as before this feature existed.
    AEC_ENABLED = os.getenv("AEC_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # WebRTC APM's internal rate - 16000 is its usual/best-supported rate;
    # audio is resampled to/from this as needed, so this rarely needs to
    # change.
    AEC_SAMPLE_RATE = _int_env("AEC_SAMPLE_RATE", "16000", min=8000, max=48000)
    # Rough estimate (ms) of the round-trip delay between audio reaching
    # the speaker and its echo arriving back at the mic - helps WebRTC's
    # delay estimator converge faster. The default is a reasonable
    # starting point for a laptop's built-in speakers/mic; raise it if
    # echo cancellation seems to lag behind in testing.
    AEC_STREAM_DELAY_MS = _int_env("AEC_STREAM_DELAY_MS", "80", min=0, max=2000)
    AEC_ENABLE_NS = os.getenv("AEC_ENABLE_NS", "true").strip().lower() not in ("false", "0", "no")
    AEC_ENABLE_AGC = os.getenv("AEC_ENABLE_AGC", "false").strip().lower() not in ("false", "0", "no")
    AEC_ENABLE_VAD = os.getenv("AEC_ENABLE_VAD", "false").strip().lower() not in ("false", "0", "no")

    # ── Phase 5: local neural TTS fallback (optional) ───────────────────
    # Replaces the old robotic Windows SAPI voice as the FIRST offline
    # fallback (edge-tts has no internet) with a local neural voice
    # (Piper) if the optional 'piper-tts' package is installed and
    # TTS_LOCAL_MODEL_PATH points at a downloaded .onnx voice - see
    # voice/tts.py's HAS_PIPER block for why this isn't in
    # requirements.txt. Falls straight back to SAPI exactly as before if
    # either isn't set up.
    TTS_LOCAL_ENABLED = os.getenv("TTS_LOCAL_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    TTS_LOCAL_MODEL_PATH = os.getenv("TTS_LOCAL_MODEL_PATH", "")

    # ── Tier 13: auto-download a Piper voice instead of making you find and
    # set TTS_LOCAL_MODEL_PATH by hand ─────────────────────────────────────
    # Default false - a second, deliberate opt-in on top of piper-tts
    # itself not being in requirements.txt (see voice/tts.py's HAS_PIPER
    # block): this is this project's consent mechanism for a ~60MB network
    # download, so turning it on IS the consent - see
    # VoiceEngine._confirm_piper_download()'s docstring. Only reached when
    # TTS_LOCAL_MODEL_PATH is blank (a model path you set yourself always
    # wins, and a broken one still fails as a real error rather than
    # silently substituting a download). The file is checked against a
    # pinned sha256 after downloading - see voice/tts.py's
    # PIPER_VOICE_CHECKSUMS and docs/VOICE_LOCAL.md.
    TTS_LOCAL_AUTO_DOWNLOAD = os.getenv(
        "TTS_LOCAL_AUTO_DOWNLOAD", "false").strip().lower() not in ("false", "0", "no")
    TTS_LOCAL_DEFAULT_VOICE = os.getenv("TTS_LOCAL_DEFAULT_VOICE", "en_US-lessac-medium").strip()
    # Blank = <data dir>/voice_models/piper (voice/tts.py computes that
    # default itself - DATA_DIR isn't assigned yet at this point in this
    # file - see _default_piper_voices_dir()).
    TTS_LOCAL_VOICES_DIR = os.getenv("TTS_LOCAL_VOICES_DIR", "").strip()
    TTS_LOCAL_DOWNLOAD_TIMEOUT_S = _float_env("TTS_LOCAL_DOWNLOAD_TIMEOUT_S", "45", min=5, max=600)

    # ── Tier 13: Kokoro - a SECOND, genuinely bilingual local voice ─────
    # Piper above is English-only (see voice/tts.py's HAS_PIPER block and
    # _speak_offline_fallback()'s docstring) - Kokoro
    # (https://github.com/thewh1teagle/kokoro-onnx, `pip install
    # kokoro-onnx`) ships real Hindi voices, so it's the one offline path
    # that can actually speak a classified-Hindi chunk in Hindi instead
    # of silently mispronouncing it in an English voice. Off by default:
    # CPU synthesis was measured here at roughly 2x real time for both
    # languages (see docs/VOICE_LOCAL.md) - a genuine latency tradeoff,
    # not a free upgrade, so someone with a Hindi-heavy offline workflow
    # opts in deliberately, same posture as TTS_PREFER_LOCAL_VOICE below.
    TTS_KOKORO_ENABLED = os.getenv(
        "TTS_KOKORO_ENABLED", "false").strip().lower() not in ("false", "0", "no")
    # Separate opt-in from TTS_LOCAL_AUTO_DOWNLOAD above: Kokoro's two
    # fixed model files are a bigger (~120MB) download than Piper's one
    # voice, so this is its own deliberate consent, not folded into
    # Piper's flag. Checked against a pinned sha256 after downloading -
    # see voice/tts.py's KOKORO_FILE_CHECKSUMS and docs/VOICE_LOCAL.md.
    TTS_KOKORO_AUTO_DOWNLOAD = os.getenv(
        "TTS_KOKORO_AUTO_DOWNLOAD", "false").strip().lower() not in ("false", "0", "no")
    # Blank = <data dir>/voice_models/kokoro (voice/tts.py computes that
    # default itself - DATA_DIR isn't assigned yet at this point in this
    # file - see _default_kokoro_model_dir()).
    TTS_KOKORO_MODEL_DIR = os.getenv("TTS_KOKORO_MODEL_DIR", "").strip()
    # kokoro-onnx's own voice names (Kokoro.get_voices()) - af_heart is
    # one of its American-English voices; hf_alpha one of its two Hindi
    # female voices (hm_omega/hm_psi are the male ones).
    TTS_KOKORO_VOICE_EN = os.getenv("TTS_KOKORO_VOICE_EN", "af_heart").strip()
    TTS_KOKORO_VOICE_HI = os.getenv("TTS_KOKORO_VOICE_HI", "hf_alpha").strip()
    TTS_KOKORO_DOWNLOAD_TIMEOUT_S = _float_env("TTS_KOKORO_DOWNLOAD_TIMEOUT_S", "60", min=5, max=1200)

    # ── Tier 7: opt INTO local-first, for someone who already opted into
    # Piper above ───────────────────────────────────────────────────────
    # The two settings above make Piper a FALLBACK (tried only once
    # edge-tts/the internet isn't available). TTS_PREFER_LOCAL_VOICE
    # (default false - nothing changes out of the box) flips that order
    # for someone who has ALREADY installed piper-tts and set
    # TTS_LOCAL_MODEL_PATH: Piper is tried FIRST, edge-tts only as the
    # fallback if Piper isn't usable right now. This is still only ever
    # reached by someone who made Piper's own opt-in choice already (see
    # voice/tts.py's HAS_PIPER block for the Windows-wheel/GPL-3.0/
    # onnxruntime-weight reasons it isn't a default), so it adds no new
    # licensing exposure - it just lets that choice actually produce
    # "local-first" behaviour instead of "local only once the cloud path
    # already failed".
    TTS_PREFER_LOCAL_VOICE = os.getenv(
        "TTS_PREFER_LOCAL_VOICE", "false").strip().lower() not in ("false", "0", "no")

    # ── Tier 25: local voice CLONING from the user's own reference sample ──
    # Coqui TTS (https://github.com/idiap/coqui-ai-TTS, `pip install
    # coqui-tts`) ships XTTS-v2, a multilingual (Hindi included) few-shot
    # voice-cloning model: give it ~10-30s of one person's own voice and it
    # speaks any text back in that same voice. This is the whole point of
    # the avatar companion (docs/ROADMAP_BEAT_SARA.md's Tier 25) - once a
    # reference clip is set up (Settings -> Avatar page's "Build avatar"
    # button, which calls ui/main_window.py's build_avatar_from_files()),
    # Wave's spoken replies use the user's OWN voice instead of
    # edge-tts/Piper/Kokoro's stock ones - see voice/tts.py's
    # _speak_via_clone(), tried FIRST, ahead of even TTS_PREFER_LOCAL_VOICE
    # above, since a user who went to the trouble of cloning their voice
    # obviously wants it actually used, not held as a third-choice fallback.
    #
    # Same NOT-in-requirements.txt, explicit-opt-in posture as Piper/Kokoro
    # above, for a bigger reason than either: coqui-tts pulls in torch (a
    # CPU build is still several hundred MB) and downloads the XTTS-v2
    # checkpoint (~1.8GB) on first use - real weight that must never be
    # forced on an install that never asked for it. Unlike Piper/Kokoro
    # there's no separate TTS_CLONE_AUTO_DOWNLOAD consent flag: providing
    # an actual reference voice sample (TTS_CLONE_REFERENCE_PATH, below) IS
    # that deliberate opt-in choice already - there's nothing further to
    # confirm.
    TTS_CLONE_ENABLED = os.getenv(
        "TTS_CLONE_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Set at runtime by build_avatar_from_files() once the user provides a
    # voice sample (also settable via this env var for a headless/scripted
    # setup) - blank means "no reference yet", in which case the cloning
    # path is skipped entirely regardless of TTS_CLONE_ENABLED above (see
    # voice/tts.py's _get_clone_engine()).
    TTS_CLONE_REFERENCE_PATH = os.getenv("TTS_CLONE_REFERENCE_PATH", "").strip()
    # XTTS-v2's own model name (Coqui TTS's model-zoo naming), not a file
    # path - the ~1.8GB checkpoint itself is cached by the `TTS` package in
    # its own cache dir on first use, like any other Hugging-Face-style
    # model download.
    TTS_CLONE_MODEL_NAME = os.getenv(
        "TTS_CLONE_MODEL_NAME", "tts_models/multilingual/multi-dataset/xtts_v2").strip()
    # Tier 31: where XTTS-v2 runs (in its own process, voice/clone_worker.py).
    # "auto" = the NVIDIA GPU when the installed PyTorch has CUDA, else the
    # CPU; "cpu" / "cuda" force one. Measured on the user's laptop
    # (i5-13450HX, RTX 3050 6GB): a sentence takes ~1.8x its own length to
    # synthesize on the CPU and ~0.4x on the GPU, so on the CPU replies start
    # several seconds later than with edge-tts. The default PyTorch wheel is
    # CPU-only; the CUDA one is a separate ~1.9GB install (docs/VOICE_LOCAL.md).
    TTS_CLONE_DEVICE = os.getenv("TTS_CLONE_DEVICE", "auto").strip().lower() or "auto"
    # Tier 31: both launchers opt Wave's process out of Windows 11's power
    # throttling (EcoQoS), which otherwise made the voice maths (cloned
    # voice, Whisper) ~15x slower a second into any sustained work while
    # Wave's window wasn't in front - see monitoring/power.py.
    POWER_THROTTLING_OPT_OUT = os.getenv(
        "POWER_THROTTLING_OPT_OUT", "true").strip().lower() not in ("false", "0", "no")

    # ── Tier 32: only the owner's voice (voice/speaker_id.py) ────────────
    # Every utterance's VOICE is compared with the owner's voiceprint - their
    # voice training through this PC's mic (scripts/train_my_voice.py) plus
    # what Wave learns from their commands; other voices - the TV, a video,
    # other people - are dropped before they're even transcribed. "auto" = on
    # whenever the speaker model (scripts/download_speaker_model.py) and a
    # voice training are there; "off" = every voice counts, as before.
    SPEAKER_VERIFY = _choice_env("SPEAKER_VERIFY", "auto", ("auto", "off"))
    # 0-1 voice similarity needed to count as the owner. Measured on the
    # owner's laptop after voice training: their held-out 1.5 s pieces
    # 0.71-0.74, a video's voices at most 0.21. Utterances under 1 s aren't
    # checked (too little to go on).
    SPEAKER_VERIFY_THRESHOLD = _float_env("SPEAKER_VERIFY_THRESHOLD", "0.35", min=0.0, max=1.0)
    # Blank = <data dir>/voice_models/speaker/<the CAM++ model file>.
    SPEAKER_MODEL_PATH = os.getenv("SPEAKER_MODEL_PATH", "").strip()

    # ── Tier 27: lifelike avatar animation (web UI's Dodo page) ─────────
    # LivePortrait (github.com/KwaiVGI/LivePortrait) re-draws the avatar
    # photo with real blinks, eye movement, breathing and a talking mouth.
    # It needs an NVIDIA GPU and CUDA PyTorch in its OWN Python environment
    # (gigabytes - never in requirements.txt), and is too slow on a laptop
    # GPU to run live, so avatar/live_render.py renders the clips once
    # (Settings -> Avatar -> "Render lifelike animation", a few minutes)
    # and the page plays them back. Point these at that environment's
    # python.exe and the LivePortrait checkout (with pretrained_weights);
    # left blank, the Dodo page keeps the simple 5-frame avatar.
    AVATAR_LIVE_PYTHON = os.getenv("AVATAR_LIVE_PYTHON", "").strip()
    AVATAR_LIVE_LP_DIR = os.getenv("AVATAR_LIVE_LP_DIR", "").strip()

    # ── Tier 7: voice activity detection (voice/buffers.py) ──────────────
    # A real speech/non-speech check run over audio AFTER stt.py finishes
    # capturing it, so a completed capture that turned out to be pure
    # silence/noise reads as "nothing said" (and skips a wasted Whisper/
    # Google call) instead of being assumed to contain speech just because
    # recognizer.listen() didn't time out. VAD_BACKEND="energy" forces the
    # always-available zero-dependency backend even if the optional
    # 'webrtcvad' package is installed; VAD_PRECHECK_ENABLED=false turns
    # the whole check off (every captured utterance goes straight to
    # recognition, the exact pre-Tier-7 behaviour).
    VAD_PRECHECK_ENABLED = os.getenv(
        "VAD_PRECHECK_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    VAD_BACKEND = os.getenv("VAD_BACKEND", "auto").strip().lower()  # "auto" | "energy"
    VAD_AGGRESSIVENESS = _int_env("VAD_AGGRESSIVENESS", "2", min=0, max=3)
    VAD_ENERGY_THRESHOLD = _float_env("VAD_ENERGY_THRESHOLD", "150.0", min=0.0, max=32767.0)

    # ── Tier 3 item #7: TTS phrase cache (voice/tts_cache.py) ────────────
    # Skips the edge-tts network round-trip entirely for a (text, voice,
    # rate) combination already spoken once - see that module's own
    # docstring for the full reasoning, including why the default
    # TTS_PHRASE_CACHE_MAXLEN is well above SARA's reference 40 characters.
    # Only the primary edge-tts path is cached; the already-fully-local
    # Piper/SAPI offline fallbacks are unchanged.
    TTS_PHRASE_CACHE_ENABLED = os.getenv(
        "TTS_PHRASE_CACHE_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    TTS_PHRASE_CACHE_SIZE = _int_env("TTS_PHRASE_CACHE_SIZE", "64", min=1, max=1000)
    TTS_PHRASE_CACHE_MAXLEN = _int_env("TTS_PHRASE_CACHE_MAXLEN", "400", min=1, max=5000)

    # ── Tier 11: streamed-reply pipeline (voice/tts.py) ──────────────────
    # TTS_PREFETCH_ENABLED: while one sentence of a streamed reply plays,
    # the next ones are already being synthesized, so there's no network
    # pause between sentences. TTS_PREFETCH_WAIT_S: the longest playback
    # waits for a prefetch still in flight before synthesizing itself.
    # TTS_WARMUP_ENABLED: initialise the audio output once at start-up
    # (background thread) so the first reply doesn't pay for it.
    TTS_PREFETCH_ENABLED = os.getenv(
        "TTS_PREFETCH_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    TTS_PREFETCH_WAIT_S = _float_env("TTS_PREFETCH_WAIT_S", "15", min=1, max=120)
    TTS_WARMUP_ENABLED = os.getenv(
        "TTS_WARMUP_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Tier 12 (voice/audio_out.py): one output stream kept open while Wave
    # talks, so sentences play back to back and echo cancellation gets a
    # continuous reference. Closed again after a minute without playback.
    TTS_PERSISTENT_STREAM = os.getenv(
        "TTS_PERSISTENT_STREAM", "true").strip().lower() not in ("false", "0", "no")

    # Directory Paths
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    FROZEN = bool(getattr(sys, "frozen", False))
    APP_DIR, DATA_DIR = resolve_dirs(FROZEN, getattr(sys, "_MEIPASS", None), sys.executable,
                                     os.environ, PROJECT_ROOT)
    BASE_DIR = APP_DIR          # read-only resources (tesseract, assets); name kept for existing imports
    DB_DIR = os.path.join(DATA_DIR, "database")
    DB_PATH = os.path.join(DB_DIR, "wave_memory.db")

    # ── Phase 4: LLM provider layer ─────────────────────────────────────
    # The provider itself (LLM_PROVIDER / LLM_API_KEY / LLM_BASE_URL /
    # LLM_MODEL / LLM_VISION_MODEL / LLM_TIMEOUT / LLM_REASONING_EFFORT) is
    # resolved straight from the environment by brain/llm_provider.py, so
    # there's exactly one place that knows the rules. GROQ_API_KEY above is
    # still honoured unchanged: with no LLM_* variables set at all, Wave
    # behaves exactly as it did before this layer existed.

    # ── Phase 4: drop-in skills with hot reload ─────────────────────────
    # Any .py file dropped into this folder that uses @skill(...) becomes a
    # tool the moment it's saved - no restart. See brain/skill_loader.py and
    # user_skills/_example_skill.py. Files starting with "_" are ignored.
    USER_SKILLS_DIR = os.getenv("WAVE_USER_SKILLS_DIR") or os.path.join(DATA_DIR, "user_skills")
    # Set to false to still load the folder once at startup but stop watching it.
    SKILLS_HOT_RELOAD = os.getenv("SKILLS_HOT_RELOAD", "true").strip().lower() not in ("false", "0", "no")
    SKILLS_POLL_INTERVAL = _float_env("SKILLS_POLL_INTERVAL", "1.5", min=0.1, max=60)

    # ── Tier 28: how many tools go out with each LLM request ────────────
    # Wave has 180+ tools. Groq and OpenAI reject any request with more
    # than 128 ("'tools': maximum number of items is 128"), and Groq's free
    # tier also caps a request at 8,000 tokens a minute - each tool
    # definition costs ~55, so 128 tools alone overflow it ("413 Request
    # too large"). Either way every reply that needed the LLM failed.
    # brain/tool_select.py sends the ones a turn can use - tools it already
    # called, your own user skills, the ones its words point at, the
    # everyday set - up to this many. 20 keeps a request near 2,700 tokens;
    # raise it (max 128) on a paid tier, 0 sends everything (only for a
    # provider without either limit).
    LLM_MAX_TOOLS = _int_env("LLM_MAX_TOOLS", "20", min=0, max=1000)

    # ── Tier 29: replies that don't stall on rate limits ────────────────
    # A request that gets "429 Too Many Requests" (or "413 Request too
    # large") moves straight to this model instead of waiting ~30 s and
    # retrying the busy one. On Groq every model has its own per-minute
    # quota, so by default the 120b model falls back to openai/gpt-oss-20b
    # (blank = that default; "off" = no fallback, just the busy message).
    LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "").strip()
    # ── Tier 34: the right model for each request (brain/model_router.py) ─
    # Short everyday turns go to this FAST model, and long / multi-step /
    # code / writing / "explain in detail" ones to LLM_MODEL - on Groq each
    # model has its own per-minute quota, so the load is spread (fewer 429s)
    # and everyday replies come sooner. Blank = openai/gpt-oss-20b on Groq
    # (none on other providers); "off" = every turn on LLM_MODEL.
    LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "").strip()
    # on = route each turn; shadow = decide and log, but always use LLM_MODEL; off.
    LLM_ROUTER = _choice_env("LLM_ROUTER", "on", ("on", "shadow", "off"))
    # When every model is rate-limited but one frees up within this many
    # seconds, Wave waits once instead of saying "busy, ask again". 0 = never wait.
    LLM_RATE_LIMIT_WAIT_S = _float_env("LLM_RATE_LIMIT_WAIT_S", "10", min=0, max=60)
    # How much earlier conversation goes with each request (characters,
    # newest first; ~4 characters a token). Long old replies used to add
    # thousands of tokens to every request. 0 = no limit.
    LLM_HISTORY_CHARS = _int_env("LLM_HISTORY_CHARS", "4000", min=0, max=200000)
    # Tier 38: messages that drop out of that history are folded into a short
    # running summary of the conversation (one background call to the fast
    # model per few messages - brain/rolling_summary.py), which goes in the
    # system prompt. false = older messages just drop out, as before.
    LLM_ROLLING_SUMMARY = os.getenv("LLM_ROLLING_SUMMARY", "true").strip().lower() not in ("false", "0", "no")
    LLM_SUMMARY_MAX_CHARS = _int_env("LLM_SUMMARY_MAX_CHARS", "600", min=100, max=4000)

    # ── Phase 6: governed tool-calling loop ─────────────────────────────
    # Before this, think() made exactly one round of tool calls no matter
    # what the model wanted - a request needing two dependent steps (e.g.
    # "check my battery, and if it's low remind me to charge it") had to be
    # guessed in a single shot or not at all. Now the model can see a tool's
    # result and decide to call another tool based on it, bounded two
    # independent ways so a confused/looping model can never hang Wave:
    # a hard cap on how many tool-calling rounds happen, and a wall-clock
    # budget for the whole loop (checked between rounds, and passed as a
    # per-call timeout to the provider where that's supported - see
    # brain/groq_core.py's _chat_completion and llm_provider.py's OpenAICompatClient).
    TOOL_LOOP_MAX_STEPS = _int_env("TOOL_LOOP_MAX_STEPS", "6", min=1, max=50)
    TOOL_LOOP_TIMEOUT_S = _float_env("TOOL_LOOP_TIMEOUT_S", "45", min=1, max=600)

    # ── Phase 6: proactive engine ────────────────────────────────────────
    # A background daemon (automation/proactive.py) that notices a couple of
    # passive signals - battery level, a calendar event about to start - and
    # speaks up on its own, the same way a human assistant would, instead of
    # only ever answering when asked. Entirely optional and independently
    # switchable per trigger; PROACTIVE_ENABLED=false restores the old
    # purely-reactive behavior.
    PROACTIVE_ENABLED = os.getenv("PROACTIVE_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    PROACTIVE_CHECK_INTERVAL = _int_env("PROACTIVE_CHECK_INTERVAL", "60", min=1, max=3600)
    PROACTIVE_BATTERY_ENABLED = os.getenv("PROACTIVE_BATTERY_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Nudge once the battery is at/under this percent AND not plugged in.
    PROACTIVE_BATTERY_THRESHOLD = _int_env("PROACTIVE_BATTERY_THRESHOLD", "20", min=0, max=100)
    # Minimum minutes between repeat low-battery nudges while it's still low
    # (so Wave mentions it once, not every single check interval).
    PROACTIVE_BATTERY_COOLDOWN_MIN = _int_env("PROACTIVE_BATTERY_COOLDOWN_MIN", "30", min=0, max=1440)
    PROACTIVE_CALENDAR_ENABLED = os.getenv("PROACTIVE_CALENDAR_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # How far ahead of a calendar event's start time to give the heads-up nudge.
    PROACTIVE_CALENDAR_LEAD_MINUTES = _int_env("PROACTIVE_CALENDAR_LEAD_MINUTES", "10", min=0, max=1440)
    # Tier 15: a third trigger, armed on demand (watch_for_download) rather
    # than always-on like battery/calendar above - most checks it's a no-op
    # (nothing armed). PROACTIVE_ENABLED=false (or the proactive engine not
    # running at all) means watch_for_download can accept the request but
    # it'll never actually fire - same "the loop just isn't ticking"
    # limitation as the other two triggers, nothing special to this one.
    PROACTIVE_DOWNLOAD_WATCH_ENABLED = os.getenv(
        "PROACTIVE_DOWNLOAD_WATCH_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Default how long watch_for_download keeps watching before giving up and
    # saying so, if the user didn't ask for a specific duration.
    DOWNLOAD_WATCH_TIMEOUT_MIN = _int_env("DOWNLOAD_WATCH_TIMEOUT_MIN", "30", min=1, max=240)

    # Tier 18: calendar sync (Google OAuth + ICS) - automation/calendar_sync.py.
    # Both kinds of feed share this one sync window: how far into the future
    # (plus the small look-back below, to still catch "earlier today") a
    # sync pulls events for - a personal ICS export can span years, and
    # without a window a single sync would import every event that ever
    # existed. Google's own events().list() call is handed this window
    # directly (timeMin/timeMax); an ICS feed has no equivalent partial
    # fetch, so the whole file is parsed and parsed events outside the
    # window are simply skipped before they ever reach upsert_synced_
    # calendar_event().
    CALENDAR_SYNC_WINDOW_DAYS = _int_env("CALENDAR_SYNC_WINDOW_DAYS", "60", min=1, max=365)
    CALENDAR_SYNC_LOOKBACK_HOURS = _int_env("CALENDAR_SYNC_LOOKBACK_HOURS", "6", min=0, max=72)
    # Google OAuth: credentials.json is the OAuth client secret the user
    # downloads themselves from Google Cloud Console - there's no default
    # bundled, since that's a per-developer credential, not something this
    # app can ship (see automation/calendar_sync.py's CalendarSync.setup_google()
    # and NEXT_STEPS.md's Tier 18 section for the one-time human setup this
    # needs). The token file is written by this app after the user completes
    # the browser consent flow once, then read back and silently refreshed
    # on every later sync.
    GOOGLE_CALENDAR_CREDENTIALS_PATH = os.getenv(
        "GOOGLE_CALENDAR_CREDENTIALS_PATH", os.path.join(DATA_DIR, "google_calendar_credentials.json"))
    GOOGLE_CALENDAR_TOKEN_PATH = os.getenv(
        "GOOGLE_CALENDAR_TOKEN_PATH", os.path.join(DATA_DIR, "google_calendar_token.json"))

    # Tier 18: time-triggered workflows - closes the README's "workflows
    # are on-demand only" limitation. Unlike the always-armed
    # battery/calendar checks above, this one also actually DOES something
    # (runs the workflow's steps) rather than just notifying, so it's the
    # first proactive trigger gated on DND/focus/sleep mode (see
    # AppController.is_dnd_or_focus_active() and check_once() below).
    PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = os.getenv(
        "PROACTIVE_WORKFLOW_SCHEDULE_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # Tier 18: the rest of the ≥10-trigger target, each independently
    # switchable like every trigger above.
    PROACTIVE_MORNING_BRIEFING_ENABLED = os.getenv(
        "PROACTIVE_MORNING_BRIEFING_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # 24h "HH:MM" - fires once per calendar day, the first tick at/after this
    # local time (so it still fires once even if Wave wasn't running exactly
    # at this minute), never before it.
    PROACTIVE_MORNING_BRIEFING_TIME = os.getenv("PROACTIVE_MORNING_BRIEFING_TIME", "08:00").strip()

    PROACTIVE_IDLE_BREAK_ENABLED = os.getenv(
        "PROACTIVE_IDLE_BREAK_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Nudge once continuous activity (no gap longer than
    # PROACTIVE_IDLE_RESET_GAP_MIN between audited actions) has lasted this long.
    PROACTIVE_BREAK_AFTER_MIN = _int_env("PROACTIVE_BREAK_AFTER_MIN", "90", min=5, max=1440)
    # A gap at least this long between two audited actions means the user
    # stepped away - breaks a "continuous" session and re-arms the nudge.
    PROACTIVE_IDLE_RESET_GAP_MIN = _int_env("PROACTIVE_IDLE_RESET_GAP_MIN", "5", min=1, max=240)

    PROACTIVE_STREAK_MILESTONES_ENABLED = os.getenv(
        "PROACTIVE_STREAK_MILESTONES_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    PROACTIVE_HABIT_DETECTION_ENABLED = os.getenv(
        "PROACTIVE_HABIT_DETECTION_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # Migrated from ReminderWorker (ui/workers.py) - see
    # automation/proactive.py's _check_reminder_followups(). Default true
    # preserves the pre-Tier-18 always-on behavior.
    PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = os.getenv(
        "PROACTIVE_REMINDER_FOLLOWUPS_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    PROACTIVE_LOW_DISK_ENABLED = os.getenv(
        "PROACTIVE_LOW_DISK_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    PROACTIVE_LOW_DISK_THRESHOLD = _int_env("PROACTIVE_LOW_DISK_THRESHOLD", "90", min=50, max=100)
    PROACTIVE_LOW_DISK_COOLDOWN_MIN = _int_env("PROACTIVE_LOW_DISK_COOLDOWN_MIN", "60", min=0, max=1440)

    # One central LLM-reword-with-template-fallback for every ambient nudge
    # above (see automation/proactive.py's _reword()) - each trigger still
    # produces its own clear, deterministic sentence; this just optionally
    # makes it sound a little more natural, the same "LLM reword, template
    # on failure/timeout/disabled" shape reminders/composer.py already uses
    # for reminders, just simpler (synchronous - check_once() has no beep
    # to protect the way ReminderWorker does).
    PROACTIVE_LLM_REWORD_ENABLED = os.getenv(
        "PROACTIVE_LLM_REWORD_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    PROACTIVE_LLM_REWORD_TIMEOUT_S = _float_env("PROACTIVE_LLM_REWORD_TIMEOUT_S", "4.0", min=0.1, max=60)
    PROACTIVE_LLM_REWORD_MAX_WORDS = _int_env("PROACTIVE_LLM_REWORD_MAX_WORDS", "40", min=5, max=200)

    # ── Phase 6: notes/files RAG ─────────────────────────────────────────
    # search_my_notes lets Wave answer "what do my notes say about X" by
    # chunking + semantically embedding .txt/.md/.pdf files (reusing the
    # exact same optional sentence-transformers pipeline as semantic memory
    # - see database/embeddings.py) instead of only knowing facts it was
    # explicitly told to remember. WAVE_NOTES_DIR adds one more folder to
    # search, on top of the same common folders search_file already looks
    # in (Desktop, Documents, Downloads).
    NOTES_RAG_ENABLED = os.getenv("NOTES_RAG_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    NOTES_DIR = os.getenv("WAVE_NOTES_DIR", "").strip()
    NOTES_CHUNK_CHARS = _int_env("NOTES_CHUNK_CHARS", "1200", min=100, max=50000)
    NOTES_CHUNK_OVERLAP = _int_env("NOTES_CHUNK_OVERLAP", "150", min=0, max=49999)
    if NOTES_CHUNK_OVERLAP >= NOTES_CHUNK_CHARS:
        _config_warnings.append(
            f"NOTES_CHUNK_OVERLAP={NOTES_CHUNK_OVERLAP} must be smaller than "
            f"NOTES_CHUNK_CHARS={NOTES_CHUNK_CHARS}"
        )
        NOTES_CHUNK_OVERLAP = 150 if 150 < NOTES_CHUNK_CHARS else 0
    NOTES_MAX_FILE_MB = _int_env("NOTES_MAX_FILE_MB", "20", min=1, max=1000)

    # ── Phase 7: confirm-before-sensitive-action gate ───────────────────
    # General, reusable replacement for one-off hardcoded confirmations
    # (the restart/shutdown dance in ui/main_window.py was the only
    # example before this). Any @skill(..., confirm=True) tool - see
    # brain/skills_registry.py - is never run on the first ask: WaveBrain
    # arms a pending confirmation (brain/groq_core.py's _guard()) and the
    # NEXT turn's reply is checked against yes/no words (English/Hinglish/
    # Hindi - brain/confirm_gate.py) before anything actually runs. A
    # pending confirmation nobody answers in time is just dropped - never
    # auto-confirmed, never auto-cancelled-with-a-message, so it can't be
    # mistaken for Wave itself deciding anything.
    CONFIRM_PENDING_TTL_S = _float_env("CONFIRM_PENDING_TTL_S", "60", min=1, max=3600)

    # ── Phase 7: untrusted-content boundary ──────────────────────────────
    # Content Wave did not get directly from the user's own typed/spoken
    # words - live web search results, OCR'd screen text, attached-file
    # text - gets wrapped in a clear "this is data, not instructions"
    # marker (brain/content_trust.py) before it reaches the model, plus a
    # lightweight weighted-regex scan for common injection phrasings
    # (English/Hinglish/Hindi) that strengthens the marker's wording when
    # something matches. This is a basic boundary, not a hard content
    # filter - nothing is ever deleted or blocked, so a false positive
    # costs nothing but a more cautious-sounding label.
    PROMPT_INJECTION_DEFENSE_ENABLED = os.getenv(
        "PROMPT_INJECTION_DEFENSE_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    CONTENT_TRUST_SCAN_THRESHOLD = _float_env("CONTENT_TRUST_SCAN_THRESHOLD", "4", min=0)
    NOTES_MAX_PDF_PAGES = _int_env("NOTES_MAX_PDF_PAGES", "60", min=1, max=10000)

    # ── Tier 1: risk-tiered confirm gate + per-turn taint tracking ──────
    # brain/tiers.py classifies every tool T0-T3 and brain/taint.py tracks
    # whether this turn already read something untrusted; together they
    # let _guard() (brain/groq_core.py) escalate a normally-auto-run tool
    # to confirm-required, or a normally-confirm tool to an outright deny,
    # for the rest of a turn that touched a web result/OCR'd screen/
    # attachment. RISK_TIER_GUARD_ENABLED=false restores the exact
    # pre-Tier-1 behaviour: _guard() reads skills.requires_confirm()
    # directly, nothing about tiers or taint consulted at all (same
    # kill-switch shape as PROMPT_INJECTION_DEFENSE_ENABLED above).
    RISK_TIER_GUARD_ENABLED = os.getenv(
        "RISK_TIER_GUARD_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # ── Tier 2: memory consolidation (brain/memory_consolidation.py) ────
    # A background pass that periodically distills durable facts out of
    # ordinary conversation, the same way remember_this does when the user
    # asks explicitly. MEMORY_CONSOLIDATION_INTERVAL_S defaults far longer
    # than SARA's reference 60s: Wave is cloud-first (see the README), so
    # every tick is a real, metered API call unless the person has a local
    # Ollama fallback configured - 10 minutes keeps this a background
    # housekeeping cost, not something that competes with or adds up
    # alongside the main conversation's own token usage.
    MEMORY_CONSOLIDATION_ENABLED = os.getenv(
        "MEMORY_CONSOLIDATION_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    MEMORY_CONSOLIDATION_INTERVAL_S = _float_env("MEMORY_CONSOLIDATION_INTERVAL_S", "600", min=30)
    MEMORY_CONSOLIDATION_BATCH_SIZE = _int_env("MEMORY_CONSOLIDATION_BATCH_SIZE", "20", min=2, max=100)
    MEMORY_CONSOLIDATION_MAX_FACTS = _int_env("MEMORY_CONSOLIDATION_MAX_FACTS", "3", min=1, max=10)
    # Deliberately short and separate from the main chat's own cfg.timeout
    # (sized for a full conversational turn) - same reasoning as
    # reminders/composer.py's CONTEXTUAL_REMINDER_LLM_TIMEOUT_S: background
    # housekeeping should fail fast and just skip the tick, never hold a
    # thread open waiting on a slow provider.
    MEMORY_CONSOLIDATION_TIMEOUT_S = _float_env("MEMORY_CONSOLIDATION_TIMEOUT_S", "8.0", min=0.5, max=60)

    # ── Tier 19: background-thread supervisor ───────────────────────────
    # MemoryConsolidator/ProactiveEngine/UserSkillLoader each wrap their own
    # tick loop in `except Exception`, so an ordinary exception from a
    # check/tick can never kill the thread - but a BaseException subtype
    # (SystemExit, or a C-extension crash surfacing as one) escaping that
    # guard silently ends the thread, with nothing anywhere noticing it
    # stopped. Tier 5 hit exactly this shape for real (pyautogui's own
    # import doing a bare sys.exit() under some Linux desktop environments)
    # - the concrete precedent, not a hypothetical. automation/thread_
    # supervisor.py polls is_alive() on each watched singleton from its own
    # daemon thread and restarts one that's gone quiet.
    # INTERVAL_S - how often the supervisor checks is_alive() on everything
    # it watches. BASE/MAX_BACKOFF_S - a dead thread is restarted after
    # BASE_BACKOFF_S, doubling on each further restart up to MAX_BACKOFF_S
    # (a thread that's immediately dying again and again shouldn't be
    # restarted in a tight loop). MAX_RESTARTS - the crash-loop cap: once a
    # single watched thread has needed this many restarts, the supervisor
    # gives up on it for the rest of this session (logged loudly) rather
    # than spinning forever on something fundamentally broken.
    # HEALTHY_RESET_S - a thread that's stayed alive this long since its
    # last restart has its restart count decayed back to 0, so one rare
    # crash years apart from another doesn't get treated as the same
    # crash loop and eat into the MAX_RESTARTS cap.
    THREAD_SUPERVISOR_INTERVAL_S = _float_env("THREAD_SUPERVISOR_INTERVAL_S", "10", min=1, max=300)
    THREAD_SUPERVISOR_BASE_BACKOFF_S = _float_env("THREAD_SUPERVISOR_BASE_BACKOFF_S", "5", min=0.5, max=300)
    THREAD_SUPERVISOR_MAX_BACKOFF_S = _float_env("THREAD_SUPERVISOR_MAX_BACKOFF_S", "300", min=1, max=3600)
    THREAD_SUPERVISOR_MAX_RESTARTS = _int_env("THREAD_SUPERVISOR_MAX_RESTARTS", "5", min=1, max=100)
    THREAD_SUPERVISOR_HEALTHY_RESET_S = _float_env("THREAD_SUPERVISOR_HEALTHY_RESET_S", "600", min=10, max=86400)

    # ── Phase 4: action audit + undo ────────────────────────────────────
    # Files Wave deletes (or overwrites) are moved/copied into TRASH_DIR
    # instead of being destroyed, which is what makes "undo" possible. Items
    # older than TRASH_RETENTION_DAYS are purged at startup. Files bigger
    # than UNDO_MAX_FILE_MB aren't copied (that would stall a voice command
    # and eat disk) - those are deleted for real and logged as not undoable.
    # UNDO_TRASH_ENABLED=false restores the old permanent-delete behaviour.
    UNDO_TRASH_ENABLED = os.getenv("UNDO_TRASH_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    TRASH_DIR = os.path.join(DB_DIR, "trash")
    TRASH_RETENTION_DAYS = _int_env("TRASH_RETENTION_DAYS", "7", min=0, max=3650)
    UNDO_MAX_FILE_MB = _int_env("UNDO_MAX_FILE_MB", "200", min=1, max=10000)
    # Oldest audit rows beyond this many are pruned so the table can't grow forever.
    AUDIT_MAX_ENTRIES = _int_env("AUDIT_MAX_ENTRIES", "1000", min=1, max=1000000)

    # ── Tier 2: fast deterministic intent layer ─────────────────────────
    # brain/intent_router.py catches a curated set of unambiguous commands
    # ("volume up", "lock my pc", "open chrome") and dispatches them
    # straight to the matching skill, before think()/think_stream() would
    # otherwise spend a Groq/OpenAI round-trip confirming what was already
    # obvious. INTENT_ROUTER_ENABLED=false restores the pre-Tier-2
    # behaviour of every command going through the LLM tool-calling loop.
    INTENT_ROUTER_ENABLED = os.getenv("INTENT_ROUTER_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # ── Tier 17: follow-up/pronoun fast path ────────────────────────────
    # brain/followup_resolver.py - "close it"/"usko band karo" after
    # open_app, "and Jaipur?"/"aur Jaipur ka?" after get_weather. Tried
    # right before intent_router.route() in think()/think_stream().
    # FOLLOWUP_RESOLVER_ENABLED=false restores plain intent_router/LLM
    # behaviour (a pronoun with no self-contained meaning just falls
    # through to the LLM, which has the real chat history to work from).
    FOLLOWUP_RESOLVER_ENABLED = os.getenv("FOLLOWUP_RESOLVER_ENABLED", "true").strip().lower() not in (
        "false", "0", "no")
    # How long "it"/a carried-over topic stays resolvable after the
    # action it refers to - same spirit as CONFIRM_PENDING_TTL_S above,
    # but its own constant: these are two different kinds of "still
    # fresh enough to act on without re-asking", not one.
    FOLLOWUP_CONTEXT_TTL_S = _float_env("FOLLOWUP_CONTEXT_TTL_S", "120", min=5, max=3600)

    # Tier 17: "unmatched-utterance log plus a small review script, so
    # real misses become new rules" - see brain/unmatched_log.py. A local,
    # append-only diagnostic log of every message neither followup_resolver
    # nor intent_router recognized, for scripts/review_unmatched_utterances.py
    # to summarize later. UNMATCHED_LOG_ENABLED=false turns logging off
    # entirely (a true kill-switch, same convention as every other feature
    # flag in this file). UNMATCHED_LOG_PATH overrides the default
    # data/unmatched_utterances.jsonl location - tests set this to a
    # tmpdir path; production deployments leave it unset.
    UNMATCHED_LOG_ENABLED = os.getenv("UNMATCHED_LOG_ENABLED", "true").strip().lower() not in (
        "false", "0", "no")
    UNMATCHED_LOG_PATH = os.getenv("UNMATCHED_LOG_PATH", "").strip()

    # ── Tier 2: centralized, rotating logging ───────────────────────────
    # See settings/logging_config.py - this used to be a single inline
    # logging.basicConfig() call right here with no rotation at all, so a
    # long-running install's log could grow without bound. LOG_LEVEL/
    # LOG_MAX_BYTES/LOG_BACKUP_COUNT/LOG_CONSOLE_ENABLED tune it; defaults
    # match what the old call effectively did except for rotation, which
    # previously didn't exist.
    LOG_LEVEL = _choice_env("LOG_LEVEL", "INFO", ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    LOG_MAX_BYTES = _int_env("LOG_MAX_BYTES", str(10 * 1024 * 1024), min=1024)
    LOG_BACKUP_COUNT = _int_env("LOG_BACKUP_COUNT", "5", min=0, max=100)
    LOG_CONSOLE_ENABLED = os.getenv("LOG_CONSOLE_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # ── Tier 4 item #11: multi-step planner ─────────────────────────────
    # planning/ (try_plan_and_execute, called from think()/think_stream()
    # right after intent_router.route() finds no match, before the normal
    # single-round-at-a-time tool-calling loop) asks the model to propose a
    # short, schema-validated sequence of already-registered skills for a
    # request that plainly needs more than one tool call, then runs that
    # sequence itself - one self-correction retry per failed step, every
    # step still going through WaveBrain's own _validate_tool_args/_guard/
    # audit.action, exactly like an LLM-chosen or intent_router-routed call.
    # PLANNER_ENABLED=false restores pre-Tier-4 behaviour: every request,
    # simple or compound, goes through the normal one-tool-at-a-time loop.
    PLANNER_ENABLED = os.getenv("PLANNER_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    PLANNER_MAX_STEPS = _int_env("PLANNER_MAX_STEPS", "6", min=1, max=50)
    PLANNER_PROPOSE_TIMEOUT_S = _float_env("PLANNER_PROPOSE_TIMEOUT_S", "20", min=1, max=300)
    PLANNER_STEP_TIMEOUT_S = _float_env("PLANNER_STEP_TIMEOUT_S", "20", min=1, max=300)
    PLANNER_TOTAL_TIMEOUT_S = _float_env("PLANNER_TOTAL_TIMEOUT_S", "75", min=1, max=1800)
    PLANNER_MAX_RETRIES_PER_STEP = _int_env("PLANNER_MAX_RETRIES_PER_STEP", "1", min=0, max=10)

    # ── Tier 6: parallel execution of independent, read-only plan steps ──
    # planning/executor.py's own module docstring explains why full
    # parallelism (every depends_on_previous=False step, regardless of
    # tier) was deliberately deferred at Tier 4: most of Wave's tools touch
    # shared, stateful things on the one machine the person is sitting at,
    # where running two at once risks races and confusing on-screen
    # results for a latency win that barely matters at PLANNER_MAX_STEPS=6.
    # That reasoning doesn't apply to a T0 (read-only, brain/tiers.py) step -
    # by definition no side effect, and the one tier _guard() always
    # resolves straight to "run" with no gating at all - so Tier 6 scopes
    # parallelism to exactly that subset: a contiguous run of 2+
    # depends_on_previous=False, T0-tier steps runs concurrently in a small
    # thread pool; anything else (any step with real side effects, or one
    # the model marked dependent) still runs strictly in order, unchanged.
    # PLANNER_PARALLEL_READONLY_STEPS=false restores fully-sequential
    # execution for every step regardless of tier (the exact pre-Tier-6
    # behaviour).
    PLANNER_PARALLEL_READONLY_STEPS = os.getenv("PLANNER_PARALLEL_READONLY_STEPS", "true").strip().lower() not in ("false", "0", "no")
    PLANNER_MAX_PARALLEL_WORKERS = _int_env("PLANNER_MAX_PARALLEL_WORKERS", "4", min=1, max=16)

    # ── Tier 4 item #13: telemetry/tracing ──────────────────────────────
    # telemetry/trace.py marks a handful of stages (routing decision, LLM
    # round(s), tool call(s)) in memory for the one turn currently in
    # flight per thread, then hands the finished summary to
    # telemetry/store.py, which batches rows onto a single background
    # writer thread - think()/think_stream() never block on a DB write.
    # TELEMETRY_ENABLED=false turns marking into a no-op everywhere it's
    # called (same kill-switch shape as PROMPT_INJECTION_DEFENSE_ENABLED).
    TELEMETRY_ENABLED = os.getenv("TELEMETRY_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    TELEMETRY_MAX_ROWS = _int_env("TELEMETRY_MAX_ROWS", "5000", min=1, max=10000000)
    TELEMETRY_QUEUE_MAX = _int_env("TELEMETRY_QUEUE_MAX", "512", min=1, max=1000000)
    TELEMETRY_FLUSH_INTERVAL_S = _float_env("TELEMETRY_FLUSH_INTERVAL_S", "2.0", min=0.05, max=300)
    TELEMETRY_BATCH_MAX = _int_env("TELEMETRY_BATCH_MAX", "50", min=1, max=100000)
    # Tier 11: time-to-first-audio per voice turn (telemetry/ttfa.py),
    # appended to <data dir>/database/ttfa.jsonl - read it with
    # `python -m telemetry.ttfa_report`. Off when TELEMETRY_ENABLED is off.
    TTFA_ENABLED = os.getenv("TTFA_ENABLED", "true").strip().lower() not in ("false", "0", "no")
    # Tier 21: "Local-only usage stats, opt-in." A separate, user-facing
    # opt-in on top of (not instead of) TELEMETRY_ENABLED above: that flag
    # already gates whether turn_trace rows exist at all, for the
    # Performance page's TTFA numbers. This one gates whether
    # telemetry/usage_stats.py is ALLOWED to read that already-local data
    # and turn it into a usage summary - off by default, and even when on,
    # nothing it computes is ever written anywhere but shown back to the
    # user; see telemetry/usage_stats.py's own docstring.
    USAGE_STATS_ENABLED = os.getenv("USAGE_STATS_ENABLED", "false").strip().lower() not in ("false", "0", "no")

    # ── Tier 14: system control surface I ───────────────────────────────
    # Shared countdown for every power/session action (shutdown, restart,
    # sleep, hibernate, log off) - long enough that a spoken "cancel that"
    # has a real chance of landing (STT + intent routing/LLM round-trip
    # all eat into the window) before it actually fires, short enough that
    # confirming the action doesn't feel like it hung. automation/
    # app_control.py's AppController is the one place this is read, for
    # both the native-OS-delay path (shutdown/restart) and Wave's own
    # cancellable timer (sleep/hibernate/log off) - see its module comments.
    # Deliberately NOT the GUI Danger Zone buttons' own hardcoded 5s
    # (ui/main_window.py) - that path's confirmation is the dialog click
    # itself, so a short follow-up delay is fine there; this is a new,
    # separate surface with no dialog, so it gets a more generous default.
    POWER_ACTION_DELAY_S = _int_env("POWER_ACTION_DELAY_S", "15", min=3, max=300)

    # ── Tier 20: secret redaction (brain/redaction.py) ──────────────────
    # Scrubs text shaped like a secret (API keys, a JWT, card numbers, and
    # Wave's own India-specific additions - Aadhaar/PAN/UPI/IFSC/phone
    # numbers) before it's logged, traced, or shown in a diagnostics
    # report. REDACT_SECRETS_ENABLED=false restores the exact pre-Tier-20
    # behaviour (nothing scrubbed) - same kill-switch shape as every other
    # feature toggle in this file.
    REDACT_SECRETS_ENABLED = os.getenv(
        "REDACT_SECRETS_ENABLED", "true").strip().lower() not in ("false", "0", "no")

    # ── Tier 20: security mode (automation/security_log.py) ─────────────
    # One global dial, independent of the tier/taint policy's own
    # defaults, that automation/security_log.py's effective_mode() and
    # brain/groq_core.py's _guard() both read: "off" disables the extra
    # Tier 20 guards entirely (exfiltration-URL/speak/RAG-origin checks -
    # the underlying Tier 1 tier/taint gate itself is a SEPARATE toggle,
    # RISK_TIER_GUARD_ENABLED above, and is unaffected); "standard" (the
    # default) is today's behaviour; "strict" tightens it further (treats
    # T1 as if it were tainted-sensitive even when untainted, for anyone
    # who wants the most cautious posture Wave can offer). Validated
    # against the three allowed values; anything else falls back to
    # "standard" with a logged warning, the same pattern _choice_env
    # already uses elsewhere in this file.
    SECURITY_MODE = _choice_env("SECURITY_MODE", "standard", ("off", "standard", "strict"))

    # Ensure directories exist
    os.makedirs(DB_DIR, exist_ok=True)


config = Config()

# Logging setup - see settings/logging_config.py for what changed and why.
# Imported here (not at module top) because logging_config.setup()'s
# default data_dir lookup imports `config` from this same module; doing
# it after `config = Config()` avoids a circular partial-import.
from settings import logging_config  # noqa: E402
logging_config.setup(config.DATA_DIR)

# -- Tier 5 / item 14: surface any config-validation problems ---------------
# Collected above while Config's class body ran (see _int_env/_float_env/
# _choice_env). Reported here rather than as each one is found, because
# logging_config.setup() just above is what gives this a real handler and a
# rotating log file, instead of Python's bare stderr fallback logger.
if _config_warnings:
    _cfg_logger = logging.getLogger("Config")
    for _msg in _config_warnings:
        _cfg_logger.warning("Invalid setting ignored: %s - falling back to the documented default.", _msg)
    _cfg_logger.warning(
        "%d setting(s) in your environment were invalid; see the warning(s) above. "
        "Wave is still running, using safe defaults for those.", len(_config_warnings),
    )