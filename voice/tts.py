import asyncio
import collections
import concurrent.futures
import hashlib
import itertools
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

import edge_tts
import pygame

from settings.config import config
from voice.aec import aec_processor, resample_linear
from voice.tts_cache import phrase_cache
# Tier 13: `import voice.X as X`, not `from voice import X` - the latter
# resolves via hasattr() on the already-cached `voice` package object,
# which would leave every test sharing one stale voice.language module
# (and its LanguageTracker singleton) the moment a test pops
# "voice.language" from sys.modules expecting a fresh one - see
# voice/stt.py's own import of the same module for the full explanation.
import voice.language as language
import voice.text_prep as text_prep

logger = logging.getLogger("TTS")

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False

try:
    import sounddevice as sd

    HAS_SOUNDDEVICE = True
except (ImportError, OSError) as _e:
    sd = None
    HAS_SOUNDDEVICE = False
    logger.info(
        f"'sounddevice' not available - TTS playback stays on pygame only, and "
        f"W.A.V.E.'s own voice won't be fed into AEC's far-end reference even "
        f"if 'aec-audio-processing' is installed (pip install sounddevice for "
        f"both). ({_e})"
    )

# Local neural TTS fallback (optional, NOT in requirements.txt on purpose).
#
# edge-tts is the primary engine and needs the internet; until now the only
# offline fallback was the old robotic Windows SAPI voice - a real hole in
# "local-first". Piper (https://github.com/OHF-Voice/piper1-gpl, `pip
# install piper-tts`) is a small, fully-local neural TTS engine that sounds
# far closer to edge-tts than SAPI does, and is the obvious fix.
#
# It's deliberately NOT added to requirements.txt here, for three reasons
# worth knowing about before you add it yourself:
#   1. Windows-wheel coverage wasn't something I could verify with
#      confidence - unlike sounddevice and aec-audio-processing (both
#      confirmed to ship Windows wheels matching this project's Python
#      3.11 CI before being added below), a bad guess here would silently
#      break `pip install -r requirements.txt` on the windows-latest CI
#      runner, which doesn't have a C/C++ build toolchain.
#   2. Current piper-tts (>=1.3.0, the actively maintained OHF-Voice/
#      piper1-gpl fork) is GPL-3.0-or-later. wave_project's own LICENSE
#      file is currently empty (no license chosen yet) - worth deciding
#      that deliberately before a GPL runtime dependency is load-bearing,
#      especially given wave.spec/build_exe.bat bundle everything into one
#      .exe via PyInstaller (the older MIT-licensed rhasspy/piper is
#      archived and no longer updated, so it isn't a drop-in alternative).
#   3. It pulls in onnxruntime (and downloads a voice model, hundreds of
#      MB, on first use) - real weight for an offline FALLBACK path.
#
# None of that makes it a bad choice - just one you should make
# explicitly. `pip install piper-tts`, download a voice
# (`python -m piper.download_voices en_US-lessac-medium`), point
# TTS_LOCAL_MODEL_PATH at the resulting .onnx file, and this activates
# automatically. Skip it and SAPI keeps working exactly as before.
#
# Targets piper-tts's current API (PiperVoice.load() + the streaming
# .synthesize() generator of AudioChunk objects, confirmed against the
# project's own docs/API_PYTHON.md as of this writing). An older
# pre-1.3.0 install has a different API and would fail the calls in
# _speak_via_piper() below - caught, logged once, and treated exactly
# like piper-tts not being installed at all.
try:
    from piper import PiperVoice

    HAS_PIPER = True
except ImportError:
    PiperVoice = None
    HAS_PIPER = False

# Tier 13: Kokoro (https://github.com/thewh1teagle/kokoro-onnx, `pip
# install kokoro-onnx`) is a SECOND optional local neural voice. Piper
# above is English-only - handed Hindi text it doesn't fail, it just
# mispronounces it, which would make _speak_offline_fallback() look
# "successful" while actually speaking gibberish. Kokoro genuinely ships
# Hindi voices (hf_alpha/hf_beta/hm_omega/hm_psi - confirmed via its own
# .get_voices()), so it's the one offline path that can speak a
# classified-Hindi chunk in Hindi at all - see _speak_offline_fallback().
#
# Also NOT in requirements.txt, same "don't surprise anyone with new
# weight" reasoning as Piper above, plus its own honest tradeoff: CPU
# synthesis measured here at roughly 2x real time for both languages
# (see docs/VOICE_LOCAL.md) - genuinely slower than Piper/edge-tts, not
# a free upgrade. Off by default (TTS_KOKORO_ENABLED) for exactly that
# reason - someone with a Hindi-heavy offline workflow can opt in and
# judge the tradeoff for themselves. Unlike piper-tts, kokoro-onnx ships
# no download CLI of its own, so its two model files are fetched by
# this project directly - see _download_kokoro_files().
try:
    from kokoro_onnx import Kokoro

    HAS_KOKORO = True
except ImportError:
    Kokoro = None
    HAS_KOKORO = False

# Tier 25: Coqui TTS (https://github.com/idiap/coqui-ai-TTS, `pip install
# coqui-tts`) - a THIRD optional local engine, and the odd one out: Piper/
# Kokoro above are stock voices picked for being a decent OFFLINE
# fallback; this one exists to make Wave speak in the USER'S OWN voice,
# cloned from a short reference sample they provide (see
# docs/ROADMAP_BEAT_SARA.md's Tier 25 - the avatar companion feature this
# was built for). Specifically XTTS-v2, Coqui's multilingual (Hindi
# included) few-shot cloning model - see settings/config.py's
# TTS_CLONE_* block for the full reasoning, and why there's no separate
# auto-download consent flag the way Piper/Kokoro each have one.
#
# Imported as `from TTS.api import TTS` - the package's own PyPI name is
# `coqui-tts` (the original `TTS` package is unmaintained; `coqui-tts` is
# its actively-maintained fork, same import path) so this works against
# either one without caring which is actually installed.
try:
    from TTS.api import TTS as CoquiTTS

    HAS_COQUI_TTS = True
except ImportError:
    CoquiTTS = None
    HAS_COQUI_TTS = False

# Tier 2: sentence-boundary splitter for speak_stream() - same approach
# _speak_via_sapi already uses below (a period/question mark/exclamation
# mark followed by whitespace, or a newline), plus '।' (the Hindi
# full-stop/danda) since brain/confirm_gate.py already treats that as
# sentence-ending punctuation for this project's Hinglish/Hindi input.
# Naive on purpose (an abbreviation like "Mr." mid-sentence will split
# early) - the same trade-off the existing SAPI fallback already makes,
# not a new one introduced here.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+|\n+")
# Soft cap on how much un-punctuated text speak_stream() will buffer
# before speaking it anyway - bounds worst-case latency if the model ever
# streams a long run-on with no sentence-ending punctuation at all,
# rather than waiting indefinitely for a sentence end that may never come.
# Tier 7: the cut point itself now prefers the last comma/semicolon
# inside this window (a clause boundary reads far more naturally spoken
# aloud as two pieces than wherever the 240th character happens to land),
# falling back to the previous last-word-boundary cut only if the run-on
# has no clause punctuation in it either - see flush() inside
# speak_stream().
_STREAM_MAX_BUFFER_CHARS = 240

# Tier 11: how many not-yet-played sentences may be queued for synthesis
# ahead of playback - the one about to play plus two more. That is enough
# to hide edge-tts's network round-trip behind the sentence that is
# playing, without firing a burst of requests for a long reply the user may
# interrupt after the first sentence anyway.
_PREFETCH_MAX_PENDING = 3

# Tier 13: known-good (sha256, size in bytes) for a Piper voice this
# project can auto-download - see _download_default_piper_voice(). Each
# entry here was checked against rhasspy/piper-voices' own git-lfs object
# id for that exact file (its SHA256, not a git blob hash) at the time it
# was added - see docs/VOICE_LOCAL.md for how to verify or refresh one.
# A voice NOT listed here (someone's own TTS_LOCAL_DEFAULT_VOICE choice)
# still downloads - see _verify_piper_voice_checksum()'s docstring for why
# that's a deliberate, logged exception rather than a silent gap.
PIPER_VOICE_CHECKSUMS = {
    "en_US-lessac-medium": {
        "sha256": "5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f",
        "size": 63201294,
    },
}

# Tier 13: same idea as PIPER_VOICE_CHECKSUMS above, but for Kokoro's two
# FIXED model files (it has no open-ended voice catalog the way Piper
# does - one model + one voices file covers every Kokoro voice) -
# downloaded straight from its GitHub release since kokoro-onnx has no
# download CLI of its own - see _download_kokoro_files() and
# docs/VOICE_LOCAL.md for how these were derived/can be refreshed.
KOKORO_RELEASE_BASE_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
)
KOKORO_FILE_CHECKSUMS = {
    "kokoro-v1.0.int8.onnx": {
        "sha256": "6e742170d309016e5891a994e1ce1559c702a2ccd0075e67ef7157974f6406cb",
        "size": 92361271,
    },
    "voices-v1.0.bin": {
        "sha256": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        "size": 28214398,
    },
}


