"""
brain/handlers/automation.py - app/window/file/hardware control, plus the
undo/audit-log and workflow tools (run_workflow replays other tools through
WaveBrain.process_actions(), which is why it's the one handler here that
takes `brain` as more than a dispatch formality).

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown to
1942+ lines with 50 _skill_* methods - a "no behavior change, pure refactor" per
the roadmap. Each function here is registered via the exact same @skill(...)
decorator at module-import time (brain/skills_registry.py's register() has
always documented its handlers as plain `fn(brain, args: dict) -> str` functions,
not bound methods - both _dispatch_tool() and _replay_confirmed() already call
handler(self, args) with the WaveBrain instance passed explicitly, so moving a
method out of the class into a plain top-level function taking `brain` instead of
`self` needed no change to either call site). See brain/handlers/__init__.py for
why these 8 files, and groq_core.py's own top for why it still imports this
package even though nothing in it is referenced by name.
"""
import datetime

from automation.app_control import AppController
from automation.file_manager import FileManager
from automation.audit import audit
from automation.undo import undo_manager
from automation.workflow_scheduler import WorkflowScheduler
from brain.skills_registry import skill
from database.db_manager import db


def _relative_time(ts: str) -> str:
    """Turns an ISO timestamp into the kind of relative phrase a person would
    actually say - 'just now', '2 minutes ago', 'an hour ago', 'yesterday at
    3:15 PM', 'on Mar 4 at 9:02 AM' - instead of a raw timestamp or a bare
    '%H:%M' that silently stops making sense once an action is more than a
    day old (Tier 5: list_recent_actions polish)."""
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ts or "sometime"
    now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
    seconds = max(0.0, (now - dt).total_seconds())
    if seconds < 45:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if dt.date() == now.date():
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    clock = dt.strftime("%I:%M %p").lstrip("0")
    if dt.date() == (now - datetime.timedelta(days=1)).date():
        return f"yesterday at {clock}"
    return f"on {dt.strftime('%b')} {dt.day} at {clock}"


def _parses_to_today(ts: str, today: datetime.date) -> bool:
    try:
        return datetime.datetime.fromisoformat(ts).date() == today
    except (TypeError, ValueError):
        return False


def _group_consecutive(records):
    """Collapses runs of the same (tool, status, summary) into one entry with
    a count, so e.g. five back-to-back volume-up presses read as 'Volume up
    (x5)' instead of five near-identical lines."""
    groups = []
    for r in records:
        key = (r.tool, r.status, r.summary())
        if groups and groups[-1][0] == key:
            groups[-1][2] += 1
        else:
            groups.append([key, r, 1])
    return [(r, n) for _key, r, n in groups]


@skill("open_app", "Opens any application, program, game, or utility by name (e.g. Chrome, VS Code, Spotify, Notepad, Calculator). Do NOT use this for websites/searches — use open_website for anything involving a website, a search query, or a named browser.",
       {"app_name": {"type": "string", "description": "Name of the app to open."}}, ["app_name"])
def _skill_open_app(brain, args: dict) -> str:
    return AppController.open_app(args.get("app_name", ""))


@skill("open_website", "Opens a website, optionally searching it for something, optionally in a SPECIFIC named browser. ALWAYS use this (never open_app) whenever the command names a browser (e.g. 'on brave', 'in chrome', 'using edge') and/or a website to search (e.g. 'search youtube for cats', 'google search for X', 'search youtube on brave'). Both 'site' and 'browser' can be given together in the same call — never pick just one and drop the other.",
       {"site": {"type": "string", "description": "The website to open, e.g. 'youtube', 'google', 'amazon', or a raw URL."},
        "query": {"type": "string", "description": "Optional — what to search for on that site. Omit if the user just wants the site opened with no search."},
        "browser": {"type": "string", "description": "Optional — a specific browser named in the command, e.g. 'chrome', 'brave', 'edge', 'firefox'. Omit entirely if no browser was named."}},
       ["site"])
def _skill_open_website(brain, args: dict) -> str:
    return AppController.open_website(args.get("site", ""), args.get("query"), args.get("browser"))


@skill("close_app", "Closes a running application by name.",
       {"app_name": {"type": "string", "description": "Name of the app to close."}}, ["app_name"],
       confirm=True)
def _skill_close_app(brain, args: dict) -> str:
    return AppController.close_app(args.get("app_name", ""))


@skill("close_current_window", "Closes the currently focused window (Alt+F4).")
def _skill_close_current_window(brain, args: dict) -> str:
    return AppController.close_current_window()


@skill("close_current_tab", "Closes the current browser tab (Ctrl+W).")
def _skill_close_current_tab(brain, args: dict) -> str:
    return AppController.close_current_tab()


# Tier 8: per-window management - a SPECIFIC window by name, unlike
# close_current_window/close_current_tab above (whatever already has focus).
@skill("list_open_windows", "Lists the titles of every currently open window. Use for 'what windows are "
       "open', 'what do I have open right now', or similar.")
