# Phase 4 - what's new and how to try it

## 1. Switch the LLM with one line (`brain/llm_provider.py`)
```dotenv
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
# LLM_MODEL=...   (optional - every provider has a default; model ids change often, so this always wins)
```
`groq` (default, unchanged), `openai`, `openrouter`, `ollama`, or `custom` + `LLM_BASE_URL` for any server that speaks
the OpenAI chat API. Settings > Advanced shows the active provider and has a **Test connection** button. A missing or
placeholder key is reported in plain words and Wave falls through to its existing Ollama / keyword tiers.
The client adapts to the provider: if a model rejects `temperature` or `max_tokens` (reasoning models do), it retries
without/renamed and remembers, so you pay for the mismatch once.

## 2. Drop-in skills with hot reload (`brain/skill_loader.py`)
Copy `user_skills/_example_skill.py` to `user_skills/dice.py`, save, and say "roll a dice". Edit the file and save again
- the new version replaces the old; make a typo and the previous working version keeps running while a toast explains
the error; delete the file and the tool disappears. It runs with Wave's own permissions - only drop in code you trust.
In a PyInstaller build a skill can only import modules that were bundled into the exe.

## 3. Platform layer - see `docs/PLATFORM.md`

## 4. Activity + undo (`automation/audit.py`, `automation/undo.py`)
Open **Activity** in the sidebar for a timeline of what Wave did. Rows that can be taken back have an **Undo** button;
**Undo last** and the voice command ("undo that", "wapas kar do") work too - even while the LLM is offline.
Undoable: file/folder creation, file deletion, overwriting a file, screenshots. Deleted files wait in `database/trash`
for `TRASH_RETENTION_DAYS` (7); a file that was replaced is backed up first; undo never overwrites a file that appeared
in the meantime and never deletes a non-empty folder. With "Save chat history locally" off, the log stays in RAM only.
"Clear All Local Data" also wipes the log and the trash.

## 5. Smoother UI
Measured: the system monitor blocked the GUI thread ~100 ms every second (`psutil.cpu_percent(interval=0.1)`); now ~0.03 ms.
Timers/animations idle while minimised or hidden, the backdrop blur isn't re-rendered during a window drag-resize,
the sparkline draws one polyline per frame, and network speed uses real elapsed time.

## 6. Packaging (`wave.spec`)
```powershell
pip install -r requirements.txt pyinstaller
.\build_exe.bat
```
Builds `dist\WaveAI.exe` - one file, no Python install needed to run it. Must be built on Windows
(PyInstaller doesn't cross-compile, and `pycaw`/`winsdk`/`pywin32` only install there anyway).
`faster-whisper` and `sentence-transformers` are left out of the default build on purpose - both
already degrade gracefully at runtime if missing, and their native dependencies are the single
biggest source of PyInstaller breakage; `WAVE_BUILD_FULL_OFFLINE=1` includes them. Full walkthrough,
the comtypes/pycaw gen-cache gotcha, and troubleshooting: `docs/PACKAGING.md`.

## Data folders
`settings/config.py` now separates `APP_DIR` (code/resources) from `DATA_DIR` (database, trash, screenshots, settings,
themes, `user_skills/`, log). Running from source both are the project folder. In a PyInstaller build `DATA_DIR` is the
folder next to the .exe (or `%LOCALAPPDATA%\\WaveAI` if not writable); override with `WAVE_DATA_DIR`.

## Tests
```powershell
python -m unittest discover -s tests -p "test_phase4_*.py"
```
Stdlib only (pytest also collects them). They cover the provider layer against a fake OpenAI server, the skill loader,
audit/undo on a real temp folder, the Windows call logs, and the new UI logic against a PyQt6 stand-in (skipped if real
PyQt6 is installed). They do not render the UI and do not build the .exe.
