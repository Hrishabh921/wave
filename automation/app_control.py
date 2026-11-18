import os
import psutil
import logging
import threading
import time
import urllib.parse
import pyautogui
import webbrowser

from automation import audit as audit_mod
from automation import undo as undo_mod
from automation.backends import get_backend
from automation.backends.base import NATIVE_SHUTDOWN_CANCELLED_MSG, NotSupported
from automation.proactive import proactive_engine
from settings.config import config
from vision.screen_capture import _enforce_retention

# Tier 15: set_clipboard needs to WRITE the clipboard - brain/handlers/
# system.py's get_clipboard already guards the same optional dependency
# for reading it; same defensive import here even though requirements.txt
# does list pyperclip, matching that existing precedent rather than
# assuming every install actually has it.
try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

logger = logging.getLogger("AppControl")

# Phase 4: AppController decides WHAT to do (which site, which process, what to say
# back); everything that is really "how this operating system does it" lives behind
# automation/backends (Windows today, Linux/macOS reference backends alongside).
# Every public action below is also written to the audit log (automation/audit.py).

# 🔧 FIX: screenshots the USER explicitly asked for (via take_screenshot()
# below) get this prefix, which is deliberately different from vision
# module's INTERNAL_SCREENSHOT_PREFIX ("wave_internal_") even though both
# save into the same screenshots/ folder. This is what lets
# delete_latest_screenshot() safely target only the user's own files.
USER_SCREENSHOT_PREFIX = "Wave_"
# User screenshots are taken on purpose, one at a time, so a more generous
# cap than the internal captures makes sense - this just stops the folder
# from growing forever if someone forgets to clean up manually.
MAX_USER_SCREENSHOTS = 50

# 🚨 NEW: known websites (open as-is) and ones that also support a direct
# search-results URL (used when the user gives a search query too).
_SITE_URLS = {
    "youtube": "https://www.youtube.com", "google": "https://www.google.com",
    "facebook": "https://www.facebook.com", "instagram": "https://www.instagram.com",
    "twitter": "https://www.twitter.com", "x": "https://www.x.com",
    "github": "https://www.github.com", "amazon": "https://www.amazon.com",
    "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com", "whatsapp": "https://web.whatsapp.com",
    "gmail": "https://mail.google.com", "maps": "https://maps.google.com",
    "wikipedia": "https://www.wikipedia.org", "linkedin": "https://www.linkedin.com",
}
_SEARCH_URL_TEMPLATES = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "amazon": "https://www.amazon.com/s?k={q}",
    "github": "https://github.com/search?q={q}",
    "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={q}",
    "reddit": "https://www.reddit.com/search/?q={q}",
    # Tier 16: "spotify" was already a known SITE (opens open.spotify.com
    # bare) but had no search template, so a query given alongside it was
    # silently dropped - play_on_spotify (brain/handlers/web.py) is the
    # first caller that actually passes one.
    "spotify": "https://open.spotify.com/search/{q}",
}

# Tier 16: named bundles of settings Tier 14 already made real tools for
# (volume, mute, brightness) - not a new capability, just a reusable
# combination of ones that already existed, applied together as ONE
# audited/undoable action. Deliberately limited to these three well-
# understood, trivially-reversible settings - no attempt to also guess
# which open windows/apps are "distracting" and close them, which would
# need a judgment call about the user's own work this project has no safe
# way to make. "focus" matches the pre-existing Settings-page Focus Mode
# button (ui/main_window.py's toggle_focus) exactly - mute only - so
# switching that GUI toggle and saying "focus mode on" do the same thing.
# Each of the other three is deliberately a distinct, defensible
# combination: study is quiet-but-not-silent (background audio is normal
# while studying) and a bit brighter for reading comfort; gaming is loud
# and fully bright; dnd is the "wind-down" pair - silent AND dim.
_MODE_BUNDLES = {
    "focus": {"mute": True},
    "study": {"mute": False, "volume": 20, "brightness": 75},
    "gaming": {"mute": False, "volume": 100, "brightness": 100},
    "dnd": {"mute": True, "brightness": 15},
    # Roadmap names this bundle "sleep/DND" - one bundle, two names a user
    # might reach for ("sleep mode on" and "dnd mode on" both mean the same
    # silent-and-dim wind-down combination) - not a second, different mode.
    "sleep": {"mute": True, "brightness": 15},
}
_MODE_NAMES = ", ".join(sorted(_MODE_BUNDLES))
# Tracks whichever mode is currently on, plus the exact settings snapshot
# to restore - module-level, same "a tiny piece of real state" shape as
# automation/proactive.py's own _download_watch. Deliberately NOT the
# audit log's "last reversible action" (which disable_mode could instead
# have looked up and replayed via undo_mod.undo_manager) - anything else
# reversible the user does after enabling a mode would make that lookup
# point at the wrong action; keeping this tier's own small, explicit
# snapshot is simpler and can't go stale that way.
_active_mode = {"name": None, "snapshot": None}

# Tier 15: press_key's own content-based refusals - deliberately a short,
# specific list of chords with a real, well-known way to cause harm
# (launch an arbitrary program, hit a security surface), not a general
# "guess what's dangerous" filter. Lives at module level next to the other
# lookup tables above, same reasoning as close_app's hardcoded critical-
# process list: a safety check that depends on the actual argument belongs
# next to/inside the tool, not in the generic tier/taint guard (which only
# ever looks at tool identity - see brain/tiers.py's TAINT_DENY_TOOLS).
_ALWAYS_BLOCKED_CHORDS = frozenset({
    "win+r", "super+r",                  # Windows/Linux Run dialog - launches arbitrary programs by name
    "ctrl+alt+del", "ctrl+alt+delete",    # the security-options screen
})
# These specifically close the active window/tab (Alt+F4 on Windows/Linux,
# Cmd+W on macOS - see close_current_window's own backend.close_window()
# implementations) - refused only when the active window LOOKS system-ish,
# not always: closing an ordinary app window this way is exactly what
# close_current_window already does on purpose.
_WINDOW_CLOSE_CHORDS = frozenset({"alt+f4", "command+w", "cmd+w"})
_SYSTEM_WINDOW_TITLE_KEYWORDS = (
    "task manager", "control panel", "settings", "system configuration",
    "registry editor", "command prompt", "windows powershell", "terminal",
    "windows security", "device manager", "group policy",
)


def _audit():
    return audit_mod.audit


# 🚨 NEW (Tier 6): open_website()'s raw-URL branch below used to hand
# site_raw to the backend with nothing beyond a startswith("http://"/
# "https://") string check - enough to pick the branch, not enough to
# call it validated. This is NOT web/web_ops.py's validate_url() (that
# guards Wave's own SERVER-SIDE fetches against SSRF to internal/metadata
# IPs, e.g. _fetch_weather()) - open_website() hands the URL to the
# PERSON'S OWN browser, launched locally, where opening
# http://192.168.1.1 or http://localhost:3000 is a completely ordinary,
# intended thing to ask for. So this only confirms the URL actually
# parses to exactly http/https with a real host, before it's handed to a
# backend that will launch a real browser against it - rejecting
# malformed input or a stray scheme that slipped past the startswith()
# check through a parsing quirk.
def _is_safe_http_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# 🚨 NEW (Tier 6): close_app() below already limits itself to a single
# candidate process (see the 🔧 FIX comment in close_app for why), but had
# nothing stopping that candidate from BEING a critical system/shell
# process if the matched name happened to line up with one -
# close_app("explorer") would exact-match and kill explorer.exe, the
# Windows shell itself. Names are matched the same way close_app already
# normalizes them (lowercased, ".exe" stripped), so one entry covers a
# name across every platform it's spelled that way. This is a hard
# refusal, not the usual T2 confirm-gate: "close explorer" is never a
# legitimate ask (there's nothing to confirm into), and a confirmation
# prompt is itself a thing you'd lose if the shell hosting it just died.
_CRITICAL_PROCESS_NAMES = frozenset({
    # Windows
    "explorer", "winlogon", "csrss", "wininit", "services", "lsass",
    "smss", "dwm", "svchost", "system", "registry",
    # macOS
    "finder", "windowserver", "loginwindow", "kernel_task", "launchd",
    "systemuiserver", "dock",
    # Linux
    "systemd", "init", "xorg", "x11", "wayland", "gnome-shell",
    "plasmashell", "kwin", "sddm", "gdm", "lightdm",
})


