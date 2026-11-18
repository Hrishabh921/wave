"""
brain/skill_loader.py — Phase 4: drop-in skills with hot reload

Put a .py file in user_skills/ (or whatever WAVE_USER_SKILLS_DIR points at)
that decorates a function with @skill(...) and Wave can use it as a tool
within a couple of seconds - no restart. Edit the file and the new version
replaces the old one; delete it and the tool disappears.

    # user_skills/dice.py
    import random
    from brain.skills_registry import skill

    @skill("roll_dice", "Rolls a dice and reports the result.",
           {"sides": {"type": "integer", "description": "Number of sides, default 6."}}, [])
    def roll_dice(brain, args):
        return f"You rolled a {random.randint(1, int(args.get('sides') or 6))}."

How it works
------------
* No extra dependency: the folder is polled (default every 1.5 s) by one
  small daemon thread comparing (mtime, size) - a few `os.stat` calls, which
  is nothing next to a 60 fps UI. `watchdog` would add a package (and a
  PyInstaller hook) for no real gain at this scale.
* A file is only loaded once it has stopped changing, so a save that is
  still being written is never executed half-finished.
* Files are compiled straight from their bytes, bypassing Python's .pyc
  cache. (A same-size edit made within the same second would otherwise be
  able to load the *previous* bytecode.)
* Loading is transactional (see SkillRegistry.staging): if the new version
  has a syntax error, raises, or declares an invalid skill, the previous
  working version stays active and an "error" event explains why.
* A file can't shadow a built-in skill or another file's skill.

Trust model: this executes Python from one folder on your own machine, with
the same rights as Wave itself. Only put files there that you wrote or
reviewed. Don't start threads or open resources at import time - a reload
re-runs the module, and the old copy's resources aren't cleaned up for you.
"""
import json
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from brain.skills_registry import skills as _default_registry, SkillValidationError

logger = logging.getLogger("SkillLoader")

# A file whose mtime is this recent may still be mid-write; wait for it.
_SETTLE_SECONDS = 0.35


@dataclass
class SkillEvent:
    """One thing that happened to a user skill file."""
    kind: str                      # "loaded" | "reloaded" | "unloaded" | "disabled" | "enabled" | "error"
    file: str
    skills: List[str] = field(default_factory=list)
    detail: str = ""

    def message(self) -> str:
        names = ", ".join(self.skills)
        if self.kind == "loaded":
            return f"Skill loaded: {names} ({self.file})"
        if self.kind == "reloaded":
            return f"Skill reloaded: {names} ({self.file})"
        if self.kind == "unloaded":
            return f"Skill removed: {names or self.file}"
        if self.kind == "disabled":
            return f"Skill disabled: {names or self.file}"
        if self.kind == "enabled":
            return f"Skill enabled: {names or self.file} ({self.file})"
        return f"Skill file {self.file} not loaded - {self.detail}"


