"""
bench/heldout_bench.py — Tier 50: commands nobody tuned anything on.

bench/golden/ is the router's regression set: every rule was written with
those sentences in view, so its accuracy says little about a sentence the
user says differently. bench/heldout/commands.jsonl (Tier 50) is the
opposite - 75 everyday commands in other words ("bump the volume up",
"gaana thoda rok do", "is the laptop charging") and 41 sentences that must
run nothing ("the brightness of the sun hurts", "skip the ads", "minimize
the risk"), written once and never used to change a rule, a threshold or
an example. It measures what a user would get: the fast path
(brain/intent_router.py) alone, and with the semantic fallback (Tier 45,
brain/semantic_intents.py, on the real sentence model) after it.

A "wrong" result is an action other than the one meant; a "false trigger"
is any action on a sentence that meant none. --gate fails on any wrong or
false trigger from the semantic layer (the fast path's own are reported,
not gated: it predates this set).

    python -m bench.heldout_bench
    python -m bench.heldout_bench --report-md bench/results/HELDOUT.md --gate
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import intent_router  # noqa: E402
from brain import semantic_intents as si  # noqa: E402
from settings.config import config  # noqa: E402

DATA = ROOT / "bench" / "heldout" / "commands.jsonl"
# The fast path names a few of these differently (same backend call).
SAME = {("minimize_all_windows", "show_desktop")}


def load(path=DATA) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _same(expected: str, got: str) -> bool:
    return got == expected or (expected, got) in SAME


def run(rows: list, semantic=None) -> list:
    """One record per row: what the fast path did, and what the semantic layer did after it."""
    out = []
    for row in rows:
        text, expected = row["t"], row["i"]
        routed = intent_router.route(text)
        fast = routed.skill if routed is not None else None
        sem = None
        if fast is None and semantic is not None:
            hit = semantic.match(text)
            sem = hit[0] if hit else None
        out.append({"t": text, "i": expected, "l": row.get("l", ""), "fast": fast, "semantic": sem})
    return out


def metrics(records: list) -> dict:
    commands = [r for r in records if r["i"] != "chat"]
    chat = [r for r in records if r["i"] == "chat"]

    def ok(r, final):
        return final is not None and _same(r["i"], final)

    m = {"commands": len(commands), "negatives": len(chat)}
    m["fast_right"] = sum(1 for r in commands if ok(r, r["fast"]))
    m["both_right"] = sum(1 for r in commands if ok(r, r["fast"] or r["semantic"]))
    m["fast_wrong"] = sum(1 for r in commands if r["fast"] and not ok(r, r["fast"]))
    m["semantic_wrong"] = sum(1 for r in commands if r["semantic"] and not ok(r, r["semantic"]))
    m["fast_false_triggers"] = sum(1 for r in chat if r["fast"])
    m["semantic_false_triggers"] = sum(1 for r in chat if r["semantic"])
    for lang in ("en", "hinglish"):
        rows = [r for r in commands if r["l"] == lang]
        m[f"{lang}_commands"] = len(rows)
        m[f"{lang}_both_right"] = sum(1 for r in rows if ok(r, r["fast"] or r["semantic"]))
        m[f"{lang}_fast_right"] = sum(1 for r in rows if ok(r, r["fast"]))
    return m


def gate_failures(m: dict) -> list:
    fails = []
    if m["semantic_wrong"]:
        fails.append(f"{m['semantic_wrong']} wrong action(s) from the semantic layer")
    if m["semantic_false_triggers"]:
        fails.append(f"{m['semantic_false_triggers']} false trigger(s) from the semantic layer")
    return fails


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100.0 * n / d:.0f}%)" if d else "-"


def report_md(m: dict, semantic_on: bool) -> str:
    lines = ["## Held-out commands", "",
             f"{m['commands']} commands and {m['negatives']} sentences that must run nothing, "
             f"written once (Tier 50) and never used to tune anything.", "",
             "| | Fast path alone | " + ("+ semantic fallback |" if semantic_on else "(semantic off) |"),
             "| --- | --- | --- |",
             f"| Commands run right | {_pct(m['fast_right'], m['commands'])} | "
             f"{_pct(m['both_right'], m['commands'])} |",
             f"| English | {_pct(m['en_fast_right'], m['en_commands'])} | "
             f"{_pct(m['en_both_right'], m['en_commands'])} |",
             f"| Hinglish | {_pct(m['hinglish_fast_right'], m['hinglish_commands'])} | "
             f"{_pct(m['hinglish_both_right'], m['hinglish_commands'])} |",
             f"| Wrong action | {m['fast_wrong']} | {m['fast_wrong'] + m['semantic_wrong']} |",
             f"| False trigger on {m['negatives']} non-commands | {m['fast_false_triggers']} | "
             f"{m['fast_false_triggers'] + m['semantic_false_triggers']} |"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--report-md", default=None)
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    saved = config.SEMANTIC_INTENTS
    config.SEMANTIC_INTENTS = True
    try:
        semantic = si.SemanticIntents()
        semantic_on = semantic._ready()
        records = run(load(args.data), semantic if semantic_on else None)
    finally:
        config.SEMANTIC_INTENTS = saved
    m = metrics(records)
    md = report_md(m, semantic_on)
    print(md)
    for r in records:
        final = r["fast"] or r["semantic"]
        if (r["i"] == "chat" and final) or (r["i"] != "chat" and not (final and _same(r["i"], final))):
            print(f"  {r['i']:22s} got {str(final):22s} ({'fast' if r['fast'] else 'semantic'}) | {r['t']}")
    if args.report_md:
        Path(args.report_md).write_text(md, encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"metrics": m, "records": records}, indent=2), encoding="utf-8")
    if args.gate:
        fails = gate_failures(m)
        if not semantic_on:
            fails.append("the sentence model isn't available - the semantic layer wasn't measured")
        if fails:
            print("GATE FAILED: " + "; ".join(fails))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
