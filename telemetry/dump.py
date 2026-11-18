"""
telemetry/dump.py — Tier 4 item #13: a small CLI to inspect recent turns.

    python -m telemetry.dump                 # last 20 turns, one line each
    python -m telemetry.dump --limit 5 -v     # last 5, with their stage marks
    python -m telemetry.dump --turn-id a1b2c3 # one specific turn, full detail
    python -m telemetry.dump --summary        # outcome/route counts only

Reads database/db_manager.py's turn_trace table directly (durable, cross-
restart history) rather than telemetry/trace.py's in-memory ring, which
only covers the current process's last 50 turns - this is the tool for
"what happened yesterday", that one is for "what just happened".
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rows(limit: int) -> List[Dict[str, Any]]:
    from database.db_manager import db
    return db.get_recent_traces(limit=limit)


def _fmt_ms(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def _one_line(row: Dict[str, Any]) -> str:
    return (f"{row.get('id', '?'):>5}  {row.get('started_at', '?'):<24} "
            f"{row.get('source', '?'):<6} {row.get('route') or '-':<18} "
            f"{row.get('outcome', '?'):<7} total={_fmt_ms(row.get('total_ms'))}  "
            f"llm={_fmt_ms(row.get('llm_ms'))}  tool={_fmt_ms(row.get('tool_ms'))}  "
            f"rounds={row.get('rounds') if row.get('rounds') is not None else '-'}  "
            f"tools={row.get('tools_called') if row.get('tools_called') is not None else '-'}")


def _detail(row: Dict[str, Any]) -> str:
    lines = [_one_line(row), f"       turn_id={row.get('turn_id')} model={row.get('model') or '-'} "
             f"provider={row.get('provider') or '-'}"]
    if row.get("error"):
        lines.append(f"       error: {row['error']}")
    try:
        stages = json.loads(row.get("stages") or "[]")
    except (TypeError, ValueError):
        stages = []
    for mark in stages:
        meta = mark.get("meta")
        suffix = f" {meta}" if meta else ""
        lines.append(f"         +{mark.get('t_ms', 0):>7.1f}ms  {mark.get('stage', '?')}{suffix}")
    return "\n".join(lines)


def _summary(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No trace rows found."
    outcomes = Counter(r.get("outcome") or "unknown" for r in rows)
    routes = Counter(r.get("route") or "unknown" for r in rows)
    totals = [r["total_ms"] for r in rows if isinstance(r.get("total_ms"), (int, float))]
    avg = sum(totals) / len(totals) if totals else 0.0
    lines = [f"{len(rows)} turn(s)  avg_total={avg:.0f}ms",
             "by outcome: " + ", ".join(f"{k}={v}" for k, v in outcomes.most_common()),
             "by route:   " + ", ".join(f"{k}={v}" for k, v in routes.most_common())]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="telemetry.dump")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--turn-id", default=None, help="show one turn's full stage marks")
    parser.add_argument("-v", "--verbose", action="store_true", help="show stage marks for every row")
    parser.add_argument("--summary", action="store_true", help="outcome/route counts only")
    args = parser.parse_args(argv)

    rows = _rows(max(args.limit, 500) if (args.turn_id or args.summary) else args.limit)
    if args.turn_id:
        match = next((r for r in rows if r.get("turn_id") == args.turn_id), None)
        if match is None:
            print(f"No trace row with turn_id={args.turn_id!r} in the last {len(rows)} rows.", file=sys.stderr)
            return 1
        print(_detail(match))
        return 0
    if args.summary:
        print(_summary(rows))
        return 0
    if not rows:
        print("No trace rows found.")
        return 0
    for row in rows[:args.limit]:
        print(_detail(row) if args.verbose else _one_line(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
