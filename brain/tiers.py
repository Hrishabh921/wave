"""
brain/tiers.py — Tier 1 item #3: risk tier of every built-in tool.

Before this file existed, brain/skills_registry.py's `confirm` flag was the
only signal _guard() had: a tool either always asks first or never does,
decided once at the @skill(...) call site and never revisited. That's a
coarse binary where SARA's reference project has four levels, and - more
importantly - it has no notion of "this tool nobody has classified yet,"
because an un-flagged tool silently defaults to confirm=False (runs
immediately). A brand-new @skill(...) someone adds next month, or a skill
hot-loaded from user_skills/, gets the safe-by-default treatment unless a
human remembers to go flag it.

This module is the fix, kept deliberately separate from skills_registry.py
itself (same separation SARA's security/tiers.py keeps from its own
skill/intent registration) so "which tools exist" and "how risky is each
one" stay two different questions answered in two different places:

    T0  read-only, no side effect at all (a lookup, a list, a search)
    T1  a real but low-risk, trivially-reversible side effect
        (opens a window, nudges a setting, saves something easy to undo)
    T2  impactful: a human could lose something and not immediately
        notice, or undoing it takes more than one more voice command
    T3  destructive, hard to walk back, or has a consequence outside
        Wave entirely (another person receives a message, say)

Any tool NOT in TIERS below - a typo'd rename, or (most likely in
practice) a skill hot-loaded from user_skills/, which by definition can
never be in a table committed ahead of time - is UNKNOWN_TIER. That's set
to T2, not T0: a tool nobody has reviewed is risky by default, not safe
by default. See brain/groq_core.py's _guard() for how a tier actually
turns into run/confirm/deny, together with brain/taint.py (item #4).

tests/test_security_tiers.py fails CI the moment this table and
brain/groq_core.py's real @skill(...) registrations drift apart in either
direction - a tool added here that no longer exists, or a tool that exists
but was never added here - so this can't quietly rot the way the old
single hardcoded 7-tool confirm list could.
"""
from __future__ import annotations

from typing import Dict

T0 = 0
T1 = 1
T2 = 2
T3 = 3

TIER_LABELS: Dict[int, str] = {
    T0: "T0 read-only",
    T1: "T1 low-risk/reversible",
    T2: "T2 impactful",
    T3: "T3 destructive",
}

# Fail-safe default for any tool this table doesn't know about yet.
UNKNOWN_TIER = T2

# Tier 15: the keyboard surface (type_text/press_key/copy_text/paste_text/
# select_all_text/undo_keystroke/redo_keystroke) sends real input to
# whatever currently has focus - Wave has no idea what that is, so unlike
# every other T1 tool, "something untrusted was read this turn" isn't
# enough reason to merely ask first (_guard()'s normal T1-tainted
# behaviour) - it's reason to refuse outright, the same strength T3 gets
# when tainted, without making every ORDINARY keypress/copy/paste require
# a spoken confirmation the rest of the time (which a flat T3 tier would).
# _guard() (brain/groq_core.py) checks this set BEFORE its normal tier
# branch, for exactly the tools listed here - nothing else is affected.
# press_key additionally refuses specific dangerous chords and an
# Alt+F4-on-a-system-window combination regardless of taint; that's
# content-based (depends on the actual key argument), so it lives inside
# AppController.press_key() itself, the same place close_app's own
# critical-process refusal lives - not here, where only TOOL IDENTITY
# matters.
TAINT_DENY_TOOLS = frozenset({
    "press_key", "type_text", "copy_text", "paste_text", "select_all_text",
    "undo_keystroke", "redo_keystroke",
})

# Tier 20: tool name -> which of its arguments holds a URL Wave itself
# either visits (open_website - the person's own browser) or FETCHES
# server-side (summarize_page/add_ics_feed - Wave's own outbound request,
# no browser or screen involved at all). That second kind is the real
# exfiltration channel a prompt-injection attack would want: craft a URL
# whose query string smuggles out whatever the tainted turn just read
# (web/web_ops.py's validate_url() already blocks it from pointing at an
# internal/metadata IP - SSRF - but nothing stops the query string itself
# from carrying data OUT to an external host the attacker controls, which
# is a completely different shape of problem). _guard() (brain/groq_core.py)
# checks this BEFORE its normal tier branch, same pattern as
# TAINT_DENY_TOOLS above - a T0 tool like summarize_page still runs freely
# on an ordinary, untainted turn; only a tainted turn with an
# exfiltration-shaped URL gets denied. See brain/exfiltration.py.
URL_ARG_TOOLS: Dict[str, str] = {
    "open_website": "site",
    "summarize_page": "url",
    "add_ics_feed": "url",
}

