# Tier 0–5 merge validation report

## Archive checks
- ZIP integrity check: passed.
- Python syntax compilation: 135 Python files checked; 0 syntax failures.

## Targeted tests
Command run:
`pytest -q tests/test_security_tiers.py tests/test_taint_and_guard.py tests/test_tier4_bench.py tests/test_tier4_planning.py`

Result: **69 passed, 18 failed**.

Failures are concentrated in integration points where the supplied Tier 5 ZIP replaces Tier 0–4 security/guard files:
- Tier 5 adds tools that are not yet present in the Tier 4 `brain/tiers.py` classification table.
- The replaced `brain/confirm_gate.py` / `brain/groq_core.py` versions do not expose all APIs and three-outcome guard behavior expected by the Tier 0–4 taint/security tests.
- Planner tests that expect a tainted destructive action to be denied instead receive a confirmation prompt.

Therefore, this archive is a **merged integration candidate**, not a release-verified package. Before using it as the main Wave project, reconcile Tier 5's tool additions with the Tier 4 security tier table and guard implementation, then rerun the full test suite on Windows.