def _skill_list_open_windows(brain, args: dict) -> str:
    return AppController.list_open_windows()


@skill("focus_window", "Switches to (brings to the front) a specific open window by name, restoring it "
       "first if it's minimized. Use open_app instead if the app may not already be running.",
       {"window_name": {"type": "string", "description": "A word or phrase from the window's title, e.g. "
                 "'notepad', 'chrome', 'spotify'."}}, ["window_name"])
def _skill_focus_window(brain, args: dict) -> str:
    return AppController.focus_window(args.get("window_name", ""))


@skill("minimize_window", "Minimizes a specific open window by name, without closing it.",
       {"window_name": {"type": "string", "description": "A word or phrase from the window's title."}},
       ["window_name"])
def _skill_minimize_window(brain, args: dict) -> str:
    return AppController.minimize_window(args.get("window_name", ""))


@skill("snap_window", "Snaps/arranges a specific open window on screen - to the left half, right half, "
       "or maximized.",
       {"window_name": {"type": "string", "description": "A word or phrase from the window's title."},
        "position": {"type": "string", "enum": ["left", "right", "maximize"],
                     "description": "Where to snap the window."}},
       ["window_name", "position"])
def _skill_snap_window(brain, args: dict) -> str:
    return AppController.snap_window(args.get("window_name", ""), args.get("position", ""))


# Tier 14: the rest of per-window management, continuing straight on from
# Tier 8's focus/minimize/snap above - same "a word or phrase from the
# title" window_name convention throughout.
_WINDOW_NAME_PARAM = {"window_name": {"type": "string", "description": "A word or phrase from the window's title."}}


@skill("move_window", "Moves a specific open window to an exact screen position, without changing its size.",
       {**_WINDOW_NAME_PARAM,
        "x": {"type": "integer", "description": "New horizontal position in pixels, from the left edge."},
        "y": {"type": "integer", "description": "New vertical position in pixels, from the top edge."}},
       ["window_name", "x", "y"])
def _skill_move_window(brain, args: dict) -> str:
    return AppController.move_window(args.get("window_name", ""), args.get("x", 0), args.get("y", 0))


@skill("resize_window", "Resizes a specific open window to an exact width and height, without moving it.",
       {**_WINDOW_NAME_PARAM,
        "width": {"type": "integer", "description": "New width in pixels."},
        "height": {"type": "integer", "description": "New height in pixels."}},
       ["window_name", "width", "height"])
def _skill_resize_window(brain, args: dict) -> str:
    return AppController.resize_window(args.get("window_name", ""), args.get("width", 0), args.get("height", 0))


@skill("maximize_window", "Maximizes a specific open window by name.", _WINDOW_NAME_PARAM, ["window_name"])
def _skill_maximize_window(brain, args: dict) -> str:
    return AppController.maximize_window(args.get("window_name", ""))


@skill("restore_window", "Restores a specific open window to its normal (not maximized, not minimized) size.",
       _WINDOW_NAME_PARAM, ["window_name"])
def _skill_restore_window(brain, args: dict) -> str:
    return AppController.restore_window(args.get("window_name", ""))


@skill("set_always_on_top", "Makes a specific open window stay on top of every other window, or turns that "
       "back off.", {**_WINDOW_NAME_PARAM,
        "enabled": {"type": "boolean", "description": "true to keep it on top, false to stop keeping it on top."}},
       ["window_name", "enabled"])
def _skill_set_always_on_top(brain, args: dict) -> str:
    return AppController.set_always_on_top(args.get("window_name", ""), args.get("enabled", True))


@skill("set_fullscreen", "Toggles fullscreen for a specific open window (sends the F11 fullscreen shortcut "
       "most apps - browsers, video/media players - respond to).", _WINDOW_NAME_PARAM, ["window_name"])
def _skill_set_fullscreen(brain, args: dict) -> str:
    return AppController.set_fullscreen(args.get("window_name", ""))


@skill("show_desktop", "Minimizes every window to show the desktop (same action as minimize_all_windows).")
def _skill_show_desktop(brain, args: dict) -> str:
    return AppController.show_desktop()


@skill("minimize_all_windows", "Minimizes every open window (same action as show_desktop).")
def _skill_minimize_all_windows(brain, args: dict) -> str:
    return AppController.minimize_all_windows()


@skill("switch_window", "Switches to the next open window (the OS's own alt-tab/cmd-tab), as opposed to "
       "focus_window's 'switch to this specific one by name'.")
def _skill_switch_window(brain, args: dict) -> str:
    return AppController.switch_window()


@skill("restart_app", "Closes an application and reopens it - use for 'restart X'/'relaunch X' rather than "
       "calling close_app then open_app separately.",
       {"app_name": {"type": "string", "description": "Name of the app to restart."}}, ["app_name"],
       confirm=True)
