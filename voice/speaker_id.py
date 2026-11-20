"""
voice/speaker_id.py — Tier 32: "is this the owner talking?"

Wave's mic hears everyone in the room - the TV, a video, other people. In
testing, a video's "Say something." was taken for a command and Wave started
talking on its own. Every utterance's VOICE is now checked against the
owner's voiceprint before anything else happens to it (voice/stt.py), so
other voices are dropped before they're even transcribed (no Whisper CPU
spent on them, nothing of theirs written to the log).

The voiceprint comes from the voice sample the owner gave for Wave's cloned
voice (config.TTS_CLONE_REFERENCE_PATH, avatar_rig/voice_reference.wav):
three centroids - the sample as recorded, and as if heard across a room by a
laptop mic (mild and harsh: reverb, band limit, noise), since the sample was
a phone recording and Wave hears through this PC's mic. A score is the best
cosine similarity to any of them, or to the centroid of what Wave has
learned from the owner's own confident commands through this mic.

Model: 3D-Speaker's CAM++ (iic/speech_campplus_sv_zh_en_16k-common_advanced,
Chinese+English, 16 kHz, 192-d), as exported by sherpa-onnx, run with
onnxruntime - no torch in Wave's process. Features: 80-bin Kaldi fbank
(kaldi-native-fbank) on samples in [-1, 1], mean-normalised over the
utterance (the model's own metadata: normalize_samples=1,
feature_normalize_type=global-mean). scripts/download_speaker_model.py
fetches it.

Calibrated on the owner's 21 s sample (10 s enrolled, the rest cut into
1-3 s pieces) against 40 clips from 10 other voices (edge-tts: Indian, Hindi,
US, UK, AU; male and female), each also through the simulated room: for 1 s
pieces the owner scored >= 0.29 even in the harsh room (>= 0.48 from 1.5 s,
>= 0.61 clean) and other voices at most 0.28 (0.25 from 1.5 s). Hence
SPEAKER_VERIFY_THRESHOLD = 0.28, leaning towards letting the owner through.
Wave's OWN cloned voice scores like the owner (0.56-0.72) - this can't stop
Wave hearing itself; AEC and the "Wave is talking" rules still do that.
"""
import hashlib
import logging
import os
import threading
import time

from settings.config import config

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - numpy is a core dependency
    np = None
    HAS_NUMPY = False

logger = logging.getLogger("SpeakerID")

MODEL_FILE = "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + MODEL_FILE)
MODEL_SHA256 = "53cab20ba8f2eb2f6e2d89ecc18e88ef3222169b61b4419cbc13666744a4574b"
SAMPLE_RATE = 16000
# Below ~1 s a voice embedding says too little: the owner's 0.5-0.8 s pieces
# scored anywhere from -0.1 to 0.5. Shorter utterances (a lone "Baby") pass
# unchecked, so the wake word is never missed; what follows is checked.
MIN_SECONDS = 1.0
LEARN_SCORE = 0.40         # only clearly-the-owner utterances refine it (other voices never passed 0.28)
MAX_LEARNED = 40
_VOICEPRINT_VERSION = 1


def default_model_path() -> str:
    configured = (getattr(config, "SPEAKER_MODEL_PATH", "") or "").strip()
    return configured or os.path.join(config.DATA_DIR, "voice_models", "speaker", MODEL_FILE)


def default_voiceprint_path() -> str:
    return os.path.join(config.DATA_DIR, "avatar_rig", "voiceprint.npz")


class SpeakerCheck:
    """One utterance's verdict: its score, whether that's the owner, and the
    embedding (kept so the caller can let Wave learn from it)."""

    __slots__ = ("score", "is_owner", "embedding")

    def __init__(self, score: float, is_owner: bool, embedding=None):
        self.score = float(score)
        self.is_owner = bool(is_owner)
        self.embedding = embedding

    def __repr__(self):
        return f"SpeakerCheck(score={self.score:.2f}, is_owner={self.is_owner})"


def pcm16_to_float(pcm: bytes):
    return np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0


