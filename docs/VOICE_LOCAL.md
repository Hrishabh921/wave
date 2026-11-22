# Local neural voices (Piper + Kokoro + voice cloning)

Wave's text-to-speech is edge-tts (cloud) by default. When that's unreachable - no
internet, or Microsoft's endpoint itself is down - Wave falls back to a fully local
voice instead of going silent. As of Tier 13 there are up to three fallback tiers,
tried in this order:

| | Piper | Kokoro | SAPI |
|---|---|---|---|
| Speaks Hindi? | No - mispronounces Devanagari with its English voice | **Yes** - ships real Hindi voices | No |
| Speaks English? | Yes | Yes (not used for this - see below) | Yes, badly |
| Default | Tried first for everything | Tried only for Hindi, and only before Piper | Last resort, always available |
| Needs | `pip install -r requirements-voice-local.txt` (installs `piper-tts`) + a voice file | `pip install -r requirements-voice-local.txt` (installs `kokoro-onnx`) + `TTS_KOKORO_ENABLED=true` + two model files | Nothing - built into Windows |
| License | GPL-3.0-or-later | MIT | N/A (Windows) |
| CPU cost | Fast | **~2x real time** (measured below) | Fast |

## Why Kokoro goes *before* Piper for Hindi, not after

This is the one non-obvious design decision in this tier, so it's worth spelling out.
Piper's English voice doesn't *fail* on Hindi text - `_speak_via_piper()` catches real
errors, but mispronouncing Devanagari with espeak-ng's English phonemizer isn't one.
It just produces bad audio and reports success. If Piper were tried first on Hindi
text the way it is for everything else, `_speak_offline_fallback()` would stop right
there, "successfully" having spoken gibberish, and Kokoro - the one engine that can
actually speak Hindi - would never get a turn.

So `_speak_offline_fallback(text, lang)` branches on `lang`:

- `lang == "hi"` **and** `TTS_KOKORO_ENABLED=true`: Kokoro is tried first. Piper (still
  English - a last-resort approximation, not a translation) only gets a turn if Kokoro
  itself isn't usable right now (not installed, model files missing and auto-download
  declined/failed, or synthesis raised).
- Anything else (English, Hinglish, or Kokoro simply not enabled): nothing changes from
  before Tier 13 - Piper, then SAPI. Kokoro is never reached for English text, on
  purpose: Piper already sounds at least as good there, without Kokoro's latency cost.

`lang` comes from the same stateless `voice/language.py` classifier
`_cloud_voice_and_text()` already uses to pick edge-tts's own Hindi voice - a per-chunk
decision, not the sticky STT-side tracker (a generated reply can switch languages
sentence to sentence; `speak_stream()` already calls this once per sentence).

## Turning them on

```powershell
pip install -r requirements.txt -r requirements-voice-local.txt
```

**Piper** (on by default once installed, for the *fallback* chain):

```powershell
python -m piper.download_voices en_US-lessac-medium
```

...then point `TTS_LOCAL_MODEL_PATH` at the resulting `.onnx` file - or skip the manual
step entirely and set `TTS_LOCAL_AUTO_DOWNLOAD=true` (see "Auto-download" below).

**Kokoro** (opt-in - genuinely bilingual, but slower; see the latency numbers below):

```ini
TTS_KOKORO_ENABLED=true
TTS_KOKORO_AUTO_DOWNLOAD=true
```

Both default to `false`. `TTS_PREFER_LOCAL_VOICE` (Tier 7) is a separate, older, narrower
knob - "try Piper before the cloud, not just after it fails" - and deliberately still
only ever reaches Piper, never Kokoro; see the Readme's "Shipped in Tier 13" for why
folding Kokoro into that path isn't worth the added complexity for Tier 13's scope.

## Auto-download: consent and checksums

Neither engine's model files ship in this repository (Piper's is a few hundred MB per
voice; Kokoro's two files are ~120MB together). Both can fetch their own files on first
use instead of making you do it by hand:

| | Piper | Kokoro |
|---|---|---|
| Flag (off by default - turning it on **is** the consent) | `TTS_LOCAL_AUTO_DOWNLOAD` | `TTS_KOKORO_AUTO_DOWNLOAD` |
| Mechanism | Shells out to Piper's own `python -m piper.download_voices <voice>` CLI | Plain streamed `requests.get` - `kokoro-onnx` ships no downloader of its own |
| Saved to | `TTS_LOCAL_VOICES_DIR`, default `<data dir>/voice_models/piper` | `TTS_KOKORO_MODEL_DIR`, default `<data dir>/voice_models/kokoro` |
| Checked against | `voice/tts.py`'s `PIPER_VOICE_CHECKSUMS` | `voice/tts.py`'s `KOKORO_FILE_CHECKSUMS` |
| Settings-UI hook (optional, narrows consent further - `None` means the flag alone decided) | `VoiceEngine.on_piper_download_consent(voice_name) -> bool` | `VoiceEngine.on_kokoro_download_consent(filename) -> bool` |

