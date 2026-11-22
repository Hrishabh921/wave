"""
voice/speaker_id.py — Tier 32: "is this the owner talking?"

Wave's mic hears everyone in the room - the TV, a video, other people. In
testing, a video's "Say something." was taken for a command and Wave started
talking on its own. Every utterance's VOICE is now checked against the
owner's voiceprint before anything else happens to it (voice/stt.py), so
other voices are dropped before they're even transcribed (no Whisper CPU
spent on them, nothing of theirs written to the log).

The voiceprint is the owner's voice training - ~30 s of them reading through
the mic Wave listens with (scripts/train_my_voice.py), and since Tier 33 also
recordings of them (train_my_voice.py --from-file: a phone recording, a voice
note) - plus what Wave learns from their own clearly-recognised commands.
Tier 32c: it never uses the cloned voice's sample - that sample needn't be the
owner (on the owner's laptop it is someone else's WhatsApp voice note, which
scores 0.16-0.26 against them). A phone recording of the owner themself
scores 0.50-0.68 against their mic training (Tier 33), so recordings of the
owner do count. Without voice training the check is simply off.

Model: 3D-Speaker's CAM++ (iic/speech_campplus_sv_zh_en_16k-common_advanced,
Chinese+English, 16 kHz, 192-d), as exported by sherpa-onnx, run with
onnxruntime - no torch in Wave's process. Features: 80-bin Kaldi fbank
(kaldi-native-fbank) on samples in [-1, 1], mean-normalised over the
utterance (the model's own metadata: normalize_samples=1,
feature_normalize_type=global-mean). scripts/download_speaker_model.py
fetches it.

Measured on the owner's laptop after voice training: their held-out 1.5 s
pieces scored 0.71-0.74; a video playing in the room (owner silent) scored at
most 0.21. Hence SPEAKER_VERIFY_THRESHOLD = 0.35. This check doesn't stop
Wave hearing its own voice - AEC and the "Wave is talking" rules still do.
"""
import hashlib
import logging
import os
import threading

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
# Below ~1 s a voice embedding says too little (pieces of 0.5-0.8 s scored
# anywhere from -0.1 to 0.5 in calibration). Shorter utterances (a lone
# "Baby") pass unchecked, so the wake word is never missed; what follows is
# checked.
MIN_SECONDS = 1.0
# Only clearly-the-owner utterances refine it (a video scored <= 0.21) - and
# "clearly" is judged against the voice training alone (Tier 33), never with
# the help of what was learned before, so the learned set can't drift towards
# another voice one borderline utterance at a time.
LEARN_SCORE = 0.40
MAX_LEARNED = 40
# 2 = voice training ("mic", since Tier 33 also "recordings") + learned
# (1 also held a voice sample's centroids)
_VOICEPRINT_VERSION = 2
TRAINING_SOURCES = ("mic", "recordings")


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
        self._loaded = False               # voiceprint.npz read yet?
        self._mic = []                     # the owner's voice training through this mic
        self._recordings = []              # ... and from recordings of them (Tier 33)
        self._learned = []                 # from their own clearly-recognised commands
        self._warned = set()
        self._announced = None             # the training size the "on" line was logged for

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
        return float(getattr(config, "SPEAKER_VERIFY_THRESHOLD", 0.35))

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
    # Saved in avatar_rig/voiceprint.npz: the voice training ("mic", and
    # since Tier 33 "recordings") and what was learned. Older files also held
    # a voice sample's centroids; those are ignored now and dropped on the
    # next save.
    def _load_voiceprint(self):
        self._loaded = True
        path = self.voiceprint_path()
        if not os.path.isfile(path):
            return
        try:
            with np.load(path, allow_pickle=False) as data:
                def rows(name):
                    return [row for row in data[name].astype(np.float32)] if name in data.files else []
                self._mic, self._recordings, self._learned = rows("mic"), rows("recordings"), rows("learned")
        except Exception as e:
            logger.warning(f"Speaker check: couldn't read the saved voiceprint ({e}) - starting a new one.")
            self._mic, self._recordings, self._learned = [], [], []

    def _save_voiceprint(self):
        path = self.voiceprint_path()

        def rows(items):
            if items:
                return np.stack(list(items)).astype(np.float32)
            return np.zeros((0, self._dim()), np.float32)

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                np.savez(f, version=np.array(_VOICEPRINT_VERSION), mic=rows(self._mic),
                         recordings=rows(self._recordings), learned=rows(self._learned))
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"Speaker check: couldn't save the voiceprint ({e}).")

    def _dim(self) -> int:
        for items in (self._mic, self._recordings, self._learned):
            if items:
                return int(np.asarray(items[0]).shape[-1])
        return 192

    def _training(self):
        """Every piece of the voice training: through the mic, and from
        recordings of the owner."""
        return list(self._mic) + list(self._recordings)

    def _training_target(self):
        return _unit(np.mean(self._training(), axis=0))

    def _ready_voiceprint(self) -> bool:
        if not self._loaded:
            self._load_voiceprint()
        if not self._training():
            self._warn_once("training", "Speaker check off: no voice training yet - run "
                                        "python scripts/train_my_voice.py (30 s of you through this mic).")
            return False
        return True

    def _targets(self):
        """The voice training as one average (mic and recordings together),
        and what was learned as another."""
        targets = [self._training_target()]
        if self._learned:
            targets.append(_unit(np.mean(self._learned, axis=0)))
        return targets

    # -- public ---------------------------------------------------------------
    def ready(self) -> bool:
        """Model and voiceprint both usable (loads them if needed)."""
        if not HAS_NUMPY or not self.enabled():
            return False
        with self._lock:
            try:
                ok = self._ready_model() and self._ready_voiceprint()
            except Exception as e:
                self._warn_once("ready", f"Speaker check off: {e}")
                return False
            size = (len(self._mic), len(self._recordings))
            if ok and self._announced != size:
                self._announced = size
                recordings = f" + {len(self._recordings)} from recordings of you" if self._recordings else ""
                logger.info(f"Speaker check on: only the voice from your voice training counts "
                            f"({len(self._mic)} pieces through the mic{recordings}, +{len(self._learned)} learned, "
                            f"threshold {self.threshold():.2f}).")
            return ok

    def warm_up(self):
        """Loads the model and the voiceprint on a background thread at
        start-up, so the first utterance isn't the one that pays."""
        thread = threading.Thread(target=self.ready, name="SpeakerID-WarmUp", daemon=True)
        thread.start()
        return thread

    def check(self, pcm16: bytes, sample_rate: int = SAMPLE_RATE):
        """The owner-or-not verdict for one utterance (16-bit mono PCM), or
        None when there's nothing to compare against (no model, no voice
        training, turned off, too short) - callers then carry on exactly as
        before."""
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
        the voiceprint, so it keeps up with the owner's voice as Wave hears
        it. Only when it scores >= LEARN_SCORE against the voice training
        itself (Tier 33: before, a score earned through the learned part
        counted too - and on the owner's laptop that let in a piece closer to
        someone else's voice than to theirs); keeps the newest MAX_LEARNED."""
        if check is None or check.embedding is None or check.score < LEARN_SCORE:
            return False
        with self._lock:
            if not self._loaded:
                self._load_voiceprint()
            if not self._training():
                return False
            embedding = np.asarray(check.embedding, dtype=np.float32)
            if float(_unit(embedding) @ self._training_target()) < LEARN_SCORE:
                return False
            self._learned.append(embedding)
            del self._learned[:-MAX_LEARNED]
            self._save_voiceprint()
        return True

    def _vet_learned(self) -> int:
        """Drops learned pieces the (new) voice training doesn't clearly
        agree with - the same test learn() applies. Returns how many."""
        if not self._learned or not self._training():
            return 0
        target = self._training_target()
        kept = [e for e in self._learned if float(_unit(e) @ target) >= LEARN_SCORE]
        dropped = len(self._learned) - len(kept)
        self._learned = kept
        return dropped

    # -- voice training (scripts/train_my_voice.py) -------------------------
    def window_embeddings(self, x, seconds: float = 3.0, hop: float = 1.5):
        """Embeddings of overlapping `seconds`-long windows of `x` (16 kHz
        float32 speech). Raises if the model can't be loaded."""
        with self._lock:
            if not self._ready_model():
                raise RuntimeError("the speaker model isn't available")
            n, step = int(seconds * SAMPLE_RATE), int(hop * SAMPLE_RATE)
            windows = [x[i:i + n] for i in range(0, max(1, len(x) - n + 1), step)] or [x]
            return [self._embed(w) for w in windows]

    def set_training(self, embeddings, source: str = "mic") -> int:
        """Replaces one part of the voice training - "mic" (the owner reading
        through this PC's mic) or "recordings" (recordings of them) - with
        these embeddings, re-checks what was learned against the new
        training, and saves. Returns how many learned pieces were dropped."""
        if source not in TRAINING_SOURCES:
            raise ValueError(f"unknown voice training source {source!r}")
        with self._lock:
            if not self._loaded:
                self._load_voiceprint()
            pieces = [np.asarray(e, dtype=np.float32) for e in embeddings]
            if source == "mic":
                self._mic = pieces
            else:
                self._recordings = pieces
            dropped = self._vet_learned()
            self._save_voiceprint()
            return dropped

    def training_size(self) -> dict:
        """{"mic": n, "recordings": n, "learned": n} - after loading."""
        with self._lock:
            if not self._loaded:
                self._load_voiceprint()
            return {"mic": len(self._mic), "recordings": len(self._recordings), "learned": len(self._learned)}

    def training_scores(self, embeddings, source: str = "mic"):
        """How these embeddings score against one part of the saved training
        (an empty list when that part is empty) - train_my_voice.py uses it
        to check a recording is the same voice as the mic training."""
        with self._lock:
            if not self._loaded:
                self._load_voiceprint()
            part = self._mic if source == "mic" else self._recordings
            if not part:
                return []
            target = _unit(np.mean(part, axis=0))
            return [float(_unit(e) @ target) for e in embeddings]

    def reset(self):
        """Forget the in-memory voiceprint (reloaded from disk on next use)."""
        with self._lock:
            self._loaded, self._mic, self._recordings, self._learned, self._announced = False, [], [], [], None


speaker_verifier = SpeakerVerifier()
