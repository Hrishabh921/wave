"""
telemetry — Tier 4 item #13: lightweight per-turn tracing for WaveBrain.

    from telemetry import trace
    trace.begin_turn(source="think")
    ...
    trace.end_turn(outcome="ok", route="tool_loop")

See telemetry/trace.py for the full API and design notes, telemetry/store.py
for how finished turns reach database/db_manager.py's turn_trace table, and
telemetry/dump.py for a CLI to inspect them (`python -m telemetry.dump`).
"""
from telemetry import trace  # noqa: F401  (re-exported for `from telemetry import trace`)

__all__ = ["trace"]
