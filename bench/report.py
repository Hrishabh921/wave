"""
bench/report.py — Tier 4 item #12: renders one bench run as Markdown.
No logic lives here beyond formatting - everything it prints was already
computed by bench/metrics.py / bench/redteam_runner.py; this just lays it
out for a human (or a PR comment) to read.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_ms(value: float) -> str:
    return f"{value:.2f}ms"


def _delta_line(label: str, before: Optional[float], after: Optional[float], *, pct: bool = True,
                lower_is_better: bool = False) -> str:
    if before is None or after is None:
        return f"- {label}: {_fmt_pct(after) if pct else after} (no baseline to compare)"
    delta = after - before
    arrow = "→"
    good = (delta <= 0) if lower_is_better else (delta >= 0)
    marker = "✅" if good or abs(delta) < 1e-9 else "⚠️"
    fmt = _fmt_pct if pct else (lambda v: f"{v:.2f}")
    return f"- {label}: {fmt(before)} {arrow} {fmt(after)} ({'+' if delta >= 0 else ''}{fmt(delta) if pct else f'{delta:.2f}'}) {marker}"


def render_router_report(summary_by_split: Dict[str, Dict[str, Any]],
                         baseline_by_split: Optional[Dict[str, Dict[str, Any]]] = None,
                         *, title: str = "Wave intent_router bench report") -> str:
    lines: List[str] = [f"# {title}", "", f"Run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
    for split, summary in summary_by_split.items():
        lines.append(f"## {split}  (n={summary['n']})")
        baseline = (baseline_by_split or {}).get(split)
        lines.append(_delta_line("Accuracy", baseline and baseline.get("accuracy"), summary["accuracy"]))
        lines.append(_delta_line("Macro F1", baseline and baseline.get("macro_f1"), summary["macro_f1"]))
        lines.append(_delta_line("False-trigger rate", baseline and baseline.get("false_trigger_rate"),
                                 summary["false_trigger_rate"], lower_is_better=True))
        if summary.get("args_accuracy") is not None:
            lines.append(_delta_line("Argument accuracy", baseline and baseline.get("args_accuracy"),
                                     summary["args_accuracy"]))
        lat, base_lat = summary["latency"], (baseline or {}).get("latency") or {}
        lines.append(_delta_line("Latency p50", base_lat.get("p50"), lat["p50"], pct=False, lower_is_better=True))
        lines.append(_delta_line("Latency p95", base_lat.get("p95"), lat["p95"], pct=False, lower_is_better=True))
        lines.append(_delta_line("Composite score", baseline and baseline.get("composite"), summary["composite"]))
        if summary["top_confusions"]:
            lines.append("")
            lines.append("Top confusions (expected → predicted, count):")
            for exp, pred, n in summary["top_confusions"]:
                lines.append(f"  - `{exp}` → `{pred}`: {n}")
        lines.append("")
    return "\n".join(lines)


def render_redteam_report(scanner: Dict[str, Any], gate: Dict[str, Any],
                          *, title: str = "Wave redteam report") -> str:
    lines: List[str] = [f"# {title}", "",
                        f"Run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}", "",
                        "## content_trust.py scanner",
                        f"- Attacks checked: {scanner['n_attacks']}  |  Benign checked: {scanner['n_benign']}",
                        f"- Recall (attacks correctly flagged): {_fmt_pct(scanner['recall'])}",
                        f"- False-positive rate (benign incorrectly flagged): {_fmt_pct(scanner['false_positive_rate'])}",
                        ""]
    if scanner["missed_attacks"]:
        lines.append("Missed attacks:")
        lines.extend(f"  - {t!r}" for t in scanner["missed_attacks"])
        lines.append("")
    if scanner["false_positives"]:
        lines.append("False positives:")
        lines.extend(f"  - {t!r}" for t in scanner["false_positives"])
        lines.append("")
    lines.append("## Confirm-gate invariant")
    lines.append(f"- Checked {gate['checked']} tool(s) expected to require confirmation.")
    if gate["ok"]:
        lines.append("- ✅ All still gated.")
    else:
        lines.append("- ⚠️ FAILURES:")
        lines.extend(f"  - {f}" for f in gate["failures"])
    return "\n".join(lines)
