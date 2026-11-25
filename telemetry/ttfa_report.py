"""
telemetry/ttfa_report.py — Tier 11: read the time-to-first-audio log
=====================================================================

    python -m telemetry.ttfa_report                 # table of everything logged
    python -m telemetry.ttfa_report --since 2026-11-05T10:00
    python -m telemetry.ttfa_report --markdown      # for docs/TTFA_RESULTS.md
    python -m telemetry.ttfa_report --protocol      # the 20 utterances to say

Reads <data dir>/database/ttfa.jsonl (and its rotated .1), which
telemetry/ttfa.py appends to on every voice turn. Groups turns by the path
the brain took (fast path / planner / LLM / ...) and by language, prints
p50/p90 TTFA, and checks the Tier 11 targets:

    fast-path commands   p50 <= 0.8 s
    LLM answers (cloud)  p50 <= 2.0 s

The protocol (docs/TTFA_PROTOCOL.md) is SARA's own 20-utterance one -
English, Hinglish and Hindi across the regex/fast-path, planner, LLM and
interrupt paths - so the numbers can be compared like for like. SARA wrote
that protocol and never filled its table in.
"""
import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional

from telemetry.ttfa import default_log_path, percentile

TARGETS_MS = {"fast path": 800.0, "LLM": 2000.0}

_ROUTE_PATHS = {
    "intent_router": "fast path",
    "semantic_intent": "fast path",
    "pending_confirmation": "fast path",
    "safety_refusal": "fast path",
    "planner": "planner",
    "tool_loop": "LLM",
    "image": "vision",
    "no_provider_fallback": "offline fallback",
    "exception_fallback": "fallback",
}

_HINGLISH_MARKERS = {
    "kya", "hai", "hain", "karo", "kar", "kardo", "kholo", "band", "chalao", "batao", "mujhe", "mera",
    "meri", "abhi", "kal", "aaj", "kitna", "kitne", "baje", "yaar", "bhai", "nahi", "nahin", "haan", "theek",
    "acha", "achha", "chalo", "ruk", "ruko", "jao", "lagao", "dikhao", "sunao", "bajao", "badhao", "kam",
    "zyada", "wala", "wali", "mein", "aur", "bhi", "kaisa", "kaise", "kahan", "kyun", "lo", "le", "de",
}

# SARA's TTFA protocol (its bench/results/voice_baseline.md), as Wave runs it.
PROTOCOL = [
    ("en", "fast path", "volume up"),
    ("en", "fast path", "take a screenshot"),
    ("en", "fast path", "open notepad"),
    ("en", "fast path", "pause the music"),
    ("hinglish", "fast path", "awaaz badhao"),
    ("hinglish", "fast path", "chrome kholo"),
    ("hi", "fast path", "आवाज़ कम करो"),
    ("hi", "fast path", "स्क्रीनशॉट लो"),
    ("en", "planner", "check my battery and then remind me to charge it in ten minutes"),
    ("en", "planner", "open notepad then take a screenshot"),
    ("hinglish", "planner", "pehle volume kam karo phir youtube pe lofi chalao"),
    ("en", "LLM", "what's a good name for a black cat"),
    ("en", "LLM", "explain what a black hole is in two sentences"),
    ("en", "LLM", "what should I cook tonight with eggs and rice"),
    ("hinglish", "LLM", "aaj kya karna chahiye, kuch idea do"),
    ("hinglish", "LLM", "mujhe ek chhoti si kahani sunao"),
    ("hi", "LLM", "मुझे एक चुटकुला सुनाओ"),
    ("en", "interrupt", "(while Wave is answering) stop"),
    ("hinglish", "interrupt", "(while Wave is answering) ruk jao"),
    ("en", "interrupt", "(while Wave is answering) new question: what time is it"),
]


def guess_language(text: str) -> str:
    text = text or ""
    if re.search(r"[ऀ-ॿ]", text):
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower()))
    return "hinglish" if words & _HINGLISH_MARKERS else "en"


def row_path(row: Dict) -> str:
    if row.get("source") == "stop":
        return "interrupt"
    if row.get("source") == "wake":
        return "wake ack"
    return _ROUTE_PATHS.get(row.get("route") or "", row.get("route") or "unknown")


def load_rows(path: Optional[str] = None, since: Optional[str] = None) -> List[Dict]:
    path = path or default_log_path()
    rows = []
    for p in (path + ".1", path):
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if since and str(row.get("at", "")) < since:
                    continue
                rows.append(row)
    return rows