def _skill_restart_app(brain, args: dict) -> str:
    return AppController.restart_app(args.get("app_name", ""))


# Tier 14: brightness - same risk profile as volume just above it in
# brain/tiers.py's T1 (trivially reversible, no confirm=True needed) but
# lives here rather than media.py: it's a display setting, not audio, and
# (like the window-management tools just above) it's a plain AppController
# call with no pycaw/pyautogui-style platform helper of its own in this
# file. get_brightness_status (the read-only counterpart) is a T0 tool
# registered in brain/handlers/system.py instead, alongside the other
# plain status/introspection reads - see that file's own comment.
@skill("set_brightness", "Sets screen brightness to an exact percentage.",
       {"percent": {"type": "integer", "description": "Target brightness, 0-100."}}, ["percent"])
def _skill_set_brightness(brain, args: dict) -> str:
    return AppController.set_brightness(args.get("percent", 50))


@skill("brightness_up", "Increases screen brightness by about 10%.")
def _skill_brightness_up(brain, args: dict) -> str:
    return AppController.brightness_up()


@skill("brightness_down", "Decreases screen brightness by about 10%.")
def _skill_brightness_down(brain, args: dict) -> str:
    return AppController.brightness_down()


@skill("brightness_max", "Sets screen brightness to 100% (fully bright).")
def _skill_brightness_max(brain, args: dict) -> str:
    return AppController.brightness_max()


@skill("brightness_min", "Sets screen brightness to its lowest setting (not fully off - screens can't turn "
       "fully black this way).")
def _skill_brightness_min(brain, args: dict) -> str:
    return AppController.brightness_min()


# Tier 44: Wi-Fi / Bluetooth / the theme, switched for real - Wave used to
# only open the matching Settings page (SARA switches them). Turning a radio
# OFF asks first (brain/tiers.py T2): without Wi-Fi Wave's own AI answers
# stop, and Bluetooth off drops a Bluetooth headset - and its mic.
@skill("turn_wifi_on", "Turns Wi-Fi on.")
def _skill_turn_wifi_on(brain, args: dict) -> str:
    return AppController.set_radio("wifi", True)


@skill("turn_wifi_off", "Turns Wi-Fi off (disconnects from the internet).", confirm=True)
def _skill_turn_wifi_off(brain, args: dict) -> str:
    return AppController.set_radio("wifi", False)


@skill("turn_bluetooth_on", "Turns Bluetooth on.")
def _skill_turn_bluetooth_on(brain, args: dict) -> str:
    return AppController.set_radio("bluetooth", True)


@skill("turn_bluetooth_off", "Turns Bluetooth off (Bluetooth headphones, mics and mice disconnect).",
       confirm=True)
def _skill_turn_bluetooth_off(brain, args: dict) -> str:
    return AppController.set_radio("bluetooth", False)


@skill("switch_to_dark_mode", "Switches Windows (apps and taskbar) to dark mode.")
def _skill_switch_to_dark_mode(brain, args: dict) -> str:
    return AppController.set_dark_mode(True)


@skill("switch_to_light_mode", "Switches Windows (apps and taskbar) to light mode.")
def _skill_switch_to_light_mode(brain, args: dict) -> str:
    return AppController.set_dark_mode(False)


@skill("lock_pc", "Locks the PC/Windows session immediately.")
def _skill_lock_pc(brain, args: dict) -> str:
    return AppController.lock_pc()


# Tier 14: power/session. All five actions below are delayed (default
# POWER_ACTION_DELAY_S, settings/config.py) specifically so saying "cancel
# shutdown" in that window actually has something to catch - see
# automation/app_control.py's own module comments. confirm=True here
# matches brain/tiers.py's T3 for all five (the table is the real
# authority for a built-in tool - see resolve_tier()'s own docstring -
# this is set to match it for a consistent, legible signal at the
# registration site too, same as every other existing T2/T3 skill here).
_DELAY_PARAM = {"delay_s": {"type": "integer", "description": "Optional - how many seconds to wait before "
                "it happens (default: a few seconds, enough time to say \"cancel shutdown\"). Only set this "
                "if the user actually asked for a specific delay."}}


@skill("shutdown_pc", "Shuts down the PC after a short delay, so it can still be cancelled. Use cancel_shutdown "
       "if the user changes their mind.", _DELAY_PARAM, [], confirm=True)
def _skill_shutdown_pc(brain, args: dict) -> str:
    return AppController.shutdown_pc(args.get("delay_s"))


@skill("restart_pc", "Restarts the PC after a short delay, so it can still be cancelled. Use cancel_shutdown "
       "if the user changes their mind.", _DELAY_PARAM, [], confirm=True)
def _skill_restart_pc(brain, args: dict) -> str:
    return AppController.restart_pc(args.get("delay_s"))


