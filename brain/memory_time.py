"""
brain/memory_time.py — Tier 35: dates in remembered facts that stay true.

A fact saved as "doctor appointment: kal" is right for one day: a week later
Wave would still read "kal" as tomorrow. SARA's Memory 2.0 parses time
expressions when it stores a memory; this does the same for Wave: relative
dates in a fact (English, Hinglish, Hindi) get the absolute date they meant
at the time they were said, in brackets (said on Wed 25 Nov 2026):

    "doctor appointment kal"   -> "doctor appointment kal (Thu 26 Nov 2026)"
    "exam next monday"         -> "exam next monday (Mon 30 Nov 2026)"
    "kal Jaipur gaya tha"      -> "kal (Tue 24 Nov 2026) Jaipur gaya tha"     (past: yesterday)
    "3 din baad flight hai"    -> "3 din baad (Sat 28 Nov 2026) flight hai"

Hindi's "kal"/"parso" mean tomorrow OR yesterday (the day after tomorrow OR
the one before) - the verb decides: a past form (tha, thi, gaya, kiya, hua,
था, गया ...) makes it the past. "aaj kal" ("these days") isn't a date. Only
RELATIVE dates are touched: "12 march" already says when (and a birthday
recurs). Pure functions, no network; the first expression found is the one
annotated, and a value that already carries a date in brackets is left alone.
"""
import re
from datetime import date, timedelta

_WEEKDAYS = {
    "monday": 0, "mon": 0, "somvar": 0, "somwar": 0, "सोमवार": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "mangalvar": 1, "mangalwar": 1, "मंगलवार": 1,
    "wednesday": 2, "wed": 2, "budhvar": 2, "budhwar": 2, "बुधवार": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "guruvar": 3, "guruwar": 3, "brihaspativar": 3, "गुरुवार": 3,
    "friday": 4, "fri": 4, "shukravar": 4, "shukrawar": 4, "शुक्रवार": 4,
    "saturday": 5, "sat": 5, "shanivar": 5, "shaniwar": 5, "शनिवार": 5,
    "sunday": 6, "sun": 6, "ravivar": 6, "raviwar": 6, "itvaar": 6, "itwar": 6, "रविवार": 6,
}
# ("the" isn't here: it's Hindi "थे" in Roman script, but far more often English)
_PAST = re.compile(r"\b(tha|thi|thaa|gaya|gayi|gaye|kiya|kiye|hua|hui|huye|aaya|aayi|aaye|mila|mili|"
                   r"was|were|went|did|had|happened|ago)\b|(था|थी|थे|गया|गई|गए|किया|हुआ|हुई|आया|आई|मिला)", re.I)
_ALREADY = re.compile(r"\((?:mon|tue|wed|thu|fri|sat|sun)\w* \d{1,2} \w+ \d{4}\)", re.I)
# "aaj kal" / "आजकल" means "these days", not today or tomorrow.
_NOWADAYS = re.compile(r"\baaj\s*kal\b|आजकल|आज कल", re.I)

_W = r"(?<![\wऀ-ॿ])"     # start of a word (Latin or Devanagari)
_E = r"(?![\wऀ-ॿ])"      # end of a word
_WEEKDAY_ALT = "|".join(sorted(map(re.escape, _WEEKDAYS), key=len, reverse=True))
# A bare "sun", "sat" or "wed" is a word, not a day - only full names stand alone.
_WEEKDAY_FULL_ALT = "|".join(sorted((re.escape(w) for w in _WEEKDAYS if len(w) > 4), key=len, reverse=True))