class UserSkillLoader:
    def __init__(self, directory: str, registry=None, poll_interval: float = 1.5, debounce: bool = True):
        self.directory = os.path.abspath(directory)
        self.registry = registry if registry is not None else _default_registry
        self.poll_interval = max(0.1, float(poll_interval))
        self.debounce = debounce
        self._listeners: List[Callable[[SkillEvent], None]] = []
        self._loaded: Dict[str, dict] = {}   # filename -> {"sig", "skills", "module"}
        self._failed: Dict[str, dict] = {}   # filename -> {"sig", "detail"}
        self._pending: Dict[str, tuple] = {}  # filename -> signature seen last poll
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Tier 21 roadmap line for ui/skills_panel.py: "user skills,
        # enable/disable". A disabled file is left exactly where it is on
        # disk (never renamed or moved - that would be a surprising thing
        # for this tool to do to someone's own file) and is simply treated
        # as absent by scan_once() below, going through the exact same
        # _load/_unload machinery a real add/delete would. The choice is
        # remembered in a small sidecar file next to the skills themselves,
        # so it survives an app restart without needing a DB table for
        # something this page-scoped.
        self._disabled_path = os.path.join(self.directory, ".disabled_skills.json")
        self._disabled: set = self._read_disabled()

    def _read_disabled(self) -> set:
        try:
            with open(self._disabled_path, "r", encoding="utf-8") as fh:
                names = json.load(fh)
            return {n for n in names if isinstance(n, str)}
        except (OSError, ValueError):
            return set()

    def _write_disabled(self) -> None:
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(self._disabled_path, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._disabled), fh)
        except OSError:
            logger.debug("Couldn't persist disabled-skills list (non-fatal).", exc_info=True)

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------
    def add_listener(self, callback: Callable[[SkillEvent], None]):
        """callback(SkillEvent) is called from the watcher thread - hand off
        to the GUI thread (e.g. via a Qt signal) before touching widgets."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self, event: SkillEvent):
        logger.log(logging.WARNING if event.kind == "error" else logging.INFO, event.message())
        for cb in list(self._listeners):
            try:
                cb(event)
            except Exception:
                logger.exception("Skill event listener failed")

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def _snapshot(self) -> Dict[str, tuple]:
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return {}
        except OSError as e:
            logger.warning(f"Can't read skills folder {self.directory}: {e}")
            return {}
        snap = {}
        for name in sorted(names):
            if not name.endswith(".py") or name.startswith(("_", ".")):
                continue
            try:
                st = os.stat(os.path.join(self.directory, name))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                snap[name] = (st.st_mtime_ns, st.st_size)
        return snap

    @staticmethod
    def _looks_settled(sig: tuple) -> bool:
        age = (time.time_ns() - sig[0]) / 1e9
        return age >= _SETTLE_SECONDS

    def is_disabled(self, filename: str) -> bool:
        with self._lock:
            return filename in self._disabled

    def disabled_skills(self) -> set:
        """A snapshot copy - callers (the Skills page) must not mutate the
        loader's own set directly."""
        with self._lock:
            return set(self._disabled)

    def set_disabled(self, filename: str, disabled: bool) -> Optional[SkillEvent]:
        """The Skills page's enable/disable toggle. Flips the sidecar flag,
        persists it, and immediately folds the file in or out via the same
        _load/_unload path scan_once() uses for a real add/delete - so a
        disabled file's tools disappear from the registry right away rather
        than waiting for the next poll, and no separate "is this one
        actually live" bookkeeping has to be kept in sync by hand."""
        with self._lock:
            disabled = bool(disabled)
            if disabled == (filename in self._disabled):
                return None  # already in the requested state
            if disabled:
                self._disabled.add(filename)
            else:
                self._disabled.discard(filename)
            self._write_disabled()

            if disabled:
                self._failed.pop(filename, None)  # an error on a now-disabled file is moot
                if filename not in self._loaded:
                    return None  # wasn't loaded (errored, or not on disk) - nothing to tear down
                gone = self.registry.unregister_source(self._source_id(filename))
                info = self._loaded.pop(filename, None)
                if info:
                    sys.modules.pop(info["module"], None)
                ev = SkillEvent("disabled", filename, gone)
            else:
                sig = self._snapshot().get(filename)
                if sig is None:
                    return None  # file isn't on disk (any more) - nothing to load
                self._pending.pop(filename, None)
                loaded_ev = self._load(filename, sig)
                ev = SkillEvent("enabled", filename, loaded_ev.skills) if loaded_ev and loaded_ev.kind != "error" \
                    else loaded_ev
        if ev is not None:
            self._emit(ev)
        return ev

    def scan_once(self, settle: Optional[bool] = None) -> List[SkillEvent]:
        """Diff the folder against what's loaded and load/reload/unload as
        needed. Returns (and emits) the resulting events. `settle=False`
        skips the "wait until the file stops changing" check (tests, and the
        one synchronous scan at startup).

        A disabled file (see set_disabled above) is treated as if it weren't
        in the folder at all: unloaded if it was loaded, and never
        (re)loaded while the flag is set, even if its contents change."""
        settle = self.debounce if settle is None else settle
        events: List[SkillEvent] = []
        with self._lock:
            snap = self._snapshot()

            for name in [n for n in self._loaded if n not in snap or n in self._disabled]:
                events.append(self._unload(name))
            for name in [n for n in self._failed if n not in snap or n in self._disabled]:
                del self._failed[name]
            self._pending = {n: s for n, s in self._pending.items() if n in snap and n not in self._disabled}

            for name, sig in snap.items():
                if name in self._disabled:
                    continue
                loaded, failed = self._loaded.get(name), self._failed.get(name)
                if (loaded and loaded["sig"] == sig) or (failed and failed["sig"] == sig):
                    self._pending.pop(name, None)
                    continue  # nothing new since we last dealt with this file
                if settle and not self._looks_settled(sig) and self._pending.get(name) != sig:
                    self._pending[name] = sig  # changed very recently - look again next poll
                    continue
                self._pending.pop(name, None)
                ev = self._load(name, sig)
                if ev is not None:
                    events.append(ev)
        for ev in events:
            self._emit(ev)
        return events

    # ------------------------------------------------------------------
    # Load / unload
    # ------------------------------------------------------------------
    @staticmethod
    def _source_id(filename: str) -> str:
        return f"user:{filename}"

    @staticmethod
    def _module_name(filename: str) -> str:
        return "wave_user_skill_" + re.sub(r"\W", "_", os.path.splitext(filename)[0])

    def _load(self, filename: str, sig: tuple) -> Optional[SkillEvent]:
        path = os.path.join(self.directory, filename)
        source_id = self._source_id(filename)
        module_name = self._module_name(filename)
        was_loaded = filename in self._loaded
        previous_module = sys.modules.get(module_name)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            # Locked by an editor / antivirus mid-save, or deleted a moment ago.
            # NOT remembered as a failure: the signature stays unseen, so the
            # next poll simply tries again.
            logger.debug(f"Skill file {filename} not readable yet ({e}); will retry.")
            return None
        try:
            code = compile(raw, path, "exec")  # straight from bytes: no stale .pyc
            module = types.ModuleType(module_name)
            module.__file__ = path
            # `@skill` is available without any import, bound to THIS loader's
            # registry (an explicit `from brain.skills_registry import skill`
            # still works and resolves to the same shared registry).
            module.__dict__["skill"] = self.registry.register
            sys.modules[module_name] = module
            with self.registry.staging(source_id) as stage:
                exec(code, module.__dict__)
                if not stage.entries:
                    raise SkillValidationError(
                        "it doesn't define any skill - decorate a function with @skill(...)"
                    )
                new_names = stage.names
        except (Exception, SystemExit) as e:
            if previous_module is not None:
                sys.modules[module_name] = previous_module
            else:
                sys.modules.pop(module_name, None)
            if isinstance(e, SyntaxError):
                detail = f"SyntaxError on line {e.lineno}: {e.msg}"
            else:
                detail = f"{type(e).__name__}: {e}"
            if was_loaded:
                detail += " (keeping the previous working version)"
            self._failed[filename] = {"sig": sig, "detail": detail}
            return SkillEvent("error", filename, list(self._loaded.get(filename, {}).get("skills", [])), detail)

        self._loaded[filename] = {"sig": sig, "skills": new_names, "module": module_name}
        self._failed.pop(filename, None)
        return SkillEvent("reloaded" if was_loaded else "loaded", filename, new_names)

    def _unload(self, filename: str) -> SkillEvent:
        info = self._loaded.pop(filename, None)
        gone = self.registry.unregister_source(self._source_id(filename))
        if info:
            sys.modules.pop(info["module"], None)
        return SkillEvent("unloaded", filename, gone)

    # ------------------------------------------------------------------
    # Background watching
    # ------------------------------------------------------------------
    def _seed_example_skill(self) -> None:
        """Packaging (PyInstaller): user_skills/_example_skill.py from the source tree
        isn't sitting next to the .exe in a frozen build - DATA_DIR starts out empty,
        so "copy _example_skill.py to dice.py" (docs/PHASE4.md) has nothing to copy.
        Seed the bundled template into the live folder once. Running from source needs
        no help: the file is already right there, so this is a no-op (config.FROZEN is
        False). Never raises - worst case the template is just missing."""
        try:
            from settings.config import config
            if not config.FROZEN:
                return
            dst = os.path.join(self.directory, "_example_skill.py")
            if os.path.exists(dst):
                return
            src = os.path.join(config.APP_DIR, "user_skills", "_example_skill.py")
            if os.path.isfile(src) and os.path.abspath(src) != os.path.abspath(dst):
                shutil.copyfile(src, dst)
        except Exception:
            logger.debug("Couldn't seed _example_skill.py template (non-fatal).", exc_info=True)

    def load_now(self) -> List[SkillEvent]:
        """One immediate, synchronous scan (creates the folder if needed)."""
        os.makedirs(self.directory, exist_ok=True)
        self._seed_example_skill()
        return self.scan_once(settle=False)

    def start(self, watch: bool = True):
        """Load everything currently in the folder, then (if `watch`) keep
        watching it from a daemon thread. Safe to call twice."""
        self.load_now()
        if not watch:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="WaveSkillWatcher", daemon=True)
            self._thread.start()

    def _run(self):
        while not self._stop.wait(self.poll_interval):
            try:
                self.scan_once()
            except Exception:
                logger.exception("Skill watcher scan failed")

    def stop(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    @property
    def watching(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_alive(self) -> bool:
        """Tier 19: same liveness check as `watching` above, just under the
        name automation/thread_supervisor.py's duck-typed contract expects
        (start()/stop()/is_alive(), shared with MemoryConsolidator/
        ProactiveEngine) - so this loader can be watched and restarted the
        same way if its thread ever dies unexpectedly. `watching` stays as
        the existing name status() itself already uses."""
        return self.watching

    def status(self) -> dict:
        with self._lock:
            return {
                "directory": self.directory,
                "watching": self.watching,
                "loaded": {f: list(i["skills"]) for f, i in self._loaded.items()},
                "errors": {f: i["detail"] for f, i in self._failed.items()},
                "disabled": sorted(self._disabled),
            }


_default_loader: Optional[UserSkillLoader] = None


def get_loader() -> UserSkillLoader:
    """The app-wide loader for config.USER_SKILLS_DIR (created on first use, not started)."""
    global _default_loader
    if _default_loader is None:
        from settings.config import config
        _default_loader = UserSkillLoader(config.USER_SKILLS_DIR, poll_interval=config.SKILLS_POLL_INTERVAL)
    return _default_loader
