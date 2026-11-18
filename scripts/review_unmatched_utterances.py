"""
scripts/review_unmatched_utterances.py — Tier 17: turns brain/unmatched_log.py's
raw JSONL into a human-reviewable summary, so a real, repeated miss can become
a new brain/intent_router.py rule.

WHY THIS EXISTS
---------------
Every turn neither followup_resolver nor intent_router recognized gets one
{ts, text} row appended to the unmatched-utterance log (see
brain/unmatched_log.py's own docstring for exactly where and why). Most of
those rows are ordinary conversation that was never going to be a fast-path
command - reading them one at a time would be almost all noise. What turns
the log into a signal is REPETITION: the same phrasing (or a close variant of
it) showing up again and again is exactly what should become a new
intent_router rule in a later tier - a one-off can't show that pattern yet.

So this script groups rows by a crude, display-only normalization (lowercase,
strip punctuation, collapse whitespace - NOT brain/content_trust.py's security
-focused normalization, which solves a different problem: evading a scanner,
not finding repeated phrasing), counts repeats, and prints the most-repeated
phrasings first. --min-count (default 2) hides one-off rows entirely.

For each printed example, the script also tries to say what (if anything) the
LLM went on to do right after that turn, by looking for the automation/audit.py
row closest after it within a short window (default 30s). This is a HINT, not
a guarantee - a miss followed closely by an unrelated action (the user asking
two things back to back) will show a wrong-looking correlation, and a miss
with no nearby action at all (ordinary chat, or a reply that needed no tool)
correctly shows "(no action followed)". Reading the two side by side is still
useful: it's the fastest way to notice "that phrasing SHOULD have mapped to
open_app" versus "that one was just chat, as expected" without re-running
Wave. Correlation is best-effort and optional - if the audit log can't be
read (a fresh checkout with nothing run yet, or a database problem), the
script still prints the grouped-and-counted summary, just without that hint
(see load_audit_records()'s own docstring).

USAGE (from the project folder)
    python scripts/review_unmatched_utterances.py
    python scripts/review_unmatched_utterances.py --min-count 3 --top 10
    python scripts/review_unmatched_utterances.py --path /some/other/log.jsonl
    python scripts/review_unmatched_utterances.py --no-audit

This never writes to the log or the audit database - read-only, same spirit
as followup_resolver's own "read-only, zero changes to the hot dispatch
path" design that brain/unmatched_log.py's own docstring points back to.
"""
import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize_for_grouping(text: str) -> str:
    """Crude, display-only grouping key - NOT content_trust.py's security
    normalization. Lowercases, drops punctuation, and collapses whitespace,
    so "Close it!" and "close it" land in the same group - but "close it"
    and "close that" correctly don't, since finding a REPEATED exact shape
    is the point (a looser match would blur distinct candidate rules
    together)."""
    text = _PUNCT_RE.sub("", text.lower())
    return _SPACE_RE.sub(" ", text).strip()


def load_rows(path: str) -> Tuple[List[dict], int]:
    """Returns (rows, skipped_count). A malformed trailing line (e.g. Wave
    was killed mid-write) is skipped, not fatal - best-effort, matching
    log_unmatched()'s own philosophy. A missing file (nothing logged yet,
    or UNMATCHED_LOG_ENABLED=false) is not an error either: just no rows."""
    rows: List[dict] = []
    skipped = 0
    if not os.path.exists(path):
        return rows, skipped
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if isinstance(row, dict) and row.get("text") and row.get("ts"):
                rows.append(row)
            else:
                skipped += 1
    return rows, skipped


def group_rows(rows: List[dict]) -> Dict[str, List[dict]]:
    """normalized text -> its rows, oldest to newest (the log is append-only,
    so file order already is chronological order)."""
    groups: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        groups[normalize_for_grouping(row["text"])].append(row)
    return groups


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def load_audit_records(limit: int = 1000) -> list:
    """Best-effort: the live automation.audit.audit singleton's recent rows.
    Returns [] (never raises) when the audit log can't be read at all - a
    fresh checkout with nothing run yet, or a database problem unrelated to
    this script. Both automation.audit and brain.unmatched_log are cheap,
    dependency-light imports (see their own modules) - this still goes
    through try/except because the thing that can actually fail here is
    *reading the sqlite file* inside audit.recent(), not importing the
    module that wraps it."""
    try:
        from automation.audit import audit
        return audit.recent(limit=limit)  # newest -> oldest; order doesn't matter below
    except Exception:
        return []


