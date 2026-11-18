"""
tests/test_tier27_avatar_live.py — the lifelike avatar ("Dodo") plumbing.

LivePortrait itself needs a CUDA GPU and its own environment, so nothing
here renders a real frame. What is covered: the clip plans (they must loop
without a jump), the mouth curve made from TTS audio, which renders count
as current, the local file server's access rules, the render job's
progress/error reporting (with stand-in renderer scripts), and the bridge
and TTS wiring that carries it all to the page.

    python -m unittest discover -s tests -p "test_tier27_avatar_live.py"
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

import numpy as np  # noqa: E402

from avatar import live as avatar_live  # noqa: E402
from avatar import live_plan  # noqa: E402
from database.db_manager import db  # noqa: E402
from settings.config import config  # noqa: E402
from voice.tts import tts_engine  # noqa: E402
import webui.bridge as bridge_module  # noqa: E402
from webui.bridge import WaveWebBridge  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _fake_render(rig_dir, **overrides):
    """A finished render on disk for the photo in rig_dir (no real frames)."""
    _write(os.path.join(rig_dir, "neutral.png"))
    d = avatar_live.live_dir(rig_dir)
    for name in ("base.jpg", "idle.pack", "talk.pack"):
        _write(os.path.join(d, name), b"bytes of " + name.encode())
    m = {"version": live_plan.VERSION, "base": "base.jpg", "fps": 25,
         "clips": {"idle": {"file": "idle.pack"}, "talk": {"file": "talk.pack"}},
         "source_mtime": os.path.getmtime(os.path.join(rig_dir, "neutral.png"))}
    m.update(overrides)
    with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    return m


def _no_proxy_get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        r = opener.open(url, timeout=5)
        return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, b"", e.headers


# ---------------------------------------------------------------------------
class PlanTests(unittest.TestCase):
    def test_every_clip_loops_without_a_jump(self):
        for clip in live_plan.CLIP_ORDER:
            n = live_plan.frame_count(clip)
            for lv in range(live_plan.levels(clip)):
                first, wrapped = live_plan.controls(clip, 0, lv), live_plan.controls(clip, n, lv)
                for key in first:
                    self.assertAlmostEqual(first[key], wrapped[key], places=6, msg=f"{clip} level {lv}: {key}")

    def test_blinks_and_glances_finish_inside_their_clip(self):
        for clip, plan in live_plan._PLANS.items():
            T = live_plan.CLIP_SECONDS[clip]
            for b in plan["blinks"]:
                self.assertLess(b + live_plan.BLINK_S, T, clip)
            first, last = plan["gaze"][0], plan["gaze"][-1]
            self.assertEqual(first[1:], last[1:], f"{clip}: eyes must end where they start")
            self.assertLess(last[0] + live_plan.SACCADE_S, T, clip)

    def test_a_blink_closes_the_eyes_fully_and_then_reopens_them(self):
        b = 1.0
        self.assertEqual(live_plan.blink_closure(b - 0.01, [b]), 0.0)
        self.assertAlmostEqual(live_plan.blink_closure(b + 0.09, [b]), 1.0)
        self.assertEqual(live_plan.blink_closure(b + live_plan.BLINK_S + 0.01, [b]), 0.0)

    def test_talk_is_rendered_once_per_mouth_opening(self):
        jaws = [live_plan.controls("talk", 10, lv)["jaw"] for lv in range(live_plan.levels("talk"))]
        self.assertEqual(jaws, list(live_plan.TALK_LEVELS))
        self.assertEqual(jaws, sorted(jaws))
        self.assertLessEqual(max(jaws), 60, "past ~60 the mouth looks like shouting")
        for clip in ("idle", "listen", "think"):
            self.assertEqual(live_plan.levels(clip), 1)
            self.assertEqual(live_plan.controls(clip, 10)["jaw"], 0.0)

    def test_total_frames(self):
        self.assertEqual(live_plan.total_frames(), 240 + 120 + 120 + 80 * len(live_plan.TALK_LEVELS))


# ---------------------------------------------------------------------------
class SpeechEnvelopeTests(unittest.TestCase):
    SR = 24000

    def test_silence_keeps_the_mouth_shut(self):
        env = avatar_live.speech_envelope(np.zeros(self.SR, dtype=np.int16), self.SR)
        self.assertEqual(len(env), 25)
        self.assertEqual(max(env), 0.0)

    def test_the_mouth_opens_on_sound_and_closes_in_the_gaps(self):
        t = np.arange(int(self.SR * 1.2)) / self.SR
        pcm = (np.sin(2 * np.pi * 220 * t) * 12000 * ((t % 0.4) < 0.2)).astype(np.int16)   # 200 ms on, 200 ms off
        env = avatar_live.speech_envelope(pcm, self.SR)
        self.assertEqual(len(env), 30)
        self.assertTrue(all(0.0 <= v <= 1.0 for v in env))
        self.assertGreater(min(env[i] for i in (2, 3, 12, 13, 22, 23)), 0.5)
        self.assertLess(max(env[i] for i in (8, 18, 28)), 0.3)

    def test_float_and_stereo_audio_work_too(self):
        x = np.random.RandomState(1).uniform(-0.5, 0.5, (self.SR // 2, 2)).astype(np.float32)
        self.assertEqual(len(avatar_live.speech_envelope(x, self.SR)), 13)
        self.assertEqual(avatar_live.speech_envelope(np.zeros(0), self.SR), [])


# ---------------------------------------------------------------------------
class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.rig = S.make_tmpdir(self)

    def test_no_render_yet(self):
        self.assertIsNone(avatar_live.load_manifest(self.rig))

    def test_a_complete_render_of_this_photo_loads(self):
        _fake_render(self.rig)
        m = avatar_live.load_manifest(self.rig)
        self.assertIsNotNone(m)
        self.assertEqual(avatar_live.asset_names(m), {"base.jpg", "idle.pack", "talk.pack"})

    def test_a_render_of_an_older_photo_is_ignored(self):
        _fake_render(self.rig, source_mtime=1.0)
        self.assertIsNone(avatar_live.load_manifest(self.rig))

    def test_a_missing_pack_or_another_version_is_ignored(self):
        _fake_render(self.rig)
        os.remove(os.path.join(avatar_live.live_dir(self.rig), "talk.pack"))
        self.assertIsNone(avatar_live.load_manifest(self.rig))
        _fake_render(self.rig, version=live_plan.VERSION + 1)
        self.assertIsNone(avatar_live.load_manifest(self.rig))

    def test_file_names_with_a_path_are_never_served(self):
        names = avatar_live.asset_names({"base": "../secret.txt", "clips": {"a": {"file": "x/b.pack"}, "b": {"file": "ok.pack"}}})
        self.assertEqual(names, {"ok.pack"})


# ---------------------------------------------------------------------------
class RendererSetupTests(unittest.TestCase):
    def _with(self, py, lp):
        return mock.patch.multiple(config, AVATAR_LIVE_PYTHON=py, AVATAR_LIVE_LP_DIR=lp)

    def test_blank_settings_say_what_to_set(self):
        with self._with("", ""):
            self.assertIn("AVATAR_LIVE_PYTHON", avatar_live.renderer_problem())

    def test_a_livePortrait_checkout_needs_its_models_too(self):
        tmp = S.make_tmpdir(self)
        py, lp = os.path.join(tmp, "python.exe"), os.path.join(tmp, "LivePortrait")
        _write(py)
        with self._with(py, lp):
            self.assertIn("isn't a LivePortrait checkout", avatar_live.renderer_problem())
        _write(os.path.join(lp, "src", "live_portrait_wrapper.py"))
        with self._with(py, lp):
            self.assertIn("models", avatar_live.renderer_problem())
        _write(os.path.join(lp, "pretrained_weights", "liveportrait", "base_models", "warping_module.pth"))
        with self._with(py, lp):
            self.assertEqual(avatar_live.renderer_problem(), "")


# ---------------------------------------------------------------------------
class RenderJobTests(unittest.TestCase):
    def _run(self, command):
        progress, done = [], []
        job = avatar_live.LiveRenderJob(S.make_tmpdir(self), on_progress=lambda d, t, c: progress.append((d, t, c)),
                                        on_done=lambda ok, err: done.append((ok, err)))
        job.command = lambda: command
        job.start()
        job.join(90)
        self.assertFalse(job.is_alive())
        return progress, done, job

    def _script(self, body):
        path = os.path.join(S.make_tmpdir(self), "fake_render.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return [sys.executable, "-u", path]

    def test_progress_and_success_are_reported(self):
        progress, done, job = self._run(self._script(
            "import json\n"
            "for d in (5, 10):\n"
            "    print(json.dumps({'event': 'progress', 'done': d, 'total': 10, 'clip': 'idle'}), flush=True)\n"
            "print('some library log line')\n"
            "print(json.dumps({'event': 'done'}))\n"))
        self.assertEqual(progress, [(5, 10, "idle"), (10, 10, "idle")])
        self.assertEqual(done, [(True, "")])
        self.assertTrue(job.ok)

    def test_a_failure_carries_the_renderers_reason(self):
        _, done, job = self._run(self._script(
            "import json, sys\n"
            "print(json.dumps({'event': 'error', 'message': 'No face found in the photo'}))\n"
            "sys.exit(2)\n"))
        self.assertEqual(done, [(False, "No face found in the photo")])
        self.assertFalse(job.ok)

    def test_the_real_renderer_refuses_cleanly_without_a_gpu_environment(self):
        # Wave's own environment has no CUDA PyTorch: the renderer must say
        # so and exit - never hang, never take Wave down with it.
        tmp = S.make_tmpdir(self)
        _, done, job = self._run([sys.executable, "-u", avatar_live.RENDERER, "--lp-dir", tmp,
                                  "--photo", os.path.join(tmp, "missing.png"), "--out", os.path.join(tmp, "live")])
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0][0])
        self.assertTrue(done[0][1])
        self.assertFalse(os.path.exists(os.path.join(tmp, "live")))


# ---------------------------------------------------------------------------
class AssetServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = S.make_tmpdir(self)
        _write(os.path.join(self.dir, "idle.pack"), b"frames")
        _write(os.path.join(self.dir, "secret.txt"), b"private")
        self.srv = avatar_live.LiveAssetServer(self.dir)
        self.addCleanup(self.srv.close)
        self.srv.set_allowed({"idle.pack"})

    def test_listed_files_are_served_behind_the_token(self):
        self.assertTrue(self.srv.base_url.startswith("http://127.0.0.1:"))
        code, body, headers = _no_proxy_get(self.srv.base_url + "/idle.pack")
        self.assertEqual((code, body), (200, b"frames"))
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_everything_else_is_refused(self):
        port = self.srv.port
        for url in (f"http://127.0.0.1:{port}/wrong-token/idle.pack",
                    f"http://127.0.0.1:{port}/idle.pack",
                    self.srv.base_url + "/secret.txt",
                    self.srv.base_url + "/../secret.txt",
                    self.srv.base_url + "/%2e%2e/secret.txt",
                    self.srv.base_url + "/"):
            self.assertEqual(_no_proxy_get(url)[0], 404, url)


# ---------------------------------------------------------------------------
class _FakeBrain:
    pending_action = None

    def think_stream(self, text, attachment=None, cancel_event=None):
        yield "ok"


class _FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events_named(self, name):
        return [c for c in self.calls if f'"{name}"' in c]


class BridgeTests(unittest.TestCase):
    def setUp(self):
        tmp = S.make_tmpdir(self)
        old_db = db.db_path
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", old_db)
        old_settings = bridge_module.SETTINGS_FILE
        bridge_module.SETTINGS_FILE = os.path.join(tmp, "wave_settings.json")
        self.addCleanup(setattr, bridge_module, "SETTINGS_FILE", old_settings)
        self.rig = os.path.join(tmp, "avatar_rig")
        os.makedirs(self.rig)
        old_rig = bridge_module.AVATAR_RIG_DIR
        bridge_module.AVATAR_RIG_DIR = self.rig
        self.addCleanup(setattr, bridge_module, "AVATAR_RIG_DIR", old_rig)
        old_hook = tts_engine.on_playback_pcm
        self.addCleanup(setattr, tts_engine, "on_playback_pcm", old_hook)
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(self._stop)
        self.window = _FakeWindow()
        self.bridge.set_window(self.window)

    def _stop(self):
        self.bridge._stop_live()
        self.bridge._reminder_worker.stop()
        self.bridge._reminder_worker.join(timeout=2)

    def test_without_a_render_the_page_hears_whether_one_can_be_made(self):
        with mock.patch.multiple(config, AVATAR_LIVE_PYTHON="", AVATAR_LIVE_LP_DIR=""):
            r = self.bridge.get_avatar_live()
            self.assertTrue(r["ok"])
            self.assertFalse(r["available"])
            self.assertFalse(r["configured"])
            self.assertIn("AVATAR_LIVE_PYTHON", r["problem"])
            started = self.bridge.render_avatar_live()
        self.assertFalse(started["ok"])

    def test_render_needs_an_avatar_photo_first(self):
        with mock.patch.object(avatar_live, "renderer_problem", return_value=""):
            r = self.bridge.render_avatar_live()
        self.assertFalse(r["ok"])
        self.assertIn("Build avatar", r["error"])

    def test_a_finished_render_is_served_to_the_page(self):
        _fake_render(self.rig)
        r = self.bridge.get_avatar_live()
        self.assertTrue(r["available"])
        self.assertEqual(r["manifest"]["base"], "base.jpg")
        code, body, _ = _no_proxy_get(r["base_url"] + "/talk.pack")
        self.assertEqual((code, body), (200, b"bytes of talk.pack"))
        self.assertEqual(_no_proxy_get(r["base_url"] + "/manifest.json")[0], 404)   # only what the page needs

    def test_window_wiring_hands_tts_audio_to_the_page(self):
        self.assertEqual(tts_engine.on_playback_pcm, self.bridge._on_tts_pcm)
        t = np.arange(12000) / 24000
        self.bridge._on_tts_pcm((np.sin(2 * np.pi * 200 * t) * 9000).astype(np.int16), 24000)
        deadline = time.time() + 3
        while not self.window.events_named("avatar_speech") and time.time() < deadline:
            time.sleep(0.02)
        pushed = self.window.events_named("avatar_speech")
        self.assertEqual(len(pushed), 1)
        self.assertIn('"env"', pushed[0])
        self.bridge.set_window(None)
        self.assertIsNone(tts_engine.on_playback_pcm)

    def test_the_tts_engine_offers_each_utterance_before_playing_it(self):
        got = []
        tts_engine.on_playback_pcm = lambda pcm, sr: got.append((len(pcm), sr))
        pcm = np.zeros(2400, dtype=np.int16)
        with mock.patch.object(tts_engine, "_persistent_output", return_value=None), \
                mock.patch.object(tts_engine, "_play_pcm_via_sounddevice", return_value=True), \
                mock.patch.object(tts_engine, "_play_pcm_via_pygame_array", return_value=True), \
                mock.patch.object(tts_engine.stop_event, "is_set", return_value=False):
            self.assertTrue(tts_engine._play_pcm(pcm, 24000))
        self.assertEqual(got, [(2400, 24000)])


# ---------------------------------------------------------------------------
class PageTests(unittest.TestCase):
    def _read(self, *parts):
        with open(os.path.join(REPO, "webui", "static", *parts), encoding="utf-8") as f:
            return f.read()

    def test_the_dodo_page_plays_the_lifelike_clips_with_the_voice(self):
        js = self._read("js", "avatar.js")
        for needle in ('wave.call("get_avatar_live")', 'wave.on("avatar_speech"', "createLivePlayer", "bestStart",
                       'wave.call("get_avatar_rig")'):
            self.assertIn(needle, js)

    def test_settings_can_start_a_render(self):
        self.assertIn('call("render_avatar_live")', self._read("js", "pages2.js"))


if __name__ == "__main__":
    unittest.main()
