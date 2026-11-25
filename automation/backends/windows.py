"""
Windows backend.

Everything in here is the code that used to live inside AppController, moved
across unchanged (same keystrokes, same sleeps, same wording of every reply) -
only the structure changed: each former AppController method that was really
"how Windows does X" is now one method of this class. tests/test_phase4_platform.py
pins the exact sequence of OS calls for each one so a refactor can't drift.
"""
import logging
import os
import shutil
import subprocess
import time

import pyautogui

try:
    import winreg
    HAS_WINREG = True
except ImportError:
    HAS_WINREG = False

try:
    import win32gui
    import win32con
    HAS_WIN32GUI = True
except ImportError:
    HAS_WIN32GUI = False

# 🔧 FIX (Tier 14): _volume_endpoint() below checks this name - without this
# try/except (the same shape HAS_WINREG/HAS_WIN32GUI just above already
# use), HAS_PYCAW was never defined in this module at all, so every volume
# call raised a NameError instead of the intended, friendly NotSupported.
# get_volume()/set_volume()/etc.'s own broad `except Exception` already
# caught that NameError and re-wrapped it as a NotSupported either way, so
# this wasn't a crash in practice - just a wrong, confusing message
# ("...didn't work: name 'HAS_PYCAW' is not defined") on EVERY Windows
# machine, pycaw installed or not, since the name was never resolvable.
try:
    import pycaw.pycaw  # noqa: F401  (import-only check; _volume_endpoint() does the real import)
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

from automation.backends.base import NATIVE_SHUTDOWN_CANCELLED_MSG, NotSupported, PlatformBackend
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr

logger = logging.getLogger("AppControl")

# WhatsApp Desktop install location Windows normally uses (Store/Squirrel installs
# land under %LOCALAPPDATA%\WhatsApp).
_WHATSAPP_EXE_RELATIVE = os.path.join("WhatsApp", "WhatsApp.exe")

# Lets a command name a SPECIFIC browser ("on brave", "in chrome").
_BROWSER_EXE_NAMES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "brave": "brave.exe", "brave browser": "brave.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "mozilla firefox": "firefox.exe",
    "opera": "opera.exe",
}


