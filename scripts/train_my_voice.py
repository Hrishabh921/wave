"""
scripts/train_my_voice.py — Tier 32b: teach Wave your voice through THIS PC's mic.

Wave's speaker check (voice/speaker_id.py) starts from the voice sample you gave
for the cloned voice - usually a phone recording, which sounds different from how
this PC's mic hears you (in the first live test the owner's lines scored 0.19-0.24
against it, under the 0.28 threshold). This records you reading a few lines through
the same mic Wave listens with, and adds that to the voiceprint.

    python scripts/train_my_voice.py              # you (30 s), then a video (20 s)
    python scripts/train_my_voice.py --no-video   # just you
    python scripts/train_my_voice.py --reset      # forget the training and what was learned

Beeps: one = start reading, two = stop talking and play a video (it measures how
other voices score through this mic, to place the threshold), three = done.
Close Wave first - it would be answering you while you read.
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

from monitoring.power import keep_full_speed  # noqa: E402
from voice import speaker_id  # noqa: E402

RATE = speaker_id.SAMPLE_RATE
LINES = (
    "Baby, abhi time kya hua hai? Aur aaj ka mausam kaisa rahega?",
    "Mujhe kal subah saat baje yaad dilana ki doctor ke paas jana hai.",
    "Ek achha sa gaana chalao, aur volume thoda kam kar do.",
    "Aaj ki news batao, phir mere reminders bhi suna dena.",
    "Wave, ab se sirf meri awaaz pehchaan kar meri baat sunna.",
)


def beep(times: int = 1):
    try:
        import sounddevice as sd  # noqa: PLC0415
        t = np.arange(int(0.18 * RATE)) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
        gap = np.zeros(int(0.12 * RATE), np.float32)
        sd.play(np.concatenate([np.concatenate([tone, gap]) for _ in range(times)]), RATE, blocking=True)
    except Exception:
        pass


def record(seconds: float):
    """`seconds` of the default mic, the way Wave's capture opens it (16 kHz, int16, mono)."""
    import sounddevice as sd  # noqa: PLC0415
    frames = []
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=RATE // 50,
                        callback=lambda data, n, t, status: frames.append(data.copy())):
        time.sleep(seconds)
    if not frames:
        return np.zeros(0, np.float32)
    return np.concatenate(frames)[:, 0].astype(np.float32) / 32768.0


