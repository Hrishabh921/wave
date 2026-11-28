"""
voice/whisper_device.py - Tier 55: Whisper on the GPU, when it really works.

Measured 2026-11-27 on 320 spoken commands (golden-set commands, two Indian
edge-tts voices each, clean and with noise, decoded exactly like
voice/stt.py): the "medium" model on an RTX 3050 makes half the word errors
of "base" on the CPU (21.5% vs 44%) in about the same time per command
(0.35-0.42 s vs 0.47 s), with 1.3 GB of GPU memory (int8_float16). On the
CPU, medium takes 3.8 s per command - slower than the speech itself - so the
CPU keeps its small model and the GPU gets the big one.

The GPU path has two traps, both handled here:

- faster-whisper's engine (CTranslate2) needs NVIDIA's CUDA 12 libraries
  (cuBLAS, cuDNN 9), which Wave's own environment doesn't ship. They often
  are on the PC anyway - the LivePortrait environment's CUDA build of torch
  ships both, so do pip's nvidia-cublas-cu12/nvidia-cudnn-cu12 wheels and a
  CUDA toolkit. CTranslate2 opens them by name with plain LoadLibrary, which
  ignores os.add_dll_directory(); loading each one once by its full path is
  enough (Windows reuses a DLL of the same name that's already loaded) and
  leaves PATH - and every program Wave starts - alone.
- A model loads on the GPU fine WITHOUT those libraries; the error only comes
  on the first decode ("Library cublas64_12.dll is not found"). So
  load_whisper_model() decodes half a second of silence before trusting the
  GPU, and otherwise falls back to the CPU model.
"""
from __future__ import annotations

import glob
import logging
import os
import site
import sys
from dataclasses import dataclass
from typing import Any, List, Optional

from settings.config import config

logger = logging.getLogger("WhisperDevice")

# What CTranslate2 opens on Windows. cuDNN finds its own sub-libraries
# (cudnn_ops64_9.dll, ...) next to cudnn64_9.dll, so they needn't be loaded here.
_NEEDED = ("cudart64_12.dll", "cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll")

_loaded: dict = {}          # DLL name -> the handle that keeps it loaded
_IS_WINDOWS = sys.platform == "win32"


@dataclass
class LoadedWhisper:
    """The model Wave transcribes with, and where/how it runs."""
    model: Any
    device: str             # "cuda" or "cpu"
    name: str
    compute_type: str
    min_confidence: float   # voice/transcript_filter's floor for this model


def _site_packages() -> List[str]:
    dirs = []
    try:
        dirs.extend(site.getsitepackages())
    except AttributeError:          # some embedded/virtualenv layouts
        pass
    dirs.append(os.path.join(sys.prefix, "Lib", "site-packages"))
    return dirs


def _python_env_roots(python_exe: str) -> List[str]:
    """The environment folder(s) a python.exe may belong to: venv/virtualenv
    (env\\Scripts\\python.exe) or conda (env\\python.exe)."""
    here = os.path.dirname(python_exe)
    return [os.path.dirname(here), here]


def find_cuda_dll_dirs() -> List[str]:
    """Folders on this PC that hold CUDA 12 / cuDNN 9 DLLs, best first."""
    candidates: List[str] = []
    for d in (config.CUDA_DLL_DIR or "").split(os.pathsep):
        if d.strip():
            candidates.append(d.strip())
    for base in _site_packages():
        candidates.extend(sorted(glob.glob(os.path.join(base, "nvidia", "*", "bin"))))
    lp_python = (getattr(config, "AVATAR_LIVE_PYTHON", "") or "").strip()
    if lp_python:
        for root in _python_env_roots(lp_python):
            candidates.append(os.path.join(root, "Lib", "site-packages", "torch", "lib"))
    if os.environ.get("CUDA_PATH"):
        candidates.append(os.path.join(os.environ["CUDA_PATH"], "bin"))
    out, seen = [], set()
    for d in candidates:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        if any(os.path.isfile(os.path.join(d, n)) for n in _NEEDED):
            out.append(d)
    return out


def preload_cuda_dlls(dirs: Optional[List[str]] = None, loader=None) -> List[str]:
    """Loads the CUDA 12 / cuDNN 9 DLLs by full path (Windows only), so that
    CTranslate2's later load-by-name finds them. Returns the paths loaded."""
    if not _IS_WINDOWS:
        return []
    if loader is None:
        import ctypes
        loader = ctypes.WinDLL
    dirs = find_cuda_dll_dirs() if dirs is None else dirs
    done = []
    for name in _NEEDED:
        if name in _loaded:
            continue
        for d in dirs:
            path = os.path.join(d, name)
            if not os.path.isfile(path):
                continue
            try:
                _loaded[name] = loader(path)
                done.append(path)
                break
            except OSError as e:
                logger.debug(f"Couldn't load {path}: {e}")
    return done


def _cpu_model(whisper_cls) -> LoadedWhisper:
    model = whisper_cls(config.FASTER_WHISPER_MODEL, device="cpu", compute_type=config.FASTER_WHISPER_COMPUTE_TYPE)
    return LoadedWhisper(model, "cpu", config.FASTER_WHISPER_MODEL, config.FASTER_WHISPER_COMPUTE_TYPE,
                         float(config.WHISPER_MIN_CONFIDENCE))


def _gpu_model(whisper_cls, np_mod) -> Optional[LoadedWhisper]:
    name, compute = config.FASTER_WHISPER_GPU_MODEL, config.FASTER_WHISPER_GPU_COMPUTE_TYPE
    if np_mod is None:              # no way to prove the GPU works
        return None
    try:
        preload_cuda_dlls()
        model = whisper_cls(name, device="cuda", compute_type=compute)
        # Loading worked; only a decode shows the GPU libraries are there.
        segments, _info = model.transcribe(np_mod.zeros(8000, dtype=np_mod.float32), beam_size=1,
                                           vad_filter=False, without_timestamps=True,
                                           condition_on_previous_text=False)
        list(segments)
    except Exception as e:
        logger.info(f"Whisper on the GPU isn't available ({type(e).__name__}: {e}) - using the CPU model.")
        return None
    return LoadedWhisper(model, "cuda", name, compute, float(config.WHISPER_GPU_MIN_CONFIDENCE))


def load_whisper_model(whisper_cls, np_mod) -> LoadedWhisper:
    """The GPU model when FASTER_WHISPER_DEVICE is "cuda"/"auto" and the GPU
    proves it works, else the CPU model. Raises only if the CPU model itself
    can't be built (the caller then stays on its non-Whisper fallback)."""
    if config.FASTER_WHISPER_DEVICE in ("cuda", "auto"):
        loaded = _gpu_model(whisper_cls, np_mod)
        if loaded is not None:
            return loaded
    return _cpu_model(whisper_cls)