@skill("sleep_pc", "Puts the PC to sleep after a short delay, so it can still be cancelled. Use cancel_shutdown "
       "if the user changes their mind.", _DELAY_PARAM, [], confirm=True)
def _skill_sleep_pc(brain, args: dict) -> str:
    return AppController.sleep_pc(args.get("delay_s"))


@skill("hibernate_pc", "Hibernates the PC after a short delay, so it can still be cancelled. Use cancel_shutdown "
       "if the user changes their mind. Not available on every OS - macOS in particular has no on-demand "
       "hibernate trigger.", _DELAY_PARAM, [], confirm=True)
def _skill_hibernate_pc(brain, args: dict) -> str:
    return AppController.hibernate_pc(args.get("delay_s"))


@skill("log_off", "Logs the current user off after a short delay, so it can still be cancelled. Use "
       "cancel_shutdown if the user changes their mind.", _DELAY_PARAM, [], confirm=True)
def _skill_log_off(brain, args: dict) -> str:
    return AppController.log_off(args.get("delay_s"))


@skill("cancel_shutdown", "Cancels a pending shutdown/restart/sleep/hibernate/log-off that's still in its "
       "delay window. Safe to call even if nothing is actually pending - it just says so.")
def _skill_cancel_shutdown(brain, args: dict) -> str:
    return AppController.cancel_shutdown()


# =============================================================================
# Tier 15: system control surface II - keyboard, browser control, clipboard
# write, folder shortcuts, and the download watcher. brain/tiers.py has the
# full risk-tier table and TAINT_DENY_TOOLS explanation for the keyboard
# group specifically; nothing below sets confirm=True except
# empty_recycle_bin, matching that table (T1 for everything else, T3 for
# that one) the same way every other skill in this file already does.
# =============================================================================
@skill("press_key", "Presses a specific key or key combination - e.g. 'enter', 'tab', 'escape', 'f5', or a "
       "chord like 'ctrl+shift+t'. Use this for a single keyboard action that doesn't fit a more specific "
       "tool - prefer copy_text/paste_text/select_all_text/undo_keystroke/redo_keystroke, or one of the "
       "browser/window tools, when one of those applies instead.",
       {"key_combo": {"type": "string", "description": "The key or chord to press, e.g. 'enter', 'f11', "
                      "'ctrl+shift+t'. Join keys held together with '+'."}}, ["key_combo"])
def _skill_press_key(brain, args: dict) -> str:
    return AppController.press_key(args.get("key_combo", ""))


@skill("type_text", "Types the given text wherever the keyboard cursor currently is, exactly as if the user "
       "had typed it themselves. Use ONLY when the user explicitly asks to type/enter specific text into "
       "whatever's focused right now - not write_notepad (which opens Notepad first) or any other app-specific tool.",
       {"text": {"type": "string", "description": "The exact text to type."}}, ["text"])
def _skill_type_text(brain, args: dict) -> str:
    return AppController.type_text(args.get("text", ""))


@skill("copy_text", "Copies whatever's currently selected, wherever the keyboard focus is (Ctrl+C / Cmd+C). "
       "Use select_all_text first if nothing is selected yet.")
def _skill_copy_text(brain, args: dict) -> str:
    return AppController.copy_text()


@skill("paste_text", "Pastes the clipboard's current contents wherever the keyboard cursor currently is "
       "(Ctrl+V / Cmd+V). Use set_clipboard first to paste specific text rather than whatever was last copied.")
def _skill_paste_text(brain, args: dict) -> str:
    return AppController.paste_text()


@skill("select_all_text", "Selects everything in whatever's currently focused (Ctrl+A / Cmd+A).")
def _skill_select_all_text(brain, args: dict) -> str:
    return AppController.select_all_text()


@skill("undo_keystroke", "Sends the standard undo keystroke (Ctrl+Z / Cmd+Z) to whatever app currently has "
       "focus. NOT the same as undo_last_action, which reverses something WAVE itself just did - this just "
       "presses the key, for undoing something inside whatever app is focused.")
def _skill_undo_keystroke(brain, args: dict) -> str:
    return AppController.undo_keystroke()


@skill("redo_keystroke", "Sends the standard redo keystroke to whatever app currently has focus (the "
       "opposite of undo_keystroke).")
def _skill_redo_keystroke(brain, args: dict) -> str:
    return AppController.redo_keystroke()


# Tier 15: browser control - all eleven just send a keyboard shortcut to
# whatever currently has focus (almost always the browser the user means,
# same assumption close_current_tab already makes) - none of these talk to
# the browser directly, so they work on any browser that honors the
# standard shortcut.
@skill("new_tab", "Opens a new browser tab (Ctrl+T / Cmd+T).")
def _skill_new_tab(brain, args: dict) -> str:
    return AppController.new_tab()


