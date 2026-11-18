"""
avatar/rig.py — Tier 25: turns one photo into a small set of pre-warped
frames (blink, mouth-open, idle head-sway left/right) that avatar/window.py
cross-fades between on a timer - the "light" animation tier the user
chose over full photorealistic per-word lip-sync, which would need a
generative model and either a GPU or a per-reply rendering delay.

Face detection uses OpenCV's Haar cascade, bundled directly in this
package (avatar/data/haarcascade_frontalface_default.xml) rather than
read from cv2.data.haarcascades - that path turned out NOT to be
reliable across opencv-python versions (newer wheels dropped the bundled
cascade data entirely in favour of DNN-based detectors), so this module
ships its own copy instead of trusting whatever happens to be installed.
It's the same small, standard, BSD-licensed asset OpenCV itself has
shipped for over a decade (Rainer Lienhart's frontal-face cascade).

Eyes/mouth are placed at fixed proportions of the detected face box, not
from real per-point facial landmarks (a mediapipe-based landmark
detector was tried for this and dropped - see this repo's history - the
current mediapipe wheel for this Python version ships only its newer
Tasks API, which needs a separately-downloaded model asset; the
proportion-based placement below is simpler, has no extra download, and
is precise enough for the blink/mouth-open warps this module actually
does). If the eyes/mouth placement looks off on a real photo, nudging
the proportions in _REGION_PROPORTIONS is the fix - swapping in a real
landmark detector behind detect_regions() is a clean future upgrade,
not a rewrite, since every other function here only consumes the
{"left_eye", "right_eye", "mouth"} dict detect_regions() returns.
"""
import json
import logging
import os

import cv2
import numpy as np

logger = logging.getLogger("AvatarRig")

_CASCADE_PATH = os.path.join(os.path.dirname(__file__), "data", "haarcascade_frontalface_default.xml")

FRAME_NAMES = ("neutral", "blink", "mouth_open", "sway_left", "sway_right")

# Eyes/mouth boxes as a fraction of the detected face box's (x, y, w, h) -
# tuned against a generic frontal-face proportion guide, not any one
# photo. (left_x0, top_y0, left_x1, top_y1) per region, in [0, 1] of the
# face box.
_REGION_PROPORTIONS = {
    "left_eye": (0.16, 0.32, 0.42, 0.44),
    "right_eye": (0.58, 0.32, 0.84, 0.44),
    "mouth": (0.30, 0.62, 0.70, 0.80),
}


class RigBuildError(Exception):
    """Raised by build_rig() for a problem worth showing the user
    directly (no face found, unreadable file) - the one call site
    (Settings page / the setup skill) that needs a plain, friendly
    reason, the same convention automation/calendar_manager.py's tool
    methods already use for "I couldn't understand X"."""


_face_cascade = None


def _get_cascade():
    global _face_cascade
    if _face_cascade is None:
        cascade = cv2.CascadeClassifier(_CASCADE_PATH)
        if cascade.empty():
            raise RigBuildError(f"Couldn't load the face-detection model at '{_CASCADE_PATH}'.")
        _face_cascade = cascade
    return _face_cascade


def detect_regions(bgr_image):
    """{"left_eye": (x0,y0,x1,y1), "right_eye": (...), "mouth": (...)}
    in pixel coordinates of `bgr_image`, or None if no face was found at
    all. Picks the largest detected face if there's more than one (a
    photo with people in the background shouldn't rig the wrong face)."""
    gray = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2GRAY)
    faces = _get_cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    regions = {}
    for name, (fx0, fy0, fx1, fy1) in _REGION_PROPORTIONS.items():
        regions[name] = (x + w * fx0, y + h * fy0, x + w * fx1, y + h * fy1)
    return regions


def _warp_region_vertical(image, box, scale):
    """Scales `box`'s height by `scale`, resampled in place - closing an
    eye is squeezing its box toward 0 height (scale < 1); opening a
    mouth is stretching it (scale > 1). The resized strip is centered
    inside the original box and padding comes from repeating its own
    edge row, not a hard edge or a gap - cheap, and at the subtle scale
    factors this module actually uses, not noticeable."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.shape[1], x1), min(image.shape[0], y1)
    if x1 <= x0 or y1 <= y0:
        return image
    out = image.copy()
    region = image[y0:y1, x0:x1]
    h = region.shape[0]
    new_h = max(1, int(round(h * scale)))
    resized = cv2.resize(region, (region.shape[1], new_h))
    edge_row = region[-1:, :, :] if scale < 1 else region[:1, :, :]
    canvas = np.tile(edge_row, (h, 1, 1))
    top = max(0, (h - new_h) // 2)
    fit_h = min(new_h, h - top)
    canvas[top:top + fit_h] = resized[:fit_h]
    out[y0:y1, x0:x1] = canvas
    return out


def build_frames(bgr_image, regions: dict) -> dict:
    """The FRAME_NAMES set, as BGR numpy arrays, from one base photo +
    its already-detected eye/mouth regions. Deliberately simple
    per-region warps and a whole-image shift for the sway frames, not a
    generative model - see this module's own docstring for why."""
    blink = _warp_region_vertical(bgr_image, regions["left_eye"], 0.15)
    blink = _warp_region_vertical(blink, regions["right_eye"], 0.15)
    mouth_open = _warp_region_vertical(bgr_image, regions["mouth"], 1.3)
    h, w = bgr_image.shape[:2]
    shift = max(1.0, w * 0.01)
    sway_left = cv2.warpAffine(bgr_image, np.float32([[1, 0, -shift], [0, 1, 0]]), (w, h),
                                borderMode=cv2.BORDER_REPLICATE)
    sway_right = cv2.warpAffine(bgr_image, np.float32([[1, 0, shift], [0, 1, 0]]), (w, h),
                                 borderMode=cv2.BORDER_REPLICATE)
    return {
        "neutral": bgr_image,
        "blink": blink,
        "mouth_open": mouth_open,
        "sway_left": sway_left,
        "sway_right": sway_right,
    }


def build_rig(photo_path: str, out_dir: str) -> dict:
    """Reads `photo_path`, detects the eyes/mouth, writes FRAME_NAMES as
    PNGs under `out_dir`, and writes a small rig.json manifest there too
    (frame filenames + the source image's size, so window.py doesn't
    need to re-detect anything at render time - this is a one-time setup
    step, not something that runs every launch). Returns the manifest.

    Raises RigBuildError if the file can't be read or no face was found
    - plain, user-facing messages, not a raw cv2 exception, since this
    is called straight from the Settings page / a setup skill."""
    image = cv2.imread(photo_path)
    if image is None:
        raise RigBuildError(f"Couldn't read '{photo_path}' as an image.")
    regions = detect_regions(image)
    if regions is None:
        raise RigBuildError("Couldn't find a face in that photo - try a clearer, front-facing one.")
    frames = build_frames(image, regions)
    os.makedirs(out_dir, exist_ok=True)
    manifest = {"frames": {}, "width": image.shape[1], "height": image.shape[0]}
    for name, frame in frames.items():
        filename = f"{name}.png"
        cv2.imwrite(os.path.join(out_dir, filename), frame)
        manifest["frames"][name] = filename
    with open(os.path.join(out_dir, "rig.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info(f"Avatar rig built from '{photo_path}' -> '{out_dir}' ({len(frames)} frames).")
    return manifest


def load_manifest(out_dir: str):
    """The rig.json a previous build_rig() call wrote under `out_dir`,
    or None if this avatar hasn't been set up yet - window.py's own
    "nothing to show" check at startup."""
    path = os.path.join(out_dir, "rig.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