A downloaded file that fails its checksum is deleted, not loaded - a corrupted or
tampered download is never silently used. A voice/file with **no** entry in the
checksum manifest still downloads, just unverified, with a logged warning: choosing a
voice other than the one this project ships and has pre-verified a hash for is already
a deliberate, informed choice, and there's no way to pin a hash ahead of time for a
file this project doesn't know about.

There's no Settings-dialog checkbox for either consent hook yet - that's real UI work,
likely natural for Tier 21 (UI depth) rather than this tier. Until then, the config
flag alone is this project's primary, legible opt-in for both.

### Reusing an older auto-download effort

The Piper mechanism (CLI shell-out, same config key names for voice/directory/timeout)
isn't new - it revives a working auto-downloader an earlier line of work had built,
which was then lost during the Tier 10 repo merge (two separate implementation lines
had each built on an earlier shared tree; unzipping one on top of the other adds and
replaces files but never deletes them, so Tier 10's cleanup pass removed files the
*other* line had added that this merged tree no longer referenced - this one included).
Checked against that merge's own validation report: the removal wasn't a response to
any bug in the downloader itself, just scope that didn't make the merged cut. Tier 13's
roadmap entry asks for the same capability back, so this restores the *same* mechanism
rather than reinventing it, with the consent flag and checksum verification this tier
specifically also asks for layered on top - neither existed in the original.

### Verifying or refreshing a pinned checksum

Both manifests pin a `(sha256, size)` pair per file. To verify or refresh one:

**Piper** - voices live on Hugging Face as git-lfs objects, and the LFS object id
*is* the file's real sha256 (git-lfs's own convention) - distinct from a plain git
blob hash, which isn't a content hash at all. Fetch it from the tree API rather than
hashing a downloaded copy by hand:

```
GET https://huggingface.co/api/models/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium
```

...and read the `.onnx` file's `lfs.oid` field.

**Kokoro** - files are plain (non-LFS) GitHub release assets, so there's no API
shortcut; download and hash directly:

```powershell
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx
curl -LO https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
sha256sum kokoro-v1.0.int8.onnx voices-v1.0.bin
```

If a vendor ever republishes a "same" file with a different hash (a model update, a
re-upload), update the relevant manifest entry in `voice/tts.py` deliberately - don't
just widen the check to accept both.

## Kokoro's latency tradeoff - measured, not assumed

Kokoro is genuinely bilingual, which is exactly why it exists in this project at all -
but it isn't a free upgrade over Piper. Measured directly in this project's own dev
sandbox (CPU-only, `kokoro-onnx` 0.6.1, the `kokoro-v1.0.int8.onnx` quantized model):

| Language | Real-time factor (synthesis time ÷ audio duration) |
|---|---|
| Hindi | ≈ 2.10 |
| English | ≈ 1.87 |

Both are meaningfully *slower than real time* - a sentence that plays for 2 seconds
takes roughly 4 seconds of CPU to generate. That's noticeably slower than Piper or
edge-tts, which is why `TTS_KOKORO_ENABLED` defaults to `false` and why it's only ever
tried for Hindi, never English (where Piper already covers the same need faster).
Someone with a genuinely Hindi-heavy offline workflow can opt in and judge the tradeoff
for themselves; the honest answer for everyone else is that it isn't worth the wait by
default.

**Caveat on the caveat:** like the rest of this project's sounddevice/AEC playback path,
this was measured in a sandboxed environment with no real audio hardware and whatever
CPU happened to be available there - a real machine's numbers (likely better on modern
hardware, GPU-accelerated onnxruntime not explored here) belong on the manual test
checklist, not assumed from this one measurement.

## Voice cloning (Coqui XTTS-v2) - Tier 25

