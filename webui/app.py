"""Tier 24: pywebview entry point for Wave's HTML/CSS/JS UI.

Deliberately tiny - everything that matters is in bridge.py (the backend
seam) and static/ (the page itself). This module's only job is: create
the WaveWebBridge, point a pywebview window at static/index.html with the
bridge as its js_api, hand the bridge a reference to the window so it can
push events (`bridge.set_window`), and start the GUI loop.

The window is sized the way ui/main_window.py's MainWindow.init_ui() sizes
the desktop one (1520x940, capped to 94%/90% of the screen, centred) with
the same black background, so both UIs open looking the same.
"""
import logging
import os

logger = logging.getLogger("WebUI")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
DEFAULT_W, DEFAULT_H = 1520, 940


def _initial_size(webview):
    """MainWindow.init_ui(): min(1520, 94% of the screen) x min(940, 90%)."""
    try:
        screen = webview.screens[0]
        avail_w, avail_h = int(screen.width), int(screen.height) - 48   # leave room for the taskbar
        return min(DEFAULT_W, int(avail_w * 0.94)), min(DEFAULT_H, int(avail_h * 0.90))
    except Exception:
        return DEFAULT_W, DEFAULT_H


def run():
    import webview
    from webui.bridge import WaveWebBridge

    bridge = WaveWebBridge()
    width, height = _initial_size(webview)
    window = webview.create_window(
        "Wave AI — Desktop Assistant",
        url=os.path.join(STATIC_DIR, "index.html"),
        js_api=bridge,
        width=width,
        height=height,
        min_size=(960, 640),
        background_color="#000000",
    )
    bridge.set_window(window)

    def on_closing():
        # Detach first: background threads (mic worker, stats, services)
        # can still push an event while they wind down, and pywebview logs
        # every evaluate_js on the disposed WebView2 as an ERROR.
        bridge.set_window(None)
        try:
            bridge.cancel_reply()
            worker = getattr(bridge, "_mic_worker", None)
            if worker is not None:
                worker.stop()
            bridge._reminder_worker.stop()
            bridge._shutdown_services()
        except Exception:
            logger.debug("shutdown cleanup failed", exc_info=True)

    try:
        window.events.closing += on_closing
    except Exception:
        logger.debug("couldn't hook the window closing event", exc_info=True)
    webview.start()


if __name__ == "__main__":
    run()