def _is_whatsapp_desktop_installed() -> bool:
    """Best-effort check for whether WhatsApp Desktop is actually installed,
    so the fallback can tell "it's installed but slow to open" apart
    from "it's not installed at all" and respond honestly to each."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        candidate = os.path.join(local_appdata, _WHATSAPP_EXE_RELATIVE)
        if os.path.isfile(candidate):
            return True
    if HAS_WINREG:
        uninstall_roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, root_path in uninstall_roots:
            try:
                with winreg.OpenKey(hive, root_path) as root_key:
                    i = 0
                    while True:
                        try:
                            subkey_name = winreg.EnumKey(root_key, i)
                        except OSError:
                            break
                        i += 1
                        if "whatsapp" in subkey_name.lower():
                            return True
            except OSError:
                continue
    return False


class WindowsBackend(PlatformBackend):
    name = "windows"
    display_name = "Windows"

    # Tier 5: expanded from 5 to 16 entries so most of what System Settings
    # itself exposes has a direct voice/text shortcut (roadmap: "a full
    # settings-pages shortcut set (~15 separate 'open X settings' intents in
    # Sara vs a handful in Wave)"). Every value is a real `ms-settings:` URI
    # (Microsoft's own documented deep-link scheme), so this is just widening
    # the map, not inventing new mechanism.
    SETTINGS_MAP = {
        "display": "ms-settings:display",
        "sound": "ms-settings:sound",
        "bluetooth": "ms-settings:bluetooth",
        "network": "ms-settings:network",
        "wifi": "ms-settings:network-wifi",
        "update": "ms-settings:windowsupdate",
        "apps": "ms-settings:appsfeatures",
        "personalization": "ms-settings:personalization",
        "privacy": "ms-settings:privacy",
        "storage": "ms-settings:storagesense",
        "battery": "ms-settings:batterysaver",
        "power": "ms-settings:powersleep",
        "about": "ms-settings:about",
        "nightlight": "ms-settings:nightlight",
        "airplane": "ms-settings:network-airplanemode",
        "settings": "ms-settings:",
    }

    # ---- apps & browsers ----
    def launch_app(self, app_name: str) -> str:
        # Tier 43: straight from Windows' list of installed apps when the
        # name clearly means one (automation/app_index.py) - no typing into
        # the Start menu, no chance of typing into the wrong window.
        from automation.app_index import app_index  # noqa: PLC0415
        entry = app_index.resolve(app_name)
        if entry is not None:
            try:
                app_index.launch(entry)
                return f"Successfully opened {entry['name']}."
            except Exception as e:
                logger.warning(f"Opening {entry['name']} from the app index failed ({e}) - using the Start menu")
        try:
            pyautogui.press('win')
            time.sleep(0.5)
            pyautogui.write(app_name, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')
            return f"Successfully opened {app_name}."
        except Exception as e:
            logger.error(f"Failed to open {app_name}: {e}")
            return f"Error opening {app_name}."

    def find_browser(self, browser_name: str):
        """Finds a specific browser's .exe regardless of where it's installed,
        using Windows' own "App Paths" registry (the same mechanism every major
        browser registers itself with) - falls back to checking PATH."""
        exe_name = _BROWSER_EXE_NAMES.get((browser_name or "").lower().strip())
        if not exe_name:
            return None
        if HAS_WINREG:
            key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                        if path and os.path.isfile(path):
                            return path
                except (FileNotFoundError, OSError):
                    continue
        return shutil.which(exe_name)

    def launch_in_browser(self, handle: str, url: str) -> None:
        subprocess.Popen([handle, url])

    # ---- system ----
    def toggle_radio(self, command: str) -> str:
        command = command.lower()
        # keys checked in this order (hotspot, bluetooth, wifi), matching the original priority exactly.
        targets = {
            "hotspot": ("ms-settings:network-mobilehotspot", "Mobile Hotspot"),
            "bluetooth": ("ms-settings:bluetooth", "Bluetooth"),
            "wifi": ("ms-settings:network-wifi", "Wi-Fi"),
        }
        try:
            for key, (uri, label) in targets.items():
                if key in command:
                    os.system(f"start {uri}")
                    time.sleep(1.2)
                    pyautogui.press('space')
                    time.sleep(0.2)
                    pyautogui.hotkey('alt', 'f4')
                    # A blind spacebar press just TOGGLES whatever checkbox already had focus, and
                    # there's no reliable way from here to read the resulting state - so the honest
                    # answer is "toggled", not a guaranteed on/off.
                    return f"Toggled {label} - check that it's now set the way you wanted."
            return "Hardware toggle not recognized."
        except Exception as e:
            logger.error(f"toggle_hardware error: {e}")
            return "Failed to toggle hardware settings."

    def close_window(self) -> None:
        pyautogui.hotkey('alt', 'f4')

    def close_tab(self) -> None:
        pyautogui.hotkey('ctrl', 'w')

    def open_system_setting(self, setting_type: str) -> str:
        setting_type = setting_type.lower()
        for key, command in self.SETTINGS_MAP.items():
            if key in setting_type:
                os.system(f"start {command}")
                return f"Successfully opened {key.capitalize()} settings for you."
        return "Could not find the requested system setting."

    def lock_screen(self) -> str:
        os.system("rundll32.exe user32.dll,LockWorkStation")
        return "Locking your PC now."

    def type_in_text_editor(self, text: str) -> str:
        try:
            self.launch_app("notepad")
            time.sleep(2)
            pyautogui.write(text, interval=0.05)
            return "Successfully opened Notepad and wrote the text."
        except Exception as e:
            return "Failed to type the text."

    # ---- OS services (Tier 8) ----
    # sc.exe's own stdout/stderr already explains a failure precisely (no
    # such service, access denied, service already running/stopped) -
    # surfaced verbatim rather than re-guessed, the same choice
    # open_in_editor above makes when VS Code's 'code' command is missing.
    # Needing elevation is itself the normal, expected way most of these
    # fail under a non-admin Wave process - that comes back as an ordinary
    # reply, not an exception, same as "no such app" from launch_app.
    def start_service(self, service_name: str) -> str:
        try:
            result = subprocess.run(["sc", "start", service_name], capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return f"Successfully started the '{service_name}' service."
            detail = (result.stdout or result.stderr or "").strip()
            return f"Couldn't start the '{service_name}' service" + (f": {detail}" if detail else ".")
        except subprocess.TimeoutExpired:
            return f"Starting the '{service_name}' service timed out."
        except Exception as e:
            logger.error(f"start_service({service_name!r}) failed: {e}")
            return f"Failed to start the '{service_name}' service."

    def stop_service(self, service_name: str) -> str:
        try:
            result = subprocess.run(["sc", "stop", service_name], capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return f"Successfully stopped the '{service_name}' service."
            detail = (result.stdout or result.stderr or "").strip()
            return f"Couldn't stop the '{service_name}' service" + (f": {detail}" if detail else ".")
        except subprocess.TimeoutExpired:
            return f"Stopping the '{service_name}' service timed out."
        except Exception as e:
            logger.error(f"stop_service({service_name!r}) failed: {e}")
            return f"Failed to stop the '{service_name}' service."

    # ---- power/session (Tier 14) ----
    # shutdown.exe's own /t flag only works with /s and /r (confirmed in
    # its documented syntax) - /h (hibernate) and /l (log off) ignore it,
    # which is why sleep/hibernate/log-off are the immediate action and
    # AppController times those three with its own cancellable timer
    # instead (see automation/backends/base.py's module comment on this
    # section). SetSuspendState is the same Win32 call Windows' own power
    # button/Start menu "Sleep"/"Hibernate" entries use.
    def shutdown_now(self, delay_s: int) -> str:
        try:
            subprocess.run(["shutdown", "/s", "/t", str(max(0, int(delay_s)))],
                           capture_output=True, text=True, timeout=10)
            return f"Shutting down in {max(0, int(delay_s))} seconds. Say \"cancel shutdown\" to stop it."
        except Exception as e:
            logger.error(f"shutdown_now error: {e}")
            return "Couldn't schedule a shutdown."

    def restart_now(self, delay_s: int) -> str:
        try:
            subprocess.run(["shutdown", "/r", "/t", str(max(0, int(delay_s)))],
                           capture_output=True, text=True, timeout=10)
            return f"Restarting in {max(0, int(delay_s))} seconds. Say \"cancel shutdown\" to stop it."
        except Exception as e:
            logger.error(f"restart_now error: {e}")
            return "Couldn't schedule a restart."

    def cancel_native_shutdown(self) -> str:
        try:
            result = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return NATIVE_SHUTDOWN_CANCELLED_MSG
            return "There wasn't a shutdown or restart scheduled to cancel."
        except Exception as e:
            logger.error(f"cancel_native_shutdown error: {e}")
            return "Couldn't check for a scheduled shutdown."

    def sleep_now(self) -> str:
        try:
            import ctypes
            # SetSuspendState(bHibernate, bForce, bWakeupEventsDisabled) -
            # the same call Windows' own Start menu Sleep entry makes.
            ctypes.windll.powrprof.SetSuspendState(False, True, False)
            return "Putting the PC to sleep now."
        except Exception as e:
            logger.error(f"sleep_now error: {e}")
            return "Couldn't put the PC to sleep."

    def hibernate_now(self) -> str:
        try:
            import ctypes
            ctypes.windll.powrprof.SetSuspendState(True, True, False)
            return "Hibernating the PC now."
        except Exception as e:
            logger.error(f"hibernate_now error: {e}")
            return "Couldn't hibernate the PC - hibernation may be turned off (powercfg /hibernate on)."

    def log_off_now(self) -> str:
        try:
            subprocess.run(["shutdown", "/l"], capture_output=True, text=True, timeout=10)
            return "Logging off now."
        except Exception as e:
            logger.error(f"log_off_now error: {e}")
            return "Couldn't log off."

    # ---- volume (Tier 14) ----
    # Real Windows Core Audio API via pycaw - same mechanism
    # brain/handlers/media.py's older adjust_system_volume/
    # toggle_system_mute used directly; now centralized here so every
    # volume change (old command or new) goes through AppController's one
    # audited, undoable path. No pyautogui media-key fallback here (unlike
    # media.py's two older helpers) - an exact %, or a reliable mute
    # state, can't be approximated by pressing a relative volume key a
    # guessed number of times.
    def _volume_endpoint(self):
        if not HAS_PYCAW:
            raise NotSupported("Volume control needs the 'pycaw' package, which wasn't found.")
        from pycaw.pycaw import AudioUtilities
        return AudioUtilities.GetSpeakers().EndpointVolume

    def get_volume(self) -> int:
        try:
            return round(self._volume_endpoint().GetMasterVolumeLevelScalar() * 100)
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Reading the system volume didn't work: {e}") from e

    def set_volume(self, percent: int) -> str:
        pct = max(0, min(100, int(percent)))
        try:
            self._volume_endpoint().SetMasterVolumeLevelScalar(pct / 100.0, None)
            return f"Volume set to {pct}%."
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Setting the volume didn't work: {e}") from e

    def is_muted(self) -> bool:
        try:
            return bool(self._volume_endpoint().GetMute())
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Reading the mute state didn't work: {e}") from e

    def mute_volume(self) -> str:
        try:
            self._volume_endpoint().SetMute(1, None)
            return "Muted."
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Muting didn't work: {e}") from e

    def unmute_volume(self) -> str:
        try:
            self._volume_endpoint().SetMute(0, None)
            return "Unmuted."
        except NotSupported:
            raise
        except Exception as e:
            raise NotSupported(f"Unmuting didn't work: {e}") from e

    # ---- brightness (Tier 14) ----
    # screen_brightness_control (already a dependency - the Settings page
    # slider has called it directly since before this backend layer
    # existed; see ui/main_window.py's HAS_SBC/sbc). display=0 matches
    # that slider's own choice of "the primary/first display" rather than
    # trying to juggle several monitors through one voice command.
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

    def list_services(self) -> list:
        import psutil
        return sorted(s.name() for s in psutil.win_service_iter())

    # ---- window lookup, shared by WhatsApp focus and per-window management ----
    def _find_window(self, name_substring: str):
        """hwnd of the first VISIBLE window whose title contains
        name_substring (case-insensitive), or None if pywin32 isn't
        available or nothing matches. "First" mirrors close_app's own
        first-match simplicity rather than trying to disambiguate several
        windows with similar titles."""
        if not HAS_WIN32GUI:
            return None
        found = []
        needle = (name_substring or "").lower()

        def _enum_handler(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and needle in win32gui.GetWindowText(hwnd).lower():
                found.append(hwnd)

        win32gui.EnumWindows(_enum_handler, None)
        return found[0] if found else None

    # ---- WhatsApp Desktop ----
    def _focus_whatsapp_window(self) -> bool:
        """Finds an already-running WhatsApp Desktop window and brings it to
        the foreground. Returns False (not an error) if pywin32 isn't
        available or no window was found yet - the caller falls back to
        just launching it in that case."""
        try:
            hwnd = self._find_window("whatsapp")
            if hwnd is None:
                return False
            if win32gui.IsIconic(hwnd):  # minimized
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            return True
        except Exception as e:
            logger.warning(f"Could not focus WhatsApp window: {e}")
            return False

    # ---- per-window management (Tier 8) ----
    # Built on the same _find_window() lookup WhatsApp focus already uses.
    # snap_window sends the OS's own Win+Arrow shortcuts rather than
    # computing pixel geometry by hand - Windows already handles per-
    # monitor resolution/DPI correctly for its own native snap, which hand-
    # rolled SetWindowPos math would have to reimplement and could get
    # wrong on a multi-monitor or scaled-DPI setup.
    def list_open_windows(self) -> list:
        if not HAS_WIN32GUI:
            raise NotSupported("Listing windows needs pywin32 (win32gui), which wasn't found.")
        titles = []

        def _enum_handler(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if title:
                    titles.append(title)

        win32gui.EnumWindows(_enum_handler, None)
        return titles

    def focus_window(self, window_name: str) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Focusing a specific window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return f"Switched to the window matching '{window_name}'."

    def minimize_window(self, window_name: str) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Minimizing a specific window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return f"Minimized the window matching '{window_name}'."

    def snap_window(self, window_name: str, position: str) -> str:
        position = (position or "").lower().strip()
        snap_keys = {"left": "left", "right": "right", "maximize": "up"}
        if position not in snap_keys:
            return f"'{position}' isn't a position I recognize - try left, right, or maximize."
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Snapping a window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)  # let the foreground switch land before the Win+Arrow combo
        pyautogui.hotkey('win', snap_keys[position])
        return f"Snapped the window matching '{window_name}' to {position}."

    # ---- the rest of per-window management (Tier 14) ----
    # Same _find_window() lookup as focus_window/minimize_window/snap_window
    # above, and the same "pywin32 missing -> NotSupported, pywin32 present
    # but nothing matched -> a plain reply" split every one of them uses.
    def get_window_rect(self, window_name: str):
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Reading a window's position needs pywin32 (win32gui), which wasn't found.")
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        return (left, top, right - left, bottom - top)

    def move_window(self, window_name: str, x: int, y: int) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Moving a window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        _, _, w, h = self.get_window_rect(window_name)
        win32gui.MoveWindow(hwnd, int(x), int(y), w, h, True)
        return f"Moved the window matching '{window_name}'."

    def resize_window(self, window_name: str, width: int, height: int) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Resizing a window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        x, y, _, _ = self.get_window_rect(window_name)
        win32gui.MoveWindow(hwnd, x, y, max(1, int(width)), max(1, int(height)), True)
        return f"Resized the window matching '{window_name}'."

    def maximize_window(self, window_name: str) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Maximizing a window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        return f"Maximized the window matching '{window_name}'."

    def restore_window(self, window_name: str) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Restoring a window needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return f"Restored the window matching '{window_name}'."

    def is_always_on_top(self, window_name: str) -> bool:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Reading a window's always-on-top state needs pywin32 (win32gui), which wasn't found.")
            return False
        return bool(win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOPMOST)

    def set_always_on_top(self, window_name: str, enabled: bool) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Setting always-on-top needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        target = win32con.HWND_TOPMOST if enabled else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(hwnd, target, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        return (f"The window matching '{window_name}' will now stay on top." if enabled else
                f"The window matching '{window_name}' will no longer stay on top.")

    def set_fullscreen(self, window_name: str) -> str:
        hwnd = self._find_window(window_name)
        if hwnd is None:
            if not HAS_WIN32GUI:
                raise NotSupported("Toggling fullscreen needs pywin32 (win32gui), which wasn't found.")
            return f"I couldn't find an open window matching '{window_name}'."
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
        pyautogui.press('f11')
        return f"Toggled fullscreen for the window matching '{window_name}'."

    def show_desktop(self) -> str:
        pyautogui.hotkey('win', 'd')
        return "Showing the desktop."

    def switch_window(self) -> str:
        pyautogui.hotkey('alt', 'tab')
        return "Switched to the next window."

    # Tier 15: best-effort only - see base.py's own comment on why this
    # never raises and an empty string just means "couldn't tell."
    def get_active_window_title(self) -> str:
        if not HAS_WIN32GUI:
            return ""
        try:
            return win32gui.GetWindowText(win32gui.GetForegroundWindow()) or ""
        except Exception:
            return ""

    def send_whatsapp_message(self, contact_name: str, message: str) -> str:
        # Focuses the WhatsApp window explicitly, and verifies (via OCR on a screenshot) that the
        # right chat actually opened before typing or sending anything - never a blind "Sent".
        import webbrowser
        try:
            focused = self._focus_whatsapp_window()
            if not focused:
                os.system("start whatsapp:")
                # Poll instead of one blind sleep, so this adapts to how fast or slow WhatsApp starts.
                for _ in range(15):  # up to ~15s
                    time.sleep(1)
                    if self._focus_whatsapp_window():
                        focused = True
                        break

            if not focused:
                # If the desktop app isn't installed, fall back to WhatsApp Web instead of dead-ending.
                # It does NOT try to blindly automate the web UI (no OCR/pyautogui there yet).
                if _is_whatsapp_desktop_installed():
                    return ("WhatsApp Desktop looks installed but didn't come to the "
                            "foreground in time, so I didn't risk typing into the wrong "
                            "window. Try again, or open it yourself first.")
                try:
                    webbrowser.open("https://web.whatsapp.com")
                    return ("WhatsApp Desktop isn't installed on this PC, so I opened "
                            f"WhatsApp Web instead - you'll need to send '{message}' to "
                            f"{contact_name} yourself from there once it's scanned/loaded.")
                except Exception as e:
                    logger.error(f"WhatsApp Web fallback failed: {e}")
                    return "WhatsApp Desktop isn't installed, and I couldn't open WhatsApp Web either."

            time.sleep(0.6)  # let the focus/restore animation settle
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(1.2)
            pyautogui.write(contact_name, interval=0.05)
            time.sleep(1.5)
            # Down-then-Enter to select the first filtered result is more reliable than a blind Enter.
            pyautogui.press('down')
            time.sleep(0.3)
            pyautogui.press('enter')
            time.sleep(1)

            # The honesty check: look at what's on screen before claiming the chat opened.
            screenshot_path = VisionCapture.take_screenshot()
            chat_confirmed = None  # None = couldn't check either way
            if screenshot_path:
                screen_text = ocr.extract_text(screenshot_path) or ""
                if screen_text and not screen_text.lower().startswith("error"):
                    chat_confirmed = contact_name.lower() in screen_text.lower()

            if chat_confirmed is False:
                return (f"I searched for '{contact_name}' but couldn't confirm their chat "
                        f"actually opened, so I didn't send anything - didn't want to risk "
                        f"messaging the wrong person. Try again, or open the chat yourself first.")

            pyautogui.write(message, interval=0.05)
            time.sleep(0.5)
            pyautogui.press('enter')

            if chat_confirmed is True:
                return f"Sent '{message}' to {contact_name} on WhatsApp."
            return (f"Typed and sent '{message}' towards {contact_name}'s chat on WhatsApp, "
                    f"but I couldn't visually confirm it landed in the right place - worth a quick check.")
        except Exception as e:
            logger.error(f"send_whatsapp_message error: {e}")
            return "Failed to send WhatsApp message."

    # ---- files & folders ----
    def open_path(self, path: str) -> None:
        os.startfile(path)

    def reveal_path(self, path: str) -> None:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def open_terminal_at(self, path: str) -> str:
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-d", path])
        else:
            subprocess.Popen(["cmd", "/k"], cwd=path, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        return f"Opened a terminal in {path}."

    def open_in_editor(self, path: str) -> str:
        code = shutil.which("code")
        if not code:
            return "VS Code's 'code' command wasn't found on PATH - enable 'Add to PATH' in the VS Code installer."
        subprocess.Popen([code, path])
        return f"Opened {os.path.basename(path) or path} in VS Code."

    def open_image_viewer(self, path: str) -> None:
        os.system(f'explorer "{path}"')

    # Tier 15: the four folder shortcuts that aren't a plain filesystem
    # path. shell:::{CLSID} opens a virtual special folder the exact same
    # way File Explorer's own sidebar/address bar does; taskmgr.exe and
    # control.exe are their own standalone executables, nothing to do
    # with explorer.exe at all.
    _SPECIAL_LOCATIONS = {
        "this_pc": ["explorer", "shell:::{20D04FE0-3AEA-1069-A2D8-08002B30309D}"],
        "recycle_bin": ["explorer", "shell:::{645FF040-5081-101B-9F08-00AA002F954E}"],
        "task_manager": ["taskmgr"],
        "control_panel": ["control"],
    }

    def open_special_location(self, name: str) -> str:
        argv = self._SPECIAL_LOCATIONS.get(name)
        if argv is None:
            return f"'{name}' isn't one of the locations I know how to open."
        try:
            subprocess.Popen(argv)
        except Exception as e:
            logger.error(f"open_special_location({name!r}) failed: {e}")
            return f"Couldn't open that: {e}"
        return f"Opened {name.replace('_', ' ')}."

    # Tier 15: T3/confirm-gated at the AppController level already, so
    # this skips SHEmptyRecycleBinW's own native confirmation dialog
    # (SHERB_NOCONFIRMATION) - a second "are you sure?" after the user
    # already said yes in chat would just be a confusing extra step.
    def empty_recycle_bin(self) -> str:
        try:
            import ctypes
            SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x00000001, 0x00000002, 0x00000004
            result = ctypes.windll.shell32.SHEmptyRecycleBinW(
                None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)
            if result != 0:
                return f"Couldn't empty the Recycle Bin (error code {result})."
            return "Emptied the Recycle Bin."
        except Exception as e:
            logger.error(f"empty_recycle_bin error: {e}")
            return "Couldn't empty the Recycle Bin."

    def desktop_dir(self) -> str:
        home = os.path.expanduser("~")
        plain, onedrive = os.path.join(home, "Desktop"), os.path.join(home, "OneDrive", "Desktop")
        # Windows can redirect the Desktop into OneDrive; keep ~/Desktop (the previous behaviour)
        # whenever it exists, and only fall back to the OneDrive one when that's where the real one is.
        if not os.path.isdir(plain) and os.path.isdir(onedrive):
            return onedrive
        return plain