@skill("next_tab", "Switches to the next browser tab (Ctrl+Tab).")
def _skill_next_tab(brain, args: dict) -> str:
    return AppController.next_tab()


@skill("previous_tab", "Switches to the previous browser tab (Ctrl+Shift+Tab).")
def _skill_previous_tab(brain, args: dict) -> str:
    return AppController.previous_tab()


@skill("reload_page", "Reloads/refreshes the current browser page (Ctrl+R / Cmd+R).")
def _skill_reload_page(brain, args: dict) -> str:
    return AppController.reload_page()


@skill("zoom_in", "Zooms in on the current browser page.")
def _skill_zoom_in(brain, args: dict) -> str:
    return AppController.zoom_in()


@skill("zoom_out", "Zooms out on the current browser page.")
def _skill_zoom_out(brain, args: dict) -> str:
    return AppController.zoom_out()


@skill("zoom_reset", "Resets the current browser page's zoom back to 100%.")
def _skill_zoom_reset(brain, args: dict) -> str:
    return AppController.zoom_reset()


@skill("scroll_up", "Scrolls the current page up by about one screen.")
def _skill_scroll_up(brain, args: dict) -> str:
    return AppController.scroll_up()


@skill("scroll_down", "Scrolls the current page down by about one screen.")
def _skill_scroll_down(brain, args: dict) -> str:
    return AppController.scroll_down()


@skill("scroll_to_top", "Scrolls straight to the top of the current page.")
def _skill_scroll_to_top(brain, args: dict) -> str:
    return AppController.scroll_to_top()


@skill("scroll_to_bottom", "Scrolls straight to the bottom of the current page.")
def _skill_scroll_to_bottom(brain, args: dict) -> str:
    return AppController.scroll_to_bottom()


@skill("set_clipboard", "Sets the clipboard to the given text, replacing whatever was copied before. Use "
       "get_clipboard to read the clipboard back.",
       {"text": {"type": "string", "description": "The text to put on the clipboard."}}, ["text"])
def _skill_set_clipboard(brain, args: dict) -> str:
    return AppController.set_clipboard(args.get("text", ""))


# Tier 15: folder shortcuts - the five "plain path under home" ones, then
# four with no plain filesystem equivalent on every OS, then the one
# genuinely destructive action in this group.
@skill("open_downloads", "Opens the Downloads folder.")
def _skill_open_downloads(brain, args: dict) -> str:
    return AppController.open_downloads()


@skill("open_documents", "Opens the Documents folder.")
def _skill_open_documents(brain, args: dict) -> str:
    return AppController.open_documents()


@skill("open_pictures", "Opens the Pictures folder.")
def _skill_open_pictures(brain, args: dict) -> str:
    return AppController.open_pictures()


@skill("open_music", "Opens the Music folder.")
def _skill_open_music(brain, args: dict) -> str:
    return AppController.open_music()


@skill("open_videos", "Opens the Videos folder.")
def _skill_open_videos(brain, args: dict) -> str:
    return AppController.open_videos()


@skill("open_this_pc", "Opens 'This PC' / the file browser's top-level computer view (drives and devices). "
       "Not every OS has a true equivalent - Wave says plainly what it opened instead when it doesn't.")
def _skill_open_this_pc(brain, args: dict) -> str:
    return AppController.open_this_pc()


@skill("open_recycle_bin", "Opens the Recycle Bin / Trash (to look at what's in it - use empty_recycle_bin "
       "to actually empty it).")
def _skill_open_recycle_bin(brain, args: dict) -> str:
    return AppController.open_recycle_bin()


@skill("open_task_manager", "Opens Task Manager, or the closest equivalent app on this OS.")
def _skill_open_task_manager(brain, args: dict) -> str:
    return AppController.open_task_manager()


@skill("open_control_panel", "Opens Control Panel / System Settings, or the closest equivalent on this OS. "
       "Prefer open_system_setting (or one of its dedicated shortcuts like open_display_settings) when the "
       "user names a SPECIFIC settings page instead of Control Panel generally.")
def _skill_open_control_panel(brain, args: dict) -> str:
    return AppController.open_control_panel()


@skill("empty_recycle_bin", "Permanently empties the Recycle Bin / Trash. This CANNOT be undone - everything "
       "in it is gone for good, not just moved somewhere else recoverable.", confirm=True)
def _skill_empty_recycle_bin(brain, args: dict) -> str:
    return AppController.empty_recycle_bin()


@skill("watch_for_download", "Watches a folder (the Downloads folder by default) and lets the user know as "
       "soon as a new download finishes there, or after a timeout if none does. Use for things like 'tell me "
       "when my download finishes' or 'let me know when that's done downloading'.",
       {"folder": {"type": "string", "description": "Optional - which folder to watch, if not the default "
                   "Downloads folder. A plain name (e.g. 'Desktop') is resolved relative to the home folder."},
        "timeout_minutes": {"type": "number", "description": "Optional - how many minutes to keep watching "
                             "before giving up, if the user asked for a specific duration."}}, [])
