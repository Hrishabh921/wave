"""
avatar/live.py — Tier 27: Wave's side of the lifelike avatar ("Dodo").

LivePortrait (avatar/live_render.py) needs CUDA PyTorch, which Wave's own
environment doesn't have, so nothing here imports it. This module:

  - says whether a lifelike render exists for the current avatar photo
    (load_manifest) and whether one can be made (renderer_problem),
  - runs avatar/live_render.py in the LivePortrait environment as a
    subprocess and reports its progress (LiveRenderJob),
  - turns one TTS utterance's audio into the 25 fps mouth-opening curve the
    Dodo page plays in step with the voice (speech_envelope),
  - serves the rendered files to the page (LiveAssetServer): 127.0.0.1
    only, behind a random token, read-only, and only the files the
    manifest names.

Setup lives in .env: AVATAR_LIVE_PYTHON (that environment's python.exe)
and AVATAR_LIVE_LP_DIR (the LivePortrait checkout with its
pretrained_weights). Left blank, the Dodo page keeps the simple 5-frame rig.
"""
import hmac
import json
import logging
import math
import os
import secrets
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from avatar import live_plan
from settings.config import config

logger = logging.getLogger("AvatarLive")

LIVE_DIRNAME = "live"
RENDERER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_render.py")
SOURCE_PHOTO = "neutral.png"         # avatar/rig.py's untouched copy of the photo
FPS = live_plan.FPS


def live_dir(rig_dir: str) -> str:
    return os.path.join(rig_dir, LIVE_DIRNAME)


def asset_names(manifest: dict) -> set:
    """Every file name the page may fetch for this render (and nothing with a path in it)."""
    names = {manifest.get("base") or ""}
    for clip in (manifest.get("clips") or {}).values():
        if isinstance(clip, dict):
            names.add(clip.get("file") or "")
    return {n for n in names if n and os.path.basename(n) == n and n not in (".", "..")}


