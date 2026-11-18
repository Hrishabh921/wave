"""
remote/netinfo.py — Tier 23: "what address should the QR code point at?"

One best-effort helper. Never raises - worst case it falls back to
127.0.0.1, which simply means the QR code/URL won't work from another
device (loopback isn't reachable from a phone), and the dashboard page
says so rather than pretending otherwise.
"""
from __future__ import annotations

import socket
from typing import Optional


def get_lan_ip() -> str:
    """The LAN-facing IPv4 address of this machine, best guess.

    Uses the standard no-packet-sent trick: opening a UDP socket "to"
    8.8.8.8 never actually transmits anything (UDP has no handshake), but
    it makes the OS pick which local interface/address *would* be used to
    reach the public internet, which getsockname() then reports - that's
    almost always the real LAN IP, not loopback or a Docker bridge. Falls
    back to socket.gethostbyname(hostname) and then to "127.0.0.1" if even
    that fails (no network at all, or a locked-down sandbox).
    """
    s: Optional[socket.socket] = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


def pairing_url(ip: str, port: int, token: str, *, scheme: str = "http") -> str:
    """The exact URL the QR code encodes and the short code is a stand-in
    for. Kept as one function so server.py, qr.py and the settings page
    all build it identically - a drifted URL format between "what the QR
    encodes" and "what /login actually accepts" would be a silent bug."""
    return f"{scheme}://{ip}:{port}/login?token={token}"
