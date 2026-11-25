"""
bench/memory_bench.py — Tier 46: does Wave's memory bring the right thing?

Modelled on SARA's bench/memory (scenarios of things said over weeks, then
questions - recall@k, contradiction, temporal, multi-hop, abstention,
forgetting), run on Wave's own pipeline. Each scenario gets a fresh
temporary database holding 22 everyday background facts - so retrieval
works the way it does once a user has told Wave a lot, where only the most
relevant facts reach the prompt - then its facts (db.save_memory()), its
messages (into the chat history, with their time, and through the people
graph like a live turn) and its "forget" requests, in time order. Each
question is then judged on exactly what the model would be shown:
brain/memory_context.py's fact lines (the relevant facts, the people the
question names, what a changed fact was before) and
brain/episodic_recall.py's dated messages.

Pass rules:
  single / multihop / history / contradiction - an expected answer in the
      first 3 fact lines; a contradiction also fails if any fact line still
      gives a superseded value as the current one;
  temporal - an expected answer in the first 3 fact lines or in the dated
      messages;
  forget   - the forgotten value appears nowhere (facts, history, messages);
  abstain  - nothing Wave adds on its own (people, dated messages) points
      at the decoy.
"Before" is the same run with retrieval as it was before Tier 46 (facts
above 0.35 similarity, else the first ones alphabetically; no people graph,
no dated messages), on the same stored facts.

    python -m bench.memory_bench
    python -m bench.memory_bench --report-md bench/results/MEMORY.md --json bench/results/memory.json
    python -m bench.memory_bench --gate        # exit 1 below the gate (overall 90%, no leak, no false memory)

Needs the sentence model (all-MiniLM-L6-v2); without it, exit 4.
"""
import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from brain import episodic_recall, memory_context, memory_time, people_graph  # noqa: E402
from database import embeddings  # noqa: E402
from database.db_manager import db  # noqa: E402
from settings.config import config  # noqa: E402

SCENARIOS = ROOT / "bench" / "memory" / "scenarios.jsonl"
PASS_K = 3
RANKED = ("single", "contradiction", "history", "multihop")
TYPES = RANKED + ("temporal", "forget", "abstain")
GATE = {"overall": 0.90, "forget_leak": 0.0, "false_memory": 0.0}
EXIT_GATE, EXIT_NO_MODEL = 1, 4

BACKGROUND = [
    ("laptop_model", "Lenovo IdeaPad 5"), ("phone_model", "Pixel 8"), ("gym_days", "Monday, Wednesday and Friday"),
    ("wake_up_time", "6:30 am"), ("coffee_order", "cappuccino without sugar"), ("car", "a grey Honda City"),
    ("blood_group", "B positive"), ("shoe_size", "9"), ("favorite_cricketer", "Virat Kohli"),
    ("internet_provider", "Airtel fiber"), ("tea", "masala chai with less sugar"), ("music_app", "Spotify"),
    ("preferred_browser", "Chrome"), ("sleep_time", "around 11:30 pm"), ("diet", "vegetarian"),
    ("height", "5 ft 9 in"), ("favorite_season", "winter"), ("typing_speed", "60 words per minute"),
    ("allergy", "dust"), ("weekend_plan", "long walks"), ("favorite_sport", "badminton"),
    ("desk_setup", "two monitors"),
]


def load(path=SCENARIOS) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _has(text: str, needles) -> bool:
    low = str(text).casefold()
    return any(str(n).casefold() in low for n in needles)


def _current(line: str) -> str:
    return line.split(" (before:", 1)[0]


def judge(question: dict, fact_lines: list, dated_lines: list, added_lines: list):
    """(passed, rank of the first fact line with an expected answer or None)."""
    kind, expect, forbid = question["type"], question.get("expect", []), question.get("forbid", [])
    rank = next((i for i, line in enumerate(fact_lines, 1) if _has(line, expect)), None) if expect else None
    if kind in RANKED:
        ok = rank is not None and rank <= PASS_K
        if kind == "contradiction" and any(_has(_current(line), forbid) for line in fact_lines):
            ok = False
    elif kind == "temporal":
        ok = (rank is not None and rank <= PASS_K) or any(_has(line, expect) for line in dated_lines)
    elif kind == "forget":
        ok = not any(_has(line, forbid) for line in fact_lines + dated_lines)
    else:
        ok = not any(_has(line, forbid) for line in added_lines + dated_lines)
    return ok, rank