def _split_ready(buffer: str, final: bool):
    """speak_stream()'s sentence cutter. Returns (pieces ready to speak,
    text still waiting for its sentence end). With final=True everything
    left is ready.

    When there's no sentence boundary yet and the buffer is getting long,
    Tier 7's rule applies: prefer the last comma/semicolon inside the
    _STREAM_MAX_BUFFER_CHARS window (a clause boundary sounds far more
    natural spoken as two pieces), and only fall back to the last word
    boundary if the run-on has no clause punctuation in this window either,
    so a run-on with NO punctuation at all still starts talking instead of
    waiting indefinitely."""
    pieces = _SENTENCE_SPLIT_RE.split(buffer)
    if final:
        return pieces, ""
    if len(pieces) > 1:
        return pieces[:-1], pieces[-1]
    if len(buffer) > _STREAM_MAX_BUFFER_CHARS:
        window = buffer[:_STREAM_MAX_BUFFER_CHARS]
        cut = max(window.rfind(","), window.rfind(";"))
        if cut > 0:
            cut += 1  # keep the comma/semicolon with the spoken piece, not the leftover buffer
        else:
            cut = buffer.rfind(" ", 0, _STREAM_MAX_BUFFER_CHARS)
            cut = cut if cut > 0 else _STREAM_MAX_BUFFER_CHARS
        return [buffer[:cut]], buffer[cut:].lstrip()
    return [], buffer

class _PieceQueue:
    """speak_stream()'s hand-off between the text reader and the speaker
    thread: FIFO, closable, and able to show the speaker what comes next
    (for prefetch top-ups) - which queue.Queue can't."""

    def __init__(self):
        self._items = collections.deque()
        self._cond = threading.Condition()
        self._closed = False

    def put(self, item):
        with self._cond:
            self._items.append(item)
            self._cond.notify()

    def close(self):
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def get(self, on_wait=None, cancelled=None, lookahead: int = 2):
        """(next item, the up-to-`lookahead` items after it), or (None, [])
        once closed and drained - or as soon as `cancelled()` is true."""
        with self._cond:
            while not self._items:
                if self._closed or (cancelled and cancelled()):
                    return None, []
                if on_wait:
                    on_wait()
                self._cond.wait(0.1)
            if cancelled and cancelled():
                return None, []
            item = self._items.popleft()
            return item, list(itertools.islice(self._items, lookahead))


# Tier 10: markdown cleanup before speaking. The old one-liner
# text.replace("*", "").replace("#", "").replace("_", "") also stripped the
# underscores INSIDE words, so "saved to my_report_v2.docx" was read out as
# "myreportv2.docx", and "C#" lost its "#". These keep the intent (drop
# emphasis/heading/code markup) without touching characters that are part
# of a word.
_MD_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
_MD_LOOSE_HASH_RE = re.compile(r"(?<![A-Za-z0-9])#+")
_MD_UNDERSCORE_RE = re.compile(r"(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])")


def clean_for_speech(text: str) -> str:
    """Removes markdown emphasis/heading/code markers from `text` so the
    voice doesn't read them out, while keeping underscores and '#' that
    belong to a word (file names, identifiers, "C#")."""
    text = text or ""
    text = text.replace("`", "").replace("*", "")
    text = _MD_HEADING_RE.sub("", text)
    text = _MD_LOOSE_HASH_RE.sub("", text)
    text = _MD_UNDERSCORE_RE.sub("", text)
    return text.strip()


