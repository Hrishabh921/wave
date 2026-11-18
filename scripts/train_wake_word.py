#!/usr/bin/env python3
"""
scripts/train_wake_word.py — train Wave's "Hey Wave" wake-word model
=======================================================================

Produces voice_models/wakewords/hey_wave.onnx and its model card
hey_wave.json - the model voice/wakeword.py loads through openWakeWord.
Wave itself needs none of this script's dependencies; they're only for
(re)training.

The recipe is openWakeWord's (synthetic speech + its frozen audio-embedding
front end + a small classifier), rebuilt so it runs on a laptop CPU in
about an hour without openWakeWord's multi-gigabyte training downloads:

  * Positives are 100% synthetic, like openWakeWord's own models: Piper's
    904-voice LibriTTS model (dataset CC BY 4.0) plus every espeak-ng
    English voice x variant, and espeak-ng's Hindi voice saying "हे वेव"
    for an Indian-accented "hey wave". Spellings like "hey vave" cover the
    w/v merger common in Indian English.
  * Negatives: public-domain book text read by the same kinds of voices
    (plus --more-speech extra Piper sentences: natural voices are what a
    TV sounds like), Hinglish and Hindi sentences, phrases that sound close
    ("hey wait", "hey dave", "heat wave", "microwave", "hey Jarvis"...),
    spliced negatives ("hey" + something else, something else + "wave",
    word-length pieces of different speakers), and procedurally generated
    noise, mains hum, fan noise, babble, music and clicks.
  * Every clip gets random speed, reverb, EQ, background noise and gain -
    rooms, microphones and loudness differ on real PCs.
  * Features are openWakeWord's own (melspectrogram -> Google's
    speech_embedding, 16 frames x 96 values = the last ~1.3 s), so the
    exported model drops straight into openwakeword.Model. The classifier
    is a small MLP trained here in numpy, with hard-negative mining, and
    written out as ONNX. Model selection uses a validation split of the
    TRAINING voices and books (recall at --val-max-fa false accepts per
    hour on a validation stream); the held-out set is used once, at the end.
  * Evaluation runs the exported file through openwakeword.Model itself,
    80 ms frame by frame, exactly like voice/wakeword.py: false rejects
    on voices and spellings never seen in training, false accepts per hour
    on a long stream of held-out voices reading held-out books with noise
    and music, and how often held-out sound-alike phrases are accepted -
    each with and without 2-frame smoothing (the score must hold for two
    consecutive frames).
  * The card records the operating point picked from those numbers:
    "standalone" (<= --max-fa-per-hour with clean FRR <= --max-frr-clean)
    or "prefilter" (clean FRR <= --prefilter-frr, and confirm_below 1.0 so
    voice/wakeword.py transcript-checks every detection).

Honest limits: every number this prints is measured on synthetic audio.
Real voices in real rooms differ - docs/WAKE_WORD.md has the one-hour TV
test and the false-reject check to run on your own PC.

Requirements (training only):
    pip install numpy scipy onnx onnxruntime openwakeword piper-tts
    espeak-ng on PATH                       (apt install espeak-ng)
    Piper voice en-us-libritts-high.onnx + .onnx.json, from
      https://github.com/rhasspy/piper/releases/tag/v0.0.2
      (voice-en-us-libritts-high.tar.gz)
    A folder of plain-text files for negative speech - the shipped model
    used NLTK's public-domain Gutenberg selection
      (https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/gutenberg.zip)

Usage:
    python scripts/train_wake_word.py --piper-voice DIR/en-us-libritts-high.onnx \\
        --corpus DIR --work WORKDIR [--out voice_models/wakewords/hey_wave.onnx]

Each stage is cached in WORKDIR; a rerun skips finished stages
(--force synth|features|train re-does that stage and everything after it).
"""
import argparse
import hashlib
import io
import json
import math
import os
import random
import re
import subprocess
import sys
import time
import wave
from datetime import datetime, timezone
from multiprocessing import Pool

import numpy as np

SR = 16000
WIN = 32000             # 2.0 s training windows -> exactly 16 embedding frames
FRAMES = 16             # openWakeWord's standard model input: 16 x 96
EMB = 96
FRAME_SAMPLES = 1280    # 80 ms

# --------------------------------------------------------------------------
# Phrases
# --------------------------------------------------------------------------
POSITIVE_TEXTS = ["hey wave", "hey wave.", "hey, wave.", "hey wave!", "hey wave?", "hey Wave,",
                  "hay wave.", "hey weyv.", "hey vave."]
POSITIVE_TEXTS_HI = ["हे वेव", "हे वेव।", "हे, वेव"]

# Said like a wake word but must NOT wake Wave. "hi/hello/okay wave" are
# deliberately in neither list - closer to what a user might intend, so the
# model is left to generalize about them rather than taught either way.
ADVERSARIAL_TEXTS = [
    "hey", "hey.", "wave.", "waves.", "hey there.", "hey you.", "hey dave.", "hey babe.", "hey wade.",
    "hey way.", "hey wait.", "hey save.", "hey gabe.", "hey jane.", "hey wayne.", "hey weigh.",
    "hey whale.", "hey wake.", "hey wait a second.", "hey we've.", "hey weave.", "hey Eva.",
    "hey Maeve.", "a wave.", "the wave.", "they wave.", "they waved.", "heat wave.", "new wave.",
    "microwave.", "brain wave.", "wave goodbye.", "make a wave.", "hey siri.", "hey google.",
    "okay google.", "hey jarvis.", "alexa.", "hey hey.", "hey man.", "hey bro.", "hey yaar.",
    "hey baby.", "hey what.", "hey wow.", "heavy.", "away.", "always.", "hey mate.", "say wave.",
    "hey grave.", "hey brave.", "hey cave.", "hey Wales.", "hey Wednesday.", "hey ways.",
]
ADVERSARIAL_TEXTS_HI = ["हे भगवान", "हे राम", "वेव", "हे", "हेलो", "हाय", "वाह", "अरे वाह"]

HINGLISH_TEXTS = [
    "kya haal hai bhai", "aaj mausam kaisa hai", "chrome kholo", "volume badhao", "gaana chalao",
    "mujhe kal subah yaad dilana", "bas karo yaar", "ruk jao ek minute", "theek hai chalo",
    "acha chalo phir milte hain", "kitne baje hai abhi", "khana kha liya kya", "papa ko call karo",
    "wifi band karo", "screenshot le lo", "kal milte hain", "kya kar rahe ho", "mera phone kahan hai",
    "light band kar do", "awaaz kam karo", "notepad mein likho", "yeh kya ho raha hai",
    "mujhe neend aa rahi hai", "chai banao", "office ke liye late ho gaya", "traffic bahut hai aaj",
    "movie dekhne chalein", "paani pee lo", "exam kal hai", "bahut garmi hai", "baarish ho rahi hai",
    "mummy ne bulaya hai", "dukaan band ho gayi", "kya bolte ho", "sab theek ho jayega",
    "jaldi karo", "dhire bolo", "aur sunao", "kuch nahi bas aise hi", "game khelte hain",
]
HINDI_TEXTS = [
    "आज मौसम कैसा है", "मुझे कल सुबह याद दिलाना", "गाना चलाओ", "आवाज़ कम करो", "क्या हाल है",
    "मैं ठीक हूँ", "चलो चलते हैं", "दरवाज़ा बंद करो", "पानी लाओ", "समय क्या हुआ है",
    "बहुत अच्छा", "कल मिलते हैं", "खाना तैयार है", "बारिश हो रही है", "मुझे भूख लगी है",
    "यह किताब बहुत अच्छी है", "बच्चे स्कूल गए हैं", "आज छुट्टी है", "बाज़ार चलें", "फ़ोन कहाँ है",
]