def _utc(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _save(key: str, value: str, ts: str, source: str) -> None:
    with db.get_connection() as conn:
        before = conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0]
    db.save_memory(key, value, source=source)
    with db.get_connection() as conn:
        k = key.strip().lower()
        conn.execute("UPDATE memory SET updated_at = ? WHERE key = ?", (ts, k))
        if conn.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] > before:
            conn.execute("UPDATE memory_history SET changed_at = ? WHERE id = (SELECT MAX(id) FROM memory_history)",
                         (ts,))
        conn.commit()


def _load_background() -> None:
    for key, value in BACKGROUND:
        _save(key, value, "2026-01-01T09:00:00", "explicit")


def _load_scenario(scenario: dict) -> None:
    events = ([(f["ts"], 0, "fact", f) for f in scenario.get("facts", [])]
              + [(m["ts"], 1, "message", m) for m in scenario.get("messages", [])]
              + [(f["ts"], 2, "forget", f) for f in scenario.get("forget", [])])
    for ts, _order, kind, item in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == "fact":
            said_on = datetime.fromisoformat(ts).date()
            _save(item["key"], memory_time.annotate(item["value"], today=said_on), ts, item.get("source", "explicit"))
        elif kind == "message":
            with db.get_connection() as conn:
                conn.execute("INSERT INTO chat_history (role, message, timestamp) VALUES ('user', ?, ?)",
                             (item["text"], _utc(ts)))
                conn.commit()
            # What a live turn learns from it (brain/people_graph.py) is stored
            # in both runs - only how it's found differs.
            for key, value in people_graph.relation_facts(item["text"]):
                _save(key, value, ts, "auto_consolidated")
        else:
            db.delete_memory(item["key"])


def _before_tier46(query: str, limit: int = 8) -> list:
    """db.get_relevant_memory() as it was before Tier 46: facts above 0.35
    similarity (plus one hop), else the first ones alphabetically."""
    all_facts = db.get_all_memory()
    if len(all_facts) <= limit:
        return all_facts
    hits = db.semantic_recall(query, top_k=limit)
    if hits:
        chosen = [(k, v) for k, v, _s in hits]
        return chosen + db._one_hop(chosen, all_facts)
    return all_facts[:limit]


def run(scenarios: list, tier46: bool = True) -> list:
    """One record per question."""
    saved = (db.db_path, db.history_enabled, config.MEMORY_PEOPLE_GRAPH, config.MEMORY_EPISODIC)
    config.MEMORY_PEOPLE_GRAPH = config.MEMORY_EPISODIC = tier46
    records = []
    # The same background facts are embedded for every scenario - once is enough.
    real_embed, cache = embeddings.embed, {}

    def cached_embed(text):
        if text not in cache:
            cache[text] = real_embed(text)
        return cache[text]

    embeddings.embed = cached_embed
    base = tempfile.mkdtemp(prefix="wave_membench_")
    try:
        # One database with the background facts, copied for each scenario.
        template = os.path.join(base, "background.db")
        db.db_path, db.history_enabled = template, True
        db.init_db()
        _load_background()
        for scenario in scenarios:
            tmp = tempfile.mkdtemp(prefix="wave_membench_")
            try:
                db.db_path = os.path.join(tmp, "memory_bench.db")
                shutil.copyfile(template, db.db_path)
                _load_scenario(scenario)
                for q in scenario["questions"]:
                    today = datetime.fromisoformat(q["at"]).date()
                    start = time.perf_counter()
                    facts = db.get_relevant_memory(q["q"]) if tier46 else _before_tier46(q["q"])
                    fact_lines = memory_context.fact_lines(facts)
                    found = episodic_recall.recall(q["q"], today=today, store=db)
                    dated = found[1] if found else []
                    ms = (time.perf_counter() - start) * 1000.0
                    added = [f"- {k}: {v}" for k, v in
                             people_graph.related_facts(q["q"], db.get_all_memory(include_archived=False))]
                    ok, rank = judge(q, fact_lines, dated, added)
                    records.append({"id": q["id"], "scenario": scenario["id"], "lang": scenario.get("lang", ""),
                                    "type": q["type"], "passed": ok, "rank": rank, "ms": round(ms, 2)})
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        embeddings.embed = real_embed
        db.db_path, db.history_enabled, config.MEMORY_PEOPLE_GRAPH, config.MEMORY_EPISODIC = saved
        shutil.rmtree(base, ignore_errors=True)
    return records


