"""
remote/security.py — Tier 23: pairing tokens, signed sessions, rate limiting.

Deliberately stdlib-only (`hmac`, `hashlib`, `secrets`, `time`) - the whole
point of this file is that the dashboard's trust boundary doesn't depend on
one more third-party package being present and un-vulnerable. Nothing here
talks to the network or to Qt; it's pure, easily-unit-tested logic, imported
by remote/server.py.

Threat model this is actually defending against: someone else on the same
Wi-Fi/LAN hitting the dashboard's port before the real owner pairs their
phone, or a signed session cookie being replayed after it should have
expired, or a lucky guess at the 6-character pairing code. It is NOT
defending against a hostile router, a compromised OS, or the internet in
general - the dashboard is LAN-only by design (see remote/runner.py's own
docstring), and nothing here tries to pretend otherwise.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# A pairing code is short on purpose - it's typed by hand only as a fallback
# to scanning the QR code, and the QR code itself carries the FULL token
# (see qr.py), so the short code is never the only thing standing between
# an attacker and pairing. 8 chars of this alphabet is ~2^38 of entropy,
# comfortably beyond what the rate limiter below lets anyone brute force in
# a human lifetime.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - unambiguous when read off a screen
_CODE_LEN = 8

# A session is good for this long after the LAST request that used it
# (sliding expiry) - not from when it was issued. Closing the browser tab
# on your phone for an hour and coming back shouldn't force a fresh pairing
# dance; walking away for a day should.
SESSION_TTL_SECONDS = 24 * 60 * 60

# Pairing attempts: this many wrong tokens from one source IP within the
# window below, then that IP is locked out until the window elapses. Keyed
# by IP (not globally) so one wrong guess from someone else's phone can't
# lock the real owner out of pairing their own.
MAX_PAIR_ATTEMPTS = 8
PAIR_ATTEMPT_WINDOW_SECONDS = 5 * 60


def generate_pairing_token() -> str:
    """A full-entropy token - this is what actually goes inside the QR
    code's URL (as a query parameter), never typed by hand. secrets.
    token_urlsafe(24) is 32 base64url characters, ~144 bits."""
    return secrets.token_urlsafe(24)


def generate_pairing_code() -> str:
    """The short, human-typeable fallback shown next to the QR code for
    anyone whose phone camera can't reach the laptop screen."""
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))


def constant_time_eq(a: str, b: str) -> bool:
    """hmac.compare_digest wants bytes on some platforms' older stdlibs;
    .encode() first so callers never have to remember that."""
    try:
        return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
    except Exception:
        return False


@dataclass
class SessionSigner:
    """Signs/verifies a tiny session cookie value: `<expiry>.<hmac>`.

    Deliberately not a JWT (no library, no header/alg-confusion surface) -
    the payload here is nothing but a timestamp, so there's nothing to
    smuggle into it. `secret` should be regenerated whenever the pairing
    token is regenerated (remote/server.py does this), so an old cookie
    from before a "revoke all sessions" click stops verifying immediately -
    no session blacklist to maintain.
    """

    secret: str

    def _mac(self, expiry: int) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            str(expiry).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def issue(self, *, now: Optional[float] = None) -> str:
        expiry = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
        return f"{expiry}.{self._mac(expiry)}"

    def verify(self, token: Optional[str], *, now: Optional[float] = None) -> bool:
        """True only for a token this signer actually issued, not yet
        expired, and not tampered with. Never raises - a malformed cookie
        (missing dot, non-numeric expiry, wrong length) is just invalid,
        the same as an expired or mismatched one."""
        if not token or "." not in token:
            return False
        expiry_str, _, mac = token.partition(".")
        try:
            expiry = int(expiry_str)
        except ValueError:
            return False
        current = now if now is not None else time.time()
        if current > expiry:
            return False
        return constant_time_eq(mac, self._mac(expiry))

    def refresh(self, *, now: Optional[float] = None) -> str:
        """A fresh token with a pushed-out expiry - called on every
        authenticated request so the sliding-TTL promise above holds."""
        return self.issue(now=now)


@dataclass
class PairingRateLimiter:
    """Per-source-IP sliding-window lockout for failed pairing attempts.
    Lives in memory only - a dashboard restart clears it, which is fine:
    restarting the app is already a much higher bar than typing a token."""

    max_attempts: int = MAX_PAIR_ATTEMPTS
    window_seconds: float = PAIR_ATTEMPT_WINDOW_SECONDS
    _attempts: Dict[str, list] = field(default_factory=dict)

    def _prune(self, ip: str, now: float) -> list:
        bucket = [t for t in self._attempts.get(ip, []) if now - t < self.window_seconds]
        self._attempts[ip] = bucket
        return bucket

    def is_locked_out(self, ip: str, *, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return len(self._prune(ip, now)) >= self.max_attempts

    def record_failure(self, ip: str, *, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        bucket = self._prune(ip, now)
        bucket.append(now)
        self._attempts[ip] = bucket

    def record_success(self, ip: str) -> None:
        """A correct pairing clears that IP's strike count - no reason to
        keep counting against someone who just proved they have the token."""
        self._attempts.pop(ip, None)


def new_pairing_material() -> Tuple[str, str, str]:
    """(token, code, session_secret) - one call, used both at dashboard
    start-up and every time the user clicks "Regenerate pairing code"."""
    return generate_pairing_token(), generate_pairing_code(), secrets.token_urlsafe(32)
