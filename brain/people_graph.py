"""
brain/people_graph.py — Tier 46: who the people in the user's life are.

"My sister's name is Priya", later "Priya works at Infosys" - and then
"where does my sister work?" needs both facts. Remembered facts are plain
key/value rows, so once there are more than a handful the prompt only gets
the ones most similar to the question (database/db_manager.py), and
"sister" is not similar to "priya_works_at". SARA keeps a graph of people:
who each one is to the user and what is known about them, and follows it
from "my sister" to Priya to her job. This does the same over Wave's own
facts:

  - learn(message): the user's own clear statements about a person -
    "my sister's name is Priya", "my friend Rohan works at Google and lives
    in Pune", "meri behen ka naam Priya hai", "mere dost Rohan Pune mein
    rehte hain" - become facts: priya_relation: sister,
    rohan_works_at: Google, rohan_lives_in: Pune. A name has to be
    Capitalised or introduced by "name is" / "naam", so "my sister is sick"
    or "my friend is busy" never make a person. No model call.
  - people(facts): the graph read back from all remembered facts - those
    rows, and the ones the model saves its own way ("sister_name: Priya",
    "best_friend: Rohan", "priya_birthday: 12 March").
  - related_facts(question, facts): "my sister" / "meri behen" / "Priya" in
    a question brings that person's relation and facts along.

A relation nobody has ("my brother" with only a sister known) brings nothing.
MEMORY_PEOPLE_GRAPH=false turns it off.
"""
import re

from settings.config import config

RELATIONS = (
    "mother", "father", "sister", "brother", "son", "daughter", "wife", "husband", "partner",
    "girlfriend", "boyfriend", "fiance", "fiancee", "grandmother", "grandfather", "cousin", "uncle", "aunt",
    "nephew", "niece", "friend", "roommate", "neighbour", "neighbor", "boss", "manager", "colleague",
    "teacher", "mentor",
)
ALIASES = {
    "mom": "mother", "mum": "mother", "mummy": "mother", "maa": "mother", "mammi": "mother", "mata": "mother",
    "dad": "father", "papa": "father", "pitaji": "father", "pita": "father",
    "behen": "sister", "behan": "sister", "bahan": "sister", "didi": "sister",
    "bhai": "brother", "bhaiya": "brother", "bhaiyya": "brother",
    "beta": "son", "beti": "daughter", "patni": "wife", "biwi": "wife", "pati": "husband",
    "dadi": "grandmother", "nani": "grandmother", "dada": "grandfather", "nana": "grandfather",
    "chacha": "uncle", "mama": "uncle", "tau": "uncle", "chachi": "aunt", "mami": "aunt", "bua": "aunt",
    "mausi": "aunt", "dost": "friend", "bestie": "friend", "flatmate": "roommate", "coworker": "colleague",
    "padosi": "neighbour",
}
# Hindi words that are also everyday fillers ("bhai, volume badha do"):
# in a question they count only as "mera bhai", "bhai ka/ki/ke".
_FILLER_LIKE = {"bhai", "beta", "dost", "mama", "dada", "tau"}

_REL_WORDS = sorted(set(RELATIONS) | set(ALIASES), key=len, reverse=True)
_REL = r"(?P<rel>" + "|".join(map(re.escape, _REL_WORDS)) + r")"
_NAME = r"(?P<name>[A-Za-z][a-z]{1,20})"
_CAPNAME = r"(?P<name>[A-Z][a-z]{1,20})"
_NOT_NAMES = frozenset(
    "is a an the very so sick busy here there not also hai ka ki ke bhi ne ko se mein aur and but was will "
    "has had who what where when why how lives works studies stays my your our his her their mera meri mere "
    "naam name ji sahab".split())

_STATEMENTS = (
    # my sister's name is Priya / my sister is called Priya / my best friend is named Rohan
    re.compile(r"\bmy\s+(?:best\s+|elder\s+|younger\s+|older\s+|little\s+|big\s+)?" + _REL
               + r"(?:'s|’s)?\s+(?:name\s+is|is\s+called|is\s+named|named|called)\s+" + _NAME, re.I),
    # meri behen ka naam Priya hai / mere dost ka naam Rohan hai
    re.compile(r"\bmer[aei]\s+(?:badi\s+|bade\s+|chhoti\s+|chhote\s+|best\s+)?" + _REL
               + r"\s+(?:ka|ki|ke)\s+naam\s+" + _NAME, re.I),
    # my sister Priya ... / mere dost Rohan ... (the name Capitalised)
    re.compile(r"\b(?i:my|mer[aei])\s+(?i:best\s+|elder\s+|younger\s+|badi\s+|bade\s+|chhoti\s+|chhote\s+)?(?i:"
               + _REL + r")\s+" + _CAPNAME + r"\b"),
    # my mother is Sunita
    re.compile(r"\b(?i:my)\s+(?i:" + _REL + r")\s+is\s+" + _CAPNAME + r"\b"),
)
_PLACE = r"(?P<obj>[A-Z][A-Za-z&]+(?:\s+[A-Z][A-Za-z&]+)?)"
_WHO = r"(?:(?:she|he|they|wo|woh|vo|who)\s+)?"
# A second clause about the same person (third person only, so "and I live
# in Jaipur" is never theirs).
_ABOUT_THEM = (
    ("lives_in", re.compile(r"\b" + _WHO + r"(?:lives|stays|is\s+living)\s+in\s+" + _PLACE)),
    ("works_at", re.compile(r"\b" + _WHO + r"(?:works|is\s+working)\s+(?:at|in|for)\s+" + _PLACE)),
    ("studies_at", re.compile(r"\b" + _WHO + r"(?:studies|is\s+studying)\s+(?:at|in)\s+" + _PLACE)),
    ("lives_in", re.compile(_PLACE + r"\s+(?i:mein|me)\s+(?i:reh|rah)(?i:ta|ti|te)\b")),
    ("works_at", re.compile(_PLACE + r"\s+(?i:mein|me)\s+(?i:kaam|job)\s+(?i:kar)(?i:ta|ti|te)\b")),
    ("works_at", re.compile(r"(?i:ki|ka|ke)\s+(?i:job)\s+" + _PLACE + r"\s+(?i:mein|me|par|pe)\b")),
    ("studies_at", re.compile(_PLACE + r"\s+(?i:mein|me)\s+(?i:padh)(?i:ta|ti|te)\b")),
)
_WORD = re.compile(r"[a-z]+(?:'s)?")
# The other words of a key that names who someone is ("sister_name",
# "best_friend", "behen_ka_naam") - "brother_city: Mumbai" is about the
# brother, not his name.
_NAME_KEY_PARTS = frozenset(
    "name naam ka ki ke best elder younger older little big badi bade chhoti chhote my user users s is".split())


