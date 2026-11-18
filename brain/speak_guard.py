"""
brain/speak_guard.py — Tier 20: a guard on text Wave will *speak* verbatim.

Two separate protections, both applied to whatever's about to come out of
the speaker, before it reaches voice/tts.py's synthesis step:

  1. Secrets are never read aloud. brain/redaction.py already keeps a
     secret out of logs/telemetry/diagnostics - this closes the one
     remaining leak path an LLM provider error or a tool result could take:
     straight out of the speaker. Always on (same REDACT_SECRETS_ENABLED
     kill-switch), regardless of SECURITY_MODE.
  2. Flagged content from an untrusted source isn't read back verbatim.
     If this turn touched something content_trust.py flagged (a prompt-
     injection-shaped web page, OCR'd screen text, or attachment - see
     brain/taint.py's turn_flagged()) AND the actual text about to be
     spoken also scans as flagged, it's replaced with a short, honest
     notice instead of being spoken - reading a flagged payload out loud
     is itself a vector (a voice-based social-engineering line, or simply
     amplifying an injection attempt through the speaker) that redaction
     alone wouldn't catch (the flagged text need not look like a secret at
     all). Gated by SECURITY_MODE != "off" - "off" restores pre-Tier-20
     behaviour (nothing but redaction applied).

Threading note, important for correctness
--------------------------------------------
brain/taint.py's turn_tainted()/turn_flagged() are per-THREAD state. The
normal AI-reply path (ui/workers.py's AIStreamWorker._run_inner()) calls
think_stream() and then voice/tts.py's speak_stream() SYNCHRONOUSLY on the
very same thread, so a turn_flagged() read at the moment speak_stream()
is first called correctly reflects that turn - but speak_stream() itself
then hands each finished sentence off to an internal "speaker" thread for
actual playback (see its own docstring). Reading taint state again from
inside that internal thread, or from voice/tts.py's lower-level
_speak_task()/_run_utterance(), would silently see a thread that was never
tainted at all and always pass the guard - a guard that LOOKS like it
checks taint but never actually fires is worse than no guard.

So this module is called from the ORIGINAL calling thread, right where
voice/tts.py's speak()/speak_stream() still run on it (speak_stream()'s own
emit() helper, before prefetch()/queuing; speak() itself, before spawning
its playback thread) - never from inside _speak_task()/_run_utterance().
"""
from __future__ import annotations

import logging
from typing import Optional

from settings.config import config

logger = logging.getLogger("brain.speak_guard")

SAFE_NOTICE = "I found something in that content that looked unsafe to read out loud, so I've left it out."


def guard_speech(text: str) -> str:
    """Redacts secrets, then - unless SECURITY_MODE is "off" - withholds
    the text entirely (replacing it with SAFE_NOTICE) if this turn touched
    flagged content AND the text itself also scans as flagged. Never
    raises - returns `text` unchanged on any internal failure, same
    fail-open philosophy as every other Tier 20 module (a guard that can
    crash the reply it's trying to protect is worse than no guard)."""
    if not text:
        return text
    try:
        from brain import redaction
        text = redaction.redact(text)
    except Exception:
        logger.debug("speak_guard: redaction step failed", exc_info=True)

    try:
        from automation import security_log
        if security_log.effective_mode() == "off":
            return text
        from brain import taint
        if not taint.turn_flagged():
            return text
        from brain import content_trust
        result = content_trust.scan(text)
        if result.flagged:
            security_log.record(
                "speak_guard",
                reason="Withheld flagged content from speech - this turn touched "
                       "untrusted content and the reply itself also scanned as flagged.",
                taint_sources=taint.turn_sources(),
            )
            return SAFE_NOTICE
    except Exception:
        logger.debug("speak_guard: flagged-content step failed - speaking redacted text as-is",
                      exc_info=True)
    return text
