"""
brain/ollama_lifecycle.py — Tier 19: Ollama lifecycle manager.

Before this file, Ollama was entirely the user's own responsibility: install
it, run it, `ollama pull` a model, and keep it running - if any of that
wasn't already true, every Ollama-backed call (the primary provider path in
brain/llm_provider.py, or the fallback path in brain/groq_core.py's
_get_ollama_client()/_think_with_ollama()) just failed quietly, over and
over, with nothing ever trying to fix it. That's still a perfectly fine
posture for someone who manages Ollama by hand - this module never changes
that unless OLLAMA_AUTOSTART says to - but for everyone else it means Wave
can make the common case ("I installed Ollama and pulled a model, now it
just works") actually true without extra setup.

What ensure_ready() does, in order, and ONLY when Ollama is actually
relevant (config.OLLAMA_ENABLED - the fallback - or LLM_PROVIDER=ollama -
the primary provider - see _is_relevant()) and config.OLLAMA_AUTOSTART
hasn't turned this off:

    1. shutil.which("ollama") - if it's not installed, stop right here.
       This is the overwhelmingly common case for anyone who hasn't set
       Ollama up at all, and it costs one cheap PATH lookup - matching
       .env.example's existing promise that "if Ollama isn't
       installed/running, Wave just skips this tier... nothing else to
       configure."
    2. GET /api/tags (Ollama's own native endpoint, one level up from the
       OpenAI-compat /v1 layer brain/llm_provider.py talks to) - reachable
       already means already running (e.g. the user started it, or the
       Ollama desktop app auto-starts it); unreachable means try
       `ollama serve` as a detached subprocess and poll until it answers
       or config.OLLAMA_STARTUP_TIMEOUT_S runs out.
    3. If config.OLLAMA_AUTO_PULL_MODEL: check whether config.OLLAMA_MODEL
       is already pulled (same /api/tags response lists it); if not,
       POST /api/pull and stream its progress until done or
       config.OLLAMA_PULL_TIMEOUT_S runs out (a first-time pull can be
       several GB - this is deliberately generous).

ensure_ready(wait=False) (the default) does all of this on its own daemon
thread, so startup is never blocked on a multi-GB download - ui/main_window.py
calls it this way. ensure_ready(wait=True) runs inline instead, for
wave_start.pyw's --healthcheck CLI, where there's no GUI responsiveness to
protect and the whole point is to report the final state before exiting.
Either way, call add_listener(fn) first if you want to hear about it -
fn(event, message) with event in {"already_running", "server_started",
"model_ready", "model_pulling", "problem"} - same cross-thread rule as
automation/thread_supervisor.py's own listeners: this fires from whatever
thread ensure_ready runs on, so a GUI listener must hop to the main thread
(a Qt signal .emit()) before touching any widget.

shutdown() is the "clean up on exit" half: it only ever stops a server
THIS module itself started (tracked in self._process) - never one the user
was already running before Wave touched it, same ownership rule
thread_supervisor.py documents for its own watched entries ("nothing here
inspects *why*... call unwatch() before intentionally stopping"). It's
best-effort and doesn't wait out an in-flight model pull first (that could
be the 30-minute OLLAMA_PULL_TIMEOUT_S, and nobody wants Wave to hang on
exit for a background download they didn't ask to wait for) - closing Wave
mid-pull just kills the server process; Ollama's own blob cache means the
next pull attempt resumes rather than starting over.
"""
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

import requests

from brain import llm_provider
from settings.config import config

logger = logging.getLogger("OllamaLifecycle")


def _is_relevant() -> bool:
    """False means: don't touch anything at all, not even shutil.which -
    Ollama plays no part in this setup right now."""
    if config.OLLAMA_ENABLED:
        return True
    try:
        return llm_provider.resolve_provider().name == "ollama"
    except Exception:
        return False


def _host_root(host: str) -> str:
    """OLLAMA_HOST is the bare host (e.g. http://localhost:11434) -
    llm_provider.py appends /v1 for the OpenAI-compat layer, but the
    endpoints this module needs (/api/tags, /api/pull) are Ollama's own
    native API, one level up from that."""
    return (host or "").rstrip("/")


