# Phase 5 - real AEC, its stt.py wiring, and a fixed TTS fallback signal

## 1. Real Acoustic Echo Cancellation (`voice/aec.py`)
Replaces the old guess - raise the mic's energy threshold while TTS is speaking, hope that tells
real speech apart from Wave's own voice leaking through the speakers - with actual reference-signal
cancellation: `voice/tts.py`'s playback callback feeds the exact PCM samples being sent to the
speaker into `feed_far_end()` as they play, and `voice/stt.py` runs every captured utterance through
`process_near_end()` before handing it to faster-whisper/Google. Uses the same WebRTC Audio
Processing Module (AEC + Noise Suppression + AGC + VAD) Chrome/Meet/Teams use for this, via the
optional `aec-audio-processing` package.

Entirely optional, same as every other dependency in this project: if the package or `numpy` isn't
installed, or `AEC_ENABLED=false`, Wave falls straight back to the energy-threshold heuristics alone
- exactly the behavior before this feature existed.

```dotenv
AEC_ENABLED=true            # false restores the old energy-threshold-only behavior
AEC_SAMPLE_RATE=16000       # WebRTC APM's native rate; audio is resampled to/from this as needed
AEC_STREAM_DELAY_MS=80      # rough mic/speaker round-trip estimate - raise if echo lags in testing
AEC_ENABLE_NS=true          # noise suppression
AEC_ENABLE_AGC=false        # automatic gain control
AEC_ENABLE_VAD=false        # voice activity detection
```

## 2. The stt.py side of the wiring - `SpeechReceiver._apply_aec()` (`voice/stt.py`)
Kept deliberately separate from AEC's own frame-handling logic in `voice/aec.py` so each is testable
on its own. The contract at this boundary: passthrough (audio unchanged) whenever AEC is disabled or
the package is unavailable, correct `AudioData` round-tripping when it's enabled, and fail-safe
passthrough on any error mid-processing - a bug in the AEC path degrades to "no echo cancellation for
this one utterance," never a crashed or dropped turn.

## 3. TTS playback fallback chain - now with an honest success/failure signal (`voice/tts.py`)
🔧 Fix: `speak_and_wait()` used to have no way to report that playback silently failed end-to-end -
`finished_speaking` fired identically whether the user actually heard anything or not. Now
`VoiceEngine.last_playback_ok` is `True` for a normal **or** a user-interrupted utterance, and `False`
only when every tier genuinely failed, in order: edge-tts+sounddevice, edge-tts+pygame, Piper (local
neural voice, if `TTS_LOCAL_ENABLED` and `TTS_LOCAL_MODEL_PATH` point at a downloaded model), then
Windows SAPI. `ui/workers.py` is the first caller that can now tell the difference between "spoke"
and "silently didn't."

## Tests
```powershell
python -m unittest discover -s tests -p "test_phase5_*.py"
```
Stdlib only (pytest also collects them). Run against fakes - a fake `aec_audio_processing.AudioProcessor`
mimicking its documented API, fake `pygame`/`edge_tts` - so none of the real audio packages need to be
installed to exercise the logic: frame re-chunking, padding/trim correctness, the fallback-chain order,
and `last_playback_ok`'s semantics. They do not play real audio and do not verify actual echo
cancellation quality - that still needs ears, a real mic/speaker pair, and `docs/PLATFORM.md`-style
manual testing on real hardware.