A third, different kind of local engine: not a stock offline fallback like Piper/Kokoro
above, but the user's OWN voice, cloned from a short reference sample - built for Wave's
avatar companion (`docs/ROADMAP_BEAT_SARA.md`'s Tier 25), which is meant to sound like
one specific person, not a generic assistant voice.

```powershell
pip install coqui-tts
```

Then set up a reference clip via Settings → Avatar page's "Build avatar" button (a
clean ~20-30s solo recording, no background noise/music) - that's it. There's no
separate enable flag the way Piper/Kokoro each need one: pointing
`TTS_CLONE_REFERENCE_PATH` at a real file already *is* the deliberate opt-in
(`TTS_CLONE_ENABLED` exists only as an emergency off-switch, defaulting to `true`).

Unlike Piper/Kokoro - which only ever get a turn once edge-tts has already failed -
cloning is tried **first**, in `_run_utterance()`, ahead of everything else including
`TTS_PREFER_LOCAL_VOICE`: a user who went to the trouble of providing their own voice
sample obviously wants it actually used, not held as a third-choice fallback. It's a
complete no-op (one cheap `_clone_reference_ready()` check, no model load at all) for
anyone who hasn't set one up, so nothing changes for everyone else.

XTTS-v2 is genuinely multilingual (Hindi included), so - unlike Piper/SAPI's
fixed-English voice - `_speak_via_clone()` passes the real classified language
("hi"/"en") straight through rather than collapsing Hindi down to an English
approximation.