def _skill_watch_for_download(brain, args: dict) -> str:
    return AppController.watch_for_download(args.get("folder"), args.get("timeout_minutes"))


@skill("cancel_download_watch", "Stops watching for a download armed by watch_for_download. Safe to call "
       "even if nothing is currently being watched - it just says so.")
def _skill_cancel_download_watch(brain, args: dict) -> str:
    return AppController.cancel_download_watch()


@skill("undo_last_action", "Reverses the most recent file/folder/screenshot change Wave made (a created file or folder, a deleted file, an overwritten file, a screenshot). Use ONLY when the user explicitly asks to undo, revert or take back what was just done.")
def _skill_undo_last_action(brain, args: dict) -> str:
    return undo_manager.undo_last(source="ai").message


@skill("list_recent_actions", "Tells the user what Wave has just done (its recent actions, newest first) and which of them can still be undone. Use for 'what did you just do?', 'what have you done?', 'abhi tak kya kiya?', 'what did you do today?', or 'what's failed?'.",
       {"count": {"type": "integer", "description": "How many recent actions to list (default 5, max 15)."},
        "filter": {"type": "string", "description": "Optional — 'today' for only today's actions, or 'failed' for only ones that didn't work. Omit for no filter."}},
       [])
def _skill_list_recent_actions(brain, args: dict) -> str:
    try:
        count = int(args.get("count") or 5)
    except (TypeError, ValueError):
        count = 5
    count = min(15, max(1, count))
    filt = (args.get("filter") or "").strip().lower()

    # Pull a bigger pool than `count` before filtering/grouping, so asking
    # for "failed actions" or "today's actions" doesn't just re-filter the
    # 5 most recent rows down to nothing when the real matches are further
    # back in the log.
    rows = audit.recent(limit=100)
    if not rows:
        return "I haven't done anything yet."

    if filt == "today":
        today = datetime.datetime.now().date()
        rows = [r for r in rows if _parses_to_today(r.ts, today)]
        if not rows:
            return "I haven't done anything today yet."
    elif filt == "failed":
        rows = [r for r in rows if r.status == "failed"]
        if not rows:
            return "Nothing's failed recently - everything I've tried has gone through."

    rows = rows[:count]
    parts = []
    for r, n in _group_consecutive(rows):
        note = " (can be undone)" if r.reversible else (" (undone)" if r.undone_at else "")
        if r.status == "failed":
            note = " (didn't work)" + note
        elif r.status == "blocked":
            note = " (blocked)" + note
        suffix = f" (x{n})" if n > 1 else ""
        parts.append(f"{_relative_time(r.ts)} - {r.summary()}{suffix}{note}")
    return "My recent actions, newest first: " + "; ".join(parts) + "."


@skill("create_folder", "Creates a new folder on the desktop.",
       {"folder_name": {"type": "string", "description": "Name for the new folder."}}, ["folder_name"])
def _skill_create_folder(brain, args: dict) -> str:
    return FileManager.create_folder(args.get("folder_name", "New Folder"))


@skill("create_file", "Creates a new empty text file on the desktop.",
       {"file_name": {"type": "string", "description": "Name for the new file, including extension."}}, ["file_name"],
       confirm=True)
def _skill_create_file(brain, args: dict) -> str:
    return FileManager.create_file(args.get("file_name", "New File.txt"))


@skill("delete_file", "Deletes a single named file from the desktop (or a given folder). Never deletes whole folders.",
       {"file_name": {"type": "string", "description": "Name of the file to delete, including extension."}}, ["file_name"],
       confirm=True)
def _skill_delete_file(brain, args: dict) -> str:
    return FileManager.delete_file(args.get("file_name", ""))


@skill("search_file", "Searches for a file by name across common folders (Desktop, Documents, Downloads).",
       {"file_name": {"type": "string", "description": "Full or partial file name to search for."}}, ["file_name"])
def _skill_search_file(brain, args: dict) -> str:
    return FileManager.search_file(args.get("file_name", ""))


@skill("write_notepad", "Opens Notepad and types the given text into it.",
       {"text": {"type": "string", "description": "The text to type."}}, ["text"])
def _skill_write_notepad(brain, args: dict) -> str:
    return AppController.write_in_notepad(args.get("text", ""))


@skill("open_system_setting", "Opens a system settings page by name - a fallback for when the user names a settings "
       "page that doesn't have its own dedicated open_*_settings tool below. Valid keys: display, sound, "
       "bluetooth, network, wifi, update, apps, personalization, privacy, storage, battery, power, about, "
       "nightlight, airplane, or settings (the settings app's home page). Prefer the dedicated open_*_settings "
       "tool when one matches - this one exists for anything those don't cover.",
       {"setting": {"type": "string", "description": "Which settings page to open - see the valid keys above."}}, ["setting"])
