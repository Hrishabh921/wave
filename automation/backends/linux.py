"""
Linux reference backend (EXPERIMENTAL).

Uses the standard desktop tools (xdg-open, gtk-launch, nmcli, rfkill, loginctl,
gnome-control-center). Its command construction is unit-tested with a fake Shell
(tests/test_phase4_platform.py) but it has NOT been run on a real Linux desktop -
treat it as a starting point, then see docs/PLATFORM.md.
"""
import logging
import os
import shutil
import webbrowser
from typing import Optional

from automation.backends.base import NATIVE_SHUTDOWN_CANCELLED_MSG, NotSupported, PlatformBackend, Shell

logger = logging.getLogger("AppControl")

_BROWSERS = {
    "chrome": ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"),
    "google chrome": ("google-chrome", "google-chrome-stable"),
    "brave": ("brave-browser", "brave"), "brave browser": ("brave-browser", "brave"),
    "edge": ("microsoft-edge", "microsoft-edge-stable"), "microsoft edge": ("microsoft-edge", "microsoft-edge-stable"),
    "firefox": ("firefox",), "mozilla firefox": ("firefox",), "opera": ("opera",),
}
# Tier 5: expanded from 5 to 13 entries (roadmap's "~15 separate 'open X
# settings' intents"), using gnome-control-center's actual panel names
# (`gnome-control-center --list` on a stock GNOME 4x desktop). No "apps" or
# "airplane" entry - GNOME has no single panel for either (per-app
# permissions live inside Privacy, and there's no dedicated airplane-mode
# panel the way Windows/mobile have one; wifi/bluetooth panels cover the
# same ground).
_GNOME_PANELS = {
    "display": "display",
    "sound": "sound",
    "bluetooth": "bluetooth",
    "network": "wifi",
    "wifi": "wifi",
    "update": "info-overview",
    "personalization": "background",
    "privacy": "privacy",
    "battery": "power",
    "power": "power",
    "about": "info-overview",
    "nightlight": "display",
    "settings": "",
}
_TERMINALS = (
    ("x-terminal-emulator", lambda p: []), ("gnome-terminal", lambda p: [f"--working-directory={p}"]),
    ("konsole", lambda p: ["--workdir", p]), ("xfce4-terminal", lambda p: [f"--working-directory={p}"]),
)
# Tier 15: same "try each, first one found wins" shape as _TERMINALS above -
# there's no single system monitor / settings app across desktop environments
# the way there is on Windows/macOS.
_SYSTEM_MONITORS = ("gnome-system-monitor", "xfce4-taskmanager", "plasma-systemmonitor", "ksysguard", "mate-system-monitor")
_SETTINGS_APPS = ("gnome-control-center", "systemsettings5", "systemsettings", "xfce4-settings-manager", "unity-control-center")


