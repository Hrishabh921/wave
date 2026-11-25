"""
macOS reference backend (EXPERIMENTAL).

Uses `open`, `pmset` and the x-apple.systempreferences: URL scheme. Command
construction is unit-tested with a fake Shell (tests/test_phase4_platform.py);
it has NOT been run on a real Mac, and System Settings pane ids change between
macOS versions - treat this as a starting point (docs/PLATFORM.md).
"""
import logging
import os
import webbrowser
from typing import Optional

from automation.backends.base import RADIO_LABELS, NotSupported, PlatformBackend, Shell

logger = logging.getLogger("AppControl")

_BROWSER_APPS = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "brave": "Brave Browser",
    "brave browser": "Brave Browser", "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
    "firefox": "Firefox", "mozilla firefox": "Firefox", "opera": "Opera", "safari": "Safari",
}
# Tier 5: expanded from 5 to 14 entries (roadmap's "~15 separate 'open X
# settings' intents"). macOS renamed System Preferences to System Settings in
# Ventura and, from Sonoma on, panes are identified by extension bundle ID
# rather than the old com.apple.preference.* scheme - these are the current
# identifiers (verified against Apple's own extension bundle IDs for Sonoma/
# Sequoia), not the legacy ones the original 5-entry map used for display/
# sound/battery/update. There is deliberately no "apps" or "airplane" entry -
# macOS has no single settings pane for either (per-app permissions live
# inside Privacy & Security, and there's no system-wide "airplane mode" pane
# the way Windows/mobile have one).
def _as_literal(s: str) -> str:
    """Escapes a value for safe interpolation into a double-quoted
    AppleScript string literal (used by the per-window management methods
    below, which build a small osascript snippet per call)."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


_PANES = {
    "display": "com.apple.Displays-Settings.extension",
    "sound": "com.apple.Sound-Settings.extension",
    "bluetooth": "com.apple.BluetoothSettings",
    "network": "com.apple.Network-Settings.extension",
    "wifi": "com.apple.wifi-settings-extension",
    "update": "com.apple.Software-Update-Settings.extension",
    "personalization": "com.apple.Appearance-Settings.extension",
    "privacy": "com.apple.settings.PrivacySecurity.extension",
    "storage": "com.apple.settings.Storage",
    "battery": "com.apple.Battery-Settings.extension",
    "power": "com.apple.Battery-Settings.extension",
    "about": "com.apple.systempreferences.GeneralSettings",
    "nightlight": "com.apple.Displays-Settings.extension",
    "settings": "",
}


class MacBackend(PlatformBackend):
    name = "macos"
    display_name = "macOS"

    # Tier 14: these three ARE overridden below, but only to raise a more
    # specific NotSupported than the generic default - no real
    # implementation exists for any of them on macOS. See PlatformBackend.
    # ALWAYS_UNSUPPORTED's own docstring in automation/backends/base.py.
    ALWAYS_UNSUPPORTED = ("hibernate_now", "is_always_on_top", "set_always_on_top")

    def __init__(self, shell: Optional[Shell] = None, applications_dir: str = "/Applications"):
        self.sh = shell or Shell()
        self.apps_dir = applications_dir

    def launch_app(self, app_name: str) -> str:
        if self.sh.run(["open", "-a", app_name])[0] == 0:
            return f"Successfully opened {app_name}."
        return f"I couldn't find an app called '{app_name}' on this Mac."

    def find_browser(self, browser_name: str):
        app = _BROWSER_APPS.get((browser_name or "").lower().strip())
        return app if app and os.path.isdir(os.path.join(self.apps_dir, f"{app}.app")) else None

    def launch_in_browser(self, handle: str, url: str) -> None:
        self.sh.spawn(["open", "-a", handle, url])

    def close_window(self) -> None:
        # Unlike the DISPLAY case on Linux there's no env var to pre-check here -
        # the common real-world failure is pyautogui lacking the Accessibility
        # permission macOS requires to send key events - so this only wraps the
        # call itself, turning ANY failure into the same friendly NotSupported
        # every other method in this file raises, instead of a raw crash.
        try:
            import pyautogui
            pyautogui.hotkey('command', 'w')
        except Exception as e:
            raise NotSupported(f"Closing the focused window didn't work: {e}") from e

    def close_tab(self) -> None:
        try:
            import pyautogui
            pyautogui.hotkey('command', 'w')
        except Exception as e:
            raise NotSupported(f"Closing the current tab didn't work: {e}") from e

    def open_system_setting(self, setting_type: str) -> str:
        setting_type = setting_type.lower()
        for key, pane in _PANES.items():
            if key in setting_type:
                self.sh.spawn(["open", f"x-apple.systempreferences:{pane}"])
                return f"Successfully opened {key.capitalize()} settings for you."
        return "Could not find the requested system setting."

    def lock_screen(self) -> str:
        self.sh.run(["pmset", "displaysleepnow"])
        return "Turning the display off now - it locks if your Mac asks for a password on wake."

    def send_whatsapp_message(self, contact_name: str, message: str) -> str:
        webbrowser.open("https://web.whatsapp.com")
        return (f"WhatsApp automation isn't supported on macOS yet, so I opened WhatsApp Web - "
                f"you'll need to send '{message}' to {contact_name} yourself.")

    def list_services(self) -> list:
        rc, out = self.sh.run(["launchctl", "list"])
        if rc != 0:
            raise NotSupported("Listing services needs launchctl, which didn't work.")
        lines = out.splitlines()[1:] if out.splitlines() else []  # first line is the header
        return sorted({line.split()[-1] for line in lines if line.strip()})

    # ---- power/session (Tier 14) ----
    # macOS has no unprivileged "shut down/restart in N seconds, with a
    # clean way to cancel" primitive the way Windows' shutdown /t /a pair
    # gives - `shutdown` itself needs sudo, and pmset has no delayed
    # shutdown/restart at all. The honest approach taken here: spawn a
    # detached `delay N; shut down`-style AppleScript (self.sh.spawn, so
    # it doesn't block Wave while waiting, and - because it's a genuinely
    # separate process - it still fires even if Wave's own process exits
    # during the countdown). The real gap this leaves: once spawned, it
    # can't be cancelled - cancel_native_shutdown is deliberately NOT
    # overridden below, so the base class's honest NotSupported is what a
    # cancel attempt gets here (AppController still cancels its own timer
    # for sleep/hibernate/log-off regardless - see its own comments).
    def shutdown_now(self, delay_s: int) -> str:
        self.sh.spawn(["osascript", "-e", f"delay {max(0, int(delay_s))}",
                       "-e", 'tell application "System Events" to shut down'])
        return (f"Shutting down in {max(0, int(delay_s))} seconds - heads up, this one can't be "
                f"cancelled once started (see docs/PLATFORM.md).")

    def restart_now(self, delay_s: int) -> str:
        self.sh.spawn(["osascript", "-e", f"delay {max(0, int(delay_s))}",
                       "-e", 'tell application "System Events" to restart'])
        return (f"Restarting in {max(0, int(delay_s))} seconds - heads up, this one can't be "
                f"cancelled once started (see docs/PLATFORM.md).")

    def sleep_now(self) -> str:
        rc, _ = self.sh.run(["pmset", "sleepnow"])
        if rc == 0:
            return "Putting the Mac to sleep now."
        raise NotSupported("Sleeping needs pmset, which didn't work.")

    def hibernate_now(self) -> str:
        # Deliberately not faked with pmset sleepnow - macOS ties real
        # hibernation to its sleep/power settings (pmset -g, hibernatemode)
        # rather than offering any direct "hibernate now" trigger the way
        # Windows' SetSuspendState does, so calling plain sleep "hibernate"
        # here would be dishonest about what actually happened.
        raise NotSupported(
            "Hibernating on demand - macOS decides when to hibernate via its own sleep "
            "settings, with no direct trigger to call. Try sleep instead")

    def log_off_now(self) -> str:
        rc, _ = self.sh.run(["osascript", "-e", 'tell application "System Events" to log out'])
        if rc == 0:
            return "Logging off now."
        raise NotSupported("Logging off needs Accessibility access for System Events.")

    # ---- volume (Tier 14) ----
    # `set volume`/`get volume settings` are plain AppleScript, no
    # Accessibility permission needed (unlike the window-management
    # AppleScript below) - these work from a fresh install.
    def get_volume(self) -> int:
        rc, out = self.sh.run(["osascript", "-e", "output volume of (get volume settings)"])
        try:
            return max(0, min(100, int(out.strip())))
        except ValueError:
            raise NotSupported(f"Reading the volume didn't work (rc={rc}).")

    def set_volume(self, percent: int) -> str:
        pct = max(0, min(100, int(percent)))
        rc, _ = self.sh.run(["osascript", "-e", f"set volume output volume {pct}"])
        if rc == 0:
            return f"Volume set to {pct}%."
        raise NotSupported("Setting the volume didn't work.")

    def is_muted(self) -> bool:
        rc, out = self.sh.run(["osascript", "-e", "output muted of (get volume settings)"])
        if out.strip().lower() in ("true", "false"):
            return out.strip().lower() == "true"
        raise NotSupported(f"Reading the mute state didn't work (rc={rc}).")

    def mute_volume(self) -> str:
        rc, _ = self.sh.run(["osascript", "-e", "set volume output muted true"])
        if rc == 0:
            return "Muted."
        raise NotSupported("Muting didn't work.")

    def unmute_volume(self) -> str:
        rc, _ = self.sh.run(["osascript", "-e", "set volume output muted false"])
        if rc == 0:
            return "Unmuted."
        raise NotSupported("Unmuting didn't work.")

    # ---- brightness (Tier 14) ----
    # screen_brightness_control's macOS support is the least complete of
    # the three platforms (display-dependent, no DDC/CI on most built-in
    # panels) - same call as the other two backends, but the most likely
    # of the three to raise here; see docs/PLATFORM.md.
    def get_brightness(self) -> int:
        try:
            import screen_brightness_control as sbc
            return int(sbc.get_brightness(display=0)[0])
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Reading screen brightness didn't work: {e}") from e

    def set_brightness(self, percent: int) -> str:
        pct = max(0, min(100, int(percent)))
        try:
            import screen_brightness_control as sbc
            sbc.set_brightness(pct, display=0)
            return f"Brightness set to {pct}%."
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Setting screen brightness didn't work: {e}") from e

    # ---- radios and the light/dark theme (Tier 44) ----
    # networksetup for Wi-Fi (the Wi-Fi hardware port, usually en0),
    # blueutil (Homebrew) for Bluetooth, System Events for the theme.
    def _wifi_device(self) -> str:
        rc, out = self.sh.run(["networksetup", "-listallhardwareports"])
        lines = out.splitlines() if rc == 0 else []
        for i, line in enumerate(lines):
            if line.strip().lower() in ("hardware port: wi-fi", "hardware port: airport") and i + 1 < len(lines):
                device = lines[i + 1].split(":", 1)[-1].strip()
                if device:
                    return device
        return "en0"

    def get_radio(self, kind: str) -> bool:
        if kind == "wifi":
            rc, out = self.sh.run(["networksetup", "-getairportpower", self._wifi_device()])
            if rc != 0:
                raise NotSupported("Couldn't read the Wi-Fi state.")
            return out.strip().lower().endswith(": on")
        if not self.sh.which("blueutil"):
            raise self._unsupported("Reading the Bluetooth state (needs blueutil: brew install blueutil)")
        rc, out = self.sh.run(["blueutil", "-p"])
        if rc != 0:
            raise NotSupported("Couldn't read the Bluetooth state.")
        return out.strip() == "1"

    def set_radio(self, kind: str, on: bool) -> str:
        label = RADIO_LABELS.get(kind, kind)
        if kind == "wifi":
            argv = ["networksetup", "-setairportpower", self._wifi_device(), "on" if on else "off"]
        else:
            if not self.sh.which("blueutil"):
                raise self._unsupported("Turning Bluetooth on or off (needs blueutil: brew install blueutil)")
            argv = ["blueutil", "-p", "1" if on else "0"]
        rc, _ = self.sh.run(argv)
        if rc != 0:
            raise NotSupported(f"Couldn't turn {label} {'on' if on else 'off'}.")
        return f"{label} is now {'on' if on else 'off'}."

    def get_dark_mode(self) -> bool:
        rc, out = self.sh.run(["osascript", "-e",
                               'tell application "System Events" to tell appearance preferences to get dark mode'])
        if rc != 0:
            raise NotSupported("Couldn't read the light/dark theme.")
        return out.strip().lower() == "true"

    def set_dark_mode(self, on: bool) -> str:
        rc, _ = self.sh.run(["osascript", "-e", 'tell application "System Events" to tell appearance preferences '
                             f'to set dark mode to {"true" if on else "false"}'])
        if rc != 0:
            raise NotSupported("Couldn't switch the theme.")
        return f"Switched to {'dark' if on else 'light'} mode."

    # ---- OS services (Tier 8) ----
    # macOS doesn't have Windows/systemd's simple named-service model:
    # launchd jobs are identified by a reverse-DNS label (e.g.
    # "com.apple.bluetoothd"), not a plain word like "bluetooth" or "sshd" -
    # there's no small, fixed lookup table the way _PANES above translates
    # casual settings names, because the set of possible labels is open-
    # ended and installation-specific. launchctl start/stop (the older,
    # simpler subcommands, still present alongside the modern bootstrap/
    # bootout/kickstart ones) take a label directly and ask an ALREADY-
    # LOADED job to start/stop - the same precondition sc.exe/systemctl
    # already require on the other two platforms, just under a different
    # naming scheme. Callers need the real launchd label, not a casual
    # name; AppController (automation/app_control.py) does not attempt to
    # guess one from a plain name the way it can for settings panes. No
    # "is launchctl installed" check, unlike linux.py's systemctl check -
    # launchctl ships with every macOS the same way `open`/`pmset` do.
    def start_service(self, service_name: str) -> str:
        rc, _ = self.sh.run(["launchctl", "start", service_name])
        if rc == 0:
            return f"Successfully started the '{service_name}' service."
        return (f"Couldn't start the '{service_name}' service - macOS identifies services by "
                f"their launchd label (e.g. 'com.apple.bluetoothd'), not a plain name, so double "
                f"check that's what was passed; it may also not be loaded, or need sudo.")

    def stop_service(self, service_name: str) -> str:
        rc, _ = self.sh.run(["launchctl", "stop", service_name])
        if rc == 0:
            return f"Successfully stopped the '{service_name}' service."
        return (f"Couldn't stop the '{service_name}' service - macOS identifies services by "
                f"their launchd label (e.g. 'com.apple.bluetoothd'), not a plain name, so double "
                f"check that's what was passed; it may also not be loaded, or need sudo.")

    # ---- per-window management (Tier 8) ----
    # macOS has no built-in CLI for this the way wmctrl/xdotool give
    # Linux one (yabai/Rectangle etc. are third-party installs this
    # backend doesn't assume) - the standard, no-extra-dependency
    # mechanism is AppleScript via System Events, automating windows
    # through the same Accessibility permission pyautogui already needs
    # for close_window/close_tab above. Matching is AppleScript's own
    # "contains" (case-sensitive substring) across every visible
    # process's windows - first match wins, mirroring close_app's own
    # simplicity rather than trying to disambiguate several matches.
    # 🔧 Worth flagging: this is hand-written AppleScript, unverified
    # against a real Mac (same EXPERIMENTAL status as the rest of this
    # file) - see docs/MANUAL_TEST_CHECKLIST.md.
    def list_open_windows(self) -> list:
        script = (
            'tell application "System Events"\n'
            '    set windowTitles to {}\n'
            '    repeat with proc in (every process whose visible is true)\n'
            '        repeat with w in (every window of proc)\n'
            '            set end of windowTitles to (name of w)\n'
            '        end repeat\n'
            '    end repeat\n'
            "    set AppleScript's text item delimiters to linefeed\n"
            '    return windowTitles as text\n'
            'end tell'
        )
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Listing windows needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        return [line for line in out.splitlines() if line.strip()]

    def _window_action_script(self, window_name: str, body: str) -> str:
        """Shared skeleton for focus/minimize/snap below: find the first
        window across every visible process whose title contains
        window_name, run `body` against it, and report "ok"/"not found"."""
        name = _as_literal(window_name)
        return (
            'tell application "System Events"\n'
            '    repeat with proc in (every process whose visible is true)\n'
            '        repeat with w in (every window of proc)\n'
            f'            if (name of w) contains "{name}" then\n'
            f'                {body}\n'
            '                return "ok"\n'
            '            end if\n'
            '        end repeat\n'
            '    end repeat\n'
            '    return "not found"\n'
            'end tell'
        )

    def focus_window(self, window_name: str) -> str:
        script = self._window_action_script(
            window_name, 'set frontmost of proc to true\n                perform action "AXRaise" of w')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Focusing a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Switched to the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def minimize_window(self, window_name: str) -> str:
        script = self._window_action_script(window_name, 'set value of attribute "AXMinimized" of w to true')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Minimizing a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Minimized the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def snap_window(self, window_name: str, position: str) -> str:
        position = (position or "").lower().strip()
        # (screenWidth / 2) as integer: screenWidth/screenHeight themselves
        # are already plain integers (Finder's `bounds` is pixel
        # coordinates), but halving one can produce a real number, and
        # System Events' size/position lists want integers.
        if position == "left":
            geometry = ('set position of w to {0, 0}\n'
                        '                set size of w to {(screenWidth / 2) as integer, screenHeight}')
        elif position == "right":
            geometry = ('set position of w to {(screenWidth / 2) as integer, 0}\n'
                        '                set size of w to {(screenWidth / 2) as integer, screenHeight}')
        elif position == "maximize":
            geometry = 'set position of w to {0, 0}\n                set size of w to {screenWidth, screenHeight}'
        else:
            return f"'{position}' isn't a position I recognize - try left, right, or maximize."
        body = f'set frontmost of proc to true\n                {geometry}'
        script = (
            'tell application "Finder" to set screenBounds to bounds of window of desktop\n'
            'set screenWidth to (item 3 of screenBounds) - (item 1 of screenBounds)\n'
            'set screenHeight to (item 4 of screenBounds) - (item 2 of screenBounds)\n'
            + self._window_action_script(window_name, body)
        )
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Snapping a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Snapped the window matching '{window_name}' to {position}."
        return f"I couldn't find an open window matching '{window_name}'."

    # ---- the rest of per-window management (Tier 14) ----
    # Built on the same _window_action_script helper snap_window above
    # already uses. maximize_window literally reuses snap_window's own
    # "maximize" branch - macOS has no separate OS-tracked maximize flag
    # to target, so "fill the screen" (what snap_window already does) IS
    # this project's maximize. always-on-top and a true "restore the
    # previous size" have no AppleScript/System-Events equivalent at all
    # (no 3rd-party window-manager tool like Rectangle assumed installed)
    # - both raise NotSupported rather than faking it; see docs/PLATFORM.md.
    def get_window_rect(self, window_name: str):
        script = self._window_action_script(
            window_name,
            'set {px, py} to position of w\n'
            '                set {sw, sh} to size of w\n'
            '                return (px as text) & "," & (py as text) & "," & (sw as text) & "," & (sh as text)')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Reading a window's position needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        try:
            x, y, w, h = (int(n) for n in out.strip().split(","))
            return (x, y, w, h)
        except ValueError:
            return None

    def move_window(self, window_name: str, x: int, y: int) -> str:
        script = self._window_action_script(window_name, f'set position of w to {{{int(x)}, {int(y)}}}')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Moving a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Moved the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def resize_window(self, window_name: str, width: int, height: int) -> str:
        script = self._window_action_script(
            window_name, f'set size of w to {{{max(1, int(width))}, {max(1, int(height))}}}')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Resizing a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Resized the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def maximize_window(self, window_name: str) -> str:
        return self.snap_window(window_name, "maximize")

    def restore_window(self, window_name: str) -> str:
        # Scoped to "bring back a minimized window" - macOS keeps no
        # separate "size before it was maximized" to undo a maximize to.
        script = self._window_action_script(window_name, 'set visible of proc to true\n'
                                             '                perform action "AXRaise" of w')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Restoring a window needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() == "ok":
            return f"Restored the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def is_always_on_top(self, window_name: str) -> bool:
        raise NotSupported("Always-on-top isn't supported on macOS yet - there's no built-in window flag for it.")

    def set_always_on_top(self, window_name: str, enabled: bool) -> str:
        raise NotSupported("Always-on-top isn't supported on macOS yet - there's no built-in window flag for it.")

    def set_fullscreen(self, window_name: str) -> str:
        # Ctrl+Cmd+F is the native macOS fullscreen shortcut most apps
        # respect - F11 is macOS's SHOW DESKTOP key instead (see
        # show_desktop below), the opposite of Windows' own mapping.
        script = self._window_action_script(window_name, 'set frontmost of proc to true')
        rc, out = self.sh.run(["osascript", "-e", script])
        if rc != 0:
            raise NotSupported("Toggling fullscreen needs Accessibility access for System Events "
                                "(System Settings > Privacy & Security > Accessibility).")
        if out.strip() != "ok":
            return f"I couldn't find an open window matching '{window_name}'."
        try:
            import pyautogui
            pyautogui.hotkey('ctrl', 'command', 'f')
        except Exception as e:
            raise NotSupported(f"Toggling fullscreen didn't work: {e}") from e
        return f"Toggled fullscreen for the window matching '{window_name}'."

    def show_desktop(self) -> str:
        try:
            import pyautogui
            pyautogui.press('f11')
        except Exception as e:
            raise NotSupported(f"Showing the desktop didn't work: {e}") from e
        return "Showing the desktop."

    def switch_window(self) -> str:
        try:
            import pyautogui
            pyautogui.hotkey('command', 'tab')
        except Exception as e:
            raise NotSupported(f"Switching windows didn't work: {e}") from e
        return "Switched to the next window."

    # Tier 15: best-effort only - see base.py's own comment on why this
    # never raises and an empty string just means "couldn't tell."
    def get_active_window_title(self) -> str:
        try:
            rc, out = self.sh.run(["osascript", "-e",
                'tell application "System Events" to get name of first process whose frontmost is true'])
            return out.strip() if rc == 0 else ""
        except Exception:
            return ""

    def open_path(self, path: str) -> None:
        self.sh.spawn(["open", path])

    def reveal_path(self, path: str) -> None:
        self.sh.spawn(["open", "-R", path])

    # Tier 15: the four folder shortcuts that aren't a plain filesystem
    # path. "Computer"/"Trash" are genuine Finder objects (real AppleScript
    # nouns, not an approximation the way Linux's equivalents are);
    # "System Settings" was "System Preferences" before macOS Ventura, so
    # this tries the current name first and falls back to the old one
    # rather than guessing which this machine has.
    def open_special_location(self, name: str) -> str:
        locations = {
            "this_pc": (["osascript", "-e", 'tell application "Finder" to open computer'],
                        "Opened Computer in Finder."),
            "recycle_bin": (["osascript", "-e", 'tell application "Finder" to open trash'],
                             "Opened the Trash."),
            "task_manager": (["open", "-a", "Activity Monitor"], "Opened Activity Monitor."),
        }
        if name in locations:
            argv, label = locations[name]
            rc, out = self.sh.run(argv)
            return label if rc == 0 else f"Couldn't open that: {out}"
        if name == "control_panel":
            rc, out = self.sh.run(["open", "-a", "System Settings"])
            if rc == 0:
                return "Opened System Settings."
            rc, out = self.sh.run(["open", "-a", "System Preferences"])
            if rc == 0:
                return "Opened System Preferences."
            return f"Couldn't open System Settings: {out}"
        return f"'{name}' isn't one of the locations I know how to open."

    # Tier 15: T3/confirm-gated at the AppController level already - the
    # confirmation already happened in chat, so this doesn't also need
    # Finder's own "are you sure?" dialog; "empty trash" doesn't raise one
    # the way deleting a single file does, so there's nothing to suppress.
    def empty_recycle_bin(self) -> str:
        rc, out = self.sh.run(["osascript", "-e", 'tell application "Finder" to empty trash'])
        if rc != 0:
            return f"Couldn't empty the Trash: {out}"
        return "Emptied the Trash."

    def open_terminal_at(self, path: str) -> str:
        self.sh.spawn(["open", "-a", "Terminal", path])
        return f"Opened a terminal in {path}."

    def open_in_editor(self, path: str) -> str:
        self.sh.spawn(["open", "-a", "Visual Studio Code", path])
        return f"Opened {os.path.basename(path) or path} in VS Code."
