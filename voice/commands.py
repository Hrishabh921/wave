"""
voice/commands.py — small, pure helpers for the handful of spoken commands
that are about the voice loop itself (wake up, go to sleep, stop, "new
question"), as opposed to commands for the brain - plus decide(), the one
function that turns "this is what was heard" into "this is what the mic
worker should do".

Tier 10: ui/workers.py's ContinuousMicWorker used to decide "go to sleep"
with a substring test:

    if any(w in text_lower for w in ["mute", "sleep", "standby"]):

so "unmute the volume", "put the PC to sleep", "I can't sleep, play some
music" and "volume mute karo" all sent Wave to sleep instead of reaching
the brain - and every English phrasing of the `toggle_mute` fast-path rule
was unreachable by voice. is_sleep_command() below only fires when the
WHOLE utterance (after normalisation and an optional "wave"/wake-phrase
prefix) is one of a short list of phrases that can only mean "Wave, stop
listening". Deliberate behaviour change: a bare "mute" is no longer a
sleep command - it reaches the brain, where the intent router maps it to
`toggle_mute` (system audio), the meaning it has in every other assistant.

Tier 11: everything the mic worker decides now goes through decide():

  * Wake + command in one breath. "Hey Wave, Chrome kholo" used to only
    wake Wave and throw the command away; now the wake phrase is stripped
    and the rest is dispatched straight away, without the "Systems
    active." announcement in between.
  * A barge-in gate. Every recognised utterance used to stop Wave's speech
    - in either mode, from anyone in the room, a TV included. Now speech
    only interrupts Wave when it is a stop command, a "new question: ...",
    or addressed to Wave (starts with a wake phrase / "Wave,"). While
    asleep, nothing but the wake phrase interrupts.
  * Voice stop commands - "stop", "ruk jao", "रुको", "never mind", "bas
    karo", ... - cancel the turn in flight, not just the audio. "New
    question: what's the time" interrupts and asks the new thing at once
    (the same interrupt shapes SARA's own TTFA protocol, bench/results/
    voice_baseline.md, lists).

No Qt, no audio, no I/O: plain string logic, testable without any of the
voice stack installed.
"""
import re
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

# ---------------------------------------------------------------------------
# Phrase tables. All matched against the WHOLE normalised utterance (or a
# whole sequence of these phrases) - never as substrings.
# ---------------------------------------------------------------------------

# "Wave, go to sleep".
SLEEP_PHRASES = frozenset({
    # English
    "sleep", "go to sleep", "sleep mode", "sleep now", "standby", "go to standby",
    "standby mode", "stop listening", "mute yourself", "mute the mic", "mute mic",
    "mute your mic", "mic off", "turn off the mic", "good night",
    # Hinglish (romanised)
    "so jao", "so ja", "ab so jao", "sleep mode on karo", "sleep mode on kar do",
    "mic band karo", "mic band kar do", "mic mute karo", "mic mute kar do",
    "sunna band karo", "sunna band kar do",
    # Hindi (Devanagari)
    "सो जाओ", "सो जा", "अब सो जाओ", "स्लीप मोड", "माइक बंद करो", "सुनना बंद करो",
})

# "Stop what you're doing / saying". Deliberately NOT "pause" (that's the
# media_play_pause fast-path intent) and NOT "band karo" on its own (in
# Hinglish that usually means "close it" - an app or a window).
STOP_PHRASES = frozenset({
    # English
    "stop", "stop it", "stop that", "stop talking", "stop speaking", "be quiet", "quiet",
    "shut up", "enough", "that's enough", "thats enough", "that is enough", "never mind",
    "nevermind", "cancel", "cancel that", "forget it", "no thanks", "no thank you",
    "not now", "wait", "hold on", "okay stop", "ok stop",
    # Hinglish (romanised)
    "ruk", "ruko", "ruk jao", "ruk ja", "rukiye", "bas", "bas karo", "bas kar", "bas kar do",
    "chup", "chup ho jao", "chup raho", "chup karo", "rehne do", "rahne do", "nahi chahiye",
    "nahin chahiye", "mat bolo", "chhodo", "chodo", "jane do", "jaane do",
    # Hindi (Devanagari)
    "रुको", "रुक जाओ", "रुक जा", "बस", "बस करो", "चुप", "चुप हो जाओ", "चुप रहो",
    "रहने दो", "नहीं चाहिए", "छोड़ो", "जाने दो",
})

# Words a speaker may put in front of a command without changing its
# meaning ("please stop listening", "okay so jao", "acha ruko").
_FILLER_WORDS = ("please", "ok", "okay", "acha", "achha", "accha", "arre", "arey", "hmm", "um", "uh",
                 "plz", "pls", "zara", "ek baar", "कृपया", "अरे")

