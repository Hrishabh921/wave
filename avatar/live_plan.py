"""
avatar/live_plan.py — Tier 27: what Dodo's lifelike clips do, frame by frame.

Pure math, standard library only. avatar/live_render.py (which runs in the
separate LivePortrait environment) turns these numbers into images, and
tests/test_tier27_avatar_live.py checks them without a GPU.

Four clips, matching avatar/state.py's four states:

  idle    breathing, slow head drift, blinks (one double), glances around,
          a smile that comes and goes
  listen  attentive: brows up a touch, small nods, eyes on you
  think   glances up and aside while a reply is being worked out
  talk    head moves with the speech; rendered once per mouth opening in
          TALK_LEVELS, so the page can pick the opening from the real voice

Every clip loops: all head motion is a sum of sines that complete whole
cycles over the clip, blinks and eye movements finish inside it, and the
eyes end where they started - frame N-1 flows straight into frame 0.

Control names are the ones avatar/live_render.py understands:
  yaw/pitch/roll   head turn, degrees (0 = as in the photo)
  smile, brow      LivePortrait's own "smile" / "eyebrow" sliders
  gx, gy           eye gaze (LivePortrait's "eye gaze" sliders)
  close            how closed the eyes are, 0..1 (a blink)
  jaw              "lip close <-> open" (0 = closed, 100 = wide open)
  purse            "pursing" (negative = lips pressed/rounded)
"""
import math

FPS = 25
VERSION = 1

# Mouth openings rendered for the talk clip. Speech rarely needs more than
# half of LivePortrait's range - past ~50 it looks like shouting or singing.
TALK_LEVELS = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0)

# seconds per clip (whole frames at FPS)
CLIP_SECONDS = {"idle": 9.6, "listen": 4.8, "think": 4.8, "talk": 3.2}
CLIP_ORDER = ("idle", "talk", "listen", "think")      # render/load order: most-used first

BLINK_CLOSE_S, BLINK_HOLD_S, BLINK_OPEN_S = 0.075, 0.035, 0.16
BLINK_S = BLINK_CLOSE_S + BLINK_HOLD_S + BLINK_OPEN_S
SACCADE_S = 0.07

_PLANS = {
    "idle": {
        "blinks": (1.3, 4.6, 4.92, 7.9),
        "gaze": ((0.0, 0.0, 0.0), (0.9, -5.0, 1.0), (2.6, 3.0, -1.5), (4.0, 0.0, 0.0),
                 (5.8, 6.0, 0.5), (7.2, -2.0, -1.0), (8.8, 0.0, 0.0)),
    },
    "listen": {
        "blinks": (2.2,),
        "gaze": ((0.0, 0.0, 0.0), (1.6, 1.5, 0.5), (3.4, 0.0, 0.0)),
    },
    "think": {
        "blinks": (1.2, 3.3),
        "gaze": ((0.0, 0.0, 0.0), (0.15, -7.0, 5.0), (2.2, -4.0, 6.0), (4.4, 0.0, 0.0)),
    },
    "talk": {
        "blinks": (1.75,),
        "gaze": ((0.0, 0.0, 0.0), (1.4, 1.5, 0.3), (2.6, 0.0, 0.0)),
    },
}


def frame_count(clip: str) -> int:
    return int(round(CLIP_SECONDS[clip] * FPS))


def _ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def blink_closure(t: float, blinks) -> float:
    """0 = eyes open, 1 = shut. Lids close fast, hold a moment, open a bit
    slower - the shape of a real blink."""
    c = 0.0
    for b in blinks:
        d = t - b
        if 0 <= d < BLINK_CLOSE_S:
            c = max(c, _ease(d / BLINK_CLOSE_S))
        elif BLINK_CLOSE_S <= d < BLINK_CLOSE_S + BLINK_HOLD_S:
            c = 1.0
        elif BLINK_CLOSE_S + BLINK_HOLD_S <= d < BLINK_S:
            c = max(c, 1.0 - _ease((d - BLINK_CLOSE_S - BLINK_HOLD_S) / BLINK_OPEN_S))
    return c


def gaze_at(t: float, path):
    """Eyes hold still, then jump quickly (a saccade) to the next target."""
    x, y = path[0][1], path[0][2]
    for ts, gx, gy in path[1:]:
        if t < ts:
            break
        k = _ease((t - ts) / SACCADE_S)
        x, y = x + (gx - x) * k, y + (gy - y) * k
    return x, y


def controls(clip: str, i: int, level: int = 0) -> dict:
    """The face controls for frame `i` of `clip` (and mouth `level` for talk)."""
    T = CLIP_SECONDS[clip]
    t = i / FPS
    w = 2 * math.pi / T
    plan = _PLANS[clip]
    gx, gy = gaze_at(t, plan["gaze"])
    c = {"gx": gx, "gy": gy, "close": blink_closure(t, plan["blinks"]), "jaw": 0.0, "purse": 0.0}
    if clip == "idle":
        c.update(yaw=1.8 * math.sin(w * t + 0.3) + 0.7 * math.sin(3 * w * t + 1.7),
                 pitch=0.9 * math.sin(w * t + 2.1) + 0.45 * math.sin(2 * w * t),
                 roll=0.7 * math.sin(w * t + 0.9),
                 smile=0.15 + 0.12 * (1 - math.cos(w * t)), brow=0.0)
    elif clip == "listen":
        c.update(yaw=0.7 * math.sin(w * t + 0.4), pitch=0.8 * math.sin(2 * w * t), roll=0.5 * math.sin(w * t + 1.2),
                 smile=0.25, brow=4.0)
    elif clip == "think":
        c.update(yaw=-0.8 + 0.5 * math.sin(w * t), pitch=0.6 + 0.3 * math.sin(2 * w * t), roll=-0.6 * math.sin(w * t + 0.5),
                 smile=0.05, brow=3.0, purse=-2.0)
    elif clip == "talk":
        c.update(yaw=1.0 * math.sin(w * t + 0.5) + 0.4 * math.sin(3 * w * t + 1.1),
                 pitch=0.8 * math.sin(2 * w * t + 0.7), roll=0.5 * math.sin(w * t + 2.0),
                 smile=0.04, brow=1.5 + 1.5 * math.sin(2 * w * t + 0.3), jaw=TALK_LEVELS[level])
    else:
        raise KeyError(clip)
    return c


def levels(clip: str) -> int:
    return len(TALK_LEVELS) if clip == "talk" else 1


def total_frames() -> int:
    return sum(frame_count(c) * levels(c) for c in CLIP_ORDER)