def _enabled() -> bool:
    return bool(getattr(config, "MEMORY_PEOPLE_GRAPH", True))


def canonical(word: str):
    """'behen' -> 'sister', 'sister' -> 'sister', anything else -> None."""
    w = (word or "").lower().removesuffix("'s").removesuffix("’s")
    if w in RELATIONS:
        return {"neighbor": "neighbour", "fiancee": "fiance"}.get(w, w)
    return ALIASES.get(w)


def relation_facts(message: str) -> list:
    """[(key, value)] the user's own sentence says about a person; [] when unsure."""
    text = str(message or "")
    for pattern in _STATEMENTS:
        m = pattern.search(text)
        if not m:
            continue
        name, rel = m.group("name"), canonical(m.group("rel"))
        if rel is None or name.lower() in _NOT_NAMES:
            continue
        name = name[:1].upper() + name[1:].lower()
        slug = name.lower()
        out = [(f"{slug}_relation", rel)]
        tail = text[m.end():]
        seen = set()
        for predicate, about in _ABOUT_THEM:
            if predicate in seen:
                continue
            fm = about.search(tail)
            if fm and fm.group("obj").lower() not in _NOT_NAMES:
                out.append((f"{slug}_{predicate}", fm.group("obj")))
                seen.add(predicate)
        return out
    return []


def learn(message: str, save) -> list:
    """Saves what relation_facts() finds with `save(key, value)`; returns it."""
    if not _enabled():
        return []
    found = relation_facts(message)
    for key, value in found:
        save(key, value)
    return found


def _looks_like_name(value: str) -> bool:
    v = str(value or "").strip()
    return 0 < len(v) <= 30 and len(v.split()) <= 2 and bool(re.fullmatch(r"[A-Za-z][A-Za-z .'-]*", v))


def people(facts) -> dict:
    """{name_slug: {"name": str, "relations": set, "facts": [(key, value)]}} from
    remembered (key, value) pairs."""
    found = {}

    def person(name):
        slug = name.strip().split()[0].lower()
        return found.setdefault(slug, {"name": name.strip(), "relations": set(), "facts": []})

    for key, value in facts:
        k = str(key or "").lower()
        parts = k.split("_")
        if k.endswith("_relation") and len(parts) >= 2:
            rel = canonical(str(value or "").split()[-1] if value else "")
            if rel:
                p = person(parts[0].capitalize())
                p["relations"].add(rel)
                p["facts"].append((key, value))
            continue
        rels = {canonical(w) for w in parts} - {None}
        others = {w for w in parts if canonical(w) is None}
        if len(rels) == 1 and others <= _NAME_KEY_PARTS and _looks_like_name(value):
            p = person(str(value))
            p["relations"] |= rels
            p["facts"].append((key, value))
    for key, value in facts:
        parts = str(key or "").lower().split("_")
        for slug, p in found.items():
            if slug in parts and (key, value) not in p["facts"]:
                p["facts"].append((key, value))
    return found


def _asked_relations(question: str) -> set:
    q = (question or "").lower()
    words = _WORD.findall(q)
    rels = set()
    for i, w in enumerate(words):
        base = w.removesuffix("'s")
        rel = canonical(base)
        if rel is None:
            continue
        if base in _FILLER_LIKE:
            before = words[i - 1] if i else ""
            after = words[i + 1] if i + 1 < len(words) else ""
            if before not in ("mera", "meri", "mere", "my") and after not in ("ka", "ki", "ke"):
                continue
        rels.add(rel)
    return rels


def related_facts(question: str, facts, limit: int = 8) -> list:
    """The facts about the people a question names - by relation ("my
    sister", "meri behen") or by name ("Priya") - relation first."""
    if not _enabled() or not question:
        return []
    facts = list(facts)
    graph = people(facts)
    rels = _asked_relations(question)
    words = set(_WORD.findall(question.lower()))
    words |= {w.removesuffix("'s") for w in words}
    out = []
    for slug, p in graph.items():
        if slug in words or (p["relations"] & rels):
            for kv in sorted(p["facts"], key=lambda kv: not str(kv[0]).lower().endswith("_relation")):
                if kv not in out:
                    out.append(kv)
    # ...and facts kept under the relation itself ("brother_city: Mumbai").
    if rels:
        for kv in facts:
            if kv not in out and rels & {canonical(w) for w in str(kv[0]).lower().split("_")}:
                out.append(kv)
    return out[:limit]