class OllamaLifecycle:
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._listeners: List[Callable[[str, str], None]] = []
        self._done = threading.Event()

    def add_listener(self, fn: Callable[[str, str], None]) -> None:
        if fn not in self._listeners:
            self._listeners.append(fn)

    def _emit(self, event: str, message: str) -> None:
        (logger.warning if event == "problem" else logger.info)(message)
        for fn in list(self._listeners):
            try:
                fn(event, message)
            except Exception:
                logger.exception(f"OllamaLifecycle listener raised for {event}")

    # ---- Ollama's native HTTP API ----
    def _query_tags(self, host: str) -> Optional[List[str]]:
        """None if the server isn't reachable at all; otherwise the list of
        locally pulled model names. One round trip answers both "is it up"
        and "what's pulled"."""
        try:
            resp = requests.get(f"{_host_root(host)}/api/tags", timeout=3)
            resp.raise_for_status()
            return [m.get("name", "") for m in (resp.json() or {}).get("models", [])]
        except Exception as e:
            logger.debug(f"Ollama not reachable at {host}: {e}")
            return None

    def _wait_until_reachable(self, host: str, timeout: float) -> Optional[List[str]]:
        deadline = time.monotonic() + timeout
        while True:
            models = self._query_tags(host)
            if models is not None:
                return models
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.5)

    @staticmethod
    def _model_present(models: List[str], model: str) -> bool:
        if model in models:
            return True
        # Ollama tags a model ("llama3.1:latest") even when it was pulled
        # by its bare name - match either way, like a human would expect.
        return any(m == model or m.split(":")[0] == model for m in models)

    def _pull_model(self, host: str, model: str) -> bool:
        self._emit("model_pulling", f"Pulling Ollama model '{model}' - this can take a while the first time.")
        try:
            resp = requests.post(f"{_host_root(host)}/api/pull", json={"name": model, "stream": True},
                                  stream=True, timeout=config.OLLAMA_PULL_TIMEOUT_S)
            resp.raise_for_status()
            last_status = ""
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("error"):
                    self._emit("problem", f"Pulling '{model}' failed: {obj['error']}")
                    return False
                status = obj.get("status", "")
                if status and status != last_status:
                    logger.info(f"ollama pull {model}: {status}")
                    last_status = status
            return True
        except Exception as e:
            self._emit("problem", f"Pulling '{model}' failed: {e}")
            return False

    # ---- server process ----
    def _start_server(self) -> bool:
        binary = shutil.which("ollama")
        if not binary:
            return False
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            proc = subprocess.Popen([binary, "serve"], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, **kwargs)
            with self._lock:
                self._process = proc
            return True
        except Exception as e:
            logger.warning(f"Couldn't launch 'ollama serve': {e}")
            return False

    # ---- the public entry points ----
    def ensure_ready(self, wait: bool = False) -> None:
        """Safe to call more than once (e.g. --healthcheck re-running it
        on an already-set-up install - each call just confirms the same
        good state again). wait=True runs inline; otherwise this returns
        immediately and the work happens on its own daemon thread."""
        self._done.clear()
        if wait:
            self._ensure_ready_guarded()
        else:
            threading.Thread(target=self._ensure_ready_guarded, name="OllamaLifecycle", daemon=True).start()

    def wait_until_done(self, timeout: Optional[float] = None) -> bool:
        """For tests (and anything else that wants to know an async
        ensure_ready() call has finished) - True if it finished within
        timeout."""
        return self._done.wait(timeout)

    def _ensure_ready_guarded(self) -> None:
        try:
            self._do_ensure_ready()
        except Exception:
            logger.exception("OllamaLifecycle.ensure_ready failed unexpectedly")
        finally:
            self._done.set()

    def _do_ensure_ready(self) -> None:
        if not _is_relevant():
            return
        if not config.OLLAMA_AUTOSTART:
            return
        if not shutil.which("ollama"):
            logger.debug("Ollama not on PATH - nothing to manage.")
            return
        host = config.OLLAMA_HOST
        models = self._query_tags(host)
        if models is None:
            if not self._start_server():
                self._emit("problem", "Found Ollama installed, but couldn't start 'ollama serve'.")
                return
            models = self._wait_until_reachable(host, config.OLLAMA_STARTUP_TIMEOUT_S)
            if models is None:
                self._emit("problem", "Started 'ollama serve' but it never became reachable.")
                return
            self._emit("server_started", f"Started Ollama ({host}).")
        else:
            self._emit("already_running", f"Ollama is already running at {host}.")

        if not config.OLLAMA_AUTO_PULL_MODEL:
            return
        model = config.OLLAMA_MODEL
        if self._model_present(models, model):
            self._emit("model_ready", f"Model '{model}' is pulled and ready.")
        elif self._pull_model(host, model):
            self._emit("model_ready", f"Model '{model}' pulled and ready.")

    def shutdown(self, timeout: float = 3.0) -> None:
        """Only stops a server THIS instance started (see module
        docstring) - a no-op otherwise, including when ensure_ready found
        one already running."""
        with self._lock:
            proc, self._process = self._process, None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("ollama serve didn't exit in time - killing it.")
            try:
                proc.kill()
                proc.wait(timeout=timeout)
            except Exception:
                logger.exception("Couldn't kill the Ollama server process.")
        except Exception:
            logger.exception("Error stopping the Ollama server process.")


ollama_lifecycle = OllamaLifecycle()
