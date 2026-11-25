"""
reminders/composer.py
Decides what Wave actually says for a reminder: tries an LLM reword in the
background and falls back to reminders/context.py's deterministic template
if that's disabled, too slow, or fails. A reminder's text is never missing
just because the LLM was unreachable.

Usage (ui/workers.py's ReminderWorker calls this directly - it's already
running on its own QThread, off the GUI thread, so a blocking wait() here
never freezes the UI):

    waiter = composer.start(message, stage="due")
    play_beep()                 # do the time-sensitive part first
    result = waiter()           # blocks only if the LLM is still running
    self.reminder_due.emit(result.text, reminder_id)

start() returns immediately - the LLM call (if attempted at all) runs on a
background thread, so a slow/unreachable provider never blocks the next
reminder in the same poll tick. wait() blocks for at most
config.CONTEXTUAL_REMINDER_LLM_TIMEOUT_S, whichever comes first.

This mirrors the start()/wait() shape of the SARA reference project's
reminder_composer.py (plain threading, not asyncio - matching both SARA's
own implementation and Wave's QThread-based workers, which have no asyncio
event loop to integrate with). Ported to Wave's own LLM plumbing
(brain/llm_provider.py) instead of SARA's generate_short(), so this reword
automatically uses whichever provider Wave's main chat is already
configured for (Groq, Ollama, or a custom OpenAI-compatible endpoint).
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from brain import llm_provider
from brain.model_router import background_model
from reminders import context as ctx
from settings.config import config

logger = logging.getLogger("ReminderComposer")


@dataclass(frozen=True)
class ComposeResult:
    text: str
    source: str   # "llm" | "template" | "disabled"
    latency_ms: int
    category: str
    level: str


# Lazily built once per process and reused - resolve_provider()/build_client()
# read the SAME env-based settings brain/groq_core.py's WaveBrain uses, so
# this reword always follows whichever provider the main chat is already
# configured for. Guarded by a lock since ReminderWorker could in principle
# fire two composer.start() calls close together (two reminders due in the
# same poll tick); OpenAICompatClient itself is already thread-safe
# (thread-local requests.Session - see llm_provider.py), it's only this
# lazy-init step that needs guarding.
_client_lock = threading.Lock()
_cached_client = None
_cached_cfg = None
_client_resolved = False


def _get_client_and_cfg():
    global _cached_client, _cached_cfg, _client_resolved
    with _client_lock:
        if not _client_resolved:
            _client_resolved = True
            try:
                cfg = llm_provider.resolve_provider()
                _cached_cfg = cfg
                _cached_client = llm_provider.build_client(cfg) if cfg.configured else None
            except Exception as e:
                logger.info(f"reminders.composer: no LLM provider available ({type(e).__name__}: {e})")
                _cached_client, _cached_cfg = None, None
        return _cached_client, _cached_cfg


class ReminderComposer:

    def __init__(self):
        self._recent = deque(maxlen=5)  # last few LLM-produced lines, so validate_text can reject an exact repeat
        self._recent_lock = threading.Lock()

    def start(
        self,
        message: str,
        stage: str = "due",
        minutes_until: Optional[int] = None,
        minutes_since: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Callable[[], ComposeResult]:
        """Kicks off composition and returns a zero-arg `wait()` closure -
        call it later (after doing whatever's time-sensitive, like playing
        the alarm beep) to get the ComposeResult. stage is "due", "upcoming",
        or "followup" - see reminders/context.py's build_prompt()."""
        now = now or datetime.now()
        intent = ctx.classify(message)
        bucket = ctx.time_bucket(now)
        policy = ctx.effective_policy(intent, bucket)
        fallback = self._template_result(intent, message, stage, minutes_until, minutes_since, now)

        if not config.CONTEXTUAL_REMINDER_LLM or not intent.subject:
            return lambda: fallback

        done = threading.Event()
        box = {"result": None}

        def worker():
            t0 = time.monotonic()
            text = None
            try:
                text = self._call_llm(intent, bucket, now, policy, stage, minutes_until, minutes_since)
            except Exception as e:
                logger.info(f"reminders.composer: LLM reword failed, template will be used ({type(e).__name__}: {e})")
            if text:
                latency_ms = int((time.monotonic() - t0) * 1000)
                with self._recent_lock:
                    self._recent.append(text)
                box["result"] = ComposeResult(
                    text=text, source="llm", latency_ms=latency_ms,
                    category=intent.category, level=intent.level,
                )
            done.set()

        threading.Thread(target=worker, daemon=True, name="wave-reminder-composer").start()

        def wait() -> ComposeResult:
            done.wait(timeout=config.CONTEXTUAL_REMINDER_LLM_TIMEOUT_S)
            return box["result"] if box["result"] is not None else fallback

        return wait

    # ------------------------------------------------------------------
    def _template_result(self, intent: ctx.ReminderIntent, message, stage, minutes_until,
                          minutes_since, now) -> ComposeResult:
        if stage == "upcoming":
            text = ctx.upcoming_message(message, minutes_until)
        elif stage == "followup":
            text = ctx.followup_message(message, minutes_since, now)
        else:
            text = ctx.due_message(message, now)
        if not text:
            text = f"Reminder: {(message or '').strip()}".strip() or "You have a reminder."
        return ComposeResult(
            text=text,
            source="template" if config.CONTEXTUAL_REMINDER_LLM else "disabled",
            latency_ms=0,
            category=intent.category,
            level=intent.level,
        )

    def _call_llm(self, intent: ctx.ReminderIntent, bucket: str, now: datetime,
                   policy: ctx.ReminderPolicy, stage: str,
                   minutes_until: Optional[int], minutes_since: Optional[int]) -> Optional[str]:
        client, cfg = _get_client_and_cfg()
        if client is None or cfg is None:
            return None
        system, user = ctx.build_prompt(
            intent, bucket, now, policy, stage=stage,
            minutes_until=minutes_until, minutes_since=minutes_since,
        )
        response = client.chat.completions.create(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=background_model(cfg),  # Tier 34: the fast model, not the main one
            temperature=0.6,
            max_tokens=100,
            # Deliberately NOT cfg.timeout - that's sized for a full
            # conversational turn. A reminder reword needs to fail fast so
            # wait() can fall back to the template well before
            # CONTEXTUAL_REMINDER_LLM_TIMEOUT_S anyway.
            timeout=config.CONTEXTUAL_REMINDER_LLM_TIMEOUT_S,
        )
        raw = ((response.choices[0].message.content if response and response.choices else "") or "").strip()

        allowed_numbers = None
        if stage == "upcoming" and minutes_until:
            allowed_numbers = [int(minutes_until)]
        elif stage == "followup" and minutes_since is not None:
            allowed_numbers = [int(minutes_since)]

        with self._recent_lock:
            recent_snapshot = list(self._recent)
        return ctx.validate_text(raw, policy.max_words, allowed_numbers=allowed_numbers, recent=recent_snapshot)


# Module-level singleton - one small instance per process, shared by every
# ReminderWorker poll tick (mirrors reminders/context.py's own
# module-level-functions style, and db_manager.py's `db = DatabaseManager()`
# / web_ops.py's `_session` singleton pattern).
composer = ReminderComposer()
