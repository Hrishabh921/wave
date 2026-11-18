# Contributing to Wave AI

## Running the tests

```
python -m pytest tests/ -q                # or, stdlib only:
python -m unittest discover -s tests
```

No real hardware, network or API key is needed: tests fake out whatever
external thing they'd otherwise touch (PyAudio, pygame's mixer, `edge_tts`,
`webrtcvad`, the LLM API, PyQt6 widgets). See `tests/_phase4_support.py`
and the `_install_fakes()`-style helper near the top of most test files for
the pattern:

1. build a minimal fake module;
2. set `sys.modules["the_real_name"] = fake_module`;
3. pop any already-imported copy of the module under test from
   `sys.modules`, then re-import it fresh;
4. assert on behaviour, not implementation.

A new test file should follow that same shape. Reuse an existing harness
instead of redefining it. For example, `tests/test_tier9_stt.py` and
`test_tier10_fixes.py` both import `_install_fakes` from
`test_phase5_stt_aec_wiring.py`.

`tests/test_repo_hygiene.py` fails if a file from an older version is
still present, or if a module in Wave's own packages isn't imported by
anything. If it names a file, read **Upgrading a checkout** below.

The suite doesn't render real PyQt6 windows on a real screen. A UI change
still needs a manual look. `docs/MANUAL_TEST_CHECKLIST.md` lists the
real-hardware checks nothing here can cover: mic/speaker behaviour, a real
Windows build. `scripts/windows_smoke_check.py` is the quick first pass on
a Windows PC.

## Upgrading a checkout

Extract a new Wave package into a **fresh folder**, then copy across your
`.env`, `database/` and any voice models. Unzipping on top of an old folder
adds and replaces files but can never delete the ones the new version
dropped. That is how the Tier 1–9 merge ended up with 66 failing tests
from leftovers (see `NEXT_STEPS.md`). If it already happened:

```
python scripts/remove_obsolete_files.py            # lists leftovers
python scripts/remove_obsolete_files.py --delete   # removes them
```

When a release removes a file, add it to that script's `OBSOLETE_PATHS`.

## Conventions this codebase actually follows

- **A new engine/backend/integration gets a feature flag and a graceful
  fallback, not a hard dependency.**
  - See the `*_ENABLED` settings in `settings/config.py` for the shape:
    `AEC_ENABLED`, `VAD_PRECHECK_ENABLED`, `TTS_LOCAL_ENABLED`,
    `PLANNER_ENABLED`, `INTENT_ROUTER_ENABLED`, `RISK_TIER_GUARD_ENABLED`,
    `PROMPT_INJECTION_DEFENSE_ENABLED`.
  - An optional import wrapped in `try/except` sets a `HAS_X` flag. The
    feature checks both that flag and its config toggle.
  - On any failure, the surrounding code degrades to its pre-feature
    behaviour: never silence, never a crash.

- **Comments explain *why*, and keep a decision history.**
  - See `voice/tts.py`'s `HAS_PIPER` block: numbered reasons for a
    choice, kept in place even after a later tier changes that choice.
  - If you reverse a past decision, extend that history; don't erase it.

- **A new tool is one `@skill(...)`-decorated function** (schema and
  handler together), in the right `brain/handlers/*.py` domain file. Every
  tool must also:
  - have a risk tier in `brain/tiers.py` (T0 read-only through T3
    destructive). `tests/test_security_tiers.py` fails CI if the table and
    the live registry drift apart in either direction;
  - get fast-path phrasings in `brain/intent_router.py` where the command
    is unambiguous, in English, romanised Hinglish and Devanagari;
  - get matching rows in `bench/golden/`.

- **Platform-specific behaviour goes through `automation/backends/`.**
  - `windows.py` is the real, verified one; `linux.py` and `macos.py` are
    experimental.
  - A new system action gets a method on all three. Where a platform
    genuinely can't do something, raise `NotSupported` with a plain
    sentence rather than leaving the method missing.

- **Numbers in names are not one shared sequence.** This project has:
  - `docs/PHASE4.md`–`PHASE6.md` and `tests/test_phase4_*.py`–`test_phase8_*.py`;
  - the README's "Shipped in Tier N" sections and `tests/test_tierN_*.py`
    (Tiers 1–9 so far);
  - `docs/ROADMAP_BEAT_SARA.md`'s Tiers 10–22, which continue that same
    Tier sequence.

  An older, parallel effort also used "Tier" numbers for different work
  (see `NEXT_STEPS.md`). Before reusing a number, grep for it, and say
  which sequence you mean.

## Where things live

- `brain/`:
  - LLM orchestration: `groq_core.py`.
  - Tool implementations, by domain: `handlers/`.
  - Tool registry: `skills_registry.py`.
  - Risk tiers: `tiers.py`. Per-turn taint tracking: `taint.py`.
  - Confirm gate: `confirm_gate.py`. Untrusted-content boundary: `content_trust.py`.
  - Deterministic fast path: `intent_router.py`.
- `voice/`:
  - STT: `stt.py`, with post-capture VAD in `buffers.py`.
  - TTS: `tts.py`, with the phrase cache in `tts_cache.py`.
  - Echo cancellation: `aec.py`.
  - Voice-loop commands such as "go to sleep": `commands.py`.
- `automation/`:
  - What the tools actually do: app/file control and the per-OS `backends/`.
  - Audit + undo, proactive nudges, notes RAG, the streak tracker.
- `planning/`: multi-step plan proposal, validation and execution for
  compound requests.
- `skills/`: built-in skills that compose several tools (daily briefing,
  self-diagnostics).
- `telemetry/`: per-turn tracing, queryable via `python -m telemetry.dump`.
- `bench/`: the eval/regression harness (`run_bench.py --compare` for CI
  gating) and the red-team suite for `content_trust.py`.
  - When accepting a new baseline, always pass `--mode all`, or the
    modes you didn't run are wiped from `baseline.json`.
- `ui/`: the PyQt6 pages and background `QThread` workers.
- `scripts/`: one-off utilities: the Windows smoke check and the
  obsolete-file cleanup.

## Adding a dependency

Answer three questions first:

1. **Windows wheels?** Are there confirmed wheels for Windows? This is a
   Windows-first project, and a bad guess breaks
   `pip install -r requirements.txt` for everyone.
2. **License?** Is it compatible with Wave's MIT license (see `LICENSE`)?
3. **Size and safety?** Is it small and safe enough to always import, or
   does it need the optional feature-flag pattern above?

New heavy or optional voice/ML packages go in an optional requirements
file instead of growing the base `requirements.txt` (see
`docs/ROADMAP_BEAT_SARA.md`, Tier 11).
