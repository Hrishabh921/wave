"""
brain/episodic_recall.py — Tier 46: "what did I tell you last Monday?"

Remembered facts hold the lasting things (a city, a job, a birthday); what
the user did or said on a given day lives only in the chat history, and
the model sees just the last few messages of it. SARA keeps dated events
and answers "what did I tell you last Monday?", "kal maine kya kharida
tha?", "what did I tell you about the cricket match?". Wave already keeps
every message with its time (chat_history), so for a question about the
past this finds
  - the day or week it names - "last Monday", "kal" with a past verb
    (yesterday), "parso", "3 din pehle", "two weeks ago", "last week"
    (brain/memory_time.py reads the relative dates) - and the user's own
    messages from then;
  - or what it asks about - "about the cricket match", "exam ke baare
    mein" - and the user's messages that mention it;
and puts them in the prompt with their dates. Only for questions about the
past ("what did I ...", "maine ... kiya tha"), only when the chat history is
kept on disk, never a message holding something the user asked Wave to
forget. MEMORY_EPISODIC=false turns it off.
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional

from brain import memory_time
from settings.config import config

MAX_LINES = 8
MAX_CHARS = 180

_HI_ASK = r"(?:kya|kisko|kise|kisse|kiske|kahan|kab|kaun|kaunsa|kaunsi|kitna|kitne|kaisa|kaise)"
_PAST_QUESTION = re.compile(
    r"\bwhat (?:did|have|had) (?:i|we)\b|\bwhat happened\b|\bdid i (?:tell|say|mention|ask)\b"
    r"|\bwhat was i\b|\bwhere did i\b|\bwho did i\b|\bwhen did i\b|\bwhat i (?:said|told)\b"
    # Hinglish: "maine" with a question word - "aaj maine phone kharida" is a statement
    r"|\b(?:maine|humne|meine)\b.*\b" + _HI_ASK + r"\b|\b" + _HI_ASK + r"\b.*\b(?:maine|humne|meine)\b", re.I)
_WEEKS_AGO = re.compile(r"\b(\d|a|one|two|three|four|ek|do|teen|char)\s+(?:weeks?|hafte|hafton)\s+"
                        r"(?:ago|pehle|pahle)\b", re.I)
_LAST_WEEK = re.compile(r"\b(?:last week|pichhle hafte|pichle hafte|pichle week)\b", re.I)
_NUM = {"a": 1, "one": 1, "ek": 1, "two": 2, "do": 2, "three": 3, "teen": 3, "four": 4, "char": 4}
_TOPIC = (
    re.compile(r"\babout\s+(?:the\s+|my\s+|a\s+|an\s+|our\s+)?(?P<t>[a-z][a-z' ]{1,40}?)\s*(?:[?.!]|$)", re.I),
    re.compile(r"\b(?P<t>[a-z][a-z' ]{1,40}?)\s+ke\s+(?:baare|bare)\s+(?:mein|me|main)\b", re.I),
)
_STOP = set(
    "the a an my me i you your our about what did tell told say said mention mentioned kya ke baare bare mein "
    "me main maine mujhe humne tha thi the hai ka ki ko se ne kuch bataya kaha and or with to of on in at for "
    "last week weeks ago day days yesterday today tomorrow kal parso aaj pehle pahle hafte two one three four "
    "do teen char monday tuesday wednesday thursday friday saturday sunday".split())


def _enabled() -> bool:
    return bool(getattr(config, "MEMORY_EPISODIC", True))


def is_recall_question(text: str) -> bool:
    return bool(_PAST_QUESTION.search(text or ""))


def window(text: str, today: Optional[date] = None):
    """(the words, first day, last day) of the past time `text` names, or None."""
    today = today or date.today()
    text = text or ""
    m = _WEEKS_AGO.search(text)
    if m:
        word = m.group(1).lower()
        n = int(word) if word.isdigit() else _NUM.get(word, 1)
        mid = today - timedelta(days=7 * n)
        return m.group(0), mid - timedelta(days=3), mid + timedelta(days=3)
    m = _LAST_WEEK.search(text)
    if m:
        monday = today - timedelta(days=today.weekday() + 7)
        return m.group(0), monday, monday + timedelta(days=6)
    found = memory_time.resolve(text, today)
    if found:
        expr, d = found
        if d <= today:
            return expr, d, d
    return None


def topic(text: str):
    """(the thing asked about, its words to look for), or None."""
    for rx in _TOPIC:
        m = rx.search(text or "")
        if m:
            words = [w for w in re.findall(r"[a-z]+", m.group("t").lower()) if w not in _STOP and len(w) >= 3]
            if words:
                return " ".join(words), words
    return None


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}"


def _stamp(when: str) -> str:
    try:
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        return f"{dt.strftime('%a')} {dt.day} {dt.strftime('%b')} {dt.strftime('%H:%M')}"
    except (TypeError, ValueError):
        return str(when or "")


def recall(text: str, today: Optional[date] = None, store=None):
    """(what it covers, [lines]) for a question about the past, else None."""
    if not _enabled() or not is_recall_question(text):
        return None
    if store is None:
        from database.db_manager import db as store  # noqa: PLC0415
    span = window(text, today)
    about = topic(text)
    if span is None and about is None:
        return None
    rows, label = [], ""
    if span is not None:
        _words, first, last = span
        rows = store.get_user_messages_on(first, last, limit=60)
        label = f"ON {_day(first)}" if first == last else f"FROM {_day(first)} TO {_day(last)}"
        if about is not None:
            narrowed = [r for r in rows if any(w in str(r[1]).lower() for w in about[1])]
            if narrowed:
                rows, label = narrowed, f"{label} ABOUT \"{about[0]}\""
    elif about is not None:
        rows = store.search_user_messages(about[1], limit=MAX_LINES, exclude=text)
        label = f"ABOUT \"{about[0]}\""
    forgotten = [str(v).lower() for v in store.get_forgotten_values() if v and len(str(v)) >= 3]
    lines = []
    for when, msg in rows:
        msg = " ".join(str(msg).split())
        low = msg.lower()
        if not msg or low == (text or "").strip().lower() or is_recall_question(msg):
            continue
        if any(f in low for f in forgotten):
            continue
        if len(msg) > MAX_CHARS:
            msg = msg[:MAX_CHARS - 3].rstrip() + "..."
        lines.append(f"- {_stamp(when)}: {msg}")
    return label, lines[-MAX_LINES:] if span is not None else lines[:MAX_LINES]


def section(text: str, user_name: str, today: Optional[date] = None) -> str:
    """The prompt lines for a question about the past - or ""."""
    try:
        found = recall(text, today)
    except Exception:
        return ""
    if not found:
        return ""
    label, lines = found
    if not lines:
        return (f"\nFROM THE CHAT HISTORY {label}: {user_name} said nothing there that answers this - "
                f"say so rather than guess.\n")
    return (f"\nWHAT {user_name.upper()} TOLD YOU {label} (their own messages, from the chat history):\n"
            + "\n".join(lines) + "\n")
