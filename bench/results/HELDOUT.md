# Held-out command results (Tier 50)

`python -m bench.heldout_bench` - see `bench/heldout_bench.py`. `bench/heldout/commands.jsonl` holds 76 everyday
commands said in other words and 41 sentences that must run nothing, written once for Tier 50 and never used to
tune a rule, a threshold or an example. "Fast path" is `brain/intent_router.py`; "+ semantic" adds Tier 45's
fallback (`brain/semantic_intents.py`, all-MiniLM-L6-v2) after it. A *wrong action* runs something other than what
was meant; a *false trigger* runs anything at all on a sentence that meant nothing.

## First run (the set unseen)

| | Fast path alone | + semantic fallback |
| --- | --- | --- |
| Commands run right | 19/76 (25%) | 56/76 (74%) |
| English | 14/53 (26%) | 41/53 (77%) |
| Hinglish | 5/23 (22%) | 15/23 (65%) |
| Wrong action | 2 | 2 (all from the fast path) |
| False trigger on 41 non-commands | 4 | 4 (all from the fast path) |

The semantic layer added 37 right answers, no wrong action and no false trigger. The fast path's six mistakes were
real bugs: "volume 2 of the book" set the volume to 2%, "my sister took a screenshot of our chat" took a screenshot,
"how do i increase brightness on a mac" raised this PC's brightness, "sound bilkul band kar do" tried to close an app
called "sound bilkul", "minimize all the windows" looked for a window called "all the windows", and "minimize the
risk" looked for one called "the risk".

## After fixing the fast-path bugs it found

These numbers are no longer held-out for those fixes (they were made after seeing the failures):

| | Fast path alone | + semantic fallback |
| --- | --- | --- |
| Commands run right | 21/76 (28%) | 58/76 (76%) |
| English | 15/53 (28%) | 42/53 (79%) |
| Hinglish | 6/23 (26%) | 16/23 (70%) |
| Wrong action | 0 | 0 |
| False trigger on 41 non-commands | 1 | 1 |

The one left is "minimize the risk" -> minimize_window("the risk"), which finds no such window and says so. The 18
commands still not run go to the model as before - mostly Hinglish ("aaj ki taaza khabar", "time kya ho raha hai")
and phrasings whose words the semantic layer's closed vocabularies don't have ("what's left on my task list").
The golden router set (`bench/golden/`, 664 rows) stays at 100% accuracy with no regressions.
