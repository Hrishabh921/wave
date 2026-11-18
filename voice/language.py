"""
voice/language.py — Tier 13: English, Hindi or Hinglish?
=========================================================

Wave's users switch between English, Hindi in Devanagari and Hinglish
(Hindi written in Latin letters, usually mixed with English) from one
sentence to the next - "volume badhao", "आज मौसम कैसा है", "what's the
weather". Until Tier 13 the recognizer was told one fixed language from
Settings and every reply was spoken in one voice. This module is the cheap,
model-free part of fixing that:

    classify(text)        -> "en" | "hi" | "hinglish"
    devanagari_ratio(text), hinglish_ratio(text)
    LanguageTracker       -> the sticky "language we're talking in", used by
                             voice/stt.py (what to tell Whisper / Google) and
                             voice/tts.py (which voice answers)

How Hinglish is recognised: a word list. Romanized Hindi has a small,
very frequent core ("hai", "kya", "nahi", "karo", "mujhe", "kal",
"chalao"...) that ordinary English text practically never contains. Words
that ARE also English ("main", "to", "do", "me", "hum", "band", "par") count
for less. The ratio of marker words to all words decides.

Stickiness: a short, ambiguous utterance ("ok", "haan", "stop", a number)
doesn't flip the language - the tracker keeps the last confident one, the
way a person keeps answering in the language you were speaking.
"""
from __future__ import annotations

import re
import threading
from collections import deque
from typing import Optional

EN, HI, HINGLISH = "en", "hi", "hinglish"
LANGUAGES = (EN, HI, HINGLISH)

_DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?|[ऀ-ॿ]+")

# Romanized-Hindi words ordinary English text practically never contains.
STRONG_MARKERS = frozenset("""
hai hain hoon hun hu tha thi thhi hoga hogi honge hua hui
kya kyaa kyu kyun kyon kaise kaisa kaisi kahan kahaan kab kaun kon kitna kitne kitni kidhar
nahi nahin nahee nai haan haa hanji ji accha acha achha achchha theek thik thek
bhai bhaiya yaar yar matlab abhi abi bahut bohot bahot thoda thodi thode zyada jyada jada
wala wali wale waala waali raha rahi rahe gaya gayi gaye gya chahiye chaahiye sakta sakti sakte
karo karna karne karke kardo kar kariye kijiye kijie karein karen karunga karungi kiya kiye
batao bataiye bata bolo boliye suno suniye dekho dekhiye chalo chalao chala chalaao chaliye
kholo khol kholiye badhao badha badhaiye ghatao ghata lagao laga hatao hata bhejo bhej likho likh
padho padh jao jaa jaana jana aao aa aana dena de dedo dijiye lena le lelo rakho rakh
mujhe mujhko mujh tujhe tujhko tumhe tumhein tumko aapko aapke aapka aapki aap tum tera teri tere
mera meri mere apna apni apne uska uski uske unka unki unke iska iski iske inka yeh woh wo vo
yahan yahaan wahan wahaan idhar udhar upar neeche niche andar bahar
aaj kal parso subah shaam sham raat dopahar savere abhi jab
phir fir lekin magar aur bhi toh sirf bas kuch kuchh sab sabhi koi kisi kisko
kaam ghar paani pani khana gaana gana awaaz awaz aawaz samay waqt din mahina saal
ek chaar paanch panch chhe saat aath nau das sau hazaar hazar lakh crore karod
ka ki ke ko se mein mai hamesha jaldi dheere dhire zara jara
""".split())

# Hindi words that are ALSO common English words - they count for less.
WEAK_MARKERS = frozenset("""
main me to do is us par hum ho band kam na mat ye ya the he hi tab teen char hue
""".split())

_WEAK_WEIGHT = 0.4


def devanagari_ratio(text: str) -> float:
    """Share of letters that are Devanagari (0..1)."""
    dev = len(_DEVANAGARI_RE.findall(text or ""))
    lat = len(_LATIN_RE.findall(text or ""))
    return dev / (dev + lat) if dev + lat else 0.0


def has_arabic_script(text: str) -> bool:
    """Whisper writes Hindi speech in Urdu script when it guesses "ur"."""
    return bool(_ARABIC_RE.search(text or ""))


def _latin_words(text: str):
    return [w for w in _WORD_RE.findall((text or "").lower()) if _LATIN_RE.match(w)]