class VoiceEngine:
    """TTS engine using Microsoft Edge's free online neural voices (edge-tts)
    for natural, human-sounding speech - a big step up from the robotic
    default Windows SAPI voice this used to use.

    Playback has two tiers, tried in order, so losing one dependency never
    means losing speech entirely:
      1. sounddevice (if installed) - a real-time output callback that
         also feeds the exact played samples into the shared AECProcessor
         (voice/aec.py) as the far-end reference, which is what makes
         actual echo cancellation possible instead of guessing from
         energy thresholds. See _play_pcm_via_sounddevice().
      2. pygame - first as a decoded in-memory Sound (still no file I/O
         per retry), then the original file-based mixer.music path. No
         AEC reference signal on this tier - see voice/aec.py's HONEST
         LIMITATION note.

    If there's no internet (or Microsoft's service is briefly unreachable),
    this falls back to a local neural voice (Piper, if installed - see the
    HAS_PIPER block above) and, failing that, the old offline Windows
    voice, so W.A.V.E. never just goes silent. Either fallback still goes
    through the same two playback tiers above.

    `speak_and_wait()` returns whether audio was actually heard (not just
    "a call was made") - see last_playback_ok below - so callers (see
    ui/workers.py's TTSWorker) can tell a genuinely silent failure apart
    from a normal spoken reply instead of reporting success either way.
    """

    # Any voice name from `edge-tts --list-voices` works here. A few natural
    # alternatives worth trying: "en-IN-PrabhatNeural" (Indian English,
    # male), "en-US-AriaNeural" / "en-US-GuyNeural" (US English).
    # These are just the *defaults* now - Settings > Voice can override them
    # at runtime via set_voice()/set_rate()/set_volume() below.
    VOICE = "en-IN-NeerjaNeural"
    # Tier 13: the edge-tts voice used for a chunk classified as Hindi
    # (Devanagari) - see _cloud_voice_and_text(). English and Hinglish
    # (romanized Hindi) both stay on VOICE above: edge-tts's Hindi voices
    # are trained on Devanagari script, and reading romanized text with
    # one tends to come out as mangled letter-by-letter Hindi rather than
    # the English-ish pronunciation Hinglish actually wants.
    VOICE_HI = "hi-IN-SwaraNeural"
    RATE = "+8%"   # slightly brisker than default - reads as more responsive
    VOLUME = "+0%"  # edge-tts's own volume param - left alone; see set_volume()

    # pygame's mixer is initialized at this rate/mono so decoded audio
    # needs little to no resampling before being handed to sounddevice/AEC
    # (edge-tts's own output is close to this already) and so the decoded
    # array is already 1-D (mono) without a downmix step in the common case.
    _MIXER_RATE = 24000

    def __init__(self):
        self.stop_event = threading.Event()
        self.speak_thread = None
        # Tier 11: is_speaking is a property now (see below) - True while a
        # single utterance is playing OR while a streamed reply is between
        # two of its sentences, so the mic's barge-in gate doesn't see a
        # "not speaking" gap every time one sentence ends and the next
        # begins.
        self._utterance_active = False
        self._stream_active = False
        self.is_speaking = False  # Tracked so STT can avoid picking up our own voice
        # Tier 11: every stop() bumps _interrupt_gen. A streamed reply
        # remembers the generation it started in and never starts another
        # sentence once that has changed - closing the race where a stop()
        # landing between two sentences used to be undone by the next
        # sentence's own stop_event.clear().
        self._gen_lock = threading.Lock()
        self._interrupt_gen = 0
        # Held for the whole of one utterance's playback (_speak_task), so
        # speak() can wait for an interrupted utterance on ANOTHER thread to
        # actually let go of the audio device before starting a new one.
        self._playback_lock = threading.Lock()
        # Tier 11: synthesize-ahead for streamed replies - see prefetch().
        self._prefetch_lock = threading.Lock()
        self._prefetch_futures = {}
        self._prefetch_pool = None
        self._prefetch_seq = itertools.count(1)
        # Optional listener fired once per utterance, right before its audio
        # is handed to the output device - telemetry/ttfa.py uses it to
        # timestamp "first audio". Must be cheap and must not raise (it's
        # guarded anyway).
        self.on_playback_start = None
        # Tier 27: optional listener called with (pcm, sample_rate) just
        # before each decoded utterance (or streamed sentence) starts
        # playing - the web UI's Dodo page moves the avatar's mouth from
        # it. Guarded like on_playback_start; never allowed to stop audio.
        self.on_playback_pcm = None
        self._audio_started = False
        self._seq = 0
        self._tmp_dir = tempfile.mkdtemp(prefix="wave_tts_")
        self._mixer_ready = False

        # Instance-level so Settings > Voice can change these at runtime.
        self.voice = self.VOICE
        self.voice_hi = self.VOICE_HI
        self.rate = self.RATE
        self.volume_level = 0.8  # 0.0-1.0, applied via pygame's mixer

        # 🔧 FIX: speak()/speak_and_wait() used to have no way to report
        # that playback silently failed (sounddevice unusable AND pygame's
        # mixer unable to play anything) - finished_speaking fired exactly
        # the same either way. Set pessimistically at the start of every
        # _speak_task and flipped True the moment *any* tier actually
        # produces audible playback, so a genuine end-to-end failure is
        # distinguishable from both success and a deliberate interruption
        # (see _speak_task). Safe to read from another thread right after
        # speak_thread.join() - the write happens-before the join() return.
        self.last_playback_ok = True

        # 🔧 Phase 5: local Piper voice - not loaded here for the same
        # "don't pay the cost at import time" reason faster-whisper's model
        # is lazy-loaded in stt.py. See _get_piper_voice().
        self._piper_voice = None
        self._piper_load_failed = False
        # Tier 13: optional per-download confirmation, for a future
        # Settings dialog to wire up - callback(voice_name) -> bool.
        # TTS_LOCAL_AUTO_DOWNLOAD=true (default false) is this project's
        # primary consent mechanism (see _confirm_piper_download()); this
        # hook only ever narrows that further, never widens it. None (the
        # default) means the flag alone already decided.
        self.on_piper_download_consent = None

        # Tier 13: Kokoro - a second, OPTIONAL offline voice (see the
        # HAS_KOKORO block up top). Same lazy-load/cache-the-failure/
        # consent-hook shape as the three Piper attributes just above,
        # kept as separate attributes since the two engines are
        # independent opt-ins with independent failure modes.
        self._kokoro_voice = None
        self._kokoro_load_failed = False
        self.on_kokoro_download_consent = None

        # Tier 25: voice cloning (the HAS_COQUI_TTS block up top) - same
        # lazy-load/cache-the-failure shape as Piper/Kokoro's attributes
        # above, but with no consent-hook/download-dir attributes of its
        # own: TTS_CLONE_REFERENCE_PATH already being set IS the user's
        # deliberate opt-in (see settings/config.py's TTS_CLONE_* docstring).
        self._clone_engine = None
        self._clone_load_failed = False

        # Tier 12: one output stream kept open across utterances - see
        # _persistent_output() and voice/audio_out.py.
        self._persistent = None

    @property
    def is_speaking(self) -> bool:
        return self._utterance_active or self._stream_active

    @is_speaking.setter
    def is_speaking(self, value: bool):
        self._utterance_active = bool(value)

    def set_voice(self, voice_id: str):
        """Switch the edge-tts voice used for English/Hinglish chunks.
        Takes effect on the next speak() call. See set_voice_hi() for the
        separate Hindi voice (Tier 13)."""
        self.voice = voice_id

    def set_voice_hi(self, voice_id: str):
        """Switch the edge-tts voice used for chunks classified as Hindi
        (Devanagari) - see _cloud_voice_and_text(). Independent of
        set_voice(): Wave speaks a bilingual reply with ONE voice per
        language, not one voice for everything."""
        self.voice_hi = voice_id

    def _cloud_voice_and_text(self, clean_text: str):
        """For the edge-tts (cloud) path: classifies `clean_text` (already
        markdown-stripped by clean_for_speech()) and returns
        (voice, prepared_text) - the edge-tts voice to use and the text
        with voice/text_prep.py's "risky" fixes applied for that language
        (Indian-grouped numbers and rupee amounts for English/Hinglish;
        full number/time/date/unit expansion for Hindi, since edge-tts's
        Hindi voice gets most of that wrong too - see text_prep's own
        "risky" mode docstring).

        Deliberately stateless (voice/language.py's classify(), not the
        sticky tracker voice/stt.py reads from): a single reply can
        genuinely switch language from one sentence to the next (English
        then Hindi then English), and speak_stream() already calls this
        once per SENTENCE, not once per reply - the sticky tracker exists
        for short, ambiguous STT utterances, which a generated reply
        sentence isn't."""
        lang = language.classify(clean_text)
        if lang == language.HI:
            return self.voice_hi, text_prep.prepare(clean_text, "hi", numbers="risky")
        return self.voice, text_prep.prepare(clean_text, "en", numbers="risky")

    def set_rate(self, rate_str: str):
        """rate_str is an edge-tts style percentage string, e.g. '+20%' or '-10%'."""
        self.rate = rate_str

    def set_volume(self, level: float):
        """level is 0.0-1.0. Volume is controlled through pygame's mixer
        (not edge-tts's own `volume=` param) so one setting applies whether
        the mixer's already playing or not even initialized yet - _speak_task
        re-applies it right before every playback regardless."""
        self.volume_level = max(0.0, min(1.0, level))
        try:
            pygame.mixer.music.set_volume(self.volume_level)
        except Exception:
            pass  # mixer may not be initialized yet - fine, applied at next playback

    def _ensure_mixer(self) -> bool:
        """Initializes pygame's mixer on first actual use rather than at
        import time, so a slow/failing audio backend can't hold up app
        startup or the STT module that imports this one. Forced to mono at
        _MIXER_RATE regardless of system defaults, both so SDL_mixer's own
        playback sounds consistent and so the array sounddevice/AEC gets
        back from a decoded Sound needs little or no resampling."""
        if self._mixer_ready:
            return True
        try:
            pygame.mixer.init(frequency=self._MIXER_RATE, size=-16, channels=1)
            self._mixer_ready = True
            return True
        except Exception as e:
            logger.error(f"pygame mixer init failed: {e}")
            return False

    def speak(self, text: str):
        if not text:
            return
        # Tier 20: redact secrets / withhold flagged content before this
        # reaches synthesis at all - see brain/speak_guard.py's own
        # docstring for why this has to happen HERE, on the calling
        # thread, rather than inside _speak_task()/_run_utterance().
        try:
            from brain import speak_guard
            text = speak_guard.guard_speech(text)
        except Exception:
            logger.debug("speak(): speak_guard failed - speaking original text", exc_info=True)
        if not text:
            return

        self.stop()
        if self.speak_thread and self.speak_thread.is_alive():
            self.speak_thread.join(timeout=0.3)
        # Tier 11: a streamed reply plays on its own thread, not
        # self.speak_thread - wait for whatever is holding the audio device
        # to notice the stop() above and let go, so the old sentence can't
        # keep playing underneath this one once stop_event is cleared.
        self._wait_playback_idle(timeout=1.0)

        with self._gen_lock:
            self.stop_event.clear()
        self.speak_thread = threading.Thread(target=self._speak_task, args=(text,), daemon=True)
        self.speak_thread.start()

    def _wait_playback_idle(self, timeout: float) -> bool:
        if self._playback_lock.acquire(timeout=max(0.0, timeout)):
            self._playback_lock.release()
            return True
        logger.warning("previous utterance did not stop within %.1fs", timeout)
        return False

    def speak_and_wait(self, text: str) -> bool:
        """Like speak(), but blocks the calling thread until playback has
        actually finished (or been stopped) - meant for a background worker
        thread that wants its own "am I still running" state to correctly
        track "is W.A.V.E. still talking". stop() remains instantly callable
        from any other thread/context to cut this short.

        Returns whether audio was actually heard (see last_playback_ok) -
        True for a normal completed or user-interrupted utterance, False
        only if every playback tier genuinely failed. If text is empty,
        nothing was attempted, so this returns True (nothing to fail at)."""
        if not text:
            return True
        self.speak(text)
        if self.speak_thread:
            self.speak_thread.join()
        return self.last_playback_ok

    def speak_stream(self, chunks) -> bool:
        """Tier 2: like speak_and_wait(), but consumes an ITERABLE of text
        chunks (brain/groq_core.py's WaveBrain.think_stream() generator)
        instead of one finished string, and speaks each sentence as soon
        as it's complete instead of waiting for the whole reply. Blocks the
        calling thread until every sentence that was going to be spoken
        has finished or been stopped, exactly like speak_and_wait() does
        for its one string.

        Tier 11 made this a real pipeline instead of "speak sentence 1,
        THEN read more text, THEN synthesize sentence 2":

          * the calling thread only reads chunks and cuts them into
            sentences (same rules as before - see _split_ready()), so the
            text source (the LLM stream) is never left waiting while a
            sentence plays;
          * every finished sentence is handed to prefetch() straight away,
            so sentence N+1 is being synthesized while sentence N plays;
          * a dedicated speaker thread plays the sentences in order, each
            through the usual _speak_task() chain (sounddevice/pygame/
            Piper/SAPI, AEC feeding, watchdog) - which now starts from the
            prefetched audio instead of a fresh network round-trip.

        Returns the same contract last_playback_ok/speak_and_wait() does:
        True if nothing was ever attempted (an empty or whitespace-only
        stream) or every attempted sentence played fine or was a
        deliberate interruption; False only if at least one sentence's
        playback genuinely failed end-to-end.

        Interruption is stop(): it cuts off the sentence playing, and no
        further sentence of THIS stream ever starts (checked under a lock
        against stop()'s generation counter, so a stop() landing between
        two sentences can't be undone by the next one). A stop() left over
        from some earlier utterance, on the other hand, does not silence a
        new stream - this starts a fresh speaking session the same way
        speak() does, including cutting off anything still playing (and
        waiting for it to let go of the audio device first)."""
        self.stop()
        self._wait_playback_idle(timeout=1.0)
        with self._gen_lock:
            gen0 = self._interrupt_gen
            self.stop_event.clear()

        def interrupted() -> bool:
            return self._interrupt_gen != gen0

        stream = _PieceQueue()
        state = {"all_ok": True, "any_spoken": False}
        speaker = threading.Thread(target=self._stream_speaker, args=(stream, gen0, state),
                                   daemon=True, name="TTS-StreamSpeaker")
        speaker_started = False

        def emit(piece: str):
            nonlocal speaker_started
            piece = piece.strip()
            if not piece or interrupted():
                return
            # Tier 20: same guard as speak() - applied here, on the
            # ORIGINAL calling thread, before the sentence is handed off
            # to prefetch()/the internal speaker thread. See
            # brain/speak_guard.py's own docstring for why it can't be
            # applied any later than this.
            try:
                from brain import speak_guard
                piece = speak_guard.guard_speech(piece).strip()
            except Exception:
                logger.debug("speak_stream(): speak_guard failed - speaking original piece",
                             exc_info=True)
            if not piece:
                return
            self.prefetch(piece)
            stream.put(piece)
            if not speaker_started:
                speaker.start()
                speaker_started = True

        buffer = ""
        try:
            for chunk in chunks:
                if interrupted():
                    break
                if not chunk:
                    continue
                buffer += chunk
                ready, buffer = _split_ready(buffer, final=False)
                for piece in ready:
                    emit(piece)
            if not interrupted():
                ready, buffer = _split_ready(buffer, final=True)
                for piece in ready:
                    emit(piece)
        finally:
            stream.close()
            if speaker_started:
                speaker.join()

        return state["all_ok"] if state["any_spoken"] else True

    def _stream_speaker(self, stream, gen0: int, state: dict):
        """speak_stream()'s playback thread: plays queued sentences in
        order until the stream is closed and drained, or until a stop()
        changes the generation. Before each sentence it tops up the
        prefetch for the next two (when sentences arrive faster than
        _PREFETCH_MAX_PENDING allows, the later ones are only prefetched
        here). Keeps is_speaking True across the short gap between two
        queued sentences; while it is waiting for the text source to
        produce the next sentence, is_speaking is honestly False."""
        try:
            while True:
                piece, lookahead = stream.get(
                    on_wait=lambda: setattr(self, "_stream_active", False),
                    cancelled=lambda: self._interrupt_gen != gen0)
                if piece is None:
                    return
                for nxt in lookahead:
                    self.prefetch(nxt)
                with self._gen_lock:
                    if self._interrupt_gen != gen0:
                        return
                    self.stop_event.clear()
                    self._stream_active = True
                state["any_spoken"] = True
                try:
                    ok = self._speak_stream_piece(piece)
                except Exception as e:
                    logger.error(f"Streaming TTS piece failed: {e}")
                    ok = False
                if not ok:
                    state["all_ok"] = False
        finally:
            self._stream_active = False

    def _speak_stream_piece(self, piece: str) -> bool:
        """Plays one sentence of a streamed reply on the calling (speaker)
        thread. Split out so tests can stand in for real playback at exactly
        this boundary."""
        return bool(self._speak_task(piece))

    # ------------------------------------------------------------------
    # Tier 11: synthesize-ahead (prefetch)
    # ------------------------------------------------------------------

    def prefetch(self, text: str) -> bool:
        """Starts synthesizing `text` with edge-tts in the background, so a
        later _speak_task() of the same text (same voice and rate) starts
        playing without waiting on the network. Returns True if a synthesis
        is now pending for it. A no-op when the local Piper voice is
        preferred (it never makes a network round-trip to hide), when the
        phrase cache already holds it, or when TTS_PREFETCH_ENABLED=false."""
        if not text or not getattr(config, "TTS_PREFETCH_ENABLED", True):
            return False
        if getattr(config, "TTS_PREFER_LOCAL_VOICE", False):
            return False
        # Tier 25: no point prefetching an edge-tts network round-trip that
        # _run_utterance() is about to throw away unused - _speak_via_clone()
        # is tried before anything else once a voice reference is set up.
        # The cheap check, not _get_clone_engine() - this must never trigger
        # the (first-call, ~1.8GB) model load itself.
        if self._clone_reference_ready():
            return False
        clean = clean_for_speech(text)
        if not clean:
            return False
        voice, prepared = self._cloud_voice_and_text(clean)
        key = (prepared, voice, self.rate)
        if phrase_cache.contains(key):
            return False
        with self._prefetch_lock:
            if key in self._prefetch_futures:
                return True
            if len(self._prefetch_futures) >= _PREFETCH_MAX_PENDING:
                return False
            if self._prefetch_pool is None:
                self._prefetch_pool = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="TTS-Prefetch")
            gen = self._interrupt_gen
            future = self._prefetch_pool.submit(self._synthesize_to_bytes, key, gen)
            self._prefetch_futures[key] = future
        return True

    def _synthesize_to_bytes(self, key, gen: int):
        """Prefetch worker: edge-tts synthesis of key=(text, voice, rate)
        into memory. Returns the mp3 bytes, or None if it was cancelled by a
        stop() before it started or synthesis failed (the player then falls
        back to its normal synthesize/offline chain)."""
        if gen != self._interrupt_gen:
            return None
        text, voice, rate = key
        path = os.path.join(self._tmp_dir, f"pre_{next(self._prefetch_seq)}.mp3")
        try:
            asyncio.run(self._synthesize_with(text, voice, rate, path))
            with open(path, "rb") as f:
                data = f.read()
            if data:
                phrase_cache.put(key, data)
            return data or None
        except Exception as e:
            logger.info(f"prefetch synthesis failed ({e}) - will synthesize at playback time")
            return None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _take_prefetched(self, key):
        """The prefetched audio for `key`, waiting for an in-flight
        synthesis if needed (but giving up early on stop()). None when
        nothing was prefetched for it or the prefetch failed."""
        with self._prefetch_lock:
            future = self._prefetch_futures.pop(key, None)
        if future is None:
            return None
        deadline = time.monotonic() + float(getattr(config, "TTS_PREFETCH_WAIT_S", 15.0))
        while True:
            try:
                return future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                if self.stop_event.is_set() or time.monotonic() > deadline:
                    return None
            except Exception:
                return None

    def _cancel_prefetch(self):
        with self._prefetch_lock:
            futures = list(self._prefetch_futures.values())
            self._prefetch_futures.clear()
        for future in futures:
            future.cancel()   # only stops ones that haven't started; started ones just get discarded

    # Short replies Wave says all the time - the voice-loop announcements
    # and the fast-path skills' own fixed answers (brain/handlers/media.py).
    # warm_up() synthesizes them once in the background so even their first
    # use of the session is a phrase-cache hit, not a network round-trip.
    WARM_PHRASES = (
        "Systems active.", "Standing by.", "Turned the volume up.", "Turned the volume down.",
        "Toggled mute.", "Toggled play/pause.", "Skipped to the next track.", "Went to the previous track.",
    )

    def warm_up(self, phrases=None):
        """Tier 11: pays the one-time audio start-up costs (pygame mixer
        init, PortAudio's device scan) at app start, on a background
        thread, instead of inside the very first reply's time-to-first-
        audio, then pre-synthesizes WARM_PHRASES into the phrase cache
        (skipped when offline - nothing breaks, they're just synthesized
        on first use as before). Safe to call more than once; never
        raises. Returns the thread (tests join it)."""
        if not getattr(config, "TTS_WARMUP_ENABLED", True):
            return None
        phrases = self.WARM_PHRASES if phrases is None else phrases

        def _warm():
            try:
                self._ensure_mixer()
            except Exception:
                pass
            if HAS_SOUNDDEVICE:
                try:
                    sd.query_devices(kind="output")
                except Exception as e:
                    logger.info(f"sounddevice warm-up skipped: {e}")
            if getattr(config, "TTS_PREFER_LOCAL_VOICE", False) or not getattr(config, "TTS_PHRASE_CACHE_ENABLED", True):
                return
            for phrase in phrases:
                voice, prepared = self._cloud_voice_and_text(clean_for_speech(phrase))
                key = (prepared, voice, self.rate)
                if phrase_cache.contains(key):
                    continue
                if self._synthesize_to_bytes(key, self._interrupt_gen) is None:
                    break      # offline (or edge-tts down) - don't hammer it for the rest

        t = threading.Thread(target=_warm, daemon=True, name="TTS-WarmUp")
        t.start()
        return t

    def _mark_audio_start(self):
        """Fires on_playback_start once per utterance, at the moment its
        audio is handed to an output device."""
        if self._audio_started:
            return
        self._audio_started = True
        listener = self.on_playback_start
        if listener is None:
            return
        try:
            listener()
        except Exception:
            logger.debug("on_playback_start listener failed", exc_info=True)

    def _notify_playback_pcm(self, pcm, sample_rate: int):
        """Hands the audio about to play to on_playback_pcm (Tier 27)."""
        listener = self.on_playback_pcm
        if listener is None:
            return
        try:
            listener(pcm, sample_rate)
        except Exception:
            logger.debug("on_playback_pcm listener failed", exc_info=True)

    def _force_reset(self):
        """Safety-net watchdog: if playback is ever still 'in progress' far
        longer than the text could possibly take to read out loud (a stuck
        pygame/audio backend, for example), force is_speaking back off so
        the mic's sensitivity doesn't stay wrongly lowered indefinitely."""
        if self.is_speaking:
            logger.warning("TTS watchdog fired - playback ran far longer than expected, forcing reset")
            self.is_speaking = False
            self._stream_active = False
            self.stop_event.set()
            try:
                pygame.mixer.music.stop()
                pygame.mixer.stop()  # also halts any Sound/Channel-based (array) playback
            except Exception:
                pass

    def _speak_task(self, text: str) -> bool:
        """Synthesizes and plays one utterance on the calling thread and
        returns whether audio was heard (also stored in last_playback_ok).
        Holds _playback_lock throughout - see speak()."""
        with self._playback_lock:
            self._run_utterance(text)
            # Read under the lock: _run_utterance's early returns all leave
            # their verdict here (see its finally), and no other utterance
            # can overwrite it until the lock is released.
            return self.last_playback_ok

    def _run_utterance(self, text: str) -> None:
        self.is_speaking = True
        self._audio_started = False
        # Pessimistic until some tier actually plays audio - see
        # last_playback_ok's docstring in __init__.
        played_ok = True
        mp3_path = None
        # ~0.6s per word is a very generous ceiling for normal speech - this
        # is purely a last-resort backstop, not the normal stopping path.
        watchdog = threading.Timer(max(20.0, len(text.split()) * 0.6), self._force_reset)
        watchdog.daemon = True
        watchdog.start()
        try:
            clean_text = clean_for_speech(text)
            if not clean_text or self.stop_event.is_set():
                return  # nothing to say, or interrupted before starting - not a failure

            # Tier 25: the user's own cloned voice, if a reference sample
            # has been set up (Settings -> Avatar) - tried FIRST, ahead of
            # even TTS_PREFER_LOCAL_VOICE below, since providing a
            # reference clip already is the deliberate "I want THIS voice"
            # choice (see settings/config.py's TTS_CLONE_* docstring).
            # _speak_via_clone() is a no-op (returns False immediately) for
            # everyone who hasn't set this up, so nothing changes for them.
            if self._speak_via_clone(clean_text, language.classify(clean_text)):
                return
            if self.stop_event.is_set():
                return

            # Tier 7: TTS_PREFER_LOCAL_VOICE - for someone who has ALREADY
            # opted into Piper (see settings/config.py), try it before
            # edge-tts instead of only ever reaching it through
            # _speak_offline_fallback() once the network path has failed.
            # Default False, so this changes nothing unless both Piper is
            # installed/configured AND this flag is explicitly turned on.
            # Deliberately NOT touching phrase_cache below: that cache
            # exists to skip edge-tts's network round-trip, which Piper
            # never makes in the first place, so caching its output would
            # add disk I/O for no latency win.
            if getattr(config, "TTS_PREFER_LOCAL_VOICE", False):
                if self._speak_via_piper(text_prep.prepare(clean_text, "en", numbers="all")):
                    return
                if self.stop_event.is_set():
                    return
                # Piper isn't usable right now - fall through to the
                # normal edge-tts-first path below exactly as if this
                # flag were off.

            if not self._ensure_mixer():
                # pygame itself is unusable - sounddevice/Piper/SAPI don't
                # depend on it (SAPI doesn't; Piper's array can go straight
                # to sounddevice), so still try the full offline chain
                # rather than giving up immediately.
                played_ok = self._speak_offline_fallback(clean_text, language.classify(clean_text))
                return

            self._seq += 1
            mp3_path = os.path.join(self._tmp_dir, f"seg_{self._seq}.mp3")

            # Tier 3 item #7: skip the edge-tts network round-trip entirely
            # if this exact (text, voice, rate) was already synthesized -
            # see voice/tts_cache.py. volume is deliberately NOT part of the
            # key: it's applied at playback time below (pygame's mixer),
            # never baked into the synthesized bytes themselves, so cached
            # audio is valid regardless of the current volume setting.
            #
            # Tier 11: audio prefetched by speak_stream() (synthesized while
            # the previous sentence was still playing) is used first.
            # Tier 13: the voice and the exact text synthesized both depend
            # on this chunk's own classified language - see
            # _cloud_voice_and_text(). prefetch() above already used the
            # same method on the same clean_text, so a prefetch hit's key
            # matches exactly.
            voice, prepared_text = self._cloud_voice_and_text(clean_text)
            cache_key = (prepared_text, voice, self.rate)
            cached_audio = self._take_prefetched(cache_key)
            if self.stop_event.is_set():
                return  # stopped while waiting on the prefetch - not a failure
            if cached_audio is None:
                cached_audio = phrase_cache.get(cache_key)
            if cached_audio is not None:
                try:
                    with open(mp3_path, "wb") as f:
                        f.write(cached_audio)
                except OSError as e:
                    logger.warning(f"couldn't write cached audio to a temp file, synthesizing fresh: {e}")
                    cached_audio = None   # fall through to a normal synthesis below

            if cached_audio is None:
                try:
                    asyncio.run(self._synthesize_with(prepared_text, voice, self.rate, mp3_path))
                except Exception as e:
                    logger.warning(f"edge-tts unavailable ({e}), falling back to offline voice")
                    played_ok = self._speak_offline_fallback(clean_text, language.classify(clean_text))
                    return

                if self.stop_event.is_set():
                    return  # interrupted mid-synthesis - not a failure

                if not os.path.exists(mp3_path):
                    logger.warning("edge-tts reported success but produced no audio file - falling back")
                    played_ok = self._speak_offline_fallback(clean_text, language.classify(clean_text))
                    return

                try:
                    with open(mp3_path, "rb") as f:
                        phrase_cache.put(cache_key, f.read())
                except OSError:
                    pass   # caching is a pure optimization - a read failure here just costs a future cache hit

            played_ok = self._play_synthesized_mp3(mp3_path)
            if not played_ok and not self.stop_event.is_set():
                played_ok = self._speak_offline_fallback(clean_text, language.classify(clean_text))

        except Exception as e:
            logger.error(f"TTS Error: {e}")
            played_ok = False
        finally:
            watchdog.cancel()
            self.is_speaking = False
            self.last_playback_ok = played_ok
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
            if mp3_path:
                try:
                    os.remove(mp3_path)
                except OSError:
                    pass

    async def _synthesize_with(self, text: str, voice: str, rate: str, out_path: str):
        communicate = edge_tts.Communicate(text, voice, rate=rate, volume=self.VOLUME)
        await communicate.save(out_path)

    # ------------------------------------------------------------------
    # Playback - sounddevice (AEC-capable) first, pygame as the safety net
    # ------------------------------------------------------------------

    def _play_synthesized_mp3(self, mp3_path: str) -> bool:
        """Plays an edge-tts mp3, preferring the decode-once-then-play
        path (enables sounddevice+AEC) and falling back to the original
        file-based pygame.mixer.music path - unchanged from before this
        feature existed - if decoding or every array-based tier fails."""
        decoded = self._decode_mp3_to_pcm(mp3_path)
        if decoded is not None:
            pcm, rate = decoded
            if self._play_pcm(pcm, rate):
                return True
            if self.stop_event.is_set():
                return True  # interrupted partway through the array-based tiers
        return self._play_mp3_via_pygame_music(mp3_path)

    def _decode_mp3_to_pcm(self, mp3_path: str):
        """Decodes a synthesized mp3 into a mono int16 PCM numpy array,
        using pygame's own (SDL_mixer) MP3 decoder so this doesn't need an
        ffmpeg/pydub dependency just to get sounddevice- and
        AEC-compatible samples. Returns (array, sample_rate), or None if
        decoding isn't possible right now (numpy missing, or the file
        itself fails to decode) - the caller falls back to the original
        file-based pygame.mixer.music path in that case."""
        if not HAS_NUMPY:
            return None
        try:
            snd = pygame.mixer.Sound(mp3_path)
            arr = np.asarray(pygame.sndarray.array(snd))
            init = pygame.mixer.get_init()
            rate = int(init[0]) if init else self._MIXER_RATE
            if arr.ndim == 2:  # stereo -> mono
                arr = arr.mean(axis=1)
            return arr.astype(np.int16), rate
        except Exception as e:
            logger.warning(f"Could not decode synthesized audio for sounddevice/AEC playback: {e}")
            return None

    def _play_pcm(self, pcm, sample_rate: int) -> bool:
        """Plays a decoded mono int16 PCM array, preferring sounddevice
        (feeds AEC's far-end reference - see voice/aec.py) and falling
        back to pygame's Sound/Channel API (no file I/O, but no AEC
        reference either) if sounddevice isn't installed or fails to open
        a stream. Returns False if NEITHER worked; the caller (either
        _play_synthesized_mp3 or _speak_via_piper) decides what to try
        next."""
        if self.stop_event.is_set() or pcm is None or len(pcm) == 0:
            return False
        self._notify_playback_pcm(pcm, sample_rate)
        if HAS_SOUNDDEVICE and HAS_NUMPY:
            # Tier 12: one persistent stream first (no device open/close
            # between sentences, a continuous echo reference), then the
            # per-utterance stream, then pygame.
            out = self._persistent_output()
            if out is not None:
                if out.play(pcm, sample_rate, self.stop_event, on_start=self._mark_audio_start):
                    return True
                if self.stop_event.is_set():
                    return True
            if self._play_pcm_via_sounddevice(pcm, sample_rate):
                return True
            if self.stop_event.is_set():
                return True
        return self._play_pcm_via_pygame_array(pcm, sample_rate)

    def _persistent_output(self):
        """voice/audio_out.py's PersistentOutput, created on first use -
        or None when TTS_PERSISTENT_STREAM is off."""
        if not getattr(config, "TTS_PERSISTENT_STREAM", True) or not (HAS_SOUNDDEVICE and HAS_NUMPY):
            return None
        if self._persistent is None:
            try:
                from voice.audio_out import PersistentOutput  # noqa: PLC0415
                self._persistent = PersistentOutput(sd, samplerate=self._MIXER_RATE, aec=aec_processor,
                                                    resample=resample_linear)
            except Exception as e:
                logger.info(f"persistent output stream unavailable: {e}")
                return None
        return self._persistent

    def _play_pcm_via_sounddevice(self, pcm, sample_rate: int) -> bool:
        """Plays a pre-decoded mono int16 PCM array through sounddevice,
        handing the EXACT samples written to the output device to the
        shared AECProcessor's far-end (reference) stream in real time -
        this is what makes real (not heuristic) echo cancellation
        possible; see voice/aec.py. The real-time output callback itself
        only copies samples and does a non-blocking queue hand-off; a
        separate daemon thread drains that queue and makes the actual
        (resample + native AEC) call off the real-time audio thread -
        doing that work directly inside the callback is a well-known way
        to cause audible underruns, so it deliberately isn't done here.

        Returns True if the stream opened and ran to completion (or was
        cleanly interrupted via stop_event); False if sounddevice itself
        isn't usable right now, so the caller falls back to pygame.

        NOTE: this has been reasoned through carefully but NOT verified
        against real audio hardware (this was written in a sandboxed
        environment with no audio device) - if W.A.V.E. sounds glitchy or
        this raises in practice, that's the first place to look; the
        pygame tiers below it are unaffected either way.
        """
        if not (HAS_SOUNDDEVICE and HAS_NUMPY) or pcm is None or len(pcm) == 0:
            return False

        pcm = np.ascontiguousarray(pcm, dtype=np.int16)
        far_end_q: "queue.Queue" = queue.Queue(maxsize=64)
        feeder_stop = threading.Event()

        def _far_end_feeder():
            while True:
                try:
                    block = far_end_q.get(timeout=0.05)
                except queue.Empty:
                    if feeder_stop.is_set():
                        return
                    continue
                try:
                    aec_processor.feed_far_end(block, sample_rate)
                except Exception:
                    pass

        feeder_thread = threading.Thread(target=_far_end_feeder, daemon=True, name="TTS-AEC-FarEnd")
        feeder_thread.start()

        pos = 0
        n_total = len(pcm)
        finished = threading.Event()

        def _callback(outdata, frames, time_info, status):
            nonlocal pos
            if self.stop_event.is_set():
                outdata[:] = 0
                raise sd.CallbackStop()
            chunk = pcm[pos: pos + frames]
            n = len(chunk)
            outdata[:n, 0] = chunk
            if n < frames:
                outdata[n:, 0] = 0
            pos += n
            if n > 0:
                try:
                    far_end_q.put_nowait(chunk.copy())
                except queue.Full:
                    pass  # dropping one far-end block is harmless; blocking here is not
            if pos >= n_total:
                raise sd.CallbackStop()

        opened_ok = True
        try:
            with sd.OutputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                callback=_callback,
                finished_callback=finished.set,
            ) as stream:
                self._mark_audio_start()
                max_wait = max(30.0, n_total / float(sample_rate) + 10.0)
                if not finished.wait(timeout=max_wait):
                    logger.warning("sounddevice playback stalled - aborting and falling back to pygame")
                    stream.abort()
                    opened_ok = False
        except Exception as e:
            logger.warning(f"sounddevice playback unavailable ({e}), falling back to pygame")
            opened_ok = False
        finally:
            feeder_stop.set()
            feeder_thread.join(timeout=0.5)

        return opened_ok

    def _play_pcm_via_pygame_array(self, pcm, sample_rate: int) -> bool:
        """Plays a decoded PCM array via pygame's Sound/Channel API
        (no temp file needed). Used when sounddevice isn't installed, or
        its playback attempt itself failed. No AEC far-end reference on
        this path (only the sounddevice paths feed voice/aec.py)."""
        if not HAS_NUMPY or not self._ensure_mixer():
            return False
        try:
            init = pygame.mixer.get_init()
            mixer_rate = int(init[0]) if init else sample_rate
            if sample_rate != mixer_rate:
                pcm = resample_linear(pcm, sample_rate, mixer_rate)
            sound = pygame.sndarray.make_sound(np.ascontiguousarray(pcm, dtype=np.int16))
            channel = sound.play()
            if channel is None:
                return False
            self._mark_audio_start()
            channel.set_volume(self.volume_level)
            while channel.get_busy():
                if self.stop_event.is_set():
                    channel.stop()
                    break
                pygame.time.wait(40)
            return True
        except Exception as e:
            logger.warning(f"pygame array playback failed: {e}")
            return False

    def _play_mp3_via_pygame_music(self, mp3_path: str) -> bool:
        """The original playback path (unchanged): streams the mp3 straight
        from disk via pygame.mixer.music, which can be stopped INSTANTLY at
        any point - this is the long-standing, reliable fallback once both
        sounddevice and in-memory decode have been tried."""
        if not self._ensure_mixer():
            return False
        try:
            pygame.mixer.music.load(mp3_path)
            pygame.mixer.music.set_volume(self.volume_level)
            pygame.mixer.music.play()
            self._mark_audio_start()
            while pygame.mixer.music.get_busy():
                if self.stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                pygame.time.wait(40)
            return True
        except Exception as e:
            logger.error(f"pygame.mixer.music playback failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Offline fallback chain: local Piper voice, then Windows SAPI
    # ------------------------------------------------------------------

    def _speak_offline_fallback(self, text: str, lang: str = "en") -> bool:
        """No internet / Microsoft's TTS endpoint unreachable (or edge-tts
        produced nothing) - try one or two local engines before falling
        back to the old Windows SAPI voice, so W.A.V.E. can always still
        speak instead of going silent. Returns whether any path actually
        produced audible speech.

        Tier 13: `text` gets voice/text_prep.py's FULL number/time/date/
        unit expansion (numbers="all") before any engine sees it, not
        just the "risky" subset edge-tts needed - Piper's espeak-ng
        phonemizer, Kokoro's phonemizer, and old SAPI all read raw digits
        far worse than edge-tts does.

        `lang` (from voice/language.py's classify(), same as
        _cloud_voice_and_text() already uses for edge-tts) decides
        ROUTING, not just text prep - and the two aren't symmetric:

        Piper and SAPI are each a single, fixed ENGLISH voice (no
        per-language model to switch to, unlike edge-tts's voice_hi - see
        _cloud_voice_and_text()'s docstring for why that's a deliberate,
        honest limitation). Handed Hindi text, Piper's espeak-ng
        phonemizer doesn't raise - it just mispronounces the Devanagari
        and still reports success, which would silently stop this method
        before ever reaching Kokoro, the one engine that can actually
        speak Hindi. So for lang="hi" with Kokoro enabled, Kokoro is
        tried FIRST; Piper (still English - a last-resort approximation,
        not a translation) only gets a turn if Kokoro itself isn't
        usable right now. For every other `lang`, nothing changes from
        before Tier 13: Piper, then SAPI. Kokoro could technically speak
        English too, but Piper already sounds at least as good there and
        doesn't cost Kokoro's ~2x CPU latency (see docs/VOICE_LOCAL.md),
        so English is deliberately never routed through it."""
        if self.stop_event.is_set():
            return True
        if lang == language.HI and getattr(config, "TTS_KOKORO_ENABLED", False):
            if self._speak_via_kokoro(text_prep.prepare(text, "hi", numbers="all"), lang):
                return True
            if self.stop_event.is_set():
                return True
        english_text = text_prep.prepare(text, "en", numbers="all")
        if self._speak_via_piper(english_text):
            return True
        if self.stop_event.is_set():
            return True
        return self._speak_via_sapi(english_text)

    def _default_piper_voices_dir(self) -> str:
        """Where an auto-downloaded Piper voice is saved - TTS_LOCAL_VOICES_DIR
        if set, else <data dir>/voice_models/piper (the same `<data
        dir>/voice_models/...` shape voice/wakeword.py already uses for
        openWakeWord's own downloaded files), so auto-download works with
        zero configuration beyond turning TTS_LOCAL_AUTO_DOWNLOAD on."""
        configured = (getattr(config, "TTS_LOCAL_VOICES_DIR", "") or "").strip()
        return configured or os.path.join(config.DATA_DIR, "voice_models", "piper")

    def _confirm_piper_download(self, voice_name: str) -> bool:
        """TTS_LOCAL_AUTO_DOWNLOAD=true (default false) is this project's
        primary consent mechanism: turning it on is itself the deliberate,
        legible opt-in act - the same posture piper-tts itself already has
        by not being in requirements.txt. No GUI dialog blocks this by
        default (Wave is voice-first; a silently-waiting modal would just
        strand a headless/background session). on_piper_download_consent,
        if a Settings UI ever sets it, gets the final say per download:
        returning exactly False declines even with the flag on. Any other
        return value (True, None, or the hook raising) proceeds - the
        flag already gave consent, so a broken hook must not be the thing
        that silently disables a working feature."""
        hook = self.on_piper_download_consent
        if hook is None:
            return True
        try:
            return hook(voice_name) is not False
        except Exception:
            logger.debug("on_piper_download_consent hook failed - proceeding "
                        "(TTS_LOCAL_AUTO_DOWNLOAD already gave consent)", exc_info=True)
            return True

    def _verify_piper_voice_checksum(self, voice_name: str, onnx_path: str) -> bool:
        """True if `onnx_path` matches PIPER_VOICE_CHECKSUMS' known-good
        (sha256, size) for `voice_name` - removing the file and returning
        False on any mismatch, so a corrupted or tampered download is
        never loaded as if it were the real voice.

        A voice with NO entry in PIPER_VOICE_CHECKSUMS (someone's own
        TTS_LOCAL_DEFAULT_VOICE, not the one this project ships and has
        pinned a hash for) is let through with a logged warning rather
        than refused: choosing a different voice is already a deliberate,
        informed choice, and this project has no way to pin a hash for a
        voice it doesn't know about ahead of time."""
        expected = PIPER_VOICE_CHECKSUMS.get(voice_name)
        if expected is None:
            logger.warning(
                f"no pinned checksum for Piper voice '{voice_name}' - the download "
                f"is NOT verified. Only {', '.join(sorted(PIPER_VOICE_CHECKSUMS)) or '(none)'} "
                f"{'is' if len(PIPER_VOICE_CHECKSUMS) == 1 else 'are'} pinned."
            )
            return True
        try:
            size = os.path.getsize(onnx_path)
            if size != expected["size"]:
                logger.warning(f"downloaded '{voice_name}' is {size} bytes, expected "
                               f"{expected['size']} - discarding it")
                os.remove(onnx_path)
                return False
            digest = hashlib.sha256()
            with open(onnx_path, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected["sha256"]:
                logger.warning(f"downloaded '{voice_name}' failed its checksum check - discarding it")
                os.remove(onnx_path)
                return False
        except OSError as e:
            logger.warning(f"couldn't verify '{voice_name}''s checksum: {e}")
            return False
        return True

    def _download_default_piper_voice(self):
        """Auto-downloads config.TTS_LOCAL_DEFAULT_VOICE via piper-tts's
        own `python -m piper.download_voices` (so this project never has
        to track Piper's own catalog/URLs itself) into
        _default_piper_voices_dir(), verifies it against
        PIPER_VOICE_CHECKSUMS, and returns the resulting .onnx path.
        Returns None - never raises - if TTS_LOCAL_AUTO_DOWNLOAD is off,
        nothing's configured, consent was declined, or the download
        fails/times out/errors/fails its checksum. Brings back the
        Oct 18-26 effort's auto-download (same CLI, same shape) with
        consent and a checksum layered on top, per the Tier 13 roadmap
        entry - see docs/VOICE_LOCAL.md."""
        if not getattr(config, "TTS_LOCAL_AUTO_DOWNLOAD", False):
            return None
        voice_name = (getattr(config, "TTS_LOCAL_DEFAULT_VOICE", "") or "").strip()
        if not voice_name:
            return None
        voices_dir = self._default_piper_voices_dir()
        onnx_path = os.path.join(voices_dir, f"{voice_name}.onnx")
        if os.path.exists(onnx_path):
            return onnx_path
        if not self._confirm_piper_download(voice_name):
            logger.info(f"Piper voice '{voice_name}' download declined - staying on SAPI for offline fallback")
            return None
        try:
            os.makedirs(voices_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"couldn't create '{voices_dir}' for the Piper voice download: {e}")
            return None
        try:
            result = subprocess.run(
                [sys.executable, "-m", "piper.download_voices", voice_name],
                cwd=voices_dir,
                timeout=float(getattr(config, "TTS_LOCAL_DOWNLOAD_TIMEOUT_S", 45.0)),
                capture_output=True, text=True,
            )
        except subprocess.TimeoutExpired:
            logger.warning(f"piper.download_voices timed out downloading '{voice_name}'")
            return None
        except Exception as e:
            logger.warning(f"piper.download_voices couldn't run: {e}")
            return None
        if result.returncode != 0:
            logger.warning(f"piper.download_voices failed ({result.returncode}): "
                           f"{(result.stderr or '').strip()[:200]}")
            return None
        if not os.path.exists(onnx_path):
            logger.warning(f"piper.download_voices reported success but '{onnx_path}' doesn't exist")
            return None
        if not self._verify_piper_voice_checksum(voice_name, onnx_path):
            return None
        logger.info(f"Downloaded and verified Piper voice '{voice_name}'.")
        return onnx_path

    def _get_piper_voice(self):
        """Lazily loads and caches the local Piper voice model. Returns
        None - never raises - if piper-tts isn't installed, no model path
        is configured and auto-download can't provide one, or the model
        fails to load, and remembers that failure for the rest of the
        session (same lazy/cache-the-failure pattern as stt.py's
        _get_whisper_model) so a missing/broken install doesn't retry the
        load on every single offline fallback.

        Tier 13: TTS_LOCAL_MODEL_PATH, if set, still works exactly as
        before (a user-chosen voice fails as a real "it's missing" error,
        never silently substituted) - auto-download is only ever reached
        when NOTHING is configured at all, via _download_default_piper_voice()."""
        if not HAS_PIPER or not getattr(config, "TTS_LOCAL_ENABLED", True):
            return None
        if self._piper_load_failed:
            return None
        if self._piper_voice is not None:
            return self._piper_voice
        model_path = (getattr(config, "TTS_LOCAL_MODEL_PATH", "") or "").strip()
        if not model_path:
            model_path = self._download_default_piper_voice()
            if not model_path:
                self._piper_load_failed = True
                return None
        elif not os.path.exists(model_path):
            logger.warning(f"TTS_LOCAL_MODEL_PATH is set but '{model_path}' doesn't exist")
            self._piper_load_failed = True
            return None
        try:
            self._piper_voice = PiperVoice.load(model_path)
            logger.info(f"Loaded local Piper voice '{model_path}' for offline fallback.")
            return self._piper_voice
        except Exception as e:
            logger.warning(f"Piper voice load failed, staying on SAPI for offline fallback: {e}")
            self._piper_load_failed = True
            return None

    def _default_kokoro_model_dir(self) -> str:
        """Where Kokoro's two downloaded model files are saved -
        TTS_KOKORO_MODEL_DIR if set, else <data dir>/voice_models/kokoro
        - same shape as _default_piper_voices_dir() above and
        voice/wakeword.py's own downloaded files."""
        configured = (getattr(config, "TTS_KOKORO_MODEL_DIR", "") or "").strip()
        return configured or os.path.join(config.DATA_DIR, "voice_models", "kokoro")

    def _confirm_kokoro_download(self, filename: str) -> bool:
        """Same shape as _confirm_piper_download() above:
        TTS_KOKORO_AUTO_DOWNLOAD=true (default false) is the primary,
        legible consent act - its own flag, separate from Piper's, since
        Kokoro's two files are a bigger (~120MB) download and deserve
        their own deliberate opt-in rather than riding along on Piper's.
        on_kokoro_download_consent, if a Settings UI ever sets it, gets
        the final say per file, and a bare False declines even with the
        flag on."""
        hook = self.on_kokoro_download_consent
        if hook is None:
            return True
        try:
            return hook(filename) is not False
        except Exception:
            logger.debug("on_kokoro_download_consent hook failed - proceeding "
                        "(TTS_KOKORO_AUTO_DOWNLOAD already gave consent)", exc_info=True)
            return True

    def _verify_kokoro_file_checksum(self, filename: str, path: str) -> bool:
        """True if `path` matches KOKORO_FILE_CHECKSUMS' known-good
        (sha256, size) for `filename` - removes the file and returns
        False on any mismatch, same reasoning as
        _verify_piper_voice_checksum() above. Unlike Piper's open-ended
        voice catalog, Kokoro has exactly two fixed filenames, both
        always pinned here - an unpinned name reaching this would mean
        this project's own manifest is stale, not a legitimate per-user
        choice, but it still only warns rather than refuses, for the same
        "don't silently brick a working feature" reasoning."""
        expected = KOKORO_FILE_CHECKSUMS.get(filename)
        if expected is None:
            logger.warning(f"no pinned checksum for Kokoro file '{filename}' - the download is NOT verified")
            return True
        try:
            size = os.path.getsize(path)
            if size != expected["size"]:
                logger.warning(f"downloaded '{filename}' is {size} bytes, expected "
                               f"{expected['size']} - discarding it")
                os.remove(path)
                return False
            digest = hashlib.sha256()
            with open(path, "rb") as f:
                for block in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected["sha256"]:
                logger.warning(f"downloaded '{filename}' failed its checksum check - discarding it")
                os.remove(path)
                return False
        except OSError as e:
            logger.warning(f"couldn't verify '{filename}''s checksum: {e}")
            return False
        return True

    def _download_one_kokoro_file(self, filename: str, dest_path: str) -> bool:
        """Streams one of Kokoro's release assets to `dest_path`.
        kokoro-onnx ships no download CLI of its own (unlike piper-tts's
        `python -m piper.download_voices`, which _download_default_piper_
        voice() above just shells out to), so this is a plain streamed
        `requests` GET instead - requests is already a core dependency
        (see requirements.txt), imported locally here the same way
        _persistent_output() above locally imports PersistentOutput: a
        network download is a rare, optional code path, not something
        every import of this module should pay for. Downloads to a
        '.part' sibling first and renames on success, so a crash or
        interrupted download never leaves a partial file sitting at the
        real filename looking valid. Returns False - never raises,
        and never leaves a partial file behind - on any network, timeout,
        or disk error."""
        import requests  # noqa: PLC0415  (optional, rarely-hit path - see docstring)

        url = f"{KOKORO_RELEASE_BASE_URL}/{filename}"
        tmp_path = dest_path + ".part"
        try:
            timeout = float(getattr(config, "TTS_KOKORO_DOWNLOAD_TIMEOUT_S", 60.0))
            with requests.get(url, stream=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            os.replace(tmp_path, dest_path)
            return True
        except Exception as e:
            logger.warning(f"couldn't download Kokoro file '{filename}': {e}")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False

    def _download_kokoro_files(self):
        """Auto-downloads Kokoro's two fixed model files into
        _default_kokoro_model_dir() and verifies each against
        KOKORO_FILE_CHECKSUMS. Returns (model_path, voices_path), or
        None - never raises - if TTS_KOKORO_AUTO_DOWNLOAD is off, consent
        was declined for either file, or either download fails/times
        out/errors/fails its checksum. Same consent+checksum shape as
        _download_default_piper_voice() above - see
        docs/VOICE_LOCAL.md."""
        if not getattr(config, "TTS_KOKORO_AUTO_DOWNLOAD", False):
            return None
        model_dir = self._default_kokoro_model_dir()
        model_path = os.path.join(model_dir, "kokoro-v1.0.int8.onnx")
        voices_path = os.path.join(model_dir, "voices-v1.0.bin")
        if os.path.exists(model_path) and os.path.exists(voices_path):
            return model_path, voices_path
        try:
            os.makedirs(model_dir, exist_ok=True)
        except OSError as e:
            logger.warning(f"couldn't create '{model_dir}' for Kokoro's model download: {e}")
            return None
        for filename, dest in (("kokoro-v1.0.int8.onnx", model_path),
                                ("voices-v1.0.bin", voices_path)):
            if os.path.exists(dest):
                continue
            if not self._confirm_kokoro_download(filename):
                logger.info(f"Kokoro file '{filename}' download declined - "
                            f"Hindi offline fallback stays unavailable")
                return None
            if not self._download_one_kokoro_file(filename, dest):
                return None
            if not self._verify_kokoro_file_checksum(filename, dest):
                return None
        logger.info("Downloaded and verified Kokoro's model files.")
        return model_path, voices_path

    def _get_kokoro_voice(self):
        """Lazily loads and caches the local Kokoro engine. Returns
        None - never raises - if kokoro-onnx isn't installed,
        TTS_KOKORO_ENABLED is off, no model files are present and
        auto-download can't provide them, or loading fails, and
        remembers that failure for the rest of the session - same
        lazy/cache-the-failure pattern as _get_piper_voice() above and
        stt.py's _get_whisper_model.

        Unlike Piper's TTS_LOCAL_MODEL_PATH, there's no "user-set exact
        file path" option here: Kokoro ships exactly one model (voice
        choice is a `voice=` argument to .create(), not a different
        model file - see _speak_via_kokoro()), so pre-placing both files
        yourself under TTS_KOKORO_MODEL_DIR works too, with or without
        TTS_KOKORO_AUTO_DOWNLOAD - auto-download is only ever reached
        when one or both files are actually missing."""
        if not HAS_KOKORO or not getattr(config, "TTS_KOKORO_ENABLED", False):
            return None
        if self._kokoro_load_failed:
            return None
        if self._kokoro_voice is not None:
            return self._kokoro_voice
        model_dir = self._default_kokoro_model_dir()
        model_path = os.path.join(model_dir, "kokoro-v1.0.int8.onnx")
        voices_path = os.path.join(model_dir, "voices-v1.0.bin")
        if not (os.path.exists(model_path) and os.path.exists(voices_path)):
            downloaded = self._download_kokoro_files()
            if not downloaded:
                self._kokoro_load_failed = True
                return None
            model_path, voices_path = downloaded
        try:
            self._kokoro_voice = Kokoro(model_path, voices_path)
            logger.info("Loaded local Kokoro voice for bilingual offline fallback.")
            return self._kokoro_voice
        except Exception as e:
            logger.warning(f"Kokoro load failed, staying on Piper/SAPI for offline fallback: {e}")
            self._kokoro_load_failed = True
            return None

    def _speak_via_piper(self, text: str) -> bool:
        """Synthesizes+plays `text` with the local Piper neural voice.
        Returns True if it actually produced audible speech, False if
        Piper isn't usable right now (caller falls through to SAPI,
        same as before this fallback existed)."""
        if not HAS_NUMPY:
            return False
        voice = self._get_piper_voice()
        if voice is None:
            return False
        try:
            pcm_bytes = bytearray()
            sample_rate = None
            channels = 1
            for chunk in voice.synthesize(text):
                if sample_rate is None:
                    sample_rate = int(chunk.sample_rate)
                    channels = int(getattr(chunk, "sample_channels", 1) or 1)
                pcm_bytes.extend(chunk.audio_int16_bytes)
            if not pcm_bytes or not sample_rate:
                return False
            pcm = np.frombuffer(bytes(pcm_bytes), dtype=np.int16)
            if channels > 1:
                pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
            return self._play_pcm(pcm, sample_rate)
        except Exception as e:
            logger.warning(f"Piper synthesis/playback failed, falling back to SAPI: {e}")
            return False

    def _speak_via_kokoro(self, text: str, lang: str) -> bool:
        """Synthesizes+plays `text` with the local Kokoro neural voice -
        the one offline engine that can actually speak Hindi.
        lang=="hi" picks TTS_KOKORO_VOICE_HI and Kokoro's own "hi"
        language code; anything else picks TTS_KOKORO_VOICE_EN and
        Kokoro's "en-us" (kokoro-onnx's .create() takes both a voice
        name AND a language code - they're kept in sync here rather
        than left for the caller to mismatch). Returns True if it
        actually produced audible speech, False if Kokoro isn't usable
        right now (caller falls through to SAPI - same contract as
        _speak_via_piper() above)."""
        if not HAS_NUMPY:
            return False
        kokoro = self._get_kokoro_voice()
        if kokoro is None:
            return False
        if lang == language.HI:
            voice_name = getattr(config, "TTS_KOKORO_VOICE_HI", "hf_alpha")
            kokoro_lang = "hi"
        else:
            voice_name = getattr(config, "TTS_KOKORO_VOICE_EN", "af_heart")
            kokoro_lang = "en-us"
        try:
            samples, sample_rate = kokoro.create(text, voice=voice_name, lang=kokoro_lang)
            if samples is None or len(samples) == 0:
                return False
            pcm = np.clip(samples, -1.0, 1.0)
            pcm = (pcm * 32767.0).astype(np.int16)
            return self._play_pcm(pcm, int(sample_rate))
        except Exception as e:
            logger.warning(f"Kokoro synthesis/playback failed, falling back to SAPI: {e}")
            return False

    def _clone_reference_ready(self) -> bool:
        """Cheap, no-load check: is there actually a usable voice-clone
        setup right now? Package installed, TTS_CLONE_ENABLED, and a
        reference file that exists - nothing here touches the (slow,
        first-call-downloads-1.8GB) engine itself. Safe to call often,
        e.g. from prefetch() per sentence, unlike _get_clone_engine()."""
        if not HAS_COQUI_TTS or not getattr(config, "TTS_CLONE_ENABLED", True):
            return False
        reference_path = (getattr(config, "TTS_CLONE_REFERENCE_PATH", "") or "").strip()
        return bool(reference_path) and os.path.exists(reference_path)

    def _get_clone_engine(self):
        """Lazily loads and caches the Coqui XTTS-v2 engine. Returns None
        - never raises - if _clone_reference_ready() says there's nothing
        to load, or the model fails to load, and remembers that failure
        for the rest of the session - same lazy/cache-the-failure pattern
        as _get_piper_voice()/_get_kokoro_voice() above. The first real
        call downloads the ~1.8GB XTTS-v2 checkpoint (Coqui TTS's own
        cache dir, not this project's) - slow, one-time, and entirely
        expected; nothing here tries to hide that or put a progress bar
        on it. Only ever called from _speak_via_clone() - everywhere else
        that just needs to know "is cloning set up right now" (e.g.
        prefetch()) uses the cheap check above instead."""
        if not self._clone_reference_ready():
            return None
        if self._clone_load_failed:
            return None
        if self._clone_engine is not None:
            return self._clone_engine
        reference_path = getattr(config, "TTS_CLONE_REFERENCE_PATH", "")
        model_name = getattr(config, "TTS_CLONE_MODEL_NAME",
                              "tts_models/multilingual/multi-dataset/xtts_v2")
        try:
            self._clone_engine = CoquiTTS(model_name)
            logger.info(f"Loaded Coqui XTTS-v2 for voice cloning from '{reference_path}'.")
            return self._clone_engine
        except Exception as e:
            logger.warning(f"Voice-clone engine load failed, staying on edge-tts/Piper/Kokoro: {e}")
            self._clone_load_failed = True
            return None

    def _speak_via_clone(self, text: str, lang: str) -> bool:
        """Synthesizes+plays `text` in the user's own cloned voice.
        Returns True if it actually produced audible speech, False if
        cloning isn't set up or usable right now (caller falls through to
        the normal edge-tts/Piper/Kokoro/SAPI chain exactly as if this
        method didn't exist - see _run_utterance()).

        lang comes from voice/language.py's classify() - XTTS-v2 genuinely
        supports both "en" and "hi" (unlike Piper/SAPI's fixed English-only
        voice), so hinglish (romanized Hindi) is the only case mapped to
        "en" here, same reasoning text_prep.prepare()'s own "en" path
        already uses for anything that isn't classified Devanagari."""
        if not HAS_NUMPY:
            return False
        engine = self._get_clone_engine()
        if engine is None:
            return False
        reference_path = getattr(config, "TTS_CLONE_REFERENCE_PATH", "")
        xtts_lang = "hi" if lang == language.HI else "en"
        prepared = text_prep.prepare(text, xtts_lang, numbers="all")
        try:
            samples = engine.tts(text=prepared, speaker_wav=reference_path, language=xtts_lang)
            if samples is None or len(samples) == 0:
                return False
            pcm = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
            pcm = (pcm * 32767.0).astype(np.int16)
            sample_rate = int(getattr(getattr(engine, "synthesizer", None),
                                       "output_sample_rate", 24000) or 24000)
            return self._play_pcm(pcm, sample_rate)
        except Exception as e:
            logger.warning(f"Voice-clone synthesis/playback failed, falling back to edge-tts: {e}")
            return False

    def _speak_via_sapi(self, text: str) -> bool:
        """Last-resort offline voice: the old local Windows voice, so
        W.A.V.E. can still speak instead of staying silent even if Piper
        also isn't available. Same sentence-by-sentence approach as
        before, since SAPI (unlike pygame/sounddevice) can only be
        interrupted cleanly between sentences. Returns whether SAPI
        actually spoke at least one sentence. No AEC reference on this
        path (SAPI owns its own playback device access directly)."""
        spoke_any = False
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            # Map our "+8%"/"-10%" style rate to SAPI's -10..+10 integer
            # scale, and our 0.0-1.0 volume to SAPI's 0-100, so Settings >
            # Voice still has an effect even when we've dropped to offline.
            try:
                rate_pct = int(self.rate.replace("%", ""))
            except ValueError:
                rate_pct = 8
            speaker.Rate = max(-10, min(10, round(rate_pct / 10)))
            speaker.Volume = int(self.volume_level * 100)

            voices = speaker.GetVoices()
            for i in range(voices.Count):
                if "Zira" in voices.Item(i).GetDescription():
                    speaker.Voice = voices.Item(i)
                    break

            sentences = re.split(r"(?<=[.!?]) +|\n+", text)
            for sentence in sentences:
                if self.stop_event.is_set():
                    break
                if sentence.strip():
                    speaker.Speak(sentence, 1)
                    self._mark_audio_start()
                    while not self.stop_event.is_set():
                        if speaker.WaitUntilDone(10):
                            break
                    spoke_any = True
                    if self.stop_event.is_set():
                        speaker.Speak("", 2)
                        break
            pythoncom.CoUninitialize()
        except Exception as e:
            logger.error(f"Offline fallback TTS (SAPI) also failed: {e}")
            return False
        # Being interrupted before the very first sentence finished counts
        # as "not a failure" too, same as every other tier - only an
        # exception above (or finishing with nothing spoken AND no
        # interruption, e.g. SAPI itself never threw but also never got a
        # sentence out) should read as a real failure.
        return spoke_any or self.stop_event.is_set()

    def stop(self):
        """Stops whatever is playing now AND ends any streamed reply in
        progress (no further sentence of it will start) - see
        speak_stream(). Pending prefetches are dropped too."""
        with self._gen_lock:
            self._interrupt_gen += 1
            self.stop_event.set()
        self._cancel_prefetch()
        try:
            pygame.mixer.music.stop()
            pygame.mixer.stop()  # also halts any Sound/Channel-based (array) playback
        except Exception:
            pass


tts_engine = VoiceEngine()