_PATTERNS = (
    ("in_days", re.compile(_W + r"(?:in\s+(\d{1,3})\s+days?|(\d{1,3})\s+(?:din|days?)\s+(?:baad|bad|later|mein|me))" + _E, re.I)),
    ("days_ago", re.compile(_W + r"(\d{1,3})\s+(?:din|days?)\s+(?:pehle|pahle|ago|before)" + _E, re.I)),
    ("next_weekday", re.compile(_W + r"(?:next|agle|agla|aane wale)\s+(" + _WEEKDAY_ALT + r")" + _E, re.I)),
    ("last_weekday", re.compile(_W + r"(?:last|pichhle|pichle|pichla)\s+(" + _WEEKDAY_ALT + r")" + _E, re.I)),
    ("next_week", re.compile(_W + r"(?:next week|agle hafte|agle week|अगले हफ्ते)" + _E, re.I)),
    ("last_week", re.compile(_W + r"(?:last week|pichhle hafte|pichle hafte|पिछले हफ्ते)" + _E, re.I)),
    ("day_after", re.compile(_W + r"(?:day after tomorrow|parso|parson|परसों)" + _E, re.I)),
    ("tomorrow", re.compile(_W + r"(?:tomorrow|tmrw|kal|कल)" + _E, re.I)),
    ("yesterday", re.compile(_W + r"(?:yesterday)" + _E, re.I)),
    ("today", re.compile(_W + r"(?:today|aaj|आज)" + _E, re.I)),
    ("weekday", re.compile(_W + r"(?:on\s+|is\s+)?(" + _WEEKDAY_FULL_ALT + r")(?:\s+ko)?" + _E, re.I)),
)


def _fmt(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {d.year}"


def _upcoming(today: date, weekday: int, skip_today: bool = True) -> date:
    ahead = (weekday - today.weekday()) % 7
    if ahead == 0 and skip_today:
        ahead = 7
    return today + timedelta(days=ahead)


def resolve(text: str, today: date = None):
    """(the expression found, the date it means) for the first relative
    date in `text`, or None."""
    today = today or date.today()
    text = text or ""
    past = bool(_PAST.search(text))
    masked = _NOWADAYS.sub(lambda m: " " * len(m.group(0)), text)     # same length, same positions
    for kind, pattern in _PATTERNS:
        m = pattern.search(masked)
        if not m:
            continue
        expr = m.group(0).strip()
        if kind == "in_days":
            n = int(m.group(1) or m.group(2))
            return expr, today + timedelta(days=n)
        if kind == "days_ago":
            return expr, today - timedelta(days=int(m.group(1)))
        if kind == "next_weekday":
            return expr, _upcoming(today, _WEEKDAYS[m.group(1).lower()])
        if kind == "last_weekday":
            back = (today.weekday() - _WEEKDAYS[m.group(1).lower()]) % 7 or 7
            return expr, today - timedelta(days=back)
        if kind == "next_week":
            return expr, _upcoming(today, 0)                # next Monday
        if kind == "last_week":
            return expr, today - timedelta(days=today.weekday() + 7)
        if kind == "day_after":
            return expr, today + timedelta(days=-2 if past else 2)
        if kind == "tomorrow":
            word = expr.lower()
            if word in ("kal", "कल"):
                return expr, today + timedelta(days=-1 if past else 1)
            return expr, today + timedelta(days=1)
        if kind == "yesterday":
            return expr, today - timedelta(days=1)
        if kind == "today":
            return expr, today
        if kind == "weekday":
            wd = _WEEKDAYS[m.group(1).lower()]
            if past:
                back = (today.weekday() - wd) % 7 or 7
                return expr, today - timedelta(days=back)
            return expr, _upcoming(today, wd, skip_today=False)
    return None


def annotate(value: str, today: date = None) -> str:
    """`value` with the absolute date its first relative date meant, in
    brackets right after that expression - or unchanged."""
    value = value or ""
    if not value.strip() or _ALREADY.search(value):
        return value
    found = resolve(value, today)
    if not found:
        return value
    expr, d = found
    masked = _NOWADAYS.sub(lambda m: " " * len(m.group(0)), value)
    idx = masked.lower().find(expr.lower())
    if idx < 0:
        return value
    end = idx + len(expr)
    return f"{value[:end]} ({_fmt(d)}){value[end:]}"
