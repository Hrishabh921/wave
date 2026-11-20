"""
scripts/download_speaker_model.py — Tier 32: fetches the speaker model that
Wave's "only your voice" check uses (voice/speaker_id.py): 3D-Speaker's CAM++
(Chinese+English, ~27 MB, from sherpa-onnx's speaker-model releases), and
installs it only if it matches the pinned SHA-256.

    python scripts/download_speaker_model.py

It also needs `pip install kaldi-native-fbank` (requirements-voice-local.txt)
and a voice sample (Settings -> Avatar -> Build avatar).
"""
import hashlib
import os
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from voice import speaker_id  # noqa: E402


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    target = speaker_id.default_model_path()
    if os.path.isfile(target) and _sha256(target) == speaker_id.MODEL_SHA256:
        print(f"Already installed: {target}")
        return 0
    os.makedirs(os.path.dirname(target), exist_ok=True)
    print(f"Downloading {speaker_id.MODEL_URL}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), suffix=".part")
    os.close(fd)
    try:
        urllib.request.urlretrieve(speaker_id.MODEL_URL, tmp)
        digest = _sha256(tmp)
        if digest != speaker_id.MODEL_SHA256:
            print(f"Checksum mismatch (got {digest}) - not installing it.")
            return 1
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    print(f"Installed {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
