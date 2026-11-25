"""
tests/test_handlers_structure.py — Tier 4: brain/handlers/ actually holds
every built-in skill, not just at the moment of this refactor but as a
standing guardrail against it quietly drifting back.
================================================================================
The refactor itself (moving ~50 _skill_* methods out of WaveBrain into
brain/handlers/*.py, grouped by domain) was verified by hand at the time
by diffing a snapshot of every tool's spec/confirm-flag/handler name
before and after - this file is the durable, ongoing version of that
check: it fails the moment a new built-in skill gets added back inside
groq_core.py's class body instead of a handlers/ file, or moved to the
wrong domain file, rather than relying on that one-time diff having been
done correctly.

    python -m unittest discover -s tests -p "test_handlers_structure.py"
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import brain.groq_core  # noqa: E402,F401  (side effect: imports brain.handlers, registering every skill)
from brain.skills_registry import skills, BUILTIN_SOURCE          # noqa: E402

# Same grouping used when the methods were actually moved - see
# /home/claude/assemble_handlers.py's DOMAIN_MAP for where this came from
# (not committed to the repo; this is the record of it that matters).
EXPECTED_DOMAIN = {
    # automation.py
    "open_app": "automation", "open_website": "automation", "close_app": "automation",
    "close_current_window": "automation", "close_current_tab": "automation", "lock_pc": "automation",
    "undo_last_action": "automation", "list_recent_actions": "automation", "create_folder": "automation",
    "create_file": "automation", "delete_file": "automation", "search_file": "automation",
    "write_notepad": "automation", "open_system_setting": "automation", "toggle_hardware": "automation",
    # Tier 44
    "turn_wifi_on": "automation", "turn_wifi_off": "automation", "turn_bluetooth_on": "automation",
    "turn_bluetooth_off": "automation", "switch_to_dark_mode": "automation", "switch_to_light_mode": "automation",
    "send_whatsapp_message": "automation", "create_workflow": "automation", "run_workflow": "automation",
    "list_workflows": "automation", "schedule_workflow": "automation", "list_workflow_schedules": "automation",
    "cancel_workflow_schedule": "automation",
    # automation.py, Tier 8: OS service start/stop (sc.exe / systemctl / launchctl)
    "start_service": "automation", "stop_service": "automation",
    # automation.py, Tier 8: per-window management
    "list_open_windows": "automation", "focus_window": "automation",
    "minimize_window": "automation", "snap_window": "automation",
    # automation.py, Tier 5: settings-page shortcuts
    "open_display_settings": "automation", "open_sound_settings": "automation",
    "open_bluetooth_settings": "automation", "open_network_settings": "automation",
    "open_wifi_settings": "automation", "open_update_settings": "automation",
    "open_apps_settings": "automation", "open_personalization_settings": "automation",
    "open_privacy_settings": "automation", "open_storage_settings": "automation",
    "open_battery_settings": "automation", "open_power_settings": "automation",
    "open_about_settings": "automation", "open_nightlight_settings": "automation",
    "open_airplane_mode_settings": "automation",
    # automation.py, Tier 14: the rest of per-window management, continuing
    # on from focus_window/minimize_window/snap_window above, plus
    # restart_app (composes close_app+open_app).
    "move_window": "automation", "resize_window": "automation", "maximize_window": "automation",
    "restore_window": "automation", "set_always_on_top": "automation", "set_fullscreen": "automation",
    "show_desktop": "automation", "minimize_all_windows": "automation", "switch_window": "automation",
    "restart_app": "automation",
    # automation.py, Tier 14: brightness (a display setting, not audio -
    # see that file's own comment for why this sits here, not media.py).
    "set_brightness": "automation", "brightness_up": "automation", "brightness_down": "automation",
    "brightness_max": "automation", "brightness_min": "automation",
    # automation.py, Tier 14: power/session, each delayed+cancellable.
    "shutdown_pc": "automation", "restart_pc": "automation", "sleep_pc": "automation",
    "hibernate_pc": "automation", "log_off": "automation", "cancel_shutdown": "automation",
    # automation.py, Tier 15: keyboard.
    "press_key": "automation", "type_text": "automation", "copy_text": "automation",
    "paste_text": "automation", "select_all_text": "automation", "undo_keystroke": "automation",
    "redo_keystroke": "automation",
    # automation.py, Tier 15: browser control.
    "new_tab": "automation", "next_tab": "automation", "previous_tab": "automation",
    "reload_page": "automation", "zoom_in": "automation", "zoom_out": "automation",
    "zoom_reset": "automation", "scroll_up": "automation", "scroll_down": "automation",
    "scroll_to_top": "automation", "scroll_to_bottom": "automation",
    # automation.py, Tier 15: clipboard write (get_clipboard, the read side,
    # stays in system.py alongside the other plain reads).
    "set_clipboard": "automation",
    # automation.py, Tier 15: folder shortcuts + the one destructive action
    # in this group.
    "open_downloads": "automation", "open_documents": "automation", "open_pictures": "automation",
    "open_music": "automation", "open_videos": "automation", "open_this_pc": "automation",
    "open_recycle_bin": "automation", "open_task_manager": "automation",
    "open_control_panel": "automation", "empty_recycle_bin": "automation",
    # automation.py, Tier 15: download watch.
    "watch_for_download": "automation", "cancel_download_watch": "automation",
    # media.py
    "play_youtube": "media", "media_play_pause": "media", "media_next_track": "media",
    "media_previous_track": "media", "volume_up": "media", "volume_down": "media",
    # media.py, Tier 14: toggle_mute is RETIRED (see that file's own
    # comment - it used to blindly flip pycaw's mute state, which could
    # mute when asked to unmute). set_volume/volume_max/volume_min/
    # mute_volume/unmute_volume replace it.
    "set_volume": "media", "volume_max": "media", "volume_min": "media",
    "mute_volume": "media", "unmute_volume": "media",
    # media.py, Tier 16: the other "play something" command, next to
    # play_youtube above.
    "play_on_spotify": "media",
    # memory.py
    "remember_this": "memory", "recall_memory": "memory", "forget_memory": "memory",
    "why_remembered": "memory",
    # reminders.py
    "set_reminder": "reminders", "list_reminders": "reminders", "cancel_reminder": "reminders",
    # calendar.py
    "create_calendar_event": "calendar", "list_calendar_events": "calendar",
    "cancel_calendar_event": "calendar",
    # calendar.py, Tier 18: calendar sync (Google OAuth + ICS)
    "add_ics_calendar_feed": "calendar", "remove_calendar_feed": "calendar",
    "list_calendar_feeds": "calendar", "sync_calendars": "calendar", "setup_google_calendar": "calendar",
    # vision.py
    "take_screenshot": "vision", "delete_latest_screenshot": "vision", "read_screen_text": "vision",
    "take_photo": "vision", "describe_screen": "vision",   # Tier 18
    # web.py
    "search_google": "web", "get_live_information": "web", "get_weather": "web", "get_news": "web",
    # web.py, Tier 16: fetch + extract, same family as the web lookups above.
    "summarize_page": "web",
    # system.py
    "daily_briefing": "system", "run_self_diagnostics": "system", "list_my_skills": "system",
    "get_pc_info": "system", "get_clipboard": "system", "search_my_notes": "system",
    "reindex_notes": "system",
    # system.py, Tier 16: "notes study tools" - both are notes_index.search()
    # with a different prompt, same as search_my_notes just above.
    "summarize_my_notes": "system", "quiz_me_on": "system",
    # system.py, Tier 14: system info - plain reads, same un-audited style
    # as get_pc_info/get_clipboard above (see that file's own comment).
    "get_uptime": "system", "get_local_ip": "system", "get_gpu_status": "system",
    "get_cpu_temperature": "system", "get_top_processes": "system", "list_services": "system",
    "get_disk_usage": "system", "get_battery_status": "system", "get_brightness_status": "system",
    # system.py, Tier 20 (#88): "why did you do/notify" - both read
    # automation/security_log.py's ring buffer, same un-audited read-only
    # style as the rest of this file.
    "why_did_you_do_that": "system", "why_did_you_notify_me": "system",
    # timers.py, Tier 5: added as its own 9th domain file - see that
    # module's docstring for why timers/stopwatch don't belong in
    # reminders.py despite a timer being built on the reminders table.
    "set_timer": "timers", "list_timers": "timers", "check_timer": "timers", "cancel_timer": "timers",
    "start_stopwatch": "timers", "stop_stopwatch": "timers", "lap_stopwatch": "timers",
    "stopwatch_status": "timers",
    # fun.py, Tier 8: added as its own 10th domain file - get_joke and the
    # streak tools don't fit any of the other 9 domains (see fun.py's own
    # module docstring for why these are plain raw tools rather than
    # routed through skills/).
    "get_joke": "fun", "check_in_streak": "fun", "get_streak_status": "fun", "reset_streak": "fun",
    # productivity.py, Tier 16: added as its own 11th domain file - quick
    # notes, to-dos, the calculator, modes, and deterministic time/date,
    # each its own small feature with nowhere else to live (see that
    # file's own module docstring). summarize_my_notes/quiz_me_on,
    # summarize_page, and play_on_spotify land in system.py/web.py/media.py
    # instead, next to the existing tools they're each closest to - see
    # those three entries lower down in this same map.
    "take_note": "productivity", "read_notes": "productivity", "clear_notes": "productivity",
    "add_todo": "productivity", "list_todos": "productivity", "complete_todo": "productivity",
    "delete_todo": "productivity", "calculate": "productivity", "convert_units": "productivity",
    "enable_mode": "productivity", "disable_mode": "productivity", "get_active_mode": "productivity",
    "get_current_time": "productivity", "get_current_date": "productivity",
    # Tier 25: the avatar companion's two UI-visibility skills.
    "show_ui": "avatar", "hide_ui": "avatar",
}

# Tier 5: the settings-page shortcuts are all zero-required-argument wrappers
# around AppController.open_system_setting() - each one's own name encodes
# exactly which key it should pass. Kept as a separate dict (rather than
# baked into EXPECTED_DOMAIN) since it's a different fact about each tool
# (which key it passes) than EXPECTED_DOMAIN tracks (which file it lives in).
SETTINGS_SHORTCUT_KEYS = {
    "open_display_settings": "display", "open_sound_settings": "sound",
    "open_bluetooth_settings": "bluetooth", "open_network_settings": "network",
    "open_wifi_settings": "wifi", "open_update_settings": "update",
    "open_apps_settings": "apps", "open_personalization_settings": "personalization",
    "open_privacy_settings": "privacy", "open_storage_settings": "storage",
    "open_battery_settings": "battery", "open_power_settings": "power",
    "open_about_settings": "about", "open_nightlight_settings": "nightlight",
    "open_airplane_mode_settings": "airplane",
}


class HandlersStructureTests(unittest.TestCase):
    def test_registry_matches_expected_domain_mapping_exactly(self):
        """Tier 4 pinned this at a literal 50 (the exact count at the moment
        of that refactor); Tier 5 adds 23 more built-in tools on top (15
        settings-page shortcuts + 8 timer/stopwatch tools), and Tier 8 adds
        10 more: get_joke/check_in_streak/get_streak_status/reset_streak in
        the new fun.py, and start_service/stop_service/list_open_windows/
        focus_window/minimize_window/snap_window in automation.py. Tier 14
        adds ~34 more net (window-management extensions, brightness, and
        power/session in automation.py; set_volume/volume_max/volume_min/
        mute_volume/unmute_volume in media.py, replacing the one retired
        toggle_mute; 9 system-info reads in system.py). Tier 15 adds 31
        more, all in automation.py: 7 keyboard, 11 browser-control, 1
        clipboard write, 9 folder shortcuts, 1 destructive (empty_recycle_bin),
        and 2 download-watch. Tier 16 adds 18 more: 14 in the new
        productivity.py (quick notes, to-dos, calculator, modes,
        deterministic time/date), plus summarize_my_notes/quiz_me_on in
        system.py, summarize_page in web.py, and play_on_spotify in
        media.py. Tier 18 adds 9 more so far: describe_screen in vision.py;
        schedule_workflow/list_workflow_schedules/cancel_workflow_schedule
        in automation.py; and add_ics_calendar_feed/remove_calendar_feed/
        list_calendar_feeds/sync_calendars/setup_google_calendar in
        calendar.py. Tier 25 adds 2 more: show_ui/hide_ui in the new
        avatar.py. So the
        meaningful, lasting check is "the live registry and this file's own
        map of every tool agree, exactly" - not any one count, which would
        otherwise need bumping by hand forever as tools are added. The
        count is still surfaced in the failure message for a quick sanity
        read when this does fail."""
        names = set(skills.names_by_source(BUILTIN_SOURCE))
        self.assertEqual(names, set(EXPECTED_DOMAIN),
                         f"count: {len(names)} registered vs {len(EXPECTED_DOMAIN)} expected")

    def test_no_skill_handler_lives_in_groq_core_py_anymore(self):
        """The actual point of Tier 4: groq_core.py keeps think()/
        think_stream()/_dispatch_tool() (and friends), but none of the
        50 tool implementations themselves. A new @skill(...) method
        accidentally added back inside the WaveBrain class body is
        exactly what this is here to catch."""
        offenders = [name for name in EXPECTED_DOMAIN
                     if skills.get_handler(name).__module__ == "brain.groq_core"]
        self.assertEqual(offenders, [], f"still defined in groq_core.py: {offenders}")

    def test_every_handler_lives_in_its_expected_domain_file(self):
        wrong = {}
        for name, domain in EXPECTED_DOMAIN.items():
            actual_module = skills.get_handler(name).__module__
            expected_module = f"brain.handlers.{domain}"
            if actual_module != expected_module:
                wrong[name] = actual_module
        self.assertEqual(wrong, {}, f"handler(s) not in their expected domain file: {wrong}")

    def test_every_handler_function_is_still_named_after_its_skill(self):
        """Not load-bearing for dispatch (get_handler(name) is what's
        actually called - see brain/skills_registry.py), but a renamed
        handler function would be a sign something was copy-pasted
        wrong during a future edit, so it's cheap to keep pinned."""
        mismatched = {name: skills.get_handler(name).__name__ for name in EXPECTED_DOMAIN
                     if skills.get_handler(name).__name__ != f"_skill_{name}"}
        self.assertEqual(mismatched, {})

    def test_each_handler_takes_brain_and_args_not_self_and_args(self):
        """The actual mechanical change this refactor depended on being
        safe: brain/skills_registry.py's register() has always documented
        handlers as plain fn(brain, args) functions (never bound methods -
        both _dispatch_tool() and _replay_confirmed() call handler(self,
        args) explicitly), so a handler's first parameter name should read
        as `brain` now that it's a free function, not `self`."""
        import inspect
        wrong = {}
        for name in EXPECTED_DOMAIN:
            params = list(inspect.signature(skills.get_handler(name)).parameters)
            if params[:1] != ["brain"]:
                wrong[name] = params
        self.assertEqual(wrong, {})