# espeak-ng variants too extreme to resemble a human voice.
_SKIP_VARIANTS = {"Demonic", "UniRobot", "anikaRobot", "Tweaky", "Mr serious"}
ESPEAK_EN_VOICES = ["en-us", "en-gb", "en-gb-scotland", "en-gb-x-rp", "en-gb-x-gbclan",
                    "en-gb-x-gbcwmd", "en-029", "en-us-nyc"]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Clip storage: a list of int16 arrays <-> one .npz
# --------------------------------------------------------------------------
def save_clips(path, clips, meta=None):
    lengths = np.array([len(c) for c in clips], dtype=np.int64)
    data = np.concatenate(clips).astype(np.int16) if clips else np.zeros(0, np.int16)
    np.savez(path, data=data, lengths=lengths, meta=np.array(json.dumps(meta or [])))


def load_clips(path):
    z = np.load(path)
    data, lengths = z["data"], z["lengths"]
    offsets = np.concatenate([[0], np.cumsum(lengths)])
    meta = json.loads(str(z["meta"])) if "meta" in z else []
    return [data[offsets[i]:offsets[i + 1]] for i in range(len(lengths))], meta


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------
def resample(x, sr_in, sr_out=SR):
    from scipy.signal import resample_poly
    if sr_in == sr_out:
        return x
    g = math.gcd(int(sr_in), int(sr_out))
    y = resample_poly(x.astype(np.float32), sr_out // g, sr_in // g)
    return np.clip(y, -32768, 32767).astype(np.int16)


def trim(x, rel=0.02, pad_s=0.03):
    """Cuts leading/trailing silence (relative to the clip's own peak)."""
    if len(x) == 0:
        return x
    a = np.abs(x.astype(np.int32))
    idx = np.flatnonzero(a > max(1, rel * a.max()))
    if idx.size == 0:
        return x[:0]
    pad = int(pad_s * SR)
    return x[max(0, idx[0] - pad): min(len(x), idx[-1] + pad)]


def wav_bytes_to_int16(buf):
    with wave.open(io.BytesIO(buf)) as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16)
    if ch > 1:
        pcm = pcm.reshape(-1, ch).mean(axis=1).astype(np.int16)
    return resample(pcm, sr)


def espeak_variants():
    vdir = None
    try:
        out = subprocess.run(["espeak-ng", "--version"], capture_output=True, text=True).stdout
        m = re.search(r"Data at:\s*(\S+)", out)
        vdir = os.path.join(m.group(1), "voices", "!v") if m else None
    except Exception:
        pass
    names = sorted(os.listdir(vdir)) if vdir and os.path.isdir(vdir) else []
    return [n for n in names if n not in _SKIP_VARIANTS]


def espeak_say(text, voice, variant, speed, pitch, gap=0):
    v = f"{voice}+{variant}" if variant else voice
    cmd = ["espeak-ng", "-v", v, "-s", str(speed), "-p", str(pitch), "-g", str(gap), "--stdout", text]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return np.zeros(0, np.int16)
    return wav_bytes_to_int16(r.stdout)


_PIPER = None


def _piper_init(model_path, threads):
    global _PIPER
    import onnxruntime
    real = onnxruntime.SessionOptions

    def opts():
        o = real()
        o.intra_op_num_threads = threads
        o.inter_op_num_threads = 1
        return o
    onnxruntime.SessionOptions = opts       # PiperVoice.load builds its own options
    from piper import PiperVoice
    _PIPER = PiperVoice.load(model_path)
    onnxruntime.SessionOptions = real


def _piper_job(job):
    text, speaker, ls, ns, nw = job
    from piper import SynthesisConfig
    cfg = SynthesisConfig(speaker_id=speaker, length_scale=ls, noise_scale=ns, noise_w_scale=nw)
    try:
        audio = b"".join(c.audio_int16_bytes for c in _PIPER.synthesize(text, syn_config=cfg))
    except Exception:
        return np.zeros(0, np.int16)
    return resample(np.frombuffer(audio, dtype=np.int16), _PIPER.config.sample_rate)


class Synth:
    def __init__(self, piper_model, procs):
        self.piper_model = piper_model
        self.procs = procs
        self.variants = espeak_variants()
        if not self.variants:
            log("WARNING: no espeak-ng variants found - espeak clips use the base voices only")

    def piper(self, jobs, label):
        if not jobs:
            return []
        t = time.time()
        threads = max(1, (os.cpu_count() or 2) // self.procs)
        with Pool(self.procs, initializer=_piper_init, initargs=(self.piper_model, threads)) as pool:
            out = pool.map(_piper_job, jobs, chunksize=4)
        log(f"  piper {label}: {len(out)} clips in {time.time() - t:.0f}s")
        return out

    def espeak(self, jobs, label):
        t = time.time()
        out = [espeak_say(*j) for j in jobs]
        log(f"  espeak {label}: {len(out)} clips in {time.time() - t:.0f}s")
        return out


def piper_jobs(texts, speakers, n, rnd, rate=(0.75, 1.35)):
    """`rnd` is a random.Random (cheap choices from long lists)."""
    return [(rnd.choice(texts), int(rnd.choice(speakers)), rnd.uniform(*rate),
             rnd.uniform(0.35, 0.9), rnd.uniform(0.4, 1.0)) for _ in range(n)]


def espeak_jobs(texts, voices, variants, n, rnd, speed=(120, 230), pitch=(15, 85)):
    return [(rnd.choice(texts), rnd.choice(voices), rnd.choice(variants) if variants else "",
             int(rnd.uniform(*speed)), int(rnd.uniform(*pitch)), 0) for _ in range(n)]


def corpus_sentences(corpus_dir, files):
    out = []
    for name in files:
        with open(os.path.join(corpus_dir, name), encoding="utf-8", errors="ignore") as f:
            text = f.read()
        text = re.sub(r"\[[^\]]*\]", " ", text)
        text = re.sub(r"[_*#<>|~\"]", " ", text)
        text = re.sub(r"\s+", " ", text)
        for sent in re.split(r"(?<=[.!?;:])\s+", text):
            words = sent.split()
            if 4 <= len(words) <= 40 and not re.search(r"\d", sent):
                out.append(" ".join(words[:28]))
    return out


# --------------------------------------------------------------------------
# Procedural noise
# --------------------------------------------------------------------------
def colored_noise(n, rng, kind):
    w = rng.standard_normal(n)
    if kind == "white":
        return w
    f = np.fft.rfft(w)
    freqs = np.fft.rfftfreq(n, 1 / SR)
    freqs[0] = freqs[1]
    if kind == "pink":
        f /= np.sqrt(freqs)
    elif kind == "brown":
        f /= freqs
    elif kind == "fan":
        f /= np.sqrt(freqs)
        f *= 1.0 / (1.0 + (freqs / rng.uniform(300, 1200)) ** 2)
    x = np.fft.irfft(f, n)
    return x / (np.std(x) + 1e-9)


def hum(n, rng):
    t = np.arange(n) / SR
    base = rng.choice([50.0, 60.0])
    x = sum(rng.uniform(0.2, 1.0) / k * np.sin(2 * np.pi * base * k * t + rng.uniform(0, 6.28))
            for k in range(1, 8))
    return x / (np.std(x) + 1e-9) + 0.05 * rng.standard_normal(n)


def music(n, rng):
    t = np.arange(n) / SR
    x = np.zeros(n)
    beat = rng.uniform(0.35, 0.7)
    root = rng.uniform(110, 330)
    pos = 0.0
    while pos < n / SR:
        dur = beat * rng.choice([1, 1, 2, 0.5])
        a, b = int(pos * SR), min(n, int((pos + dur) * SR))
        if b <= a:
            break
        tt = t[a:b] - pos
        env = np.minimum(1, tt / 0.01) * np.exp(-tt * rng.uniform(1.5, 6))
        chord = [1, 1.25, 1.5] if rng.random() < 0.5 else [1, 1.2, 1.5]
        f0 = root * rng.choice([1, 9 / 8, 4 / 3, 3 / 2, 5 / 3])
        for r in chord:
            for h in (1, 2, 3):
                x[a:b] += env * np.sin(2 * np.pi * f0 * r * h * tt) / h
        if rng.random() < 0.6:            # kick/snare-ish hit
            k = min(b - a, int(0.12 * SR))
            kk = np.arange(k) / SR
            x[a:a + k] += 1.5 * np.exp(-kk * 30) * (np.sin(2 * np.pi * 60 * kk) + 0.5 * rng.standard_normal(k))
        pos += dur
    return x / (np.std(x) + 1e-9)


def clicks(n, rng):
    x = 0.02 * rng.standard_normal(n)
    for _ in range(int(rng.uniform(5, 30) * n / SR)):
        i = int(rng.uniform(0, n - 400))
        k = int(rng.uniform(80, 400))
        x[i:i + k] += rng.uniform(0.5, 3) * np.exp(-np.arange(k) / (k / 5)) * rng.standard_normal(k)
    return x / (np.std(x) + 1e-9)


def noise_clip(n, rng):
    kind = rng.choice(["white", "pink", "brown", "fan", "hum", "music", "clicks", "silence"])
    if kind == "hum":
        return hum(n, rng), kind
    if kind == "music":
        return music(n, rng), kind
    if kind == "clicks":
        return clicks(n, rng), kind
    if kind == "silence":
        return 0.01 * rng.standard_normal(n), kind
    return colored_noise(n, rng, kind), kind


# --------------------------------------------------------------------------
# Augmentation
# --------------------------------------------------------------------------
def speed_perturb(x, rng, lo=0.9, hi=1.1):
    f = rng.uniform(lo, hi)
    n = int(len(x) / f)
    if n < 2:
        return x
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float32))


