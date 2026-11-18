"""
voice/wakeword.py — Tier 11: an acoustic wake-word gate (openWakeWord), so
Wave no longer has to TRANSCRIBE everything it hears while asleep just to
look for "hey wave" in the text.

Before this, ui/workers.py's mic loop sent every phrase captured while
SLEEPING through full speech recognition and then did a substring test on
the transcript. That meant everything said near the mic was transcribed -
by Google's cloud service whenever local faster-whisper wasn't available -
just to decide whether to wake up. With a model loaded here,
SpeechReceiver.listen(wake_gate=...) runs only this small local model on a
sleeping-mode phrase, and only when it fires is the phrase transcribed (so
"Hey Jarvis, open Chrome" still wakes Wave AND runs the command).

Which model
-----------
openWakeWord needs a trained model per phrase:

* Wave ships its own "Hey Wave" model, voice_models/wakewords/hey_wave.onnx,
  trained by scripts/train_wake_word.py on synthetic speech only (MIT, no
  openWakeWord pretrained model or data in it) - see docs/WAKE_WORD.md and
  the model card hey_wave.json next to it (recommended threshold, measured
  false accepts / false rejects).
* WAKE_WORD_MODEL_PATH - any other custom model file (.onnx).
* WAKE_WORD_MODEL_NAME - one of openWakeWord's pretrained phrases
  ("hey_jarvis", "alexa", "hey_mycroft", "hey_rhasspy"), downloaded on
  first use when WAKE_WORD_AUTO_DOWNLOAD is on. NOTE the license: those
  pretrained models are CC BY-NC-SA 4.0 (non-commercial), so Wave never
  bundles them - they're only fetched on the user's own machine when the
  user picks one.

openWakeWord also needs its two small feature models (melspectrogram +
speech embedding). Those are fetched from openWakeWord's own releases on
first use into <data dir>/voice_models/openwakeword/ - a writable folder
that survives restarts even for the one-file .exe - and are not bundled.

Two-stage check: a detection is followed by transcription anyway (to get
the command said in the same breath), so a weak detection (score below
WAKE_WORD_CONFIRM_BELOW) is only accepted if the transcript also starts
with something like the wake phrase - "hey wave", "hey way", "hey wait"
... Most false accepts (a TV saying "hey Dave, wait") fail that check.

WAKE_WORD_ENGINE: "auto" (default) uses openWakeWord when it's installed
and a model is configured or present; otherwise the transcript-based
fallback in voice/commands.py is used, exactly as before. "transcript"
forces that fallback; "openwakeword" insists on the model (and logs loudly
if it can't load, then still falls back rather than going deaf).

Always inference_framework="onnx": openWakeWord's own default is "tflite",
which isn't available on Windows.

Never raises out of detect(): any failure means "no wake word" for that
phrase, and a model that failed to load is never retried this session.
"""
import glob
import importlib.util
import json
import logging
import os
import re
import threading
from typing import Optional

from settings.config import config
from voice.commands import looks_like_wake

logger = logging.getLogger("WakeWord")

try:
    import numpy as np
    HAS_NUMPY = True
except Exception:  # pragma: no cover - numpy ships with faster-whisper anyway
    np = None
    HAS_NUMPY = False

# openwakeword pulls in onnxruntime/scipy on import - only paid for when a
# wake model is actually used (the first time Wave goes to sleep), not at
# app start-up. find_spec() is a cheap "is it installed" check.
try:
    HAS_OPENWAKEWORD = importlib.util.find_spec("openwakeword") is not None
except (ImportError, ValueError):
    HAS_OPENWAKEWORD = False
_OWWModel = None


def _model_class():
    global _OWWModel
    if _OWWModel is None:
        from openwakeword.model import Model  # noqa: PLC0415
        _OWWModel = Model
    return _OWWModel


SAMPLE_RATE = 16000
FRAME_SAMPLES = 1280          # 80 ms - the frame size openWakeWord's models are trained on
_LEAD_IN_S = 1.0              # silence fed first, so the feature window is full when speech starts
_TAIL_S = 0.5                 # and after, so a wake word right at the end still gets scored

