"""Tier 24: the pywebview + HTML/CSS/JS front end (SARA's own UI technology).

This package is a NEW UI technology for Wave, built alongside the existing
PyQt6 layer in `ui/` rather than on top of it. Why alongside, not a deletion
of `ui/`: `ui/`'s ~9,300 lines carry nearly two years of tested behavior and
the project's entire fake-Qt test suite is written against it - throwing
that away in the same tier that replaces the rendering technology would mean
shipping an untested rewrite of everything at once. Instead:

  - `webui/bridge.py` - `WaveWebBridge`, the one seam between the web page
    and Wave's real backend. Every method on it is pure Python (no Qt
    import anywhere in this package), JSON-in/JSON-out, and talks to the
    SAME brain/automation/database modules `ui/main_window.py` already
    talks to - a message sent from the web chat goes through the exact same
    `WaveBrain.think_stream()` call a typed desktop message does, so a
    T2/T3 tool it triggers still has to clear `brain/confirm_gate.py`'s
    normal confirmation round-trip. No second, weaker code path.
  - `webui/voice_worker.py` - plain `threading.Thread` ports of
    `ui/workers.py`'s `ContinuousMicWorker`/`AIStreamWorker`. Those two
    classes were already thin `QThread` wrappers around Qt-independent
    calls into `voice/*.py` and `brain/groq_core.py` - porting them to plain
    threads with callbacks instead of Qt signals needed no change to any
    underlying voice/brain logic at all.
  - `webui/app.py` - the pywebview entry point: opens one native window,
    points it at `webui/static/index.html`, and registers `WaveWebBridge`
    as the page's `js_api`.
  - `webui/bridge_parity.py` - `ParityMixin`, the rest of the bridge: the
    calls the desktop's pages make (files, music, system monitor, Tier 21
    pages, settings, onboarding, background services, global hotkeys).
  - `webui/static/` - the actual UI, rebuilt (Tier 26) to look and behave
    like the PyQt6 desktop UI page for page: `index.html` + `js/theme.js`
    (ui/theme.py's 11 themes, applied as CSS variables) + `css/tokens.css`
    / `css/main.css` (ui/theme.py's QSS translated to CSS) +
    `css/motion.css` (the sliding page stack) + `js/widgets.js` (gauges,
    sparklines, visualizer, sliders, combos, dialogs, toasts) + `js/app.js`
    / `js/pages2.js` (one builder per desktop page) + `js/bridge.js` (a thin
    promise wrapper around `window.pywebview.api.*`) + `fonts/` (the Font
    Awesome 6 font qtawesome draws the desktop icons with). No build step,
    no framework - plain HTML/CSS/JS, edit a file and see the change.

`wave_web_start.pyw` (repo root) launches this UI. The original
`wave_start.pyw` (PyQt6) is UNCHANGED and still works - it is not a
fallback bolted on after the fact, it is simply the other, still-fully-
tested, still-fully-supported UI technology Wave now ships with. Both read
and write the exact same `wave_settings.json` (`settings/config.py`'s
`DATA_DIR`) and the exact same SQLite database, so switching which one you
launch does not lose or fork any data.
"""
