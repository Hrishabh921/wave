"""
voice/tts_cache.py — Tier 3 item #7: a TTS cache.
================================================================================
voice/tts.py's primary engine (edge-tts) is a real network round-trip to
Microsoft's service for every single speak() call - including one for the
exact same text spoken twice in a row. Right now there's no way for that
to NOT happen: _speak_task() synthesizes to a temp mp3, plays it, and
deletes it, every time, with nothing remembered in between.

This is a small, thread-safe, in-memory LRU of already-synthesized audio
bytes, keyed on exactly what edge-tts's own output actually depends on
(see tts.py's _synthesize(): text, voice, rate - NOT volume, which is
applied at PLAYBACK time via pygame's mixer, not baked into the
synthesized bytes, so it doesn't belong in the key). A cache hit skips
the network call entirely; everything downstream of that (decode, play,
the sounddevice/pygame/Piper/SAPI fallback chain) is unchanged either way.

Adapted from SARA's own audio/tts/cache.py, which is this same idea
(OrderedDict + lock, get/put, a max size, a max-length gate so one long
reply can't evict a cache full of short, likely-to-repeat phrases) - but
NOT ported file-for-file. Two real differences, both because Wave's own
candidate phrases are a different shape than SARA's:
  - SARA's gate defaults to 40 characters, sized for its own short wake-
    word acks ("Hi.", "Okay."). Wave's own best candidates named in the
    roadmap - brain/confirm_gate.py's pending_message()/denied_message() -
    run 150-270 characters once a real file name is filled in (measured,
    not guessed - see the default below). A 40-character gate would
    exclude the exact messages this was asked to help with.
  - Honest caveat the roadmap doesn't mention: reminders/context.py's
    due_message()/upcoming_message()/followup_message() all interpolate
    a clock time or a relative "in N minutes"/"N minutes ago" phrase, so
    despite being called "templates" their actual rendered text is
    different almost every time they fire - this cache won't help THOSE
    specifically nearly as often as the name suggests. It still helps
    plenty elsewhere: a repeated confirm/deny ask, reminders/context.py's
    own fixed fallback ("You have a reminder."), and any other exact
    phrase Wave happens to say twice.

A plain class with a module-level singleton (phrase_cache below), not
SARA's bare module-level functions: Wave's own tests construct their own
isolated instances this way elsewhere (e.g. automation/proactive.py's
ProactiveEngine, brain/memory_consolidation.py's MemoryConsolidator), and
it means a test doesn't have to reach into this module's private globals
and restore them afterward just to get a clean cache per test.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional, Tuple

from settings.config import config

CacheKey = Tuple[str, str, str]  # (text, voice, rate) - see module docstring for why not volume


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    size: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0


class PhraseCache:
    """Thread-safe LRU of (text, voice, rate) -> synthesized audio bytes.
    `max_entries` and `max_text_len` default to the live config values
    (TTS_PHRASE_CACHE_SIZE / TTS_PHRASE_CACHE_MAXLEN) so production code
    never has to pass them explicitly; tests pass small values directly
    to exercise eviction without synthesizing dozens of phrases."""

    def __init__(self, max_entries: Optional[int] = None, max_text_len: Optional[int] = None):
        self.max_entries = max_entries if max_entries is not None else config.TTS_PHRASE_CACHE_SIZE
        self.max_text_len = max_text_len if max_text_len is not None else config.TTS_PHRASE_CACHE_MAXLEN
        self._data: "OrderedDict[CacheKey, bytes]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: CacheKey) -> Optional[bytes]:
        if not config.TTS_PHRASE_CACHE_ENABLED:
            return None
        with self._lock:
            audio = self._data.get(key)
            if audio is None:
                self._misses += 1
                return None
            self._data.move_to_end(key)   # LRU: a hit counts as freshly used
            self._hits += 1
            return audio

    def contains(self, key: CacheKey) -> bool:
        """Tier 11: membership check that does NOT count as a hit or a miss
        and doesn't touch LRU order - voice/tts.py's prefetch() uses it to
        skip synthesizing a phrase the cache already holds, without skewing
        the hit-rate stats the real playback lookup reports."""
        if not config.TTS_PHRASE_CACHE_ENABLED:
            return False
        with self._lock:
            return key in self._data

    def put(self, key: CacheKey, audio: bytes) -> None:
        """No-op (not an error) for text longer than max_text_len or empty
        audio - this cache is deliberately for short, likely-to-repeat
        phrases, not a general-purpose store for every reply Wave ever
        speaks. key[0] is the text; see the CacheKey type alias above."""
        if not config.TTS_PHRASE_CACHE_ENABLED or not audio:
            return
        text = key[0]
        if not text or len(text) > self.max_text_len:
            return
        with self._lock:
            self._data[key] = audio
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                self._data.popitem(last=False)   # evict least-recently-used

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(hits=self._hits, misses=self._misses, size=len(self._data))


# One shared cache for the whole process, the same singleton shape as
# voice/tts.py's own `tts_engine = VoiceEngine()` just below it.
phrase_cache = PhraseCache()