# ---------------------------------------------------------------------------
# One entry per tool currently registered in brain/groq_core.py (see every
# @skill("name", ...) there). Grouped by tier rather than alphabetically -
# the grouping is the part worth reviewing; the exact order within a tier
# isn't meaningful.
#
# The 7 tools that already carried confirm=True before this file existed
# (delete_file, delete_latest_screenshot, forget_memory,
# send_whatsapp_message, close_app, create_file, take_photo) all land at T2
# or T3 below, on purpose: that's what keeps today's confirm-or-not
# behaviour unchanged for an ordinary, untainted turn (see _guard()) - this
# table only adds new behaviour (taint-based escalation, T3+tainted deny),
# it doesn't relax anything that already asked first.
# ---------------------------------------------------------------------------
TIERS: Dict[str, int] = {
    # -- T0: read-only, no side effect --------------------------------
    "get_live_information": T0,
    "get_weather": T0,
    "get_news": T0,
    "daily_briefing": T0,
    "run_self_diagnostics": T0,
    "read_screen_text": T0,
    "describe_screen": T0,          # Tier 18: same read-only shape as read_screen_text, just vision-model-backed
    "list_recent_actions": T0,
    "list_my_skills": T0,
    "get_pc_info": T0,
    "get_clipboard": T0,
    "recall_memory": T0,
    "why_remembered": T0,          # Tier 20: read-only provenance lookup, same shape as recall_memory
    "why_did_you_do_that": T0,     # Tier 20: read-only security-log lookup
    "why_did_you_notify_me": T0,   # Tier 20: read-only security-log lookup
    "search_my_notes": T0,
    "search_file": T0,
    "list_workflows": T0,
    "list_workflow_schedules": T0,   # Tier 18: same read-only shape as list_workflows
    "list_reminders": T0,
    "list_calendar_events": T0,
    "list_calendar_feeds": T0,       # Tier 18: same read-only shape as list_calendar_events
    "list_timers": T0,
    "check_timer": T0,
    "stopwatch_status": T0,          # reads elapsed time; never mutates (lap_stopwatch does the mutating)
    "get_joke": T0,                  # Tier 8: a curated-list lookup, no side effect at all
    "get_streak_status": T0,         # Tier 8: same tier as list_reminders/recall_memory - a plain read
    "list_open_windows": T0,         # Tier 8: same tier as list_calendar_events - a plain read
    # Tier 14: system info - every one of these only ever reads something
    # (psutil, a socket, a service list, an optional GPU library) and
    # changes nothing, same tier as get_pc_info/list_open_windows above.
    "get_uptime": T0,
    "get_local_ip": T0,
    "get_gpu_status": T0,
    "get_cpu_temperature": T0,
    "get_top_processes": T0,
    "list_services": T0,             # a read-only query - start_service/stop_service (T2) are the actions
    "get_disk_usage": T0,
    "get_battery_status": T0,
    "get_brightness_status": T0,     # reads the current %; set_brightness/up/down/max/min (T1) change it
    # Tier 16: productivity - plain reads, same tier as list_reminders/
    # get_streak_status above: nothing here changes anything.
    "read_notes": T0,
    "list_todos": T0,
    "calculate": T0,                 # a calculator query - no side effect, just arithmetic
    "convert_units": T0,             # same - a lookup/conversion, nothing stored or changed
    "get_active_mode": T0,           # reads _active_mode; enable_mode/disable_mode (T1) change it
    "get_current_time": T0,          # a clock read - see productivity.py's own docstring for why
    "get_current_date": T0,          # this exists at all even though the system prompt already has it
    "summarize_page": T0,            # fetches and extracts text - same tier as get_live_information/
                                      # get_weather/get_news above, which also just read the web
    "summarize_my_notes": T0,        # same tier as search_my_notes - a read against the notes index
    "quiz_me_on": T0,                # same - search_my_notes with a different prompt, not a new read path

    # -- T1: low risk, easily reversible -------------------------------
    "open_app": T1,
    "open_website": T1,
    "close_current_window": T1,
    "close_current_tab": T1,
    # Tier 8: per-window management - same tier as close_current_window/
    # close_current_tab just above: a focus change or reposition, nothing
    # lost, undo is one more command away (focus_window/snap it back).
    "focus_window": T1,
    "minimize_window": T1,
    "snap_window": T1,
    # Tier 14: the rest of per-window management - same tier and the same
    # reasoning as focus_window/minimize_window/snap_window just above.
    # always-on-top/fullscreen/show-desktop/minimize-all/switch are all
    # transient UI state too, nothing a user could lose.
    "move_window": T1,
    "resize_window": T1,
    "maximize_window": T1,
    "restore_window": T1,
    "set_always_on_top": T1,
    # Tier 25: show_ui/hide_ui toggle the full dashboard window's own
    # visibility, nothing more - same tier and reasoning as focus_window/
    # minimize_window above (transient UI state, trivially reversible by
    # saying the opposite command right back).
    "show_ui": T1,
    "hide_ui": T1,
    "set_fullscreen": T1,
    "show_desktop": T1,
    "minimize_all_windows": T1,
    "switch_window": T1,
    "play_youtube": T1,
    "media_play_pause": T1,
    "media_next_track": T1,
    "media_previous_track": T1,
    "volume_up": T1,
    "volume_down": T1,
    # Tier 14: toggle_mute is RETIRED (brain/handlers/media.py's own
    # comment has the bug this fixes) - mute_volume/unmute_volume/
    # set_volume/volume_max/volume_min replace it, all T1 like the volume
    # tools above: easily reversible (undo_last_action, or just say the
    # opposite), nothing lost. 🏆 all five now go through AppController's
    # audited, undoable path - see automation/app_control.py.
    "set_volume": T1,
    "volume_max": T1,
    "volume_min": T1,
    "mute_volume": T1,
    "unmute_volume": T1,
    # Tier 14: brightness - same risk profile as volume just above (a
    # display setting, trivially reversible, undoable).
    "set_brightness": T1,
    "brightness_up": T1,
    "brightness_down": T1,
    "brightness_max": T1,
    "brightness_min": T1,
    "search_google": T1,
    "take_screenshot": T1,          # undo_last_action covers this directly
    "lock_pc": T1,                  # trivially reversible: log back in
    "undo_last_action": T1,         # the "fix it" action - must stay cheap to run
    # Tier 14: the "fix it" action for power/session, same reasoning as
    # undo_last_action just above - a cancel has to stay cheap to run, so
    # this is T1 even though every action it cancels (just below) is T3.
    "cancel_shutdown": T1,
    # Tier 15: keyboard - sends input to whatever currently has focus.
    # Ordinary use is exactly as reversible as the rest of this bucket
    # (press another key, Ctrl+Z, retype it), so T1 like everything else
    # here - the taint protection these seven specifically need is
    # TAINT_DENY_TOOLS above, not a higher baseline tier, which would make
    # every ordinary keypress confirm-gated instead of just the tainted
    # ones refused.
    "press_key": T1,
    "type_text": T1,
    "copy_text": T1,
    "paste_text": T1,
    "select_all_text": T1,
    "undo_keystroke": T1,
    "redo_keystroke": T1,
    # Tier 15: browser control - same reasoning as the window-management
    # group above: transient, obviously reversible (scroll back, zoom
    # back, switch tabs again), nothing a user could lose.
    "new_tab": T1,
    "next_tab": T1,
    "previous_tab": T1,
    "reload_page": T1,
    "zoom_in": T1,
    "zoom_out": T1,
    "zoom_reset": T1,
    "scroll_up": T1,
    "scroll_down": T1,
    "scroll_to_top": T1,
    "scroll_to_bottom": T1,
    # Tier 15: clipboard write - get_clipboard (T0) already reads it;
    # writing is a real side effect but trivially overwritten again.
    "set_clipboard": T1,
    # Tier 15: folder shortcuts - same tier as open_system_setting/
    # open_app above: opens a window, changes nothing itself.
    "open_downloads": T1,
    "open_documents": T1,
    "open_pictures": T1,
    "open_music": T1,
    "open_videos": T1,
    "open_this_pc": T1,
    "open_recycle_bin": T1,
    "open_task_manager": T1,
    "open_control_panel": T1,
    # Tier 15: download watcher - arms/cancels a background check; nothing
    # is touched on disk either way.
    "watch_for_download": T1,
    "cancel_download_watch": T1,
    "create_folder": T1,
    "write_notepad": T1,
    "open_system_setting": T1,      # navigates to a settings page; changes nothing itself
    # Tier 5: dedicated settings-page shortcuts - same tier and same reasoning
    # as open_system_setting itself, since each is a zero-argument wrapper
    # around that exact same navigate-only call.
    "open_display_settings": T1,
    "open_sound_settings": T1,
    "open_bluetooth_settings": T1,
    "open_network_settings": T1,
    "open_wifi_settings": T1,
    "open_update_settings": T1,
    "open_apps_settings": T1,
    "open_personalization_settings": T1,
    "open_privacy_settings": T1,
    "open_storage_settings": T1,
    "open_battery_settings": T1,
    "open_power_settings": T1,
    "open_about_settings": T1,
    "open_nightlight_settings": T1,
    "open_airplane_mode_settings": T1,
    "toggle_hardware": T1,          # wifi/bluetooth/hotspot - obvious and trivially reversible
    "create_workflow": T1,
    "run_workflow": T1,             # each replayed step is re-gated individually as it dispatches
    "schedule_workflow": T1,         # Tier 18: same risk shape as set_reminder - a future action, reversible via cancel
    "cancel_workflow_schedule": T1,  # same tier as cancel_reminder
    "set_reminder": T1,
    "cancel_reminder": T1,
    "set_timer": T1,                 # same tier as set_reminder - it IS one, under the hood
    "cancel_timer": T1,              # same tier as cancel_reminder
    "start_stopwatch": T1,           # low-risk/reversible: restarting loses only the current lap
    "stop_stopwatch": T1,
    "lap_stopwatch": T1,
    "create_calendar_event": T1,
    "cancel_calendar_event": T1,
    # Tier 18: calendar sync. add/remove are stored, easily-undone feed
    # registrations (same reasoning as create_calendar_event/schedule_
    # workflow above); sync_calendars fetches+upserts events - real but
    # low-risk and idempotent (same shape as watch_for_download: "arms/
    # does a background check; nothing destructive"); setup_google_calendar
    # opens a browser to a real external OAuth consent screen - Wave itself
    # changes nothing beyond that (same "opens a browser/app" shape as
    # open_apps_settings/play_on_spotify), the actual grant decision is the
    # user's own via Google's own UI, and disconnecting is one more
    # command away (remove_calendar_feed("google") also deletes the cached
    # token - see automation/calendar_sync.py's CalendarSync.remove_feed).
    "add_ics_calendar_feed": T1,
    "remove_calendar_feed": T1,
    "sync_calendars": T1,
    "setup_google_calendar": T1,
    "reindex_notes": T1,            # rebuilds Wave's own index; touches no user file
    "remember_this": T1,            # reversible via forget_memory
    "check_in_streak": T1,          # Tier 8: additive, low-stakes - same risk profile as set_reminder
    # Tier 16: productivity. take_note/add_todo are additive and low-stakes,
    # same reasoning as check_in_streak/remember_this just above.
    # complete_todo/delete_todo are each a real side effect but a cheap one
    # to walk back (re-add the text, or complete_todo again by mistake
    # loses nothing) - same "removes a tracked item, no undo, low stakes"
    # reasoning cancel_reminder already got at T1. enable_mode/disable_mode
    # are a settings nudge like volume/brightness above - trivially
    # reversible (the other one undoes it, or say the opposite, or
    # undo_last_action). play_on_spotify is a browser search/open, same
    # tier as play_youtube/open_website/search_google.
    "take_note": T1,
    "add_todo": T1,
    "complete_todo": T1,
    "delete_todo": T1,              # same "tracked item removed, no undo, low stakes" tier as cancel_reminder
    "enable_mode": T1,
    "disable_mode": T1,
    "play_on_spotify": T1,

    # -- T2: impactful - hard to notice, or more than one step to undo --
    "close_app": T2,                # may silently drop another app's unsaved work
    "create_file": T2,              # undo_last_action covers this directly
    "delete_latest_screenshot": T2,  # undo_last_action covers this directly
    "take_photo": T2,               # webcam activation - consent-sensitive more than destructive
    "start_service": T2,            # Tier 8: starting an unexpected background service (a VPN, a
                                     # server) can have effects outside the obvious "ok, started" -
                                     # same confirm-first posture as stop_service for symmetry, even
                                     # though stop is where the real "broke something" risk concentrates
    "stop_service": T2,             # Tier 8: can silently break something unrelated (printing, name
                                     # resolution, a dev server) in a way that doesn't surface until
                                     # later and isn't obviously connected back to this - same "hard to
                                     # notice" reasoning as close_app above, not T3: starting it back up
                                     # again is one more command, same as close_app reopening the app
    "reset_streak": T2,             # Tier 8: no undo path - a long-earned best streak is just gone,
                                     # same "the fact is just gone" reasoning as forget_memory, but
                                     # scoped to one small, self-contained piece of gamification data
                                     # rather than arbitrary knowledge/a file/another person - T2, not T3
    "restart_app": T2,               # Tier 14: close_app's own risk (may silently drop unsaved work)
                                      # plus a reopen - same tier as close_app, for the same reason
    "clear_notes": T2,                # Tier 16: no undo path - every quick note is just gone, same
                                       # "the fact is just gone" reasoning as reset_streak/forget_memory,
                                       # scoped to one small, self-contained piece of data - T2, not T3

    # -- T3: destructive, irreversible, or reaches outside Wave ---------
    "delete_file": T3,
    "empty_recycle_bin": T3,         # Tier 15 - roadmap: "T3, confirm, not undoable, and it says so";
                                      # genuinely gone, no trash-of-the-trash to recover from (unlike
                                      # delete_latest_screenshot's own recoverable-trash path)
    "forget_memory": T3,            # no undo path - the fact is just gone
    "send_whatsapp_message": T3,    # cannot be unsent; a real person receives it
    # Tier 14: power/session - "All T3 with confirmation, and delayed so
    # they can still be cancelled" (roadmap). cancel_shutdown itself is
    # deliberately T1 instead (see that entry above) - cancelling isn't
    # the destructive half of this pair.
    "shutdown_pc": T3,
    "restart_pc": T3,
    "sleep_pc": T3,
    "hibernate_pc": T3,
    "log_off": T3,
}


