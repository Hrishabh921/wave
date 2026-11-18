"""
remote/runner.py — Tier 23: starts/stops the dashboard's uvicorn server.

Wave's main event loop is Qt's, not asyncio's, so uvicorn - which wants to
own an asyncio event loop - runs on its own daemon thread here rather than
being awaited from anywhere. uvicorn.Server exposes `should_exit` for
exactly this "stop a server that's running on another thread" case; this
module's stop() sets it and joins the thread rather than killing it.

This is a LAN tool, not an internet-facing one. Binding host="0.0.0.0"
(the default) means every interface on this machine accepts connections
on the chosen port - reachable from anything on the same Wi-Fi/LAN, which
is the whole point (your phone, on the same network). It does nothing to
make itself reachable from the internet, and nothing here opens a port on
your router. ui/settings_panel.py's Remote page says this in plain words
next to the Enable checkbox.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from remote.bridge import RemoteBridge
from remote.netinfo import get_lan_ip, pairing_url
from remote.security import new_pairing_material
from remote.server import HAS_FASTAPI, DashboardState, create_app

try:
    import uvicorn
except Exception:  # pragma: no cover - mirrors HAS_FASTAPI; uvicorn ships with fastapi's extra in requirements.txt
    uvicorn = None  # type: ignore[assignment]

logger = logging.getLogger("remote.runner")

DEFAULT_PORT = 8787


@dataclass
class PairingInfo:
    url: str
    code: str
    lan_ip: str
    port: int


class RemoteDashboardServer:
    """Owns at most one running uvicorn server at a time. Safe to
    construct even when fastapi/uvicorn aren't installed - only start()
    actually requires them, and it fails informatively instead of
    raising an ImportError from somewhere deep in the call stack."""

    def __init__(self, brain, assistant_name: str = "Wave"):
        self._brain = brain
        self._assistant_name = assistant_name
        self._bridge = RemoteBridge(brain)
        self._state: Optional[DashboardState] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._port = DEFAULT_PORT

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def available(self) -> bool:
        """False means "can't be started here at all" (fastapi/uvicorn
        missing) - distinct from is_running, which is about whether it
        currently IS started."""
        return HAS_FASTAPI and uvicorn is not None

    def start(self, host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> Tuple[bool, str]:
        """Returns (ok, message). Starting an already-running server is a
        no-op that reports success - idempotent, so a settings page that
        calls start() on every load_settings() doesn't double-bind."""
        if self.is_running:
            return True, "Already running."
        if not self.available:
            return False, (
                "fastapi/uvicorn aren't installed - pip install fastapi "
                "\"uvicorn[standard]\" to use the remote dashboard."
            )
        token, code, secret = new_pairing_material()
        self._state = DashboardState(
            bridge=self._bridge, assistant_name=self._assistant_name,
            pairing_token=token, pairing_code=code, session_secret=secret,
        )
        self._port = port
        try:
            app = create_app(self._state)
            config = uvicorn.Config(app, host=host, port=port, log_level="warning")
            self._server = uvicorn.Server(config)
        except Exception as exc:
            logger.error("remote dashboard failed to configure: %s", exc)
            return False, f"Could not start the dashboard: {exc}"

        self._thread = threading.Thread(target=self._server.run, daemon=True, name="wave-remote-dashboard")
        self._thread.start()
        return True, f"Remote dashboard running on port {port}."

    def stop(self, *, join_timeout: float = 5.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=join_timeout)
        self._thread = None
        self._server = None

    def regenerate_pairing(self) -> Optional[PairingInfo]:
        """New token/code/session-secret - invalidates every paired
        phone's session at once (see DashboardState.rotate's docstring).
        Returns None if the dashboard isn't running."""
        if self._state is None:
            return None
        token, code, secret = new_pairing_material()
        self._state.rotate(token, code, secret)
        return self.pairing_info()

    def pairing_info(self) -> Optional[PairingInfo]:
        """None if the dashboard isn't running - there's nothing to pair
        with yet."""
        if self._state is None:
            return None
        ip = get_lan_ip()
        return PairingInfo(
            url=pairing_url(ip, self._port, self._state.pairing_token),
            code=self._state.pairing_code,
            lan_ip=ip,
            port=self._port,
        )
