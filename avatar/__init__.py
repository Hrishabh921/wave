"""
avatar/ — Tier 25: the avatar companion.

A new front-end "face" for Wave, built from the user's own photo and
voice. Doesn't touch brain/voice/automation at all - it only listens to
events those already fire (mic mode changes, reply start/speak/done) and
renders a small floating window reacting to them. See
docs/ROADMAP_BEAT_SARA.md's Tier 25 section for the full writeup.

- state.py    — IDLE/LISTENING/THINKING/SPEAKING state machine (Qt-free).
- rig.py      — one-time setup: turns a single photo into a small set of
                pre-warped frames (blink, mouth-open, idle-sway).
- window.py   — the floating, always-on-top PyQt6 widget that actually
                renders those frames on a timer.

Tier 27 - lifelike animation for the web UI's Dodo page:
- live_plan.py   — what the idle/listen/think/talk clips do, frame by
                   frame (pure math, no dependencies).
- live_render.py — renders those clips with LivePortrait; runs in its own
                   CUDA environment, never inside Wave.
- live.py        — Wave's side: render job, the mouth curve from TTS
                   audio, and the local file server the page loads from.
"""
