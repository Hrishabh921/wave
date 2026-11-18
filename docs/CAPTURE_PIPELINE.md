# The capture pipeline (Tier 12)

Until Tier 12, Wave listened the way `speech_recognition` does: open the microphone, wait for a sound louder than one fixed threshold, record until a fixed silence timeout, close the microphone, and only then look at the audio. Echo cancellation ran on the whole phrase afterwards, a phrase was cut at a fixed cap, and an unplugged microphone stayed broken until a restart.

Tier 12 replaces that with one continuous stream:

```
microphone ──► sounddevice input stream (stays open, 20 ms blocks)
                 │  PortAudio thread: copy + hand off, nothing else
                 ▼
            capture thread
                 ├─ echo cancellation, frame by frame (voice/aec.py)
                 ├─ mic level  ──► the waveform
                 └─ endpointer (voice/endpoint.py)
                      noise floor · VAD + energy · pre-roll · end-of-speech
                 ▼
            finished utterances ──► SpeechReceiver.listen()
                                     wake gate (asleep) ─► Whisper / Google
                                     ─► transcript filter ─► the mic worker
Wave's voice ──► one persistent output stream (voice/audio_out.py)
                 └─ every block it plays, silence included ─► AEC far end
```

## What changed, and why

| | Before | Tier 12 |
|---|---|---|
| Microphone | Opened and closed for every phrase | One stream, open while the mic is on |
| Start of speech | One energy threshold, calibrated once at start-up | WebRTC VAD **and** energy above a **tracked noise floor** (10th percentile of the last 3 s, smoothed). A fan switching on raises the floor in a few seconds instead of reading as one endless phrase |
| First syllable | Could be clipped | 300 ms of pre-roll kept |
| Very quiet rooms | Faint room tone could be captured and handed to Whisper, which normalises its input and "hears" words in it | A **gain floor**: nothing below −58 dBFS counts as speech, however low the noise floor |
| End of speech | 0.35–0.6 s silence timeout, 64 ms granularity, estimated afterwards | 450 ms of non-speech (`STT_END_SILENCE_MS`), 20 ms granularity, **exact** time of the last speech frame — time-to-first-audio no longer estimates it |
| Phrase length | Capped (15 s since Tier 10, 8 s before) | No fixed cap; `STT_MAX_UTTERANCE_S` (60 s) is only a safety net. A 30-second command is fine |
| Echo cancellation | Whole phrase after capture | Every 20 ms frame as it arrives — the "honest limitation" `voice/aec.py` documented since Phase 5 is closed |
| Echo reference | Started/stopped with each sentence Wave spoke | Continuous: one output stream, silence included |
| Between sentences of a reply | A device open/close each | Back to back on the same stream |
| Mic unplugged | Broken until restart | No audio for 2.5 s ⇒ the stream is reopened (with backoff) |
| Headphones plugged in | Kept using the old default device | The default input is re-checked every 3 s; capture follows it |
| Whisper output | Used as-is | Known hallucinations ("Thank you.", "Thanks for watching!", "[Music]"…) on short or quiet audio rejected, repetition loops collapsed, degenerate output (compression ratio) and low confidence rejected; every transcript carries its confidence |
| While you talk | Nothing on screen | **Live captions** — a rough transcript in the chat, updated about every 0.8 s (local Whisper only) |
| Talking over Wave without "Wave," | Ignored (Tier 11 barge-in gate) | Also interrupts if it's loud and clear: ≥ `BARGE_IN_SNR_DB` (18 dB) above the floor, and only with real echo cancellation running |

## Settings

| `.env` | Default | Meaning |
|---|---|---|
| `CAPTURE_ENGINE` | `auto` | `auto` = the continuous stream when `sounddevice` works, otherwise the old `speech_recognition` path. `legacy` forces the old path |
| `STT_END_SILENCE_MS` | `450` | Silence that ends a phrase. Lower = faster replies but more risk of cutting you off mid-thought |
| `STT_PREROLL_MS` | `300` | Audio kept from before speech was detected |
| `STT_MAX_UTTERANCE_S` | `60` | Safety cap only |
| `LIVE_CAPTIONS` | `auto` | `auto` = on with a local Whisper model and at least 4 CPU cores; `on` / `off` |
| `BARGE_IN_SNR_DB` | `18` | How far above the noise floor unaddressed speech must be to cut Wave off; `0` turns that off (then only "stop", "new question" and speech addressed to Wave do) |
| `WHISPER_MIN_CONFIDENCE` | `0.35` | Below this a Whisper transcript is treated as "didn't catch that" |
| `FASTER_WHISPER_BEAM_SIZE` | `5` | `1` is faster with a small accuracy cost |
| `TTS_PERSISTENT_STREAM` | `true` | One output stream kept open while Wave talks (closed after a minute idle) |

## Falling back

Nothing here can leave Wave deaf or mute:

- No `sounddevice`, `CAPTURE_ENGINE=legacy`, or the stream won't open ⇒ the old path, retried at most once a minute.
- The persistent output stream won't open or stalls ⇒ that sentence plays through a per-utterance stream, then pygame, then Piper/SAPI — the Phase 5 chain.
- No `webrtcvad` ⇒ energy-only speech detection with a stricter margin.
- No local Whisper ⇒ no live captions; Google recognition as before.

## Checking it on your PC

See "Tier 12" in `docs/MANUAL_TEST_CHECKLIST.md`: a 30-second command, unplugging and replugging the microphone, plugging headphones in mid-session, live captions, and the AEC on/off comparison.
