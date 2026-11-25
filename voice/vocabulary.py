"""
voice/vocabulary.py — Tier 37: the names Whisper should know.

Whisper turns names it hasn't seen into near-misses ("Jaipur" -> "Jypur",
"Arijit Singh" -> "Arogyt sing"). SARA measured the fix on its own speech
(2026-10-30): listing the user's names in Whisper's prompt took English word
errors from 5.7% to 2.1% on clean audio (9.3% -> 7.7% at 8 dB of noise), for
~10 ms a decode, and no listed word ever showed up on silence or noise. A
bigger Whisper model didn't help. Wave's prompt had only the wake words and
a style line.

Now it also gets, in this order: STT_EXTRA_VOCABULARY (.env, comma-
separated), the names in Wave's memory - the user's own, family, friends,
places ("wife: Priya" gives "Priya"; any Capitalised word in a short fact) -
and a few apps Whisper garbles. Short on purpose: Whisper keeps only the
last ~224 prompt tokens. Cached for 60 s; never raises. STT_VOCABULARY=off
turns it off.
"""
import logging
import re
import threading
import time

from settings.config import config

logger = logging.getLogger("STT.vocabulary")

MAX_WORDS = 24
CACHE_S = 60.0
_APPS = ("WhatsApp", "YouTube", "Spotify", "Instagram")
# Facts whose whole (short) value is a name or a place.
_NAME_KEYS = re.compile(r"name|wife|husband|mother|father|mom|dad|sister|brother|friend|son|daughter|boss|"
                        r"partner|girlfriend|boyfriend|city|town|village|place|pet|dog|cat|maa|papa|bhai|"
                        r"behen|didi|beta|beti|dost|biwi|pati|patni|shehar|gaon", re.I)
_CAPITALISED = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_DATE_NOTE = re.compile(r"\s*\((?:mon|tue|wed|thu|fri|sat|sun)\w* \d{1,2} \w+ \d{4}\)", re.I)
_STOP = {"The", "And", "But", "For", "With", "From", "This", "That", "Yes", "Not", "Mon", "Tue", "Wed", "Thu",
         "Fri", "Sat", "Sun", "January", "February", "March", "April", "May", "June", "July", "August",
         "September", "October", "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday",
         "Friday", "Saturday", "Sunday", "Wave"}

_lock = threading.Lock()
_cache = (0.0, "")


def _enabled() -> bool:
    return (getattr(config, "STT_VOCABULARY", "auto") or "auto").strip().lower() not in ("off", "false", "0", "no")


def build_vocabulary(facts) -> list:
    """The words, most important first, without repeats (case-insensitive)."""
    words = []

    def add(word):
        word = (word or "").strip(" .,;:!?\"'")
        if len(word) >= 2 and word.lower() not in {w.lower() for w in words} and word not in _STOP:
            words.append(word)

    for word in (getattr(config, "STT_EXTRA_VOCABULARY", "") or "").split(","):
        add(word)
    for key, value in facts or ():
        value = _DATE_NOTE.sub("", str(value or "")).strip()
        if not value or len(value) > 60:
            continue
        if _NAME_KEYS.search(str(key or "")) and len(value.split()) <= 3:
            add(value if value[:1].isupper() or not value.isascii() else value.title())
        for word in _CAPITALISED.findall(value):
            add(word)
    for app in _APPS:
        add(app)
    return words[:MAX_WORDS]


def vocabulary_hint() -> str:
    """"Priya, Jaipur, WhatsApp." - or "" when off or there's nothing."""
    global _cache
    if not _enabled():
        return ""
    now = time.monotonic()
    with _lock:
        at, hint = _cache
        if hint is not None and now - at < CACHE_S and at:
            return hint
    try:
        from database.db_manager import db  # noqa: PLC0415  (lazy: keeps the STT import light)
        words = build_vocabulary(db.get_all_memory())
        hint = (", ".join(words) + ".") if words else ""
    except Exception as e:
        logger.debug(f"vocabulary hint unavailable: {e}")
        hint = ""
    with _lock:
        _cache = (now, hint)
    return hint


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = (0.0, "")
