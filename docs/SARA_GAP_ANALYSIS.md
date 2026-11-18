# Wave vs SARA — gap analysis

Snapshot date: 2026-11-04, after Tier 9.

**What was compared.** Real source on both sides, read file by file:

- **SARA**: `aiiiiiii.zip` (`SARA-AI-Automation-Powered-Personal-Desktop-Assistant-voice-command--main`, sha256 `6db004e7…`, byte-identical to the copy uploaded at the start of this project). Read only. None of SARA's code was run.
- **Wave**: your own `wave_project_clean.zip` (sha256 `a42d3dc2…`), i.e. your real repo after merging Tier 9, not the sandbox copy Tiers 1–9 were built in. Its test suite *was* run.

Every "Wave lacks X" below was checked by grepping Wave's code, not assumed. File/line references point at the evidence.

---

## Bottom line

1. **Fix the repo first.** Your merged repo is **red**: 66 failing and 12 erroring tests, all from five leftover test files of the older Oct 18–26 parallel effort. Those files test code paths the merge replaced. There are also five orphaned modules and stale docs. This is the most urgent item, and the smallest. See §1.
2. **The biggest real gap is the voice loop**, not the feature count:
   - The wake word is "transcribe everything, then search for a substring".
   - The streaming LLM→TTS path exists but is **not wired into the UI**, so every reply waits for the full answer plus full synthesis.
   - Capture is blocking and phrase-at-a-time.
   - Hindi/English is a manual per-session setting.

   SARA's voice pipeline (~4.9K lines across STT/TTS) is a different class of system. See §2.1–2.4.