def load_sample(path: str):
    """A voice sample as 16 kHz mono float32 in [-1, 1]."""
    import soundfile  # noqa: PLC0415
    from scipy.signal import resample_poly  # noqa: PLC0415

    data, rate = soundfile.read(path, dtype="float32", always_2d=True)
    x = data.mean(axis=1)
    if int(rate) != SAMPLE_RATE:
        g = np.gcd(int(rate), SAMPLE_RATE)
        x = resample_poly(x, SAMPLE_RATE // g, int(rate) // g).astype(np.float32)
    return np.clip(x, -1.0, 1.0)


def room(x, seed: int, decay: float, snr_db: float, band):
    """`x` as if heard across a room by a laptop mic: a decaying random
    reverb tail, band-limited, with white noise at `snr_db` - deterministic
    for a given seed, so a voiceprint always rebuilds the same."""
    from scipy.signal import butter, sosfilt  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    n = int(min(0.6, decay * 5) * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    rir = rng.standard_normal(n) * np.exp(-t / decay)
    rir[0] = 1.0
    y = np.convolve(x, rir / np.abs(rir).sum() * 4)[: len(x)]
    y = sosfilt(butter(4, list(band), btype="band", fs=SAMPLE_RATE, output="sos"), y)
    y = y + rng.standard_normal(len(y)) * (np.sqrt(np.mean(y ** 2)) / (10 ** (snr_db / 20)))
    return (y / (np.abs(y).max() + 1e-9) * 0.7).astype(np.float32)


_CONDITIONS = (
    ("as recorded", None),
    ("mild room", dict(decay=0.03, snr_db=30.0, band=(80, 7600))),
    ("harsh room", dict(decay=0.08, snr_db=20.0, band=(120, 7000))),
)


def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / (float(np.linalg.norm(v)) + 1e-9)


class SpeakerVerifier:
    """The owner's voiceprint and the model that compares voices to it."""

    def __init__(self, model_path=None, voiceprint_path=None, embedder=None):
        self._model_path = model_path
        self._voiceprint_path = voiceprint_path
        self._embedder = embedder          # tests: a callable float32 samples -> embedding
        self._session = None
        self._input_name = None
        self._load_failed = False
        self._lock = threading.RLock()
        self._centroids = None             # (k, d) for the current sample
        self._sample_key = None
        self._learned = []
        self._warned = set()
        self._announced = None             # the voiceprint the "on" line was logged for

    # -- configuration ---------------------------------------------------
    def model_path(self) -> str:
        return self._model_path or default_model_path()

    def voiceprint_path(self) -> str:
        return self._voiceprint_path or default_voiceprint_path()

    @staticmethod
    def enabled() -> bool:
        return (getattr(config, "SPEAKER_VERIFY", "auto") or "auto").strip().lower() not in ("off", "false", "0", "no")

    @staticmethod
    def threshold() -> float:
        return float(getattr(config, "SPEAKER_VERIFY_THRESHOLD", 0.28))

    @staticmethod
    def sample_path() -> str:
        path = (getattr(config, "TTS_CLONE_REFERENCE_PATH", "") or "").strip()
        return path if path and os.path.isfile(path) else ""

    def _warn_once(self, key: str, message: str):
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)

    # -- the model ---------------------------------------------------------
    def _embed(self, samples):
        """float32 16 kHz samples in [-1, 1] -> unit-length voice embedding."""
        if self._embedder is not None:
            return _unit(self._embedder(samples))
        import kaldi_native_fbank as knf  # noqa: PLC0415

        opts = knf.FbankOptions()
        opts.frame_opts.dither = 0.0
        opts.frame_opts.samp_freq = SAMPLE_RATE
        opts.frame_opts.snip_edges = True
        opts.mel_opts.num_bins = 80
        fbank = knf.OnlineFbank(opts)
        fbank.accept_waveform(SAMPLE_RATE, samples.astype(np.float32).tolist())
        fbank.input_finished()
        feats = np.array([fbank.get_frame(i) for i in range(fbank.num_frames_ready)], dtype=np.float32)
        feats -= feats.mean(axis=0, keepdims=True)
        out = self._session.run(None, {self._input_name: feats[None]})[0][0]
        return _unit(out)

    def _ready_model(self) -> bool:
        if self._embedder is not None:
            return True
        if self._session is not None:
            return True
        if self._load_failed:
            return False
        path = self.model_path()
        if not os.path.isfile(path):
            self._warn_once("model", f"Speaker check off: no speaker model at '{path}' "
                                     "(python scripts/download_speaker_model.py fetches it).")
            return False
        try:
            import kaldi_native_fbank  # noqa: F401, PLC0415
            import onnxruntime as ort  # noqa: PLC0415

            with open(path, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
            if digest != MODEL_SHA256:
                self._load_failed = True
                logger.warning(f"Speaker check off: '{path}' isn't the expected model (sha256 {digest[:12]}...).")
                return False
            options = ort.SessionOptions()
            options.intra_op_num_threads = 2
            self._session = ort.InferenceSession(path, sess_options=options, providers=["CPUExecutionProvider"])
            self._input_name = self._session.get_inputs()[0].name
            return True
        except Exception as e:
            self._load_failed = True
            logger.warning(f"Speaker check off: couldn't load the speaker model ({e}).")
            return False

    # -- the voiceprint ------------------------------------------------------
    def _sample_identity(self, path: str):
        return [os.path.abspath(path), int(os.path.getmtime(path)), int(os.path.getsize(path)), MODEL_FILE,
                _VOICEPRINT_VERSION]

    def _ready_voiceprint(self) -> bool:
        path = self.sample_path()
        if not path:
            self._warn_once("sample", "Speaker check off: no voice sample set up (Settings -> Avatar -> Build avatar).")
            return False
        key = self._sample_identity(path)
        if self._centroids is not None and self._sample_key == key:
            return True
        if self._load_voiceprint(key):
            return True
        started = time.monotonic()
        x = load_sample(path)
        win = SAMPLE_RATE * 3
        windows = [x[i:i + win] for i in range(0, max(1, len(x) - win + 1), SAMPLE_RATE)] or [x]
        centroids = []
        for c, (_, effect) in enumerate(_CONDITIONS):
            embs = [self._embed(w if effect is None else room(w, 1000 * (c + 1) + i, **effect))
                    for i, w in enumerate(windows)]
            centroids.append(_unit(np.mean(embs, axis=0)))
        self._centroids, self._sample_key, self._learned = np.stack(centroids), key, []
        self._save_voiceprint()
        logger.info(f"Speaker check: voiceprint built from '{path}' ({len(windows)} windows, "
                    f"{time.monotonic() - started:.1f}s).")
        return True

    def _load_voiceprint(self, key) -> bool:
        path = self.voiceprint_path()
        if not os.path.isfile(path):
            return False
        try:
            with np.load(path, allow_pickle=False) as data:
                if [str(v) for v in data["sample_key"].tolist()] != [str(v) for v in key]:
                    return False
                self._centroids = data["centroids"].astype(np.float32)
                learned = data["learned"].astype(np.float32)
            self._learned = [row for row in learned]
            self._sample_key = key
            return True
        except Exception as e:
            logger.warning(f"Speaker check: couldn't read the saved voiceprint ({e}) - rebuilding it.")
            return False

    def _save_voiceprint(self):
        path = self.voiceprint_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            learned = np.stack(self._learned) if self._learned else np.zeros((0, self._centroids.shape[1]), np.float32)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                np.savez(f, sample_key=np.array([str(v) for v in self._sample_key]),
                         centroids=self._centroids, learned=learned)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"Speaker check: couldn't save the voiceprint ({e}).")

    def _targets(self):
        targets = list(self._centroids)
        if self._learned:
            targets.append(_unit(np.mean(self._learned, axis=0)))
        return targets

    # -- public ---------------------------------------------------------------
    def ready(self) -> bool:
        """Model and voiceprint both usable (loads/builds them if needed)."""
        if not HAS_NUMPY or not self.enabled():
            return False
        with self._lock:
            try:
                ok = self._ready_model() and self._ready_voiceprint()
            except Exception as e:
                self._warn_once("ready", f"Speaker check off: {e}")
                return False
            if ok and self._announced != self._sample_key:
                self._announced = self._sample_key
                logger.info(f"Speaker check on: only the voice in '{self.sample_path()}' counts "
                            f"(+{len(self._learned)} learned, threshold {self.threshold():.2f}).")
            return ok

    def warm_up(self):
        """Loads the model and builds/loads the voiceprint on a background
        thread at start-up, so the first utterance isn't the one that pays."""
        thread = threading.Thread(target=self.ready, name="SpeakerID-WarmUp", daemon=True)
        thread.start()
        return thread

    def check(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE):
        """The owner-or-not verdict for one utterance (16-bit mono PCM), or
        None when there's nothing to compare against (no model, no sample,
        turned off, too short) - callers then carry on exactly as before."""
        if not self.ready():
            return None
        try:
            x = pcm16_to_float(pcm16)
            if int(sample_rate) != SAMPLE_RATE:
                from scipy.signal import resample_poly  # noqa: PLC0415
                g = np.gcd(int(sample_rate), SAMPLE_RATE)
                x = resample_poly(x, SAMPLE_RATE // g, int(sample_rate) // g).astype(np.float32)
            if len(x) < MIN_SECONDS * SAMPLE_RATE:
                return None
            with self._lock:
                emb = self._embed(x)
                score = max(float(emb @ t) for t in self._targets())
            return SpeakerCheck(score, score >= self.threshold(), emb)
        except Exception as e:
            self._warn_once("check", f"Speaker check failed ({e}) - letting audio through unchecked.")
            return None

    def learn(self, check) -> bool:
        """Adds a clearly-the-owner utterance (heard through this PC's mic) to
        the voiceprint, so it gets better at the owner's voice as Wave hears
        it. Only for scores >= LEARN_SCORE; keeps the newest MAX_LEARNED."""
        if check is None or check.embedding is None or check.score < LEARN_SCORE:
            return False
        with self._lock:
            if self._centroids is None:
                return False
            self._learned.append(np.asarray(check.embedding, dtype=np.float32))
            del self._learned[:-MAX_LEARNED]
            self._save_voiceprint()
        return True

    def reset(self):
        """Forget the voiceprint (the voice sample changed)."""
        with self._lock:
            self._centroids, self._sample_key, self._learned = None, None, []


speaker_verifier = SpeakerVerifier()
