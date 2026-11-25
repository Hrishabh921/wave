# Wave bench — Tier 4 item #12

An eval/regression harness for Wave's two deterministic, offline safety/
routing layers: `brain/intent_router.py` (the fast pre-LLM intent layer,
Tier 2 item #4) and `brain/content_trust.py` (the prompt-injection scanner,
Tier 1). Modelled on SARA's own `bench/`, with scope adjusted to what Wave
actually has - see each module's own docstring for exactly what was kept,
simplified, or re-aimed and why (`bench/runner.py` on "router mode" only,
`bench/redteam_runner.py` on why there's no allow/deny policy matrix here).

## Running it

```bash
python -m bench.run_bench                                   # router + redteam, print a summary
python -m bench.run_bench --mode router                     # intent_router only
python -m bench.run_bench --mode redteam                    # content_trust scanner + confirm-gate invariant only
python -m bench.run_bench --compare                         # exit non-zero (for CI) on a regression vs. baseline.json
python -m bench.run_bench --accept-baseline                 # write this run's results as the new baseline
python -m bench.run_bench --report-md bench/results/RESULTS.md --json bench/results/latest.json
```

`--mode redteam`'s confirm-gate invariant check needs Wave's real tool
registry populated, which only happens as a side effect of importing
`brain/groq_core.py` - so that one check pulls in groq_core's real
dependencies (pyautogui and friends), same as running the actual app. The
router check and the scanner check do not - `brain/intent_router.py` and
`brain/content_trust.py` are both pure, dependency-light modules on their
own.

`bench/thresholds.json` defines the regression limits `--compare` checks
against `bench/baseline.json`: max accuracy/F1/composite drop, max
false-trigger-rate increase (plus an absolute cap), max p95 latency
increase, and (for redteam) minimum scanner recall / maximum false-
positive rate. Exit code 1 = a router threshold failed, 2 = a redteam
threshold or the confirm-gate invariant failed, 0 = clean (or nothing to
compare against yet).

## Memory bench (Tier 46)

```bash
python -m bench.memory_bench                                  # scenarios: before Tier 46 vs now
python -m bench.memory_bench --scenarios bench/memory/heldout.jsonl
python -m bench.memory_bench --gate                           # exit 1 below 90% / any leak / any false memory
```

Modelled on SARA's memory bench: `bench/memory/scenarios.jsonl` (36 scenarios,
54 questions) and `bench/memory/heldout.jsonl` (18 questions written after the
ranking was settled) say things over weeks - facts, messages, "forget that" -
then ask single, contradiction, history, multi-hop (people), temporal,
forget and abstain questions, each judged on exactly the lines the model would
be shown (`brain/memory_context.py`, `brain/episodic_recall.py`). Needs the
sentence model (exit 4 without it). Results: `bench/results/MEMORY.md`.

## Data

- `golden/intents_en.jsonl`, `intents_hi.jsonl`, `intents_hinglish.jsonl` -
  one row per labelled utterance, every `intent_router.py` rule covered in
  at least one language, plus rows that should correctly get **no** match
  (`"i": "chat"`) for everything intent_router deliberately doesn't cover
  (reminders, weather, WhatsApp, calendar, notes, workflows - see that
  module's own "Scope, on purpose" section).
- `golden/negatives.jsonl` - ordinary chit-chat, all expecting `"chat"`.
- `golden/adversarial.jsonl` - negation/conditional/hypothetical/idiom-
  start guard probes specifically (`"don't close this window"`, `"can you
  lock the pc"` as a **positive** control showing polite phrasing still
  works, `"close the deal with the client"`, ...).
- `redteam/attacks.jsonl` - text that should score at/above
  `content_trust.CONTENT_TRUST_SCAN_THRESHOLD` (default 4.0); every row's
  note cites the exact pattern(s)/weight(s) it was built to trip, checked
  against the real patterns in `brain/content_trust.py`, not assumed.
- `redteam/benign.jsonl` - text that should stay under threshold, including
  deliberate near-misses (a single low-weight pattern hit that shouldn't be
  enough on its own) and one **known, kept-on-purpose false positive** (see
  below).

Every non-obvious label carries an `"n"` (note) explaining *why* - see
`bench/dataset.py`'s own docstring for the row schema. A row's label is
never adjusted just to make a number look better; see the findings below,
which is exactly what happened instead.

## Known gaps and findings this harness surfaces

These were discovered BY building and running this harness against the
real code (not assumed or invented), and are recorded here rather than
quietly worked around, because that is the entire point of a benchmark
that is supposed to catch real regressions:

1. **`intent_router.py` had zero Devanagari-script coverage** when this
   harness first ran it (every rule phrase was English or romanized
   Hindi/Hinglish, e.g. "awaaz badhao" - nothing matched native Devanagari
   text at all). **Closed in Tier 8:** Devanagari phrases were added to
   `_RULES` for `undo`/`take_screenshot`/`lock_pc`/`media_previous_track`/
   `volume_up`/`volume_down`/`toggle_mute`, plus two new matcher
   combinators (`_suffix_arg()`/`_prefix_or_suffix_arg()`) so `open_app`/
   `close_app` route correctly whether the verb comes first or last.
   `golden/intents_hi.jsonl`'s 9 previously-`"chat"` rows were updated to
   their now-correctly-routed intent (2 rows - reminders/weather - are
   still deliberately `"chat"`, out of scope for a different reason, with
   an updated note explaining why).
2. **Trailing Hindi negation ("mat" placed AFTER the phrase) wasn't
   caught.** `_NEGATION_MARKERS` had `"mat karo"`/`"mat kar"` (mat
   attached to a verb) but not a bare trailing `"mat"` - so "awaaz badhao
   mat" (which means "don't turn up the volume") used to still fire
   `volume_up`. **Closed in Tier 8:** a new `_has_standalone_negator()`
   check matches `"mat"`/`"मत"` as a standalone word (never a substring,
   so it can't misfire inside an unrelated word) and is OR'd into the
   existing compound/ambiguous-request guard. See the updated note on that
   row in `golden/intents_hinglish.jsonl`.
3. **"developer mode" is a known false positive** in
   `content_trust.py`'s `jailbreak-terms` pattern - it's also an ordinary,
   everyday Android/phone settings term. Kept deliberately in
   `redteam/benign.jsonl` (not swapped for an easier example) so
   `false_positive_rate` reflects it honestly; `thresholds.json`'s
   `max_false_positive_rate: 0.1` already accounts for this one known case
   (current rate: ~7.7%, one row out of thirteen).
4. **"never mind, lock the pc" is over-cautious**, not under: `"never "`
   is a negation marker and fires even though "never mind" is dismissing
   something unrelated, not negating the lock command. Documented on that
   row in `golden/adversarial.jsonl` rather than mislabeled to look more
   precise than the router actually is.

None of these were bugs introduced by Tier 4 - they were properties of the
Tier 2/Tier 1 code this harness evaluates, measured for the first time
instead of assumed. Findings 1 and 2 are now closed (Tier 8, above);
3 and 4 remain open by design/not yet addressed.
