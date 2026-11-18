"""
brain/exfiltration.py — Tier 20: exfiltration-shaped URL detection.

Part of the Tier 20 policy matrix (brain/groq_core.py's _guard(), via
brain/tiers.py's URL_ARG_TOOLS). web/web_ops.py's validate_url() already
stops Wave's own server-side fetches (summarize_page) from reaching an
internal/metadata IP - that's SSRF protection, a completely different
concern from this module. Nothing in Wave today stops a tainted turn (one
that already read a prompt-injection-shaped web page, OCR'd screen, or
attachment this turn - see brain/taint.py) from asking Wave to visit or
fetch a URL whose query string or path SMUGGLES OUT whatever that turn
just read, to a host the attacker controls. Wave making the request (or
the person's own browser, for open_website) and the attacker's server
simply logging it is the entire attack - no malware, no further action
needed once the request lands.

This module is one small, dependency-free heuristic: not a claim of
catching every exfiltration-shaped URL a determined attacker could craft
(no static scanner can), just the two shapes that actually matter in
practice and are cheap to check with no false-positive risk worth
mentioning:
  1. A secret-shaped value sitting directly in the URL (reuses
     brain/redaction.py's own scanner - if something that looks like an
     API key/card number/Aadhaar/etc. is literally in the URL, something
     is very wrong regardless of where the URL points).
  2. A long base64/hex-shaped blob in the query string or fragment - the
     classic "?data=<thousands of encoded bytes>" shape a real exfil
     payload takes, well past what any legitimate site parameter needs
     (a tracking id or session token is a handful of characters, not
     hundreds).

Design, on purpose - same fail-open philosophy as every other Tier 20
module here: never raises, and an ordinary, short, parameter-free URL is
never flagged.
"""
from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Optional

logger = logging.getLogger("brain.exfiltration")

# A continuous base64/hex-shaped run this long, inside a query value or
# the fragment, is far past anything a legitimate URL parameter needs
# (session ids/tokens are typically 16-64 chars) and well into "this is
# encoded data being carried somewhere" territory.
_BLOB_MIN_LEN = 120
_BLOB_RE = re.compile(r"[A-Za-z0-9+/_\-]{%d,}={0,2}" % _BLOB_MIN_LEN)


def looks_like_exfiltration(url: str) -> Optional[str]:
    """Returns a short, plain-language reason if `url` looks shaped to
    exfiltrate data, or None if it looks like an ordinary URL. Never
    raises - a check that could crash the tool call it's guarding would be
    worse than no check at all."""
    if not url:
        return None
    try:
        from brain import redaction
        hits = redaction.scan_secrets(url)
        if hits:
            kinds = sorted({name for _, _, name in hits})
            return f"the URL itself contains what looks like a secret ({', '.join(kinds)})"

        parsed = urllib.parse.urlparse(url)
        haystack = f"{parsed.query} {parsed.fragment}"
        m = _BLOB_RE.search(haystack)
        if m:
            return f"the URL's query/fragment carries a {len(m.group(0))}-character encoded blob"
    except Exception:
        logger.debug("exfiltration.looks_like_exfiltration failed", exc_info=True)
        return None
    return None