class LinuxBackend(PlatformBackend):
    name = "linux"
    display_name = "Linux"

    def __init__(self, shell: Optional[Shell] = None):
        self.sh = shell or Shell()

    def launch_app(self, app_name: str) -> str:
        for cand in dict.fromkeys([app_name, app_name.replace(" ", "-"), app_name.replace(" ", "")]):
            exe = self.sh.which(cand)
            if exe:
                self.sh.spawn([exe])
                return f"Successfully opened {app_name}."
            if self.sh.which("gtk-launch") and self.sh.run(["gtk-launch", cand])[0] == 0:
                return f"Successfully opened {app_name}."
        return f"I couldn't find an app called '{app_name}' on this system."

    def find_browser(self, browser_name: str):
        for exe in _BROWSERS.get((browser_name or "").lower().strip(), ()):
            path = self.sh.which(exe)
            if path:
                return path
        return None

    def launch_in_browser(self, handle: str, url: str) -> None:
        self.sh.spawn([handle, url])

    def toggle_radio(self, command: str) -> str:
        command = command.lower()
        want = "off" if any(w in command for w in (" off", "disable", "band", "bandh")) else \
               "on" if any(w in command for w in (" on", "enable", "chalu", "start")) else None
        if "wifi" in command or "wi-fi" in command:
            if not self.sh.which("nmcli"):
                raise NotSupported("Toggling Wi-Fi needs NetworkManager (nmcli), which wasn't found.")
            if want is None:
                _, state = self.sh.run(["nmcli", "radio", "wifi"])
                want = "off" if state.strip().lower() == "enabled" else "on"
            self.sh.run(["nmcli", "radio", "wifi", want])
            _, state = self.sh.run(["nmcli", "radio", "wifi"])
            return f"Wi-Fi is now {'on' if state.strip().lower() == 'enabled' else 'off'}."
        if "bluetooth" in command:
            if not self.sh.which("rfkill"):
                raise NotSupported("Toggling Bluetooth needs rfkill, which wasn't found.")
            if want is None:
                raise NotSupported("Say whether to turn Bluetooth on or off - there's no safe 'toggle' on this system.")
            self.sh.run(["rfkill", "unblock" if want == "on" else "block", "bluetooth"])
            return f"Asked the system to turn Bluetooth {want}."
        return "Hardware toggle not recognized."

    def close_window(self) -> None:
        # pyautogui's Linux backend (python-xlib) reads os.environ['DISPLAY']
        # with no .get() default, so on a real headless box (no X server,
        # DISPLAY unset) this used to blow up with a raw, unhandled
        # KeyError: 'DISPLAY' instead of the friendly NotSupported every
        # other method in this file raises when a tool it needs is missing.
        if not os.environ.get("DISPLAY"):
            raise NotSupported("Closing the focused window needs a running X session (DISPLAY isn't set).")
        try:
            import pyautogui
            pyautogui.hotkey('alt', 'f4')
        except SystemExit as e:
            # 🔧 Tier 9: confirmed for real against a live X server - `import
            # pyautogui` itself calls sys.exit(...) (not a normal exception)
            # when its optional mouseinfo dependency can't find tkinter, which
            # a real Linux box has no guarantee of having installed. SystemExit
            # is deliberately NOT an Exception subclass, so the except clause
            # below never caught this - it took the whole app down instead of
            # giving the same friendly NotSupported as every other failure
            # here. mouseinfo is only used by pyautogui's own debugging GUI,
            # never by hotkey(), so this is safe to treat as just another
            # "the tool this needed isn't available" case.
            raise NotSupported(f"Closing the focused window didn't work: {e}") from e
        except Exception as e:
            raise NotSupported(f"Closing the focused window didn't work: {e}") from e

    def close_tab(self) -> None:
        if not os.environ.get("DISPLAY"):
            raise NotSupported("Closing the current tab needs a running X session (DISPLAY isn't set).")
        try:
            import pyautogui
            pyautogui.hotkey('ctrl', 'w')
        except SystemExit as e:
            # 🔧 Tier 9: see close_window()'s own comment above - same
            # real, confirmed failure mode, same fix.
            raise NotSupported(f"Closing the current tab didn't work: {e}") from e
        except Exception as e:
            raise NotSupported(f"Closing the current tab didn't work: {e}") from e

    def open_system_setting(self, setting_type: str) -> str:
        if not self.sh.which("gnome-control-center"):
            raise NotSupported("Opening settings needs GNOME's control center, which wasn't found.")
        setting_type = setting_type.lower()
        for key, panel in _GNOME_PANELS.items():
            if key in setting_type:
                self.sh.spawn(["gnome-control-center", *([panel] if panel else [])])
                return f"Successfully opened {key.capitalize()} settings for you."
        return "Could not find the requested system setting."

    def lock_screen(self) -> str:
        for argv in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
            if self.sh.which(argv[0]) and self.sh.run(argv)[0] == 0:
                return "Locking your screen now."
        raise NotSupported("Locking the screen needs loginctl or xdg-screensaver, and neither worked.")

    def send_whatsapp_message(self, contact_name: str, message: str) -> str:
        webbrowser.open("https://web.whatsapp.com")
        return (f"WhatsApp automation isn't supported on Linux yet, so I opened WhatsApp Web - "
                f"you'll need to send '{message}' to {contact_name} yourself.")

    # ---- OS services (Tier 8) ----
    # systemd units, via systemctl - same "check the tool exists, NotSupported
    # if not" shape as toggle_radio's nmcli/rfkill checks and open_system_setting's
    # gnome-control-center check above, rather than assuming every Linux box
    # runs systemd. self.sh.run() only surfaces stdout/returncode (by design -
    # see automation/backends/base.py's Shell), and systemctl's own diagnostic
    # text for a start/stop failure goes to stderr, so a failure here is kept
    # honestly plain rather than parsing output this sandbox can't verify
    # against a real systemd - this backend is still EXPERIMENTAL (see the
    # module docstring).
    def start_service(self, service_name: str) -> str:
        if not self.sh.which("systemctl"):
            raise NotSupported("Starting services needs systemd (systemctl), which wasn't found.")
        rc, _ = self.sh.run(["systemctl", "start", service_name])
        if rc == 0:
            return f"Successfully started the '{service_name}' service."
        return (f"Couldn't start the '{service_name}' service - it may not exist, or starting "
                f"it may need elevated privileges (sudo).")

    def stop_service(self, service_name: str) -> str:
        if not self.sh.which("systemctl"):
            raise NotSupported("Stopping services needs systemd (systemctl), which wasn't found.")
        rc, _ = self.sh.run(["systemctl", "stop", service_name])
        if rc == 0:
            return f"Successfully stopped the '{service_name}' service."
        return (f"Couldn't stop the '{service_name}' service - it may not exist, or stopping "
                f"it may need elevated privileges (sudo).")

    def list_services(self) -> list:
        if not self.sh.which("systemctl"):
            raise NotSupported("Listing services needs systemd (systemctl), which wasn't found.")
        _, out = self.sh.run(["systemctl", "list-units", "--type=service", "--state=running",
                              "--no-legend", "--no-pager"])
        return sorted({line.split()[0] for line in out.splitlines() if line.strip()})

    # ---- power/session (Tier 14) ----
    # `shutdown`'s own delay argument is in whole MINUTES, not seconds
    # (unlike Windows' shutdown /t) - delay_s is rounded up to the nearest
    # minute (floor of 1, so a short delay still schedules something
    # rather than firing immediately), honestly coarser than the Windows
    # backend's second-level precision. systemctl suspend/hibernate are
    # the standard systemd session actions; log-off tries GNOME's own
    # session-quit first (this file is already GNOME-control-center-
    # biased - see _GNOME_PANELS above) before falling back to loginctl,
    # which needs the real username, not a guess.
    def _delay_minutes(self, delay_s: int) -> str:
        return "now" if delay_s < 30 else f"+{max(1, round(delay_s / 60))}"

    # 🔧 FIX: both messages used to always claim "about N minute(s)" from
    # the RAW delay_s, even when _delay_minutes() just rounded that same
    # delay down to "now" (anything under 30s, which includes
    # POWER_ACTION_DELAY_S's own 15s default) - telling the user they had
    # roughly a minute to say "cancel shutdown" when the real `shutdown`
    # command underneath had already been told to act immediately, with
    # no real cancellable window at all. Both messages now read off the
    # same minutes string that was actually passed to the command, so
    # they can never promise a countdown the command isn't honoring.
    def shutdown_now(self, delay_s: int) -> str:
        if not self.sh.which("shutdown"):
            raise NotSupported("Shutting down needs the 'shutdown' command, which wasn't found.")
        minutes = self._delay_minutes(delay_s)
        self.sh.run(["shutdown", "-h", minutes])
        if minutes == "now":
            return ("Shutting down now - Linux's shutdown delay is minute-granularity, too "
                    "coarse for a cancellable countdown this short.")
        return f"Shutting down in about {minutes[1:]} minute(s). Say \"cancel shutdown\" to stop it."

    def restart_now(self, delay_s: int) -> str:
        if not self.sh.which("shutdown"):
            raise NotSupported("Restarting needs the 'shutdown' command, which wasn't found.")
        minutes = self._delay_minutes(delay_s)
        self.sh.run(["shutdown", "-r", minutes])
        if minutes == "now":
            return ("Restarting now - Linux's shutdown delay is minute-granularity, too "
                    "coarse for a cancellable countdown this short.")
        return f"Restarting in about {minutes[1:]} minute(s). Say \"cancel shutdown\" to stop it."

    def cancel_native_shutdown(self) -> str:
        if not self.sh.which("shutdown"):
            raise NotSupported("Cancelling a shutdown needs the 'shutdown' command, which wasn't found.")
        rc, _ = self.sh.run(["shutdown", "-c"])
        return NATIVE_SHUTDOWN_CANCELLED_MSG if rc == 0 else \
            "There wasn't a shutdown or restart scheduled to cancel."

    def sleep_now(self) -> str:
        if not self.sh.which("systemctl"):
            raise NotSupported("Sleeping needs systemd (systemctl), which wasn't found.")
        self.sh.run(["systemctl", "suspend"])
        return "Putting the PC to sleep now."

    def hibernate_now(self) -> str:
        if not self.sh.which("systemctl"):
            raise NotSupported("Hibernating needs systemd (systemctl), which wasn't found.")
        self.sh.run(["systemctl", "hibernate"])
        return "Hibernating the PC now."

    def log_off_now(self) -> str:
        if self.sh.which("gnome-session-quit"):
            self.sh.run(["gnome-session-quit", "--logout", "--no-prompt"])
            return "Logging off now."
        if self.sh.which("loginctl"):
            import getpass
            try:
                user = getpass.getuser()
            except Exception:
                user = ""
            if user:
                self.sh.run(["loginctl", "terminate-user", user])
                return "Logging off now."
        raise NotSupported("Logging off needs gnome-session-quit or loginctl, and neither worked.")

    # ---- volume (Tier 14) ----
    # pactl (PulseAudio/PipeWire's own CLI, standard on most modern desktop
    # distros) is tried first; amixer (plain ALSA) is the fallback for a
    # system without a sound server running. Both are plain-text CLIs with
    # no JSON mode, so parsing is a small, deliberately forgiving regex
    # rather than a strict format match - unverified against a real
    # desktop, like the rest of this file (see the module docstring).
    def _pactl_sink(self) -> str:
        return "@DEFAULT_SINK@"

    def get_volume(self) -> int:
        import re
        if self.sh.which("pactl"):
            _, out = self.sh.run(["pactl", "get-sink-volume", self._pactl_sink()])
            m = re.search(r"(\d+)%", out)
            if m:
                return int(m.group(1))
        if self.sh.which("amixer"):
            _, out = self.sh.run(["amixer", "get", "Master"])
            m = re.search(r"\[(\d+)%\]", out)
            if m:
                return int(m.group(1))
        raise NotSupported("Reading the volume needs pactl or amixer, and neither worked.")

    def set_volume(self, percent: int) -> str:
        pct = max(0, min(100, int(percent)))
        if self.sh.which("pactl"):
            rc, _ = self.sh.run(["pactl", "set-sink-volume", self._pactl_sink(), f"{pct}%"])
            if rc == 0:
                return f"Volume set to {pct}%."
        if self.sh.which("amixer"):
            rc, _ = self.sh.run(["amixer", "set", "Master", f"{pct}%"])
            if rc == 0:
                return f"Volume set to {pct}%."
        raise NotSupported("Setting the volume needs pactl or amixer, and neither worked.")

    def is_muted(self) -> bool:
        import re
        if self.sh.which("pactl"):
            _, out = self.sh.run(["pactl", "get-sink-mute", self._pactl_sink()])
            if out.strip():
                return "yes" in out.lower()
        if self.sh.which("amixer"):
            _, out = self.sh.run(["amixer", "get", "Master"])
            m = re.search(r"\[(on|off)\]", out)
            if m:
                return m.group(1) == "off"
        raise NotSupported("Reading the mute state needs pactl or amixer, and neither worked.")

    def mute_volume(self) -> str:
        if self.sh.which("pactl"):
            rc, _ = self.sh.run(["pactl", "set-sink-mute", self._pactl_sink(), "1"])
            if rc == 0:
                return "Muted."
        if self.sh.which("amixer"):
            rc, _ = self.sh.run(["amixer", "set", "Master", "mute"])
            if rc == 0:
                return "Muted."
        raise NotSupported("Muting needs pactl or amixer, and neither worked.")

    def unmute_volume(self) -> str:
        if self.sh.which("pactl"):
            rc, _ = self.sh.run(["pactl", "set-sink-mute", self._pactl_sink(), "0"])
            if rc == 0:
                return "Unmuted."
        if self.sh.which("amixer"):
            rc, _ = self.sh.run(["amixer", "set", "Master", "unmute"])
            if rc == 0:
                return "Unmuted."
        raise NotSupported("Unmuting needs pactl or amixer, and neither worked.")

    # ---- brightness (Tier 14) ----
    # screen_brightness_control already picks the right Linux mechanism
    # for itself (sysfs backlight, xrandr, or ddcutil over DDC/CI) - same
    # single cross-platform call as the Windows backend, see its comment there.
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

    # ---- per-window management (Tier 8) ----
    # wmctrl for list/focus/minimize (a small, standard EWMH client,
    # present on most X11 desktop installs); xdotool for snap_window,
    # since it can resize/move AND give screen geometry through one
    # coherent tool rather than mixing two. Both raise NotSupported if
    # missing, same as the nmcli/rfkill/gnome-control-center checks above -
    # and both are fundamentally X11 tools, so (like close_window/
    # close_tab's own DISPLAY check) there's a real, known gap under a
    # native Wayland session even when the tool itself is installed; see
    # docs/PLATFORM.md.
    def list_open_windows(self) -> list:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Listing windows needs wmctrl, which wasn't found.")
        _, out = self.sh.run(["wmctrl", "-l"])
        titles = []
        for line in out.splitlines():
            parts = line.split(None, 3)   # id, desktop, client machine, title
            if len(parts) == 4:
                titles.append(parts[3])
        return titles

    def focus_window(self, window_name: str) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Focusing a window needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-a", window_name])
        if rc == 0:
            return f"Switched to the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def minimize_window(self, window_name: str) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Minimizing a window needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-r", window_name, "-b", "add,hidden"])
        if rc == 0:
            return f"Minimized the window matching '{window_name}'."
        return f"I couldn't find an open window matching '{window_name}'."

    def snap_window(self, window_name: str, position: str) -> str:
        position = (position or "").lower().strip()
        if position not in ("left", "right", "maximize"):
            return f"'{position}' isn't a position I recognize - try left, right, or maximize."
        if not self.sh.which("xdotool"):
            raise NotSupported("Snapping a window needs xdotool, which wasn't found.")
        _, found = self.sh.run(["xdotool", "search", "--name", window_name])
        win_id = found.splitlines()[0].strip() if found.strip() else ""
        if not win_id:
            return f"I couldn't find an open window matching '{window_name}'."
        _, geom = self.sh.run(["xdotool", "getdisplaygeometry"])
        try:
            width, height = (int(n) for n in geom.split())
        except ValueError:
            return "Couldn't read the screen size, so I didn't try to move the window."
        self.sh.run(["xdotool", "windowactivate", win_id])
        if position == "left":
            self.sh.run(["xdotool", "windowsize", win_id, str(width // 2), str(height)])
            self.sh.run(["xdotool", "windowmove", win_id, "0", "0"])
        elif position == "right":
            self.sh.run(["xdotool", "windowsize", win_id, str(width // 2), str(height)])
            self.sh.run(["xdotool", "windowmove", win_id, str(width // 2), "0"])
        else:  # maximize
            self.sh.run(["xdotool", "windowmove", win_id, "0", "0"])
            self.sh.run(["xdotool", "windowsize", win_id, str(width), str(height)])
        return f"Snapped the window matching '{window_name}' to {position}."

    # ---- the rest of per-window management (Tier 14) ----
    # xdotool for geometry/move/resize (same tool snap_window above
    # already needs for screen geometry); wmctrl's "-b" state flags for
    # maximize/restore/above/fullscreen (the same EWMH states list_open_
    # windows/focus_window/minimize_window already lean on through
    # wmctrl); xprop to read one of those states back for
    # is_always_on_top (no "-b" READ equivalent in wmctrl itself).
    # switch_window sends the raw Alt+Tab keypress rather than reimplementing
    # a window switcher - what that keypress actually does depends on the
    # window manager, unlike Windows' own alt-tab which always works the
    # same way; an honest, documented gap in this experimental backend.
    def _xdotool_window_id(self, window_name: str) -> str:
        if not self.sh.which("xdotool"):
            raise NotSupported("Window geometry needs xdotool, which wasn't found.")
        _, found = self.sh.run(["xdotool", "search", "--name", window_name])
        return found.splitlines()[0].strip() if found.strip() else ""

    def get_window_rect(self, window_name: str):
        win_id = self._xdotool_window_id(window_name)
        if not win_id:
            return None
        _, out = self.sh.run(["xdotool", "getwindowgeometry", "--shell", win_id])
        vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        try:
            return (int(vals["X"]), int(vals["Y"]), int(vals["WIDTH"]), int(vals["HEIGHT"]))
        except (KeyError, ValueError):
            return None

    def move_window(self, window_name: str, x: int, y: int) -> str:
        win_id = self._xdotool_window_id(window_name)
        if not win_id:
            return f"I couldn't find an open window matching '{window_name}'."
        self.sh.run(["xdotool", "windowmove", win_id, str(int(x)), str(int(y))])
        return f"Moved the window matching '{window_name}'."

    def resize_window(self, window_name: str, width: int, height: int) -> str:
        win_id = self._xdotool_window_id(window_name)
        if not win_id:
            return f"I couldn't find an open window matching '{window_name}'."
        self.sh.run(["xdotool", "windowsize", win_id, str(max(1, int(width))), str(max(1, int(height)))])
        return f"Resized the window matching '{window_name}'."

    def maximize_window(self, window_name: str) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Maximizing a window needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-r", window_name, "-b", "add,maximized_vert,maximized_horz"])
        return (f"Maximized the window matching '{window_name}'." if rc == 0 else
                f"I couldn't find an open window matching '{window_name}'.")

    def restore_window(self, window_name: str) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Restoring a window needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-r", window_name, "-b", "remove,maximized_vert,maximized_horz"])
        return (f"Restored the window matching '{window_name}'." if rc == 0 else
                f"I couldn't find an open window matching '{window_name}'.")

    def is_always_on_top(self, window_name: str) -> bool:
        win_id = self._xdotool_window_id(window_name)
        if not win_id or not self.sh.which("xprop"):
            return False
        _, out = self.sh.run(["xprop", "-id", win_id, "_NET_WM_STATE"])
        return "_NET_WM_STATE_ABOVE" in out

    def set_always_on_top(self, window_name: str, enabled: bool) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Setting always-on-top needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-r", window_name, "-b", f"{'add' if enabled else 'remove'},above"])
        if rc != 0:
            return f"I couldn't find an open window matching '{window_name}'."
        return (f"The window matching '{window_name}' will now stay on top." if enabled else
                f"The window matching '{window_name}' will no longer stay on top.")

    def set_fullscreen(self, window_name: str) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Toggling fullscreen needs wmctrl, which wasn't found.")
        rc, _ = self.sh.run(["wmctrl", "-r", window_name, "-b", "toggle,fullscreen"])
        return (f"Toggled fullscreen for the window matching '{window_name}'." if rc == 0 else
                f"I couldn't find an open window matching '{window_name}'.")

    def show_desktop(self) -> str:
        if not self.sh.which("wmctrl"):
            raise NotSupported("Showing the desktop needs wmctrl, which wasn't found.")
        self.sh.run(["wmctrl", "-k", "on"])
        return "Showing the desktop."

    def switch_window(self) -> str:
        if not os.environ.get("DISPLAY"):
            raise NotSupported("Switching windows needs a running X session (DISPLAY isn't set).")
        if not self.sh.which("xdotool"):
            raise NotSupported("Switching windows needs xdotool, which wasn't found.")
        self.sh.run(["xdotool", "key", "alt+Tab"])
        return "Sent Alt+Tab - what that does next depends on your window manager."

    # Tier 15: best-effort only - see base.py's own comment on why this
    # never raises and an empty string just means "couldn't tell."
    def get_active_window_title(self) -> str:
        if not os.environ.get("DISPLAY") or not self.sh.which("xdotool"):
            return ""
        rc, out = self.sh.run(["xdotool", "getactivewindow", "getwindowname"])
        return out.strip() if rc == 0 else ""

    def open_path(self, path: str) -> None:
        self.sh.spawn(["xdg-open", path])

    def reveal_path(self, path: str) -> None:
        self.sh.spawn(["xdg-open", os.path.dirname(os.path.abspath(path)) if os.path.isfile(path) else path])

    # Tier 15: the four folder shortcuts that aren't a plain filesystem
    # path. "This PC" and "Recycle Bin" are honest best-effort guesses,
    # not a real equivalent - Linux has no single "my computer" view, and
    # not every file manager renders the XDG trash folder specially, but
    # both are more useful than a flat refusal and the reply says plainly
    # what was actually opened. "Task Manager"/"Control Panel" try each
    # known app for the desktop environment in turn, same shape as
    # open_terminal_at's own _TERMINALS list just above.
    def open_special_location(self, name: str) -> str:
        if name == "this_pc":
            self.sh.spawn(["xdg-open", os.path.expanduser("~")])
            return "Linux has no single 'This PC' view - opened your home folder instead."
        if name == "recycle_bin":
            trash_dir = os.path.join(os.path.expanduser("~"), ".local", "share", "Trash", "files")
            os.makedirs(trash_dir, exist_ok=True)
            self.sh.spawn(["xdg-open", trash_dir])
            return "Opened the trash folder."
        if name == "task_manager":
            for exe in _SYSTEM_MONITORS:
                found = self.sh.which(exe)
                if found:
                    self.sh.spawn([found])
                    return f"Opened {exe}."
            raise NotSupported("No system monitor app (gnome-system-monitor, xfce4-taskmanager, ...) was found.")
        if name == "control_panel":
            for exe in _SETTINGS_APPS:
                found = self.sh.which(exe)
                if found:
                    self.sh.spawn([found])
                    return f"Opened {exe}."
            raise NotSupported("No settings app (gnome-control-center, systemsettings, ...) was found.")
        return f"'{name}' isn't one of the locations I know how to open."

    # Tier 15: T3/confirm-gated at the AppController level already; nothing
    # here asks a second time. XDG trash has no single empty-it command,
    # so this just deletes the two folders the spec defines it as -
    # genuinely gone either way, same honesty as the roadmap asks for.
    def empty_recycle_bin(self) -> str:
        trash_home = os.path.join(os.path.expanduser("~"), ".local", "share", "Trash")
        files_dir, info_dir = os.path.join(trash_home, "files"), os.path.join(trash_home, "info")
        try:
            removed = 0
            for d in (files_dir, info_dir):
                if not os.path.isdir(d):
                    continue
                for entry in os.listdir(d):
                    full = os.path.join(d, entry)
                    removed += 1
                    if os.path.isdir(full) and not os.path.islink(full):
                        shutil.rmtree(full, ignore_errors=True)
                    else:
                        os.remove(full)
            if removed == 0 and not os.path.isdir(files_dir):
                return "The trash folder doesn't exist yet - nothing to empty."
            return "Emptied the trash."
        except Exception as e:
            logger.error(f"empty_recycle_bin error: {e}")
            return "Couldn't empty the trash."

    def open_terminal_at(self, path: str) -> str:
        for exe, args in _TERMINALS:
            found = self.sh.which(exe)
            if found:
                self.sh.spawn([found, *args(path)], cwd=path)
                return f"Opened a terminal in {path}."
        raise NotSupported("No terminal emulator was found.")

    def open_in_editor(self, path: str) -> str:
        code = self.sh.which("code")
        if not code:
            return "VS Code's 'code' command wasn't found on PATH."
        self.sh.spawn([code, path])
        return f"Opened {os.path.basename(path) or path} in VS Code."

    def desktop_dir(self) -> str:
        if self.sh.which("xdg-user-dir"):
            rc, out = self.sh.run(["xdg-user-dir", "DESKTOP"])
            if rc == 0 and out and os.path.isabs(out):
                return out
        return super().desktop_dir()
