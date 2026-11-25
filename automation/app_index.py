"""
automation/app_index.py — Tier 43: open any installed app at once.

"open X" for an app Wave had no shortcut for drove the Start menu: press Win,
type the name, press Enter - over a second of fixed waits, and when the
Start menu didn't take focus the name was typed into whatever window was in
front. SARA keeps an index of the installed apps and launches from it in
milliseconds. This does the same on Windows:

  * the index is Windows' own list of Start-menu apps (`Get-StartApps`:
    desktop programs and Store apps alike, each with its AppID), built in
    the background at start-up, refreshed every REFRESH_S, and kept in
    <DATA_DIR>/app_index.json so it's there at once on the next start;
  * resolve(name): the exact name, then common nicknames ("vs code",
    "calc"), then every word of the request among the app name's words, then
    a close spelling (similarity >= FUZZY); anything vaguer returns None and
    the old Start-menu way runs, as before;
  * launch(entry): `explorer.exe shell:AppsFolder\\<AppID>`. Only AppIDs from
    Windows' own list are ever launched - the user's words never become a
    path or a command.

APP_INDEX=false turns it off.
"""
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher

from settings.config import config

logger = logging.getLogger("AppIndex")

REFRESH_S = 30 * 60
FUZZY = 0.82
_SKIP = re.compile(r"\b(uninstall|uninstaller|readme|read me|release notes|documentation|help|license|"
                   r"website|support|manual|changelog|what's new)\b", re.I)
_SAFE_APPID = re.compile(r"^[^\"\r\n\x00]{1,400}$")
# What people call an app -> words of its real name.
ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
    "calc": "calculator", "calci": "calculator", "edge": "microsoft edge", "chrome": "google chrome",
    "ppt": "powerpoint", "explorer": "file explorer", "files": "file explorer", "cmd": "command prompt",
    "store": "microsoft store", "teams": "microsoft teams", "task manager": "task manager",
    "control panel": "control panel", "snipping": "snipping tool", "sticky notes": "sticky notes",
}


def normalize(name: str) -> str:
    text = (name or "").casefold().replace("&", " and ")
    text = re.sub(r"[®™©]", "", text)
    text = re.sub(r"[^\wऀ-ॿ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_query(query: str) -> str:
    q = normalize(query)
    q = re.sub(r"^(the|my)\s+", "", q)
    q = re.sub(r"\s+(app|application|program|software|ko|kholo|open)$", "", q)
    return q.strip()


class AppIndex:
    def __init__(self, path: str = None, runner=None, clock=time.time):
        self._path = path
        self._runner = runner or subprocess.run
        self._clock = clock
        self._lock = threading.Lock()
        self._entries = None           # [{"name", "appid", "key"}]
        self._built_at = 0.0
        self._building = False

    # -- configuration ---------------------------------------------------
    @staticmethod
    def enabled() -> bool:
        return sys.platform == "win32" and bool(getattr(config, "APP_INDEX", True))

    def path(self) -> str:
        return self._path or os.path.join(config.DATA_DIR, "app_index.json")

    # -- the index -------------------------------------------------------------
    def _set(self, entries: list, built_at: float) -> None:
        clean = []
        for e in entries:
            name, appid = str(e.get("name") or "").strip(), str(e.get("appid") or "").strip()
            if not name or not appid or _SKIP.search(name) or not _SAFE_APPID.match(appid):
                continue
            clean.append({"name": name, "appid": appid, "key": normalize(name)})
        with self._lock:
            self._entries, self._built_at = clean, built_at

    def _load_cache(self) -> None:
        try:
            with open(self.path(), encoding="utf-8") as f:
                data = json.load(f)
            self._set(data.get("apps") or [], float(data.get("built_at") or 0))
        except Exception:
            with self._lock:
                if self._entries is None:
                    self._entries = []

    def build_now(self) -> int:
        """Asks Windows for its Start-menu apps (a second or two), keeps
        them, and saves them for the next start. Returns how many."""
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command",
               "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = self._runner(cmd, capture_output=True, text=True, timeout=30, creationflags=flags)
        raw = json.loads((result.stdout or "").strip() or "[]")
        if isinstance(raw, dict):
            raw = [raw]
        apps = [{"name": r.get("Name"), "appid": r.get("AppID")} for r in raw if isinstance(r, dict)]
        now = self._clock()
        self._set(apps, now)
        try:
            os.makedirs(os.path.dirname(self.path()) or ".", exist_ok=True)
            tmp = self.path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"built_at": now, "apps": [{"name": e["name"], "appid": e["appid"]}
                                                     for e in self._entries]}, f)
            os.replace(tmp, self.path())
        except Exception as e:
            logger.debug(f"couldn't save the app index: {e}")
        logger.info(f"App index: {len(self._entries)} installed apps.")
        return len(self._entries)

    def warm_up(self) -> None:
        """At start-up: the saved index at once, a fresh one in the background
        when it's older than REFRESH_S."""
        if not self.enabled():
            return
        if self._entries is None:
            self._load_cache()
        with self._lock:
            if self._building or self._clock() - self._built_at < REFRESH_S:
                return
            self._building = True

        def run():
            try:
                self.build_now()
            except Exception as e:
                logger.info(f"App index unavailable ({type(e).__name__}: {e}) - apps open the old way.")
            finally:
                with self._lock:
                    self._building = False
        threading.Thread(target=run, name="AppIndex", daemon=True).start()

    # -- lookups ---------------------------------------------------------------
    def resolve(self, query: str):
        """The index entry `query` means, or None when it isn't clear."""
        if not self.enabled():
            return None
        if self._entries is None:
            self._load_cache()
        self.warm_up()
        with self._lock:
            entries = list(self._entries or [])
        if not entries:
            return None
        q = _clean_query(query)
        if not q:
            return None
        targets = [q] + ([ALIASES[q]] if q in ALIASES else [])
        for t in targets:
            exact = [e for e in entries if e["key"] == t]
            if exact:
                return min(exact, key=lambda e: len(e["name"]))
        for t in targets:
            words = set(t.split())
            holders = [e for e in entries if words <= set(e["key"].split())]
            if holders:
                return min(holders, key=lambda e: (len(e["key"].split()), len(e["name"])))
        best, best_score = None, 0.0
        for e in entries:
            score = SequenceMatcher(None, q, e["key"]).ratio()
            if score > best_score:
                best, best_score = e, score
        return best if best_score >= FUZZY else None

    @staticmethod
    def launch(entry: dict) -> None:
        appid = str(entry.get("appid") or "")
        if not _SAFE_APPID.match(appid):
            raise ValueError("not an app id from Windows' list")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + appid], creationflags=flags)


app_index = AppIndex()