def _skill_open_system_setting(brain, args: dict) -> str:
    return AppController.open_system_setting(args.get("setting", "settings"))


# Tier 5: ~15 dedicated, zero-argument settings shortcuts (roadmap: "a full
# settings-pages shortcut set (~15 separate 'open X settings' intents in Sara
# vs a handful in Wave)"). Each is a one-line wrapper around the SAME
# AppController.open_system_setting() every platform backend already
# implements (see automation/backends/*.py's expanded SETTINGS_MAP/_PANES/
# _GNOME_PANELS) - this tier adds no new mechanism, just gives the model a
# direct, no-argument tool to pick for each page instead of having to fill in
# open_system_setting's free-text `setting` argument correctly every time.
@skill("open_display_settings", "Opens Display settings (resolution, brightness, multiple monitors).")
def _skill_open_display_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("display")


@skill("open_sound_settings", "Opens Sound settings (speakers, microphone, volume mixer).")
def _skill_open_sound_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("sound")


@skill("open_bluetooth_settings", "Opens Bluetooth settings (pair/manage Bluetooth devices).")
def _skill_open_bluetooth_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("bluetooth")


@skill("open_network_settings", "Opens Network settings (Ethernet, VPN, network status).")
def _skill_open_network_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("network")


@skill("open_wifi_settings", "Opens Wi-Fi settings (see/join networks, forget a network).")
def _skill_open_wifi_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("wifi")


@skill("open_update_settings", "Opens Windows/system Update settings (check for and install updates).")
def _skill_open_update_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("update")


@skill("open_apps_settings", "Opens Apps & Features settings (installed apps, uninstall, default apps).")
def _skill_open_apps_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("apps")


@skill("open_personalization_settings", "Opens Personalization settings (background, colors, themes, lock screen).")
def _skill_open_personalization_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("personalization")


@skill("open_privacy_settings", "Opens Privacy settings (app permissions - camera, microphone, location, etc).")
def _skill_open_privacy_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("privacy")


@skill("open_storage_settings", "Opens Storage settings (disk space usage, Storage Sense).")
def _skill_open_storage_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("storage")


@skill("open_battery_settings", "Opens Battery settings (battery saver, battery usage/health).")
def _skill_open_battery_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("battery")


@skill("open_power_settings", "Opens Power & sleep settings (sleep timers, power plans).")
def _skill_open_power_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("power")


@skill("open_about_settings", "Opens the About page (device name, specs, OS build/version).")
def _skill_open_about_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("about")


@skill("open_nightlight_settings", "Opens Night light settings (warm-toned display at night).")
def _skill_open_nightlight_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("nightlight")


@skill("open_airplane_mode_settings", "Opens Airplane mode / Network settings to toggle airplane mode.")
def _skill_open_airplane_mode_settings(brain, args: dict) -> str:
    return AppController.open_system_setting("airplane")


@skill("toggle_hardware", "Turns the mobile hotspot on or off (for Wi-Fi or Bluetooth use turn_wifi_on / "
       "turn_wifi_off / turn_bluetooth_on / turn_bluetooth_off).",
       {"instruction": {"type": "string", "description": "e.g. 'turn on wifi', 'turn off bluetooth'."}}, ["instruction"])
def _skill_toggle_hardware(brain, args: dict) -> str:
    return AppController.toggle_hardware(args.get("instruction", ""))


# Tier 8: start/stop a named OS-level background service - a different
# thing from open_app/close_app above (an ordinary application window).
# Per-platform mechanism lives behind the backend (automation/backends/) -
# sc.exe on Windows, systemctl on Linux, launchctl on macOS.
@skill("start_service", "Starts a named OS-level background service (NOT a regular application) - a "
       "Windows service, a Linux systemd service, or a macOS launchd job. Use open_app instead for an "
       "ordinary application the user wants opened.",
       {"service_name": {"type": "string", "description": "The service's name as its own OS service "
                 "manager knows it - a Windows service name, a systemd unit like 'sshd', or a macOS "
                 "launchd label like 'com.apple.bluetoothd'."}},
       ["service_name"])
def _skill_start_service(brain, args: dict) -> str:
    return AppController.start_service(args.get("service_name", ""))


@skill("stop_service", "Stops a named OS-level background service (NOT a regular application) - a "
       "Windows service, a Linux systemd service, or a macOS launchd job. Use close_app instead for an "
       "ordinary application the user wants closed.",
       {"service_name": {"type": "string", "description": "The service's name as its own OS service "
                 "manager knows it - a Windows service name, a systemd unit like 'sshd', or a macOS "
                 "launchd label like 'com.apple.bluetoothd'."}},
       ["service_name"])
def _skill_stop_service(brain, args: dict) -> str:
    return AppController.stop_service(args.get("service_name", ""))