# Lead-ins for "interrupt and ask this instead".
_NEW_QUESTION_RE = re.compile(
    r"^\W*(?:(?:okay|ok|wait|stop|acha|achha|ruko|ruk jao)\W+)?"
    r"(?:new question|another question|different question|naya sawal|naya sawaal|"
    r"naya question|dusra sawal|doosra sawal|dusra question|नया सवाल|दूसरा सवाल)"
    r"\s*[:,\-.!?]*\s*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)

_PUNCT_RE = re.compile(r"[^\w\sऀ-ॿ]+")   # keep word chars, whitespace, the Devanagari block
_SPACE_RE = re.compile(r"\s+")


def normalize_utterance(text: str) -> str:
    """Lower-cases, turns punctuation into spaces and collapses whitespace.
    Devanagari is kept intact - its vowel signs aren't `\\w`, which is why
    the whole U+0900-U+097F block is kept explicitly (the danda '।' in it is
    turned into a space first)."""
    text = (text or "").lower()
    text = text.replace("।", " ").replace("॥", " ")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _sorted_prefixes(prefixes: Iterable[str]) -> Tuple[str, ...]:
    """Normalised, de-duplicated, longest first (so "hey wave" is tried
    before "wave")."""
    return tuple(sorted({normalize_utterance(p) for p in prefixes if p and normalize_utterance(p)},
                        key=len, reverse=True))


def _strip_prefixes(text: str, prefixes: Iterable[str]) -> str:
    """Removes any number of leading filler/wake prefixes from an already
    normalised `text`. Each prefix must be followed by a word boundary, so
    "wavelength" is never mistaken for "wave"."""
    prefixes = tuple(prefixes)
    changed = True
    while changed and text:
        changed = False
        for prefix in prefixes:
            if text == prefix:
                return ""
            if text.startswith(prefix + " "):
                text = text[len(prefix) + 1:].lstrip()
                changed = True
                break
    return text


def _is_sequence_of(norm: str, phrases: frozenset, max_parts: int = 3) -> bool:
    """True if `norm` is one phrase from `phrases`, or 2-`max_parts` of them
    back to back ("ruk jao nahi chahiye", "stop never mind")."""
    words = norm.split()
    n = len(words)
    if not n:
        return False
    # best[i] = fewest phrases that exactly cover words[:i]
    best = [0] + [None] * n
    for i in range(1, n + 1):
        for j in range(i):
            if best[j] is not None and " ".join(words[j:i]) in phrases:
                cand = best[j] + 1
                if best[i] is None or cand < best[i]:
                    best[i] = cand
    return best[n] is not None and best[n] <= max_parts


def _core(text: str, wake_phrases: Iterable[str]) -> str:
    """The normalised utterance with any wake phrase / "wave" / filler
    prefixes removed."""
    prefixes = _sorted_prefixes(tuple(wake_spellings(wake_phrases)) + ("hey wave", "hello wave", "okay wave", "ok wave", "wave")
                                + _FILLER_WORDS)
    return _strip_prefixes(normalize_utterance(text), prefixes)


def is_sleep_command(text: str, extra_prefixes: Iterable[str] = ()) -> bool:
    """True only if the whole utterance is a request for Wave itself to go
    to sleep. `extra_prefixes` lets the caller pass the user's configured
    wake phrases so "hey wave so jao" / "<custom wake word> sleep" work too."""
    if not normalize_utterance(text):
        return False
    return _core(text, extra_prefixes) in SLEEP_PHRASES


def is_stop_command(text: str, extra_prefixes: Iterable[str] = ()) -> bool:
    """True if the whole utterance only says "stop" in one of its forms -
    possibly two or three of them together ("ruk jao, nahi chahiye") and
    possibly addressed ("Wave, stop")."""
    core = _core(text, extra_prefixes)
    return bool(core) and _is_sequence_of(core, STOP_PHRASES)


def split_new_question(text: str) -> Optional[str]:
    """For "new question: what's the time" / "naya sawal, mausam kaisa hai"
    returns the question part ("what's the time"); None if the utterance
    isn't a "new question" interrupt. An addressed form ("Wave, new
    question: ...") works too. Returns "" if the lead-in had nothing after
    it."""
    stripped = strip_wake_phrase(text, ())[1] if text else ""
    m = _NEW_QUESTION_RE.match(stripped or "")
    if not m:
        return None
    return m.group("rest").strip()


def _wake_regex(phrase: str) -> "re.Pattern":
    words = [re.escape(w) for w in normalize_utterance(phrase).split()]
    if not words:
        return None
    filler = r"(?:(?:ok|okay|hey|hi|hello|acha|achha|arre|um|uh)[\s,.!?-]+)*"
    body = r"[\s,.!?-]+".join(words)
    return re.compile(r"^[\s,.!?-]*" + filler + body + r"(?![\wऀ-ॿ])[\s,.!?:;。、，！？-]*", re.IGNORECASE)


# Tier 30: other ways Whisper writes a few short Hindi/Hinglish wake words
# (seen in tests with Wave's own model: "Do do", "Do-do", "Sua", Devanagari,
# Urdu script, Japanese kana when it mistook the language). A configured wake
# word also matches these. Plain English words it sometimes produced instead
# ("So", "Sour") are deliberately left out - they'd wake Wave by accident.
WAKE_SPELLINGS = {
    "baby": ("bebi", "beby", "बेबी", "बेबि", "بے بی", "بیبی"),
    "babu": ("baboo", "babbu", "babuu", "बाबू", "बाबु", "بابو", "бабу", "бабu"),
    "dodo": ("do do", "doh doh", "dodoo", "dodu", "डोडो", "डो डो", "ڈوڈو", "ドードー", "どーどー"),
    "suar": ("suwar", "sooar", "suvar", "suaar", "sua", "सूअर", "सुअर", "सूवर", "سوار", "スワー"),
}


def wake_spellings(phrases: Iterable[str]) -> list:
    """`phrases` plus the known alternative spellings of any of them."""
    out = []
    for p in phrases or ():
        if not p:
            continue
        out.append(p)
        out.extend(WAKE_SPELLINGS.get(normalize_utterance(p), ()))
    return out


def strip_wake_phrase(text: str, wake_phrases: Iterable[str],
                      allow_bare_name: bool = True) -> Tuple[bool, str]:
    """(addressed, remainder). `addressed` is True if `text` starts with one
    of `wake_phrases` (or, with allow_bare_name, with "Wave" followed by a
    word boundary - "Wave, open Chrome"); `remainder` is the ORIGINAL text
    after it, so its casing/punctuation reach the brain unchanged. If not
    addressed, remainder is `text` stripped. Tier 30: each wake phrase also
    matches its WAKE_SPELLINGS."""
    text = text or ""
    phrases = sorted({p for p in wake_spellings(wake_phrases) if p and normalize_utterance(p)},
                     key=lambda p: len(normalize_utterance(p)), reverse=True)
    if allow_bare_name:
        phrases += ["hey wave", "hello wave", "okay wave", "ok wave", "wave"]
    for phrase in phrases:
        rx = _wake_regex(phrase)
        if rx is None:
            continue
        m = rx.match(text)
        if m:
            return True, text[m.end():].strip()
    return False, text.strip()


# Tier 11: what a recognizer typically writes when someone said "hey wave"
# (including the w/v merger common in Indian English). Used ONLY after the
# acoustic wake-word model already fired (voice/wakeword.py): to confirm a
# weak detection, and to strip the heard wake phrase off the command.
_HEARD_WAKE_RE = re.compile(
    r"^[\s,.!?-]*(?:(?:ok|okay|hey|hi|hay|he|a|eh)[\s,.!?-]+)?"
    r"(?:wave|waves|waved|way|ways|wait|weight|wade|weigh|whey|wake|weave|we've|vave|vev|wev|wav)"
    r"(?![\wऀ-ॿ'])[\s,.!?:;-]*",
    re.IGNORECASE)
_HEARD_WAKE_DEVANAGARI_RE = re.compile(r"^[\s,.!?।-]*(?:हे[\s,.!?।-]+)?(?:वेव|वेब|वेइव)[\s,.!?।:;-]*")


def looks_like_wake(transcript: str) -> bool:
    """Whether a transcript plausibly STARTS with "hey wave" as a
    recognizer might have written it ("Hey way, open Chrome", "a wave",
    "हे वेव")."""
    text = (transcript or "").strip()
    return bool(text) and bool(_HEARD_WAKE_RE.match(text) or _HEARD_WAKE_DEVANAGARI_RE.match(text))


def strip_heard_wake(text: str) -> str:
    """`text` minus a leading heard-wake phrase (see looks_like_wake), or
    `text` unchanged if it doesn't start with one."""
    text = text or ""
    for rx in (_HEARD_WAKE_RE, _HEARD_WAKE_DEVANAGARI_RE):
        m = rx.match(text)
        if m:
            return text[m.end():].strip()
    return text.strip()


def is_filler_only(text: str) -> bool:
    """True for an empty remainder or one made only of filler words ("hey
    wave, umm" -> after stripping, "umm")."""
    norm = normalize_utterance(text)
    if not norm:
        return True
    return _strip_prefixes(norm, _sorted_prefixes(_FILLER_WORDS)) == ""


# ---------------------------------------------------------------------------
# The decision the mic worker acts on.
# ---------------------------------------------------------------------------

IGNORE = "ignore"                    # drop it: not for Wave, or nothing to do
WAKE = "wake"                        # go AWAKE (announce it)
WAKE_AND_COMMAND = "wake_and_command"  # go AWAKE quietly and dispatch `text`
COMMAND = "command"                  # dispatch `text`
SLEEP = "sleep"                      # go SLEEPING
STOP = "stop"                        # stop speaking + cancel the turn in flight
STOP_AND_COMMAND = "stop_and_command"  # stop, then dispatch `text`


@dataclass(frozen=True)
class MicDecision:
    action: str
    text: str = ""
    interrupt: bool = False   # stop Wave's current speech/turn before acting

    @property
    def dispatches(self) -> bool:
        return self.action in (COMMAND, WAKE_AND_COMMAND, STOP_AND_COMMAND) and bool(self.text)


def decide(text: str, mode: str, wake_phrases: Iterable[str], tts_active: bool,
           wake_detected: bool = False, loud_barge_in: bool = False) -> MicDecision:
    """What the mic worker should do with one recognised utterance.

    `mode` is "SLEEPING" or "AWAKE". `tts_active` is whether Wave is
    speaking right now. `wake_detected` is True when an acoustic wake-word
    model (voice/wakeword.py) already fired for this audio - then the
    transcript doesn't have to contain the wake phrase for Wave to wake.

    Barge-in policy (Tier 11): while Wave is speaking, only a stop command,
    a "new question: ...", or something addressed to Wave interrupts it.
    Unaddressed speech during Wave's own reply - someone else in the room,
    a TV, a stray echo the AEC missed - is dropped instead of cutting Wave
    off. Once Wave has finished speaking, any utterance in AWAKE mode is a
    command, exactly as before.

    Tier 12: `loud_barge_in` - the mic worker sets it when the speech stood
    well above the room's noise floor (BARGE_IN_SNR_DB) while real echo
    cancellation was running. Then an unaddressed utterance of two words or
    more ("what about tomorrow") also interrupts: you're clearly talking to
    the machine, not someone across the room. Fillers and single words
    still don't."""
    wake_phrases = tuple(wake_phrases or ())
    if not normalize_utterance(text) and not wake_detected:
        return MicDecision(IGNORE)

    addressed, remainder = strip_wake_phrase(text or "", wake_phrases, allow_bare_name=(mode == "AWAKE"))
    if wake_detected:
        # The acoustic model heard the wake word; the recognizer may have
        # written it as "hey way" / "a wave" - don't send that to the brain.
        if not addressed:
            remainder = strip_heard_wake(remainder)
        addressed = True

    if mode == "SLEEPING":
        if not addressed:
            return MicDecision(IGNORE)            # never interrupt or act while asleep
        if is_filler_only(remainder):
            return MicDecision(WAKE, interrupt=tts_active)
        if is_stop_command(remainder, wake_phrases):
            return MicDecision(STOP, interrupt=True)  # e.g. silence a reminder being read out
        if is_sleep_command(remainder, wake_phrases):
            return MicDecision(IGNORE)            # already asleep
        question = split_new_question(remainder)
        if question is not None:
            remainder = question
            if is_filler_only(remainder):
                return MicDecision(WAKE, interrupt=True)
        return MicDecision(WAKE_AND_COMMAND, text=remainder, interrupt=True)

    # AWAKE
    if is_stop_command(text, wake_phrases):
        return MicDecision(STOP, interrupt=True)
    question = split_new_question(text)
    if question is not None:
        if is_filler_only(question):
            return MicDecision(STOP, interrupt=True)
        return MicDecision(STOP_AND_COMMAND, text=question, interrupt=True)
    if is_sleep_command(text, wake_phrases):
        return MicDecision(SLEEP, interrupt=True)
    if tts_active and not addressed:
        if loud_barge_in and len(normalize_utterance(remainder).split()) >= 2 and not is_filler_only(remainder):
            return MicDecision(COMMAND, text=remainder, interrupt=True)   # Tier 12: loud and clear
        return MicDecision(IGNORE)                # the barge-in gate
    if is_filler_only(remainder):
        return MicDecision(IGNORE, interrupt=addressed and tts_active)
    return MicDecision(COMMAND, text=remainder, interrupt=tts_active)
