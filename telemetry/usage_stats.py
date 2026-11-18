"""
telemetry/usage_stats.py
=============================================================================
Tier 21 roadmap item: "Local-only usage stats, opt-in."

This is deliberately NOT a new collection pipeline. telemetry/trace.py
already writes one row per finished think()/think_stream() turn into the
local turn_trace table (database/db_manager.py), gated by
config.TELEMETRY_ENABLED (default on) - that's what feeds the Performance
page's TTFA numbers, and it is turn_trace's own row that already carries
source (voice/text/wake/stop), outcome, timing, and - inside its meta JSON
column - which skill handled the turn (see brain/groq_core.py's
trace.annotate(skill=...) calls).

summarize() below only READS that already-local SQLite table and turns it
into a plain usage summary (turns per day, voice vs. text split, success
rate, most-used skills). Nothing here makes a network call, and nothing
here runs at all unless config.USAGE_STATS_ENABLED is explicitly turned on
(see ui/settings_panel.py's cb_usage_stats checkbox) - that is this
feature's OWN opt-in, separate from and on top of TELEMETRY_ENABLED: a
user can have turn_trace rows locally (for Performance) while usage_stats
stays off, or turn this on later and immediately see a summary of
everything already logged - no backfill needed because nothing new had to
be collected.
"""
import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from settings.config import config

logger = logging.getLogger("UsageStats")


def enabled() -> bool:
    return bool(getattr(config, "USAGE_STATS_ENABLED", False))


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def summarize(days: int = 7, limit: int = 5000) -> dict:
    """Returns {"enabled": False} immediately - no DB read at all - if the
    opt-in is off. Otherwise:
        {"enabled": True, "days": N, "total_turns": N,
         "by_source": {"voice": N, "text": N, ...},
         "success_rate": 0.0-1.0 or None (no turns),
         "top_skills": [(name, count), ...]}
    Never raises - a DB read failure or malformed row yields a result with
    zero counts rather than breaking whatever page is showing this."""
    if not enabled():
        return {"enabled": False}

    try:
        from database.db_manager import db
        rows = db.get_recent_traces(limit=limit)
    except Exception:
        logger.debug("usage stats: couldn't read turn_trace", exc_info=True)
        rows = []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    by_source = Counter()
    skills = Counter()
    total = 0
    ok = 0
    for row in rows or []:
        started = _parse_ts(row.get("started_at"))
        if started is not None and started < cutoff:
            continue
        total += 1
        by_source[row.get("source") or "unknown"] += 1
        if row.get("outcome") == "ok":
            ok += 1
        try:
            meta = json.loads(row.get("meta") or "{}")
        except (TypeError, ValueError):
            meta = {}
        skill = meta.get("skill") if isinstance(meta, dict) else None
        if skill:
            skills[skill] += 1

    return {
        "enabled": True,
        "days": days,
        "total_turns": total,
        "by_source": dict(by_source),
        "success_rate": round(ok / total, 3) if total else None,
        "top_skills": skills.most_common(10),
    }