# 🚨 NEW (Tier 8): start_service()/stop_service() below ask the OS's own
# service manager (sc.exe / systemctl / launchctl) to start or stop
# something by name - a DIFFERENT namespace from _CRITICAL_PROCESS_NAMES
# above, not a second copy of it (Windows' "Dnscache" SERVICE name has
# nothing to do with any single PROCESS name; "sshd" is a systemd unit,
# never something you'd close_app() by that name). Mirrors that same
# hard-refusal reasoning, but only wired into stop_service(), never
# start_service() - the same asymmetry _CRITICAL_PROCESS_NAMES itself has
# (it only ever guards close_app(), never open_app()): the dangerous
# direction is yanking something a running system already depends on out
# from under it, not (re)enabling it. See _normalize_service_name() for
# how a name is matched against this (lowercased, trailing ".service"
# stripped), so "sshd", "sshd.service" and "SSHD" all hit the same entry.
_CRITICAL_SERVICE_NAMES = frozenset({
    # Windows (sc.exe service short names)
    "rpcss", "dcomlaunch", "eventlog", "winmgmt", "plugplay", "power",
    "bfe", "mpssvc", "lanmanserver", "lanmanworkstation", "dhcp",
    "dnscache", "netman",
    # Linux (systemd unit names, ".service" stripped)
    "systemd-journald", "systemd-logind", "dbus", "systemd-udevd",
    "polkit", "sshd", "ssh", "networking", "systemd-networkd",
    "systemd-resolved", "display-manager", "systemd-timesyncd",
    # macOS (launchd labels)
    "com.apple.windowserver", "com.apple.loginwindow",
    "com.apple.systemuiserver", "com.apple.dock",
})


def _normalize_service_name(service_name: str) -> str:
    norm = (service_name or "").strip().lower()
    return norm[:-len(".service")] if norm.endswith(".service") else norm


# 🚨 NEW (Tier 14): sleep/hibernate/log-off have no native OS delay to lean
# on the way shutdown/restart do (see automation/backends/base.py's
# power/session comment) - Wave owns that delay itself with one plain
# threading.Timer, tracked here so cancel_shutdown() can call .cancel() on
# it. Module-level, not on AppController, because AppController is a pure
# classmethod facade with no instances to hold it on - the same reason
# automation/audit.py's `audit` and automation/undo.py's `trash`/
# `undo_manager` are module-level singletons rather than attributes of
# some controller instance. Only ONE such action can be pending at a time:
# scheduling a new one silently replaces (cancels) whatever was already
# waiting, rather than letting two race - the same "only the most recent
# one matters" simplicity undo_last_action's own last-reversible-wins logic
# already assumes elsewhere.
_power_timer_lock = threading.Lock()
_pending_power_timer: "threading.Timer | None" = None
_pending_power_label = ""


def _schedule_power_action(label: str, delay_s: int, fire) -> None:
    global _pending_power_timer, _pending_power_label
    with _power_timer_lock:
        if _pending_power_timer is not None:
            _pending_power_timer.cancel()
        t = threading.Timer(max(0, int(delay_s)), fire)
        t.daemon = True
        _pending_power_timer, _pending_power_label = t, label
        t.start()


def _cancel_pending_power_timer() -> str:
    """Cancels whatever Wave-owned power timer is pending, if any. Returns
    its label ("sleep"/"hibernate"/"log off") on success, "" if nothing
    was pending - act.() messages are composed from that, not from here,
    so this stays a plain, tool-agnostic helper."""
    global _pending_power_timer, _pending_power_label
    with _power_timer_lock:
        if _pending_power_timer is None:
            return ""
        _pending_power_timer.cancel()
        label, _pending_power_timer, _pending_power_label = _pending_power_label, None, ""
        return label


def _fire_power_action(backend_call) -> None:
    """Runs a scheduled sleep/hibernate/log-off's IMMEDIATE backend call once
    the timer fires - from the timer's own background thread, with nobody
    left to hand a return value to, so any failure is only ever logged,
    never raised (the user already got "sleeping in 15 seconds..." when
    this was scheduled - same one-audit-entry-at-schedule-time shape
    set_reminder/set_timer already use, not a second entry when it fires).
    Clears the pending-timer bookkeeping first so a cancel_shutdown() that
    lands in the same instant doesn't re-cancel an already-fired timer."""
    global _pending_power_timer, _pending_power_label
    with _power_timer_lock:
        _pending_power_timer, _pending_power_label = None, ""
    try:
        result = backend_call()
        logger.info(f"Scheduled power action fired: {result}")
    except Exception as e:
        logger.error(f"Scheduled power action failed: {e}")