@skill("send_whatsapp_message", "Sends a WhatsApp message to a contact. This already opens/focuses WhatsApp itself if needed - never call open_app for WhatsApp first.",
       {"contact_name": {"type": "string"}, "message": {"type": "string"}}, ["contact_name", "message"],
       confirm=True)
def _skill_send_whatsapp_message(brain, args: dict) -> str:
    return AppController.send_whatsapp_message(args.get("contact_name", ""), args.get("message", ""))


@skill("create_workflow", "Saves a named sequence of commands as a reusable routine/workflow, to be replayed later in one go with run_workflow.",
       {"name": {"type": "string", "description": "A short name for the routine, e.g. 'good morning'."},
        "actions": {"type": "array", "items": {"type": "string"},
                    "description": "Ordered list of commands to run, each phrased like a normal voice command, e.g. ['open spotify', 'open vs code', 'what is the weather today']."}},
       ["name", "actions"])
def _skill_create_workflow(brain, args: dict) -> str:
    wf_name = args.get("name", "").strip()
    actions = args.get("actions", [])
    if not wf_name or not actions:
        return "A workflow needs both a name and at least one action."
    return f"Saved the '{wf_name}' workflow with {len(actions)} step(s)." if db.save_workflow(wf_name, actions) else "Couldn't save that workflow."


@skill("run_workflow", "Runs every command saved in a previously created workflow, in order.",
       {"name": {"type": "string", "description": "The workflow's name, as given to create_workflow."}}, ["name"])
def _skill_run_workflow(brain, args: dict) -> str:
    wf_name = args.get("name", "").strip()
    actions = db.get_workflow(wf_name)
    if not actions:
        return f"I don't have a workflow called '{wf_name}'."
    # Reuses the same executor a single voice command already goes
    # through - no separate execution path needed. Phase 7: that's
    # exactly why a gated step inside a workflow (process_actions()
    # calling through to _guard_keyword() same as a typed command
    # would) is caught here too, with no workflow-specific gating code
    # needed - but a workflow that keeps running steps AFTER one just
    # got blocked pending confirmation would make the gate pointless
    # (silently doing everything else while pretending to have paused),
    # so replay stops right there instead of continuing past it. Only
    # one workflow step can be "the" pending confirmation at a time
    # anyway (brain.pending_action is a single slot, not a queue), which
    # is the other reason not to keep going: a second gated step later
    # in the list would just silently overwrite the first one's pending
    # request rather than queuing behind it.
    results = []
    for a in actions:
        res = brain.process_actions(a)
        if res:
            results.append(res)
        if brain.pending_action is not None:
            break
    if not results:
        return f"Ran '{wf_name}', but nothing produced a result."
    return f"Ran '{wf_name}': " + " | ".join(results)


@skill("list_workflows", "Lists the names of all saved workflows.")
def _skill_list_workflows(brain, args: dict) -> str:
    names = db.list_workflows()
    return "Saved workflows: " + ", ".join(names) if names else "You don't have any saved workflows yet."


# Tier 18: time-triggered workflows - "run this workflow every morning at
# 7", closing the README's "workflows are on-demand only" limitation. The
# actual due-checking/running happens in automation/proactive.py's
# _check_scheduled_workflows() on its own background thread (see that
# module and automation/workflow_scheduler.py); these three tools are just
# the voice/chat front door onto the same persisted workflow_schedules
# table - create/list/cancel, same shape as set_reminder/list_reminders/
# cancel_reminder right below them.
@skill("schedule_workflow",
       "Schedules a previously-created workflow to run automatically at a given time, optionally repeating. "
       "Use for 'run my morning routine every day at 7am' or 'run the backup workflow next Monday at 9'.",
       {"name": {"type": "string", "description": "The workflow's name, as given to create_workflow."},
        "when_text": {"type": "string",
                      "description": "When it should (first) run, e.g. '7am', 'tomorrow at 9am', 'next monday 8pm'."},
        "repeat": {"type": "string", "enum": ["once", "daily", "weekly"],
                  "description": "How often to repeat. Defaults to 'once' if omitted."}},
       ["name", "when_text"])
def _skill_schedule_workflow(brain, args: dict) -> str:
    return WorkflowScheduler.schedule_workflow(args.get("name", ""), args.get("when_text", ""),
                                                args.get("repeat", "once"))


@skill("list_workflow_schedules", "Lists all scheduled (time-triggered) workflows and when they next run.")
def _skill_list_workflow_schedules(brain, args: dict) -> str:
    return WorkflowScheduler.list_schedules()


@skill("cancel_workflow_schedule", "Cancels a workflow's schedule(s), so it goes back to running on-demand only.",
       {"name": {"type": "string", "description": "The scheduled workflow's name."}}, ["name"])
def _skill_cancel_workflow_schedule(brain, args: dict) -> str:
    return WorkflowScheduler.cancel_schedule(args.get("name", ""))
