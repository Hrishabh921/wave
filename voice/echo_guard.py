"""
voice/echo_guard.py — Tier 48: Wave's own voice, heard back, isn't the user.

The echo canceller (voice/aec.py) takes what Wave plays out of the mic, and
the speaker check (voice/speaker_id.py) drops a voice that isn't the
owner's - but not while Wave is talking, so that "ruk jao" always gets
through. In that window, what the canceller misses (speaker bleed on a
loud laptop, the room's reverb, a reply's last words) is transcribed and
Wave would hear - and answer - its own sentence. SARA adds a check on the
transcript; this is Wave's: every sentence Wave says is noted with when it
was said, and a transcript whose audio began while one was playing (or
within REVERB_S after it ended) is compared with it - most of its words in
the sentence, a near-identical run of characters, or the sentence's last
few words - and dropped when it's clearly Wave's own. A low-confidence
transcript needs a little less. Speech that began after Wave stopped is
never checked: "open chrome" right after "Shall I open Chrome?" is the
user's answer. Wave speaking Hinglish that Whisper writes down in
Devanagari isn't recognised as an echo (the words don't line up) - the
canceller and the speaker check still apply. ECHO_GUARD=false turns it off.
"""
import difflib
import re
import threading
import time
from collections import deque

from settings.config import config

REVERB_S = 0.8          # audio that began this long after a sentence ended can still be its tail
KEEP_S = 60.0
_WORD = re.compile(r"[a-z0-9ऀ-ॿ']+")


def _enabled() -> bool:
    return bool(getattr(config, "ECHO_GUARD", True))


def words(text: str) -> list:
    return [w.strip("'") for w in _WORD.findall((text or "").lower()) if w.strip("'")]


def similar(heard: list, said: list, lenient: bool = False) -> bool:
    """Whether `heard` is (part of) `said` as a microphone would hear it back."""
    if not heard or not said:
        return False
    if len(heard) == 1:
        return len(heard[0]) >= 3 and heard[0] in said[-3:]       # a lone word from the sentence's end
    said_set = set(said)
    if sum(1 for w in heard if w in said_set) / len(heard) >= (0.7 if lenient else 0.8):
        return True
    k = min(len(heard), 4)
    if heard[-k:] == said[-k:]:
        return True
    joined, n = " ".join(heard), len(heard)
    best = max(difflib.SequenceMatcher(None, joined, " ".join(said[i:i + n])).ratio()
               for i in range(max(1, len(said) - n + 1)))
    return best >= (0.72 if lenient else 0.8)


class EchoGuard:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._said = deque(maxlen=24)          # [started, ended or None, words]

    def started(self, text: str):
        """Wave starts saying `text`; returns the token finished() takes."""
        said = words(text)
        if not said:
            return None
        entry = [self._clock(), None, said]
        with self._lock:
            now = entry[0]
            while self._said and self._said[0][1] is not None and now - self._said[0][1] > KEEP_S:
                self._said.popleft()
            self._said.append(entry)
        return entry

    def finished(self, token) -> None:
        if token is not None and token[1] is None:
            token[1] = self._clock()

    def is_echo(self, text: str, heard_from: float = None, heard_to: float = None, confidence: float = None) -> bool:
        """True when `text`, whose audio ran from heard_from to heard_to
        (time.monotonic(); unknown = just now), is one of Wave's own sentences."""
        if not _enabled():
            return False
        heard = words(text)
        if not heard:
            return False
        now = self._clock()
        heard_to = now if heard_to is None else heard_to
        heard_from = heard_to if heard_from is None else heard_from
        with self._lock:
            candidates = [said for start, end, said in self._said
                          if start <= heard_to and heard_from <= (now if end is None else end) + REVERB_S]
        lenient = confidence is not None and confidence < 0.5
        return any(similar(heard, said, lenient) for said in candidates)


echo_guard = EchoGuard()
