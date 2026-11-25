"""
voice/ducking.py — Tier 39: turn other apps down while Wave listens or talks.

With YouTube or Spotify playing, the mic hears the music with the user, and
the user hears Wave over the music. SARA "ducks": it lowers OTHER apps'
volume while it's listening, then puts it back - the way smart speakers do.
Wave now does the same while it's AWAKE (after the wake word, until it goes
back to sleep) and while it's speaking: every other app's audio session
(Windows Core Audio, through pycaw - the per-app volume, nothing system-wide)
goes to AUDIO_DUCK_LEVEL of its own volume, and back once Wave is idle again
(after RELEASE_S, so it doesn't pump between sentences).

Careful with other people's things:
  * Wave's own process and its children are never touched;
  * the original volumes are written to disk BEFORE anything changes, and put
    back at the next start if Wave was closed or crashed while ducking;
  * a session whose volume the user changed in the meantime is left alone;
  * best-effort throughout: no pycaw / COM, and nothing happens at all.
AUDIO_DUCKING=false turns it off.
"""
import json
import logging
import os
import threading
import time

from settings.config import config

logger = logging.getLogger("Ducking")

POLL_S = 0.25
RELEASE_S = 1.2           # idle this long before the volumes come back
CHANGED_BY_USER = 0.02    # a volume this far from what Wave set was changed by someone else


class PycawBackend:
    """Windows' per-app audio sessions. Used only on the ducking thread
    (COM is initialised there)."""

    def __init__(self):
        from pycaw.pycaw import AudioUtilities  # noqa: PLC0415
        self._utils = AudioUtilities

    @staticmethod
    def init_thread():
        import comtypes  # noqa: PLC0415
        comtypes.CoInitialize()

    def sessions(self):
        """(key, pid, process name, volume interface) for every session with a process."""
        out = []
        for s in self._utils.GetAllSessions():
            proc = getattr(s, "Process", None)
            volume = getattr(s, "SimpleAudioVolume", None)
            if proc is None or volume is None:
                continue
            try:
                name, pid = proc.name(), proc.pid
            except Exception:
                continue
            ident = getattr(s, "InstanceIdentifier", None) or f"{name}:{pid}"
            out.append((str(ident), pid, name, volume))
        return out

    @staticmethod
    def get(volume) -> float:
        return float(volume.GetMasterVolume())

    @staticmethod
    def muted(volume) -> bool:
        return bool(volume.GetMute())

    @staticmethod
    def set(volume, level: float) -> None:
        volume.SetMasterVolume(max(0.0, min(1.0, float(level))), None)


def _own_pids() -> set:
    pids = {os.getpid()}
    try:
        import psutil  # noqa: PLC0415
        pids.update(c.pid for c in psutil.Process(os.getpid()).children(recursive=True))
    except Exception:
        pass
    return pids