try:
    from automation.audit import summarize as _audit_summarize
except Exception:  # pragma: no cover - automation.audit should always import; belt-and-suspenders only
    def _audit_summarize(tool, args):
        return f"{tool}({args})"


def find_correlated_action(ts: str, audit_records: list, window_s: float = 30.0):
    """The audit record whose own timestamp is the closest one AFTER `ts`,
    within window_s seconds - or None. "After", not "nearest either
    direction": an action that happened BEFORE the unmatched turn can't be
    what that turn caused."""
    base = _parse_ts(ts)
    if base is None:
        return None
    best, best_delta = None, None
    for rec in audit_records:
        rec_ts = _parse_ts(rec.ts)
        if rec_ts is None or rec_ts < base:
            continue
        delta = (rec_ts - base).total_seconds()
        if delta > window_s:
            continue
        if best_delta is None or delta < best_delta:
            best, best_delta = rec, delta
    return best


def build_report(rows: List[dict], skipped: int, audit_records: list,
                  top: int = 20, min_count: int = 2, window_s: float = 30.0) -> str:
    lines = []
    header = f"{len(rows)} unmatched utterance(s) logged"
    if skipped:
        header += f" ({skipped} malformed line(s) skipped)"
    lines.append(header + ".")
    if not rows:
        lines.append("Nothing to review yet.")
        return "\n".join(lines)

    groups = group_rows(rows)
    repeated = {k: v for k, v in groups.items() if len(v) >= min_count}
    lines.append(f"{len(groups)} distinct phrasing(s); {len(repeated)} repeated "
                 f"{min_count}+ times (shown below, most-repeated first).")
    if not repeated:
        lines.append(f"\nNo phrasing repeated {min_count}+ times yet - nothing here looks like a "
                      "pattern worth a new intent_router rule yet.")
        return "\n".join(lines)

    ranked = sorted(repeated.items(), key=lambda kv: len(kv[1]), reverse=True)[:top]
    for normalized, group in ranked:
        lines.append(f"\n[{len(group)}x] {normalized!r}")
        for row in group[-3:]:  # most recent few, shown with their original (un-normalized) text
            if audit_records:
                correlated = find_correlated_action(row["ts"], audit_records, window_s)
                hint = (f" -> then {_audit_summarize(correlated.tool, correlated.args)} "
                        f"[{correlated.status}]") if correlated else " -> (no action followed)"
            else:
                hint = ""
            lines.append(f"    {row['ts']}  {row['text']!r}{hint}")
    return "\n".join(lines)


def main(argv=None) -> int:
    import brain.unmatched_log as ul  # deferred: only needed for its default log_path()

    parser = argparse.ArgumentParser(
        description="Summarize brain/unmatched_log.py's raw log into repeated phrasings worth "
                    "turning into new intent_router rules.")
    parser.add_argument("--path", default=None,
                        help="log file to read (default: whatever brain.unmatched_log.log_path() "
                             "resolves to - the live UNMATCHED_LOG_PATH override, or "
                             "data/unmatched_utterances.jsonl)")
    parser.add_argument("--top", type=int, default=20, help="show at most this many groups (default 20)")
    parser.add_argument("--min-count", type=int, default=2,
                        help="hide phrasings repeated fewer than this many times (default 2)")
    parser.add_argument("--window-s", type=float, default=30.0,
                        help="seconds after a miss to look for a correlated audit action (default 30)")
    parser.add_argument("--no-audit", action="store_true",
                        help="skip audit-log correlation even if it's available")
    args = parser.parse_args(argv)

    path = args.path or ul.log_path()
    rows, skipped = load_rows(path)
    audit_records = [] if args.no_audit else load_audit_records()

    print(f"Reading {path}")
    if not audit_records and not args.no_audit:
        print("(audit-log correlation unavailable - showing the raw summary only)")
    print()
    print(build_report(rows, skipped, audit_records, top=args.top, min_count=args.min_count,
                       window_s=args.window_s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
