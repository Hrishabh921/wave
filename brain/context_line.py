"""
brain/context_line.py — Tier 47: one line about what's on the PC right now.

"Kya chal raha hai?", "is my laptop charging?", "close this" - the model
had no idea what the user was looking at, so it asked or called a tool.
SARA adds a short note to each turn ("active window ...; battery 61%
(charging)"). This is Wave's: the app in front (its name - "Google Chrome",
"VS Code" - never Wave itself), the battery and whether it's charging,
read at most every 5 seconds. The window's TITLE can hold a document name,
a chat or an email subject, and the prompt goes to the cloud model, so it's
left out unless CONTEXT_WINDOW_TITLES=true - and even then never for a
title that looks like banking, passwords, a login or a private window.
CONTEXT_LINE=false turns the line off.
"""
import os
import re
import threading
import time

from settings.config import config

TTL_S = 5.0
_DENY = re.compile(
    r"bank|banking|paypal|upi|paytm|phonepe|gpay|google pay|password|passwd|credential|keepass|bitwarden|"
    r"1password|lastpass|authenticator|\botp\b|wallet|credit card|debit card|netbanking|incognito|inprivate|"
    r"private brows|vpn|sign ?in|log ?in|login|aadhaar|pan card",
    re.I)
_FRIENDLY = {
    "chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox", "brave": "Brave", "opera": "Opera",
    "code": "VS Code", "explorer": "File Explorer", "winword": "Word", "excel": "Excel", "powerpnt": "PowerPoint",
    "outlook": "Outlook", "olk": "Outlook", "onenote": "OneNote", "spotify": "Spotify", "whatsapp": "WhatsApp",
    "notepad": "Notepad", "teams": "Teams", "ms-teams": "Teams", "discord": "Discord", "slack": "Slack",
    "vlc": "VLC", "acrord32": "Adobe Acrobat", "acrobat": "Adobe Acrobat", "zoom": "Zoom", "telegram": "Telegram",
    "windowsterminal": "Windows Terminal", "cmd": "Command Prompt", "powershell": "PowerShell",
    "taskmgr": "Task Manager", "systemsettings": "Settings", "mspaint": "Paint", "photos": "Photos",
    "calculatorapp": "Calculator", "obs64": "OBS", "steam": "Steam", "notion": "Notion", "obsidian": "Obsidian",
    "pycharm64": "PyCharm", "idea64": "IntelliJ IDEA", "devenv": "Visual Studio", "figma": "Figma",
}
_lock = threading.Lock()
_cache = {"at": -1e9, "text": ""}


def _enabled() -> bool:
    return bool(getattr(config, "CONTEXT_LINE", True))


def _foreground():
    """(process name, window title) of the window in front; (None, None) off Windows/on error."""
    try:
        import psutil  # noqa: PLC0415
        import win32gui  # noqa: PLC0415
        import win32process  # noqa: PLC0415
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None, None
        _tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        if pid == os.getpid():
            return "", ""                          # Wave's own window
        return psutil.Process(pid).name(), win32gui.GetWindowText(hwnd)
    except Exception:
        return None, None


def _battery():
    try:
        import psutil  # noqa: PLC0415
        b = psutil.sensors_battery()
        return (int(round(b.percent)), bool(b.power_plugged)) if b is not None else None
    except Exception:
        return None


def app_name(process: str) -> str:
    stem = re.sub(r"\.exe$", "", (process or "").strip(), flags=re.I)
    if not stem or stem.lower() in ("python", "pythonw", "applicationframehost", "searchhost", "shellexperiencehost",
                                    "startmenuexperiencehost", "lockapp"):
        return ""
    return _FRIENDLY.get(stem.lower(), stem[:1].upper() + stem[1:])


def redact_title(title: str) -> str:
    t = " ".join((title or "").split())
    if not t or _DENY.search(t) or t in ("Program Manager", "Task Switching"):
        return ""
    return t[:80]


def build(foreground=None, battery=None, now=None) -> str:
    """The line, or "" when off / nothing is known. Never raises."""
    if not _enabled():
        return ""
    foreground, battery, now = foreground or _foreground, battery or _battery, now or time.monotonic
    with _lock:
        if now() - _cache["at"] < TTL_S:
            return _cache["text"]
    parts = []
    try:
        process, title = foreground()
        app = app_name(process) if process else ""
        if process and process.lower() == "explorer.exe" and (title or "") in ("", "Program Manager"):
            app = "the desktop"
        if app:
            shown = redact_title(title) if getattr(config, "CONTEXT_WINDOW_TITLES", False) else ""
            parts.append(f"{app} is in front" + (f' ("{shown}")' if shown and shown != app else ""))
    except Exception:
        pass
    try:
        b = battery()
        if b:
            parts.append(f"battery {b[0]}% ({'charging' if b[1] else 'on battery'})")
    except Exception:
        pass
    text = ("ON THE PC RIGHT NOW: " + "; ".join(parts) + ". Use it only when it matters to what's asked.") if parts else ""
    with _lock:
        _cache.update(at=now(), text=text)
    return text


def reset() -> None:
    with _lock:
        _cache.update(at=-1e9, text="")
