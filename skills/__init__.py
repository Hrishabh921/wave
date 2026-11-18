"""
skills/ — Tier 3 item 10: a modular "standalone skills" layer.

Everything registered through brain/skills_registry.py (the @skill
decorator) is a RAW TOOL: one LLM-callable action, one underlying
operation (open_app, get_weather, save_reminder, ...). That's the right
shape for most of what Wave does, and user_skills/ (brain/skill_loader.py)
extends exactly that same shape with hot-reloadable drop-in files.

A STANDALONE SKILL here is different in kind, not just in size: it's a
small piece of orchestration that combines several of those raw tools into
one higher-level capability with its own internal logic - "give me my
daily briefing" isn't one API call, it's "get the weather, get today's
calendar events, and today's pending reminders, and read back whatever of
that actually succeeded, in a sensible order". That composition logic
needs a home that ISN'T a single @skill-decorated method quietly growing
inside brain/groq_core.py, and isn't a user_skills/ drop-in file either
(those are meant to be small and user-authored, loaded/unloaded live) -
it's built-in, multi-step, Wave-maintained logic. This package is that
home.

    skills/base.py             - StandaloneSkill: the small shared contract
    skills/registry.py         - name -> StandaloneSkill lookup table
    skills/daily_briefing.py   - weather + today's calendar events + today's
                                  pending reminders, read back as one summary
    skills/self_diagnostics.py - checks the LLM provider, the database, the
                                  disk, and the voice stack, and reports
                                  what's actually working right now

Each one is still reached through exactly ONE ordinary @skill-decorated
tool in brain/groq_core.py (daily_briefing / run_self_diagnostics) - the
LLM tool-calling surface doesn't change shape or grow new conventions; the
tool's handler just delegates into skills.registry.run(...) instead of
doing the work inline.
"""