# Spoken phrase for each pretrained model, used to strip it from the
# transcript ("hey jarvis open chrome" -> "open chrome").
PRETRAINED_PHRASES = {
    "hey_jarvis": "hey jarvis",
    "alexa": "alexa",
    "hey_mycroft": "hey mycroft",
    "hey_rhasspy": "hey rhasspy",
}


def default_custom_model_path() -> str:
    """The bundled "Hey Wave" model: next to the code when running from
    source, inside the unpacked bundle for a PyInstaller build."""
    for root in (getattr(config, "APP_DIR", None), config.PROJECT_ROOT):
        if root:
            path = os.path.join(root, "voice_models", "wakewords", "hey_wave.onnx")
            if os.path.isfile(path):
                return path
    return os.path.join(config.PROJECT_ROOT, "voice_models", "wakewords", "hey_wave.onnx")


def feature_models_dir() -> str:
    """Writable home for openWakeWord's feature models (and any pretrained
    wake model the user picks)."""
    return os.path.join(config.DATA_DIR, "voice_models", "openwakeword")


def _package_models_dir():
    """openwakeword's own resources/models folder, or None."""
    try:
        spec = importlib.util.find_spec("openwakeword")
        if spec is not None and spec.origin:
            return os.path.join(os.path.dirname(spec.origin), "resources", "models")
    except (ImportError, ValueError):
        pass
    return None


def _find_feature_models():
    """(melspectrogram.onnx, embedding_model.onnx) from the first folder
    that has both: Wave's own feature_models_dir(), then openwakeword's
    package resources (where its own download_models() puts them). None if
    neither has them."""
    for folder in (feature_models_dir(), _package_models_dir()):
        if not folder:
            continue
        mel = os.path.join(folder, "melspectrogram.onnx")
        emb = os.path.join(folder, "embedding_model.onnx")
        if os.path.isfile(mel) and os.path.isfile(emb):
            return mel, emb
    return None