def reverb(x, rng):
    from scipy.signal import fftconvolve
    rt60 = rng.uniform(0.12, 0.9)
    n = int(SR * min(1.0, rt60 * 1.2))
    t = np.arange(n) / SR
    h = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    h[0] = rng.uniform(1.5, 4.0)                       # direct path
    for _ in range(int(rng.integers(2, 6))):           # a few early reflections
        h[int(rng.uniform(0.002, 0.03) * SR)] += rng.uniform(0.3, 1.0)
    y = fftconvolve(x, h)[:len(x)]
    return y / (np.std(y) + 1e-9) * (np.std(x) + 1e-9)


def eq(x, rng):
    from scipy.signal import butter, sosfilt
    sos = []
    if rng.random() < 0.7:
        sos.append(butter(2, rng.uniform(60, 400), "highpass", fs=SR, output="sos"))
    if rng.random() < 0.7:
        sos.append(butter(2, rng.uniform(2800, 7600), "lowpass", fs=SR, output="sos"))
    y = x
    for s in sos:
        y = sosfilt(s, y)
    return y


def augment_speech(x, rng, p_reverb=0.5):
    y = x.astype(np.float32)
    if rng.random() < 0.5:
        y = speed_perturb(y, rng)
    if rng.random() < p_reverb:
        y = reverb(y, rng)
    if rng.random() < 0.8:
        y = eq(y, rng)
    return y


