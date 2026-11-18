"""
remote/ — Tier 23: the remote mobile dashboard.

Closes §2.2 of claude/JARVIS_GAP_ANALYSIS.md ("no remote phone dashboard"):
Jarvis ships a FastAPI+uvicorn web control panel paired to a phone by QR
code; Wave had nothing reachable off the desktop at all.

This package is entirely optional and entirely off by default. Nothing in
here is imported by ui/main_window.py's module-level code - only
ui/settings_panel.py's Remote page, and only when the user actually ticks
"Enable remote dashboard", does anything in this package get imported or
run. Deleting this folder removes the whole feature and nothing else in
Wave notices.

Module map:
    security.py   - pairing tokens, signed session cookies, a small
                     per-IP rate limiter for pairing attempts. No new
                     third-party dependency: stdlib `hmac`/`secrets` only.
    netinfo.py     - "what LAN IP should the QR code point at" - one
                     best-effort helper, never raises.
    qr.py          - renders a pairing URL to a PNG (qrcode package;
                     HAS_QRCODE=False and a plain-text fallback if it's
                     not installed - same optional-dependency pattern as
                     voice/tts.py's HAS_PIPER).
    bridge.py      - the ONLY thing allowed to call into WaveBrain from
                     this package. Serializes concurrent phone requests
                     behind one lock so a remote command can never
                     interleave mid-turn with whatever the desktop (voice
                     or typed) is doing, and tags every remote turn with
                     source="remote" in the same audit log every other
                     surface already writes to.
    server.py      - the FastAPI app: pairing, session-gated chat API,
                     and the static mobile UI. HAS_FASTAPI=False (and the
                     whole feature simply can't start) if fastapi/uvicorn
                     aren't installed.
    runner.py      - starts/stops that app's uvicorn server on its own
                     background thread, because Wave's main event loop is
                     Qt's, not asyncio's.

Every remote command - from pairing to the final reply - runs through
EXACTLY the pipeline a typed desktop message does (brain.process_actions,
via bridge.py), so a T2/T3 tool asked for from a phone still has to clear
the same brain/confirm_gate.py confirmation round-trip a desktop user
would see, word for word - there is no separate, weaker code path for
"remote" actions to slip through.
"""
