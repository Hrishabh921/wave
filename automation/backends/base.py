"""
The contract every platform backend fulfils.

Most methods return the sentence Wave should say back to the user (that is what
AppController hands to the LLM / TTS), a few return plain values. Anything a
backend can't do should `raise NotSupported(...)` (or just not override the
method): AppController turns that into a friendly "isn't supported on <OS> yet"
reply instead of a crash, and `capabilities()` reports it honestly.
"""
import logging
import shutil
import subprocess
import webbrowser
from abc import ABC
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PlatformBackend")


class Shell:
    """The only way non-Windows backends touch the OS (which / run / spawn), so tests can
    substitute a fake and assert the exact commands without needing that OS."""

    def which(self, name: str) -> Optional[str]:
        return shutil.which(name)

    def run(self, argv: List[str], timeout: float = 10) -> Tuple[int, str]:
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            return 127, ""

    def spawn(self, argv: List[str], **kwargs) -> None:
        subprocess.Popen(argv, **kwargs)


class NotSupported(Exception):
    """This backend can't do that on this OS (yet)."""


# Tier 44: how each radio is named to the user.
RADIO_LABELS = {"wifi": "Wi-Fi", "bluetooth": "Bluetooth"}


# Tier 14: the exact "a native shutdown/restart really was cancelled" sentence,
# shared between every backend that can actually do this (Windows, Linux) and
# AppController.cancel_shutdown()'s own success check, so combining it with
# "...and the pending sleep/hibernate/log-off too" doesn't depend on a fragile
# string match against free-text duplicated three times over.
NATIVE_SHUTDOWN_CANCELLED_MSG = "Cancelled the scheduled shutdown/restart."


