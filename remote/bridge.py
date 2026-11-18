"""
remote/bridge.py — Tier 23: the one door this package has into WaveBrain.

Nothing else in remote/ ever touches `brain` directly - server.py calls
RemoteBridge.ask(), full stop. That keeps the trust boundary in one place:
if a phone-originated message is ever going to get special handling (a
lower confirm threshold, a different system prompt, anything), this is the
only file that would need to change.

It deliberately does NOT get special handling. `ask()` calls
brain.think_stream() - the exact same method ui/workers.py's
AIStreamWorker calls for a typed or spoken desktop message - and joins its
chunks into one string. A T2/T3 tool the phone's message triggers still
goes through brain/confirm_gate.py's normal confirmation round-trip
(brain.pending_action), worded exactly as it would be for a desktop user;
the confirmation question comes back as this call's return value like any
other reply, and the next ask() with "yes"/"haan"/etc. replays it exactly
as typing "yes" into the desktop chat would. There is no bypass.

Concurrency: WaveBrain was never built to be called from two threads at
once (ui/main_window.py only ever has one AIStreamWorker in flight,
cancelling the previous one before starting another - see
MainWindow.send_message()). A phone request arriving while the desktop is
mid-turn, or two phone requests arriving at once, would otherwise race on
brain.pending_action and the chat-history save. RemoteBridge.ask() holds
one lock around the whole brain.think_stream() call for exactly this
reason - a second caller blocks until the first turn finishes rather than
interleaving with it. On a single-user LAN dashboard this costs nothing
anyone will notice; it exists purely to stop silent state corruption.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("remote.bridge")

_REPLY_PREVIEW_CHARS = 300


class RemoteBridge:
    """Wraps one WaveBrain instance for remote/server.py to call."""

    def __init__(self, brain):
        self._brain = brain
        self._lock = threading.Lock()

    def ask(self, user_text: str, *, timeout: Optional[float] = None) -> str:
        """Runs `user_text` through the brain exactly as a desktop typed
        message would be, and returns the finished reply string.

        `timeout` is how long to wait for the LOCK (i.e. for whatever turn
        is already in flight to finish) before giving up - it is NOT a
        timeout on the brain's own thinking time, which this bridge never
        second-guesses. None (the default) waits indefinitely, matching
        what would happen if two desktop messages were ever queued.
        Returns a short apology string rather than raising if the lock
        can't be acquired in time, since this is a reply a phone is
        waiting to see, not an internal error path."""
        acquired = self._lock.acquire(timeout=timeout if timeout is not None else -1)
        if not acquired:
            return "Wave is still finishing your last request - try again in a moment."
        try:
            started = time.monotonic()
            try:
                chunks = list(self._brain.think_stream(user_text))
            except Exception as exc:  # pragma: no cover - defensive; think_stream itself is heavily tested elsewhere
                logger.error("remote turn failed: %s", exc)
                return "Something went wrong answering that - please try again."
            reply = "".join(chunks)
            elapsed_ms = (time.monotonic() - started) * 1000
            self._audit(user_text, reply, elapsed_ms)
            return reply
        finally:
            self._lock.release()

    def _audit(self, user_text: str, reply: str, elapsed_ms: float) -> None:
        """A visible trail that something came in over the dashboard,
        independent of whatever per-tool rows think_stream's own tool
        dispatch already wrote (those still say source="ai" - this is a
        separate row for the turn itself, not a replacement for them)."""
        try:
            from automation.audit import audit
            audit.record(
                "remote_message",
                {"text": user_text[:_REPLY_PREVIEW_CHARS]},
                result=reply[:_REPLY_PREVIEW_CHARS],
                status="ok",
                source="remote",
            )
        except Exception as exc:  # audit is best-effort - never let logging break a reply
            logger.debug("remote audit log skipped: %s", exc)
