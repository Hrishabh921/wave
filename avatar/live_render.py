"""
avatar/live_render.py — Tier 27: renders Dodo's lifelike clips with LivePortrait.

Runs in the SEPARATE LivePortrait environment (CUDA PyTorch), never inside
Wave's own Python - avatar/live.py starts it as a subprocess:

    <AVATAR_LIVE_PYTHON> -u avatar/live_render.py --lp-dir <LivePortrait checkout>
                         --photo <avatar_rig/neutral.png> --out <avatar_rig/live>

LivePortrait (github.com/KwaiVGI/LivePortrait) re-draws the face from the
photo with real eyelids, a real mouth and natural head motion, but on a
laptop GPU it manages only a few frames a second - so instead of running
live, every clip avatar/live_plan.py describes is rendered once, here, and
the Dodo page just plays the frames back.

Output (inside --out):
  manifest.json   sizes, the face region, and where every frame sits
  base.jpg        the whole photo - the page draws it first
  <clip>.pack     that clip's frames back to back: the face region only, as
                  WebP with the paste-back mask as alpha, so drawing a frame
                  over base.jpg gives exactly LivePortrait's own paste-back

Writes into <out>.tmp and swaps it in only once every clip is done, so a
failed or cancelled render never breaks the animation already there.
Progress goes to stdout, one JSON object per line.
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys
import time


def emit(**kw):
    print(json.dumps(kw), flush=True)


def _load_plan():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_plan.py")
    spec = importlib.util.spec_from_file_location("wave_live_plan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Face:
    """LivePortrait loaded once, one photo prepared, then any frame of
    controls rendered into the face region."""

    def __init__(self, lp_dir, photo_path):
        sys.path.insert(0, lp_dir)
        import cv2
        import numpy as np
        from src.config.crop_config import CropConfig
        from src.config.inference_config import InferenceConfig
        from src.live_portrait_wrapper import LivePortraitWrapper
        from src.utils.camera import get_rotation_matrix
        from src.utils.crop import prepare_paste_back
        from src.utils.cropper import Cropper
        from src.utils.retargeting_utils import calc_eye_close_ratio
        self.cv2, self.np, self._rot = cv2, np, get_rotation_matrix
        inf, crop_cfg = InferenceConfig(), CropConfig()
        self.w = LivePortraitWrapper(inference_cfg=inf)
        cropper = Cropper(crop_cfg=crop_cfg, flag_force_cpu=True)   # face detection runs once - CPU is fine
        img = cv2.imread(photo_path, cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"Couldn't read the photo: {photo_path}")
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        self.img = img[: img.shape[0] - img.shape[0] % 2, : img.shape[1] - img.shape[1] % 2]
        crop = cropper.crop_source_image(self.img, crop_cfg)
        if crop is None:
            raise RuntimeError("No face found in the photo")
        I_s = self.w.prepare_source(crop["img_crop_256x256"])
        info = self.w.get_kp_info(I_s)
        self.R_s = get_rotation_matrix(info["pitch"], info["yaw"], info["roll"])
        self.f_s = self.w.extract_feature_3d(I_s)
        self.x_s = self.w.transform_keypoint(info)
        self.lmk = crop["lmk_crop"]
        self.src_eye = float(calc_eye_close_ratio(self.lmk[None]).mean())
        dev = self.w.device
        self.kp, self.scale = info["kp"].to(dev), info["scale"].to(dev)
        self.t = info["t"].clone().to(dev)
        self.t[..., 2] = 0
        self.exp0 = info["exp"].to(dev)
        self.pose0 = (info["pitch"], info["yaw"], info["roll"])
        self.eye_rest = self._eye_delta(self.src_eye)
        # where the face can change: the paste-back mask's bounding box
        H, W = self.img.shape[:2]
        mask = prepare_paste_back(inf.mask_crop, crop["M_c2o"], dsize=(W, H))[..., 0]
        ys, xs = np.nonzero(mask > 1.0 / 255)
        x0, y0 = max(0, int(xs.min()) - 2), max(0, int(ys.min()) - 2)
        x1, y1 = min(W, int(xs.max()) + 3), min(H, int(ys.max()) + 3)
        self.region = (x0, y0, x1 - x0, y1 - y0)
        self.alpha = np.clip(np.round(mask[y0:y1, x0:x1] * 255), 0, 255).astype(np.uint8)
        M = np.array(crop["M_c2o"], dtype=np.float64)[:2].copy()
        M[0, 2] -= x0
        M[1, 2] -= y0
        self.M_region = M

    def _eye_delta(self, ratio):
        import torch
        with torch.no_grad():
            return self.w.retarget_eye(self.x_s, self.w.calc_combined_eye_ratio([[float(ratio)]], self.lmk))

    def render_region(self, c, scale=1.0):
        """BGRA face region for one set of controls (see live_plan.py),
        resized by `scale`."""
        import torch
        cv2, np = self.cv2, self.np
        with torch.no_grad():
            d = self.exp0.clone()
            s = c.get("smile", 0.0)
            if s:
                d[0, 20, 1] += s * -0.01; d[0, 14, 1] += s * -0.02; d[0, 17, 1] += s * 0.0065; d[0, 17, 2] += s * 0.003
                d[0, 13, 1] += s * -0.00275; d[0, 16, 1] += s * -0.00275; d[0, 3, 1] += s * -0.0035; d[0, 7, 1] += s * -0.0035
            e = c.get("brow", 0.0)
            if e > 0:
                d[0, 1, 1] += e * 0.001; d[0, 2, 1] += e * -0.001
            elif e < 0:
                d[0, 1, 0] += e * -0.001; d[0, 2, 0] += e * 0.001; d[0, 1, 1] += e * 0.0003; d[0, 2, 1] += e * -0.0003
            x, y = c.get("gx", 0.0), c.get("gy", 0.0)
            if x or y:
                if x > 0:
                    d[0, 11, 0] += x * 0.0007; d[0, 15, 0] += x * 0.001
                else:
                    d[0, 11, 0] += x * 0.001; d[0, 15, 0] += x * 0.0007
                d[0, 11, 1] += y * -0.001; d[0, 15, 1] += y * -0.001
                b = -y / 2.0
                d[0, 11, 1] += b * -0.001; d[0, 13, 1] += b * 0.0003; d[0, 15, 1] += b * -0.001; d[0, 16, 1] += b * 0.0003
            v = c.get("purse", 0.0)
            if v:
                d[0, 14, 1] += v * 0.001; d[0, 3, 1] += v * -0.0005; d[0, 7, 1] += v * -0.0005; d[0, 17, 2] += v * -0.0005
            v = c.get("jaw", 0.0)
            if v:
                d[0, 19, 1] += v * 0.001; d[0, 19, 2] += v * 0.0001; d[0, 17, 1] += v * -0.0001
            p0, y0, r0 = self.pose0
            R_d = self._rot(p0 + c.get("pitch", 0.0), y0 + c.get("yaw", 0.0), r0 + c.get("roll", 0.0))
            R_new = (R_d @ self.R_s.permute(0, 2, 1)) @ self.R_s
            x_d = self.scale * (self.kp @ R_new + d) + self.t
            close = c.get("close", 0.0)
            if close > 1e-3:
                x_d = x_d + (self._eye_delta(self.src_eye * (1.0 - close)) - self.eye_rest)
            x_d = self.w.stitching(self.x_s, x_d)
            face = self.w.parse_output(self.w.warp_decode(self.f_s, self.x_s, x_d)["out"])[0]
        _, _, rw, rh = self.region
        warped = cv2.warpAffine(face, self.M_region, (rw, rh), flags=cv2.INTER_LINEAR)
        bgra = np.dstack([warped[..., ::-1], self.alpha])          # RGB -> BGR, + the mask as alpha
        if scale != 1.0:
            bgra = cv2.resize(bgra, scaled_size(rw, rh, scale), interpolation=cv2.INTER_AREA)
        return bgra


def scaled_size(w, h, scale):
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render Dodo's lifelike avatar clips with LivePortrait.")
    ap.add_argument("--lp-dir", required=True)
    ap.add_argument("--photo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--quality", type=int, default=88)
    # The Dodo page fills the window with the photo (2026-11-27: the owner
    # asked for it full-size, uncropped), so frames are stored at full size -
    # at the old 3/4 they'd be upscaled on a 1080p screen and look soft.
    ap.add_argument("--scale", type=float, default=1.0)
    a = ap.parse_args(argv)
    plan = _load_plan()
    started = time.time()
    try:
        import torch
        if not torch.cuda.is_available():
            emit(event="error", message="No CUDA GPU in the LivePortrait environment (is CUDA PyTorch installed?)")
            return 2
        emit(event="stage", stage="loading")
        face = _Face(a.lp_dir, a.photo)
    except Exception as e:
        emit(event="error", message=f"{type(e).__name__}: {e}")
        return 2

    cv2 = face.cv2
    out, tmp = os.path.abspath(a.out), os.path.abspath(a.out) + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    S = a.scale
    H, W = face.img.shape[:2]
    canvas = scaled_size(W, H, S)
    rx, ry, rw, rh = face.region
    region_scaled = [int(round(rx * S)), int(round(ry * S)), *scaled_size(rw, rh, S)]
    base = cv2.resize(cv2.cvtColor(face.img, cv2.COLOR_RGB2BGR), canvas, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", base, [cv2.IMWRITE_JPEG_QUALITY, 92])
    with open(os.path.join(tmp, "base.jpg"), "wb") as f:
        f.write(buf.tobytes())
    total, done = plan.total_frames(), 0
    clips = {}
    emit(event="progress", done=0, total=total, clip=plan.CLIP_ORDER[0])
    for clip in plan.CLIP_ORDER:
        n, n_levels = plan.frame_count(clip), plan.levels(clip)
        offsets, poses, pos = [], [], 0
        with open(os.path.join(tmp, clip + ".pack"), "wb") as pack:
            for lv in range(n_levels):
                for i in range(n):
                    c = plan.controls(clip, i, lv)
                    ok, enc = cv2.imencode(".webp", face.render_region(c, S), [cv2.IMWRITE_WEBP_QUALITY, a.quality])
                    if not ok:
                        emit(event="error", message="WebP encoding failed")
                        return 3
                    data = enc.tobytes()
                    pack.write(data)
                    offsets.append([pos, len(data)])
                    pos += len(data)
                    if lv == 0:
                        poses.append([round(c.get("yaw", 0.0), 3), round(c.get("pitch", 0.0), 3), round(c.get("roll", 0.0), 3)])
                    done += 1
                    if done % 5 == 0 or done == total:
                        emit(event="progress", done=done, total=total, clip=clip)
        clips[clip] = {"file": clip + ".pack", "frames": n, "levels": n_levels, "offsets": offsets, "poses": poses}
        if clip == "talk":
            clips[clip]["level_jaw"] = list(plan.TALK_LEVELS)
    manifest = {
        "version": plan.VERSION, "fps": plan.FPS, "photo": [W, H], "region": list(face.region),
        "scale": S, "canvas": list(canvas), "region_scaled": region_scaled,
        "base": "base.jpg", "format": "webp", "clips": clips,
        "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "render_seconds": round(time.time() - started, 1),
        "source": os.path.basename(a.photo), "source_mtime": os.path.getmtime(a.photo),
    }
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    old = out + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.isdir(out):
        os.replace(out, old)
    os.replace(tmp, out)
    shutil.rmtree(old, ignore_errors=True)
    emit(event="done", frames=total, seconds=manifest["render_seconds"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
