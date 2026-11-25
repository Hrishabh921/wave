# Memory bench results (Tier 46)

`python -m bench.memory_bench` - see `bench/memory_bench.py` for how each question is judged. Every scenario
runs on a fresh temporary database with 22 everyday background facts (so only the most relevant facts reach
the prompt, as for a user who has told Wave a lot), embedder all-MiniLM-L6-v2. "Before Tier 46" is the
retrieval Wave had until then (facts above 0.35 similarity, else the first ones alphabetically; no people
graph; no dated messages) on the same stored facts.

## Scenarios (`bench/memory/scenarios.jsonl`)

36 scenarios, 54 questions. The word lists in `brain/memory_rank.py` were written while looking at these
results, so they are not an unbiased test - see the held-out set below for that.

| Metric | Before Tier 46 | Tier 46 |
| --- | --- | --- |
| Overall pass rate | 64.8% | 100.0% |
| Recall@1 | 56.8% | 70.3% |
| Recall@3 | 70.3% | 100.0% |
| MRR | 0.6577 | 0.8514 |
| Type single | 84.6% | 100.0% |
| Type contradiction | 62.5% | 100.0% |
| Type history | 25.0% | 100.0% |
| Type multihop | 75.0% | 100.0% |
| Type temporal | 27.3% | 100.0% |
| Type forget | 100.0% | 100.0% |
| Type abstain | 100.0% | 100.0% |
| Forgetting leak rate | 0.0% | 0.0% |
| False-memory rate | 0.0% | 0.0% |
| Retrieval p50 / p95 | 36.3 / 43.6 ms | 42.5 / 46.4 ms |

## Held-out questions (`bench/memory/heldout.jsonl`)

16 scenarios, 18 questions written after the ranking was settled and run once, unchanged: other words for the
same things ("which company employs me?", "what's my wifi called?", "main kahan padhta hoon?"), people the
scenarios didn't have (girlfriend, roommate, "mera bhai ... kaam karta hai"), other days ("last Thursday",
"kal ... dekhi thi").

| Metric | Before Tier 46 | Tier 46 |
| --- | --- | --- |
| Overall pass rate | 55.6% | 100.0% |
| Recall@1 | 61.5% | 76.9% |
| Recall@3 | 61.5% | 100.0% |
| MRR | 0.6500 | 0.8846 |
| Type single | 66.7% | 100.0% |
| Type contradiction | 100.0% | 100.0% |
| Type history | 100.0% | 100.0% |
| Type multihop | 25.0% | 100.0% |
| Type temporal | 0.0% | 100.0% |
| Type forget | 100.0% | 100.0% |
| Type abstain | 100.0% | 100.0% |
| Forgetting leak rate | 0.0% | 0.0% |
| False-memory rate | 0.0% | 0.0% |
| Retrieval p50 / p95 | 36.7 / 44.7 ms | 40.8 / 70.2 ms |

## What this does not measure

- A changed fact saved under a *different* label ("city: Ajmer", later "current_city: Jaipur") stays two
  facts; the scenarios update the same label, as Wave's tools are asked to.
- Abstention here is "nothing Wave adds on its own points at a wrong answer"; with the whole fact list in front
  of it, the model still has to follow the prompt's "never guess personal details" rule.
- SARA's memory bench (its `bench/results/RESULTS.md`) is a different set of questions on a different store
  (gold-extracted subject/predicate/object facts, a hash embedder); the numbers are not directly comparable.
