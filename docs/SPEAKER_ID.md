# Only your voice (speaker check) - Tier 32

Wave's mic hears everyone in the room: the TV, a video, other people. In testing, a
video saying "Say something." was taken for a command and Wave started talking on its
own. Since Tier 32, every utterance's **voice** is checked against the owner's
voiceprint before anything else happens to it (`voice/stt.py` →
`voice/speaker_id.py`). Anyone else is dropped before they're even transcribed: no
Whisper CPU spent on them, nothing they said written to the log.

## How it decides

- **The voiceprint is your voice training through this PC's mic** -
  `scripts/train_my_voice.py`, ~30 s of you reading through the mic Wave listens with -
  plus what Wave learns from your own commands. **Until you've trained once, the check
  is off** and every voice counts, as before.
- **It learns.** An utterance that clearly is you (score ≥ 0.40) is added (the newest 40
  are kept), so Wave keeps up with your voice as it hears it. Never while Wave itself is
  talking.
- **A score** is the cosine similarity to the training (or to what was learned). At or
  above `SPEAKER_VERIFY_THRESHOLD` (0.35) it's you.
- **Short utterances (< 1 s) aren't checked** - a voice says too little in that time -
  so a lone "Baby" always wakes Wave. Whatever follows is checked.
- **Not while Wave is talking**: its echo is in that audio, and your "ruk jao" must
  always get through. The score is still logged.

The log shows every decision:

```
Speaker check: 0.62 (you)
Not your voice (speaker score 0.12) - ignored, not transcribed
```

### Why not the cloned voice's sample? (Tier 32c)

Tier 32 first built the voiceprint from the voice sample Wave's cloned voice uses
(`TTS_CLONE_REFERENCE_PATH`). Two things are wrong with that: the sample needn't be the
owner at all (on the owner's laptop it was someone else's WhatsApp voice note), and even
a phone recording of the owner sounds like a stranger through a laptop mic - the owner's
mic voice scored only **0.16-0.19** against it, under the threshold, so Wave would have
ignored its own owner. The speaker check now ignores the sample completely; it scores
**at most 0.22** against the trained voiceprint, i.e. it's rejected like any other voice.

## Setting it up

```powershell
wave_env\Scripts\pip install kaldi-native-fbank==1.23.3
wave_env\Scripts\python scripts\download_speaker_model.py
wave_env\Scripts\python scripts\train_my_voice.py
```

`train_my_voice.py` (Wave closed, TV off): one beep - read the five lines it prints
(30 s); two beeps - stop talking and play a video (20 s, it shows how other voices score
through the same mic); three beeps - done. Pieces that don't sound like the rest are
dropped, and nothing is saved unless the recording is one clear voice. `--no-video`
skips the video part, `--video-only` just measures a video without touching the
training, `--reset` forgets the training and everything learned. Play the video on
another device (a TV, a phone): this laptop's mic array removes the laptop's own speaker
sound.

The model is 3D-Speaker's CAM++ (`iic/speech_campplus_sv_zh_en_16k-common_advanced`,
Chinese+English, 16 kHz, 192-d; the 3D-Speaker project is Apache-2.0) as exported by
sherpa-onnx, ~27 MB, checked against a pinned SHA-256. It runs with onnxruntime (no
torch in Wave's process) and takes ~30 ms per utterance on the CPU. The voiceprint is
`avatar_rig/voiceprint.npz` (git-ignored, like the rest of `avatar_rig/`).

| `.env` | default | |
|---|---|---|
| `SPEAKER_VERIFY` | `auto` | on when the model and your voice training are there; `off` = every voice counts |
| `SPEAKER_VERIFY_THRESHOLD` | `0.35` | lower lets more through (you *and* others), higher is stricter |
| `SPEAKER_MODEL_PATH` | blank | blank = `voice_models/speaker/<model>` |

## Measured

On the owner's laptop (Intel Smart Sound mic array), after voice training:

| | score |
|---|---|
| the owner, held-out 1.5 s pieces of the training recording | 0.71-0.74 |
| a video playing on a phone in the room (owner silent), 12 pieces | at most 0.21, mean 0.15 |
| the cloned voice's sample (someone else's voice note) | at most 0.22 |

Sharing the room and the mic doesn't make voices match: the video scored the same 0.21
against the training alone. The model choice came from an earlier synthetic calibration
(the owner's sample against 10 edge-tts voices through a simulated room): CAM++ held up
there, while WeSpeaker's ResNet34 fell apart (the owner down to -0.01). Pieces under 1 s
scored anywhere from -0.1 to 0.5 - hence no check below 1 s.

## Limits

- This doesn't stop Wave hearing its own voice - echo cancellation and the "Wave is
  talking" rules still do that.
- A TV saying a short "baby" can still wake Wave (under 1 s isn't checked); what it says
  next is dropped, and Wave dozes off again.
- If Wave starts ignoring you or answering others, the log lines above show the scores -
  adjust `SPEAKER_VERIFY_THRESHOLD`, re-run `train_my_voice.py` (e.g. after moving to a
  different mic or headset), or set `SPEAKER_VERIFY=off`.