def summarize(rows: Iterable[Dict], sources=("voice", "stop", "wake")) -> List[Dict]:
    """One line per path: counts and p50/p90 of the relevant latency
    (time to first audio; for interrupts, time until the audio stopped)."""
    groups: Dict[str, List[Dict]] = {}
    for row in rows:
        if row.get("source") not in sources:
            continue
        groups.setdefault(row_path(row), []).append(row)
    out = []
    for path in sorted(groups, key=lambda p: (p not in TARGETS_MS, p)):
        g = groups[path]
        key = "stop_ms" if path == "interrupt" else "ttfa_ms"
        vals = [r.get(key) for r in g if r.get(key) is not None]
        out.append({
            "path": path, "n": len(g), "measured": len(vals),
            "p50_ms": percentile(vals, 50), "p90_ms": percentile(vals, 90),
            "p50_first_token_ms": percentile([r.get("first_token_ms") for r in g], 50),
            "p50_stt_ms": percentile([r.get("stt_ms") for r in g], 50),
            "languages": sorted({guess_language(r.get("heard", "")) for r in g}),
            "target_ms": TARGETS_MS.get(path),
        })
    return out


def by_language(rows: Iterable[Dict]) -> List[Dict]:
    groups: Dict[str, List[float]] = {}
    for row in rows:
        if row.get("source") != "voice" or row.get("ttfa_ms") is None:
            continue
        groups.setdefault(guess_language(row.get("heard", "")), []).append(row["ttfa_ms"])
    return [{"language": lang, "n": len(v), "p50_ms": percentile(v, 50), "p90_ms": percentile(v, 90)}
            for lang, v in sorted(groups.items())]


def _fmt(ms) -> str:
    return "—" if ms is None else f"{ms / 1000.0:.2f} s"


def render(rows: List[Dict], markdown: bool = False) -> str:
    summary = summarize(rows)
    langs = by_language(rows)
    lines = []
    if not summary:
        return ("No voice turns logged yet. Talk to Wave with the mic on (TTFA_ENABLED=true), then run "
                "this again. Log: " + default_log_path())
    if markdown:
        lines += ["| Path | Turns | p50 | p90 | p50 first text | p50 STT | Target | Met |",
                  "|---|---|---|---|---|---|---|---|"]
        for s in summary:
            met = "" if s["target_ms"] is None or s["p50_ms"] is None else ("✅" if s["p50_ms"] <= s["target_ms"] else "❌")
            lines.append(f"| {s['path']} | {s['n']} | {_fmt(s['p50_ms'])} | {_fmt(s['p90_ms'])} | "
                         f"{_fmt(s['p50_first_token_ms'])} | {_fmt(s['p50_stt_ms'])} | "
                         f"{_fmt(s['target_ms']) if s['target_ms'] else ''} | {met} |")
        lines += ["", "| Language | Turns | p50 | p90 |", "|---|---|---|---|"]
        for l in langs:
            lines.append(f"| {l['language']} | {l['n']} | {_fmt(l['p50_ms'])} | {_fmt(l['p90_ms'])} |")
        return "\n".join(lines)
    lines.append(f"{'path':<18}{'turns':>6}{'p50':>9}{'p90':>9}{'1st text':>10}{'STT':>9}   target")
    for s in summary:
        target = ""
        if s["target_ms"] is not None and s["p50_ms"] is not None:
            target = f"p50 <= {_fmt(s['target_ms'])}  {'MET' if s['p50_ms'] <= s['target_ms'] else 'NOT MET'}"
        lines.append(f"{s['path']:<18}{s['n']:>6}{_fmt(s['p50_ms']):>9}{_fmt(s['p90_ms']):>9}"
                     f"{_fmt(s['p50_first_token_ms']):>10}{_fmt(s['p50_stt_ms']):>9}   {target}")
    if langs:
        lines.append("")
        lines.append("by language: " + ", ".join(f"{l['language']} {_fmt(l['p50_ms'])} (n={l['n']})" for l in langs))
    lines.append("")
    lines.append("TTFA = from the end of your speech to Wave's first audio. Interrupt = until Wave went quiet.")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Time-to-first-audio report (Tier 11)")
    ap.add_argument("--log", default=None, help=f"default: {default_log_path()}")
    ap.add_argument("--since", default=None, help="ISO time; only turns at or after it")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--protocol", action="store_true", help="print the 20 utterances to say, then exit")
    args = ap.parse_args(argv)
    if args.protocol:
        print("Say each line to Wave (mic on, Wave awake), one at a time, waiting for the answer:\n")
        for i, (lang, path, text) in enumerate(PROTOCOL, 1):
            print(f"{i:>2}. [{lang:<8} {path:<9}] {text}")
        print("\nThen: python -m telemetry.ttfa_report --since <the time you started>")
        return 0
    print(render(load_rows(args.log, args.since), markdown=args.markdown))
    return 0


if __name__ == "__main__":
    sys.exit(main())