class AppController:
    """Handles deep interaction, UI automation, and Window Controls."""

    @staticmethod
    def _backend():
        return get_backend()

    @classmethod
    def open_app(cls, app_name: str) -> str:
        app_name = app_name.lower().strip()
        with _audit().action("open_app", {"app_name": app_name}) as act:
            websites = ["youtube", "google", "facebook", "instagram", "twitter", "github"]
            for site in websites:
                if site in app_name:
                    webbrowser.open(f"https://www.{site}.com")
                    return act.ok(f"Successfully opened {site} in browser.")
            try:
                return act.done(cls._backend().launch_app(app_name))
            except NotSupported as e:
                return act.fail(str(e))

    # 🚨 NEW: handles "search <site> [for <query>] [on/in <browser>]" as ONE
    # combined action, instead of forcing open_app to guess between the
    # site name and the browser name (which is what caused "search youtube
    # on brave" to just open Brave, losing the "youtube" part).
    @classmethod
    def open_website(cls, site: str, query: str = None, browser: str = None) -> str:
        site_raw = (site or "").strip()
        site_key = site_raw.lower()

        if site_key.startswith("http://") or site_key.startswith("https://"):
            url = site_raw if _is_safe_http_url(site_raw) else None
        else:
            matched = next((k for k in _SITE_URLS if k in site_key or site_key in k), None)
            if matched and query and matched in _SEARCH_URL_TEMPLATES:
                url = _SEARCH_URL_TEMPLATES[matched].format(q=urllib.parse.quote_plus(query))
            elif matched:
                url = _SITE_URLS[matched]
            elif query:
                # Unknown site name but a search term was given — search
                # for "<query> <site>" on Google as a sane fallback.
                url = f"https://www.google.com/search?q={urllib.parse.quote_plus(query + ' ' + site_raw)}"
            else:
                url = f"https://www.{site_key.replace(' ', '')}.com"

        backend = cls._backend()
        browser_path = backend.find_browser(browser) if browser else None
        label = f"{query} on {site_raw}" if query else site_raw
        with _audit().action("open_website", {"site": site_raw, "query": query, "browser": browser}) as act:
            if url is None:
                return act.fail(f"That doesn't look like a valid, safe web address: {site_raw!r}.")
            try:
                if browser_path:
                    backend.launch_in_browser(browser_path, url)
                    return act.ok(f"Opened {label} in {browser.strip().title()}.")
                backend.open_url_default(url)
                if browser:
                    return act.ok(f"Couldn't find {browser} installed, so I opened {label} in your default browser instead.")
                return act.ok(f"Opened {label} in your default browser.")
            except Exception as e:
                logger.error(f"open_website error: {e}")
                return act.fail(f"Error opening {site_raw}.")

    @classmethod
    def toggle_hardware(cls, command: str) -> str:
        with _audit().action("toggle_hardware", {"instruction": command}) as act:
            try:
                return act.done(cls._backend().toggle_radio(command))
            except NotSupported as e:
                return act.fail(str(e))

    # 🚨 NEW (Tier 8): start/stop a named OS service via the platform
    # backend (sc.exe / systemctl / launchctl - see automation/backends/).
    # Same T2-confirm-gated posture as close_app below (brain/tiers.py),
    # for the same reason: the effect can be easy to miss in the moment
    # and only show up later as some unrelated thing quietly not working.
    @classmethod
    def start_service(cls, service_name: str) -> str:
        service_name = (service_name or "").strip()
        with _audit().action("start_service", {"service_name": service_name}) as act:
            if not service_name:
                return act.fail("I need to know which service to start.")
            try:
                return act.done(cls._backend().start_service(service_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def stop_service(cls, service_name: str) -> str:
        service_name = (service_name or "").strip()
        with _audit().action("stop_service", {"service_name": service_name}) as act:
            if not service_name:
                return act.fail("I need to know which service to stop.")
            # Hard refusal, not the usual T2 confirm-gate - see
            # _CRITICAL_SERVICE_NAMES above for why this doesn't just defer
            # to the confirm prompt the way an ordinary T2 tool would.
            if _normalize_service_name(service_name) in _CRITICAL_SERVICE_NAMES:
                return act.fail(f"'{service_name}' is a critical system service - I won't stop that.")
            try:
                return act.done(cls._backend().stop_service(service_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def close_app(cls, app_name: str) -> str:
        app_name = app_name.lower().strip()
        with _audit().action("close_app", {"app_name": app_name}) as act:
            try:
                exact_matches = []
                substring_matches = []
                for process in psutil.process_iter(['pid', 'name']):
                    proc_name = (process.info['name'] or "").lower()
                    if not proc_name:
                        continue
                    proc_stem = proc_name[:-4] if proc_name.endswith(".exe") else proc_name
                    if proc_stem == app_name:
                        exact_matches.append(process)
                    elif app_name in proc_name:
                        substring_matches.append(process)

                candidates = exact_matches or substring_matches
                if not candidates:
                    return act.fail(f"'{app_name}' doesn't appear to be running.")

                # 🔧 FIX: this used to terminate EVERY process whose name merely
                # *contained* app_name as a substring - closing something like
                # "code" could take down several unrelated processes at once.
                # Now it prefers an exact name match, and even on a substring
                # match only closes the single first one found - far less
                # collateral risk from an imprecise or misheard app name.
                target = candidates[0]

                # 🚨 NEW (Tier 6): refuse outright if that one candidate is a
                # critical system/shell process - see _CRITICAL_PROCESS_NAMES
                # above for why this doesn't just fall back to a confirm-gate.
                target_stem = (target.info['name'] or "").lower()
                target_stem = target_stem[:-4] if target_stem.endswith(".exe") else target_stem
                if target_stem in _CRITICAL_PROCESS_NAMES:
                    return act.fail(f"'{target.info['name']}' is a critical system process - I won't close that.")

                proc_label = target.info['name']
                psutil.Process(target.info['pid']).terminate()
                return act.ok(f"Closed {proc_label}.")
            except Exception as e:
                logger.error(f"close_app error: {e}")
                return act.fail(f"Error closing {app_name}.")

    @classmethod
    def close_current_window(cls) -> str:
        with _audit().action("close_current_window", {}) as act:
            try:
                cls._backend().close_window()
            except NotSupported as e:
                return act.fail(str(e))
            return act.ok("Closed the current window.")

    @classmethod
    def close_current_tab(cls) -> str:
        with _audit().action("close_current_tab", {}) as act:
            try:
                cls._backend().close_tab()
            except NotSupported as e:
                return act.fail(str(e))
            return act.ok("Closed the current tab.")

    # 🚨 NEW (Tier 8): per-window management - unlike close_current_window/
    # close_current_tab above (only ever act on whatever already has
    # focus), these target one SPECIFIC window by (a piece of) its title -
    # the same "find it by name" shape open_app/close_app already use for
    # apps. list_open_windows does its own user-facing phrasing here (the
    # backend only hands back plain titles) - same split
    # db.get_upcoming_reminders()/_skill_list_reminders() draws for
    # reminders, just inlined here rather than through a second layer,
    # matching how every other AppController method already composes its
    # own reply directly.
    @classmethod
    def list_open_windows(cls) -> str:
        with _audit().action("list_open_windows", {}) as act:
            try:
                titles = cls._backend().list_open_windows()
            except NotSupported as e:
                return act.fail(str(e))
            if not titles:
                return act.ok("No windows are currently open.")
            count = len(titles)
            return act.ok(f"You have {count} window{'s' if count != 1 else ''} open: " + "; ".join(titles) + ".")

    @classmethod
    def focus_window(cls, window_name: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("focus_window", {"window_name": window_name}) as act:
            if not window_name:
                return act.fail("I need to know which window to switch to.")
            try:
                return act.done(cls._backend().focus_window(window_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def minimize_window(cls, window_name: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("minimize_window", {"window_name": window_name}) as act:
            if not window_name:
                return act.fail("I need to know which window to minimize.")
            try:
                return act.done(cls._backend().minimize_window(window_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def snap_window(cls, window_name: str, position: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("snap_window", {"window_name": window_name, "position": position}) as act:
            if not window_name:
                return act.fail("I need to know which window to snap.")
            try:
                return act.done(cls._backend().snap_window(window_name, position))
            except NotSupported as e:
                return act.fail(str(e))

    # ---- power/session (Tier 14) ----
    # shutdown_pc/restart_pc hand delay_s straight to the backend, which
    # uses the OS's OWN delayed command (see automation/backends/base.py) -
    # safe to call synchronously since any NotSupported raises right here,
    # before anything happens. sleep_pc/hibernate_pc/log_off instead
    # SCHEDULE a plain timer and return immediately, so a failure would
    # otherwise only surface silently, 15 seconds later, in a log nobody's
    # watching - capabilities() is checked FIRST specifically so an
    # honestly-unsupported one (hibernate on macOS) fails loudly, now,
    # instead of promising something that quietly never happens.
    @classmethod
    def shutdown_pc(cls, delay_s: int = None) -> str:
        delay = max(0, int(delay_s)) if delay_s is not None else config.POWER_ACTION_DELAY_S
        with _audit().action("shutdown_pc", {"delay_s": delay}) as act:
            try:
                return act.done(cls._backend().shutdown_now(delay))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def restart_pc(cls, delay_s: int = None) -> str:
        delay = max(0, int(delay_s)) if delay_s is not None else config.POWER_ACTION_DELAY_S
        with _audit().action("restart_pc", {"delay_s": delay}) as act:
            try:
                return act.done(cls._backend().restart_now(delay))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def _schedule_delayed_power_action(cls, tool: str, label: str, feature: str, delay_s: int = None) -> str:
        delay = max(0, int(delay_s)) if delay_s is not None else config.POWER_ACTION_DELAY_S
        with _audit().action(tool, {"delay_s": delay}) as act:
            backend = cls._backend()
            if not backend.capabilities().get(feature):
                try:
                    getattr(backend, feature)()  # the real, specific reason why - raises, never acts
                except NotSupported as e:
                    return act.fail(str(e))
            _schedule_power_action(label, delay, lambda: _fire_power_action(getattr(backend, feature)))
            return act.ok(f"{label.capitalize()} in {delay} seconds. Say \"cancel shutdown\" to stop it.")

    @classmethod
    def sleep_pc(cls, delay_s: int = None) -> str:
        return cls._schedule_delayed_power_action("sleep_pc", "sleeping", "sleep_now", delay_s)

    @classmethod
    def hibernate_pc(cls, delay_s: int = None) -> str:
        return cls._schedule_delayed_power_action("hibernate_pc", "hibernating", "hibernate_now", delay_s)

    @classmethod
    def log_off(cls, delay_s: int = None) -> str:
        return cls._schedule_delayed_power_action("log_off", "logging off", "log_off_now", delay_s)

    @classmethod
    def cancel_shutdown(cls) -> str:
        with _audit().action("cancel_shutdown", {}) as act:
            cancelled = []
            timer_label = _cancel_pending_power_timer()
            if timer_label:
                cancelled.append(f"the scheduled {timer_label}")
            native_unsupported = None
            try:
                if cls._backend().cancel_native_shutdown() == NATIVE_SHUTDOWN_CANCELLED_MSG:
                    cancelled.append("the scheduled shutdown/restart")
            except NotSupported as e:
                native_unsupported = str(e)
            if cancelled:
                return act.ok(f"Cancelled {' and '.join(cancelled)}.")
            if native_unsupported:
                return act.ok(native_unsupported)
            return act.ok("There wasn't anything scheduled to cancel.")

    # ---- volume (Tier 14) ----
    # A fully audited+undoable replacement for brain/handlers/media.py's
    # older direct pycaw calls - that file's volume_up/volume_down skills
    # (and its new explicit mute_volume/unmute_volume, replacing the old
    # ambiguous toggle_mute - see its own module docstring for the bug
    # this fixes) now call these instead, so every volume change, old
    # command or new, lands on the one audited, undoable timeline. 🏆
    @classmethod
    def get_volume(cls) -> int:
        """Not audited - a plain read, same as e.g. list_open_windows's own backend call;
        only the methods that actually CHANGE the volume are."""
        return cls._backend().get_volume()

    @classmethod
    def set_volume(cls, percent: int) -> str:
        with _audit().action("set_volume", {"percent": percent}) as act:
            backend = cls._backend()
            try:
                previous = backend.get_volume()
                result = backend.set_volume(percent)
            except NotSupported as e:
                return act.fail(str(e))
            act.set_undo("restore_volume", {"percent": previous}, f"Set the volume back to {previous}%")
            return act.ok(result)

    @classmethod
    def volume_max(cls) -> str:
        return cls.set_volume(100)

    @classmethod
    def volume_min(cls) -> str:
        return cls.set_volume(0)

    @classmethod
    def _nudge_volume(cls, delta: int) -> str:
        try:
            current = cls._backend().get_volume()
            return cls.set_volume(max(0, min(100, current + delta)))
        except NotSupported:
            pass
        # Backend volume control isn't available (pycaw/pactl/amixer
        # missing, etc.) - the same media-key fallback volume_up/
        # volume_down always had, before this method existed, kept so
        # losing that dependency doesn't lose volume control entirely.
        # Still audited here directly (unlike before Tier 14) - there's
        # just no exact resulting % to report or to capture for undo this
        # way, so no act.set_undo(...). Deliberately audited under
        # volume_up/volume_down's OWN name here, NOT "set_volume" - unlike
        # the real-backend branch above, this is a direction-only nudge
        # with no known resulting percent, so it keeps the tool identity
        # (and TOOL_LABELS "Volume up"/"Volume down" wording) that best
        # matches what actually happened, and keeps a run of same-direction
        # fallback nudges grouping together in list_recent_actions the same
        # way a run of real set_volume calls does.
        tool = "volume_up" if delta > 0 else "volume_down"
        with _audit().action(tool, {"via": "media_key"}) as act:
            pyautogui.press('volumeup' if delta > 0 else 'volumedown')
            return act.ok(f"Turned the volume {'up' if delta > 0 else 'down'}.")

    @classmethod
    def volume_up(cls) -> str:
        return cls._nudge_volume(10)

    @classmethod
    def volume_down(cls) -> str:
        return cls._nudge_volume(-10)

    @classmethod
    def _set_mute_state(cls, tool: str, mute: bool) -> str:
        with _audit().action(tool, {}) as act:
            backend = cls._backend()
            try:
                was_muted = backend.is_muted()
                result = backend.mute_volume() if mute else backend.unmute_volume()
            except NotSupported as e:
                return act.fail(str(e))
            # Captured prior state, not a blind "undo = the opposite call" -
            # mute while already muted is a no-op, and a blind flip would
            # wrongly unmute a PC that was muted before this command too.
            act.set_undo("restore_mute_state", {"was_muted": was_muted},
                         f"{'Mute' if was_muted else 'Unmute'} it again")
            return act.ok(result)

    @classmethod
    def mute_volume(cls) -> str:
        return cls._set_mute_state("mute_volume", True)

    @classmethod
    def unmute_volume(cls) -> str:
        return cls._set_mute_state("unmute_volume", False)

    # ---- brightness (Tier 14) ----
    @classmethod
    def get_brightness_status(cls) -> str:
        with _audit().action("get_brightness_status", {}) as act:
            try:
                return act.ok(f"Screen brightness is at {cls._backend().get_brightness()}%.")
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def set_brightness(cls, percent: int) -> str:
        with _audit().action("set_brightness", {"percent": percent}) as act:
            backend = cls._backend()
            try:
                previous = backend.get_brightness()
                result = backend.set_brightness(percent)
            except NotSupported as e:
                return act.fail(str(e))
            act.set_undo("restore_brightness", {"percent": previous}, f"Set brightness back to {previous}%")
            return act.ok(result)

    @classmethod
    def _nudge_brightness(cls, delta: int) -> str:
        # Unlike _nudge_volume, there's no universal "brightness up/down"
        # media key to fall back on if the backend can't read the current
        # level, so this just fails outright - but still audited under
        # brightness_up/brightness_down's OWN name (not set_brightness),
        # same reasoning as _nudge_volume's fallback: keeps same-direction
        # failures grouping correctly in list_recent_actions instead of a
        # "delta" sign nobody's looking at silently merging opposite
        # directions into one summary.
        tool = "brightness_up" if delta > 0 else "brightness_down"
        try:
            current = cls._backend().get_brightness()
        except NotSupported as e:
            with _audit().action(tool, {}) as act:
                return act.fail(str(e))
        return cls.set_brightness(max(0, min(100, current + delta)))

    @classmethod
    def brightness_up(cls) -> str:
        return cls._nudge_brightness(10)

    @classmethod
    def brightness_down(cls) -> str:
        return cls._nudge_brightness(-10)

    @classmethod
    def brightness_max(cls) -> str:
        return cls.set_brightness(100)

    @classmethod
    def brightness_min(cls) -> str:
        return cls.set_brightness(0)

    # ---- modes (Tier 16) - see _MODE_BUNDLES' own comment above ----
    @classmethod
    def enable_mode(cls, name: str) -> str:
        key = (name or "").strip().lower()
        bundle = _MODE_BUNDLES.get(key)
        if bundle is None:
            return f"I don't have a '{name}' mode - I know {_MODE_NAMES}."
        with _audit().action("enable_mode", {"mode": key}) as act:
            backend = cls._backend()
            # Snapshot whatever's genuinely true RIGHT NOW, before touching
            # anything - if a different mode was already on, this correctly
            # captures ITS settings (not some imagined "original" state from
            # before any mode at all), so disable_mode always restores
            # exactly what was really there a moment ago, same reasoning
            # set_always_on_top's own Tier 14 undo capture uses.
            snapshot = {}
            try:
                snapshot["volume_percent"] = backend.get_volume()
            except NotSupported:
                snapshot["volume_percent"] = None
            try:
                snapshot["was_muted"] = backend.is_muted()
            except NotSupported:
                snapshot["was_muted"] = None
            try:
                snapshot["brightness_percent"] = backend.get_brightness()
            except NotSupported:
                snapshot["brightness_percent"] = None

            # 🔧 Found while testing with a backend that supports volume but
            # not brightness: applying these three settings one at a time
            # with a single try/except around all of them left a REAL,
            # silent bug - a NotSupported on the brightness step still
            # reported failure, but the volume change just above it had
            # already gone through and stayed changed, with no undo step
            # recorded either (a failed action's act.fail() never calls
            # set_undo). `touched` tracks exactly which settings this call
            # itself changed, so a failure partway through can roll back
            # ONLY those, leaving the system exactly as it was rather than
            # half-switched into a mode the user was just told didn't work.
            applied, touched = [], []
            try:
                if "volume" in bundle:
                    backend.set_volume(bundle["volume"])
                    touched.append("volume")
                    applied.append(f"volume to {bundle['volume']}%")
                if "mute" in bundle:
                    backend.mute_volume() if bundle["mute"] else backend.unmute_volume()
                    touched.append("mute")
                    if bundle["mute"]:
                        applied.append("muted")
                if "brightness" in bundle:
                    backend.set_brightness(bundle["brightness"])
                    touched.append("brightness")
                    applied.append(f"brightness to {bundle['brightness']}%")
            except NotSupported as e:
                if "volume" in touched and snapshot.get("volume_percent") is not None:
                    try:
                        backend.set_volume(snapshot["volume_percent"])
                    except NotSupported:
                        pass  # already failing; nothing more useful to do here
                if "mute" in touched and snapshot.get("was_muted") is not None:
                    try:
                        backend.mute_volume() if snapshot["was_muted"] else backend.unmute_volume()
                    except NotSupported:
                        pass
                return act.fail(str(e))

            _active_mode["name"], _active_mode["snapshot"] = key, snapshot
            act.set_undo("restore_mode_settings", snapshot, f"Turn {key} mode off")
            summary = f" - {', '.join(applied)}." if applied else "."
            return act.ok(f"{key.title()} mode on{summary}")

    @classmethod
    def disable_mode(cls) -> str:
        """Turns off whichever mode is currently on, restoring the exact
        settings _active_mode snapshotted right before enabling it - the
        same effect as undo_last_action right after enabling a mode (both
        ultimately restore from the same kind of snapshot - see
        enable_mode's act.set_undo above), just reachable by saying
        'turn off study mode' instead of 'undo that', and correct even if
        something else happened in between (undo_last_action wouldn't be,
        since IT acts on whatever's now last in the audit log)."""
        name, snapshot = _active_mode["name"], _active_mode["snapshot"]
        if not name:
            return "No mode is currently on."
        with _audit().action("disable_mode", {"mode": name}) as act:
            backend = cls._backend()
            try:
                if snapshot.get("volume_percent") is not None:
                    backend.set_volume(snapshot["volume_percent"])
                if snapshot.get("was_muted") is not None:
                    backend.mute_volume() if snapshot["was_muted"] else backend.unmute_volume()
                if snapshot.get("brightness_percent") is not None:
                    backend.set_brightness(snapshot["brightness_percent"])
            except NotSupported as e:
                return act.fail(str(e))
            _active_mode["name"], _active_mode["snapshot"] = None, None
            return act.ok(f"{name.title()} mode off.")

    @classmethod
    def get_active_mode(cls) -> str:
        with _audit().action("get_active_mode", {}) as act:
            name = _active_mode["name"]
            return act.ok(f"{name.title()} mode is on." if name else "No mode is currently on.")

    @classmethod
    def is_dnd_or_focus_active(cls) -> bool:
        """Tier 18: a side-effect-free, UNaudited check for the proactive
        engine's own DND/focus gate (automation/proactive.py's
        check_once()) to poll every tick without spamming the audit log
        the way calling get_active_mode() above (an @skill-audited tool)
        every single tick would. Reads _active_mode directly instead -
        same module-level dict get_active_mode() itself reads, just
        without the act.ok()/audit.record() wrapper.

        "focus" and "sleep" both count, not just "dnd" literally - this
        checks the INTENT (do-not-disturb-shaped), not one specific mode
        name. See _MODE_BUNDLES above: "sleep" is its own separate key
        with the same {mute: True, brightness: 15} bundle as "dnd" - it is
        NOT normalized/aliased down to "dnd" anywhere, so both names have
        to be checked explicitly here rather than comparing against a
        single string."""
        return _active_mode["name"] in ("dnd", "sleep", "focus")

    # ---- the rest of per-window management (Tier 14) ----
    # move_window/resize_window read the window's CURRENT rect first
    # (needed anyway, to keep the dimension the caller isn't changing) and
    # reuse it as the undo payload - undo is just calling the same method
    # again with the old numbers, no separate "restore" primitive needed.
    # maximize_window/restore_window/set_fullscreen/show_desktop/
    # minimize_all_windows/switch_window are deliberately NOT in the undo
    # set (see automation/backends/base.py's set_fullscreen docstring, and
    # the roadmap's own undo list - only "volume, brightness, window
    # position/size, always-on-top").
    @classmethod
    def move_window(cls, window_name: str, x: int, y: int) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("move_window", {"window_name": window_name, "x": x, "y": y}) as act:
            if not window_name:
                return act.fail("I need to know which window to move.")
            backend = cls._backend()
            try:
                previous = backend.get_window_rect(window_name)
                result = backend.move_window(window_name, x, y)
            except NotSupported as e:
                return act.fail(str(e))
            if previous is not None:
                act.set_undo("restore_window_position",
                             {"window_name": window_name, "x": previous[0], "y": previous[1]},
                             f"Move '{window_name}' back")
            # 🔧 FIX: act.done(), not act.ok() - a backend that can't find the
            # window returns a plain "I couldn't find..." STRING here (same
            # convention every other per-window method uses), not a raise;
            # act.ok() would have forced that into an "ok" audit row, same
            # mistake as the one below in resize_window/set_always_on_top.
            return act.done(result)

    @classmethod
    def resize_window(cls, window_name: str, width: int, height: int) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("resize_window", {"window_name": window_name, "width": width, "height": height}) as act:
            if not window_name:
                return act.fail("I need to know which window to resize.")
            backend = cls._backend()
            try:
                previous = backend.get_window_rect(window_name)
                result = backend.resize_window(window_name, width, height)
            except NotSupported as e:
                return act.fail(str(e))
            if previous is not None:
                act.set_undo("restore_window_size",
                             {"window_name": window_name, "width": previous[2], "height": previous[3]},
                             f"Resize '{window_name}' back")
            return act.done(result)

    @classmethod
    def maximize_window(cls, window_name: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("maximize_window", {"window_name": window_name}) as act:
            if not window_name:
                return act.fail("I need to know which window to maximize.")
            try:
                return act.done(cls._backend().maximize_window(window_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def restore_window(cls, window_name: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("restore_window", {"window_name": window_name}) as act:
            if not window_name:
                return act.fail("I need to know which window to restore.")
            try:
                return act.done(cls._backend().restore_window(window_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def set_always_on_top(cls, window_name: str, enabled: bool = True) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("set_always_on_top", {"window_name": window_name, "enabled": bool(enabled)}) as act:
            if not window_name:
                return act.fail("I need to know which window to set.")
            backend = cls._backend()
            try:
                was_on_top = backend.is_always_on_top(window_name)
                result = backend.set_always_on_top(window_name, bool(enabled))
            except NotSupported as e:
                return act.fail(str(e))
            # 🔧 FIX: only register the undo - and only report "ok" - when
            # the window was actually found. Unlike move_window/resize_window
            # above, is_always_on_top() has no get_window_rect()-style "None
            # means not found" signal (it returns a plain False for a window
            # it can't find, the same as for one it DID find but that just
            # isn't on top) - result's own text is the only reliable signal,
            # so it's checked explicitly here rather than trusting was_on_top.
            if audit_mod.infer_status(result) == "ok":
                act.set_undo("restore_always_on_top", {"window_name": window_name, "was_on_top": was_on_top},
                             f"{'Keep' if was_on_top else 'Stop keeping'} '{window_name}' on top")
            return act.done(result)

    @classmethod
    def set_fullscreen(cls, window_name: str) -> str:
        window_name = (window_name or "").strip()
        with _audit().action("set_fullscreen", {"window_name": window_name}) as act:
            if not window_name:
                return act.fail("I need to know which window to make fullscreen.")
            try:
                return act.done(cls._backend().set_fullscreen(window_name))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def show_desktop(cls) -> str:
        with _audit().action("show_desktop", {}) as act:
            try:
                return act.done(cls._backend().show_desktop())
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def minimize_all_windows(cls) -> str:
        # Same OS feature as show_desktop (Windows' own Win+D is a toggle
        # that both minimizes everything AND restores it again) - two
        # tool names/phrasings for the one action, not two mechanisms.
        with _audit().action("minimize_all_windows", {}) as act:
            try:
                return act.done(cls._backend().show_desktop())
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def switch_window(cls) -> str:
        with _audit().action("switch_window", {}) as act:
            try:
                return act.done(cls._backend().switch_window())
            except NotSupported as e:
                return act.fail(str(e))

    # 🚨 NEW (Tier 14): close, wait a beat, reopen - deliberately composed
    # from the EXISTING close_app/open_app above rather than duplicating
    # their process-matching/critical-process-guard logic, so a critical
    # process is refused here exactly the same way close_app already
    # refuses it, with no second copy of that list to keep in sync.
    # collapse_if_children=True because close_app/open_app each log their
    # OWN row already - a third "Restarted X" row on top would be noise,
    # not a missing audit trail (see automation/audit.py's own docstring).
    @classmethod
    def restart_app(cls, app_name: str) -> str:
        app_name = (app_name or "").strip()
        with _audit().action("restart_app", {"app_name": app_name}, collapse_if_children=True) as act:
            if not app_name:
                return act.fail("I need to know which app to restart.")
            close_result = cls.close_app(app_name)
            # 🔧 FIX: infer_status() alone misses two of close_app's three
            # failure messages - "'{app_name}' doesn't appear to be
            # running." and "'{name}' is a critical system process - I
            # won't close that." both lead with the quoted app/process
            # name, not a recognized fail/block prefix (infer_status only
            # strips a leading quote CHARACTER, not the whole quoted name,
            # so the text after that is still "ghost' doesn't..." - never a
            # match). Unfixed, restart_app would silently fall through to
            # reopening the app even though the close genuinely failed or
            # was refused - defeating the "stop here" this check exists
            # for, in exactly the two cases ("not running", "critical
            # process") most likely to actually occur. close_app's only
            # non-error failure shape happens to be "leads with a quoted
            # name" while its one success message ("Closed X.") never
            # does, so checking for that leading quote here - local to
            # restart_app, so it can't change close_app's own wording or
            # any other caller's behavior - closes the gap without
            # touching the shared infer_status() heuristic other tools
            # also rely on.
            if audit_mod.infer_status(close_result) != "ok" or close_result.startswith("'"):
                return act.fail(close_result)
            time.sleep(1.0)  # give the OS a moment to actually release it before relaunching
            open_result = cls.open_app(app_name)
            return act.done(f"{close_result} {open_result}")

    @classmethod
    def write_in_notepad(cls, text: str) -> str:
        with _audit().action("write_notepad", {"text": text}) as act:
            try:
                return act.done(cls._backend().type_in_text_editor(text))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def send_whatsapp_message(cls, contact_name: str, message: str) -> str:
        # The message text itself is deliberately NOT stored in the audit log - only who it went to.
        with _audit().action("send_whatsapp_message",
                             {"contact_name": contact_name, "message": f"({len(message or '')} characters)"}) as act:
            try:
                return act.done(cls._backend().send_whatsapp_message(contact_name, message))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def open_system_setting(cls, setting_type: str) -> str:
        with _audit().action("open_system_setting", {"setting": setting_type}) as act:
            try:
                return act.done(cls._backend().open_system_setting(setting_type))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def lock_pc(cls) -> str:
        with _audit().action("lock_pc", {}) as act:
            try:
                return act.done(cls._backend().lock_screen())
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def take_screenshot(cls) -> str:
        with _audit().action("take_screenshot", {}) as act:
            try:
                save_dir = os.path.join(config.DATA_DIR, "screenshots")
                if not os.path.exists(save_dir):
                    os.makedirs(save_dir)

                timestamp = time.strftime("%Y%m%d_%H%M%S")
                file_path = os.path.join(save_dir, f"{USER_SCREENSHOT_PREFIX}{timestamp}.png")

                pyautogui.screenshot(file_path)
                try:
                    cls._backend().open_image_viewer(file_path)
                except NotSupported:
                    pass    # the picture is saved either way
                # 🔧 FIX: this folder used to only ever grow - every screenshot
                # taken stayed forever. Now caps how many user screenshots are
                # kept, deleting the oldest once the limit is passed. Only ever
                # touches USER_SCREENSHOT_PREFIX files (never internal OCR ones).
                _enforce_retention(save_dir, USER_SCREENSHOT_PREFIX, MAX_USER_SCREENSHOTS)
                act.set_undo("remove_created_file", {"target": os.path.abspath(file_path)},
                             "Delete the screenshot")
                return act.ok("Screenshot captured and saved in your project folder.")
            except Exception as e:
                logger.error(f"Failed to capture screen: {e}")
                return act.fail("Error capturing the screen.")

    # 🚨 LATEST: Delete Only Latest Screenshot Logic 🚨
    @classmethod
    def delete_latest_screenshot(cls) -> str:
        with _audit().action("delete_latest_screenshot", {}) as act:
            try:
                save_dir = os.path.join(config.DATA_DIR, "screenshots")

                if not os.path.exists(save_dir):
                    return act.fail("You don't have any saved screenshots to delete.")

                # 🔧 FIX: filtering to USER_SCREENSHOT_PREFIX makes this command only ever see
                # and act on screenshots the user actually took themselves - never the
                # internal OCR captures VisionCapture silently takes for its own use.
                files = [
                    os.path.join(save_dir, f) for f in os.listdir(save_dir)
                    if f.startswith(USER_SCREENSHOT_PREFIX) and f.lower().endswith((".png", ".jpg"))
                ]

                if not files:
                    return act.fail("No screenshots found in the folder to delete.")

                # Sabse naya file dhundho (Time ke hisaab se)
                latest_file = max(files, key=os.path.getctime)

                # Phase 4: into Wave's recoverable trash instead of oblivion (undoable).
                stashed = undo_mod.trash.stash(latest_file) if undo_mod.trash_enabled() else None
                if stashed is None:
                    os.remove(latest_file)
                    return act.ok("Successfully deleted the latest screenshot.")
                act.set_undo("restore_from_trash",
                             {"trash": stashed.trash_path, "original": os.path.abspath(latest_file)},
                             "Restore the screenshot")
                return act.ok("Successfully deleted the latest screenshot. (Say \"undo\" to bring it back.)")
            except Exception as e:
                logger.error(f"Failed to delete screenshot: {e}")
                return act.fail("Error deleting the screenshot.")

    # =========================================================================
    # Tier 15: system control surface II - keyboard, browser control,
    # clipboard write, folder shortcuts, and the download watcher. See
    # brain/tiers.py for the risk-tier table (TAINT_DENY_TOOLS especially)
    # and brain/handlers/automation.py for where each of these is
    # registered as a voice/LLM-facing @skill(...).
    # =========================================================================

    # ---- keyboard ----------------------------------------------------------
    @staticmethod
    def _normalize_chord(key_combo: str) -> str:
        """'Win + R', 'WIN+r', 'windows+r' all collapse to 'win+r' - same
        spirit as FileManager._sanitize_name's path-separator stripping:
        normalize once, right at the edge, so every check after this point
        can compare against one canonical spelling."""
        alias = {"windows": "win", "super": "win", "delete": "del", "esc": "escape"}
        parts = [p.strip().lower() for p in (key_combo or "").replace(" ", "").split("+") if p.strip()]
        return "+".join(alias.get(p, p) for p in parts)

    @classmethod
    def _modifier_key(cls) -> str:
        """'command' on macOS, 'ctrl' everywhere else - the one real
        platform difference the browser-control/clipboard-keystroke tools
        below have, so this is the only thing they ask the backend for."""
        return "command" if cls._backend().name == "macos" else "ctrl"

    @classmethod
    def _send_hotkey(cls, tool: str, keys: tuple, success: str, fail: str) -> str:
        """Shared body for every tool below that's really just 'send this
        exact chord' - audited under its OWN tool name (not a generic
        'send_hotkey'), same reasoning as _nudge_volume's media-key
        fallback in the Tier 14 block above: keeps same-tool repeats
        grouping correctly in list_recent_actions."""
        with _audit().action(tool, {}) as act:
            try:
                pyautogui.hotkey(*keys)
            except Exception as e:
                logger.error(f"{tool} failed: {e}")
                return act.fail(fail)
            return act.ok(success)

    @classmethod
    def press_key(cls, key_combo: str) -> str:
        chord = cls._normalize_chord(key_combo)
        if not chord:
            return "Which key?"
        with _audit().action("press_key", {"key_combo": chord}) as act:
            if chord in _ALWAYS_BLOCKED_CHORDS:
                return act.blocked(f"I won't send {chord} - that's not a chord I'll send on request.")
            if chord in _WINDOW_CLOSE_CHORDS:
                try:
                    title = (cls._backend().get_active_window_title() or "").lower()
                except Exception:
                    title = ""
                if title and any(kw in title for kw in _SYSTEM_WINDOW_TITLE_KEYWORDS):
                    return act.blocked(
                        f"The active window looks like it might be '{title}' - I won't send {chord} to "
                        f"that one. Say \"close window\" if you want me to close whatever's focused.")
            keys = chord.split("+")
            try:
                if len(keys) == 1:
                    pyautogui.press(keys[0])
                else:
                    pyautogui.hotkey(*keys)
            except Exception as e:
                logger.error(f"press_key({chord!r}) failed: {e}")
                return act.fail(f"Couldn't send {chord}.")
            return act.ok(f"Sent {chord}.")

    @classmethod
    def type_text(cls, text: str) -> str:
        if not text:
            return "What should I type?"
        # 🔧 Audited by LENGTH, never the text itself - unlike every other
        # tool's args here, what gets typed could be a password or anything
        # else someone wouldn't want sitting in plain text in list_recent_
        # actions/the audit log. set_clipboard below makes the same choice.
        with _audit().action("type_text", {"length": len(text)}) as act:
            try:
                pyautogui.write(text, interval=0.02)
            except Exception as e:
                logger.error(f"type_text failed: {e}")
                return act.fail("Couldn't type that.")
            return act.ok(f"Typed {len(text)} character{'s' if len(text) != 1 else ''}.")

    @classmethod
    def copy_text(cls) -> str:
        return cls._send_hotkey("copy_text", (cls._modifier_key(), "c"), "Copied.", "Couldn't copy.")

    @classmethod
    def paste_text(cls) -> str:
        return cls._send_hotkey("paste_text", (cls._modifier_key(), "v"), "Pasted.", "Couldn't paste.")

    @classmethod
    def select_all_text(cls) -> str:
        return cls._send_hotkey("select_all_text", (cls._modifier_key(), "a"),
                                 "Selected all.", "Couldn't select all.")

    @classmethod
    def undo_keystroke(cls) -> str:
        return cls._send_hotkey("undo_keystroke", (cls._modifier_key(), "z"),
                                 "Sent undo.", "Couldn't send undo.")

    @classmethod
    def redo_keystroke(cls) -> str:
        # Ctrl+Y is the far more common Windows/Linux redo shortcut (Word,
        # most editors); Ctrl+Shift+Z also works in many apps but Ctrl+Y is
        # the safer single default. macOS apps overwhelmingly expect
        # Cmd+Shift+Z instead - there's no macOS Cmd+Y equivalent.
        mod = cls._modifier_key()
        keys = (mod, "shift", "z") if mod == "command" else (mod, "y")
        return cls._send_hotkey("redo_keystroke", keys, "Sent redo.", "Couldn't send redo.")

    # ---- browser control ----------------------------------------------------
    # Generic hotkeys a focused browser window is overwhelmingly likely to
    # honor - same "send the shortcut, no app-specific integration" spirit
    # as close_current_tab (Ctrl+W) further up this file. No new backend
    # method needed for any of these: the only real platform difference is
    # Ctrl vs Cmd, already handled by _modifier_key() - there's no OS-level
    # divergence the way closing a window or reading the active title has.
    @classmethod
    def new_tab(cls) -> str:
        return cls._send_hotkey("new_tab", (cls._modifier_key(), "t"),
                                 "Opened a new tab.", "Couldn't open a new tab.")

    @classmethod
    def next_tab(cls) -> str:
        # Ctrl+Tab (not Cmd+Tab, which is the OS's app-switcher) - browsers
        # honor plain Ctrl+Tab for next-tab on every platform including macOS.
        return cls._send_hotkey("next_tab", ("ctrl", "tab"), "Switched to the next tab.", "Couldn't switch tabs.")

    @classmethod
    def previous_tab(cls) -> str:
        return cls._send_hotkey("previous_tab", ("ctrl", "shift", "tab"),
                                 "Switched to the previous tab.", "Couldn't switch tabs.")

    @classmethod
    def reload_page(cls) -> str:
        return cls._send_hotkey("reload_page", (cls._modifier_key(), "r"),
                                 "Reloaded the page.", "Couldn't reload the page.")

    @classmethod
    def zoom_in(cls) -> str:
        return cls._send_hotkey("zoom_in", (cls._modifier_key(), "+"), "Zoomed in.", "Couldn't zoom in.")

    @classmethod
    def zoom_out(cls) -> str:
        return cls._send_hotkey("zoom_out", (cls._modifier_key(), "-"), "Zoomed out.", "Couldn't zoom out.")

    @classmethod
    def zoom_reset(cls) -> str:
        return cls._send_hotkey("zoom_reset", (cls._modifier_key(), "0"),
                                 "Reset the zoom level.", "Couldn't reset the zoom.")

    @classmethod
    def scroll_up(cls) -> str:
        return cls._send_hotkey("scroll_up", ("pageup",), "Scrolled up.", "Couldn't scroll up.")

    @classmethod
    def scroll_down(cls) -> str:
        return cls._send_hotkey("scroll_down", ("pagedown",), "Scrolled down.", "Couldn't scroll down.")

    @classmethod
    def scroll_to_top(cls) -> str:
        # Home/End (not Ctrl+Home, which is a "top of document" shortcut in
        # text editors, not browsers) - works on Windows/Linux keyboards
        # directly; some Mac laptop keyboards need Fn+Left for Home, same
        # honest platform quirk set_fullscreen's own F11 comment flags for
        # "most apps", not a Wave bug.
        return cls._send_hotkey("scroll_to_top", ("home",), "Scrolled to the top.", "Couldn't scroll to the top.")

    @classmethod
    def scroll_to_bottom(cls) -> str:
        return cls._send_hotkey("scroll_to_bottom", ("end",),
                                 "Scrolled to the bottom.", "Couldn't scroll to the bottom.")

    # ---- clipboard write -----------------------------------------------------
    @classmethod
    def set_clipboard(cls, text: str) -> str:
        if not HAS_PYPERCLIP:
            return "Clipboard writing needs the 'pyperclip' package installed."
        # Audited by length only - see type_text's own comment just above.
        with _audit().action("set_clipboard", {"length": len(text or "")}) as act:
            try:
                pyperclip.copy(text or "")
            except Exception as e:
                logger.error(f"set_clipboard failed: {e}")
                return act.fail("Couldn't set the clipboard.")
            return act.ok("Copied that to the clipboard.")

    # ---- folder shortcuts ----------------------------------------------------
    # The five below are each just a path under the home folder - reusing
    # the SAME backend open_path() the Files-page-only open_path()/
    # reveal_path() wrappers at the tail of this file already call. No new
    # backend method needed, just expanduser() plus making THESE audited
    # (new voice/LLM-facing tools, unlike that pre-existing pair).
    _HOME_FOLDERS = {
        "open_downloads": "Downloads", "open_documents": "Documents",
        "open_pictures": "Pictures", "open_music": "Music", "open_videos": "Videos",
    }

    @classmethod
    def _open_home_folder(cls, tool: str) -> str:
        folder_name = cls._HOME_FOLDERS[tool]
        path = os.path.join(os.path.expanduser("~"), folder_name)
        with _audit().action(tool, {"path": path}) as act:
            try:
                cls._backend().open_path(path)
            except (NotSupported, OSError) as e:
                return act.fail(f"Couldn't open {folder_name}: {e}")
            return act.ok(f"Opened {folder_name}.")

    @classmethod
    def open_downloads(cls) -> str:
        return cls._open_home_folder("open_downloads")

    @classmethod
    def open_documents(cls) -> str:
        return cls._open_home_folder("open_documents")

    @classmethod
    def open_pictures(cls) -> str:
        return cls._open_home_folder("open_pictures")

    @classmethod
    def open_music(cls) -> str:
        return cls._open_home_folder("open_music")

    @classmethod
    def open_videos(cls) -> str:
        return cls._open_home_folder("open_videos")

    # The four below have no plain filesystem equivalent (This PC, Recycle
    # Bin) or none at all on every OS (Task Manager, Control Panel) - they
    # go through the new open_special_location() backend method instead,
    # one real implementation per OS (automation/backends/*.py). That
    # method hands back a plain string that can itself describe success OR
    # failure (e.g. Windows' "Couldn't open that: ..."), same shape as
    # open_app's launch_app() above - so act.done() (which infers ok/failed
    # from the text) is the right call here, not a blind act.ok().
    @classmethod
    def open_this_pc(cls) -> str:
        with _audit().action("open_this_pc", {}) as act:
            try:
                return act.done(cls._backend().open_special_location("this_pc"))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def open_recycle_bin(cls) -> str:
        with _audit().action("open_recycle_bin", {}) as act:
            try:
                return act.done(cls._backend().open_special_location("recycle_bin"))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def open_task_manager(cls) -> str:
        with _audit().action("open_task_manager", {}) as act:
            try:
                return act.done(cls._backend().open_special_location("task_manager"))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def open_control_panel(cls) -> str:
        with _audit().action("open_control_panel", {}) as act:
            try:
                return act.done(cls._backend().open_special_location("control_panel"))
            except NotSupported as e:
                return act.fail(str(e))

    @classmethod
    def empty_recycle_bin(cls) -> str:
        """T3 (brain/tiers.py) - genuinely gone, no trash-of-the-trash to
        recover from, unlike delete_latest_screenshot's own recoverable
        path. By the time this runs, the confirm gate has already asked -
        this just does it and says so plainly, no second check here."""
        with _audit().action("empty_recycle_bin", {}) as act:
            try:
                return act.done(cls._backend().empty_recycle_bin())
            except NotSupported as e:
                return act.fail(str(e))

    # ---- download watch -------------------------------------------------
    # Thin, audited wrappers over automation/proactive.py's ProactiveEngine
    # (the one place the actual watch state/temp-file logic lives) - same
    # "AppController decides whether to call it, the other module does the
    # real work" split as undo_last_action delegating to undo_manager above.
    @classmethod
    def _resolve_watch_folder(cls, folder: str):
        if not folder:
            return None
        expanded = os.path.expanduser(folder)
        return expanded if os.path.isabs(expanded) else os.path.join(os.path.expanduser("~"), expanded)

    @classmethod
    def watch_for_download(cls, folder: str = None, timeout_minutes: float = None) -> str:
        with _audit().action("watch_for_download", {"folder": folder, "timeout_minutes": timeout_minutes}) as act:
            resolved = cls._resolve_watch_folder(folder)
            return act.done(proactive_engine.arm_download_watch(resolved, timeout_minutes))

    @classmethod
    def cancel_download_watch(cls) -> str:
        with _audit().action("cancel_download_watch", {}) as act:
            return act.done(proactive_engine.cancel_download_watch())

    # ---- used by the Files page (not "what Wave did", so not audited) ----
    @classmethod
    def open_path(cls, path: str):
        """Open a file/folder with its default app. Returns None on success, else a message."""
        try:
            cls._backend().open_path(path)
        except (NotSupported, OSError) as e:
            return f"Couldn't open it: {e}"

    @classmethod
    def reveal_path(cls, path: str):
        try:
            cls._backend().reveal_path(path)
        except (NotSupported, OSError) as e:
            return f"Couldn't show it: {e}"

    @classmethod
    def open_terminal_at(cls, path: str) -> str:
        try:
            return cls._backend().open_terminal_at(path)
        except (NotSupported, OSError) as e:
            return f"Couldn't open a terminal: {e}"

    @classmethod
    def open_in_vscode(cls, path: str) -> str:
        try:
            return cls._backend().open_in_editor(path)
        except (NotSupported, OSError) as e:
            return f"Couldn't open VS Code: {e}"


# =============================================================================
# 🏆 Tier 14: undo handlers for volume/brightness/window position+size/
# always-on-top - registered here (not automation/undo.py itself) because
# each one needs a live backend call, exactly the "two steps" automation/
# undo.py's own module docstring describes for adding an undoable action
# "elsewhere". Every handler re-reads reality defensively, same spirit as
# undo.py's own file-based handlers (e.g. _remove_created_file double-
# checking the disk): a window that's since been closed/renamed, or a
# backend that's stopped being able to do this at all, is reported back
# as a clear failure, never a silent no-op dressed up as success.
# =============================================================================
def _window_not_found(message: str) -> bool:
    return message.startswith("I couldn't find an open window matching")


def _undo_restore_volume(payload: dict):
    try:
        return True, get_backend().set_volume(int(payload.get("percent", 50)))
    except NotSupported as e:
        return False, str(e)


def _undo_restore_mute_state(payload: dict):
    try:
        backend = get_backend()
        was_muted = bool(payload.get("was_muted", False))
        return True, (backend.mute_volume() if was_muted else backend.unmute_volume())
    except NotSupported as e:
        return False, str(e)


def _undo_restore_brightness(payload: dict):
    try:
        return True, get_backend().set_brightness(int(payload.get("percent", 50)))
    except NotSupported as e:
        return False, str(e)


def _undo_restore_window_position(payload: dict):
    name = payload.get("window_name", "")
    try:
        message = get_backend().move_window(name, int(payload.get("x", 0)), int(payload.get("y", 0)))
    except NotSupported as e:
        return False, str(e)
    return (False, message) if _window_not_found(message) else (True, message)


def _undo_restore_window_size(payload: dict):
    name = payload.get("window_name", "")
    try:
        message = get_backend().resize_window(name, int(payload.get("width", 100)), int(payload.get("height", 100)))
    except NotSupported as e:
        return False, str(e)
    return (False, message) if _window_not_found(message) else (True, message)


def _undo_restore_always_on_top(payload: dict):
    name = payload.get("window_name", "")
    try:
        message = get_backend().set_always_on_top(name, bool(payload.get("was_on_top", False)))
    except NotSupported as e:
        return False, str(e)
    return (False, message) if _window_not_found(message) else (True, message)


def _undo_restore_mode_settings(payload: dict):
    """Tier 16: undoes enable_mode via the SAME generic path
    undo_last_action already uses for every other setting - this is what
    'undo that' does right after turning a mode on, as opposed to
    disable_mode()'s own direct restore (see that method's docstring for
    why it doesn't just call into this). Always clears _active_mode after
    restoring, even in the (rare) case where a second mode was switched to
    in between and this undo is actually undoing THAT one - the restored
    settings could coincidentally still match an earlier named mode, but
    this module has no reliable way to tell, and reporting "no mode is on"
    is the honest answer when it can't, never a guessed name that might be
    wrong."""
    backend = get_backend()
    try:
        if payload.get("volume_percent") is not None:
            backend.set_volume(int(payload["volume_percent"]))
        if payload.get("was_muted") is not None:
            backend.mute_volume() if payload["was_muted"] else backend.unmute_volume()
        if payload.get("brightness_percent") is not None:
            backend.set_brightness(int(payload["brightness_percent"]))
    except NotSupported as e:
        return False, str(e)
    _active_mode["name"], _active_mode["snapshot"] = None, None
    return True, "Restored your previous volume/brightness settings."


undo_mod.undo_manager.register("restore_volume", _undo_restore_volume)
undo_mod.undo_manager.register("restore_mute_state", _undo_restore_mute_state)
undo_mod.undo_manager.register("restore_brightness", _undo_restore_brightness)
undo_mod.undo_manager.register("restore_window_position", _undo_restore_window_position)
undo_mod.undo_manager.register("restore_window_size", _undo_restore_window_size)
undo_mod.undo_manager.register("restore_always_on_top", _undo_restore_always_on_top)
undo_mod.undo_manager.register("restore_mode_settings", _undo_restore_mode_settings)
