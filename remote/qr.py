"""
remote/qr.py — Tier 23: renders the pairing URL as a QR code.

Same optional-dependency shape as the rest of Wave (voice/tts.py's
HAS_PIPER, web/web_ops.py's HAS_PYWHATKIT guard): `qrcode` is a genuinely
optional package (it's only needed by the one feature in this file), so an
import failure degrades the feature instead of crashing anything that
imports this module. The settings page checks HAS_QRCODE and shows the
plain-text pairing code/URL instead of a QR image when it's False.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger("remote.qr")

try:
    import qrcode
    HAS_QRCODE = True
except Exception:  # pragma: no cover - exercised by tests via monkeypatch
    qrcode = None
    HAS_QRCODE = False


def build_pairing_qr_png(url: str) -> bytes:
    """PNG bytes encoding `url`. Raises RuntimeError if qrcode isn't
    installed - callers (the settings page, the /login page's own QR
    preview) check HAS_QRCODE first and show a text fallback instead of
    calling this at all, so this is a programming-error guard, not a
    user-facing path."""
    if not HAS_QRCODE:
        raise RuntimeError(
            "qrcode isn't installed - pip install qrcode[pil] to show a "
            "scannable code (the plain pairing code/URL still work without it)."
        )
    qr = qrcode.QRCode(
        version=None,  # auto-size to the data
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