def load_manifest(rig_dir: str):
    """The render for the CURRENT photo, or None - missing, unreadable,
    from another version, incomplete, or made from a photo that has since
    been replaced (Settings -> Avatar -> Build avatar)."""
    d = live_dir(rig_dir)
    try:
        with open(os.path.join(d, "manifest.json"), encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or m.get("version") != live_plan.VERSION:
        return None
    clips = m.get("clips")
    if not isinstance(clips, dict) or "idle" not in clips:
        return None
    if not all(os.path.isfile(os.path.join(d, n)) for n in asset_names(m)):
        return None
    try:
        if abs(os.path.getmtime(os.path.join(rig_dir, SOURCE_PHOTO)) - float(m.get("source_mtime", 0))) > 1.0:
            return None
    except (OSError, TypeError, ValueError):
        return None
    return m


def renderer_settings():
    py = (getattr(config, "AVATAR_LIVE_PYTHON", "") or "").strip().strip('"')
    lp = (getattr(config, "AVATAR_LIVE_LP_DIR", "") or "").strip().strip('"')
    return py, lp


def renderer_problem() -> str:
    """"" when a lifelike render can be made, otherwise why not (shown in Settings)."""
    py, lp = renderer_settings()
    if not py or not lp:
        return ("Lifelike animation isn't set up: point AVATAR_LIVE_PYTHON and AVATAR_LIVE_LP_DIR in .env "
                "at a LivePortrait environment (see .env.example).")
    if not os.path.isfile(py):
        return f"AVATAR_LIVE_PYTHON not found: {py}"
    if not os.path.isfile(os.path.join(lp, "src", "live_portrait_wrapper.py")):
        return f"AVATAR_LIVE_LP_DIR isn't a LivePortrait checkout: {lp}"
    if not os.path.isfile(os.path.join(lp, "pretrained_weights", "liveportrait", "base_models", "warping_module.pth")):
        return f"LivePortrait's models aren't downloaded into {os.path.join(lp, 'pretrained_weights')}"
    return ""


class LiveRenderJob(threading.Thread):
    """Runs avatar/live_render.py in the LivePortrait environment.

    on_progress(done, total, clip) as frames finish; on_done(ok, error)
    once, at the end. The renderer only swaps the new clips in when all of
    them are done, so cancel() at any point leaves the old ones intact."""

    def __init__(self, rig_dir: str, on_progress=None, on_done=None):
        super().__init__(daemon=True, name="AvatarLiveRender")
        self.rig_dir = rig_dir
        self.on_progress, self.on_done = on_progress, on_done
        self.done, self.total = 0, live_plan.total_frames()
        self.error = ""
        self.ok = False
        self.cancelled = False
        self._proc = None

    def command(self):
        py, lp = renderer_settings()
        return [py, "-u", RENDERER, "--lp-dir", lp, "--photo", os.path.join(self.rig_dir, SOURCE_PHOTO),
                "--out", live_dir(self.rig_dir)]

    def run(self):
        ok, err, tail = False, "", []
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8")
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            self._proc = subprocess.Popen(
                self.command(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for raw in self._proc.stdout:
                line = raw.strip()
                msg = None
                if line.startswith("{"):
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        msg = None
                if not isinstance(msg, dict):
                    if line:
                        tail = (tail + [line])[-20:]
                    continue
                event = msg.get("event")
                if event == "progress":
                    self.done = int(msg.get("done", self.done))
                    self.total = int(msg.get("total", self.total)) or self.total
                    self._call(self.on_progress, self.done, self.total, msg.get("clip", ""))
                elif event == "error":
                    err = str(msg.get("message") or "render failed")
                elif event == "done":
                    ok = True
            code = self._proc.wait()
            if code != 0:
                ok = False
                err = err or (tail[-1] if tail else f"renderer exited with code {code}")
        except Exception as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        if self.cancelled:
            ok, err = False, "cancelled"
        if not ok:
            logger.warning(f"Lifelike avatar render failed: {err}")
        self.ok, self.error = ok, err
        self._call(self.on_done, ok, err)

    def cancel(self):
        self.cancelled = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    @staticmethod
    def _call(fn, *args):
        if fn is None:
            return
        try:
            fn(*args)
        except Exception:
            logger.debug("live render callback failed", exc_info=True)


def speech_envelope(pcm, sample_rate: int, fps: int = FPS) -> list:
    """How open the mouth should be, 0..1, at each 1/fps of one utterance.

    Loudness in decibels relative to the utterance's own range (so a quiet
    and a loud voice both use the whole range), with a fast attack and a
    softer release like a real jaw, one frame early because the lips move
    a moment before the sound. The 1.15 power keeps small sounds subtle."""
    import numpy as np
    x = np.asarray(pcm)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x.astype(np.float64)
    if x.size and np.abs(x).max() > 1.5:
        x = x / 32768.0
    hop = max(1, int(round(sample_rate / fps)))
    n = int(math.ceil(len(x) / hop)) if len(x) else 0
    if n == 0:
        return []
    cs = np.concatenate([[0.0], np.cumsum(x * x)])
    centers = np.arange(n) * hop
    a = np.clip(centers - hop, 0, len(x))
    b = np.clip(centers + hop, 0, len(x))
    rms = np.sqrt((cs[b] - cs[a]) / np.maximum(1, b - a) + 1e-12)
    db = 20 * np.log10(rms + 1e-6)
    if db.max() < -60:                      # silence
        return [0.0] * n
    voiced = db > db.max() - 35
    lo, hi = np.percentile(db[voiced], 15), np.percentile(db[voiced], 97)
    loud = np.clip((db - lo) / max(1e-6, hi - lo), 0.0, 1.0)
    env = np.zeros(n)
    prev = 0.0
    for i, v in enumerate(loud):
        prev = prev + (v - prev) * (0.55 if v > prev else 0.32)
        env[i] = prev
    env = np.concatenate([env[1:], [0.0]]) ** 1.15
    return [round(float(v), 3) for v in env]


class LiveAssetServer:
    """Hands the rendered clips to the page. 127.0.0.1 only, on a random
    port, behind a random token in the path, GET only, and only the file
    names set_allowed() was given - nothing else in the folder (or outside
    it) can be reached. CORS is open because the page is served from
    pywebview's own server on another port; the token is what keeps other
    pages out."""

    def __init__(self, directory: str):
        self.directory = directory
        self.token = secrets.token_urlsafe(24)
        self._allowed = frozenset()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner._serve(self)

            def log_message(self, *a):
                pass

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 64        # Windows refuses connections once a small backlog is full

        self._httpd = Server(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True, name="AvatarLiveAssets").start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/{self.token}"

    def set_allowed(self, names):
        self._allowed = frozenset(names)

    _TYPES = {".jpg": "image/jpeg", ".json": "application/json", ".pack": "application/octet-stream"}

    def _serve(self, req):
        parts = urllib.parse.urlsplit(req.path).path.lstrip("/").split("/")
        name = parts[1] if len(parts) == 2 else ""
        if len(parts) != 2 or not hmac.compare_digest(parts[0], self.token) or name not in self._allowed:
            req.send_error(404)
            return
        try:
            with open(os.path.join(self.directory, name), "rb") as f:
                body = f.read()
        except OSError:
            req.send_error(404)
            return
        req.send_response(200)
        req.send_header("Content-Type", self._TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))
        req.send_header("Content-Length", str(len(body)))
        req.send_header("Access-Control-Allow-Origin", "*")
        req.send_header("Cache-Control", "no-cache")
        req.end_headers()
        req.wfile.write(body)

    def close(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