def speech_only(x):
    """The voiced parts of `x` joined together (30 ms frames above the room's noise floor)."""
    frame = int(0.03 * RATE)
    n = len(x) // frame
    if n == 0:
        return np.zeros(0, np.float32), 0.0
    frames = x[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    floor = float(np.percentile(rms, 20))
    keep = rms > max(3.0 * floor, 0.004)
    # 150 ms of hangover either side so words aren't clipped
    hang = np.convolve(keep.astype(int), np.ones(11, int), "same") > 0
    voiced = frames[hang].reshape(-1)
    return voiced, float(rms.max())


def pieces(x, seconds: float):
    n = int(seconds * RATE)
    return [x[i:i + n] for i in range(0, len(x) - n + 1, n)]


def unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (float(np.linalg.norm(v)) + 1e-9)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Teach Wave your voice through this PC's mic.")
    parser.add_argument("--seconds", type=float, default=30.0, help="how long you read (default 30)")
    parser.add_argument("--video-seconds", type=float, default=20.0, help="how long the video plays (default 20)")
    parser.add_argument("--no-video", action="store_true", help="skip the video part")
    parser.add_argument("--reset", action="store_true", help="forget the voice training and what was learned")
    args = parser.parse_args(argv)
    verifier = speaker_id.speaker_verifier

    if args.reset:
        path = verifier.voiceprint_path()
        if os.path.exists(path):
            os.remove(path)
        verifier.reset()
        print("Voice training forgotten (the voiceprint is rebuilt from the voice sample on next use).")
        return 0

    keep_full_speed()
    if not verifier._ready_model():
        print("The speaker model isn't installed - run: python scripts/download_speaker_model.py")
        return 1
    print("Read these lines out loud, at your normal voice, TV and videos OFF:")
    for line in LINES:
        print("   ", line)
    beep(1)
    print(f"[recording you for {args.seconds:.0f}s]", flush=True)
    you = record(args.seconds)
    video = None
    if not args.no_video:
        beep(2)
        print(f"[stop talking - play a video now; recording it for {args.video_seconds:.0f}s]", flush=True)
        video = record(args.video_seconds)
    beep(3)

    voiced, peak = speech_only(you)
    print(f"\nyou: {len(you) / RATE:.0f}s recorded, {len(voiced) / RATE:.1f}s of speech (peak {peak:.2f})")
    if len(voiced) < 12 * RATE:
        print("Not enough speech heard (need ~12 s) - nothing saved. Speak a little louder or closer, and try again.")
        return 2
    embs = verifier.window_embeddings(voiced)
    mean_all = unit(np.mean(embs, axis=0))
    loo = [float(e @ unit(np.mean([o for j, o in enumerate(embs) if j != i], axis=0))) for i, e in enumerate(embs)]
    kept = [e for e, s in zip(embs, loo) if s >= 0.45]
    print(f"pieces: {len(embs)}, agreeing with the rest: {len(kept)} (min {min(loo):.2f}, mean {np.mean(loo):.2f})")
    if len(kept) < max(4, 2 * len(embs) // 3):
        print("The recording doesn't sound like one voice (too much noise, or someone/something else talking) "
              "- nothing saved. Try again with the room quiet.")
        return 3
    before = verifier.sample_scores(kept)
    if before is not None:
        print(f"your mic voice vs the voice sample alone: mean {np.mean(before):.2f}, min {min(before):.2f} "
              f"(threshold {verifier.threshold():.2f})")

    # How the owner's own short commands would score: train on the first two
    # thirds of the speech, score 1.5 s pieces of the last third.
    cut = int(len(voiced) * 2 / 3)
    train_part = unit(np.mean(verifier.window_embeddings(voiced[:cut]), axis=0))
    targets = list(verifier._centroids) if verifier._centroids is not None else []
    own_scores = [max(float(e @ t) for t in targets + [train_part])
                  for e in (verifier.window_embeddings(p, seconds=1.5, hop=1.5)[0] for p in pieces(voiced[cut:], 1.5))]
    if own_scores:
        print(f"your 1.5 s pieces (held out) would score: min {min(own_scores):.2f}, "
              f"p10 {np.percentile(own_scores, 10):.2f}, mean {np.mean(own_scores):.2f}")

    verifier.set_training(kept)
    verifier.reset()                     # reload with the training included
    verifier.ready()
    print(f"saved: {len(kept)} pieces of your voice through this mic -> {verifier.voiceprint_path()}")

    if video is not None:
        v_voiced, v_peak = speech_only(video)
        others = []
        for p in pieces(v_voiced, 1.5):
            check = verifier.check((np.clip(p, -1, 1) * 32767).astype("<i2").tobytes())
            if check is not None:
                others.append(check.score)
        print(f"\nvideo: {len(v_voiced) / RATE:.1f}s of speech (peak {v_peak:.2f}), {len(others)} pieces")
        if others:
            print(f"other voices now score: max {max(others):.2f}, p95 {np.percentile(others, 95):.2f}, "
                  f"mean {np.mean(others):.2f}")
            if own_scores:
                low_you, high_them = float(np.percentile(own_scores, 10)), float(max(others))
                suggestion = round(min(0.6, max(0.2, (low_you + high_them) / 2)), 2)
                gap = "a clear gap" if low_you > high_them else "an OVERLAP"
                print(f"suggested SPEAKER_VERIFY_THRESHOLD: {suggestion:.2f} ({gap}: you p10 {low_you:.2f} "
                      f"vs other voices max {high_them:.2f})")
        else:
            print("no speech heard from the video - threshold left as it is")
    return 0


if __name__ == "__main__":
    sys.exit(main())