class SettingsPagesShortcutTests(unittest.TestCase):
    """Tier 5: each open_*_settings tool is a one-line wrapper that must
    pass AppController.open_system_setting() the ONE specific key its own
    name promises - a copy-pasted body that actually opens a different
    page than its name/description claim would be silently wrong in a way
    none of the HandlersStructureTests checks above would catch (they only
    check domain/naming/signature, never behavior)."""

    def test_each_shortcut_passes_its_own_exact_key(self):
        from unittest import mock
        wrong = {}
        for name, key in SETTINGS_SHORTCUT_KEYS.items():
            handler = skills.get_handler(name)
            with mock.patch("brain.handlers.automation.AppController.open_system_setting",
                            return_value="ok") as mocked:
                handler(None, {})
                called_with = mocked.call_args.args[0] if mocked.call_args.args else None
                if called_with != key:
                    wrong[name] = called_with
        self.assertEqual(wrong, {}, f"shortcut(s) passing the wrong key: {wrong}")

    def test_every_shortcut_takes_no_required_arguments(self):
        """These are meant to be picked directly by the model with zero
        slot-filling - a required argument creeping in would defeat the
        point of having a dedicated tool per settings page at all."""
        non_empty = {name: skills.get_spec(name)["function"]["parameters"]["required"]
                     for name in SETTINGS_SHORTCUT_KEYS
                     if skills.get_spec(name)["function"]["parameters"]["required"]}
        self.assertEqual(non_empty, {})


if __name__ == "__main__":
    unittest.main()
