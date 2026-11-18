# Wake word

Wave can listen for its wake word two ways:

| | On-device model (Tier 11) | Speech recognition (before Tier 11, still the fallback) |
|---|---|---|
| How | A small [openWakeWord](https://github.com/dscripka/openWakeWord) model scores each captured phrase on your PC | The phrase is transcribed, and the text is searched for the wake phrase |
| While Wave is asleep | Nothing is transcribed until the model hears the wake word | Everything said near the mic is transcribed — by Google's service whenever faster-whisper isn't installed (since Tier 11 that only happens with `STT_CLOUD_WHILE_ASLEEP=true`) |
| Phrases | "Hey Wave" (the model Wave ships), or any other trained model | Any phrase you pick in Settings |
| Needs | `pip install -r requirements-voice-local.txt` | Nothing extra |

SARA, the project Wave is benchmarked against, uses openWakeWord too — but it expects a custom `hey_sara.onnx` that its repository doesn't include, so out of the box its passive wake is off, and a production-trained model is still on its own backlog. **Wave ships its model:** `voice_models/wakewords/hey_wave.onnx`.

## Turning it on

```powershell
pip install -r requirements-voice-local.txt
```

Then start Wave. Settings → Voice → **Wake Word Detection** shows what is listening:

- *On-device 'Hey Wave' model ready (starts when Wave sleeps)* — it loads the first time Wave goes to sleep. On that first load, openWakeWord's two small feature files (a melspectrogram model and Google's speech-embedding model, ~2.5 MB together) are downloaded from openWakeWord's GitHub releases into `<data dir>/voice_models/openwakeword/`. After that it works offline.
- *The on-device model listens for 'Hey Wave' - pick that wake word to use it* — you picked another wake phrase ("Okay Wave", a custom one). A "Hey Wave" model must never stand between you and a phrase it wasn't trained on, so those stay on speech recognition.
- *No on-device wake model found* — openwakeword isn't installed (or the model file is missing); speech recognition is used, exactly as before.

**Wake Word Detection** choices:

| Choice | `.env` | Behaviour |
|---|---|---|
| Automatic (default) | `WAKE_WORD_ENGINE=auto` | The model when it's installed **and** its phrase is your wake phrase; otherwise speech recognition |
| On-device model | `WAKE_WORD_ENGINE=openwakeword` | Always the model (logs a warning and falls back to speech recognition if it can't load — Wave never goes deaf) |
| Speech recognition only | `WAKE_WORD_ENGINE=transcript` | Never the model |

**Wake Sensitivity** moves the model's threshold (higher sensitivity = lower threshold = wakes more easily, and falsely more often). Left alone, Wave uses the threshold recommended in the model's card (`hey_wave.json`). `WAKE_WORD_THRESHOLD=` in `.env` overrides both.

## How a wake is decided

1. While asleep, the mic captures a phrase (energy threshold + Tier 7's VAD).
2. The model scores it, 80 ms frame by 80 ms frame. The phrase's score is the best score the model **held for `smoothing_frames` consecutive frames** (from the model card; 2 for the bundled model): a real "hey wave" keeps the score up for several frames, most false triggers are a one-frame spike. Below the threshold: the phrase is dropped, untranscribed.
3. Above it, the phrase *is* transcribed — that's how "Hey Wave, Chrome kholo" wakes Wave **and** runs the command in one breath.
4. **Two-stage check.** A detection scoring below `WAKE_WORD_CONFIRM_BELOW` is only accepted if the transcript also starts with something like the wake phrase — "hey wave", "hey way", "hey wait", "a wave", "हे वेव"… Most false wakes (a TV saying "hey Dave, wait") fail this. Left unset, the value comes from the model card: a model trained as a **prefilter** (see below) asks for `1.0`, so *every* detection is checked; otherwise 0.85, so a strong detection stands on its own. `WAKE_WORD_CONFIRM_BELOW=0` turns the check off.
5. The heard wake phrase is stripped ("Hey way, open Chrome" → "open Chrome") and the rest goes to the brain.

## The bundled model

`voice_models/wakewords/hey_wave.onnx` (~0.8 MB) plus its card `hey_wave.json`: phrase, recommended threshold, SHA-256, training settings and the evaluation numbers below.

**How it was trained** (`scripts/train_wake_word.py`, about 1½ hours on a 2-core laptop CPU):

- **100% synthetic positives**, as openWakeWord's own models are: "hey wave" (and spellings like "hey vave" for the w/v merger common in Indian English) spoken by 800 of the 904 voices of Piper's LibriTTS model, every espeak-ng English voice × variant, and espeak-ng's Hindi voice saying "हे वेव".
- **Negatives:** public-domain book text (Project Gutenberg) read by the same kinds of voices — with 1,000 extra natural-sounding Piper sentences, because a TV sounds like Piper, not like espeak; Hinglish and Hindi sentences; sound-alike phrases ("hey wait", "hey dave", "heat wave", "microwave", "hey Jarvis", "hey Siri"…); **spliced negatives** — the "hey" of "hey wave" followed by something else, something else followed by the "wave", and word-length pieces of different speakers back to back, so the model learns that only the whole phrase counts; and generated noise, mains hum, fan noise, babble, music and clicks.
- Every clip gets random speed, reverb, EQ, background noise and loudness.
- Features are openWakeWord's own (16 frames × 96 values ≈ the last 1.3 s), so the file drops straight into `openwakeword.Model`. The classifier is a small MLP (1536 → 128 → 64 → 1) trained in numpy with two rounds of hard-negative mining. **Model selection** uses a validation split of the *training* voices and books (recall at 2 false accepts per hour of natural speech); the held-out voices and books below are touched only once, for the final numbers.
- **Operating point.** The script picks, from the held-out measurements, the threshold and smoothing with the fewest false rejects within 1 false accept per hour — if that point also keeps clean false rejects ≤ 10%, the model is **standalone**. Otherwise it is marked a **prefilter**: a point that keeps nearly every real wake (clean false rejects ≤ 5%), and the card sets `confirm_below: 1.0` so the transcript check decides every detection. A prefilter still keeps the promise that matters — nothing is transcribed unless the model fires — it just fires more often than a standalone model would.

**License:** MIT, like the rest of Wave. Training speech came from Piper's LibriTTS voice (dataset CC BY 4.0, <https://www.openslr.org/60/>) and espeak-ng; text from public-domain books. No openWakeWord pretrained model or training data is in it — those are CC BY-NC-SA 4.0 (non-commercial), which is why Wave never bundles them.

### Measured (synthetic, held out)

Evaluated through `openwakeword.Model` itself, frame by frame, exactly as Wave runs it. **Held out** means voices (100 Piper speakers, 12 espeak variants) and books the model never saw in training:

Smoothing 1 frame (80 ms):

| Threshold | False accepts/hour | False rejects, noisy | False rejects, clean | Sound-alike accept rate |
|---|---|---|---|---|
| 0.20 | 118.0 | 5.8% | 3.8% | 35.4% |
| 0.30 | 74.0 | 7.1% | 4.2% | 28.5% |
| **0.35 (shipped)** | **52.0** | **8.2%** | **4.9%** | **25.8%** |
| 0.40 | 39.0 | 10.0% | 5.1% | 21.9% |
| 0.50 | 18.0 | 14.2% | 6.9% | 16.9% |
| 0.60 | 5.0 | 21.3% | 11.1% | 13.5% |
| 0.70 | 3.0 | 32.0% | 18.7% | 10.4% |
| 0.80 | 0.0 | 47.1% | 30.4% | 6.5% |
| 0.90 | 0.0 | 69.8% | 58.0% | 1.5% |

Smoothing 2 frames (160 ms):

| Threshold | False accepts/hour | False rejects, noisy | False rejects, clean | Sound-alike accept rate |
|---|---|---|---|---|
| 0.20 | 55.0 | 7.1% | 4.9% | 27.7% |
| 0.30 | 21.0 | 10.4% | 6.2% | 18.9% |
| 0.35 | 14.0 | 11.3% | 7.1% | 16.9% |
| 0.40 | 12.0 | 13.8% | 8.7% | 15.4% |
| 0.50 | 4.0 | 23.6% | 11.8% | 10.4% |
| 0.60 | 2.0 | 35.6% | 18.9% | 6.5% |
| 0.70 | 1.0 | 48.4% | 31.1% | 3.1% |
| 0.80 | 0.0 | 63.3% | 50.7% | 2.7% |
| 0.90 | 0.0 | 83.1% | 76.4% | 0.4% |

(Full table, every threshold from 0.20 to 0.95, is in `hey_wave.json`'s `metrics_synthetic.by_smoothing`.)

- *False accepts per hour* — on a 60-minute stream of held-out voices reading held-out books, with noise and music beds and two sound-alike phrases ("hey wait", "heat wave"…) per minute mixed in. A wake counts once per 1.5 s.
- *False rejects, noisy* — held-out "hey wave" clips with reverb, EQ and background noise at 5–30 dB SNR.
- *False rejects, clean* — the same voices, scored the way Wave scores a captured phrase (`WakeWordGate.score_pcm16`).
- *Sound-alike accept rate* — how often 260 held-out clips of phrases like "hey wait", "heat wave", "hey Dave" score above the threshold, with no transcript check applied. The two-stage check (step 4 above) is what actually keeps most of these out in practice — it isn't part of this number.

**Shipped: smoothing 1 frame, threshold 0.35 — a prefilter, not a standalone model.** 52 false accepts an hour, on paper, sounds far too high — but it isn't what you'll experience, because `confirm_below` is `1.0` for this card: *every one* of those 52 acoustic detections, same as a real "hey wave", still has to produce a transcript that looks like the wake phrase before Wave treats it as a wake. That second stage is real (it's in `voice/wakeword.py` and tested in `ListenWithGateTests`), but it isn't measured in the table above — these numbers are the acoustic model scored alone, exactly as `score_pcm16` runs it, with no transcript involved, because simulating "what would Whisper transcribe this synthetic clip as, and would it look like 'hey wave'" wasn't something this training run could honestly measure. So the true false-accept rate you'd see is somewhere between this table's 52/hour and the near-zero a working two-stage check should produce - and that gap can only be closed by the real-hardware test in `docs/MANUAL_TEST_CHECKLIST.md`, not by more synthetic numbers.

Why 0.35/smoothing-1 over 0.35/smoothing-2 (14/hour, for 2.2 more points of noisy false rejects)? Because it's the lowest-latency point (one 80 ms frame instead of two) that still keeps clean false rejects at or under the 5% prefilter target; smoothing 2 only matches that target at threshold 0.20, where its false-accept rate (55/hour) is a wash with smoothing 1's 52. Either point would have been a reasonable choice — `scripts/train_wake_word.py` breaks the tie on latency.

**These numbers are synthetic.** Real voices, real rooms and real TVs differ, and the two-stage check above isn't part of them (it can only lower false accepts). The Tier 11 targets — **≤ 1 false wake per hour** of TV, **≤ 5% false rejects** — are for the *whole* two-stage pipeline, not the acoustic model alone, and are measured on your PC: see "Tier 11" in `docs/MANUAL_TEST_CHECKLIST.md`.

## Other wake words

- **openWakeWord's pretrained phrases:** `WAKE_WORD_MODEL_NAME=hey_jarvis` (or `alexa`, `hey_mycroft`, `hey_rhasspy`) — downloaded on first use into `<data dir>/voice_models/openwakeword/`. Pick the matching phrase in Settings (Custom… → "hey jarvis"). Their license is CC BY-NC-SA 4.0: personal, non-commercial use only.
- **Your own model:** `WAKE_WORD_MODEL_PATH=C:\path\to\model.onnx` — any openWakeWord-format model (input `[batch, 16, 96]`, output `[batch, 1]`). A `model.json` card next to it (`"phrase"`, `"recommended_threshold"`) is read if present.
- **Retrain "Hey Wave", or train another phrase** with `scripts/train_wake_word.py` (edit `POSITIVE_TEXTS`/`ADVERSARIAL_TEXTS` at the top). Its docstring lists the requirements; it runs on Linux or WSL (it calls `espeak-ng`).

## Troubleshooting

| Symptom | Check |
|---|---|
| Settings says the model couldn't load | `pip show openwakeword onnxruntime`; on the first sleep Wave needs internet once for the feature files (see `wave_system.log`: "Wake word model … failed to load") |
| Wakes too often | Lower *Wake Sensitivity* (or raise `WAKE_WORD_THRESHOLD`), and keep `WAKE_WORD_CONFIRM_BELOW` at its default |
| Misses you | Raise *Wake Sensitivity*; say it at normal volume within a few metres; check the mic level (the waveform moves with your voice now) |
| Never wakes with a custom phrase | Expected with *Automatic*: the "Hey Wave" model only listens for "hey wave" — custom phrases use speech recognition |
| Wakes while Wave is talking | The barge-in gate ignores unaddressed speech while Wave talks, but a loud reply on speakers can still trip the mic — headphones, or `AEC_ENABLED=true` with `sounddevice` installed |
