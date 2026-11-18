"""
brain/redaction.py — Tier 20: secret redaction before anything is logged,
traced, or shown in diagnostics.
================================================================================
Nothing in Wave today scrubs a secret out of a log line, a telemetry
annotation, or a diagnostics report before it's written/shown. An LLM
provider error message that happens to echo back an API key, a tool
argument a user pasted that contains a password, or a card number
mentioned in passing - all of it would go straight into wave_system.log,
telemetry/store.py's turn_trace table, or a spoken/printed diagnostics
report, in full, forever (or until the log rotates).

This module is one small, dependency-free scanner that finds text shaped
like a secret and replaces it with a `[REDACTED:<kind>]` tag - never
partial masking (no "sk-...1234" - the whole thing is gone), and never a
claim of catching everything a determined user could paste. It is wired
into exactly the three places the roadmap named:
  * settings/logging_config.py - a handler-level logging.Filter, same
    shape as Tier 19's _TurnIdFilter and attached the same way (per
    HANDLER, not the root logger - see that module for why).
  * telemetry/trace.py - annotate()/mark()'s string metadata and
    end_turn()'s `error` field are redacted before they're stored in the
    ring buffer / handed to telemetry/store.py for the DB.
  * automation/health_check.py / skills/self_diagnostics.py - their
    final report strings are redacted before being returned (an LLM
    provider's own error text is the most likely leak path here - a
    "invalid API key: sk-..." message would otherwise go straight into a
    spoken or on-screen diagnostics report).

Kinds covered, in two groups:
  * SARA's own baseline (per docs/SARA_GAP_ANALYSIS.md): a PEM private
    key block, Google (AIza...), OpenAI (sk-...), GitHub (gh?_...), AWS
    access key ids (AKIA...), a JWT, a bearer token, key=value-shaped
    passwords, and card numbers (validated with a real Luhn check, not
    just "16 digits in a row" - a plain 16-digit run is common in
    non-card contexts and would otherwise false-positive constantly).
  * 🏆 Wave's own additions, none of which SARA covers: Groq API keys
    (gsk_... - Wave's own default provider), Aadhaar numbers (validated
    with the real Verhoeff checksum algorithm UIDAI uses, not just "12
    digits"), PAN, UPI IDs (restricted to a curated list of real bank/PSP
    handle suffixes - see _UPI_HANDLES below for why an unrestricted
    version would just be a worse email regex), IFSC codes, and Indian
    mobile phone numbers.

Design, on purpose - same philosophy as brain/content_trust.py's scan():
every public function is a no-op, and NEVER raises, on bad input or a
disabled feature (REDACT_SECRETS_ENABLED=false restores exactly the
pre-Tier-20 behavior - nothing is scrubbed, nothing changes). A redactor
that could crash the log line it's trying to protect would be strictly
worse than no redactor at all.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from settings.config import config

logger = logging.getLogger("brain.redaction")


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum (bank card numbers). `digits` must already be
    digits-only (no spaces/dashes)."""
    try:
        total = 0
        parity = len(digits) % 2
        for i, ch in enumerate(digits):
            d = int(ch)
            if i % 2 == parity:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0
    except Exception:
        return False


# Verhoeff checksum tables - the real algorithm UIDAI uses for Aadhaar's
# own check digit (the last of the 12), not a plain "12 digits" guess.
_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff_ok(digits: str) -> bool:
    """True if `digits` (digits-only, 12 characters for a real Aadhaar
    number) passes the Verhoeff check - computed over the WHOLE string,
    check digit included; a valid number always reduces to 0."""
    try:
        c = 0
        for i, ch in enumerate(reversed(digits)):
            c = _V_D[c][_V_P[i % 8][int(ch)]]
        return c == 0
    except Exception:
        return False


# Real bank/PSP handles a genuine UPI VPA (virtual payment address) ends
# in - deliberately NOT "any string@any string", which would just be a
# worse email regex and would flag every ordinary email address in a log
# line. Not exhaustive (new handles appear over time) - this is the same
# "basic boundary, not a claim of completeness" standard content_trust.py
# already holds itself to, not a claim every real VPA is covered.
_UPI_HANDLES = (
    "upi", "ybl", "okaxis", "okhdfcbank", "oksbi", "okicici", "paytm",
    "apl", "axl", "ibl", "axisbank", "sbi", "hdfcbank", "icici", "yesbank",
    "idfcbank", "kotak", "federal", "indus", "pingpay", "airtel", "jio",
    "freecharge", "pockets", "rapl", "waaxis", "yapl",
)


def _luhn_validator(matched: str) -> bool:
    digits = re.sub(r"[ \-]", "", matched)
    return 13 <= len(digits) <= 19 and _luhn_ok(digits)