def get_tier(name: str) -> int:
    """This tool's risk tier, straight off the table - UNKNOWN_TIER if
    `name` isn't in it. Deliberately simple/pure (no registry lookup): the
    one thing this answers is "has this specific name been through tier
    review," which is exactly what tests/test_security_tiers.py needs to
    check it against the live registry. See resolve_tier() below for what
    brain/groq_core.py's _guard() actually calls at runtime - it needs a
    softer fallback than this for a hot-loaded skill that isn't in this
    committed table by definition, but IS still registered."""
    return TIERS.get(name, UNKNOWN_TIER)


def resolve_tier(name: str, *, is_registered: bool, legacy_confirm: bool) -> int:
    """What _guard() actually calls. Three cases, not two:

    1. `name` is in TIERS - the normal case for every built-in tool -
       that reviewed tier wins, full stop.
    2. `name` isn't in TIERS but IS registered (is_registered=True) - a
       skill hot-loaded from user_skills/, which can never be in a table
       committed ahead of time. Its own confirm=True/False (legacy_confirm,
       from skills_registry.py's SkillRegistry.requires_confirm()) still
       decides between T2 and T1: that's still a real, deliberate signal
       from whoever wrote the skill, not nothing, even though it's
       coarser than a full T0-T3 review. Falling all the way back to
       UNKNOWN_TIER here - ignoring an explicit confirm=False - would
       silently change a hot-loaded skill's baseline behaviour the moment
       Tier 1 shipped, for a tool nobody touched at all; see
       tests/test_phase4_brain.py's HotSkillThroughTheBrainTests for
       exactly the regression that would have caused.
    3. `name` isn't registered anywhere at all - a typo, a stale
       reference, a name from before a rename - UNKNOWN_TIER, the blind
       fail-safe. There's no signal to fall back to here at all, so this
       is the only case that's actually "nobody classified this," which
       is what UNKNOWN_TIER's risky-by-default reasoning is for."""
    if name in TIERS:
        return TIERS[name]
    if is_registered:
        return T2 if legacy_confirm else T1
    return UNKNOWN_TIER


def label(tier: int) -> str:
    """Human-readable tier name for logs/diagnostics - never raises on an
    out-of-range int, since a corrupted/future tier value shouldn't be
    able to crash whatever is just trying to describe it."""
    return TIER_LABELS.get(tier, f"T{tier}")
