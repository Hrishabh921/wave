"""
brain/grounding.py — Tier 47: time-sensitive questions answered from a fresh search.

"Petrol ka rate kya hai aaj?", "who is the current RBI governor?", "latest
iPhone price?" - the model's own knowledge is months old, and since Tier 42
the prompt only TELLS it to use get_live_information for such questions;
it doesn't always. SARA decides in code: a question with a recency cue that
no tool answers better gets one web search before the model is asked, and
the model answers from it. needs_grounding() is that check - conservative:
a question (not a command), with a recency cue ("latest", "current",
"abhi ka", "aaj ka rate", "who won", "price of" ...), not about the user
("my", "mera"), not one Wave's own tools answer (weather, news, the time or
date, the PC). fetch() runs the search with a hard time limit; nothing found
or too slow = the model answers as before (and may still search itself).
WEB_GROUNDING=false turns it off.
"""
import concurrent.futures
import re

from settings.config import config

TIMEOUT_S = 6.0      # ddgs takes 2-5 s on the owner's connection

_RECENCY = re.compile(
    r"\b(?:latest|newest|current(?:ly)?|right now|as of (?:today|now)|today'?s|this (?:week|month|year)|"
    r"recent(?:ly)?|nowadays|price of|prices? (?:of|for|in)|rate of|rates? (?:of|for|in)|exchange rate|"
    r"stock price|share price|how much (?:is|does|do|are)\b.*\b(?:cost|price|rate)|"
    r"who (?:is|are) the (?:current|new|present)|who won|who is winning|who'?s winning|live score|score of|"
    r"release date|launched|abhi ka|abhi ki|abhi ke|aaj ka|aaj ki|aaj ke|aajkal|taaza|"
    r"kitne ka (?:hai|mil)|kya rate|kya bhav|bhav kya|rate kya|kaun jeeta|kisne jeeta|kaun jeet|"
    r"(?:petrol|diesel|gold|silver|sona|sone|chandi|dollar|rupee|bitcoin|sensex|nifty) (?:ka|ki|ke|price|rate))\b"
    # who holds a post - it changes, the model's training doesn't
    r"|\bwho(?:'s| is| are)\s+(?:the\s+)?(?:current\s+|new\s+|present\s+)?(?:prime minister|pm|president|ceo|"
    r"chief minister|cm|governor|captain|coach|chairman|chief justice|speaker)\b"
    r"|\b(?:prime minister|pm|president|ceo|chief minister|cm|captain|coach)\s+kaun\s+hai\b",
    re.I)
_QUESTION = re.compile(
    r"^\W*(?:what|what's|whats|who|who's|when|where|which|how|is|are|does|do|did|tell me|kya|kaun|kab|kahan|"
    r"kitna|kitni|kitne|batao|bata)\b|\?\s*$|\b(?:kya hai|kitna hai|kitne ka hai|kaun hai|kya chal raha hai)\W*$",
    re.I)
_COMMAND = re.compile(r"^\W*(?:open|close|play|set|turn|start|stop|remind|send|call|write|type|search|google|"
                      r"kholo|chalao|lagao|band)\b", re.I)
# Answered better by Wave's own tools or memory.
_OWN = re.compile(r"\b(?:my|mine|mera|meri|mere|mujhe|weather|mausam|news|headlines?|khabar|samachar|"
                  r"time|samay|date|tarikh|battery|volume|brightness|screen|wifi|bluetooth)\b", re.I)
_NOTHING = ("i couldn't find", "sorry", "no results")
# Search engines find nothing for "petrol ka rate kya hai aaj Delhi mein?"
# but do for "petrol rate today delhi": Hindi helper words dropped, a few
# translated.
_HI_DROP = frozenset("ka ki ke ko se mein me ne hai hain tha thi kya kaun kab kahan kitna kitni kitne batao bata "
                     "abhi wala wali wale aur bhi please yaar bhai".split())
_HI_SWAP = {"aaj": "today", "aajkal": "nowadays", "bhav": "price", "daam": "price", "keemat": "price",
            "jeeta": "won", "jeeti": "won", "sona": "gold", "sone": "gold", "chandi": "silver"}


def search_query(text: str) -> str:
    """The question as a search engine wants it."""
    words = re.findall(r"[A-Za-z0-9.+#']+", text or "")
    if not any(w.lower() in _HI_DROP or w.lower() in _HI_SWAP for w in words):
        return (text or "").strip()
    kept = [_HI_SWAP.get(w.lower(), w) for w in words if w.lower() not in _HI_DROP]
    return " ".join(kept) if len(kept) >= 2 else (text or "").strip()


def _enabled() -> bool:
    return bool(getattr(config, "WEB_GROUNDING", True))


def needs_grounding(text: str) -> bool:
    t = (text or "").strip()
    if not _enabled() or len(t) < 8 or len(t) > 200 or _COMMAND.match(t) or _OWN.search(t):
        return False
    return bool(_RECENCY.search(t)) and bool(_QUESTION.search(t))


def fetch(query: str, search, timeout_s: float = TIMEOUT_S):
    """search(query) with a hard time limit; the text, or None."""
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="WaveGrounding")
    try:
        future = pool.submit(search, query)
        try:
            result = (future.result(timeout=timeout_s) or "").strip()
        except Exception:
            return None
    finally:
        pool.shutdown(wait=False)
    if not result or result.lower().startswith(_NOTHING):
        return None
    return result[:1500]
