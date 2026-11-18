"""
skills/registry.py — name -> StandaloneSkill lookup, separate from
brain/skills_registry.py's SkillRegistry (which holds raw, LLM-facing
tools). Deliberately NOT merged with that one: these aren't sent to the LLM
as individually callable tools (an LLM tool - daily_briefing,
run_self_diagnostics, both in brain/groq_core.py - is what's callable; this
registry is what that tool's handler looks itself up in), and they don't
support hot-reload from user_skills/ - a standalone skill is built-in,
reviewed Wave code, not a drop-in file.
"""
import logging

from skills.base import StandaloneSkill
from skills.daily_briefing import run_daily_briefing
from skills.self_diagnostics import run_self_diagnostics

logger = logging.getLogger("StandaloneSkills")

_SKILLS = {
    "daily_briefing": StandaloneSkill(
        name="daily_briefing",
        description="Weather, today's calendar events, and today's pending reminders, read back as one summary.",
        run=run_daily_briefing,
    ),
    "self_diagnostics": StandaloneSkill(
        name="self_diagnostics",
        description="Checks the LLM provider, database, disk space, and voice stack, and reports what's actually working.",
        run=run_self_diagnostics,
    ),
}


def run(name: str) -> str:
    entry = _SKILLS.get(name)
    if entry is None:
        return f"I don't have a '{name}' skill."
    try:
        return entry.run()
    except Exception as e:
        logger.error(f"Standalone skill '{name}' failed: {e}")
        return f"Something went wrong running {name.replace('_', ' ')}."


def list_skills() -> list:
    """(name, description) for every registered standalone skill."""
    return [(s.name, s.description) for s in _SKILLS.values()]