3. **The voice surface is about half of SARA's.** Wave has 83 tools against 154, and 21 deterministic fast-path rules against 140 intents (415 regex patterns). Several things Wave *can* do (shutdown, brightness, local IP, copy) exist only as **GUI buttons** and can't be reached by voice. See §2.5.
4. **Wave is genuinely ahead in places, and should stay ahead there:** cross-platform backends, provider-agnostic LLM layer, real undo for file actions, hot-reload skills, WhatsApp/webcam, and test volume (974 working test functions after cleanup, vs SARA's 602). See §4.

### Size snapshot

| | SARA | Wave |
|---|---:|---:|
| App Python (excl. tests/bench) | ~44.6K lines, 144 files | ~23K lines, 97 files |
| Frontend (JS/CSS/HTML) | ~8.7K lines (pywebview) | — (PyQt6, inside the Python above) |
| LLM tools (risk-tier table) | 154 | 83 |
| Deterministic fast-path intents | 140 intents / 415 patterns | 21 rules |
| STT code | 2,482 lines (`sara/audio/stt/`) | 522 lines (`voice/stt.py` + `buffers.py`) |
| TTS code | 2,449 lines (`sara/audio/tts/`) | 921 lines (`voice/tts.py` + `tts_cache.py`) |
| Security code | 11 files, 2,332 lines | 4 files, 706 lines |
| Proactive triggers | 9 | 2 (+ reminder heads-up/follow-up) |
| Bench golden rows / red-team rows | 596 / 174 | 129 / 31 |
| Test functions | 602 (47 files) | 1,059 (53 files); 974 after the §1 cleanup |

---

## 1. Fix first: the merged repo is inconsistent

> **Status: fixed in Tier 10** (2026-11-04). The suite is green again, the leftovers are removed, and `tests/test_repo_hygiene.py` guards against a repeat. See `NEXT_STEPS.md`.

Your repo is the Tier 1–9 tree, byte-for-byte, **plus** files only the older parallel effort had (its own "Tiers 1–9", per `NEXT_STEPS.md`). Wherever both efforts had a file at the same path, the Tier 1–9 version won. Files only the old effort had were left behind.

### 1.1 The test suite is red

`pytest tests/` on your repo: **990 passed, 66 failed, 12 errors, 3 skipped.** Run on its own, each leftover file gives:

| Leftover test file | Result alone | Why |
|---|---|---|
| `tests/test_taint_and_guard.py` | 26 failed, 3 passed, 7 errors | tests the old effort's `taint`/guard API (`begin_turn`, `wrap_and_scan`, `_guard_decision`) |
| `tests/test_tier5_additions.py` | 24 failed, 2 passed, 5 errors | tests `start_timer`/labelled stopwatches/`app_stats` streaks that no longer exist |
| `tests/test_piper_auto_download.py` | 10 failed | tests a Piper auto-download branch the current `voice/tts.py` doesn't have |
| `tests/test_stt_vad.py` | 5 failed, 15 passed | `voice/stt_vad.py`'s own unit tests pass; its `stt.py` integration tests don't |
| `tests/test_content_trust_fixtures.py` | **7 passed** | still valid — keep it |

One legitimate test, `tests/test_tier4_telemetry.py::...test_intent_router_hit_is_traced_with_its_own_route`, also fails in the full run. It passes alone (13/13), so the leftovers' side effects are breaking it.

### 1.2 Orphaned modules (nothing in the app imports them)

- `automation/timer_manager.py`: superseded by `brain/handlers/timers.py`.
- `voice/stt_vad.py`: superseded by `voice/buffers.py` + `SpeechReceiver._passes_vad()`.
- `skills/joke.py`: superseded by `brain/handlers/fun.py`.
- `skills/streak.py`: superseded by `automation/streak_manager.py`. **Also broken**: it calls `db.get_stat(...)`, which no longer exists in `database/db_manager.py`.
- `planning/test_doubles.py`: duplicates `tests/test_doubles.py`.

### 1.3 Stale docs

- `NEXT_STEPS.md` describes the old tree: 69 tools, 654 tests, `planning/schema.py` validators, Piper auto-download, an `app_stats` table, `move_window`. **None of that is in the current code.**
- `CONTRIBUTING.md` points at `voice/stt_vad.py` as the STT VAD.
- `MERGE_VALIDATION_REPORT.md` reports on an older merge.

### 1.4 Possibly lost in the merge

These are inferred from `NEXT_STEPS.md` and the leftover tests; the old versions of the overwritten files aren't in the zip:

- Piper as the default voice, with automatic voice-model download (your `piper_voices/` / `voice_models/` folders suggest you wanted this).
- `validate_url()` / `validate_app_target()` in `planning/schema.py`. Tier 6 put equivalent URL/critical-process checks in `automation/app_control.py` instead.
- `move_window` / `resize_window`. Not currently present; see §2.5.

### 1.5 Worth keeping from the old effort

- `tests/fixtures/` + `tests/test_content_trust_fixtures.py` (realistic injected/normal HTML, all passing).
- `scripts/windows_smoke_check.py` + `WINDOWS_SMOKE_TEST.md` (a real-machine smoke script Tier 9 didn't have).

### 1.6 A correction about the roadmap

The original roadmap files (`attachment-682c3055.txt`, `attachment.txt`) were in this session's uploads the whole time. I had wrongly concluded they were lost, so the Tier 8/9 I built covered different ground than the roadmap's own Tier 8/9:

- **Roadmap Tier 8** = UI polish depth: design tokens, motion. Not done (see §3.6).
- **Roadmap Tier 9** = realistic fixtures, `CONTRIBUTING.md`, a running `NEXT_STEPS`/`PROJECT_MEMORY`, and "Engineering & Safety Principles" / "Engineering Snapshot" README sections. The fixtures and the two docs exist only as the old effort's (now stale) copies. The README sections are still missing.

---

## 2. Gaps vs SARA — high priority

### 2.1 Wake word: transcription + substring match

**SARA**

- A local openWakeWord neural model runs on raw 80 ms frames (`sara/audio/stt/engine.py:1694`).
- No speech-to-text runs until the wake word fires. The docstring says so explicitly: *"Whisper/STT is NEVER used here."*
- It never listens for the wake word while TTS is speaking, and it discards that audio.
- One caveat: SARA expects a custom `models/wakewords/hey_sara.onnx` that its repo doesn't ship (`config.py:510`). Without that file, passive wake detection is disabled.

**Wave** (`ui/workers.py:98-127`): `ContinuousMicWorker` calls `stt_engine.listen()` in a loop in **both** modes. In SLEEPING mode it does `any(w in text_lower for w in self.wake_phrases)` on the full transcript. Consequences:

- **Everything said near the mic is transcribed, even while "asleep".** If faster-whisper is unavailable, that audio goes to Google's speech API (`voice/stt.py:309-312`).
- **"Hey Wave, open Chrome" in one breath only wakes Wave.** The command part is discarded; the user has to repeat it.
- **Real bug: sleep words are substrings.** In AWAKE mode, `if any(w in text_lower for w in ["mute", "sleep", "standby"])` sends Wave to sleep (`ui/workers.py:124`). "**un**mute the volume", "put the PC to sleep", "I can't sleep, play music", and "volume mute karo" all put Wave to sleep instead of reaching the brain. Every English phrasing of the `toggle_mute` rule ("mute", "unmute", "mute the volume", "mute kar do") is swallowed this way. Only "awaaz band karo" and the Devanagari forms get through.
- **No barge-in gate.** Every recognized utterance, in either mode, emits `interruption_signal` → `stop_speaking` (`ui/workers.py:118`, `ui/main_window.py:2165`). Room chatter or a TV can cut Wave off mid-sentence even while it's asleep.

*Fix:* add an openWakeWord stage ahead of STT (SARA's `requirements.txt` pins `openwakeword==0.6.0`). Strip the wake phrase and keep the rest of the utterance. Match sleep words as whole commands, not substrings.

### 2.2 Streaming exists but isn't used, and isn't pipelined

- `brain.think_stream()`, `voice/tts.py`'s `speak_stream()` and `ui/workers.py`'s `AIStreamWorker` were all built in Tier 2, but **`ui/main_window.py` never uses them**. It only creates `AIWorker` (→ `brain.think()`, full reply) and `TTSWorker` (whole reply) (lines 2220, 2266, 2331). Time-to-first-audio is therefore *full LLM generation + full synthesis*.
- `speak_stream()` itself is serial: each sentence goes through `speak_and_wait()` (`voice/tts.py:285`). Sentence N+1's edge-tts round-trip doesn't even start until sentence N has finished playing, which leaves an audible gap between sentences.

**SARA** feeds the LLM stream straight into `tts.speak_stream()` (`sara/orchestrator/dispatcher.py:1089`). That runs a three-stage chunker → synth → playback pipeline on separate threads (`sara/audio/tts/engine.py:716-840`) into a persistent output stream (`player.py`), with a startup warm-up.

*Fix:* switch `send_message()` and the voice path to `AIStreamWorker`, and make `speak_stream` synthesize ahead while the previous sentence plays. This is probably the single largest latency win available.

### 2.3 Capture pipeline depth

**Wave** (`voice/stt.py`) uses `speech_recognition`'s blocking `Microphone()`/`listen(timeout=1, phrase_time_limit=8)`. That has several consequences:

- Anything longer than **8 seconds is cut off**.
- AEC runs on the whole phrase after capture.
- VAD is a post-capture yes/no check.
- Calibration happens once at startup.
- Nothing watches whether the mic stream is still alive.
- There is no Whisper hallucination filter ("Thank you for watching." on near-silence).
- The waveform on screen is decorative: `AudioVisualizer._tick` draws `random.uniform(...)` bars (`ui/widgets_basic.py:377`), not mic level.

**SARA** (`sara/audio/stt/engine.py`, 1,979 lines) does all of the following:

- Continuous sounddevice/PyAudio callback stream.
- Per-chunk AEC worker.
- Pre-roll ring buffer, so word onsets aren't clipped.
- Noise floor and periodic recalibration (`_recalib_loop`).
- Stream health watchdog (`_watchdog_loop`).
- Known-hallucination and repetition filters (`helpers.py:178-218`).
- Confidence and speech-rate metadata on each transcript (`TranscriptionResult`).
- A gain floor so room tone isn't amplified into fake speech.
- Live mic level pushed to the UI orb.

*Practical fixes that don't need a full rewrite:*

- Raise or remove the 8 s phrase limit.
- Add `webrtcvad-wheels` to `requirements.txt`. SARA uses it; it ships prebuilt wheels, which fixes Tier 7's "webrtcvad needs a compiler" finding.
- Port SARA's hallucination phrase filter.
- Drive the visualizer from real RMS.

### 2.4 Hindi / English is manual

**STT.** The language is one setting (`en-IN` default). `_whisper_language_code()` turns that into a forced `"en"` for faster-whisper (`voice/stt.py:112-118`). Hindi speech with Whisper pinned to English tends to come back translated or mangled. There is no auto-detection.

**TTS.** One edge-tts voice at a time, changed only from Settings (`ui/settings_panel.py:340`). Whichever voice is selected (default `en-IN-NeerjaNeural`) reads every reply, whatever its language or script. Text cleanup before speaking is `text.replace("*","").replace("#","").replace("_","")` (`voice/tts.py:363`). That strips underscores out of file names too, and does nothing for numbers, times, currency, URLs or emoji.

**SARA** handles this in three places:

- It auto-detects language, including Devanagari and romanized-Hinglish markers (`sara/audio/stt/helpers.py:130`).
- It biases Whisper with a language-aware prompt.
- It routes each TTS chunk to an English or Hindi voice (`voice_params.py`) and normalizes numbers, times and currency into English *or Hindi* words (`text_prep.py`, `_int_to_words_hi`).

You write to Wave in Hinglish, so this matters for you specifically.

### 2.5 Voice surface: 83 tools vs 154, 21 fast-path rules vs 140 intents

Capabilities SARA exposes to voice that Wave lacks entirely, or has **only as a GUI button** (marked 🖱):

- **Power/session:** shutdown, restart, sleep, hibernate, log off, cancel shutdown. 🖱 restart/shutdown exist only as Apps & Power buttons (`ui/main_window.py:1159,1166`).
- **Volume:** set to an exact %, max, min, explicit mute vs **unmute**. Wave's `toggle_mute` can't "unmute" safely: if already unmuted, it mutes.
- **Brightness:** set, up, down, max, min, status. 🖱 slider only (`ui/main_window.py:1342-1356`).
- **Windows:** move, resize, maximize active, always-on-top, fullscreen, show desktop, minimize all, restore, switch window (Alt-Tab), restart an app. Wave has list/focus/minimize/snap.
- **Keyboard/browser:** type text anywhere, press a key, copy/paste/select-all, Ctrl+Z/Ctrl+Y, new/next/previous tab, reload, zoom in/out/reset, scroll up/down/top/bottom.
- **Clipboard write.** Wave can read the clipboard; 🖱 copy exists only as chat-bubble buttons.
- **Folders:** open Downloads/Documents/Pictures/Music/Videos/This PC/Recycle Bin/Task Manager/Control Panel, empty the Recycle Bin, "tell me when this download finishes" (SARA `orchestrator/notifications.py`).
- **System info:** uptime, GPU usage/temperature, process list, list services. 🖱 local IP is on the dashboard only.
- **Quick notes & to-dos:** take/read/clear note, add/list/complete/delete to-do. Wave has no to-do list at all; `write_notepad` just types into Notepad.
- **Notes study tools:** summarize my notes on X, quiz me on X (SARA `skills/notes_qa.py`). Wave has `search_my_notes`.
- **Calculator:** safe AST evaluation (`orchestrator/calc_utils.py`). Wave leaves arithmetic to the LLM.
- **Web:** summarize a URL (fetch + BeautifulSoup), play on Spotify, reliable YouTube lookup via `yt-dlp`.
- **Modes:** "study / gaming / focus mode" bundles, plus "undo my last setting change".
- **Explainability:** "why did you do that?" / "why did you notify me?" from a decision log.

The deterministic layer matters for latency. SARA routes 140 intents without an LLM round-trip; Wave routes 21, and everything else costs at least one cloud call before anything happens.

### 2.6 Screen understanding is OCR only

- Wave's voice tool `read_screen_text` is Tesseract OCR (`brain/handlers/vision.py:36`), so it can't describe a chart, a photo, or what an error dialog is about.
- Wave *does* have a vision-model path, but only for images attached in chat (`brain/groq_core.py:_think_with_image`). Its "no Groq API key" error text there is stale: the client is provider-agnostic.

**SARA**'s `screenshot_describe` sends a screenshot to a vision model and returns a voice-friendly description (`sara/tools/vision.py`).

*Fix:* add a `describe_screen` tool that reuses `_think_with_image` on a fresh screenshot.

### 2.7 Calendar is local-only

- Wave's calendar is a SQLite table (`automation/calendar_manager.py`). Real Google or Outlook events never appear, so proactive meeting nudges only cover events created inside Wave.
- **SARA** has real Google Calendar read/create over OAuth (`sara/tools/calendar.py`).

### 2.8 Proactive engine: 2 triggers vs 9

**Wave** (`automation/proactive.py`): battery and calendar. Reminder heads-up/follow-up live in `ReminderWorker`.

**SARA** (`sara/orchestrator/proactive.py`, 845 lines) adds:

- **Time-triggered routines.** Wave's own README lists "workflows are on-demand only" as a limitation.
- Morning prefetch.
- Idle/break reminder.
- Streak milestones.
- **Habit detection**: the same action at the same hour on 3+ days prompts "want to turn this into a routine?".
- Per-trigger on/off switches and cooldowns.
- Focus-mode gating.
- Optional LLM rephrasing of the nudge.

---

## 3. Gaps vs SARA — depth and robustness

### 3.1 Security depth

Risk tiers, taint and the three-outcome guard are roughly at parity. SARA goes further in several places:

- **Secret redaction** before anything is logged (`core/security/redact.py`: keys, tokens, passwords, private keys, card numbers with a Luhn check). Wave logs recognized speech verbatim (`voice/stt.py:318`) and has no redaction anywhere.
- **Security event log + Settings card** (`core/security/events.py`, `gui/app/security_api.py`), plus a user-selectable off/standard/strict mode.
- **Policy matrix** of tier × *origin* × taint × *mode*, plus exfiltration-shaped-URL detection and key-chord blocking (`core/security/policy.py`, `policy_rules.py`). Wave's decision is tier × taint.
- **Injection scanner hardening.** SARA normalizes zero-width characters, bidi controls and Unicode "tag" characters before scanning (`detector.py`). It also strips invisible HTML elements before extraction, checks text that will be *spoken verbatim* (`guard_spoken`), and drops flagged RAG hits in strict mode (`untrusted.py`). Wave's `brain/content_trust.py` regexes are solid for plain text in all three scripts, but have none of these.
- Not a gap: Wave avoids SARA's need for sensitive-path rules by confining file actions to a sanitized name under the Desktop folder (`automation/file_manager.py:_sanitize_name`). That's a good design.

### 3.2 Cancellation and timeouts

- **Wave's emergency stop hard-kills threads** with `QThread.terminate()` (`ui/main_window.py:332-341`). Qt documents that as unsafe: it can die mid-SQLite-write or while holding a lock.
- There's no graceful "stop" for the current turn.
- The plan deadline is checked only *between* steps, so one hung tool call stalls the plan (`planning/executor.py:391`).
- Ollama calls have no per-call timeout (`groq_core.py:1062-1065`).

**SARA** has cooperative turn cancellation (`_TurnCancelled`, `_plan_cancelled`), per-step dispatch timeouts on futures under an absolute plan deadline (`core/planning/executor.py`), and a network-call timeout plus circuit breaker (`orchestrator/network_utils.py`).

### 3.3 Background-thread supervision

If Wave's reminder, proactive or consolidation thread dies, nothing notices, and reminders silently stop. SARA's `orchestrator/supervisor.py` watches these threads by name and restarts the ones that are safe to restart.

### 3.4 Follow-ups and pronouns

Wave leaves "close **it**" and "what about **Jaipur**?" entirely to the LLM's chat history; its fast-path rules can't use context. SARA resolves them deterministically and asks for clarification when ambiguous (`orchestrator/context_tracking.py`).

### 3.5 Memory quality

- Wave replaces a memory only on an **exact key** match (`database/db_manager.py:361`). Tier 2's automatic consolidation can therefore pile up near-duplicate or contradicting facts under different keys.
- **SARA** supersedes a fact when a new one is ≥0.92 cosine-similar (`core/rag.py:_check_fact_supersede`). It also keeps decision and proactive-event logs (which power "why did you do that?") and a single background DB writer so the hot path never blocks on disk (`core/memory.py`, `orchestrator/db_writer.py`).

### 3.6 UI gaps

- **No page to see or edit reminders, calendar events, timers, saved memories, notes or skills.** All of it is voice/chat only. That matters most for memories: Tier 2 writes facts automatically, and there's no screen where you can review or delete them.
- No latency/performance page. Telemetry exists, but only via `python -m telemetry.dump`.
- No security page and no usage-analytics page.
- The waveform isn't real (§2.3).
- **Roadmap Tier 8 (design tokens + motion) isn't done.** `ui/theme.py` has 20 colour tokens, but radii and font sizes are hard-coded in QSS strings, and there are no motion tokens. SARA has `tokens.css`, `motion.css`/`motion.js`, `fx.js` and 10 themes; Wave has 5 + custom.

### 3.7 Evaluation breadth

- Bench: 129 golden rows against SARA's 596. SARA's set includes 141 noisy-STT variants (`bench/tools/augment_noisy.py`).
- Red-team: 31 rows against 174.
- No unmatched-utterance log to grow fast-path rules from real usage (SARA `core/unmatched_log.py`).

### 3.8 Ops

- Nothing checks for a missing mic, models or LLM at startup and reports it loudly (SARA `health_check.py`). Wave's `run_self_diagnostics` runs only when asked.
- No Ollama lifecycle manager to auto-start `ollama serve` (SARA `ollama_manager.py`).
- One `requirements.txt`, with no CPU/GPU or build split.

---

## 4. Where Wave is ahead — don't regress these

- **Cross-platform backends** (`automation/backends/`, now partly verified on a real virtual X11 desktop). SARA hard-codes `_IS_WINDOWS = platform.system() == "Windows"` per module.
- **Provider-agnostic LLM** (Groq/OpenAI/OpenRouter/Ollama/custom) and a cloud-first setup that needs no GPU. SARA's README recommends an NVIDIA GPU.
- **Real undo** of file create/delete/overwrite and screenshots, with a recoverable trash and an Activity timeline. SARA only undoes preference changes, plus a Ctrl+Z keystroke.
- **Hot-reload `user_skills/`.** SARA discovers skills once at startup.
- **WhatsApp message sending, webcam photo, file create/delete/search, PDF/document attachments in chat.** SARA can only *open* WhatsApp.
- **Test volume**: 974 working test functions after the §1 cleanup (967 from Tiers 1–9 plus the 7 fixture tests worth keeping), against SARA's 602. SARA's `tests/e2e/` holds only fake adapters, with no actual e2e test, so Wave isn't behind there.
- **More pages** (11 vs SARA's 6 sections) and a native PyQt6 shell.

---

## 5. Docs and legal

- `LICENSE` is still **0 bytes** while the README says MIT. Still unresolved, and Piper (GPL-3.0) makes it matter.
- **No Privacy & Data Flow section.** Wave is cloud-first (Groq LLM, Microsoft edge-tts, Google STT fallback, DuckDuckGo), so users deserve the plain list SARA's README gives.
- No screenshots, and none of the roadmap's "Engineering & Safety Principles" / "Engineering Snapshot" sections.

---

## 6. Suggested order

Superseded by `docs/ROADMAP_BEAT_SARA.md`, which turns every gap above into Tiers 10–22. It sets measurable targets taken from SARA's own published bench numbers and backlog.
