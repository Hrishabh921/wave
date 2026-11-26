"""
voice/text_prep.py — Tier 13: text made ready to be SPOKEN
===========================================================

A reply written for the screen isn't ready for a voice: "₹1,50,000",
"9:30 pm", "25°C", "e.g.", a URL, an emoji, **bold**. Cloud voices
(edge-tts) cope with most of it; the local voices Wave falls back to
offline (Piper's espeak-ng phonemizer, Windows SAPI) read "1,50,000" as
"one, fifty, thousand" and URLs letter by letter - and none of them reads
digits the Hindi way inside a Hindi sentence.

    prepare(text, lang="en", numbers="all")

  lang      "en" or "hi": the language of the VOICE that will speak it
            (voice/tts.py picks the voice per chunk - see voice/language.py).
  numbers   "all"   - every number, time, date, amount, percentage and
                      temperature becomes words (local voices);
            "risky" - only what cloud voices get wrong: Indian digit
                      grouping (1,50,000), rupee amounts, and anything in
                      a Hindi voice;
            "none".

Also always: markdown links keep their label, bare URLs become "a link",
emoji are dropped, and a short list of abbreviations is expanded ("e.g."
-> "for example", "Dr." -> "Doctor", "vs" -> "versus").

English uses the Indian system (lakh, crore) when the number is written
that way or is a rupee amount of a lakh or more; otherwise thousand /
million / billion. Hindi number words 0-99 are irregular, so they're a
table; times are said the Hindi way (साढ़े नौ, सवा दस, पौने ग्यारह, डेढ़,
ढाई).
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# English number words
# ---------------------------------------------------------------------------
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ORDINAL_EN = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
               "nine": "ninth", "twelve": "twelfth"}


def _en_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else ""))
    elif n or not parts:
        parts.append(_ONES[n])
    return " ".join(parts)


def number_to_words_en(n: int, indian: bool = False) -> str:
    """12345 -> "twelve thousand three hundred forty-five"; with indian=True
    150000 -> "one lakh fifty thousand"."""
    n = int(n)
    if n < 0:
        return "minus " + number_to_words_en(-n, indian)
    if n < 1000:
        return _en_below_1000(n)
    if indian:
        parts = []
        crore, n = divmod(n, 10 ** 7)
        if crore:
            parts.append(number_to_words_en(crore, indian=True) + " crore")
        lakh, n = divmod(n, 10 ** 5)
        if lakh:
            parts.append(_en_below_1000(lakh) + " lakh")
        thousand, n = divmod(n, 1000)
        if thousand:
            parts.append(_en_below_1000(thousand) + " thousand")
        if n:
            parts.append(_en_below_1000(n))
        return " ".join(parts)
    parts = []
    for value, name in ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand")):
        if n >= value:
            q, n = divmod(n, value)
            parts.append(number_to_words_en(q) + " " + name)
    if n:
        parts.append(_en_below_1000(n))
    return " ".join(parts)


def ordinal_en(n: int) -> str:
    words = number_to_words_en(n)
    head, sep, last = words.rpartition(" ")
    tail_head, dash, tail = last.rpartition("-")
    if tail in _ORDINAL_EN:
        tail = _ORDINAL_EN[tail]
    elif tail.endswith("y"):
        tail = tail[:-1] + "ieth"
    else:
        tail += "th"
    last = tail_head + dash + tail
    return (head + sep + last) if head else last


def year_en(n: int) -> str:
    """2026 -> "twenty twenty-six", 2005 -> "two thousand five", 1990 ->
    "nineteen ninety"."""
    if 2000 <= n <= 2009:
        return number_to_words_en(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return number_to_words_en(hi) + " hundred"
    return number_to_words_en(hi) + " " + (("oh " + _ONES[lo]) if lo < 10 else number_to_words_en(lo))


# ---------------------------------------------------------------------------
# Hindi number words
# ---------------------------------------------------------------------------
_HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस "
    "ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस "
    "इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस "
    "इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस "
    "इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास "
    "इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर "
    "इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी "
    "इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे "
    "इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_HI_0_99) == 100


def number_to_words_hi(n: int) -> str:
    """150000 -> "एक लाख पचास हज़ार"."""
    n = int(n)
    if n < 0:
        return "माइनस " + number_to_words_hi(-n)
    if n < 100:
        return _HI_0_99[n]
    parts = []
    crore, n = divmod(n, 10 ** 7)
    if crore:
        parts.append(number_to_words_hi(crore) + " करोड़")
    lakh, n = divmod(n, 10 ** 5)
    if lakh:
        parts.append(_HI_0_99[lakh] + " लाख")
    thousand, n = divmod(n, 1000)
    if thousand:
        parts.append(_HI_0_99[thousand] + " हज़ार")
    hundred, n = divmod(n, 100)
    if hundred:
        parts.append(_HI_0_99[hundred] + " सौ")
    if n:
        parts.append(_HI_0_99[n])
    return " ".join(parts)


_HI_ORDINAL = {1: "पहला", 2: "दूसरा", 3: "तीसरा", 4: "चौथा", 6: "छठा"}


def ordinal_hi(n: int) -> str:
    return _HI_ORDINAL.get(n) or number_to_words_hi(n) + "वाँ"


def number_words(n: int, lang: str, indian: bool = False) -> str:
    return number_to_words_hi(n) if lang == "hi" else number_to_words_en(n, indian=indian)


def decimal_words(text: str, lang: str, indian: bool = False) -> str:
    """"3.14" -> "three point one four" / "तीन दशमलव एक चार"."""
    whole, _, frac = text.partition(".")
    head = number_words(int(whole or 0), lang, indian)
    if not frac:
        return head
    point = " दशमलव " if lang == "hi" else " point "
    digits = " ".join(number_words(int(d), lang) for d in frac)
    return head + point + digits


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)\s]+\)")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F900-\U0001F9FF"
    "⌀-⏿️‍⃣]+")
_NUM = r"\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_INDIAN_GROUPED_RE = re.compile(r"^\d{1,2}(?:,\d{2})+,\d{3}(?:\.\d+)?$")
_CURRENCY_RE = re.compile(
    r"(?P<pre>₹|Rs\.?\s?|INR\s?|\$|USD\s?|€|£)(?P<num>" + _NUM + r")(?:\s?(?P<scale>lakh|lac|crore|cr|k|K|million|mn|bn|billion)\b)?"
    r"|(?P<num2>" + _NUM + r")\s?(?P<post>rupees|rupaye|रुपये|रुपए|dollars)\b", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(?P<num>" + _NUM + r")\s?%")
_TEMP_RE = re.compile(r"(?P<num>-?" + _NUM + r")\s?°\s?(?P<unit>[CF])\b")
_TIME_RE = re.compile(r"\b(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?::[0-5]\d)?(?:\s?(?P<ap>[ap])\.?\s?m\b\.?)?"
                      r"(?P<baje>\s*(?:बजे|baje)(?![\w\u0900-\u097F]))?", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(?P<y>(?:19|20)\d{2})-(?P<mo>0?[1-9]|1[0-2])-(?P<d>0?[1-9]|[12]\d|3[01])\b")
_DMY_DATE_RE = re.compile(r"\b(?P<a>\d{1,2})[/.](?P<b>\d{1,2})[/.](?P<y>(?:19|20)?\d{2})\b")
_ORDINAL_RE = re.compile(r"\b(?P<num>\d+)(?P<suf>st|nd|rd|th)\b", re.IGNORECASE)
_YEAR_CONTEXT_RE = re.compile(r"\b(?P<w>in|since|from|till|until|of|year|by|before|after)\s+(?P<y>(?:19|20)\d{2})\b", re.IGNORECASE)
_UNIT_RE = re.compile(r"(?P<num>" + _NUM + r")\s?(?P<unit>km/hr?|kmph|kph|m/s|mph|kwh|kw|mah|ghz|mhz|khz|hz|"
                      r"mbps|gbps|kbps|fps|lbs|lb|mg|ft|km|kms|kg|kgs|cm|mm|ml|gb|mb|kb|tb|mins?|hrs?|secs?)\b",
                      re.IGNORECASE)
_PLAIN_NUM_RE = re.compile(r"(?<![\w.])(?P<neg>-)?(?P<num>" + _NUM + r")(?![\w])")
# Tier 48: what the cloned voice (which gets every number as words) read
# wrongly - a phone number as "nine billion eight hundred ...", "Python
# 3.12.1" as "three point one two.1", "1/2 cup" as "one/two cup", "10-15
# minutes" as "ten-fifteen", "5 pm" as "five pm".
_PHONE_RE = re.compile(r"(?<![\w.+,])(?P<cc>\+\d{1,3}[\s-]?)?(?P<body>\d(?:[\s-]?\d){9,12})(?![\w.,])")
_VERSION_RE = re.compile(r"(?<![\w.])(?P<v>[vV](?:ersion\s+)?)?(?P<a>\d{1,3})\.(?P<b>\d{1,3})(?:\.(?P<c>\d{1,4}))?"
                         r"(?:\.(?P<d>\d{1,4}))?(?![\w.])")
_HOUR_AMPM_RE = re.compile(r"\b(?P<h>1[0-2]|0?[1-9])\s?(?P<ap>[ap])\.?\s?m\b\.?", re.IGNORECASE)
_FRACTION_RE = re.compile(r"(?<![\w./])(?P<f>1/2|1/3|2/3|1/4|3/4|24/7|50/50)(?![\w./])")
_RANGE_RE = re.compile(
    r"(?P<pre>\b(?:around|about|approx(?:imately)?|between|from|roughly|lagbhag|takriban)\s+)?"
    r"(?<![\w.:/-])(?P<a>\d+(?:\.\d+)?)\s?[-–]\s?(?P<b>\d+(?:\.\d+)?)(?![\w.:/-])"
    r"(?P<post>\s?(?:%|percent|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?|times|people|"
    r"km|kms|kg|kgs|rupees|lakh|crore|din|ghante|minute|saal|mahine|log)\b)?", re.IGNORECASE)
_FRACTIONS = {"1/2": ("half", "आधा"), "1/3": ("one third", "एक तिहाई"), "2/3": ("two thirds", "दो तिहाई"),
              "1/4": ("a quarter", "एक चौथाई"), "3/4": ("three quarters", "तीन चौथाई"),
              "24/7": ("twenty-four seven", "चौबीसों घंटे"), "50/50": ("fifty-fifty", "आधा-आधा")}

_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
_MONTHS_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर",
              "नवंबर", "दिसंबर"]

_UNITS = {
    "km": ("kilometres", "किलोमीटर"), "kms": ("kilometres", "किलोमीटर"), "kg": ("kilograms", "किलो"),
    "kgs": ("kilograms", "किलो"), "cm": ("centimetres", "सेंटीमीटर"), "mm": ("millimetres", "मिलीमीटर"),
    "ml": ("millilitres", "मिलीलीटर"), "gb": ("gigabytes", "जीबी"), "mb": ("megabytes", "एमबी"),
    "kb": ("kilobytes", "केबी"), "tb": ("terabytes", "टीबी"), "min": ("minutes", "मिनट"), "mins": ("minutes", "मिनट"),
    "hr": ("hours", "घंटे"), "hrs": ("hours", "घंटे"), "sec": ("seconds", "सेकंड"), "secs": ("seconds", "सेकंड"),
    # Tier 48
    "km/h": ("kilometres per hour", "किलोमीटर प्रति घंटा"), "km/hr": ("kilometres per hour", "किलोमीटर प्रति घंटा"),
    "kmph": ("kilometres per hour", "किलोमीटर प्रति घंटा"), "kph": ("kilometres per hour", "किलोमीटर प्रति घंटा"),
    "m/s": ("metres per second", "मीटर प्रति सेकंड"), "mph": ("miles per hour", "मील प्रति घंटा"),
    "kwh": ("kilowatt hours", "किलोवाट घंटे"), "kw": ("kilowatts", "किलोवाट"),
    "mah": ("milliamp hours", "मिलीएम्पियर आवर"), "ghz": ("gigahertz", "गीगाहर्ट्ज़"), "mhz": ("megahertz", "मेगाहर्ट्ज़"),
    "khz": ("kilohertz", "किलोहर्ट्ज़"), "hz": ("hertz", "हर्ट्ज़"), "mbps": ("megabits per second", "एमबीपीएस"),
    "gbps": ("gigabits per second", "जीबीपीएस"), "kbps": ("kilobits per second", "केबीपीएस"),
    "fps": ("frames per second", "फ़्रेम प्रति सेकंड"), "lbs": ("pounds", "पाउंड"), "lb": ("pounds", "पाउंड"),
    "mg": ("milligrams", "मिलीग्राम"), "ft": ("feet", "फ़ीट"),
}

_ABBREVIATIONS_EN = [
    (re.compile(r"\be\.g\.,?", re.IGNORECASE), "for example,"),
    (re.compile(r"\bi\.e\.,?", re.IGNORECASE), "that is,"),
    (re.compile(r"\betc\.", re.IGNORECASE), "et cetera."),
    (re.compile(r"\bvs\.?(?=\s)", re.IGNORECASE), "versus"),
    (re.compile(r"\bapprox\.", re.IGNORECASE), "approximately"),
    (re.compile(r"\bDr\.(?=\s+[A-Z])"), "Doctor"),
    (re.compile(r"\bMr\.(?=\s+[A-Z])"), "Mister"),
    (re.compile(r"\bMrs\.(?=\s+[A-Z])"), "Missus"),
    (re.compile(r"\bMs\.(?=\s+[A-Z])"), "Miz"),
    (re.compile(r"\bSt\.(?=\s+[A-Z])"), "Saint"),
    (re.compile(r"\bNo\.\s?(?=\d)"), "number "),
    (re.compile(r"\bw\.r\.t\.?", re.IGNORECASE), "with respect to"),
]
_ABBREVIATIONS_HI = [
    (re.compile(r"\be\.g\.,?", re.IGNORECASE), "जैसे,"),
    (re.compile(r"\betc\.", re.IGNORECASE), "वगैरह।"),
    (re.compile(r"\bDr\.(?=\s)"), "डॉक्टर"),
    (re.compile(r"\bNo\.\s?(?=\d)"), "नंबर "),
]


def _num_value(text: str) -> float:
    return float(text.replace(",", ""))


def _number_phrase(num: str, lang: str, force_indian: bool = False) -> str:
    plain = num.replace(",", "")
    indian = force_indian or bool(_INDIAN_GROUPED_RE.match(num))
    if "." in plain:
        return decimal_words(plain, lang, indian)
    return number_words(int(plain), lang, indian)


# ---------------------------------------------------------------------------
# The passes
# ---------------------------------------------------------------------------
def strip_markup(text: str, lang: str = "en") -> str:
    text = _MD_LINK_RE.sub(lambda m: m.group(1), text or "")
    text = _URL_RE.sub("एक लिंक" if lang == "hi" else "a link", text)
    text = _EMOJI_RE.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def expand_abbreviations(text: str, lang: str = "en") -> str:
    for pattern, repl in (_ABBREVIATIONS_HI if lang == "hi" else _ABBREVIATIONS_EN):
        text = pattern.sub(repl, text)
    return text


def _currency(m: re.Match, lang: str) -> str:
    pre = (m.group("pre") or "").strip().rstrip(".").lower()
    num = m.group("num") or m.group("num2")
    post = (m.group("post") or "").lower()
    scale = (m.group("scale") or "").lower()
    rupee = pre in ("₹", "rs", "inr") or post in ("rupees", "rupaye", "रुपये", "रुपए")
    if rupee:
        name, sub = ("रुपये", "पैसे") if lang == "hi" else ("rupees", "paise")
    elif pre in ("$", "usd") or post == "dollars":
        name, sub = ("डॉलर", "सेंट") if lang == "hi" else ("dollars", "cents")
    elif pre == "€":
        name, sub = ("यूरो", "सेंट") if lang == "hi" else ("euros", "cents")
    else:
        name, sub = ("पाउंड", "पेंस") if lang == "hi" else ("pounds", "pence")
    scale_word = {"lakh": "lakh", "lac": "lakh", "crore": "crore", "cr": "crore", "k": "thousand",
                  "million": "million", "mn": "million", "bn": "billion", "billion": "billion"}.get(scale, "")
    if lang == "hi":
        scale_word = {"lakh": "लाख", "crore": "करोड़", "thousand": "हज़ार", "million": "मिलियन",
                      "billion": "बिलियन"}.get(scale_word, "")
    indian = rupee and (_num_value(num) >= 100000 or bool(_INDIAN_GROUPED_RE.match(num)))
    plain = num.replace(",", "")
    if "." in plain and len(plain.split(".", 1)[1]) == 2 and not scale_word:
        whole, frac = plain.split(".", 1)
        words = number_words(int(whole), lang, indian) + " " + name
        if int(frac):
            joiner = " और " if lang == "hi" else " and "
            words += joiner + number_words(int(frac), lang) + " " + sub
        return words
    words = _number_phrase(num, lang, force_indian=indian)
    return " ".join(w for w in (words, scale_word, name) if w)


def _time(m: re.Match, lang: str) -> str:
    """Hindi: "9:30" / "9:30 बजे" -> "साढ़े नौ बजे" (the बजे is part of the
    phrase, so a written one isn't doubled). English keeps a written
    "baje" after the words."""
    words = _time_words(m, lang)
    if lang != "hi" and m.group("baje"):
        words += m.group("baje")
    return words


def _time_words(m: re.Match, lang: str) -> str:
    return _clock_words(int(m.group("h")), int(m.group("m")), (m.group("ap") or "").lower(), lang)


def _digits(digits: str, lang: str) -> str:
    return " ".join((_HI_0_99[int(d)] if lang == "hi" else _ONES[int(d)]) for d in digits)


def _phone(m: re.Match, lang: str) -> str:
    """Digit by digit, in the groups it was written in (or 5 + 5 for a
    bare ten-digit mobile number), country code first."""
    cc = re.sub(r"\D", "", m.group("cc") or "")
    body = m.group("body")
    groups = [g for g in re.split(r"[\s-]+", body) if g]
    if len(groups) == 1:
        g = groups[0]
        sizes = {10: (5, 5), 11: (3, 4, 4), 12: (4, 4, 4), 13: (3, 5, 5)}.get(len(g), (len(g),))
        groups, at = [], 0
        for size in sizes:
            groups.append(g[at:at + size])
            at += size
    words = ", ".join(_digits(g, lang) for g in groups)
    return ((("प्लस " if lang == "hi" else "plus ") + _digits(cc, lang) + ", ") if cc else "") + words


def _version(m: re.Match, lang: str) -> str:
    a, b, c, d, v = m.group("a"), m.group("b"), m.group("c"), m.group("d"), m.group("v")
    if c is None and not v:
        return m.group(0)                       # "3.14" is a decimal number - the plain pass reads it
    if not v and d is None and int(a) <= 31 and int(b) <= 12 and len(c) in (2, 4):
        return m.group(0)                       # "25.11.2026" is a date
    point = " दशमलव " if lang == "hi" else " point "
    parts = [p for p in (a, b, c, d) if p is not None]
    words = point.join(number_words(int(p), lang) for p in parts)
    return (("वर्ज़न " if lang == "hi" else "version ") + words) if v else words


def _range(m: re.Match, lang: str) -> str:
    """"10-15 minutes" -> "10 to 15 minutes" - only with a unit after or
    "around"/"between" before it: "India won 3-2" is a score."""
    if not (m.group("pre") or m.group("post")):
        return m.group(0)
    between = (m.group("pre") or "").strip().lower() == "between"
    joiner = " से " if lang == "hi" else (" and " if between else " to ")
    return (m.group("pre") or "") + m.group("a") + joiner + m.group("b") + (m.group("post") or "")


def _clock_words(h: int, mi: int, ap: str, lang: str) -> str:
    if lang == "hi":
        h24 = h
        if ap == "p" and h < 12:
            h24 = h + 12
        elif ap == "a" and h == 12:
            h24 = 0
        part = ""
        if ap or h > 12:
            part = ("सुबह" if 4 <= h24 < 12 else "दोपहर" if 12 <= h24 < 16 else "शाम" if 16 <= h24 < 20 else "रात") + " "
        h12 = h24 % 12 or 12
        nxt = h12 % 12 + 1
        if mi == 0:
            return f"{part}{_HI_0_99[h12]} बजे"
        if mi == 30:
            special = {1: "डेढ़", 2: "ढाई"}.get(h12)
            return f"{part}{special or 'साढ़े ' + _HI_0_99[h12]} बजे"
        if mi == 15:
            return f"{part}सवा {_HI_0_99[h12]} बजे"
        if mi == 45:
            return f"{part}पौने {'एक' if nxt == 1 else _HI_0_99[nxt]} बजे"
        return f"{part}{_HI_0_99[h12]} बजकर {_HI_0_99[mi]} मिनट"
    hour = number_to_words_en(h % 12 or 12) if ap else number_to_words_en(h)
    if mi == 0:
        words = hour + (" o'clock" if not ap else "")
    elif mi < 10:
        words = hour + " oh " + _ONES[mi]
    else:
        words = hour + " " + number_to_words_en(mi)
    if ap:
        words += " a.m." if ap == "a" else " p.m."
    return words


def _date(day: int, month: int, year: Optional[int], lang: str) -> str:
    if lang == "hi":
        out = f"{_HI_0_99[day]} {_MONTHS_HI[month - 1]}"
        return out + (f" {number_to_words_hi(year)}" if year else "")
    out = f"{_MONTHS_EN[month - 1]} {ordinal_en(day)}"
    return out + (f", {year_en(year)}" if year else "")


def expand_numbers(text: str, lang: str = "en", mode: str = "all") -> str:
    """See the module docstring for `mode`."""
    if mode == "none":
        return text
    risky_only = mode == "risky" and lang != "hi"

    text = _CURRENCY_RE.sub(
        lambda m: _currency(m, lang) if (not risky_only or (m.group("pre") or "").strip().rstrip(".").lower()
                                         in ("₹", "rs", "inr") or m.group("post")) else m.group(0), text)
    # Tier 48: a phone number is read digit by digit by every voice.
    text = _PHONE_RE.sub(lambda m: _phone(m, lang), text)
    if risky_only:
        # Only Indian-grouped plain numbers ("1,50,000") are a problem for a cloud voice.
        return _PLAIN_NUM_RE.sub(
            lambda m: ((("minus " if m.group("neg") else "") + _number_phrase(m.group("num"), lang, True))
                       if _INDIAN_GROUPED_RE.match(m.group("num")) else m.group(0)), text)

    def iso(m):
        return _date(int(m.group("d")), int(m.group("mo")), int(m.group("y")), lang)

    def dmy(m):
        a, b, y = int(m.group("a")), int(m.group("b")), int(m.group("y"))
        if y < 100:
            y += 2000
        day, month = (a, b) if b <= 12 else (b, a)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return m.group(0)
        return _date(day, month, y, lang)

    text = _ISO_DATE_RE.sub(iso, text)
    text = _VERSION_RE.sub(lambda m: _version(m, lang), text)        # Tier 48, before "3.12.1" looks like a date
    text = _DMY_DATE_RE.sub(dmy, text)
    text = _TIME_RE.sub(lambda m: _time(m, lang), text)
    text = _HOUR_AMPM_RE.sub(lambda m: _clock_words(int(m.group("h")), 0, m.group("ap").lower(), lang), text)
    text = _FRACTION_RE.sub(lambda m: _FRACTIONS[m.group("f")][1 if lang == "hi" else 0], text)
    text = _RANGE_RE.sub(lambda m: _range(m, lang), text)
    text = _PERCENT_RE.sub(lambda m: _number_phrase(m.group("num"), lang) + (" प्रतिशत" if lang == "hi" else " percent"), text)
    text = _TEMP_RE.sub(lambda m: (("माइनस " if lang == "hi" else "minus ") if m.group("num").startswith("-") else "")
                        + _number_phrase(m.group("num").lstrip("-"), lang)
                        + (" डिग्री " if lang == "hi" else " degrees ")
                        + ({"C": "सेल्सियस", "F": "फ़ारेनहाइट"} if lang == "hi" else {"C": "Celsius", "F": "Fahrenheit"})[m.group("unit").upper()],
                        text)
    text = _UNIT_RE.sub(lambda m: _number_phrase(m.group("num"), lang) + " "
                        + _UNITS[m.group("unit").lower()][1 if lang == "hi" else 0], text)
    text = _ORDINAL_RE.sub(lambda m: (ordinal_hi if lang == "hi" else ordinal_en)(int(m.group("num"))), text)
    if lang != "hi":
        text = _YEAR_CONTEXT_RE.sub(lambda m: m.group("w") + " " + year_en(int(m.group("y"))), text)
    text = _PLAIN_NUM_RE.sub(lambda m: ("माइनस " if lang == "hi" else "minus ") * bool(m.group("neg"))
                             + _number_phrase(m.group("num"), lang), text)
    return text


def prepare(text: str, lang: str = "en", numbers: str = "all") -> str:
    """Everything above, in order. `lang` is the speaking voice's language."""
    lang = "hi" if lang == "hi" else "en"
    text = strip_markup(text or "", lang)
    text = expand_abbreviations(text, lang)
    text = expand_numbers(text, lang, numbers)
    return re.sub(r"\s{2,}", " ", text).strip()
