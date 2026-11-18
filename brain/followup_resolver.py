"""
brain/followup_resolver.py — Tier 17: "aur Jaipur ka?" / "close it"
=====================================================================

A second, equally small deterministic layer, sitting right next to
brain/intent_router.py and tried just before it in think()/think_stream()
(see those two call sites in brain/groq_core.py). Where intent_router.py
maps a SELF-CONTAINED message to a skill, this module maps a message that
only makes sense GIVEN what Wave just did - a pronoun ("it"/"usko"/"that")
or an elliptical follow-up ("and Jaipur?") whose antecedent is the most
recent thing in automation/audit.py's action log, not anything in the
message itself.

    routed = followup_resolver.resolve_followup(user_text)
    if routed is not None:
        reply = self._dispatch_tool(routed.skill, routed.args, source="followup_resolver")

Deliberately built on automation.audit.audit.recent() rather than any new
mutable state on WaveBrain - that log already records every dispatched
action (AI-chosen and intent-router-chosen alike, per _dispatch_tool()'s
own identical-path guarantee), already lives in RAM for the whole session
regardless of the "save chat history locally" setting (see audit.py's own
module docstring), and is already read by this tier's own
list_recent_actions skill. Piggybacking on it means zero changes to the
hot dispatch path in groq_core.py/_dispatch_tool() - this module only
ever READS audit.recent(), never writes to it, and groq_core.py's two
call sites are the only lines that needed touching at all.

Scope, on purpose (same discipline as intent_router.py's own docstring)
-------------------------------------------------------------------------
Only the roadmap's own two concrete shapes, plus the narrow, obviously-safe
siblings of the second one:

  * an app/window target follow-up - "close it"/"usko band karo" after
    open_app/close_app/restart_app/focus_window set what "it" refers to.
    Covers close_app, open_app ("open it again" - re-opening, not a bare
    "open it", which is ambiguous when the thing is presumably still
    open), restart_app, and focus_window. Deliberately NOT covering every
    other window-management tool (maximize/minimize/fullscreen/restore) -
    a reasonable future extension, not attempted here to keep this tier's
    scope bounded.
  * a weather-location follow-up - "and Jaipur?"/"aur Jaipur ka?" after
    get_weather, reusing the fact that the last turn asked about weather
    at all (the city is new; the TOPIC carries over).

Both are gated on RECENCY (automation.audit.audit.recent(1) - only the
single most recently dispatched action, and only if it's within
config.FOLLOWUP_CONTEXT_TTL_S and succeeded) - a pronoun this many
messages later, or one referring to something that failed, has no
reliable antecedent, and the safe behavior is the same as any other
intent_router miss: return None and let the LLM (which has the real chat
history) sort it out, possibly by asking a clarifying question itself.
This module never generates a clarifying question on its own - doing so
isn't really a routing decision, and the LLM is already well-equipped to
ask one using the history it already has (see brain/groq_core.py's
messages/history construction in think()).

Negation/conditional/hypothetical guards are NOT reimplemented here -
_is_compound_or_ambiguous (imported from intent_router) is reused as-is,
so "don't close it"/"should I close it" correctly fall through exactly
the same way they do for intent_router's own rules.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from brain.intent_router import _is_compound_or_ambiguous  # reuse, don't reimplement
from settings.config import config


@dataclass(frozen=True)
class RoutedFollowup:
    """Same shape as intent_router.RoutedIntent (skill + args + a label for
    logging only) - kept as its own tiny class rather than importing that
    one, since intent_router.py's RoutedIntent.rule is documented as "never
    sent to a tool" and this module's callers (groq_core.py) treat the two
    interchangeably at the call site but conceptually they come from two
    different deterministic layers."""
    skill: str
    args: Dict[str, str] = field(default_factory=dict)
    rule: str = ""


# Tools that set "it"/"usko" to their app_name/window_name argument, and are
# also themselves valid follow-up targets (so "open chrome" -> "close it"
# -> "open it again" -> "restart it" chains naturally - each step both
# consumes the previous target and becomes the new one).
_APP_TARGET_TOOLS = ("open_app", "close_app", "restart_app", "focus_window")


def _last_app_target() -> Optional[str]:
    """The app/window name of the most recent successful, still-fresh
    open_app/close_app/restart_app/focus_window dispatch, or None if
    there isn't one (nothing dispatched yet, it failed, it's stale, or
    it was a different kind of action entirely)."""
    from automation.audit import audit  # local import: keeps this module importable standalone (tests)

    rows = audit.recent(limit=1)
    if not rows:
        return None
    rec = rows[0]
    if rec.tool not in _APP_TARGET_TOOLS or rec.status != "ok":
        return None
    if not _is_fresh(rec.ts):
        return None
    name = rec.args.get("app_name") or rec.args.get("window_name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _last_action_was_weather() -> bool:
    """True if the most recent successful dispatch was get_weather, within
    the same freshness window as _last_app_target() above."""
    from automation.audit import audit

    rows = audit.recent(limit=1)
    if not rows:
        return False
    rec = rows[0]
    return rec.tool == "get_weather" and rec.status == "ok" and _is_fresh(rec.ts)


def _is_fresh(ts: str) -> bool:
    try:
        age_s = (datetime.now() - datetime.fromisoformat(ts)).total_seconds()
    except ValueError:
        return False
    return 0 <= age_s <= config.FOLLOWUP_CONTEXT_TTL_S


_CLOSE_IT_PHRASES = ("close it", "close that", "usko band karo", "use band karo",
                     "band kar do", "ise band karo", "इसे बंद करो", "उसे बंद करो")
_OPEN_IT_AGAIN_PHRASES = ("open it again", "open that again", "usko wapas kholo",
                          "wapas kholo", "फिर से खोलो")
_RESTART_IT_PHRASES = ("restart it", "restart that", "usko restart karo", "ise restart karo")
_FOCUS_IT_PHRASES = ("focus on it", "switch to it", "usko switch karo", "ise switch karo")

_WEATHER_FOLLOWUP_PREFIXES = ("and ", "what about ")
_WEATHER_FOLLOWUP_HINGLISH = (("aur ", " ka"), ("aur ", " ka kya hai"), ("aur ", " mein"), ("aur ", ""))


def _app_target_followup(lower: str) -> Optional[RoutedFollowup]:
    for phrases, skill in (
        (_CLOSE_IT_PHRASES, "close_app"),
        (_OPEN_IT_AGAIN_PHRASES, "open_app"),
        (_RESTART_IT_PHRASES, "restart_app"),
        (_FOCUS_IT_PHRASES, "focus_window"),
    ):
        if lower not in phrases:
            continue
        target = _last_app_target()
        if not target:
            return None
        arg_name = "window_name" if skill == "focus_window" else "app_name"
        return RoutedFollowup(skill=skill, args={arg_name: target}, rule=f"followup_{skill}")
    return None


def _weather_followup(lower: str) -> Optional[RoutedFollowup]:
    text = lower.strip().rstrip("?").strip()
    if not text:
        return None
    city = None
    for prefix in _WEATHER_FOLLOWUP_PREFIXES:
        if text.startswith(prefix):
            city = text[len(prefix):].strip()
            break
    if city is None:
        for prefix, suffix in _WEATHER_FOLLOWUP_HINGLISH:
            if text.startswith(prefix) and (not suffix or text.endswith(suffix)):
                end = len(text) - len(suffix) if suffix else len(text)
                city = text[len(prefix):end].strip()
                if city:
                    break
    if not city:
        return None
    if not _last_action_was_weather():
        return None
    return RoutedFollowup(skill="get_weather", args={"location": city}, rule="followup_get_weather")


def resolve_followup(user_text: str) -> Optional[RoutedFollowup]:
    """Returns a RoutedFollowup if `user_text` is one of the curated
    follow-up shapes above AND the action log has a fresh, successful
    antecedent for it, else None - meaning: fall through to
    intent_router.route(), exactly as if this module didn't exist."""
    if not config.FOLLOWUP_RESOLVER_ENABLED:
        return None
    if not user_text or not user_text.strip():
        return None
    lower = user_text.strip().lower()
    if _is_compound_or_ambiguous(lower):
        return None
    return _app_target_followup(lower) or _weather_followup(lower)
