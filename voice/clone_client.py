"""
voice/clone_client.py — Tier 31: Wave's side of the voice-clone process.

XTTS-v2 runs in voice/clone_worker.py's own process, never in Wave's (that
module explains why: Python's GIL made it up to 100x slower in here, and it
starved Wave's audio threads in turn). CloneVoiceProcess starts that process,
waits for it to load the model and the user's voice, then sends it one
sentence at a time and gets float32 samples back. The process ends when Wave
does: closing its stdin is its signal to exit.
"""
import itertools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

import numpy as np

from voice import clone_worker  # only for its module name (it runs in its own process)

logger = logging.getLogger("TTS.clone")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The very first start downloads the ~1.8GB model (measured: 7 minutes).
READY_TIMEOUT_S = 900.0
_EXITED = object()


class CloneProcessError(RuntimeError):
    pass


def _python_executable() -> str:
    """The interpreter to start the worker with: Wave's own, except that a
    windowed pythonw.exe is swapped for its console twin so the pipes work
    (the child is started without a window either way)."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        console = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(console):
            return console
    return exe


def _read_exact(stream, size: int):
    chunks, left = [], size
    while left > 0:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


class CloneVoiceProcess:
    """One running voice-clone process for one voice sample."""

    def __init__(self, model_name: str, reference_path: str, device: str = "auto", command=None):
        self.model_name = model_name
        self.reference_path = reference_path
        self.device = device or "auto"
        self.info = {}
        self._command = command
        self._proc = None
        self._replies = queue.Queue()
        self._ids = itertools.count(1)

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self, timeout: float = READY_TIMEOUT_S) -> dict:
        """Starts the process and waits until it has loaded the model and the
        voice. Returns its ready message; raises CloneProcessError."""
        if getattr(sys, "frozen", False):
            raise CloneProcessError("voice cloning needs Wave's Python environment, not the packaged app")
        command = self._command or [_python_executable(), "-m", clone_worker.__name__, "--model", self.model_name,
                                    "--device", self.device, "--reference", self.reference_path]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, cwd=REPO_ROOT, creationflags=flags)
        except OSError as e:
            raise CloneProcessError(f"couldn't start the voice-clone process: {e}") from e
        threading.Thread(target=self._read_replies, args=(self._proc.stdout,), daemon=True,
                         name="TTS-CloneReplies").start()
        threading.Thread(target=self._read_log, args=(self._proc.stderr,), daemon=True,
                         name="TTS-CloneLog").start()
        reply = self._next(timeout)
        if reply is None or not reply[0].get("ready"):
            error = reply[0].get("error") if reply is not None else None
            self.close()
            raise CloneProcessError(error or "the voice-clone process didn't start")
        self.info = reply[0]
        return self.info

    def synthesize(self, text: str, lang: str, timeout=None):
        """`text` in the user's voice -> (float32 samples, sample rate).
        Raises CloneProcessError - and stops the process if it stopped
        answering (the default timeout is generous: 60s + 0.5s per
        character, several times what a laptop CPU needs)."""
        if not self.alive:
            raise CloneProcessError("the voice-clone process isn't running")
        rid = next(self._ids)
        self._send({"cmd": "say", "id": rid, "text": text, "lang": lang})
        deadline = time.monotonic() + (timeout if timeout is not None else 60.0 + 0.5 * len(text))
        while True:
            reply = self._next(max(0.0, deadline - time.monotonic()))
            if reply is None:
                exited = not self.alive
                self.close()
                raise CloneProcessError("the voice-clone process exited" if exited
                                        else "the voice-clone process stopped answering")
            header, samples = reply
            if header.get("id") != rid:
                continue   # an answer to an earlier request that was given up on
            if not header.get("ok"):
                raise CloneProcessError(header.get("error") or "synthesis failed")
            if samples is None:
                samples = np.zeros(0, dtype=np.float32)
            return samples, int(header.get("rate") or 24000)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()

    # -- internals -----------------------------------------------------
    def _send(self, message: dict) -> None:
        proc = self._proc
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("ascii"))
            proc.stdin.flush()
        except (AttributeError, OSError, ValueError) as e:
            raise CloneProcessError(f"couldn't reach the voice-clone process: {e}") from e

    def _next(self, timeout: float):
        try:
            item = self._replies.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _EXITED:
            self._replies.put(_EXITED)   # stays "exited" for every later call
            return None
        return item

    def _read_replies(self, stream) -> None:
        try:
            for line in iter(stream.readline, b""):
                try:
                    header = json.loads(line)
                except ValueError:
                    logger.debug(f"voice-clone process: {line[:200]!r}")
                    continue
                samples = None
                count = int(header.get("n") or 0) if isinstance(header, dict) else 0
                if count:
                    data = _read_exact(stream, 4 * count)
                    if data is None:
                        break
                    samples = np.frombuffer(data, dtype="<f4")
                if isinstance(header, dict):
                    self._replies.put((header, samples))
        except Exception:
            logger.debug("voice-clone reply reader stopped", exc_info=True)
        finally:
            self._replies.put(_EXITED)

    @staticmethod
    def _read_log(stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                if line.startswith(("WARNING", "ERROR", "CRITICAL", "Traceback")):
                    logger.warning(f"voice-clone process: {line[:300]}")
                else:
                    logger.debug(f"voice-clone process: {line[:300]}")
        except Exception:
            pass