def _aadhaar_validator(matched: str) -> bool:
    digits = re.sub(r"[ \-]", "", matched)
    return len(digits) == 12 and _verhoeff_ok(digits)


@dataclass(frozen=True)
class _SecretKind:
    name: str
    pattern: re.Pattern
    validator: Optional[Callable[[str], bool]] = None


_KINDS: List[_SecretKind] = [
    # -- SARA's baseline kinds --------------------------------------------
    (_SecretKind("private_key", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"
        r"[\s\S]+?-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"))),
    _SecretKind("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    _SecretKind("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}\b")),
    _SecretKind("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    _SecretKind("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    _SecretKind("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    _SecretKind("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9\-\._~\+/]{10,}=*", re.I)),
    _SecretKind("kv_password", re.compile(
        r"\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*"
        r"['\"]?\S{4,}['\"]?", re.I)),
    _SecretKind("card_number", re.compile(r"\b(?:\d[ \-]?){13,19}\d\b"), _luhn_validator),
    # -- 🏆 Wave's own additions - SARA covers none of these --------------
    _SecretKind("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    _SecretKind("aadhaar", re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}\b"), _aadhaar_validator),
    _SecretKind("pan", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b")),
    _SecretKind("upi_id", re.compile(
        r"\b[\w.\-]{2,256}@(?:" + "|".join(re.escape(h) for h in _UPI_HANDLES) + r")\b", re.I)),
    _SecretKind("ifsc", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
    _SecretKind("in_phone", re.compile(r"(?<!\d)(?:\+?91[\-\s]?|0)?[6-9]\d{9}(?!\d)")),
]


def _matches() -> List[_SecretKind]:
    return _KINDS


def scan_secrets(text: str) -> List[Tuple[int, int, str]]:
    """(start, end, kind) for every secret-shaped span found, after
    resolving overlaps (a longer match wins; ties keep the earlier kind
    in _KINDS' own definition order, so a more specific pattern checked
    first - e.g. groq_key before a generic bearer_token shape - isn't
    accidentally shadowed by a later, broader one). Never raises - a scan
    that fails returns no hits rather than taking down whatever called it."""
    if not text:
        return []
    candidates: List[Tuple[int, int, str]] = []
    try:
        for kind in _matches():
            for m in kind.pattern.finditer(text):
                if kind.validator is not None and not kind.validator(m.group(0)):
                    continue
                candidates.append((m.start(), m.end(), kind.name))
    except Exception:
        logger.debug("redaction.scan_secrets failed", exc_info=True)
        return []
    # Longest-span-first, then by where it starts, then definition order -
    # a stable greedy left-to-right sweep keeping only non-overlapping spans.
    candidates.sort(key=lambda c: (c[0], -(c[1] - c[0])))
    chosen: List[Tuple[int, int, str]] = []
    cursor = -1
    for start, end, name in candidates:
        if start < cursor:
            continue
        chosen.append((start, end, name))
        cursor = end
    return chosen


def redact(text: str) -> str:
    """Replaces every secret-shaped span in `text` with `[REDACTED:<kind>]`.
    Returns `text` unchanged when it's empty, REDACT_SECRETS_ENABLED is
    False, or anything goes wrong - never raises, never partially mangles
    the string."""
    if not text or not getattr(config, "REDACT_SECRETS_ENABLED", True):
        return text
    try:
        hits = scan_secrets(text)
        if not hits:
            return text
        out = []
        cursor = 0
        kinds = []
        for start, end, name in hits:
            out.append(text[cursor:start])
            out.append(f"[REDACTED:{name}]")
            cursor = end
            kinds.append(name)
        out.append(text[cursor:])
        result = "".join(out)
        _log_redaction(kinds)
        return result
    except Exception:
        logger.debug("redaction.redact failed - returning the original text", exc_info=True)
        return text


def _log_redaction(kinds: List[str]) -> None:
    """Tier 20: tells automation/security_log.py a secret was actually
    scrubbed - one of the two genuinely-new event kinds that module adds on
    top of audit.py's own log (see its docstring). The reason text here is
    just the kind names, never the secret itself, so this is never a second
    leak path; lazy/local import both ways (security_log imports this
    module too, to redact its own event text) keeps neither module needing
    the other at import time."""
    if not kinds:
        return
    try:
        from automation import security_log
        unique = sorted(set(kinds))
        security_log.record(
            "redaction",
            reason=f"Scrubbed {len(kinds)} secret-shaped value(s) ({', '.join(unique)}) "
                   f"before logging/tracing/showing it.",
        )
    except Exception:
        logger.debug("redaction._log_redaction failed", exc_info=True)
