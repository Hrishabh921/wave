# Only your voice (speaker check) - Tier 32

Wave's mic hears everyone in the room: the TV, a video, other people. In testing, a
video saying "Say something." was taken for a command and Wave started talking on its
own. Since Tier 32, every utterance's **voice** is checked against the owner's
voiceprint before anything else happens to it (`voice/stt.py` →
`voice/speaker_id.py`). Anyone else is dropped before they're even transcribed: no
Whisper CPU spent on them, nothing they said written to the log.

## How it decides

- **The voiceprint** comes from the owner's voice training through this PC's mic (see
  below) and from the voice sample the owner gave for Wave's cloned voice
  (Settings → Avatar → Build avatar; `TTS_CLONE_REFERENCE_PATH`,
  `avatar_rig/voice_reference.wav`). It holds three averages of that sample: as
  recorded, and as if heard across a room by a laptop mic (mild and harsh: reverb, band
  limit, noise). The sample was a phone recording; Wave hears through the PC's mic.
- **It learns.** An utterance that clearly is the owner (score ≥ 0.40) adds itself to
  the voiceprint (the newest 40 are kept), so Wave gets better at the owner's voice as
  heard through this mic. Never while Wave itself is talking - its cloned voice sounds
  like the owner.
- **A score** is the best cosine similarity to any of those averages (or to what was
  learned). At or above `SPEAKER_VERIFY_THRESHOLD` (0.28) it's the owner.
- **Short utterances (< 1 s) aren't checked** - a voice says too little in that time -
  so a lone "Baby" always wakes Wave. Whatever follows is checked.
- **Not while Wave is talking**: its echo is in that audio, and the owner's "ruk jao"
  must always get through. The score is still logged.

The log shows every decision:

```
Speaker check: 0.62 (you)
Not your voice (speaker score 0.12) - ignored, not transcribed
```

## Setting it up

```powershell
wave_env\Scripts\pip install kaldi-native-fbank==1.23.3
wave_env\Scripts\python scripts\download_speaker_model.py
wave_env\Scripts\python scripts\train_my_voice.py
```

### Voice training through this PC's mic (Tier 32b) - do this

The voice sample is usually a phone recording, and a phone and a laptop mic hear the
same person very differently. On the owner's laptop their own voice through the mic
scored only **0.16-0.19** against their WhatsApp sample - below the 0.28 threshold, so
Wave would have ignored its owner. `scripts/train_my_voice.py` fixes that: Wave closed,
TV off, it beeps once and records you reading five lines (30 s), beeps twice and
records 20 s of a video with you silent (to see how other voices score through the same
mic), and beeps three times when done. Pieces that don't sound like the rest are
dropped, and nothing is saved if the recording isn't one clear voice. After training,
the owner's held-out 1.5 s pieces scored **0.71-0.74**. `--no-video` skips the video
part; `--reset` forgets the training and everything learned.

The video part only helps when the video plays on another device (a TV, a phone): this
laptop's mic array removes the laptop's own speaker sound.

The model is 3D-Speaker's CAM++ (`iic/speech_campplus_sv_zh_en_16k-common_advanced`,
Chinese+English, 16 kHz, 192-d; the 3D-Speaker project is Apache-2.0) as exported by
sherpa-onnx, ~27 MB, checked against a pinned SHA-256. It runs with onnxruntime (no
torch in Wave's process) and takes ~30 ms per utterance on the CPU. The voiceprint is
built once (~3 s) and cached in `avatar_rig/voiceprint.npz`; a new voice sample rebuilds
it, and deleting the file forgets what was learned.

| `.env` | default | |
|---|---|---|
| `SPEAKER_VERIFY` | `auto` | on when the model and a voice sample are there; `off` = every voice counts |
| `SPEAKER_VERIFY_THRESHOLD` | `0.28` | lower lets more through (you *and* others), higher is stricter |
| `SPEAKER_MODEL_PATH` | blank | blank = `voice_models/speaker/<model>` |

## Calibration

On the owner's 21 s sample (10 s enrolled, the rest cut into pieces) against 40 clips
from 10 other voices (edge-tts: Indian English, Hindi, US, UK, AU; male and female),
each also through the simulated room:

| | owner (lowest) | other voices (highest) |
|---|---|---|
| 1 s, harsh room | 0.29 | 0.28 |
| 1.5 s, harsh room | 0.48 | 0.25 |
| clean | ≥ 0.61 | 0.15 |

Below 1 s the owner's pieces scored anywhere from -0.1 to 0.5 - hence no check there.
The other model tried (WeSpeaker ResNet34) fell apart through the simulated room
(owner down to -0.01) and was dropped. Playing clips through the laptop's speaker to its
own mic wasn't possible as a test: this laptop's mic array removes its own speaker's
sound.

## Limits

- **Wave's own cloned voice scores like the owner** (0.56-0.72), so this can't stop Wave
  hearing itself - echo cancellation and the "Wave is talking" rules still do that.
- A TV saying a short "baby" can still wake Wave (under 1 s isn't checked); what it says
  next is dropped, and Wave dozes off again.
- How other people's voices score through the owner's mic *after* training isn't
  measured yet (in the first training run the "video" part held the owner's own voice,
  so the threshold stayed at 0.28). If Wave starts ignoring you or answering others, the
  log lines above show the scores - adjust `SPEAKER_VERIFY_THRESHOLD`, re-run
  `train_my_voice.py`, or set `SPEAKER_VERIFY=off`.
