"""
brain/exact_answers.py — Tier 42: answers worked out in code, not guessed.

"25 december mein kitne din bache hain?", "45 din baad kaunsi date hogi?",
"what day was 15 august 1947?", "2400 ka 15 percent?" - a language model
works these out token by token and sometimes gets them wrong, and each costs
a model call. SARA answers them exactly, in code. This does the same for
Wave: date differences (days until / since a date), a date N days or weeks
from today or ago, the weekday of a date, and percentages - in English,
Hinglish and Hindi phrasing, answered in the same language. Anything else
returns None and goes to the model as before. Pure functions, no I/O.
"""
import re
from datetime import date, timedelta

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_FIXED_DAYS = {   # dates that never move (festivals on the lunar calendar do - those go to the model)
    "new year": (1, 1), "christmas": (12, 25), "independence day": (8, 15), "republic day": (1, 26),
    "gandhi jayanti": (10, 2), "valentine's day": (2, 14), "valentines day": (2, 14),
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE = (r"(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>" + _MONTH_ALT + r")|(?P<m2>" + _MONTH_ALT +
         r")\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?|(?P<fixed>" + "|".join(map(re.escape, _FIXED_DAYS)) +
         r"))(?:,?\s+(?P<y>\d{4}))?")
_HINGLISH = re.compile(r"\b(kitne|kitna|kitni|din|baad|pehle|kaunsi|kaunsa|ko|hai|tha|thi|bache|baaki|ka|ke|ki|"
                       r"hoga|hogi|percent|pratishat)\b", re.I)

def _d(prefix: str) -> str:
    """The date pattern with its group names prefixed (one alternative each)."""
    return _DATE.replace("?P<", "?P<" + prefix)


_UNTIL = (
    re.compile(r"(?:how many days|days)\s+(?:are\s+)?(?:left\s+)?(?:until|till|to|before)\s+" + _d("a_"), re.I),
    re.compile(_d("b_") + r"\s+(?:mein|me|tak)\s+kitne\s+din", re.I),
    re.compile(r"kitne\s+din\s+(?:bache|baaki|reh gaye)\s+(?:hain\s+)?" + _d("c_"), re.I),
)
_SINCE = (
    re.compile(r"how many days (?:since|from)\s+" + _d("a_"), re.I),
    re.compile(_d("b_") + r"\s+(?:ko|se)\s+kitne\s+din\s+(?:ho gaye|hue|beet gaye)", re.I),
)
_OFFSET = re.compile(r"(?P<n>\d{1,4})\s+(?P<unit>days?|din|weeks?|hafte|hafton)\s+(?P<dir>from (?:today|now)|later|"
                     r"after|baad|ago|pehle|before)", re.I)
_WEEKDAY = (
    re.compile(r"(?:what|which)\s+day\s+(?:of the week\s+)?(?:is|was|will be|falls on)\s+" + _d("a_"), re.I),
    re.compile(_d("b_") + r"\s+(?:ko\s+)?(?:kaunsa|kaun sa|kya)\s+din", re.I),
)
_PERCENT = re.compile(r"(?P<p>\d+(?:\.\d+)?)\s*(?:%|percent|pratishat)\s+of\s+(?P<n>\d+(?:\.\d+)?)|"
                      r"(?P<n2>\d+(?:\.\d+)?)\s+(?:ka|ke|ki)\s+(?P<p2>\d+(?:\.\d+)?)\s*(?:%|percent|pratishat)", re.I)


def _date_from(groups: dict, today: date, prefix: str = "", future: bool = True):
    g = {k[len(prefix):]: v for k, v in groups.items() if k.startswith(prefix) and v}
    if "fixed" in g:
        month, day = _FIXED_DAYS[g["fixed"].lower()]
    else:
        day = int(g.get("d1") or g.get("d2") or 0)
        month = _MONTHS.get((g.get("m1") or g.get("m2") or "").lower())
    if not month or not day:
        return None
    year = int(g["y"]) if g.get("y") else today.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not g.get("y") and future and d < today:
        d = date(year + 1, month, day)
    if not g.get("y") and not future and d > today:
        d = date(year - 1, month, day)
    return d


def _first(patterns, text):
    """[(group prefix, match)] for the first of `patterns` that matches, or []."""
    for pattern in patterns:
        m = pattern.search(text)
        if m:
            prefix = next(iter(k for k in m.groupdict() if "_" in k)).split("_")[0] + "_"
            return [(prefix, m)]
    return []


def _fmt(d: date) -> str:
    return f"{d.strftime('%A')}, {d.day} {d.strftime('%B')} {d.year}"


def answer(text: str, today: date = None):
    """A one-line spoken answer, or None when this isn't one of these."""
    today = today or date.today()
    text = (text or "").strip()
    if not text or len(text) > 160:
        return None
    hinglish = bool(_HINGLISH.search(text))

    m = _PERCENT.search(text)
    if m:
        p = float(m.group("p") or m.group("p2"))
        n = float(m.group("n") or m.group("n2"))
        value = round(p * n / 100.0, 2)
        shown = int(value) if value == int(value) else value
        pct = int(p) if p == int(p) else p
        whole = int(n) if n == int(n) else n
        return f"{whole} ka {pct} percent {shown} hai." if hinglish else f"{pct}% of {whole} is {shown}."

    for prefix, m in _first(_UNTIL, text):
        d = _date_from(m.groupdict(), today, prefix=prefix)
        if d:
            n = (d - today).days
            if hinglish:
                return f"{d.day} {d.strftime('%B')} mein {n} din baaki hain." if n else "Woh aaj hi hai!"
            return f"{n} days until {_fmt(d)}." if n else "That's today!"

    for prefix, m in _first(_SINCE, text):
        d = _date_from(m.groupdict(), today, prefix=prefix, future=False)
        if d:
            n = (today - d).days
            return (f"{d.day} {d.strftime('%B')} {d.year} ko {n} din ho gaye." if hinglish
                    else f"{n} days since {_fmt(d)}.")

    for prefix, m in _first(_WEEKDAY, text):
        d = _date_from(m.groupdict(), today, prefix=prefix)
        if d:
            past = d < today
            return (f"{d.day} {d.strftime('%B')} {d.year} ko {d.strftime('%A')} {'tha' if past else 'hai'}."
                    if hinglish else f"{d.day} {d.strftime('%B')} {d.year} {'was' if past else 'is'} a {d.strftime('%A')}.")

    m = _OFFSET.search(text)
    if m and re.search(r"\b(date|day|tarikh|tareekh|kaunsi|kaunsa|kya hogi|what)\b", text, re.I):
        n = int(m.group("n"))
        days = n * 7 if m.group("unit").lower().startswith(("week", "haft")) else n
        back = m.group("dir").lower() in ("ago", "pehle", "before")
        d = today - timedelta(days=days) if back else today + timedelta(days=days)
        if hinglish:
            return f"Us din {_fmt(d)} {'tha' if back else 'hoga'}."
        return f"That {'was' if back else 'will be'} {_fmt(d)}."
    return None
