"""
brain/memory_rank.py — Tier 46: which remembered facts a question is about.

Facts are matched to a message by sentence similarity (all-MiniLM-L6-v2 on
"key: value"), but measured on the memory bench (bench/memory_bench.py)
that alone misses the everyday ones: "where do I live now?" is only 0.20
similar to "city: Jaipur", "do you remember what I'm called?" 0.20 to
"name: Arjun" (and 0.24 to "pet: a beagle named Bruno"), "mujhe kaunsi
mithai pasand hai?" closer to "name: Arjun" than to "favorite_sweet". Below
the old 0.35 floor nothing matched at all and the prompt got the first
eight facts in alphabetical order. SARA mixes in word overlap; so does this:
a question's words - and the words a fact's label uses for the same thing
("live" / "rehta" -> city, address, home; "kaam" -> job, workplace;
"mithai" -> sweet; "called" / "naam" -> name) - found in the fact's label
count fully, in its value half. score = similarity + LEXICAL_WEIGHT * share.
"""
import re

LEXICAL_WEIGHT = 0.25

_LIVE = ("city", "address", "home", "hometown", "lives", "live", "location", "stay", "stays", "town")
_WORK = ("work", "works", "workplace", "job", "company", "office", "employer", "profession", "occupation")
_STUDY = ("study", "studies", "college", "school", "course", "university", "degree", "class")
_LIKE = ("favorite", "favourite", "likes", "like", "loves", "love", "preferred")
SYNONYMS = {
    "live": _LIVE, "lives": _LIVE, "living": _LIVE, "stay": _LIVE, "stays": _LIVE, "rehta": _LIVE,
    "rehti": _LIVE, "rehte": _LIVE, "rahta": _LIVE, "rahti": _LIVE, "rahte": _LIVE, "city": _LIVE,
    "sheher": _LIVE, "shahar": _LIVE, "ghar": ("home", "address", "house", "city"), "address": ("address", "home"),
    "work": _WORK, "works": _WORK, "working": _WORK, "job": _WORK, "company": _WORK, "office": _WORK,
    "kaam": _WORK, "naukri": _WORK,
    "study": _STUDY, "studies": _STUDY, "studying": _STUDY, "college": _STUDY, "school": _STUDY,
    "padhta": _STUDY, "padhti": _STUDY, "padhai": _STUDY,
    "called": ("name",), "name": ("name",), "naam": ("name",),
    "birthday": ("birthday", "dob", "born"), "born": ("birthday", "dob", "born"), "janamdin": ("birthday",),
    "food": ("food", "dish", "cuisine"), "khana": ("food", "dish"), "eat": ("food", "dish"),
    "mithai": ("sweet", "sweets", "dessert"), "sweet": ("sweet", "sweets", "dessert"),
    "color": ("color", "colour"), "colour": ("color", "colour"), "rang": ("color", "colour"),
    "language": ("language", "languages"), "bhasha": ("language",),
    "pet": ("pet", "dog", "cat"), "dog": ("pet", "dog"), "cat": ("pet", "cat"),
    "pasand": _LIKE, "favorite": _LIKE, "favourite": _LIKE, "like": _LIKE, "love": _LIKE,
    "phone": ("phone", "mobile", "number"), "mobile": ("phone", "mobile", "number"),
}
_STOP = set(
    "the a an and or of to in at on for with from by is are was were be been am do does did done have has had "
    "what which where when who whom whose why how my mine me i i'm im you your we our us it its this that these "
    "those now then before after ago again still also just really please tell know remember remind say said "
    "about any some there here today yesterday tomorrow days day mera meri mere mujhe mujhse main hum maine kya "
    "kaun kaunsa kaunsi kahan kab kaise kitna kitne kitni hai hain tha thi the ho hoon raha rahi rahe abhi pehle "
    "ka ki ke ko se mein me ne bhi aur ya to wo woh vo yeh ye".split())
_WORD = re.compile(r"[a-z]+")


def content_words(text: str) -> list:
    words = []
    for w in _WORD.findall((text or "").lower().replace("'s", "")):
        if len(w) >= 3 and w not in _STOP and w not in words:
            words.append(w)
    return words


def lexical(question_words: list, key: str, value: str) -> float:
    """Share of the question's words this fact names: in its label 1, in its value 0.5."""
    if not question_words:
        return 0.0
    label = set(str(key or "").lower().replace("-", "_").split("_"))
    said = set(_WORD.findall(str(value or "").lower()))
    hits = 0.0
    for w in question_words:
        if ({w} | set(SYNONYMS.get(w, ()))) & label:
            hits += 1.0
        elif w in said:
            hits += 0.5
    return hits / len(question_words)
