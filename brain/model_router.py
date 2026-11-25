"""
brain/model_router.py — Tier 34: the right model for each request.

SARA picks a model per request (a learned router over cheap/fast and main
models). Wave sent every LLM turn to one model - openai/gpt-oss-120b on
Groq - and only moved to a backup after a "429 Too Many Requests". On
Groq's free tier every model has its OWN per-minute quota (8,000 tokens),
and most turns are short and everyday. So now:

  * short everyday turns go to a FAST model (LLM_FAST_MODEL; on Groq
    openai/gpt-oss-20b by default - same family, quicker, its own quota);
  * requests that need the big model go to LLM_MODEL: long ones,
    multi-step ones, code, writing (an email, an essay), "explain in
    detail / samjhao", comparisons, documents;
  * each keeps the other as its fallback (brain/groq_core.py), and a fast
    model that fails outright hands the turn to the main one.

It learns, like SARA's router does from outcomes: when the turn after a
fast-model answer is a correction ("galat hai", "that's wrong", "phir se")
or the same question asked again, that KIND of request - its bucket:
language x length x question-or-not - counts a miss. A bucket whose fast
answers miss too often goes to the main model from then on (and gets
another try on the fast one a few days later). The counts live in
<DATA_DIR>/model_router.json.

LLM_ROUTER: on (default) | shadow (decide and log, but always use the main
model - to see what it would do) | off.
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from difflib import SequenceMatcher

from settings.config import config

logger = logging.getLogger("ModelRouter")

FAST, MAIN = "fast", "main"
LONG_CHARS = 240            # a request longer than this needs the main model
SHORT_CHARS = 60
WINDOW = 20                 # outcomes remembered per bucket and tier
MIN_MISSES = 3              # a bucket moves to the main model after this many misses...
MISS_RATE = 0.34            # ...when they're at least this share of its fast answers
RETRY_FAST_AFTER_S = 3 * 24 * 3600   # then the fast model gets another try
JUDGE_WITHIN_S = 180.0      # the next turn only says something about this one within this time

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINGLISH = re.compile(r"\b(hai|hain|kya|kaise|kaisa|kyun|kyon|mujhe|mera|meri|mere|tum|tumhara|aap|aapka|karo|kar|"
                       r"karna|nahi|nahin|bata|batao|chahiye|kab|kahan|kaun|kitna|kitne|accha|acha|theek|yaar|"
                       r"bhai|haan|matlab|wala|wali|abhi|aaj|kal)\b", re.I)
_QUESTION = re.compile(r"\?\s*$|^\W*(what|who|whom|whose|which|when|where|why|how|is|are|do|does|did|can|could|"
                       r"should|will|would|kya|kaise|kyun|kyon|kab|kahan|kaha|kaun|kitna|kitne|kitni|kaunsa)\b", re.I)

# Requests that need the main model, with the reason logged for each.
_HEAVY = (
    ("code", re.compile(r"\b(code|coding|program(ming)?|python|javascript|typescript|java|c\+\+|html|css|function|"
                        r"script|regex|sql|debug|stack ?trace|exception|compile|algorithm)\b", re.I)),
    ("writing", re.compile(r"\b(write|draft|compose|rewrite|likh(o|na|do|kar|iye)?|essay|story|kahani|poem|kavita|"
                           r"shayari|letter|email|e-mail|application|resume|cv|speech|article|report|caption)\b", re.I)),
    ("detail", re.compile(r"\b(explain|in detail|detail (me|mein|se)|step[- ]by[- ]step|samjha(o|iye|na|do)|vistar|"
                          r"elaborate|in depth|compare|comparison|difference between|pros and cons|analy[sz]e|"
                          r"analysis|reason(ing)? why|fark|antar)\b", re.I)),
    ("multi-step", re.compile(r"\b(and then|after that|then also|uske baad|iske baad|aur phir|phir uske|"
                              r"first .{3,60} then)\b", re.I)),
    ("maths", re.compile(r"\b(solve|equation|integral|derivative|probability|proof|prove|word problem)\b", re.I)),
    ("plan", re.compile(r"\b(plan (a|my|for)|itinerary|schedule (a|my) (day|week)|roadmap|strategy)\b", re.I)),
)

# What a user says when the last answer missed.
_CORRECTION = re.compile(
    r"\b(wrong|incorrect|that'?s not (it|right|what)|not what i (asked|said|meant)|you didn'?t (get|understand)|"
    r"nonsense|galat|ghalat|bakwas|samjh?a nahi|samjh?i nahi|samjhe nahi|ye nahi (pucha|poocha|kaha)|"
    r"maine (ye|yeh) nahi|phir se (batao|bolo|samjhao)|dobara (batao|bolo|samjhao)|sahi (se )?(batao|jawab)|"
    r"try again|answer properly|गलत|बकवास|फिर से|दोबारा)\b", re.I)


@dataclass
class Route:
    tier: str            # FAST or MAIN
    reason: str          # why - for the log and the trace
    bucket: str          # the kind of request the learning counts against
    learned: bool = False


def language_of(text: str) -> str:
    if _DEVANAGARI.search(text or ""):
        return "hi"
    if len(_HINGLISH.findall(text or "")) >= 1:
        return "hinglish"
    return "en"


def bucket_of(text: str) -> str:
    text = (text or "").strip()
    size = "short" if len(text) <= SHORT_CHARS else "medium" if len(text) <= LONG_CHARS else "long"
    kind = "question" if _QUESTION.search(text) else "other"
    return f"{language_of(text)}:{size}:{kind}"


def classify(text: str, attachment=None) -> Route:
    """The heuristic choice alone (no learning)."""
    text = (text or "").strip()
    bucket = bucket_of(text)
    if attachment:
        return Route(MAIN, "attachment", bucket)
    if len(text) > LONG_CHARS:
        return Route(MAIN, "long request", bucket)
    for reason, pattern in _HEAVY:
        if pattern.search(text):
            return Route(MAIN, reason, bucket)
    return Route(FAST, "everyday", bucket)


def is_correction(text: str) -> bool:
    return bool(_CORRECTION.search(text or ""))


def is_repeat(previous: str, text: str) -> bool:
    a, b = (previous or "").strip().lower(), (text or "").strip().lower()
    if len(a) < 8 or len(b) < 8:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.8


def default_path() -> str:
    return os.path.join(config.DATA_DIR, "model_router.json")


def fast_model_for(provider_name: str, main_model: str):
    """The fast model for this provider - LLM_FAST_MODEL, or on Groq
    openai/gpt-oss-20b - or None ("off", or no different one). Also what
    background jobs (memory consolidation, reminder rewording, proactive
    lines) use, so they don't spend the main model's per-minute quota."""
    m = (getattr(config, "LLM_FAST_MODEL", "") or "").strip()
    if m.lower() in ("off", "none", "0", "false"):
        return None
    if not m and (provider_name or "") == "groq":
        m = "openai/gpt-oss-20b"
    return m if m and m != main_model else None


def background_model(cfg) -> str:
    """The model a background job asks: the fast one when there is one."""
    return fast_model_for(getattr(cfg, "name", ""), getattr(cfg, "model", "")) or getattr(cfg, "model", "")


class ModelRouter:
    """Heuristic choice + what was learned from the user's reactions."""

    def __init__(self, path: str = None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._stats = None          # bucket -> {"fast": [0/1...], "main": [...], "main_since": ts}
        self._pending = None        # (bucket, tier, text, time) of the last routed turn

    # -- configuration ---------------------------------------------------
    @staticmethod
    def mode() -> str:
        return (getattr(config, "LLM_ROUTER", "on") or "on").strip().lower()

    def path(self) -> str:
        return self._path or default_path()

    # -- persistence -----------------------------------------------------
    def _load(self):
        if self._stats is not None:
            return
        self._stats = {}
        try:
            with open(self.path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._stats = {k: v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Model router: couldn't read {self.path()} ({e}) - starting fresh.")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path()) or ".", exist_ok=True)
            tmp = self.path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._stats, f)
            os.replace(tmp, self.path())
        except Exception as e:
            logger.warning(f"Model router: couldn't save its counts ({e}).")

    # -- learning ----------------------------------------------------------
    def observe_user_turn(self, text: str) -> None:
        """Called with every user turn, before it's answered: what it says
        about the PREVIOUS routed answer (a miss or not)."""
        with self._lock:
            pending, self._pending = self._pending, None
            if pending is None:
                return
            bucket, tier, previous, at = pending
            if self._clock() - at > JUDGE_WITHIN_S:
                return              # too late to tell anything
            miss = is_correction(text) or is_repeat(previous, text)
            self._record(bucket, tier, miss)

    def _record(self, bucket: str, tier: str, miss: bool) -> None:
        self._load()
        entry = self._stats.setdefault(bucket, {})
        window = entry.setdefault(tier, [])
        window.append(1 if miss else 0)
        del window[:-WINDOW]
        if tier == FAST and self._fast_misses_too_often(entry) and not entry.get("main_since"):
            entry["main_since"] = self._clock()
            logger.info(f"Model router: '{bucket}' requests go to the main model from now on "
                        f"(the fast one missed {sum(entry[FAST])} of its last {len(entry[FAST])}).")
        self._save()

    @staticmethod
    def _fast_misses_too_often(entry: dict) -> bool:
        fast = entry.get(FAST) or []
        misses = sum(fast)
        if misses < MIN_MISSES:
            return False
        main = entry.get(MAIN) or []
        main_rate = (sum(main) / len(main)) if main else 0.0
        return misses / len(fast) >= max(MISS_RATE, main_rate + 0.15)

    def _learned_main(self, bucket: str) -> bool:
        """This bucket was moved to the main model (and it's not yet time to
        give the fast one another try)."""
        self._load()
        entry = self._stats.get(bucket) or {}
        since = entry.get("main_since")
        if not since:
            return False
        if self._clock() - since >= RETRY_FAST_AFTER_S:
            entry.pop("main_since", None)
            entry[FAST] = []            # a fresh start for the fast model
            self._save()
            logger.info(f"Model router: giving the fast model another try with '{bucket}' requests.")
            return False
        return True

    # -- the choice --------------------------------------------------------
    def choose(self, text: str, attachment=None) -> Route:
        route = classify(text, attachment)
        with self._lock:
            if route.tier == FAST and self._learned_main(route.bucket):
                route = Route(MAIN, "learned: fast answers missed here", route.bucket, learned=True)
            self._pending = (route.bucket, route.tier, text or "", self._clock())
        return route

    def stats(self) -> dict:
        with self._lock:
            self._load()
            return json.loads(json.dumps(self._stats))


model_router = ModelRouter()
