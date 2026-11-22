"""
voice/clone_xtts.py — Tier 31: XTTS-v2 speaking in the user's voice.

Used only inside voice/clone_worker.py's own process - never in Wave's (see
that module for why). XttsVoice works the voice latents out once per sample
file, with the same maths and the same config values as XTTS-v2's own
get_conditioning_latents()/synthesize(), except that the sample is read with
soundfile: since torchaudio 2.9, torchaudio.load() goes through torchcodec,
which needs FFmpeg's DLLs - a normal Windows PC doesn't have them, so XTTS's
own loader failed on every single sentence.
"""
import logging
import os

import numpy as np

logger = logging.getLogger("TTS.clone")

# What XttsVoice needs from the loaded model, and the generation settings
# XTTS-v2's own synthesize() reads from its config.
MODEL_METHODS = ("get_speaker_embedding", "get_gpt_cond_latents", "inference")
INFERENCE_KEYS = ("temperature", "length_penalty", "repetition_penalty", "top_k", "top_p")
COND_SAMPLE_RATE = 22050   # XTTS-v2's conditioning rate


def resolve_device(wanted: str, cuda_available: bool) -> str:
    """config.TTS_CLONE_DEVICE ("auto" / "cpu" / "cuda") resolved: the GPU
    only when asked for (or "auto") AND this PyTorch build can use it."""
    wanted = (wanted or "auto").strip().lower()
    if wanted == "cpu":
        return "cpu"
    if cuda_available:
        return "cuda"
    if wanted == "cuda":
        logger.warning("TTS_CLONE_DEVICE=cuda, but this PyTorch has no CUDA - voice cloning runs on the CPU.")
    return "cpu"


def load_voice_sample(path: str, sample_rate: int = COND_SAMPLE_RATE):
    """The voice sample as XTTS-v2 takes it: a [1, samples] float tensor at
    `sample_rate`, mono, clipped to [-1, 1] - what the model's own
    load_audio() returns, read with soundfile instead of torchaudio.load()."""
    import soundfile  # noqa: PLC0415  (a coqui-tts dependency)
    import torch  # noqa: PLC0415
    import torchaudio  # noqa: PLC0415

    data, rate = soundfile.read(path, dtype="float32", always_2d=True)
    audio = torch.from_numpy(np.ascontiguousarray(data.T))
    if audio.size(0) != 1:
        audio = audio.mean(dim=0, keepdim=True)
    if rate != sample_rate:
        audio = torchaudio.functional.resample(audio, rate, sample_rate)
    return audio.clamp_(-1.0, 1.0)


class XttsVoice:
    """A loaded XTTS-v2 model plus the user's voice: set_reference() once,
    then synthesize() each sentence."""

    def __init__(self, model, sample_rate: int = 24000, device: str = "cpu", loader=load_voice_sample):
        missing = [name for name in MODEL_METHODS if not callable(getattr(model, name, None))]
        if missing:
            raise TypeError(f"not an XTTS-v2 model (it has no {', '.join(missing)})")
        self.model = model
        self.sample_rate = int(sample_rate or 24000)
        self.device = device
        self._loader = loader
        self._key = None
        self._latents = None

    def set_reference(self, path: str) -> bool:
        """Works out (gpt conditioning latents, speaker embedding) for the
        sample at `path`. Returns False when that exact file (path + mtime +
        size) is already in use."""
        key = (path, os.path.getmtime(path), os.path.getsize(path))
        if key == self._key:
            return False
        cfg = self.model.config
        audio = self._loader(path, COND_SAMPLE_RATE)
        audio = audio[:, : COND_SAMPLE_RATE * int(getattr(cfg, "max_ref_len", 30))]
        if getattr(cfg, "sound_norm_refs", False):
            peak = float(audio.abs().max())
            if peak > 0:
                audio = audio / peak * 0.75
        speaker = self.model.get_speaker_embedding(audio, COND_SAMPLE_RATE)
        gpt = self.model.get_gpt_cond_latents(audio, COND_SAMPLE_RATE, length=int(getattr(cfg, "gpt_cond_len", 30)),
                                              chunk_length=int(getattr(cfg, "gpt_cond_chunk_len", 4)))
        self._key, self._latents = key, (gpt, speaker)
        return True

    def synthesize(self, text: str, lang: str):
        """`text` ("en" or "hi", already prepared by voice/text_prep.py) in
        the user's voice, as float32 samples at self.sample_rate. Long text
        is split at XTTS's own per-language limit."""
        if self._latents is None:
            raise RuntimeError("no voice sample set")
        cfg = self.model.config
        settings = {k: getattr(cfg, k) for k in INFERENCE_KEYS if hasattr(cfg, k)}
        out = self.model.inference(text, lang, self._latents[0], self._latents[1],
                                   enable_text_splitting=True, **settings)
        return np.asarray(out["wav"], dtype=np.float32)


MODEL_FILES = ("config.json", "model.pth", "vocab.json")


def cached_model_dir(model_name: str):
    """The folder `model_name` (coqui's "tts_models/multilingual/
    multi-dataset/xtts_v2") was already downloaded to - Hugging Face's
    cache - found WITHOUT the network, or None: not downloaded yet, its
    licence not accepted on this PC yet, or not a model coqui fetches from
    Hugging Face. coqui-tts's own TTS(model_name) asks huggingface.co about
    the model on every start, and first checks the licence file - asking
    with input() when it isn't there; either can hold the cloned voice up.
    Loading this folder by path does neither, so the licence is checked
    here the way coqui checks it (its tos_agreed.txt, or
    COQUI_TOS_AGREED=1)."""
    try:
        from huggingface_hub import snapshot_download  # noqa: PLC0415
        from TTS.utils.manage import ModelManager  # noqa: PLC0415

        model_type, lang, dataset, model = model_name.split("/")
        manager = ModelManager(progress_bar=False)
        item = manager.models_dict[model_type][lang][dataset][model]
        repo_id = item.get("repo_id")
        if not repo_id:
            return None
        if not ModelManager.tos_agreed(item, manager.output_prefix / f"{model_type}--{lang}--{dataset}--{model}"):
            return None
        folder = snapshot_download(repo_id, allow_patterns=item.get("allow"), ignore_patterns=item.get("ignore"),
                                   local_files_only=True)
    except Exception as e:
        logger.info(f"No local copy of {model_name} to load directly ({type(e).__name__}: {e}).")
        return None
    folder = os.path.abspath(folder)
    if all(os.path.isfile(os.path.join(folder, name)) for name in MODEL_FILES):
        return folder
    return None


def load_xtts(model_name: str, device: str = "auto") -> XttsVoice:
    """Loads XTTS-v2 through coqui-tts on the resolved device - straight
    from the local copy when there is one (cached_model_dir(): no network),
    else by name, which downloads it the very first time (~1.8GB)."""
    import torch  # noqa: PLC0415
    from TTS.api import TTS  # noqa: PLC0415

    resolved = resolve_device(device, torch.cuda.is_available())
    folder = cached_model_dir(model_name)
    if folder is not None:
        logger.info(f"Loading {model_name} from {folder} (no network).")
        engine = TTS(model_path=os.path.join(folder, "model.pth"), config_path=os.path.join(folder, "config.json"),
                     progress_bar=False)
    else:
        engine = TTS(model_name)
    if resolved != "cpu":
        engine = engine.to(resolved)
    synthesizer = engine.synthesizer
    return XttsVoice(synthesizer.tts_model, int(getattr(synthesizer, "output_sample_rate", 24000) or 24000),
                     resolved)
