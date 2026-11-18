"""
skills/base.py — the shared contract every standalone skill follows.

Deliberately small: a standalone skill is a function of () -> str (or, for
daily_briefing, of nothing at all - it only reads from the DB and the web,
it has no need of `brain`) that Wave is willing to run end-to-end and
report the final text of. It does NOT get its own timeout/retry/UI-event
machinery the way SARA's full skill framework does - Wave's raw tools
(brain/skills_registry.py) already have a shared execution path
(_dispatch_tool: audit logging, confirm-gating, exception handling) and
every standalone skill here is itself only ever reached through one such
tool, so it inherits all of that for free. Keeping this layer thin is
deliberate, not a placeholder for more machinery later - see
skills/__init__.py for why this layer exists at all.
"""
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StandaloneSkill:
    name: str                # e.g. "daily_briefing" - matches the LLM tool that reaches it
    description: str         # human-readable, shown in list_standalone_skills
    run: Callable[[], str]   # run() -> str; never raises (each skill catches its own errors)