def metrics(records: list) -> dict:
    def rate(rows):
        return round(sum(r["passed"] for r in rows) / len(rows), 4) if rows else None

    ranked = [r for r in records if r["type"] in RANKED]
    out = {"questions": len(records), "overall": rate(records)}
    for k in (1, 3):
        out[f"recall@{k}"] = (round(sum(1 for r in ranked if r["rank"] and r["rank"] <= k) / len(ranked), 4)
                              if ranked else None)
    out["mrr"] = round(sum(1.0 / r["rank"] for r in ranked if r["rank"]) / len(ranked), 4) if ranked else None
    for t in TYPES:
        out[f"type:{t}"] = rate([r for r in records if r["type"] == t])
    forget = [r for r in records if r["type"] == "forget"]
    abstain = [r for r in records if r["type"] == "abstain"]
    out["forget_leak"] = round(1 - rate(forget), 4) if forget else 0.0
    out["false_memory"] = round(1 - rate(abstain), 4) if abstain else 0.0
    times = sorted(r["ms"] for r in records)
    out["p50_ms"] = round(statistics.median(times), 2) if times else None
    out["p95_ms"] = round(times[min(len(times) - 1, int(0.95 * len(times)))], 2) if times else None
    return out


def gate_failures(m: dict) -> list:
    fails = []
    if (m.get("overall") or 0) < GATE["overall"]:
        fails.append(f"overall {m.get('overall')} < {GATE['overall']}")
    for key in ("forget_leak", "false_memory"):
        if (m.get(key) or 0) > GATE[key]:
            fails.append(f"{key} {m.get(key)} > {GATE[key]}")
    return fails


def _pct(v):
    return "-" if v is None else f"{v * 100:.1f}%"


def report_md(before: dict, after: dict, n_scenarios: int) -> str:
    rows = [("Overall pass rate", "overall"), ("Recall@1", "recall@1"), ("Recall@3", "recall@3"), ("MRR", "mrr")]
    rows += [(f"Type {t}", f"type:{t}") for t in TYPES]
    rows += [("Forgetting leak rate", "forget_leak"), ("False-memory rate", "false_memory")]
    lines = ["## Memory", "",
             f"Scenarios: {n_scenarios} | Questions: {after['questions']} | Embedder: all-MiniLM-L6-v2 | "
             f"22 background facts each", "",
             "| Metric | Before Tier 46 | Tier 46 |", "| --- | --- | --- |"]
    for label, key in rows:
        fmt = (lambda v: "-" if v is None else f"{v:.4f}") if key == "mrr" else _pct
        lines.append(f"| {label} | {fmt(before.get(key))} | {fmt(after.get(key))} |")
    lines.append(f"| Retrieval p50 / p95 | {before['p50_ms']} / {before['p95_ms']} ms | "
                 f"{after['p50_ms']} / {after['p95_ms']} ms |")
    return "\n".join(lines) + "\n"


def model_available() -> bool:
    try:
        return config.SEMANTIC_MEMORY_ENABLED and embeddings.embed("hello") is not None
    except Exception:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenarios", default=str(SCENARIOS))
    parser.add_argument("--report-md", default=None)
    parser.add_argument("--json", dest="json_out", default=None)
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if not model_available():
        print("memory bench: the sentence model (all-MiniLM-L6-v2) isn't available - nothing measured.")
        return EXIT_NO_MODEL
    scenarios = load(args.scenarios)
    before_records = run(scenarios, tier46=False)
    after_records = run(scenarios, tier46=True)
    before, after = metrics(before_records), metrics(after_records)
    md = report_md(before, after, len(scenarios))
    print(md)
    failed = [r["id"] for r in after_records if not r["passed"]]
    if failed:
        print("Failed with Tier 46:", ", ".join(failed))
    if args.report_md:
        Path(args.report_md).write_text(md, encoding="utf-8")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps({"before": before, "tier46": after, "records": after_records},
                                                  indent=2), encoding="utf-8")
    if args.gate:
        fails = gate_failures(after)
        if fails:
            print("GATE FAILED: " + "; ".join(fails))
            return EXIT_GATE
    return 0


if __name__ == "__main__":
    sys.exit(main())
