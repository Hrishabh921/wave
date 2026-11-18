"""
voice/transcript_filter.py — Tier 12: Whisper hygiene
=======================================================

Whisper is trained on subtitled video, so on silence, room tone or a
cough it likes to "hear" things: "Thank you.", "Thanks for watching!",
"Subtitles by ...", "♪", or a phrase looping ("I'm going to go. I'm going
to go. I'm going to go."). This module decides whether a transcript should
be trusted, and cleans the ones that should:

  * confidence from Whisper's own numbers - the average token log-prob
    and the no-speech probability of each segment - attached to every
    transcript (voice/stt.py's Transcript.confidence);
  * known hallucinations, rejected when the audio gives no reason to
    believe them (short, quiet, or low-confidence);
  * repetition loops collapsed ("open open open chrome" stays a user's
    stutter; the same 3+ word phrase three times is a loop);
  * a compression-ratio check (Whisper's own signal for degenerate
    repetitive output).

Pure functions: no model, no audio - testable with plain data.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

# Lower-cased, punctuation-stripped. English first, then Hindi/Hinglish.
KNOWN_HALLUCINATIONS = frozenset({
    "thank you", "thank you very much", "thanks", "thanks for watching", "thank you for watching",
    "thanks for watching and see you next time", "please subscribe", "subscribe", "like and subscribe",
    "please like and subscribe", "subtitles by the amara org community", "subtitles by",
    "transcribed by", "transcription by", "captions by", "bye", "bye bye", "goodbye", "you", "so",
    "okay", "uh", "um", "hmm", "mm", "oh", "the end", "music", "applause", "laughter", "silence",
    "blank audio", "inaudible", "foreign", "see you next time", "see you in the next video",
    "i'll see you in the next video", "thank you so much for watching",
    "धन्यवाद", "शुक्रिया", "नमस्ते", "सब्सक्राइब करें", "dhanyavaad", "shukriya",
})
_BRACKETED = re.compile(r"^\s*[\[\(\*♪♫#].*[\]\)\*♪♫#]?\s*$")
_WORD = re.compile(r"[\wऀ-ॿ']+", re.UNICODE)


def normalize(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


@dataclass
class SegmentInfo:
    text: str
    avg_logprob: float = 0.0
    no_speech_prob: float = 0.0
    compression_ratio: float = 1.0
    duration_s: float = 0.0


@dataclass
class Verdict:
    text: str               # cleaned text ("" = nothing usable)
    confidence: float       # 0..1
    reason: str = ""        # why it was rejected or changed, for the log


def segment_confidence(segments: Sequence[SegmentInfo]) -> float:
    """exp(mean avg_logprob) scaled down by the worst no-speech
    probability - roughly 'how sure Whisper was there were words, and which
    ones'. 1.0 for a transcript from an engine without these numbers."""
    segs = [s for s in segments if s is not None]
    if not segs:
        return 0.0
    weights = [max(0.1, s.duration_s or len(s.text)) for s in segs]
    lp = sum(w * s.avg_logprob for w, s in zip(weights, segs)) / sum(weights)
    nsp = max(s.no_speech_prob for s in segs)
    return max(0.0, min(1.0, math.exp(lp) * (1.0 - nsp)))


def collapse_repetitions(text: str, min_words: int = 3, max_repeats: int = 2) -> str:
    """Collapses a phrase of >= min_words words repeated back to back more
    than max_repeats times down to one copy. A short stutter ("open open
    chrome") is left alone."""
    words = (text or "").split()
    if len(words) < min_words * (max_repeats + 1):
        return text.strip() if text else ""
    out: List[str] = []
    i = 0
    while i < len(words):
        collapsed = False
        for n in range(min(12, (len(words) - i) // (max_repeats + 1)), min_words - 1, -1):
            chunk = [w.lower().strip(".,!?") for w in words[i:i + n]]
            reps = 1
            while i + (reps + 1) * n <= len(words) and \
                    [w.lower().strip(".,!?") for w in words[i + reps * n:i + (reps + 1) * n]] == chunk:
                reps += 1
            if reps > max_repeats:
                out.extend(words[i:i + n])
                i += reps * n
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return " ".join(out)


def judge(text: str, segments: Optional[Sequence[SegmentInfo]] = None, audio_s: float = 0.0,
          snr_db: Optional[float] = None, min_confidence: float = 0.35) -> Verdict:
    """The decision voice/stt.py applies to a Whisper transcript.

    Rejected (text ""):
      * empty / only punctuation or a bracketed tag ("[Music]", "(applause)");
      * a known hallucination, unless the audio clearly held speech
        (longer than 1.5 s AND at least 15 dB above the noise floor AND
        confident) - "thank you" said on purpose survives;
      * confidence below min_confidence;
      * a compression ratio above 2.4 (degenerate repetitive output).
    Cleaned: repetition loops collapsed."""
    segments = list(segments or [])
    conf = segment_confidence(segments) if segments else 1.0
    raw = (text or "").strip()
    norm = normalize(raw)
    if not norm or _BRACKETED.match(raw) and len(norm.split()) <= 3:
        return Verdict("", conf, "empty or a bracketed tag")
    if segments and max(s.compression_ratio for s in segments) > 2.4:
        return Verdict("", conf, "degenerate repetition (compression ratio)")
    if norm in KNOWN_HALLUCINATIONS:
        clear_speech = audio_s >= 1.5 and (snr_db is None or snr_db >= 15.0) and conf >= 0.6
        if not clear_speech:
            return Verdict("", conf, f"known Whisper hallucination '{norm}'")
    if segments and conf < min_confidence:
        return Verdict("", conf, f"low confidence {conf:.2f}")
    cleaned = collapse_repetitions(raw)
    return Verdict(cleaned, conf, "" if cleaned == raw else "repetition collapsed")