def mix_window(fg, bg, end, rng, snr_db):
    """Places `fg` (float) so it ends at sample `end` of a WIN-long window
    over background `bg` (float, unit RMS, len >= WIN) at `snr_db`, then
    scales the whole window to a random loudness. Returns int16."""
    win = np.zeros(WIN, np.float32)
    start = end - len(fg)
    if start < 0:
        fg = fg[-start:]
        start = 0
    win[start:start + len(fg)] = fg[: WIN - start]
    sig_rms = np.sqrt(np.mean(fg ** 2)) if len(fg) else 1.0
    if bg is not None:
        off = int(rng.integers(0, max(1, len(bg) - WIN)))
        b = bg[off:off + WIN]
        win += b * sig_rms / (10 ** (snr_db / 20.0)) / (np.std(b) + 1e-9)
    target_dbfs = rng.uniform(-38, -14)
    rms = np.sqrt(np.mean(win ** 2)) + 1e-9
    win *= (10 ** (target_dbfs / 20.0) * 32767.0) / rms
    if rng.random() < 0.05:
        win *= rng.uniform(1.5, 3.0)                    # occasional clipping
    return np.clip(win, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------
# Stage 1: synthesis
# --------------------------------------------------------------------------
def stage_synth(args, work):
    py_rng = random.Random(args.seed)
    synth = Synth(args.piper_voice, args.procs)
    variants = synth.variants
    py_rng.shuffle(variants)
    n_hold_var = max(1, len(variants) // 8)
    var_hold, var_train = variants[:n_hold_var], variants[n_hold_var:]
    speakers = list(range(904))
    py_rng.shuffle(speakers)
    spk_hold, spk_train = speakers[:100], speakers[100:]
    files = sorted(f for f in os.listdir(args.corpus) if f.endswith(".txt"))
    hold_files = [f for f in files if any(k in f for k in ("carroll", "chesterton-thursday", "bryant"))] or files[-2:]
    train_files = [f for f in files if f not in hold_files]
    sents_train = corpus_sentences(args.corpus, train_files)
    sents_hold = corpus_sentences(args.corpus, hold_files)
    log(f"corpus: {len(sents_train)} training sentences, {len(sents_hold)} held-out "
        f"({', '.join(hold_files)}); espeak variants {len(var_train)} train / {len(var_hold)} held out")
    s = args.scale

    def run(name, piper_j, espeak_j):
        path = os.path.join(work, f"clips_{name}.npz")
        if os.path.exists(path):
            log(f"  {name}: cached")
            return
        clips = synth.piper(piper_j, name) + synth.espeak(espeak_j, name)
        clips = [trim(c) for c in clips]
        keep = [c for c in clips if len(c) > int(0.15 * SR)]
        save_clips(path, keep)
        log(f"  {name}: kept {len(keep)}/{len(clips)}")

    R = py_rng
    log("stage synth: positives")
    run("pos_train", piper_jobs(POSITIVE_TEXTS, spk_train, int(2400 * s), R),
        espeak_jobs(POSITIVE_TEXTS, ESPEAK_EN_VOICES, var_train, int(1500 * s), R)
        + espeak_jobs(POSITIVE_TEXTS_HI, ["hi"], var_train, int(400 * s), R, speed=(120, 210)))
    run("pos_hold", piper_jobs(POSITIVE_TEXTS, spk_hold, int(250 * s), R),
        espeak_jobs(POSITIVE_TEXTS, ESPEAK_EN_VOICES, var_hold, int(150 * s), R)
        + espeak_jobs(POSITIVE_TEXTS_HI, ["hi"], var_hold, int(50 * s), R, speed=(120, 210)))
    log("stage synth: sound-alike negatives")
    run("adv_train", piper_jobs(ADVERSARIAL_TEXTS, spk_train, int(1000 * s), R),
        espeak_jobs(ADVERSARIAL_TEXTS, ESPEAK_EN_VOICES, var_train, int(1100 * s), R)
        + espeak_jobs(ADVERSARIAL_TEXTS_HI, ["hi"], var_train, int(150 * s), R, speed=(120, 210)))
    run("adv_hold", piper_jobs(ADVERSARIAL_TEXTS, spk_hold, int(120 * s), R),
        espeak_jobs(ADVERSARIAL_TEXTS, ESPEAK_EN_VOICES, var_hold, int(120 * s), R)
        + espeak_jobs(ADVERSARIAL_TEXTS_HI, ["hi"], var_hold, int(20 * s), R, speed=(120, 210)))
    log("stage synth: negative speech")
    run("speech_train", piper_jobs(sents_train + HINGLISH_TEXTS, spk_train, int(420 * s), R, rate=(0.8, 1.25)),
        espeak_jobs(sents_train + HINGLISH_TEXTS, ESPEAK_EN_VOICES, var_train, int(900 * s), R)
        + espeak_jobs(HINDI_TEXTS, ["hi"], var_train, int(150 * s), R, speed=(120, 200)))
    run("speech_hold", piper_jobs(sents_hold + HINGLISH_TEXTS, spk_hold, int(200 * s), R, rate=(0.8, 1.25)),
        espeak_jobs(sents_hold + HINGLISH_TEXTS, ESPEAK_EN_VOICES, var_hold, int(420 * s), R)
        + espeak_jobs(HINDI_TEXTS, ["hi"], var_hold, int(60 * s), R, speed=(120, 200)))
    # More natural-voice negatives: a TV sounds like Piper's LibriTTS voices,
    # not like espeak, and FA/h on natural speech is what users notice.
    if args.more_speech:
        run("speech_more", piper_jobs(sents_train + HINGLISH_TEXTS, spk_train, int(args.more_speech * s),
                                      random.Random(args.seed + 5), rate=(0.8, 1.25)), [])


# --------------------------------------------------------------------------
# Stage 2: windows + features
# --------------------------------------------------------------------------
class Embedder:
    def __init__(self):
        from openwakeword.utils import AudioFeatures, download_models
        try:
            download_models(model_names=["__feature_models_only__"])
        except Exception as e:
            log(f"  (feature model download skipped: {e})")
        try:
            self.f = AudioFeatures(inference_framework="onnx", ncpu=max(1, int(os.environ.get("WAKE_TRAIN_NCPU", "1"))))
        except TypeError:
            self.f = AudioFeatures(inference_framework="onnx")

    def windows(self, x):
        """(N, WIN) int16 -> (N, 16, 96) float32"""
        out = []
        for i in range(0, len(x), 256):
            out.append(self.f.embed_clips(np.asarray(x[i:i + 256]), batch_size=64))
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, FRAMES, EMB), np.float32)

    def stream(self, x):
        """long int16 stream -> (frames, 96) float32, one frame per 80 ms.
        Embedding k covers samples [1280k, 1280k + 12400), so the stream is
        cut into 60 s chunks that each carry 12 800 samples of look-ahead
        and keep exactly their own 750 frames - identical to embedding the
        whole stream at once, without holding it all in memory."""
        chunk, look_ahead = SR * 60, 12800
        parts = []
        for start in range(0, len(x), chunk):
            seg = x[start:start + chunk + look_ahead]
            if len(seg) < 12400:
                break
            e = self.f._get_embeddings(seg)
            parts.append(e[:chunk // FRAME_SAMPLES])
        return np.concatenate(parts).astype(np.float32) if parts else np.zeros((0, EMB), np.float32)


def build_background_bank(speech, rng, n=160, seconds=6):
    """Unit-RMS backgrounds: noise kinds, music, and babble from negative speech."""
    bank = []
    for i in range(n):
        if i % 4 == 3 and speech:
            mix = np.zeros(SR * seconds)
            for _ in range(int(rng.integers(3, 7))):
                c = speech[int(rng.integers(0, len(speech)))].astype(np.float32)
                c = np.tile(c, int(np.ceil(SR * seconds / max(1, len(c)))))[:SR * seconds]
                mix += c / (np.std(c) + 1e-9) * rng.uniform(0.4, 1.0)
            bank.append(mix / (np.std(mix) + 1e-9))
        else:
            x, _ = noise_clip(SR * seconds, rng)
            bank.append(x)
    return bank


def positive_like_windows(clips, bank, speech, rng, copies, end_range=(0.04, 0.5), p_context=0.25):
    out = []
    for c in clips:
        for _ in range(copies):
            fg = augment_speech(c, rng)
            if len(fg) > WIN - int(0.05 * SR):
                fg = fg[-(WIN - int(0.05 * SR)):]
            end = WIN - int(rng.uniform(*end_range) * SR)
            if p_context and rng.random() < p_context and speech:
                ctx = augment_speech(speech[int(rng.integers(0, len(speech)))], rng).astype(np.float32)
                gap = int(rng.uniform(0.08, 0.45) * SR)
                lead = ctx[-max(1, end - len(fg) - gap):] if end - len(fg) - gap > 0 else ctx[:0]
                pad = np.zeros(gap, np.float32)
                fg = np.concatenate([lead / (np.std(lead) + 1e-9) * (np.std(fg) + 1e-9) * rng.uniform(0.5, 1.2)
                                     if len(lead) else lead, pad, fg])
            bg = bank[int(rng.integers(0, len(bank)))] if rng.random() < 0.9 else None
            out.append(mix_window(fg, bg, end, rng, snr_db=rng.uniform(0, 30)))
    return np.stack(out) if out else np.zeros((0, WIN), np.int16)


def noise_windows(bank, rng, n):
    out = []
    for _ in range(n):
        x, _ = noise_clip(WIN, rng)
        if rng.random() < 0.5:
            b = bank[int(rng.integers(0, len(bank)))]
            off = int(rng.integers(0, len(b) - WIN))
            x = x + b[off:off + WIN] * rng.uniform(0.3, 1.5)
        out.append(mix_window(np.zeros(0, np.float32), x, WIN, rng, snr_db=0))
    return np.stack(out)


def split_clips(clips, frac, rng):
    """(train, val) - a random `frac` of the clips held out for validation."""
    idx = rng.permutation(len(clips))
    k = int(round(len(clips) * frac))
    return [clips[i] for i in idx[k:]], [clips[i] for i in idx[:k]]


def _segment(clips, rng, lo=0.15, hi=0.9):
    c = clips[int(rng.integers(0, len(clips)))]
    n = int(rng.uniform(lo, hi) * SR)
    if len(c) <= n:
        return c.astype(np.float32)
    a = int(rng.integers(0, len(c) - n))
    return c[a:a + n].astype(np.float32)


def spliced_windows(speech, pos, bank, rng, n):
    """Negatives built from pieces, each ending where a wake word would:
    word-length segments of different speakers back to back; the "hey" of
    "hey wave" followed by something else; something else followed by the
    "wave". They teach the model that only the whole phrase counts."""
    out = []
    for i in range(n):
        kind = i % 4
        parts = []
        if kind == 1 and pos:
            p = pos[int(rng.integers(0, len(pos)))]
            parts = [p[: int(len(p) * rng.uniform(0.22, 0.32))].astype(np.float32), _segment(speech, rng)]
        elif kind == 2 and pos:
            p = pos[int(rng.integers(0, len(pos)))]
            parts = [_segment(speech, rng), p[int(len(p) * rng.uniform(0.62, 0.72)):].astype(np.float32)]
        else:
            parts = [_segment(speech, rng) for _ in range(int(rng.integers(2, 6)))]
        pieces = []
        for k, part in enumerate(parts):
            part = part / (np.std(part) + 1e-9) * rng.uniform(0.6, 1.4)
            pieces.append(part)
            if k < len(parts) - 1:
                pieces.append(np.zeros(int(rng.uniform(0.0, 0.08) * SR), np.float32))
        fg = augment_speech(np.concatenate(pieces), rng)
        if len(fg) > WIN - int(0.05 * SR):
            fg = fg[-(WIN - int(0.05 * SR)):]
        end = WIN - int(rng.uniform(0.04, 0.5) * SR)
        bg = bank[int(rng.integers(0, len(bank)))] if rng.random() < 0.9 else None
        out.append(mix_window(fg, bg, end, rng, snr_db=rng.uniform(0, 30)))
    return np.stack(out) if out else np.zeros((0, WIN), np.int16)


def make_stream(speech, adv, bank, rng, minutes, adv_per_min=1.0):
    """A long negative stream: speech with pauses, noise/music beds, babble,
    and sound-alike phrases sprinkled in. Returns int16."""
    total = int(minutes * 60 * SR)
    out = np.zeros(total, np.float32)
    pos = 0
    adv_every = 60.0 / adv_per_min if adv_per_min else 1e9
    next_adv = rng.uniform(5, adv_every)
    while pos < total:
        if adv and pos / SR >= next_adv:
            c = adv[int(rng.integers(0, len(adv)))]
            next_adv += rng.uniform(0.5, 1.5) * adv_every
        else:
            c = speech[int(rng.integers(0, len(speech)))]
        seg = augment_speech(c, rng, p_reverb=0.4)
        seg = seg / (np.std(seg) + 1e-9) * 10 ** (rng.uniform(-6, 6) / 20)
        n = min(len(seg), total - pos)
        out[pos:pos + n] += seg[:n]
        pos += n + int(rng.uniform(0.05, 1.2) * SR)
    # noise beds over random stretches
    bed_pos = 0
    while bed_pos < total:
        length = int(rng.uniform(10, 60) * SR)
        if rng.random() < 0.6:
            b = bank[int(rng.integers(0, len(bank)))]
            reps = np.tile(b, int(np.ceil(length / len(b))))[:length]
            n = min(length, total - bed_pos)
            out[bed_pos:bed_pos + n] += reps[:n] * 10 ** (rng.uniform(-30, -5) / 20)
        bed_pos += length
    # loudness drifts slowly, like a TV across the room
    env = np.interp(np.arange(total), np.linspace(0, total, 40), 10 ** (rng.uniform(-30, -14, 40) / 20))
    out = out / (np.sqrt(np.mean(out ** 2)) + 1e-9) * env * 32767
    return np.clip(out, -32768, 32767).astype(np.int16)


def stage_features(args, work):
    rng = np.random.default_rng(args.seed + 1)
    path = os.path.join(work, "features.npz")
    if os.path.exists(path):
        log("stage features: cached")
        return
    emb = Embedder()
    pos_all, _ = load_clips(os.path.join(work, "clips_pos_train.npz"))
    pos_ho, _ = load_clips(os.path.join(work, "clips_pos_hold.npz"))
    adv_all, _ = load_clips(os.path.join(work, "clips_adv_train.npz"))
    adv_ho, _ = load_clips(os.path.join(work, "clips_adv_hold.npz"))
    sp_all, _ = load_clips(os.path.join(work, "clips_speech_train.npz"))
    more = os.path.join(work, "clips_speech_more.npz")
    if os.path.exists(more):
        sp_all = sp_all + load_clips(more)[0]
    sp_ho, _ = load_clips(os.path.join(work, "clips_speech_hold.npz"))
    # Validation (model selection) comes out of the TRAINING voices/books;
    # the held-out voices/books stay untouched for the final numbers.
    split_rng = np.random.default_rng(args.seed + 3)
    pos_tr, pos_va = split_clips(pos_all, 0.1, split_rng)
    adv_tr, adv_va = split_clips(adv_all, 0.1, split_rng)
    sp_tr, sp_va = split_clips(sp_all, 0.15, split_rng)
    bank_tr = build_background_bank(sp_tr, rng)
    bank_va = build_background_bank(sp_va, np.random.default_rng(args.seed + 98), n=60)
    bank_ho = build_background_bank(sp_ho, np.random.default_rng(args.seed + 99))
    s = args.scale

    t = time.time()
    feats = {}
    log(f"stage features: {len(pos_tr)} positive clips x{args.pos_copies}; negative speech "
        f"{len(sp_tr)} train / {len(sp_va)} validation clips")
    feats["pos"] = emb.windows(positive_like_windows(pos_tr, bank_tr, sp_tr, rng, args.pos_copies))
    log(f"  positives done ({time.time() - t:.0f}s)")
    feats["adv"] = emb.windows(positive_like_windows(adv_tr, bank_tr, sp_tr, rng, 2))
    log(f"  sound-alikes done ({time.time() - t:.0f}s)")
    feats["noise"] = emb.windows(noise_windows(bank_tr, rng, int(2500 * s)))
    log(f"  noise done ({time.time() - t:.0f}s)")
    feats["splice"] = emb.windows(spliced_windows(sp_tr, pos_tr, bank_tr, rng, int(args.splice * s)))
    log(f"  spliced negatives done ({time.time() - t:.0f}s)")
    stream = make_stream(sp_tr, adv_tr, bank_tr, rng, minutes=args.train_stream_minutes)
    frames = emb.stream(stream)
    idx = np.arange(0, len(frames) - FRAMES + 1, 2)
    feats["stream"] = np.stack([frames[i:i + FRAMES] for i in idx])
    log(f"  training stream {args.train_stream_minutes:.0f} min -> {len(feats['stream'])} windows ({time.time() - t:.0f}s)")
    feats["pos_val"] = emb.windows(positive_like_windows(pos_va, bank_va, sp_va, rng, 2))
    feats["adv_val"] = emb.windows(positive_like_windows(adv_va, bank_va, sp_va, rng, 1))
    val_stream = make_stream(sp_va, [], bank_va, np.random.default_rng(args.seed + 6),
                             minutes=args.val_stream_minutes, adv_per_min=0)
    vframes = emb.stream(val_stream)
    feats["val_stream"] = np.stack([vframes[i:i + FRAMES] for i in range(0, len(vframes) - FRAMES + 1)])
    feats["pos_hold"] = emb.windows(positive_like_windows(pos_ho, bank_ho, sp_ho, rng, 2))
    feats["adv_hold"] = emb.windows(positive_like_windows(adv_ho, bank_ho, sp_ho, rng, 2))
    log(f"  validation + held-out sets done ({time.time() - t:.0f}s)")
    np.savez(path, **feats)

    # The held-out pieces for the streaming evaluation, as raw audio: plain
    # speech (what a TV sounds like) and the sound-alike phrases separately.
    hold_stream = make_stream(sp_ho, [], bank_ho, np.random.default_rng(args.seed + 7),
                              minutes=args.eval_stream_minutes, adv_per_min=0)
    np.save(os.path.join(work, "eval_stream.npy"), hold_stream)
    ev_rng = np.random.default_rng(args.seed + 8)
    pos_eval = [mix_window(augment_speech(c, ev_rng), bank_ho[int(ev_rng.integers(0, len(bank_ho)))],
                           WIN - int(ev_rng.uniform(0.3, 0.6) * SR), ev_rng, snr_db=ev_rng.uniform(5, 30))
                for c in pos_ho]
    np.save(os.path.join(work, "eval_pos.npy"), np.stack(pos_eval))
    clean_eval = [trim(c) for c in pos_ho]
    save_clips(os.path.join(work, "eval_pos_clean.npz"), clean_eval)
    log(f"stage features: done in {time.time() - t:.0f}s")


# --------------------------------------------------------------------------
# Stage 3: training (numpy MLP)
# --------------------------------------------------------------------------
class MLP:
    def __init__(self, dims, rng):
        self.W = [rng.standard_normal((a, b)).astype(np.float32) * np.sqrt(2.0 / a) for a, b in zip(dims[:-1], dims[1:])]
        self.b = [np.zeros(b, np.float32) for b in dims[1:]]
        self.mean = None
        self.std = None

    def forward(self, x, train=False, rng=None, drop=0.0):
        x = (x - self.mean) / self.std
        acts = [x]
        masks = []
        h = x
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            z = h @ W + b
            if i < len(self.W) - 1:
                h = np.maximum(z, 0)
                if train and drop:
                    m = (rng.random(h.shape) > drop).astype(np.float32) / (1 - drop)
                    h = h * m
                    masks.append(m)
                else:
                    masks.append(None)
                acts.append(h)
            else:
                logits = z[:, 0]
        return logits, acts, masks

    def predict(self, x, batch=4096):
        out = []
        for i in range(0, len(x), batch):
            logits, _, _ = self.forward(x[i:i + batch])
            out.append(1 / (1 + np.exp(-np.clip(logits, -30, 30))))
        return np.concatenate(out) if out else np.zeros(0)


def train_mlp(X, y, w, val_fn, args, rng, init=None, epochs=None, lr=None):
    dims = [FRAMES * EMB] + args.hidden + [1]
    model = init or MLP(dims, rng)
    if model.mean is None:
        model.mean = X.mean(axis=0).astype(np.float32)
        model.std = (X.std(axis=0) + 1e-3).astype(np.float32)
    params = model.W + model.b
    m_ = [np.zeros_like(p) for p in params]
    v_ = [np.zeros_like(p) for p in params]
    step = 0
    lr = lr or args.lr
    epochs = epochs or args.epochs
    n = len(X)
    best = (-1, None)
    for ep in range(epochs):
        perm = rng.permutation(n)
        for i in range(0, n, args.batch):
            bi = perm[i:i + args.batch]
            xb, yb, wb = X[bi], y[bi], w[bi]
            logits, acts, masks = model.forward(xb, train=True, rng=rng, drop=args.dropout)
            p = 1 / (1 + np.exp(-np.clip(logits, -30, 30)))
            g = (p - yb) * wb / wb.sum()                    # d(weighted BCE)/d logits
            grads_W, grads_b = [None] * len(model.W), [None] * len(model.b)
            d = g[:, None]
            for li in range(len(model.W) - 1, -1, -1):
                grads_W[li] = acts[li].T @ d + args.l2 * model.W[li]
                grads_b[li] = d.sum(axis=0)
                if li > 0:
                    d = (d @ model.W[li].T) * (acts[li] > 0)
                    if masks[li - 1] is not None:
                        d = d * masks[li - 1]
            step += 1
            cur_lr = lr * min(1.0, step / 200) * (0.5 * (1 + math.cos(math.pi * ep / epochs)))
            for k, (prm, gr) in enumerate(zip(params, grads_W + grads_b)):
                m_[k] = 0.9 * m_[k] + 0.1 * gr
                v_[k] = 0.999 * v_[k] + 0.001 * gr * gr
                mh = m_[k] / (1 - 0.9 ** step)
                vh = v_[k] / (1 - 0.999 ** step)
                prm -= (cur_lr * mh / (np.sqrt(vh) + 1e-8)).astype(np.float32)
        score, desc = val_fn(model)
        if score > best[0]:
            best = (score, ([W.copy() for W in model.W], [b.copy() for b in model.b]))
        if ep % 5 == 4 or ep == epochs - 1:
            log(f"    epoch {ep + 1}/{epochs}: {desc}")
    model.W, model.b = best[1]
    return model


def threshold_for_fa(stream_scores, hours, max_fa, smooth=1):
    """The lowest threshold (0.01 steps) whose false accepts per hour on a
    stream of negatives stay within `max_fa`."""
    sm = smooth_scores(stream_scores, smooth)
    for thr in np.arange(0.05, 1.0, 0.01):
        if count_events(sm, thr) / hours <= max_fa:
            return float(thr)
    return 0.995


def make_val_fn(pos_val, adv_val, val_stream, max_fa):
    """Model selection on VALIDATION data only (training voices and books
    the classifier never saw): recall at the threshold where the validation
    stream of natural speech gives `max_fa` false accepts per hour."""
    hours = len(val_stream) * FRAME_SAMPLES / SR / 3600.0

    def fn(model):
        thr = threshold_for_fa(model.predict(val_stream), hours, max_fa)
        recall = float(np.mean(model.predict(pos_val) >= thr))
        adv_acc = float(np.mean(model.predict(adv_val) >= thr)) if len(adv_val) else 0.0
        return recall, f"val recall {recall:.3f} at {max_fa:g} FA/h (threshold {thr:.2f}); sound-alikes accepted {adv_acc:.3f}"
    return fn


def stage_train(args, work):
    path = os.path.join(work, "model.npz")
    if os.path.exists(path):
        log("stage train: cached")
        return
    rng = np.random.default_rng(args.seed + 2)
    z = np.load(os.path.join(work, "features.npz"))
    flat = lambda a: a.reshape(len(a), -1)
    pos, adv, noise, stream = flat(z["pos"]), flat(z["adv"]), flat(z["noise"]), flat(z["stream"])
    splice = flat(z["splice"]) if "splice" in z.files else np.zeros((0, pos.shape[1]), np.float32)
    X = np.concatenate([pos, adv, noise, splice, stream])
    y = np.concatenate([np.ones(len(pos)), np.zeros(len(adv) + len(noise) + len(splice) + len(stream))]).astype(np.float32)
    # Negatives outnumber positives; weight so a false accept costs more than a false reject.
    w = np.concatenate([np.ones(len(pos)), np.full(len(adv), args.adv_weight), np.full(len(noise), 1.0),
                        np.full(len(splice), args.splice_weight), np.full(len(stream), 1.0)]).astype(np.float32)
    w[y == 0] *= args.neg_weight * len(pos) / max(1, (y == 0).sum())
    val_fn = make_val_fn(flat(z["pos_val"]), flat(z["adv_val"]), flat(z["val_stream"]), args.val_max_fa)
    log(f"stage train: {len(pos)} positive / {len(X) - len(pos)} negative windows "
        f"({len(splice)} spliced); hidden {args.hidden}")
    model = train_mlp(X, y, w, val_fn, args, rng)
    for r in range(args.mining_rounds):
        p_neg = model.predict(X[y == 0])
        hard = np.flatnonzero(y == 0)[p_neg > 0.2]
        log(f"  hard-negative round {r + 1}: {len(hard)} negatives scoring > 0.2 get weight x{args.hard_weight}")
        if len(hard) == 0:
            break
        w[hard] *= args.hard_weight
        model = train_mlp(X, y, w, val_fn, args, rng, init=model, epochs=max(6, args.epochs // 3), lr=args.lr / 3)
    np.savez(path, mean=model.mean, std=model.std, **{f"W{i}": W for i, W in enumerate(model.W)},
             **{f"b{i}": b for i, b in enumerate(model.b)})
    log("stage train: done")


def load_mlp(path):
    z = np.load(path)
    n = sum(1 for k in z.files if k.startswith("W"))
    m = MLP.__new__(MLP)
    m.W = [z[f"W{i}"] for i in range(n)]
    m.b = [z[f"b{i}"] for i in range(n)]
    m.mean, m.std = z["mean"], z["std"]
    return m


# --------------------------------------------------------------------------
# Stage 4: ONNX export
# --------------------------------------------------------------------------
def export_onnx(model, out_path):
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    W = [w.astype(np.float32) for w in model.W]
    b = [x.astype(np.float32) for x in model.b]
    # fold the input standardization into the first layer
    inv = (1.0 / model.std).astype(np.float32)
    W0 = (W[0] * inv[:, None]).astype(np.float32)
    b0 = (b[0] - (model.mean * inv) @ W[0]).astype(np.float32)
    W, b = [W0] + W[1:], [b0] + b[1:]
    inits, nodes = [], []
    nodes.append(helper.make_node("Flatten", ["input"], ["x0"], axis=1))
    cur = "x0"
    for i, (Wi, bi) in enumerate(zip(W, b)):
        inits += [numpy_helper.from_array(Wi, f"W{i}"), numpy_helper.from_array(bi, f"b{i}")]
        nodes.append(helper.make_node("Gemm", [cur, f"W{i}", f"b{i}"], [f"z{i}"]))
        cur = f"z{i}"
        if i < len(W) - 1:
            nodes.append(helper.make_node("Relu", [cur], [f"h{i}"]))
            cur = f"h{i}"
    nodes.append(helper.make_node("Sigmoid", [cur], ["output"]))
    graph = helper.make_graph(
        nodes, "hey_wave",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, ["batch", FRAMES, EMB])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, ["batch", 1])],
        inits)
    m = helper.make_model(graph, producer_name="wave-train_wake_word", opset_imports=[helper.make_opsetid("", 13)])
    m.ir_version = 8          # loadable by onnxruntime >= 1.10 (Wave pins 1.20 on Windows)
    onnx.checker.check_model(m)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    onnx.save(m, out_path)
    return out_path


# --------------------------------------------------------------------------
# Stage 5: evaluation through openwakeword.Model, frame by frame
# --------------------------------------------------------------------------
class OWW:
    """The exported model as voice/wakeword.py runs it: openwakeword.Model,
    onnx, 80 ms frames."""

    def __init__(self, model_path):
        from openwakeword.model import Model
        self.m = Model(wakeword_models=[model_path], inference_framework="onnx")
        self.name = list(self.m.models.keys())[0]

    def scores(self, audio):
        self.m.reset()
        out = np.zeros(len(audio) // FRAME_SAMPLES, np.float32)
        for i in range(len(out)):
            out[i] = self.m.predict(audio[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES])[self.name]
        return out


def count_events(scores, thr, refractory_frames=19):
    events, i = 0, 0
    hits = np.flatnonzero(np.asarray(scores) >= thr)
    last = -10 ** 9
    for h in hits:
        if h >= last + refractory_frames:      # ~1.5 s: one wake, not one per frame
            events += 1
            last = h
    return events


def smooth_scores(scores, k):
    """Score that must hold for k consecutive 80 ms frames: the minimum over
    each run of k frames. Real wake words keep the score up for several
    frames; most false triggers are a one-frame spike. voice/wakeword.py
    applies the same rule (the card's smoothing_frames)."""
    x = np.asarray(scores, dtype=np.float32)
    if k <= 1 or len(x) < k:
        return x if k <= 1 else np.zeros(1, np.float32)
    return np.min(np.stack([x[i:len(x) - k + 1 + i] for i in range(k)]), axis=0)


def gate_scores(oww, clip):
    """Per-frame scores for one captured phrase, padded exactly the way
    voice/wakeword.py's WakeWordGate.score_pcm16 does: 1 s of silence
    first, 0.5 s after."""
    x = np.concatenate([np.zeros(SR, np.int16), clip.astype(np.int16), np.zeros(SR // 2, np.int16)])
    return oww.scores(x)


def stage_eval(args, work, onnx_path):
    log("stage eval: streaming the exported model through openwakeword.Model")
    oww = OWW(onnx_path)
    stream = np.load(os.path.join(work, "eval_stream.npy"))
    s_scores = oww.scores(stream)
    hours = len(stream) / SR / 3600.0
    pos = np.load(os.path.join(work, "eval_pos.npy"))
    pad = np.zeros(SR // 2, np.int16)
    p_seqs = [oww.scores(np.concatenate([pad, c, pad])) for c in pos]
    clean, _ = load_clips(os.path.join(work, "eval_pos_clean.npz"))
    g_seqs = [gate_scores(oww, c) for c in clean]
    adv, _ = load_clips(os.path.join(work, "clips_adv_hold.npz"))
    a_seqs = [gate_scores(oww, c) for c in adv]
    by_smoothing = {}
    for k in (1, 2):
        sm = smooth_scores(s_scores, k)
        pm = np.array([smooth_scores(q, k).max() for q in p_seqs])
        gm = np.array([smooth_scores(q, k).max() for q in g_seqs])
        am = np.array([smooth_scores(q, k).max() for q in a_seqs])
        rows = []
        for thr in np.round(np.arange(0.2, 0.96, 0.05), 2):
            rows.append({
                "threshold": float(thr),
                "false_accepts_per_hour": round(count_events(sm, thr) / hours, 2),
                "false_reject_rate_noisy": round(float(np.mean(pm < thr)), 4),
                "false_reject_rate_clean": round(float(np.mean(gm < thr)), 4),
                "sound_alike_accept_rate": round(float(np.mean(am >= thr)), 4),
            })
        by_smoothing[k] = rows
        for r in rows:
            log(f"  smooth {k} thr {r['threshold']:.2f}: FA/h {r['false_accepts_per_hour']:6.2f}   "
                f"FRR noisy {r['false_reject_rate_noisy']:.3f}   FRR clean {r['false_reject_rate_clean']:.3f}   "
                f"sound-alikes {r['sound_alike_accept_rate']:.3f}")
    return {"stream_hours": round(hours, 3), "stream": "held-out voices reading held-out books, with noise and music; "
            "no wake word and no sound-alike phrases", "positives_noisy": int(len(pos)),
            "positives_clean": int(len(clean)), "sound_alike_clips": int(len(adv)),
            "by_smoothing": {str(k): v for k, v in by_smoothing.items()}}


def choose_operating_point(metrics, max_fa, max_frr_clean, prefilter_frr):
    """(smoothing_frames, threshold, mode). "standalone": the lowest-FRR point
    within the false-accept budget, if its clean FRR is acceptable. Otherwise
    "prefilter": a point that keeps nearly every real wake (clean FRR <=
    prefilter_frr), with every detection confirmed by the transcript."""
    best = None
    for k, rows in metrics["by_smoothing"].items():
        for r in rows:
            if r["false_accepts_per_hour"] <= max_fa:
                key = (r["false_reject_rate_clean"] + r["false_reject_rate_noisy"], int(k))
                if best is None or key < best[0]:
                    best = (key, int(k), r)
    if best is not None and best[2]["false_reject_rate_clean"] <= max_frr_clean:
        return best[1], best[2]["threshold"], "standalone", best[2]
    # Prefilter: among points that keep clean FRR within budget, the one with
    # the fewest false accepts (they all go to the transcript check).
    cand = []
    for k, rows in metrics["by_smoothing"].items():
        for r in rows:
            if r["false_reject_rate_clean"] <= prefilter_frr:
                cand.append(((r["false_accepts_per_hour"], r["false_reject_rate_noisy"]), int(k), r))
    if not cand:
        rows = metrics["by_smoothing"]["1"]
        return 1, rows[0]["threshold"], "prefilter", rows[0]
    cand.sort(key=lambda c: c[0])
    return cand[0][1], cand[0][2]["threshold"], "prefilter", cand[0][2]


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piper-voice", required=True, help="en-us-libritts-high.onnx (its .onnx.json next to it)")
    ap.add_argument("--corpus", required=True, help="folder of .txt files for negative speech")
    ap.add_argument("--work", required=True, help="cache folder for clips/features/model")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                  "voice_models", "wakewords", "hey_wave.onnx"))
    ap.add_argument("--scale", type=float, default=1.0, help="multiplies every clip count (0.2 for a quick try)")
    ap.add_argument("--procs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--pos-copies", type=int, default=3)
    ap.add_argument("--more-speech", type=int, default=1000,
                    help="extra Piper (natural-voice) negative sentences for training; 0 = none")
    ap.add_argument("--splice", type=int, default=6000, help="spliced negative windows ('hey' + something else...)")
    ap.add_argument("--splice-weight", type=float, default=1.5)
    ap.add_argument("--train-stream-minutes", type=float, default=90.0)
    ap.add_argument("--val-stream-minutes", type=float, default=20.0)
    ap.add_argument("--val-max-fa", type=float, default=2.0, help="FA/h budget the validation recall is read at")
    ap.add_argument("--eval-stream-minutes", type=float, default=60.0)
    ap.add_argument("--hidden", type=int, nargs="+", default=[128, 64])
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--l2", type=float, default=1e-4)
    ap.add_argument("--dropout", type=float, default=0.3)
    ap.add_argument("--neg-weight", type=float, default=3.0)
    ap.add_argument("--adv-weight", type=float, default=2.0)
    ap.add_argument("--hard-weight", type=float, default=3.0)
    ap.add_argument("--mining-rounds", type=int, default=2)
    ap.add_argument("--threshold", type=float, default=None,
                    help="threshold to recommend; default: chosen from the evaluation (see choose_operating_point)")
    ap.add_argument("--max-fa-per-hour", type=float, default=1.0)
    ap.add_argument("--max-frr-clean", type=float, default=0.10,
                    help="a standalone operating point must miss at most this share of clean wakes")
    ap.add_argument("--prefilter-frr", type=float, default=0.05,
                    help="otherwise: a prefilter point missing at most this share, every detection transcript-confirmed")
    ap.add_argument("--force", choices=["synth", "features", "train"], default=None)
    args = ap.parse_args(argv)

    os.makedirs(args.work, exist_ok=True)
    order = ["synth", "features", "train"]
    if args.force:
        for stage in order[order.index(args.force):]:
            for f in {"synth": [x for x in os.listdir(args.work) if x.startswith("clips_")],
                      "features": ["features.npz", "eval_stream.npy", "eval_pos.npy", "eval_pos_clean.npz"],
                      "train": ["model.npz"]}[stage]:
                p = os.path.join(args.work, f)
                if os.path.exists(p):
                    os.remove(p)
    t0 = time.time()
    stage_synth(args, args.work)
    stage_features(args, args.work)
    stage_train(args, args.work)
    model = load_mlp(os.path.join(args.work, "model.npz"))
    onnx_path = export_onnx(model, args.out)
    log(f"exported {onnx_path} ({os.path.getsize(onnx_path) // 1024} KB)")
    metrics = stage_eval(args, args.work, onnx_path)
    smoothing, rec, mode, point = choose_operating_point(metrics, args.max_fa_per_hour, args.max_frr_clean,
                                                         args.prefilter_frr)
    if args.threshold is not None:
        rec = args.threshold
    log(f"operating point: {mode}, threshold {rec}, smoothing {smoothing} frame(s): FA/h "
        f"{point['false_accepts_per_hour']}, FRR clean {point['false_reject_rate_clean']}, "
        f"noisy {point['false_reject_rate_noisy']}, sound-alikes {point['sound_alike_accept_rate']}")
    with open(onnx_path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    card = {
        "name": "hey_wave",
        "phrase": "hey wave",
        "format": "openWakeWord classifier (input 16x96 speech_embedding frames, output 1 score)",
        "recommended_threshold": rec,
        "smoothing_frames": smoothing,
        # standalone: the model decides (weak detections still get the
        # transcript check below WAKE_WORD_CONFIRM_BELOW). prefilter: the
        # model only decides which phrases are worth transcribing - every
        # detection must also LOOK like the wake phrase in the transcript.
        "mode": mode,
        **({"confirm_below": 1.0} if mode == "prefilter" else {}),
        "operating_point": point,
        "sha256": sha,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "training_seconds": round(time.time() - t0),
        "license": "MIT (same as Wave). Training speech was synthesized with Piper's LibriTTS voice "
                   "(dataset CC BY 4.0, https://www.openslr.org/60/) and espeak-ng; negative text from "
                   "public-domain Project Gutenberg books. No openWakeWord pretrained (CC BY-NC-SA) model "
                   "or data is part of it.",
        "data": {"positive_phrases": POSITIVE_TEXTS + POSITIVE_TEXTS_HI,
                 "sound_alike_negatives": ADVERSARIAL_TEXTS + ADVERSARIAL_TEXTS_HI,
                 "scale": args.scale, "seed": args.seed},
        "metrics_synthetic": metrics,
        "caveat": "All metrics are on synthetic, held-out voices. Measure on your own PC - see docs/WAKE_WORD.md.",
        "hyperparameters": {k: getattr(args, k) for k in ("hidden", "epochs", "batch", "lr", "l2", "dropout",
                                                         "neg_weight", "adv_weight", "hard_weight",
                                                         "mining_rounds", "pos_copies", "more_speech", "splice",
                                                         "splice_weight", "train_stream_minutes")},
    }
    card_path = os.path.splitext(onnx_path)[0] + ".json"
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, indent=2, ensure_ascii=False)
    log(f"model card: {card_path} (recommended threshold {rec}, {mode})")
    log(f"total {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