def model_card(model_path: str) -> dict:
    """The JSON card scripts/train_wake_word.py writes next to a model
    (hey_wave.json), or {} if there isn't one / it can't be read."""
    card_path = os.path.splitext(model_path or "")[0] + ".json"
    try:
        with open(card_path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def smoothed_peak(frame_scores, k: int = 1) -> float:
    """max over t of min(frame_scores[t : t + k]) - the best score held for
    k consecutive frames (k = 1: the plain maximum)."""
    xs = list(frame_scores or ())
    if not xs:
        return 0.0
    k = max(1, int(k or 1))
    if len(xs) < k:
        return 0.0
    return max(min(xs[i:i + k]) for i in range(len(xs) - k + 1))


class WakeWordGate:
    """Loads (lazily, once) and runs one openWakeWord model over captured
    phrases. Construct it freely - nothing heavy happens until the first
    detect()/available check."""

    def __init__(self, engine: str = None, model_path: str = None, model_name: str = None,
                 threshold: float = None, phrase: str = None, auto_download: bool = None):
        self.engine = (engine or getattr(config, "WAKE_WORD_ENGINE", "auto") or "auto").lower()
        self.model_path = model_path if model_path is not None else getattr(config, "WAKE_WORD_MODEL_PATH", "")
        self.model_name = model_name if model_name is not None else getattr(config, "WAKE_WORD_MODEL_NAME", "")
        self._threshold = threshold      # None: WAKE_WORD_THRESHOLD, else the model card's recommendation
        self.auto_download = (auto_download if auto_download is not None
                              else getattr(config, "WAKE_WORD_AUTO_DOWNLOAD", True))
        self._phrase = phrase if phrase is not None else getattr(config, "WAKE_WORD_PHRASE", "")
        self._model = None
        self._load_failed = False
        self._lock = threading.Lock()
        self.last_score = 0.0
        self._confirm_below = None       # None: WAKE_WORD_CONFIRM_BELOW, else the model card's
        self._card = (None, {})

    def _model_card(self) -> dict:
        """The resolved model's card, read once per model path."""
        ref = self._resolved_model_ref()
        if not ref or not os.path.isfile(ref):
            return {}
        if self._card[0] != ref:
            self._card = (ref, model_card(ref))
        return self._card[1]

    @property
    def confirm_below(self) -> float:
        """Detections scoring below this must also LOOK like the wake phrase
        in the transcript (see confirm()). An explicit value wins
        (attribute, then WAKE_WORD_CONFIRM_BELOW in .env); otherwise the
        model card's - a model trained as a "prefilter" asks for 1.0, i.e.
        every detection is transcript-checked; otherwise 0.85."""
        if self._confirm_below is not None:
            return float(self._confirm_below)
        if getattr(config, "WAKE_WORD_CONFIRM_BELOW_SET", False):
            return float(getattr(config, "WAKE_WORD_CONFIRM_BELOW", 0.85))
        card = self._model_card().get("confirm_below")
        if isinstance(card, (int, float)) and 0.0 <= card <= 1.0:
            return float(card)
        return float(getattr(config, "WAKE_WORD_CONFIRM_BELOW", 0.85))

    @confirm_below.setter
    def confirm_below(self, value: float):
        self._confirm_below = value

    @property
    def smoothing_frames(self) -> int:
        """How many consecutive 80 ms frames the score must stay above the
        threshold (the model card's smoothing_frames; 1 = a single frame,
        openWakeWord's own default). A real "hey wave" holds the score up
        for several frames; most false triggers are one-frame spikes."""
        k = self._model_card().get("smoothing_frames")
        return int(k) if isinstance(k, int) and 1 <= k <= 8 else 1

    @property
    def threshold(self) -> float:
        """An explicit value wins (constructor, then WAKE_WORD_THRESHOLD in
        .env); otherwise the trained model's own recommended threshold from
        its card; otherwise 0.5."""
        if self._threshold is not None:
            return float(self._threshold)
        if getattr(config, "WAKE_WORD_THRESHOLD_SET", False):
            return float(getattr(config, "WAKE_WORD_THRESHOLD", 0.5))
        rec = self._model_card().get("recommended_threshold")
        if isinstance(rec, (int, float)) and 0.0 < rec < 1.0:
            return float(rec)
        return float(getattr(config, "WAKE_WORD_THRESHOLD", 0.5))

    @threshold.setter
    def threshold(self, value: float):
        self._threshold = value

    # ---- which model, if any -------------------------------------------
    def _resolved_model_ref(self) -> Optional[str]:
        """A path or a pretrained name to load, or None if no model is
        configured/present."""
        if self.model_path:
            return self.model_path
        custom = default_custom_model_path()
        if os.path.isfile(custom):
            return custom
        if self.model_name:
            return self.model_name
        return None

    @property
    def phrase(self) -> str:
        """The spoken phrase this model listens for, normalised for
        stripping from a transcript."""
        if self._phrase:
            return self._phrase
        ref = self._resolved_model_ref() or ""
        card_phrase = self._model_card().get("phrase")
        if isinstance(card_phrase, str) and card_phrase.strip():
            return card_phrase.strip().lower()
        name = os.path.splitext(os.path.basename(ref))[0].lower()
        for key, spoken in PRETRAINED_PHRASES.items():
            if name.startswith(key):
                return spoken
        return name.replace("_", " ").split(" v")[0].strip() or "hey wave"

    def wanted(self) -> bool:
        """Whether the config asks for the acoustic gate at all."""
        if self.engine == "transcript":
            return False
        if self.engine == "openwakeword":
            return True
        return self._resolved_model_ref() is not None          # "auto"

    @property
    def available(self) -> bool:
        """True only if the gate is wanted AND its model is loaded (loading
        it now if needed)."""
        if not self.wanted():
            return False
        return self._ensure_model() is not None

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if self._load_failed:
            return None
        with self._lock:
            if self._model is not None or self._load_failed:
                return self._model
            ref = self._resolved_model_ref()
            if not HAS_OPENWAKEWORD or not HAS_NUMPY or ref is None:
                if self.engine == "openwakeword":
                    logger.warning("WAKE_WORD_ENGINE=openwakeword but openWakeWord/numpy isn't installed or no "
                                   "model is configured - falling back to transcript-based wake words.")
                self._load_failed = True
                return None
            try:
                self._model = self._load(ref)
                logger.info(f"Wake word model loaded: {ref} (phrase '{self.phrase}', threshold {self.threshold})")
            except Exception as e:
                logger.warning(f"Wake word model '{ref}' failed to load - staying transcript-based: {e}")
                self._load_failed = True
                self._model = None
            return self._model

    def _load(self, ref: str):
        """openwakeword.Model for `ref` (a path or a pretrained name). The
        feature models come from feature_models_dir() - or from
        openwakeword's own package folder if someone already ran its
        download there - and are downloaded into feature_models_dir() only
        when neither has them (and WAKE_WORD_AUTO_DOWNLOAD allows it)."""
        target = feature_models_dir()
        is_path = os.path.sep in ref or "/" in ref or ref.endswith((".onnx", ".tflite"))
        features = _find_feature_models()
        missing = features is None
        if not is_path:
            missing = missing or not glob.glob(os.path.join(target, f"{ref}*.onnx"))
        if missing and self.auto_download:
            from openwakeword.utils import download_models  # noqa: PLC0415
            os.makedirs(target, exist_ok=True)
            # A name that matches no wake model = feature models only.
            download_models(model_names=["__feature_models_only__"] if is_path else [ref], target_directory=target)
            features = _find_feature_models()
        if not is_path:
            found = sorted(glob.glob(os.path.join(target, f"{ref}*.onnx")))
            if not found:
                raise FileNotFoundError(f"pretrained wake model '{ref}' not found in {target}")
            ref = found[-1]
        kwargs = {}
        if features is not None:
            kwargs = {"melspec_model_path": features[0], "embedding_model_path": features[1]}
        return _model_class()(wakeword_models=[ref], inference_framework="onnx", **kwargs)

    def confirm(self, transcript: str, score: float = None) -> bool:
        """Second stage, after the phrase has been transcribed: a strong
        detection stands on its own; a weaker one needs the transcript to
        start with something like the wake phrase. Never rejects when the
        check is switched off (WAKE_WORD_CONFIRM_BELOW=0)."""
        score = self.last_score if score is None else score
        if score >= self.confirm_below:
            return True
        return looks_like_wake(transcript)

    # ---- scoring --------------------------------------------------------
    def score_pcm16(self, pcm: bytes) -> float:
        """Wake score of a whole phrase of 16 kHz mono int16 PCM: the best
        score the model held for `smoothing_frames` consecutive frames.
        0.0 on any failure."""
        model = self._ensure_model()
        if model is None or not pcm:
            return 0.0
        try:
            k = self.smoothing_frames
            samples = np.frombuffer(pcm, dtype=np.int16)
            samples = np.concatenate([
                np.zeros(int(SAMPLE_RATE * _LEAD_IN_S), dtype=np.int16),
                samples,
                np.zeros(int(SAMPLE_RATE * _TAIL_S), dtype=np.int16),
            ])
            frame_scores = []
            with self._lock:
                model.reset()
                for start in range(0, len(samples) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                    scores = model.predict(samples[start:start + FRAME_SAMPLES])
                    frame_scores.append(max((float(v) for v in scores.values()), default=0.0) if scores else 0.0)
            best = smoothed_peak(frame_scores, k)
            self.last_score = best
            return best
        except Exception as e:
            logger.debug(f"Wake word scoring failed - treating as no wake word: {e}")
            return 0.0

    def detect_pcm16(self, pcm: bytes) -> bool:
        return self.score_pcm16(pcm) >= self.threshold

    def detect(self, audio) -> bool:
        """`audio` is a speech_recognition AudioData (anything with
        get_raw_data(convert_rate=, convert_width=))."""
        try:
            pcm = audio.get_raw_data(convert_rate=SAMPLE_RATE, convert_width=2)
        except Exception as e:
            logger.debug(f"Couldn't convert captured audio for the wake word model: {e}")
            return False
        return self.detect_pcm16(pcm)


_gate: Optional[WakeWordGate] = None
_gate_lock = threading.Lock()


def get_wake_gate() -> WakeWordGate:
    """The shared gate built from settings/config.py."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = WakeWordGate()
        return _gate


def reset_wake_gate() -> None:
    """Forget the shared gate (after Settings changes the wake word), so the
    next get_wake_gate() reads the new configuration."""
    global _gate
    with _gate_lock:
        _gate = None
