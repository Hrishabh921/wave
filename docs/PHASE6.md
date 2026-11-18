# Phase 6 - Tier 3: governance, proactive nudges, notes RAG, and a local calendar

## 1. Governed tool-calling loop - Tier 3 gap #6 (`brain/groq_core.py`'s `_ToolLoopGovernor`)
Before this, a turn made exactly one round of tool calls no matter what the model wanted - a request
needing two dependent steps ("check my battery, and if it's low remind me to charge it") had to be
guessed in a single shot or not at all, since the old follow-up pass always forced `tool_choice="none"`.
Now the model can see a tool's result and decide to call another tool based on it, bounded two
independent ways so a confused or genuinely-looping model can never hang a turn: a step-count cap and
a wall-clock deadline, either of which ends the loop. A round already in flight is always allowed to
finish rather than being cut off mid-call; one governor instance per turn, never reused.

```dotenv
TOOL_LOOP_MAX_STEPS=6      # hard cap on tool-calling rounds in a single turn
TOOL_LOOP_TIMEOUT_S=45     # wall-clock budget for the whole loop; also passed as a per-call timeout
                           # where the provider supports it (llm_provider.py's OpenAICompatClient)
```

## 2. The proactive engine - Tier 3 gap #5 (`automation/proactive.py`)
Before this, Wave was purely reactive - it only ever spoke when asked something. (Reminders already
fire on their own, but that's the user explicitly asking to be told something later; nothing was
watching ambient signals and deciding to speak up unprompted.) `ProactiveEngine` is a plain-Python
daemon thread with zero Qt dependency - `ui/main_window.py` bridges it into the GUI thread with a
`pyqtSignal`, the same `add_listener(...)`-callback pattern already used for `automation.audit.audit`.

Two triggers for now:
- **Battery** - nudges once when at/under `PROACTIVE_BATTERY_THRESHOLD`% and not charging, with a
  cooldown *and* hysteresis so it nudges once rather than on every check while sitting at a low
  level; re-arms once charging or back above the threshold.
- **Calendar** - nudges once per event id when a stored event is within
  `PROACTIVE_CALENDAR_LEAD_MINUTES` of starting.

State (cooldowns, which events/dips have already been nudged) is intentionally RAM-only, same
philosophy as `brain/skill_loader.py`'s file-signature cache - a restart re-arming a still-low-battery
nudge once is a harmless edge case.

```dotenv
PROACTIVE_ENABLED=true               # false restores the old purely-reactive behavior
PROACTIVE_CHECK_INTERVAL=60
PROACTIVE_BATTERY_ENABLED=true
PROACTIVE_BATTERY_THRESHOLD=20
PROACTIVE_BATTERY_COOLDOWN_MIN=30
PROACTIVE_CALENDAR_ENABLED=true
PROACTIVE_CALENDAR_LEAD_MINUTES=10
```

## 3. "Mere notes se batao" - notes RAG - Tier 3 gap #7 (`automation/notes_rag.py`)
Before this, Wave could only answer from facts it had been explicitly told out loud
(`remember_this`/`recall_memory`) - no way to read the user's own files and answer from them. This
indexes `.txt`/`.md`/`.pdf` files from the same common folders `file_manager.py`'s `search_file`
already looks in (Desktop, Documents, Downloads), plus an optional `WAVE_NOTES_DIR` override, chunks
each file, and embeds each chunk with the same optional sentence-transformers pipeline semantic
memory already uses (`database/embeddings.py`) - degrading the same graceful way the rest of the app
does when that package isn't installed: search still works via a plain keyword fallback, just without
semantic ranking.

```dotenv
NOTES_RAG_ENABLED=true
WAVE_NOTES_DIR=              # extra folder to search, on top of the common ones above
NOTES_CHUNK_CHARS=1200
NOTES_CHUNK_OVERLAP=150
NOTES_MAX_FILE_MB=20
NOTES_MAX_PDF_PAGES=60
```

## 4. Local calendar (`automation/calendar_manager.py`)
Before this, "calendar" had zero real backing anywhere in the codebase - Wave could set a one-off
reminder/alarm (`brain/groq_core.py`'s `set_reminder`) but had no concept of a scheduled *event*:
something with a start time, usually a duration, sometimes a location, that the user wants listed
back ("what's on my calendar today"). `CalendarManager` mirrors `_set_reminder`'s own `dateparser`
usage as closely as possible - same library, same settings, same "tell the user plainly if parsing
failed" behavior - just storing to `calendar_events` instead of `reminders`, and supporting range
queries (today/tomorrow/this week) on top of single points in time. It's a stateless,
classmethod-based backend (same shape as `FileManager`/`AppController`); it does not open its own
`automation.audit.action` scope since `brain/groq_core.py`'s `_dispatch_tool` already wraps every
tool call in one.

## Tests
```powershell
python -m unittest discover -s tests -p "test_phase6_*.py"
```
Stdlib only (pytest also collects them). None of these four need PyQt6 or a real `groq` package -
`test_phase6_governance.py` reuses `test_phase4_brain.py`'s fake-OpenAI-server harness (`_fake_openai.py`)
plus faked `pyautogui`/`pywhatkit`, and the other three are plain-backend tests against the real
(temp-file-backed) `database/db_manager.py`. They do not exercise the GUI-thread bridging in
`ui/main_window.py` itself - see Phase 4's UI-logic stand-in pattern for that style of test.