class Ducker:
    def __init__(self, backend=None, state_path: str = None, clock=time.monotonic, speaking=None):
        self._backend = backend
        self._state_path = state_path
        self._clock = clock
        self._speaking = speaking
        self._lock = threading.Lock()
        self._awake = False
        self._ducked = {}          # key -> {"name", "original", "ducked"}
        self._idle_since = None
        self._thread = None
        self._stop = threading.Event()

    # -- configuration ---------------------------------------------------
    @staticmethod
    def enabled() -> bool:
        return bool(getattr(config, "AUDIO_DUCKING", True))

    @staticmethod
    def level() -> float:
        return float(getattr(config, "AUDIO_DUCK_LEVEL", 0.3))

    def state_path(self) -> str:
        return self._state_path or os.path.join(config.DATA_DIR, "audio_ducking.json")

    # -- what the UIs call -----------------------------------------------
    def set_awake(self, awake: bool) -> None:
        """The mic worker's mode: AWAKE ducks (with Wave's speech), anything else doesn't."""
        with self._lock:
            self._awake = bool(awake)
        if self.enabled():
            self._ensure_thread()

    def stop(self) -> None:
        """Puts everything back and stops the thread (Wave closing)."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)

    # -- the thread ---------------------------------------------------------
    def _ensure_thread(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="AudioDucking", daemon=True)
            self._thread.start()

    def _run(self):
        try:
            if self._backend is None:
                self._backend = PycawBackend()
            init = getattr(self._backend, "init_thread", None)
            if init is not None:
                init()
        except Exception as e:
            logger.info(f"Audio ducking unavailable ({type(e).__name__}: {e}).")
            return
        self.restore_leftovers()
        while not self._stop.wait(POLL_S):
            try:
                self.tick()
            except Exception:
                logger.debug("ducking tick failed", exc_info=True)
        try:
            self.restore()
        except Exception:
            logger.debug("restoring volumes failed", exc_info=True)

    def _wants_duck(self) -> bool:
        if not self.enabled():
            return False
        speaking = False
        try:
            if self._speaking is not None:
                speaking = bool(self._speaking())
            else:
                from voice.tts import tts_engine  # noqa: PLC0415
                speaking = bool(tts_engine.is_speaking)
        except Exception:
            pass
        return self._awake or speaking

    def tick(self) -> None:
        if self._wants_duck():
            self._idle_since = None
            self.duck()
        elif self._ducked:
            now = self._clock()
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= RELEASE_S:
                self.restore()

    # -- ducking ---------------------------------------------------------------
    def duck(self) -> None:
        """Turns down every other app's session not already turned down
        (one that starts playing mid-conversation too)."""
        b = self._backend
        mine = _own_pids()
        changed = False
        for key, pid, name, volume in b.sessions():
            if key in self._ducked or pid in mine:
                continue
            try:
                original = b.get(volume)
                if b.muted(volume) or original <= 0.01:
                    continue
                target = round(original * self.level(), 3)
                if target >= original:
                    continue
                self._ducked[key] = {"name": name, "original": original, "ducked": target}
                self._save()                          # on disk BEFORE the change
                b.set(volume, target)
                changed = True
            except Exception:
                self._ducked.pop(key, None)
        if changed:
            logger.info(f"Turned other apps down while Wave listens/talks: "
                        f"{', '.join(sorted({d['name'] for d in self._ducked.values()}))}")

    def restore(self) -> None:
        """Every turned-down session back to its own volume - unless the user
        changed it in the meantime."""
        if not self._ducked:
            return
        b = self._backend
        live = {key: volume for key, _pid, _name, volume in b.sessions()}
        for key, info in list(self._ducked.items()):
            volume = live.get(key)
            try:
                if volume is not None and abs(b.get(volume) - info["ducked"]) <= CHANGED_BY_USER:
                    b.set(volume, info["original"])
            except Exception:
                logger.debug("couldn't restore a session", exc_info=True)
            self._ducked.pop(key, None)
        self._idle_since = None
        self._save()
        logger.info("Other apps' volume is back.")

    def restore_leftovers(self) -> None:
        """At start: volumes a previous run turned down and never put back
        (closed or crashed while ducking)."""
        try:
            with open(self.state_path(), encoding="utf-8") as f:
                left = json.load(f)
        except Exception:
            return
        if not left:
            return
        b = self._backend
        by_name = {}
        for info in left.values():
            by_name.setdefault(info.get("name"), info)
        restored = 0
        for key, _pid, name, volume in b.sessions():
            info = left.get(key) or by_name.get(name)
            if not info:
                continue
            try:
                if abs(b.get(volume) - float(info["ducked"])) <= CHANGED_BY_USER:
                    b.set(volume, float(info["original"]))
                    restored += 1
            except Exception:
                pass
        self._ducked = {}
        self._save()
        if restored:
            logger.info(f"Put back {restored} app volume(s) a previous run had turned down.")

    def _save(self) -> None:
        try:
            path = self.state_path()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._ducked, f)
            os.replace(tmp, path)
        except Exception:
            logger.debug("couldn't save the ducking state", exc_info=True)


ducker = Ducker()
