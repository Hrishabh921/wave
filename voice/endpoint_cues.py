"""
voice/endpoint_cues.py — Tier 36: did the speaker actually finish?

Wave ended an utterance after 450 ms of silence, whatever had been said. A
pause in the middle of a sentence - "mujhe kal subah ... yaad dilana ki
doctor ke paas jana hai" - so became two commands: the first half was
answered on its own and the rest was lost or misread. SARA's adaptive
endpointing looks at the words: a sentence that ends on "and", "aur",
"ki", "to", "because", "lekin", an "um", or trails off ("...", a comma)
isn't finished yet, so it waits a little longer for the rest.

Wave does the same one step later, on the transcript: voice/stt.py holds an
unfinished-sounding transcript for a moment (STT_CONTINUATION_WAIT_S) and
joins it with what comes next. Pure functions, English / Hinglish / Hindi.
"""
import re
import unicodedata

# Words a sentence doesn't end on - it's still going. (Not "in"/"on"/"me"/"tab": "turn
# the wifi on", "log in", "help me", "close this tab" are whole commands.)
_CONTINUATION = {
    # English
    "and", "or", "but", "so", "because", "cause", "cuz", "then", "with", "to", "for", "of", "the", "a", "an",
    "my", "your", "his", "her", "their", "our", "about", "if", "that", "which", "who", "when", "where",
    "like", "into", "from", "also", "plus", "is", "are", "was", "and then", "as well as",
    # fillers - nothing after them yet
    "um", "umm", "uh", "uhh", "er", "erm", "hmm", "mm",
    # Hinglish
    "aur", "ya", "lekin", "par", "magar", "toh", "phir", "ki", "ke", "ka", "ko", "se", "mein", "jo",
    "kyunki", "kyuki", "matlab", "yaani", "jaise", "wala", "wali", "wale", "mera", "meri", "mere", "tera",
    "teri", "tere", "uska", "uski", "uske", "iska", "iski", "iske", "ya phir", "aur phir", "uske baad",
    "isliye", "agar", "jab", "ek", "kuch", "bhi",
    # Hindi
    "और", "या", "लेकिन", "पर", "मगर", "तो", "फिर", "की", "के", "का", "को", "से", "में", "जो", "क्योंकि", "मतलब",
    "यानी", "जैसे", "मेरा", "मेरी", "मेरे", "अगर", "जब", "एक", "कुछ", "भी",
}
_TRAILING_OFF = re.compile(r"(\.\.\.|…|,|-|—|:)\s*$")
_END_PUNCT = re.compile(r"[\s.!;:\"'”’)\]]+$")


def _words(text: str) -> list:
    text = unicodedata.normalize("NFC", text or "").casefold()
    return re.findall(r"[\wऀ-ॿ']+", text)


def sounds_unfinished(text: str) -> bool:
    """True when `text` reads like a sentence that was cut off mid-way."""
    raw = (text or "").strip()
    if not raw:
        return False
    if raw.endswith("?"):
        return False                       # a question asked in full
    if _TRAILING_OFF.search(raw):
        return True
    words = _words(_END_PUNCT.sub("", raw))
    if len(words) < 2:
        return False                       # "and" on its own is noise, not half a command
    last_two = " ".join(words[-2:])
    return words[-1] in _CONTINUATION or last_two in _CONTINUATION


def join(first: str, second: str) -> str:
    """The two halves as one sentence: the first's trailing "..." / comma /
    full stop dropped, and the second's capital letter kept as Whisper wrote it."""
    head = re.sub(r"(\.\.\.|…|[,.\-—:])\s*$", "", (first or "").strip())
    return f"{head} {(second or '').strip()}".strip()
