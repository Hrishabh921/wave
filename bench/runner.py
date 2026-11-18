"""
bench/runner.py — Tier 4 item #12: runs golden rows through the REAL
brain/intent_router.py (never a handler, never the LLM, never a mock).
================================================================================
"router mode" (the only mode this harness runs, same scope as SARA's own
bench, which also only benchmarks its deterministic router layer rather
than the full LLM pipeline): intent_router.route() is pure, synchronous,
offline, and has zero dependency on GROQ_API_KEY/PyQt6/network, so every
row runs in microseconds and the whole golden set finishes in well under a
second - fast enough to run on every CI build, which is the entire point
of a regression harness (a benchmark too slow to run on every change
doesn't catch anything until long after the change that broke it).
Benchmarking the full LLM tool-calling loop instead would need live API
calls (slow, costs money, non-deterministic) - out of scope for what this
module does, same as it was for SARA's own bench/runner.py.
"""
from __future__ import annotations

import time
from typing import Dict, List

from bench.dataset import Row
from bench.metrics import CHAT, Record


def _args_match(expected: Dict, actual: Dict) -> bool:
    return dict(expected or {}) == dict(actual or {})


def run_router(rows: List[Row]) -> List[Record]:
    """One Record per Row - see bench/metrics.py for the shape. Imports
    brain.intent_router lazily (not at module top) so `python -m
    bench.run_bench --help` and similar stay fast/dependency-light even
    before Wave's own settings/config.py (which this transitively pulls
    in) has anything it needs configured."""
    from brain import intent_router

    records: List[Record] = []
    for row in rows:
        t0 = time.perf_counter()
        routed = intent_router.route(row.text)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        pred = routed.skill if routed is not None else CHAT
        args_ok = None
        if row.expected_args is not None:
            args_ok = routed is not None and _args_match(row.expected_args, routed.args)

        records.append({
            "pred": pred, "expected": row.intent, "lang": row.lang, "style": row.style,
            "latency_ms": latency_ms, "args_ok": args_ok, "rule": routed.rule if routed else None,
            "text": row.text, "note": row.note, "source": f"{row.source_file}:{row.line_no}",
        })
    return records
