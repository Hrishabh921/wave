"""
brain/response_cache.py — Tier 40: instant answers to questions already answered.

"What is photosynthesis?" asked again next week costs another model call
(and Groq's per-minute quota) for the same answer. SARA caches final answers
to stable, non-personal knowledge questions. This does the same for Wave, on
purpose conservatively - a wrong cached answer is worse than a slow one:

  * only a self-contained question about general knowledge is ever cached:
    nothing about the user (my, mujhe, mera ...), time or news (today, latest,
    abhi, price, weather ...), the PC (open, this file, screen ...), or the
    conversation so far (it, that, uska, ye ...);
  * only a plain model answer is stored - no tool was called, nothing failed;
  * the key is the normalised question + the model that answered + the
    persona version, so a different model or persona starts over;
  * entries expire after LLM_CACHE_TTL_DAYS; at most MAX_ENTRIES are kept.

Stored in the main SQLite database (table response_cache). Forgetting a
memory or clearing the chat doesn't involve these - they hold nothing
personal by construction. LLM_RESPONSE_CACHE=false turns it off.
"""
import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timedelta

from settings.config import config

logger = logging.getLogger("ResponseCache")

PERSONA_VERSION = "1"
MAX_ENTRIES = 300
MIN_WORDS = 3

_QUESTION = re.compile(r"\?\s*$|^\W*(what|who|why|how|when|where|which|explain|define|tell me about|meaning of|"
                       r"difference between|kya|kaun|kyun|kyon|kaise|kab|kahan|matlab|batao|samjhao)\b", re.I)
# Anything that makes the answer depend on the user, the moment, the PC or the conversation.
_NOT_STABLE = re.compile(
    r"\b(i|me|my|mine|myself|we|our|us|you|your|yours|mera|meri|mere|mujhe|mujhse|main|mai|hum|hamara|hamari|"
    r"apna|apni|apne|tum|tumhara|tumhari|aap|aapka|aapki|"
    r"today|tonight|tomorrow|yesterday|now|current|currently|latest|recent|recently|this week|this year|news|"
    r"score|price|prices|rate|stock|weather|temperature|forecast|time|date|day|year|aaj|abhi|kal|parso|"
    r"open|close|play|set|remind|call|send|search|find|show|download|delete|this|that|these|those|it|its|"
    r"he|she|him|her|they|them|his|their|here|there|ye|yeh|wo|woh|vo|voh|uska|uski|uske|iska|iski|iske|"
    r"inka|unka|isse|usse|screen|file|folder|clipboard|note|notes|reminder|calendar|email|message|pc|laptop|"
    r"computer|volume|brightness|wifi|bluetooth|battery|"
    # small talk and things that should come out different each time
    r"haal|hal|kaise ho|kaisi ho|how are|whats up|what's up|joke|jokes|chutkula|story|kahani|poem|kavita|"
    r"song|gaana|gana|suggest|recommend|random|fun fact|idea|ideas)\b", re.I)
_WORD = re.compile(r"[\wऀ-ॿ]+")


def normalize(question: str) -> str:
    text = unicodedata.normalize("NFC", question or "").casefold()
    return " ".join(_WORD.findall(text))


def is_cacheable_question(question: str) -> bool:
    text = (question or "").strip()
    if not text or len(text) > 200:
        return False
    words = _WORD.findall(text)
    if len(words) < MIN_WORDS:
        return False
    if not _QUESTION.search(text):
        return False
    return not _NOT_STABLE.search(text)


def _enabled() -> bool:
    return bool(getattr(config, "LLM_RESPONSE_CACHE", True))


def _key(question: str, model: str) -> str:
    raw = f"{normalize(question)}|{model or ''}|{PERSONA_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _conn():
    from database.db_manager import db  # noqa: PLC0415
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS response_cache (key TEXT PRIMARY KEY, question TEXT NOT NULL, "
                 "answer TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL, last_hit TEXT, hits INTEGER DEFAULT 0)")
    return conn


def get(question: str, model: str):
    """The cached answer, or None."""
    if not _enabled() or not is_cacheable_question(question):
        return None
    ttl = float(getattr(config, "LLM_CACHE_TTL_DAYS", 14) or 14)
    try:
        with _conn() as conn:
            row = conn.execute("SELECT answer, created_at FROM response_cache WHERE key = ?",
                               (_key(question, model),)).fetchone()
            if not row:
                return None
            created = datetime.fromisoformat(row[1])
            if datetime.now() - created > timedelta(days=ttl):
                conn.execute("DELETE FROM response_cache WHERE key = ?", (_key(question, model),))
                conn.commit()
                return None
            conn.execute("UPDATE response_cache SET hits = hits + 1, last_hit = ? WHERE key = ?",
                         (datetime.now().isoformat(), _key(question, model)))
            conn.commit()
            return row[0]
    except Exception as e:
        logger.debug(f"response cache read failed: {e}")
        return None


def put(question: str, answer: str, model: str) -> bool:
    """Stores a plain model answer to a cacheable question."""
    answer = (answer or "").strip()
    if not _enabled() or not answer or not is_cacheable_question(question):
        return False
    if len(answer) < 2 or len(answer) > 4000:
        return False
    try:
        with _conn() as conn:
            now = datetime.now().isoformat()
            conn.execute("INSERT INTO response_cache (key, question, answer, model, created_at, last_hit, hits) "
                         "VALUES (?, ?, ?, ?, ?, ?, 0) ON CONFLICT(key) DO UPDATE SET answer=excluded.answer, "
                         "created_at=excluded.created_at", (_key(question, model), question.strip()[:200], answer,
                                                            model, now, now))
            conn.execute("DELETE FROM response_cache WHERE key NOT IN (SELECT key FROM response_cache "
                         "ORDER BY COALESCE(last_hit, created_at) DESC LIMIT ?)", (MAX_ENTRIES,))
            conn.commit()
        return True
    except Exception as e:
        logger.debug(f"response cache write failed: {e}")
        return False


def clear() -> None:
    try:
        with _conn() as conn:
            conn.execute("DELETE FROM response_cache")
            conn.commit()
    except Exception as e:
        logger.debug(f"response cache clear failed: {e}")
