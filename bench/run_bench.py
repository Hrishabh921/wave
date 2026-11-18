"""
bench/run_bench.py — Tier 4 item #12: the CLI entry point.

    python -m bench.run_bench                          # router mode, print a summary
    python -m bench.run_bench --mode redteam            # scanner + gate-invariant only
    python -m bench.run_bench --mode all --compare      # both, fail (exit 1) on regression
    python -m bench.run_bench --accept-baseline         # write current results as the new baseline
    python -m bench.run_bench --report-md bench/results/RESULTS.md --json bench/results/latest.json

Exit codes (for CI to gate on): 0 = no regression / nothing to compare against,
1 = a router-mode threshold was exceeded, 2 = a redteam threshold or the
confirm-gate invariant failed. --compare checks both halves when --mode all;
either one failing is enough for a non-zero exit.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BENCH_DIR = Path(__file__).resolve().parent
ROOT = BENCH_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench import dataset, metrics, report                      # noqa: E402
from bench import redteam_runner                                 # noqa: E402
from bench.runner import run_router                              # noqa: E402

DEFAULT_BASELINE = BENCH_DIR / "baseline.json"
DEFAULT_THRESHOLDS = BENCH_DIR / "thresholds.json"
DEFAULT_REDTEAM_DIR = BENCH_DIR / "redteam"


def _load_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _split_records(records: List[metrics.Record]) -> Dict[str, List[metrics.Record]]:
    splits: Dict[str, List[metrics.Record]] = {"router/all": records}
    for lang in sorted({r["lang"] for r in records}):
        splits[f"router/{lang}"] = [r for r in records if r["lang"] == lang]
    return splits


def run_router_mode(files: Optional[List[str]], latency_budget_ms: float) -> Dict[str, Dict[str, Any]]:
    rows = dataset.load_golden(files)
    records = run_router(rows)
    splits = _split_records(records)
    return {name: metrics.summarize(recs, latency_budget_ms=latency_budget_ms) for name, recs in splits.items()}


def run_redteam_mode(redteam_dir: Path) -> Dict[str, Any]:
    attacks = dataset.load_jsonl(redteam_dir / "attacks.jsonl")
    benign = dataset.load_jsonl(redteam_dir / "benign.jsonl")
    outcomes = redteam_runner.run_scanner(attacks + benign)
    return {
        "scanner": redteam_runner.scanner_metrics(outcomes),
        "gate": redteam_runner.check_gate_invariant(),
    }


def check_router_regressions(current: Dict[str, Dict[str, Any]], baseline: Optional[Dict[str, Dict[str, Any]]],
                             thresholds: Dict[str, Any]) -> List[str]:
    if baseline is None:
        return []
    failures: List[str] = []
    for split, summary in current.items():
        base = baseline.get(split)
        if base is None:
            continue
        if base["accuracy"] - summary["accuracy"] > thresholds["max_accuracy_drop"]:
            failures.append(f"{split}: accuracy dropped {base['accuracy']:.4f} -> {summary['accuracy']:.4f}")
        if base["macro_f1"] - summary["macro_f1"] > thresholds["max_macro_f1_drop"]:
            failures.append(f"{split}: macro_f1 dropped {base['macro_f1']:.4f} -> {summary['macro_f1']:.4f}")
        b_ftr, c_ftr = base.get("false_trigger_rate"), summary.get("false_trigger_rate")
        if b_ftr is not None and c_ftr is not None and (c_ftr - b_ftr) > thresholds["max_false_trigger_rate_increase"]:
            failures.append(f"{split}: false_trigger_rate rose {b_ftr:.4f} -> {c_ftr:.4f}")
        if c_ftr is not None and c_ftr > thresholds["max_false_trigger_rate_absolute"]:
            failures.append(f"{split}: false_trigger_rate {c_ftr:.4f} exceeds the absolute cap "
                            f"{thresholds['max_false_trigger_rate_absolute']:.4f}")
        if base["composite"] - summary["composite"] > thresholds["max_composite_drop"]:
            failures.append(f"{split}: composite score dropped {base['composite']:.4f} -> {summary['composite']:.4f}")
        p95_delta = summary["latency"]["p95"] - base["latency"]["p95"]
        if p95_delta > thresholds["max_p95_latency_increase_ms"]:
            failures.append(f"{split}: p95 latency rose {base['latency']['p95']:.3f}ms -> "
                            f"{summary['latency']['p95']:.3f}ms")
    return failures


def check_redteam_regressions(current: Dict[str, Any], thresholds: Dict[str, Any]) -> List[str]:
    failures: List[str] = []
    rt = thresholds.get("redteam", {})
    recall = current["scanner"]["recall"]
    fpr = current["scanner"]["false_positive_rate"]
    if recall is not None and recall < rt.get("min_recall", 0.0):
        failures.append(f"redteam scanner recall {recall:.4f} below minimum {rt.get('min_recall')}")
    if fpr is not None and fpr > rt.get("max_false_positive_rate", 1.0):
        failures.append(f"redteam scanner false-positive-rate {fpr:.4f} above maximum {rt.get('max_false_positive_rate')}")
    if not current["gate"]["ok"]:
        failures.extend(f"confirm-gate invariant: {f}" for f in current["gate"]["failures"])
    return failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.run_bench")
    parser.add_argument("--mode", choices=["router", "redteam", "all"], default="all")
    parser.add_argument("--files", default=None, help="comma-separated golden filenames (default: all of them)")
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS))
    parser.add_argument("--redteam-dir", default=str(DEFAULT_REDTEAM_DIR))
    parser.add_argument("--compare", action="store_true", help="exit non-zero on a regression vs. baseline")
    parser.add_argument("--accept-baseline", action="store_true", help="write this run's results as the new baseline")
    parser.add_argument("--report-md", default=None, help="write a Markdown report to this path")
    parser.add_argument("--json", dest="json_out", default=None, help="write raw results as JSON to this path")
    parser.add_argument("--quiet", action="store_true", help="suppress the printed summary")
    args = parser.parse_args(argv)

    thresholds = _load_json(Path(args.thresholds)) or {}
    baseline = _load_json(Path(args.baseline)) or {}
    files = args.files.split(",") if args.files else None

    result: Dict[str, Any] = {}
    failures: List[str] = []

    if args.mode in ("router", "all"):
        router_summary = run_router_mode(files, thresholds.get("p95_latency_budget_ms", 2.0))
        result["router"] = router_summary
        if args.compare:
            failures.extend(check_router_regressions(router_summary, baseline.get("router"), thresholds))

    if args.mode in ("redteam", "all"):
        redteam_summary = run_redteam_mode(Path(args.redteam_dir))
        result["redteam"] = redteam_summary
        if args.compare:
            failures.extend(check_redteam_regressions(redteam_summary, thresholds))

    if not args.quiet:
        if "router" in result:
            for split, summary in result["router"].items():
                print(f"[router] {split}: n={summary['n']} acc={summary['accuracy']:.4f} "
                     f"f1={summary['macro_f1']:.4f} ftr={summary['false_trigger_rate']} "
                     f"p95={summary['latency']['p95']:.3f}ms composite={summary['composite']:.4f}")
        if "redteam" in result:
            s = result["redteam"]["scanner"]
            g = result["redteam"]["gate"]
            print(f"[redteam] recall={s['recall']} fpr={s['false_positive_rate']} "
                 f"gate_ok={g['ok']} ({g['checked']} tool(s) checked)")
        if failures:
            print(f"\n{len(failures)} regression(s) vs. baseline:")
            for f in failures:
                print(f"  - {f}")
        elif args.compare:
            print("\nNo regressions vs. baseline.")

    if args.report_md:
        md_parts = []
        if "router" in result:
            md_parts.append(report.render_router_report(result["router"], baseline.get("router")))
        if "redteam" in result:
            md_parts.append(report.render_redteam_report(result["redteam"]["scanner"], result["redteam"]["gate"]))
        Path(args.report_md).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_md).write_text("\n\n---\n\n".join(md_parts), encoding="utf-8")

    if args.json_out:
        _write_json(Path(args.json_out), result)

    if args.accept_baseline:
        _write_json(Path(args.baseline), result)
        if not args.quiet:
            print(f"\nBaseline written to {args.baseline}")

    if failures:
        return 2 if (args.mode == "redteam" or any("redteam" in f or "confirm-gate" in f for f in failures)) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
