"""
remote/server.py — Tier 23: the FastAPI app behind the remote dashboard.

HAS_FASTAPI follows the same optional-dependency pattern as the rest of
this package (see remote/__init__.py and remote/qr.py) - `fastapi` and
`uvicorn` are declared in requirements.txt but are allowed to be missing;
remote/runner.py checks this flag before trying to start anything, and
ui/settings_panel.py's Remote page shows an install hint instead of a
crash when it's False.

Routes:
    GET  /login            - pairing page (reads ?token=... from the QR
                              code's URL and auto-submits; also accepts
                              the short code typed by hand)
    POST /api/pair          - {"token": "..."} -> sets the signed session
                               cookie on success, 401 + a rate-limit
                               lockout on repeated failure
    GET  /                  - the chat page (redirects to /login if the
                               session cookie is missing/invalid/expired)
    GET  /api/status        - {"assistant_name": ..., "paired": bool}
    POST /api/message       - {"text": "..."} -> {"reply": "..."}, the
                               one call that actually reaches the brain,
                               via remote/bridge.py - session-gated AND
                               CSRF-header-gated (see _require_session)
    GET  /static/*          - app.js / style.css for both pages

Every state-changing request (pairing, sending a message) must carry the
header `X-Wave-Dashboard: 1`. A plain cross-site <form> POST (the classic
CSRF vector) cannot set a custom header without JavaScript already
running same-origin, so this one header - combined with the session
cookie being SameSite=Strict - is the whole CSRF defense, and it is
deliberately this simple rather than pulling in a dedicated CSRF-token
library for a single-user LAN tool.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from remote.bridge import RemoteBridge
from remote.security import (
    PairingRateLimiter,
    SessionSigner,
    constant_time_eq,
)

logger = logging.getLogger("remote.server")

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE_NAME = "wave_session"
CSRF_HEADER_NAME = "x-wave-dashboard"

try:
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    HAS_FASTAPI = True
except Exception:  # pragma: no cover - exercised by tests via monkeypatch
    FastAPI = None  # type: ignore[assignment]
    Request = Response = HTMLResponse = JSONResponse = RedirectResponse = None  # type: ignore[assignment]
    StaticFiles = None  # type: ignore[assignment]
    HAS_FASTAPI = False


class DashboardState:
    """Everything one running dashboard needs that isn't the brain itself.
    Held outside the FastAPI app object so remote/runner.py can rotate the
    pairing token/regenerate sessions without tearing the server down."""

    def __init__(self, bridge: RemoteBridge, assistant_name: str,
                 pairing_token: str, pairing_code: str, session_secret: str):
        self.bridge = bridge
        self.assistant_name = assistant_name
        self.pairing_token = pairing_token
        self.pairing_code = pairing_code
        self.signer = SessionSigner(session_secret)
        self.rate_limiter = PairingRateLimiter()

    def rotate(self, pairing_token: str, pairing_code: str, session_secret: str) -> None:
        """Called by "Regenerate pairing code" in the settings panel.
        Changing session_secret invalidates every previously-issued
        session cookie at once (SessionSigner.verify uses the new secret,
        so an old cookie's HMAC no longer matches) - the simplest possible
        "log everyone out" with no session store to walk."""
        self.pairing_token = pairing_token
        self.pairing_code = pairing_code
        self.signer = SessionSigner(session_secret)


def _client_ip(request: "Request") -> str:
    return request.client.host if request.client else "unknown"


def create_app(state: DashboardState):
    """Builds and returns the FastAPI app. Raises RuntimeError if fastapi
    isn't installed - callers (remote/runner.py) check HAS_FASTAPI first,
    so this is a defensive double-check, not the normal way the absence
    is discovered."""
    if not HAS_FASTAPI:
        raise RuntimeError(
            "fastapi/uvicorn aren't installed - pip install fastapi uvicorn[standard] "
            "to use the remote dashboard."
        )

    app = FastAPI(title="Wave Remote Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def _read_page(name: str) -> str:
        path = STATIC_DIR / name
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return f"<html><body><p>Missing page: {name}</p></body></html>"

    def _has_valid_session(request: "Request") -> bool:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        return state.signer.verify(cookie)

    def _require_csrf_header(request: "Request") -> bool:
        return request.headers.get(CSRF_HEADER_NAME) == "1"

    @app.get("/login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(_read_page("login.html"))

    @app.post("/api/pair")
    async def pair(request: "Request", response: "Response"):
        if not _require_csrf_header(request):
            return JSONResponse({"error": "missing dashboard header"}, status_code=400)
        ip = _client_ip(request)
        if state.rate_limiter.is_locked_out(ip):
            return JSONResponse(
                {"error": "too many attempts - wait a few minutes and try again"},
                status_code=429,
            )
        body = await request.json()
        candidate = str(body.get("token") or body.get("code") or "")
        valid = constant_time_eq(candidate, state.pairing_token) or constant_time_eq(
            candidate.upper().replace(" ", ""), state.pairing_code
        )
        if not valid:
            state.rate_limiter.record_failure(ip)
            return JSONResponse({"error": "incorrect pairing code"}, status_code=401)
        state.rate_limiter.record_success(ip)
        session_value = state.signer.issue()
        resp = JSONResponse({"ok": True, "assistant_name": state.assistant_name})
        resp.set_cookie(
            SESSION_COOKIE_NAME, session_value,
            httponly=True, samesite="strict", max_age=24 * 60 * 60,
        )
        return resp

    @app.get("/", response_class=HTMLResponse)
    async def index(request: "Request"):
        if not _has_valid_session(request):
            return RedirectResponse("/login")
        return HTMLResponse(_read_page("index.html"))

    @app.get("/api/status")
    async def status(request: "Request"):
        return JSONResponse({
            "assistant_name": state.assistant_name,
            "paired": _has_valid_session(request),
        })

    @app.post("/api/message")
    async def message(request: "Request", response: "Response"):
        if not _has_valid_session(request):
            return JSONResponse({"error": "not paired"}, status_code=401)
        if not _require_csrf_header(request):
            return JSONResponse({"error": "missing dashboard header"}, status_code=400)
        body = await request.json()
        text = str(body.get("text") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        reply = state.bridge.ask(text)
        # Sliding-TTL refresh: every authenticated request extends the
        # session, same as remote/security.py's own docstring promises.
        response.set_cookie(
            SESSION_COOKIE_NAME, state.signer.refresh(),
            httponly=True, samesite="strict", max_age=24 * 60 * 60,
        )
        return JSONResponse({"reply": reply})

    return app
