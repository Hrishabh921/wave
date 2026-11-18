# Time to first audio (TTFA)

How long after you stop talking does Wave start answering out loud? Tier 11 measures this on every voice turn and gives you a one-command report. SARA wrote a protocol for the same measurement (its `bench/results/voice_baseline.md`) and never filled in the table. Wave runs SARA's protocol so the two can be compared like for like.

## What is measured

Every voice turn is timed from the end of your speech:

```
you stop     silence timeout   transcript    sent to the   first text    first sound
talking      over (capture)    ready         brain         of the reply  out of Wave
   │──────────────│───────────────│─────────────│─────────────│─────────────│
speech_end    capture_end      recognized    dispatched    first_token   first_audio
```

- **TTFA = first_audio − speech_end.**
- **speech_end** is an estimate. The recognizer only knows you stopped once its silence timeout has passed, so Wave uses `capture_end − pause_threshold` (0.35–0.6 s, depending on headphones/speakers and whether Wave was talking). That timeout is part of the delay you feel, so it is counted.
- **first_audio** is the moment audio is handed to the output device. The device's own buffering (usually 10–50 ms) comes on top and can't be measured from software.
- **Interrupts** ("stop", "ruk jao", "new question: …") are timed until Wave went quiet: `audio_stopped − speech_end`.
- A spoken "Hey Wave" on its own is timed until the "Systems active." reply starts (path `wake ack`).
- Typed messages are logged too (source `typed`, timed from Enter), but the report only counts voice turns.

Where the numbers come from:

| Mark | Set by |
|---|---|
| `speech_end`, `capture_end`, `recognized` | `voice/stt.py` (timestamps on the transcript) |
| `dispatched` | `ui/main_window.py` `send_message()` |
| `first_token` | `ui/workers.py` `AIStreamWorker`, at the first piece of reply text |
| `first_audio` | `voice/tts.py`, the moment playback starts (`on_playback_start`) |
| `audio_stopped` | `ui/main_window.py` `_on_voice_stop()` |
| route (fast path / planner / LLM …) | the brain's own trace for that turn (`telemetry/trace.py`) |

Each finished turn is appended to `<data dir>/database/ttfa.jsonl` (rotated at 2 MB) — next to Wave's database, so it stays on your machine. `TTFA_ENABLED=false`, or `TELEMETRY_ENABLED=false`, turns it off.

## Reading the numbers

```
python -m telemetry.ttfa_report              # everything logged so far
python -m telemetry.ttfa_report --since 2026-11-05T18:00
python -m telemetry.ttfa_report --markdown   # paste into the results table below
```

It prints p50/p90 per path (fast path, planner, LLM, interrupt, wake ack) and per language (English, Hinglish, Hindi — guessed from the transcript), and checks the Tier 11 targets.

## Targets

| Path | Target |
|---|---|
| Fast-path commands (brain/intent_router.py) | p50 ≤ 0.8 s |
| LLM answers (cloud provider) | p50 ≤ 2.0 s |

## The protocol (20 utterances)

`python -m telemetry.ttfa_report --protocol` prints this list.

1. Note the time, then wake Wave ("Hey Wave"). Keep the mic on.
2. Say each line, one at a time. Wait for the answer to finish (for the interrupt lines, interrupt while Wave is still answering the previous question).
3. Run `python -m telemetry.ttfa_report --since <that time> --markdown` and paste the output below.

| # | Language | Path | Say |
|---|---|---|---|
| 1 | en | fast path | volume up |
| 2 | en | fast path | take a screenshot |
| 3 | en | fast path | open notepad |
| 4 | en | fast path | pause the music |
| 5 | hinglish | fast path | awaaz badhao |
| 6 | hinglish | fast path | chrome kholo |
| 7 | hi | fast path | आवाज़ कम करो |
| 8 | hi | fast path | स्क्रीनशॉट लो |
| 9 | en | planner | check my battery and then remind me to charge it in ten minutes |
| 10 | en | planner | open notepad then take a screenshot |
| 11 | hinglish | planner | pehle volume kam karo phir youtube pe lofi chalao |
| 12 | en | LLM | what's a good name for a black cat |
| 13 | en | LLM | explain what a black hole is in two sentences |
| 14 | en | LLM | what should I cook tonight with eggs and rice |
| 15 | hinglish | LLM | aaj kya karna chahiye, kuch idea do |
| 16 | hinglish | LLM | mujhe ek chhoti si kahani sunao |
| 17 | hi | LLM | मुझे एक चुटकुला सुनाओ |
| 18 | en | interrupt | (while Wave is answering) stop |
| 19 | hinglish | interrupt | (while Wave is answering) ruk jao |
| 20 | en | interrupt | (while Wave is answering) new question: what time is it |

Run it twice: once with headphones, once on laptop speakers (the silence timeout differs: 0.5 s vs 0.6 s).

## Results

### On your PC — pending

This needs a real microphone, speakers, and the cloud STT/LLM/TTS services, none of which exist in the environment Tier 11 was built in. Paste the `--markdown` output here after running the protocol.

| Path | Turns | p50 | p90 | p50 first text | p50 STT | Target | Met |
|---|---|---|---|---|---|---|---|
| fast path | | | | | | 0.80 s | |
| planner | | | | | | | |
| LLM | | | | | | 2.00 s | |
| interrupt | | | | | | | |
| wake ack | | | | | | | |

### What could be measured without hardware (Tier 11 build environment)

These are the parts of the chain that don't need a microphone or the network:

- **Brain, fast path:** about **40–60 ms** from the transcript to the first reply text for `volume up`, `chrome kholo`, `आवाज़ कम करो` — routing, the tool call with a stand-in backend, two SQLite writes, the audit row and the trace. Measured with the CPU busy training the wake-word model, so on an idle PC it is lower. It is a small slice of a 0.8 s budget.
- **Speech pipeline:** with synthesis taking S and playback P per sentence, the Tier 2 loop spoke an N-sentence reply in N × (S + P); the Tier 11 pipeline takes about S + N × P, because sentence N+1 is synthesized while sentence N plays. `tests/test_phase8_tts_stream.py::StreamPipelineTests::test_the_pipeline_hides_synthesis_behind_playback` checks this on every run (4 sentences, S = P = 0.15 s: about 0.75 s instead of 1.2 s).
- **Short fixed replies** ("Turned the volume up.", "Systems active." …) are synthesized once at start-up (`TTS_WARMUP_ENABLED`), so a fast-path command never waits for the TTS service. The phrase cache then serves them instantly.

### Expected breakdown, and what Tiers 12–13 change

For a fast-path command, the remaining time is mostly outside the brain:

| Piece | Today | After |
|---|---|---|
| Silence timeout (endpointing) | 0.35–0.6 s, fixed | Tier 12: streaming VAD endpointing, typically 0.2–0.3 s |
| Recognition | Google: network round-trip; faster-whisper `base` on CPU: depends on the CPU | Tier 12: confidence gating; Tier 13: per-utterance language |
| Brain fast path | ~0.05 s | — |
| First audio of the reply | phrase cache: ~0 s; new text: one edge-tts round-trip | Tier 13: local Piper voice by default |

So the fast-path target may well need Tier 12's endpointing to be met on speakers. The table above, filled in on your PC, is what decides that.
