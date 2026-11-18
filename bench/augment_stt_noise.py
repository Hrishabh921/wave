"""
bench/augment_stt_noise.py — Tier 17: "golden set >= 600 rows, plus a
noisy-STT augmentation tool that generates misspelled and dropped-word
variants" (docs/ROADMAP_BEAT_SARA.md, Tier 17's "Bench" bullet).

A real, reusable, idempotent tool - not a one-off script. Run it any
time the clean source rows in bench/golden/intents_en.jsonl/
intents_hi.jsonl/intents_hinglish.jsonl grow (a later tier adds more
intents, say) and it regenerates bench/stt_noise/noisy_variants.jsonl
from scratch to match: one noisy variant per clean positive row, same
expected skill/args as its source (STT noise changes how a sentence
SOUNDS, not what it MEANT), fully deterministic (seeded per source row
from its own file name + line number) so re-running produces
byte-identical output when the sources haven't changed.

Written to bench/stt_noise/, deliberately NOT bench/golden/ - the
roadmap's own comparison table defines "golden set size" as "every row
across bench/golden/*.jsonl" and its "Done when" floor (accuracy >=
85%, Hindi/Hinglish >= 75%, macro-F1 >= 70%, false-trigger <= 3%) is
computed over exactly that directory (dataset.GOLDEN_FILES). Mixing
deliberately-degraded noise into the same scored set would either (a)
force tuning the noise to be unrealistically gentle just to clear the
floor, or (b) drag a real accuracy/F1 number below a threshold that
was never meant to measure noise robustness in the first place - both
dishonest in different directions. Keeping this tool's output in its
own directory keeps the two measurements - "accuracy on realistic
phrasing diversity" vs. "robustness to STT noise on already-known
phrasings" - honest and separately reportable. See NEXT_STEPS.md's
Tier 17 entry for this tool's own current robustness number, measured
and reported the same way as the 135-vs-150 rule-count shortfall: as a
real finding, not something to quietly tune away.

Six transforms, picked in this file deterministically, modelling real
transcription artifacts rather than arbitrary text mangling. All of
them are biased AWAY from corrupting the command/trigger word itself
(see _LIKELY_TRIGGER_WORDS) wherever an alternative target exists -
realistic, because short, common command verbs ("open", "close",
"set") are exactly what an STT model is best at recognizing reliably;
it's the long-tail content around them (app names, note text, city
names) that typically carries the noise:
  * word_drop            - a low-energy *function* word dropped
                            entirely (never a command verb - see
                            _DROPPABLE_WORDS; declines rather than
                            falling back to a destructive random drop
                            when no safe candidate is present)
  * typo_drop_letter     - one interior letter missing from a word
  * typo_swap_letters    - two adjacent interior letters transposed
  * phonetic_substitute  - a real homophone-style STT confusion
                            (English only - see _PHONETIC_SUBS; Hindi/
                            Hinglish rows use the other, script-agnostic
                            transforms instead rather than invented
                            phonetic pairs this project can't verify)
  * filler_insertion     - "um"/"uh"/"like" inserted mid-utterance (not
                            at position 0, where it would corrupt a
                            prefix match in a way a real pause after
                            the command word mostly wouldn't)
  * doubled_word         - a stutter/repeat artifact

Deliberately NOT self-verifying like add_tier17_golden.py's intent-
addition generator (which aborts on any routing mismatch - there, a
mismatch means the rule was written wrong). Here a mismatch is a
legitimate, honest data point about router robustness to noise. Every
generated row's note (`n`) records which transform was applied and
whether intent_router.route() currently gets it right, so a reviewer
can see the real, honest split at a glance rather than a set quietly
tuned until it all passes.

Usage:
    PYTHONPATH=<benchstubs> python -m bench.augment_stt_noise
    PYTHONPATH=<benchstubs> python -m bench.augment_stt_noise --quiet
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

BENCH_DIR = Path(__file__).resolve().parent
ROOT = BENCH_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.dataset import Row, load_jsonl  # noqa: E402

SOURCE_FILES = ("intents_en.jsonl", "intents_hi.jsonl", "intents_hinglish.jsonl")
OUTPUT_FILE = BENCH_DIR / "stt_noise" / "noisy_variants.jsonl"

# A handful of real, common STT homophone-style confusions. English
# only, deliberately - see the module docstring for why Hindi/Hinglish
# rows don't get an invented equivalent list here.
_PHONETIC_SUBS = {
    "to": "too", "for": "four", "their": "there", "there": "their",
    "write": "right", "won": "one", "no": "know", "buy": "by",
    "new": "knew", "weather": "whether", "here": "hear", "are": "our",
    "then": "than", "its": "it's",
}

# Short, usually-droppable function words an STT engine commonly eats
# at low volume/energy - these, and only these, are candidates for
# word_drop. Never a command verb: dropping "the"/"please"/"ka" leaves
# the sentence's meaning (and usually its routing) intact; dropping
# "open"/"close"/"set" would not.
_DROPPABLE_WORDS = {
    "the", "a", "an", "is", "to", "please", "on", "in", "my", "it",
    "of", "that", "this", "do", "me", "ka", "ki", "ko", "hai", "mein",
    "se", "ek", "kya",
}

# Command/trigger verbs that _RULES prefixes/suffixes actually key off
# of - typo and doubled-word transforms avoid these words whenever a
# non-trigger word is also available, so noise lands on the argument
# content (app names, note text, search terms, ...) rather than on the
# literal token a fast-path rule is matching against. Not exhaustive by
# design - a reasonable sample of the highest-frequency trigger words
# across the 135 rules, not a mirror of _RULES itself (which would
# make this list a maintenance burden tightly coupled to intent_router.py).
_LIKELY_TRIGGER_WORDS = {
    "open", "close", "start", "stop", "set", "turn", "enable", "disable",
    "play", "search", "cancel", "delete", "mark", "complete", "remove",
    "restart", "switch", "focus", "minimize", "maximize", "summarize",
    "quiz", "recall", "forget", "reset", "take", "note", "calculate",
    "type", "write", "what's", "whats", "find", "get", "list", "show",
    "mute", "unmute", "lock", "shutdown", "sleep", "hibernate",
}


def _rng_for(source_file: str, line_no: int) -> random.Random:
    seed_bytes = f"{source_file}:{line_no}".encode("utf-8")
    seed = int(hashlib.sha256(seed_bytes).hexdigest()[:12], 16)
    return random.Random(seed)


def _words(text: str) -> List[str]:
    return text.split(" ")


def _is_trigger(word: str) -> bool:
    return word.lower().strip(",.?!") in _LIKELY_TRIGGER_WORDS


def _prefer_non_trigger(candidates: List[int], words: List[str], rng: random.Random) -> Optional[int]:
    """Picks among `candidates` (word indices), preferring ones that
    aren't a likely trigger word - falls back to any candidate only if
    every one of them happens to be a trigger word."""
    if not candidates:
        return None
    non_trigger = [i for i in candidates if not _is_trigger(words[i])]
    return rng.choice(non_trigger) if non_trigger else rng.choice(candidates)


def _drop_one_word(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    if len(words) < 3:
        return None
    droppable_idx = [i for i, w in enumerate(words) if w.lower().strip(",.?!") in _DROPPABLE_WORDS]
    if not droppable_idx:
        return None  # decline rather than destructively dropping a content/trigger word
    idx = rng.choice(droppable_idx)
    out = words[:idx] + words[idx + 1:]
    return " ".join(out) if out else None


def _typo_drop_letter(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    candidates = [i for i, w in enumerate(words) if len(w) >= 5]
    idx = _prefer_non_trigger(candidates, words, rng)
    if idx is None:
        return None
    w = words[idx]
    pos = rng.randrange(1, len(w) - 1)  # never the first/last char
    words[idx] = w[:pos] + w[pos + 1:]
    return " ".join(words)


def _typo_swap_letters(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    candidates = [i for i, w in enumerate(words) if len(w) >= 4]
    idx = _prefer_non_trigger(candidates, words, rng)
    if idx is None:
        return None
    w = list(words[idx])
    pos = rng.randrange(1, len(w) - 2)  # swap pos/pos+1, both interior
    w[pos], w[pos + 1] = w[pos + 1], w[pos]
    words[idx] = "".join(w)
    return " ".join(words)


def _phonetic_substitute(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    candidates = [i for i, w in enumerate(words) if w.lower().strip(",.?!") in _PHONETIC_SUBS]
    if not candidates:
        return None
    idx = rng.choice(candidates)
    key = words[idx].lower().strip(",.?!")
    words[idx] = _PHONETIC_SUBS[key]
    return " ".join(words)


def _insert_filler(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    if len(words) < 2:
        return None
    filler = rng.choice(("um", "uh", "like"))
    pos = rng.randrange(1, len(words) + 1)  # never position 0 - see module docstring
    words.insert(pos, filler)
    return " ".join(words)


def _double_word(text: str, rng: random.Random) -> Optional[str]:
    words = _words(text)
    candidates = list(range(len(words)))
    idx = _prefer_non_trigger(candidates, words, rng)
    if idx is None:
        return None
    words.insert(idx, words[idx])
    return " ".join(words)


_TRANSFORMS: List[Tuple[str, Callable[[str, random.Random], Optional[str]]]] = [
    ("word_drop", _drop_one_word),
    ("typo_drop_letter", _typo_drop_letter),
    ("typo_swap_letters", _typo_swap_letters),
    ("phonetic_substitute", _phonetic_substitute),
    ("filler_insertion", _insert_filler),
    ("doubled_word", _double_word),
]


def _noisy_variant(row: Row) -> Optional[Tuple[str, str]]:
    """Returns (transform_name, noisy_text), trying every transform in
    a per-row-deterministic shuffled order until one actually applies,
    or None if every transform declined (only possible for very short
    rows with no safe droppable/typo-able/fillerable word at all)."""
    order_rng = _rng_for(row.source_file, row.line_no)
    order = list(_TRANSFORMS)
    order_rng.shuffle(order)
    for name, fn in order:
        result = fn(row.text, _rng_for(row.source_file, row.line_no))  # fresh, same-seed rng per attempt
        if result and result != row.text:
            return name, result
    return None


def generate(quiet: bool = False) -> List[dict]:
    source_rows: List[Row] = []
    for fname in SOURCE_FILES:
        source_rows.extend(load_jsonl(BENCH_DIR / "golden" / fname))

    # Importing the router is deferred until here so this module stays
    # importable (e.g. from a future review script) without pulling in
    # the whole brain/ package just to read its docstring.
    from brain import intent_router as ir

    out_rows: List[dict] = []
    skipped = 0
    router_ok = 0
    by_transform: dict = {}
    for row in source_rows:
        variant = _noisy_variant(row)
        if variant is None:
            skipped += 1
            continue
        transform_name, noisy_text = variant
        result = ir.route(noisy_text)
        matched = result is not None and result.skill == row.intent
        by_transform.setdefault(transform_name, [0, 0])
        by_transform[transform_name][1] += 1
        if matched:
            router_ok += 1
            by_transform[transform_name][0] += 1
        note = (f"noisy-stt {transform_name} of golden/{row.source_file}:{row.line_no} "
                f"('{row.text}'); router: {'ok' if matched else 'miss'}")
        out_rows.append({
            "t": noisy_text, "i": row.intent, "l": row.lang, "s": "noisy_stt",
            **({"a": row.expected_args} if row.expected_args is not None else {}),
            "n": note,
        })

    if not quiet:
        total = len(out_rows)
        pct = (router_ok / total * 100) if total else 0.0
        print(f"augment_stt_noise: {total} noisy variants generated from {len(source_rows)} source rows "
              f"({skipped} source rows had no safe transform and were skipped)")
        print(f"router robustness on noisy variants (informational only - NOT part of the scored "
              f"golden set): {router_ok}/{total} still route correctly ({pct:.1f}%)")
        for name in sorted(by_transform):
            ok, n = by_transform[name]
            print(f"  {name}: {ok}/{n} ({ok / n * 100:.1f}%)")
    return out_rows


def write(rows: List[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(json.dumps({"_defaults": {}}) + "\n")
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="suppress the summary print")
    args = parser.parse_args(argv)
    rows = generate(quiet=args.quiet)
    write(rows)
    if not args.quiet:
        print(f"wrote {len(rows)} rows to {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
