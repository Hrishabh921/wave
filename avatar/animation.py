"""
avatar/animation.py — Tier 25: the avatar's frame-selection logic, kept
completely separate from avatar/window.py's QWidget so it's testable
without PyQt6 installed at all (this sandbox doesn't have it - see
tests/test_tier5_ui_logic.py's own REAL_QT check for the same situation
elsewhere in this project). avatar/window.py imports frame_for_state()
from here; nothing in this module ever imports Qt.
"""
from avatar.state import AvatarState

BLINK_MIN_INTERVAL_S = 2.5
BLINK_MAX_INTERVAL_S = 6.0
BLINK_DURATION_S = 0.12
IDLE_SWAY_AFTER_S = 3.0
SWAY_PERIOD_S = 6.0
THINK_SWAY_PERIOD_S = 1.4
SPEAK_FLAP_PERIOD_S = 0.22
FRAME_INTERVAL_MS = 66  # ~15fps - plenty for this animation style, light on CPU


def frame_for_state(state: AvatarState, seconds_in_state: float, now: float,
                     next_blink_at: float) -> str:
    """Which avatar/rig.py FRAME_NAMES entry belongs on screen right now:

      SPEAKING  - alternates "neutral"/"mouth_open" on a fixed cadence -
                  the "light" tier's mouth-flap, not amplitude-driven lip
                  sync (see avatar/state.py's own docstring for why).
      THINKING  - "sway_left"/"sway_right" on a faster cadence than idle
                  sway - a "pondering" tell.
      LISTENING - "neutral", held steady - no sway (reads as attentive,
                  not bored), but still blinks are NOT suppressed by this
                  function; LISTENING simply never checks next_blink_at,
                  which is an intentional, small difference from IDLE.
      IDLE      - "neutral", with an occasional "blink" and a slow sway
                  once settled in for IDLE_SWAY_AFTER_S seconds.

    `now`/`next_blink_at` are time.monotonic() seconds - the same clock
    AvatarStateMachine.entered_at already uses, so seconds_in_state and
    now are always directly comparable."""
    if state == AvatarState.SPEAKING:
        phase = int(seconds_in_state / SPEAK_FLAP_PERIOD_S) % 2
        return "mouth_open" if phase == 0 else "neutral"
    if state == AvatarState.THINKING:
        phase = (seconds_in_state % THINK_SWAY_PERIOD_S) / THINK_SWAY_PERIOD_S
        return "sway_left" if phase < 0.5 else "sway_right"
    if state == AvatarState.LISTENING:
        return "neutral"
    # IDLE
    if next_blink_at <= now < next_blink_at + BLINK_DURATION_S:
        return "blink"
    if seconds_in_state > IDLE_SWAY_AFTER_S:
        phase = (seconds_in_state % SWAY_PERIOD_S) / SWAY_PERIOD_S
        return "sway_left" if phase < 0.5 else "sway_right"
    return "neutral"
