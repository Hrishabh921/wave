"""
bench/metrics.py — Tier 4 item #12: pure metric functions over a list of
run records. No I/O, no Wave/brain imports - every function here takes
plain dicts and returns plain numbers/dicts, so this file (and every
function in it) is trivially unit-testable on its own, same as SARA's
bench/metrics.py it's modelled on.

A `Record` (just a dict, not a class, so callers never need to import a
type to build one) looks like:
    {"pred": "volume_up", "expected": "volume_up", "lang": "en",
     "latency_ms": 0.12, "args_ok": True}
`pred`/`expected` are either a real skill name or the literal string
"chat" (meaning: no tool should fire - see bench/dataset.py). `args_ok` is
None when the row had no expected_args to check (see bench/runner.py),
True/False otherwise.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

CHAT = "chat"

Record = Dict[str, Any]


def accuracy(records: List[Record]) -> float:
    if not records:
        return 0.0
    correct = sum(1 for r in records if r["pred"] == r["expected"])
    return correct / len(records)


def confusion_pairs(records: List[Record], limit: int = 20) -> List[Tuple[str, str, int]]:
    """The most common (expected, pred) MISMATCH pairs, most frequent
    first - a quick "what's actually going wrong" view, not a full
    matrix (which gets unreadable past a handful of labels)."""
    counts: Dict[Tuple[str, str], int] = {}
    for r in records:
        if r["pred"] != r["expected"]:
            key = (r["expected"], r["pred"])
            counts[key] = counts.get(key, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(exp, pred, n) for (exp, pred), n in ordered[:limit]]


def per_intent_prf(records: List[Record]) -> Dict[str, Dict[str, float]]:
    """Precision/recall/F1 for every label that appears as either an
    expected or predicted value anywhere in `records` (this naturally
    includes "chat" as just another label - its own precision/recall are
    meaningful too: recall is "of the genuinely no-match-expected rows,
    how many correctly got no match", which is the same thing
    false_trigger_rate() reports as a single number from the other
    direction)."""
    labels = {r["expected"] for r in records} | {r["pred"] for r in records}
    out: Dict[str, Dict[str, float]] = {}
    for label in sorted(labels):
        tp = sum(1 for r in records if r["pred"] == label and r["expected"] == label)
        fp = sum(1 for r in records if r["pred"] == label and r["expected"] != label)
        fn = sum(1 for r in records if r["expected"] == label and r["pred"] != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        out[label] = {"precision": round(precision, 4), "recall": round(recall, 4),
                      "f1": round(f1, 4), "support": tp + fn}
    return out


def macro_f1(records: List[Record]) -> float:
    prf = per_intent_prf(records)
    if not prf:
        return 0.0
    return round(sum(v["f1"] for v in prf.values()) / len(prf), 4)


def false_trigger_rate(records: List[Record]) -> Optional[float]:
    """Among rows that expected NO tool to fire (expected == "chat" -
    negatives.jsonl and most of adversarial.jsonl), the fraction where one
    fired anyway. None (not 0.0) when there are no such rows at all, so a
    caller never mistakes "nothing to measure" for "a perfect score"."""
    chat_rows = [r for r in records if r["expected"] == CHAT]
    if not chat_rows:
        return None
    false_positives = sum(1 for r in chat_rows if r["pred"] != CHAT)
    return round(false_positives / len(chat_rows), 4)


def args_accuracy(records: List[Record]) -> Optional[float]:
    """Among rows that had an expected_args dict to check (see
    bench/runner.py - only set where argument-extraction correctness
    matters, e.g. open_app's app_name), the fraction where the router's
    extracted args matched exactly. None when no row carried an
    expectation to check."""
    checked = [r for r in records if r.get("args_ok") is not None]
    if not checked:
        return None
    return round(sum(1 for r in checked if r["args_ok"]) / len(checked), 4)


def _percentile(sorted_values: List[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def latency_stats(records: List[Record]) -> Dict[str, float]:
    values = sorted(r["latency_ms"] for r in records if r.get("latency_ms") is not None)
    if not values:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "mean": 0.0, "max": 0.0, "n": 0}
    return {
        "p50": round(_percentile(values, 50), 4), "p95": round(_percentile(values, 95), 4),
        "p99": round(_percentile(values, 99), 4), "mean": round(sum(values) / len(values), 4),
        "max": round(values[-1], 4), "n": len(values),
    }


def latency_score(p95_ms: float, budget_ms: float) -> float:
    """1.0 at or under budget, decaying linearly to 0.0 at 2x budget, and
    clamped to 0.0 past that - a smooth penalty rather than a hard
    pass/fail, so a small latency regression nudges the composite score
    down instead of a threshold cliff hiding exactly how close it is."""
    if budget_ms <= 0:
        return 1.0 if p95_ms <= 0 else 0.0
    if p95_ms <= budget_ms:
        return 1.0
    return round(max(0.0, 1.0 - (p95_ms - budget_ms) / budget_ms), 4)


def composite_score(acc: float, f1: float, ftr: Optional[float], lat_score: float,
                    weights: Optional[Dict[str, float]] = None) -> float:
    """One headline number blending accuracy, macro-F1, false-trigger
    safety, and latency - what run_bench.py's --compare threshold checks
    primarily watch for a regression. ftr=None (nothing to measure, see
    false_trigger_rate()) is treated as a perfect 1.0 contribution rather
    than dropping the term, so a dataset split with no negative rows at
    all doesn't silently shrink the weights of the other three."""
    w = weights or {"accuracy": 0.35, "f1": 0.25, "safety": 0.25, "latency": 0.15}
    safety = 1.0 if ftr is None else (1.0 - ftr)
    score = (w["accuracy"] * acc + w["f1"] * f1 + w["safety"] * safety + w["latency"] * lat_score)
    return round(score, 4)


def summarize(records: List[Record], *, latency_budget_ms: float) -> Dict[str, Any]:
    """Everything run_bench.py needs for one split (e.g. "router/en" or
    "router/all"), in one call."""
    lat = latency_stats(records)
    ftr = false_trigger_rate(records)
    acc = accuracy(records)
    f1 = macro_f1(records)
    lat_sc = latency_score(lat["p95"], latency_budget_ms)
    return {
        "n": len(records),
        "accuracy": round(acc, 4),
        "macro_f1": f1,
        "false_trigger_rate": ftr,
        "args_accuracy": args_accuracy(records),
        "latency": lat,
        "latency_score": lat_sc,
        "composite": composite_score(acc, f1, ftr, lat_sc),
        "top_confusions": confusion_pairs(records, limit=10),
    }