def hinglish_ratio(text: str) -> float:
    """Weighted share of Latin-script words that are romanized-Hindi markers."""
    words = _latin_words(text)
    if not words:
        return 0.0
    strong = sum(1 for w in words if w in STRONG_MARKERS)
    weak = sum(1 for w in words if w in WEAK_MARKERS and w not in STRONG_MARKERS)
    return (strong + _WEAK_WEIGHT * weak) / len(words)


def _strong_count(text: str) -> int:
    return sum(1 for w in _latin_words(text) if w in STRONG_MARKERS)


def classify(text: str) -> str:
    """"hi" (mostly Devanagari), "hinglish" (romanized Hindi, often mixed
    with English) or "en". Empty text is "en"."""
    if devanagari_ratio(text) >= 0.3:
        return HI
    ratio = hinglish_ratio(text)
    strong = _strong_count(text)
    if ratio >= 0.25 and strong >= 1:
        return HINGLISH
    if strong >= 2 and ratio >= 0.15:
        return HINGLISH
    return EN


# Said by everyone, whatever language they're speaking - never decisive alone.
_FILLER_MARKERS = frozenset("haan haa hanji ji acha accha achha achchha theek thik thek yaar yar bas bhai toh aur".split())


def is_decisive(text: str) -> bool:
    """Whether `text` is enough to switch the conversation's language: any
    Devanagari; two or more strong Hinglish markers; a short command with
    one ("chrome kholo"); or three or more words without a strong marker
    (plain English). "ok", "haan", "stop", "5" aren't."""
    if devanagari_ratio(text) > 0:
        return True
    words = _latin_words(text)
    strong = [w for w in words if w in STRONG_MARKERS]
    if len(strong) >= 2:
        return True
    if len(strong) == 1 and 2 <= len(words) <= 4 and strong[0] not in _FILLER_MARKERS:
        return True
    return len(words) >= 3 and not strong


class LanguageTracker:
    """The language the conversation is in right now - sticky across short,
    ambiguous utterances. Thread-safe (the mic thread writes, the TTS and UI
    threads read)."""

    def __init__(self, default: str = EN, history: int = 6):
        self._lock = threading.Lock()
        self._default = default if default in LANGUAGES else EN
        self._current = self._default
        self._history = deque(maxlen=history)

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    def observe(self, text: str, detected: Optional[str] = None, probability: Optional[float] = None) -> str:
        """Records one utterance (what the user said, or the recognizer's own
        language guess `detected` with its `probability`). Returns the
        language now in effect."""
        lang = classify(text)
        if lang == EN and detected == HI and (probability or 0.0) >= 0.7 and devanagari_ratio(text) == 0.0:
            # Whisper heard Hindi but wrote Latin letters: that's Hinglish.
            lang = HINGLISH
        confident_guess = (detected in (EN, HI) and (probability or 0.0) >= 0.9
                           and len(_latin_words(text)) + len(_DEVANAGARI_RE.findall(text or "")) // 3 >= 3)
        with self._lock:
            if is_decisive(text) or confident_guess:
                self._current = lang
            self._history.append(lang)
            return self._current

    def reset(self):
        with self._lock:
            self._current = self._default
            self._history.clear()

    # ---- what the recognizers should be told ---------------------------
    def whisper_language(self) -> Optional[str]:
        """The language code to decode with, or None to let Whisper detect it
        (before anything decisive has been heard). Hinglish decodes as "en"
        so it stays in Latin letters - the intent router reads both scripts."""
        with self._lock:
            if not self._history:
                return None
            return HI if self._current == HI else EN

    def google_language(self, configured: str = "en-IN") -> str:
        """Google's recognizer takes one BCP-47 tag per request."""
        with self._lock:
            if self._current == HI:
                return "hi-IN"
            if (configured or "").lower().startswith("hi"):
                return "en-IN"           # configured Hindi, but the user is speaking English/Hinglish
            return configured or "en-IN"

    def initial_prompt(self) -> Optional[str]:
        """A short Whisper prompt in the current style - it nudges spelling
        (romanized Hinglish stays romanized) and vocabulary ("Wave")."""
        with self._lock:
            cur = self._current
        if cur == HI:
            return "हाँ, ठीक है। वेव, क्रोम खोलो। आज मौसम कैसा है?"
        if cur == HINGLISH:
            return "Haan, theek hai. Wave, Chrome kholo. Volume badhao. Kal subah yaad dilana."
        return None


tracker = LanguageTracker()
