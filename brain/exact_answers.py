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
Tier 48 adds unit conversions - "5 km in miles", "how many cm in a foot",
"2 kilo kitne pound hote hain", "37 degrees celsius in fahrenheit" - with
the convert_units tool's own tables (automation/calculator.py).
"""
import re
from datetime import date, timedelta
from typing import Optional

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
        month = _MONTHS.get((g.get("m1") or g.get("m2") or "").lower(), 0)
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


def answer(text: str, today: Optional[date] = None):
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

    return _conversion(text, hinglish)


# ---- Tier 48: unit conversions ("5 km in miles", "2 kilo kitne pound") ----
# automation/calculator.py's tables (the convert_units tool's), so the two
# never disagree. "in", "c", "f", "k" alone are too ambiguous to be units
# here; currencies need today's rate and go to the model.
_UNIT_ALIASES = {"kilo": "kg", "kilos": "kg", "°c": "c", "°f": "f", "degree celsius": "c", "degrees celsius": "c",
                 "degree fahrenheit": "f", "degrees fahrenheit": "f", "centigrade": "c", "litre": "l", "liter": "l"}


def _unit_pattern() -> str:
    from automation.calculator import unit_names  # noqa: PLC0415
    words = set(unit_names()) - {"in", "c", "f", "k"}
    words |= set(_UNIT_ALIASES)
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_U = _unit_pattern()
_V = r"(?P<v>-?\d+(?:\.\d+)?)"
_ASK = r"^\W*(?:(?:what(?:'s| is)|how much is|tell me|batao|bata do)\s+)?"
_CONVERT = (
    # "convert 5 km to miles", "what is 5 km in miles?", "100 fahrenheit in celsius", "5 km ko miles mein badlo"
    re.compile(_ASK + r"(?:convert\s+)?" + _V + r"\s*(?P<f>" + _U + r")\s+(?:in|to|into|ko|=)\s+(?P<t>" + _U + r")\b"
               r"(?:\s+(?:mein|me|main))?(?:\s+(?:badlo|badal do|convert karo|kitna|kitne|kya hai|hota hai|"
               r"hote hain|hai|please))?\W*$", re.I),
    # "how many cm in a foot", "how many miles are in 10 km"
    re.compile(r"^\W*how many\s+(?P<t>" + _U + r")\s+(?:are\s+)?(?:there\s+)?in\s+(?:a\s+|an\s+|one\s+|" + _V
               + r"\s*)(?P<f>" + _U + r")\W*$", re.I),
    # "5 km is how many miles", "2 kilo kitne pound hote hain", "5 km mein kitne mile"
    re.compile(_ASK + _V + r"\s*(?P<f>" + _U + r")\s+(?:(?:mein|me|main|is|are|equals)\s+)?(?:kitne|kitna|kitni|"
               r"how many)\s+(?P<t>" + _U + r")\b(?:\s+(?:hote hain|hota hai|hain|hai|honge|hoga))?\W*$", re.I),
)


def _number(x: float) -> str:
    if abs(x) >= 100:
        x = round(x, 1)
    elif abs(x) >= 1:
        x = round(x, 2)
    else:
        x = float(f"{x:.4g}")
    return str(int(x)) if x == int(x) else f"{x}".rstrip("0").rstrip(".")


def _conversion(text: str, hinglish: bool):
    from automation.calculator import convert_value  # noqa: PLC0415
    for pattern in _CONVERT:
        m = pattern.search(text)
        if not m:
            continue
        f_word, t_word = m.group("f"), m.group("t")
        value = float(m.group("v")) if m.group("v") else 1.0
        f = _UNIT_ALIASES.get(f_word.lower(), f_word)
        t = _UNIT_ALIASES.get(t_word.lower(), t_word)
        out = convert_value(value, f, t)
        if out is None:
            return None
        shown = _number(value)
        if hinglish:
            return f"{shown} {f_word} {_number(out)} {t_word} {'hote hain' if out != 1 else 'hota hai'}."
        return f"{shown} {f_word} is {_number(out)} {t_word}."
    return None
