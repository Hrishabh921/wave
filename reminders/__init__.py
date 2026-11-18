"""
reminders/ - Tier 3 / item 8 ("reminders deepening").

    context.py  - local, instant, deterministic understanding of a
                  reminder (category, urgency, time-of-day, wording
                  templates, the LLM prompt builder, output validation).
                  No database, no network, no LLM - also doubles as the
                  final fallback when composer.py can't reach an LLM.
    composer.py - decides what Wave actually says: kicks off an LLM
                  reword in the background and falls back to context.py's
                  template if that's disabled, slow, or fails.

Modeled on the reminder_context.py / reminder_composer.py pattern from the
SARA reference project, adapted to Wave's own DB layer (database/db_manager.py),
LLM provider (brain/llm_provider.py), and QThread-based workers
(ui/workers.py) rather than copied as-is.
"""