class PlatformBackend(ABC):
    name = "base"
    display_name = "this operating system"

    # Every overridable operation - used by capabilities() and the docs/tests.
    FEATURES = (
        "launch_app", "find_browser", "launch_in_browser", "open_url_default", "toggle_radio",
        "close_window", "close_tab", "open_system_setting", "lock_screen", "type_in_text_editor",
        "send_whatsapp_message", "open_path", "reveal_path", "open_terminal_at", "open_in_editor",
        "open_image_viewer", "desktop_dir", "start_service", "stop_service",
        "list_open_windows", "focus_window", "minimize_window", "snap_window",
        # Tier 14: a read-only counterpart to start_service/stop_service.
        "list_services",
        # Tier 14: power/session.
        "shutdown_now", "restart_now", "cancel_native_shutdown", "sleep_now", "hibernate_now", "log_off_now",
        # Tier 14: volume.
        "get_volume", "set_volume", "is_muted", "mute_volume", "unmute_volume",
        # Tier 14: brightness.
        "get_brightness", "set_brightness",
        # Tier 14: the rest of per-window management, started in Tier 8.
        "get_window_rect", "move_window", "resize_window", "maximize_window", "restore_window",
        "is_always_on_top", "set_always_on_top", "set_fullscreen", "show_desktop", "switch_window",
        # Tier 15: best-effort active-window title, folder shortcuts that
        # aren't a plain path, and emptying the Recycle Bin.
        "get_active_window_title", "open_special_location", "empty_recycle_bin",
    )

    # Tier 14: features a subclass overrides ONLY to raise a MORE SPECIFIC
    # NotSupported than the generic one below (e.g. macOS's hibernate_now,
    # which explains there's no on-demand hibernate trigger at all, rather
    # than the bare "isn't supported yet") - the override itself does no
    # real work, so capabilities()'s normal "is it overridden" heuristic
    # would wrongly call these supported. Listing a feature here is what
    # keeps that honest.
    ALWAYS_UNSUPPORTED: Tuple[str, ...] = ()

    def _unsupported(self, feature: str) -> NotSupported:
        return NotSupported(f"{feature} isn't supported on {self.display_name} yet.")

    # ---- apps & browsers ----
    def launch_app(self, app_name: str) -> str:
        raise self._unsupported("Launching apps by name")

    def find_browser(self, browser_name: str) -> Optional[str]:
        """An opaque handle (path / app name) for a specific browser, or None if it isn't installed."""
        return None

    def launch_in_browser(self, handle: str, url: str) -> None:
        raise self._unsupported("Opening a specific browser")

    def open_url_default(self, url: str) -> None:
        webbrowser.open(url)           # the stdlib already knows every OS's default browser

    # ---- system ----
    def toggle_radio(self, command: str) -> str:
        raise self._unsupported("Toggling Wi-Fi / Bluetooth / hotspot")

    def close_window(self) -> None:
        raise self._unsupported("Closing the focused window")

    def close_tab(self) -> None:
        raise self._unsupported("Closing the current tab")

    def open_system_setting(self, setting_type: str) -> str:
        raise self._unsupported("Opening system settings")

    def lock_screen(self) -> str:
        raise self._unsupported("Locking the screen")

    def type_in_text_editor(self, text: str) -> str:
        raise self._unsupported("Typing into a text editor")

    def send_whatsapp_message(self, contact_name: str, message: str) -> str:
        raise self._unsupported("WhatsApp automation")

    # ---- OS services (Tier 8) ----
    # "Service" means whatever this OS's own service manager calls it: a
    # Windows SCM service name (sc.exe), a systemd unit (systemctl), or a
    # launchd job label on macOS - three different naming schemes, which is
    # exactly why AppController.start_service/stop_service (automation/
    # app_control.py) pass the name straight through rather than trying to
    # normalize "the bluetooth service" into one of them. raise NotSupported
    # here (the base default) when the OS has no service manager at all to
    # ask; a *named* service that doesn't exist is NOT NotSupported - that's
    # a normal, expected failure the concrete backend reports back as a
    # string (same split launch_app already draws for "no such app").
    def start_service(self, service_name: str) -> str:
        raise self._unsupported("Starting a system service")

    def stop_service(self, service_name: str) -> str:
        raise self._unsupported("Stopping a system service")

    def list_services(self) -> List[str]:
        """Names of currently running OS-level services, in whatever this OS's own
        naming scheme is (a read-only counterpart to start_service/stop_service
        above) - plain titles, same split list_open_windows draws elsewhere."""
        raise self._unsupported("Listing system services")

    # ---- power/session (Tier 14) ----
    # Split the way the roadmap asks: shutdown/restart get the OS's OWN
    # delayed command (survives Wave's own process dying - see
    # automation/app_control.py's module docstring for why that matters),
    # while sleep/hibernate/log-off have no such native delay to lean on
    # (Windows' own shutdown.exe only honours /t with /s and /r, not /h or
    # /l) - AppController times those with its own cancellable timer
    # instead, and these three *_now() methods are deliberately the
    # immediate, undelayed action, called only once that timer fires.
    # cancel_native_shutdown only ever needs to undo shutdown_now/
    # restart_now - AppController's own timer cancellation (a plain
    # threading.Timer.cancel()) covers the other three, so there's no
    # backend method for that half.
    def shutdown_now(self, delay_s: int) -> str:
        raise self._unsupported("Shutting down the PC")

    def restart_now(self, delay_s: int) -> str:
        raise self._unsupported("Restarting the PC")

    def cancel_native_shutdown(self) -> str:
        raise self._unsupported("Cancelling a scheduled shutdown/restart")

    def sleep_now(self) -> str:
        raise self._unsupported("Putting the PC to sleep")

    def hibernate_now(self) -> str:
        raise self._unsupported("Hibernating the PC")

    def log_off_now(self) -> str:
        raise self._unsupported("Logging off the current user")

    # ---- volume (Tier 14) ----
    # A SEPARATE primitive set from brain/handlers/media.py's older pycaw
    # calls (which predate this backend layer) - AppController's new
    # volume methods call these instead, and media.py's volume_up/
    # volume_down/mute_volume/unmute_volume skills now go through
    # AppController too, so every volume change - old command or new -
    # ends up on the one audited, undoable path. See AppController.
    def get_volume(self) -> int:
        raise self._unsupported("Reading the system volume")

    def set_volume(self, percent: int) -> str:
        raise self._unsupported("Setting an exact system volume")

    def is_muted(self) -> bool:
        raise self._unsupported("Reading whether audio is muted")

    def mute_volume(self) -> str:
        raise self._unsupported("Muting system audio")

    def unmute_volume(self) -> str:
        raise self._unsupported("Unmuting system audio")

    # ---- brightness (Tier 14) ----
    # `screen_brightness_control` (already a dependency - the Settings page
    # slider has used it since before this backend layer existed) is
    # itself already cross-platform, so every backend's implementation of
    # these two is a thin, near-identical call into it - the backend seam
    # still earns its keep here because it's what lets AppController wrap
    # the call in one consistent audit+undo path regardless of OS, and
    # what makes a platform with no working brightness backend (a desktop
    # monitor with no DDC/CI support, say) fail the same honest
    # NotSupported way every other unavailable feature here does.
    def get_brightness(self) -> int:
        raise self._unsupported("Reading screen brightness")

    def set_brightness(self, percent: int) -> str:
        raise self._unsupported("Setting screen brightness")

    # ---- radios and the light/dark theme (Tier 44) ----
    # `kind` is "wifi" or "bluetooth". SARA switches both radios and the
    # theme itself; Wave only opened the matching Settings page. get_* is
    # what AppController records so "wapas kar do" can put it back.
    def get_radio(self, kind: str) -> bool:
        raise self._unsupported(f"Reading the {RADIO_LABELS.get(kind, kind)} state")

    def set_radio(self, kind: str, on: bool) -> str:
        raise self._unsupported(f"Turning {RADIO_LABELS.get(kind, kind)} on or off")

    def get_dark_mode(self) -> bool:
        raise self._unsupported("Reading the light/dark theme")

    def set_dark_mode(self, on: bool) -> str:
        raise self._unsupported("Switching between light and dark mode")

    # ---- per-window management (Tier 8) ----
    # One SPECIFIC window, named by (a substring of) its title - a
    # different thing from close_window above, which only ever acts on
    # whatever already has focus. list_open_windows returns plain titles
    # (AppController does the "you have N windows open: ..." phrasing,
    # the same split db.get_upcoming_reminders()/_skill_list_reminders()
    # draws elsewhere); the other three return the sentence to say back,
    # same as every other backend method here.
    def list_open_windows(self) -> List[str]:
        raise self._unsupported("Listing open windows")

    def focus_window(self, window_name: str) -> str:
        raise self._unsupported("Focusing a specific window")

    def minimize_window(self, window_name: str) -> str:
        raise self._unsupported("Minimizing a specific window")

    def snap_window(self, window_name: str, position: str) -> str:
        raise self._unsupported("Snapping a window to a screen position")

    # ---- the rest of per-window management (Tier 14) ----
    # get_window_rect is a GETTER with no Tier-8 precedent (list/focus/
    # minimize/snap never needed to read geometry back) - added
    # specifically so AppController can capture a window's position/size
    # BEFORE move_window/resize_window change it, and undo by calling
    # move_window/resize_window again with the captured values. Same
    # reasoning for is_always_on_top, read before set_always_on_top
    # changes it - a plain "call the opposite" undo is wrong the moment
    # the call was a no-op (already topmost, asked to make it topmost
    # again), so the real prior value has to be captured, not assumed.
    def get_window_rect(self, window_name: str):
        """(x, y, width, height) in screen pixels, or raises NotSupported/returns
        None if `window_name` isn't found - mirrors focus_window's own
        "couldn't find it" vs "can't do this at all" split."""
        raise self._unsupported("Reading a window's position and size")

    def move_window(self, window_name: str, x: int, y: int) -> str:
        raise self._unsupported("Moving a window")

    def resize_window(self, window_name: str, width: int, height: int) -> str:
        raise self._unsupported("Resizing a window")

    def maximize_window(self, window_name: str) -> str:
        raise self._unsupported("Maximizing a window")

    def restore_window(self, window_name: str) -> str:
        raise self._unsupported("Restoring a window to its normal size")

    def is_always_on_top(self, window_name: str) -> bool:
        raise self._unsupported("Reading a window's always-on-top state")

    def set_always_on_top(self, window_name: str, enabled: bool) -> str:
        raise self._unsupported("Setting a window always-on-top")

    def set_fullscreen(self, window_name: str) -> str:
        """Sends the F11 fullscreen toggle most apps (browsers, video/media
        players) respond to - not a true OS-level borderless-window trick,
        which has no generic cross-app equivalent. A toggle, not an
        explicit on/off, same as the F11 key itself - so (unlike
        always-on-top) this is deliberately NOT in the undo set; pressing
        it again is its own undo."""
        raise self._unsupported("Toggling fullscreen")

    def show_desktop(self) -> str:
        raise self._unsupported("Showing the desktop")

    def switch_window(self) -> str:
        """Cycles to the next window (the OS's own alt-tab/cmd-tab), as
        opposed to focus_window's "switch to THIS specific one by name"."""
        raise self._unsupported("Switching to the next window")

    # Tier 15: best-effort only, used by AppController.press_key()'s
    # Alt+F4-on-a-system-window check - NOT exposed as its own voice tool
    # (nobody asks "what's my active window"). Returns "" rather than
    # raising on any failure (no pywin32/xdotool/System Events access, no
    # window focused, ...) - the caller treats an empty title as "can't
    # tell, so don't block on it," never as a crash.
    def get_active_window_title(self) -> str:
        return ""

    # ---- files & folders ----
    def open_path(self, path: str) -> None:
        raise self._unsupported("Opening a file with its default app")

    def reveal_path(self, path: str) -> None:
        raise self._unsupported("Showing a file in the file manager")

    # Tier 15: the four folder shortcuts that are NOT a plain filesystem
    # path (Downloads/Documents/Pictures/Music/Videos already resolve to
    # one via os.path.expanduser() in AppController and reuse open_path()
    # above - these four genuinely need a per-OS command/special URI).
    # `name` is one of "this_pc"/"recycle_bin"/"task_manager"/"control_panel".
    def open_special_location(self, name: str) -> str:
        raise self._unsupported("Opening that")

    # Tier 15: T3, confirm-gated, deliberately with NO undo path - unlike
    # delete_latest_screenshot's own recoverable Wave-trash, this empties
    # the OS's own trash/Recycle Bin, so whatever was already in it is
    # genuinely gone.
    def empty_recycle_bin(self) -> str:
        raise self._unsupported("Emptying the Recycle Bin")

    def open_terminal_at(self, path: str) -> str:
        raise self._unsupported("Opening a terminal")

    def open_in_editor(self, path: str) -> str:
        raise self._unsupported("Opening VS Code")

    def open_image_viewer(self, path: str) -> None:
        self.open_path(path)

    def desktop_dir(self) -> str:
        import os
        return os.path.join(os.path.expanduser("~"), "Desktop")

    # ---- introspection ----
    def capabilities(self) -> Dict[str, bool]:
        """feature -> whether this backend can really do it. A feature counts if the backend
        overrides it, or if the base class's default is a working implementation
        (open_url_default uses the stdlib; open_image_viewer reuses open_path)."""
        overridden = {f: getattr(type(self), f) is not getattr(PlatformBackend, f) for f in self.FEATURES}
        for f in self.ALWAYS_UNSUPPORTED:
            overridden[f] = False
        overridden["open_url_default"] = True
        overridden["desktop_dir"] = True
        overridden["open_image_viewer"] = overridden["open_image_viewer"] or overridden["open_path"]
        return overridden


class UnsupportedBackend(PlatformBackend):
    """Used on an OS Wave has no backend for: every operation is a polite 'not yet'."""

    def __init__(self, os_name: str = "unknown"):
        self.name = os_name
        self.display_name = os_name or "this operating system"