**Not added to `requirements-voice-local.txt` alongside Piper/Kokoro, on purpose**:
`coqui-tts` pulls in `torch` (a CPU build is still several hundred MB) and downloads
the XTTS-v2 checkpoint itself (~1.8GB, cached by the `TTS` package's own cache dir) on
first use - real weight, and a real wait, that nobody should hit without having asked
for this specific feature. `tests/test_tier25_voice_clone.py` covers the engine
load/routing/fallback logic entirely against a mocked `CoquiTTS` class; the real
package has not been installed or run end-to-end in this project's own development
sandbox (disk-space discipline, same reasoning as every other heavy optional
dependency here) - a real reference clip, a real install, and a real listen on actual
hardware are still needed before this is more than "the code path is correct."

### Running it for real - Tier 31

Installed and run on the user's laptop (Windows 11, i5-13450HX, 16 GB, RTX 3050 6GB)
with their real sample. What that took:

- **The licence prompt.** XTTS-v2 asks once, in a console, to accept Coqui's
  non-commercial CPML. A windowed launch (`pythonw`) has no console, so do the first
  download from a terminal (or with `COQUI_TOS_AGREED=1`); coqui-tts then writes
  `tos_agreed.txt` next to the model and never asks again. (Since Tier 33 the voice
  process can't wait on that question: it fails at once and says so in
  `wave_voice_clone.log`.)
- **Its own process.** XTTS-v2 never runs inside Wave's process: its token loop gives
  up and re-takes Python's GIL about a thousand times per token, so any busy Python
  thread in Wave (there are many - mic, AEC, UI bridge) made it up to 100x slower, and
  it starved Wave's audio threads in turn. `voice/clone_worker.py` runs it in a process
  of its own, which `voice/clone_client.py`'s `CloneVoiceProcess` starts and feeds one
  sentence at a time over stdin/stdout; it exits when Wave does. Torch isn't loaded
  into Wave's own process at all.
- **No FFmpeg needed.** Since torchaudio 2.9, `torchaudio.load()` goes through
  torchcodec, which needs FFmpeg's DLLs - so XTTS's own loader failed on every
  sentence. The worker reads the sample with soundfile and works the voice latents out
  once per sample file (`voice/clone_xtts.py`), with the model's own settings.
- **Rendered ahead, never waited for.** On the CPU a sentence takes ~1.8x its own
  length to synthesize, so `prefetch()` queues each streamed sentence on one ordered
  worker (the next one is made while the current one plays). Both UIs start the voice
  process at start-up in the background (`warm_up_clone()`, ~15-20 s, ~2.7 GB of RAM in
  that process); until it's up, sentences go out in the edge-tts voice.
- **Windows power throttling.** Windows 11 moved Wave's process to slow efficiency
  cores about a second into any sustained work - torch's maths got ~15x slower - so
  both launchers and the voice process opt out (`monitoring/power.py`).

Measured (the same model and sample):

| | synthesis time / speech length | first sentence of a reply starts after |
|---|---|---|
| CPU (wave_env's default PyTorch) | ~1.8x | ~1.8x that sentence's length |
| RTX 3050 (CUDA PyTorch) | ~0.4x | ~0.4x that sentence's length |

In the real web app on the CPU, with Whisper transcribing room audio at the same time,
sentences ran at 2.4-4.2x and a typed question's first cloned audio came ~13 s after
sending - the GPU build is what makes the cloned voice keep up with conversation.

Speaker similarity to the sample (XTTS's own speaker encoder): 0.63-0.76 for cloned
lines, 0.21 for edge-tts's Neerja.

`TTS_CLONE_DEVICE=auto` (the default) uses the GPU as soon as PyTorch can. wave_env
ships the CPU build; to switch it to the CUDA build of the same version (~1.9 GB
download; NVIDIA driver with CUDA 13 support):

```powershell
wave_env\Scripts\pip install --no-deps --force-reinstall torch==2.14.1+cu130 --index-url https://download.pytorch.org/whl/cu130
```

### In the voice picker, and a start that can't hang - Tier 33

**Settings -> Voice.** The cloned voice is the picker's first entry, named after its
voice sample (`r1.wav` shows as "r1 — Cloned voice"), and Wave's default whenever a
sample is set; picking Neerja, Aria, ... turns it off, picking it again turns it back
on (and loads it, if it isn't loaded). A line under the picker says where it stands:
loading (with the voice that fills in until then), ready (CPU or GPU), couldn't start,
or no sample yet. Picking a sample with "Build avatar" makes the cloned voice the voice
again. The setting is saved as a voice id (`"tts_voice": "clone"` or
`"en-US-AriaNeural"` in `wave_settings.json`); a label saved before Tier 33 only ever
chose the fill-in voice (the picker had no cloned voice then), and keeps that meaning.
The web UI applies a saved edge-tts voice before the warm-up, so XTTS-v2 isn't loaded
for nothing.

**The start that never finished.** On the user's laptop, Wave started from VS Code
spoke every reply in Neerja: its voice process sat there, never ready, idle about two
seconds of CPU in - after its imports, where coqui-tts checks the licence (asking with
`input()` when it can't find `tos_agreed.txt`) and then asks huggingface.co about the
model, as `TTS(model_name)` does on every start. Nothing said why: the process's output
went to a pipe Wave only logged at debug level, and Wave waited up to 15 minutes. Now:

- **Loaded from the local copy.** `voice/clone_xtts.py` finds the downloaded model in
  Hugging Face's cache without the network (`snapshot_download(local_files_only=True)`)
  and loads it by path, after checking the licence the way coqui-tts does
  (`tos_agreed.txt`, or `COQUI_TOS_AGREED=1`). Only a model that isn't downloaded yet
  goes through `TTS(model_name)`. Measured: ready in 16 s with the network unreachable,
  and the same speech as before (one sentence, fixed seed: the same 6.50 s, samples
  within 1.5e-5).
- **No question can block it.** The process's stdin is NUL for everything but the
  request protocol, so a library asking a question (`input()`) gets "no answer" at once
  and the error reaches Wave's log, instead of waiting for a line Wave never sends.
- **Its own log.** Everything the process prints goes straight into
  `wave_voice_clone.log` in Wave's data folder (rolled to `.1` past 1 MB) - not through
  a pipe Wave has to keep reading.
- **Not silent while slow.** Every 60 s that it's still loading, Wave's log says so,
  with the process's last line; one that exits before it's ready is reported with its
  last line too.

## What's still open

- No Settings-dialog UI for either download's consent prompt (see above) - natural
  Tier 21 work.
- `TTS_PREFER_LOCAL_VOICE` doesn't know about Kokoro - deliberately out of scope this
  tier (see "Turning them on" above).
- Kokoro's own voice quality, and both engines' real latency on actual hardware, are
  on `docs/MANUAL_TEST_CHECKLIST.md`'s Tier 13 entry - everything above is reasoned
  through carefully and the latency numbers are real measurements, but neither
  substitutes for a human listening on their own PC.
- Voice cloning (just above) now has a real ~21s reference clip prepared from a
  WhatsApp voice note the user gave ("r1" - someone else's voice, the user said on
  2026-11-21; `avatar_rig/r1.wav` since Tier 33, before that `voice_reference.wav`: the
  single longest clean, continuous stretch of speech `ffmpeg silencedetect` found in
  the 65s note, 35.3-56.3 s, not just the first N seconds), and
  `config.TTS_CLONE_REFERENCE_PATH` is wired
  up and persisted (see `docs/ROADMAP_BEAT_SARA.md`'s Tier 25 "Status" for a real bug
  this surfaced and fixed - the reference path wasn't being saved across restarts).
  Tier 31 installed and ran it on the user's machine (see "Running it for real"
  above): it works and measures like their voice. Still open: the user's own
  listening verdict, and the CUDA PyTorch build in wave_env for GPU speed.
