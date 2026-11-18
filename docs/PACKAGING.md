# Packaging a standalone .exe (`wave.spec`)

Turns the source checkout into one `WaveAI.exe` a person can run on a Windows machine that has
no Python installed. Must be built **on Windows** - PyInstaller bundles whatever is importable in
the environment it runs in; it does not cross-compile from Linux/macOS, and several of Wave's
packages (`pycaw`, `winsdk`, `pywin32`) only install on Windows in the first place.

## Quick start

```powershell
.\wave_env\Scripts\Activate.ps1        # the SAME venv Wave normally runs in
pip install -r requirements.txt pyinstaller
.\build_exe.bat
```

`dist\WaveAI.exe` is the result (one file, no installer). `build_exe.bat` just runs
`pyinstaller wave.spec --clean --noconfirm` with a couple of sanity checks - see that file or
run the pyinstaller command directly if you want more control.

## What actually ships in the .exe

* All of Wave's own `.py` files, compiled in - nothing extra to do there, since
  `automation/backends/__init__.py` and `brain/skill_loader.py` were deliberately written to use
  only plain, static imports (see the comments in both) specifically so PyInstaller's import
  analysis can see everything without hidden-import hints.
* `user_skills/_example_skill.py`, so "copy `_example_skill.py` to `dice.py`" (`docs/PHASE4.md`)
  has something to copy even when there's no source checkout next to the .exe. `brain/skill_loader.py`'s
  `_seed_example_skill()` copies it into the live, writable `user_skills/` folder the first time
  Wave runs frozen; it's a no-op when running from source (the file's already right there).
* `qtawesome`'s icon font/data, `dateparser`'s locale data, and `winsdk`'s per-namespace modules -
  all three are pulled in with `collect_all`/`collect_data_files` in `wave.spec` because Wave reaches
  them by name/string at runtime (an icon name, a language code, a WinRT namespace), which plain
  import analysis can't see ahead of time.
* Every Pygments lexer and style (`collect_submodules("pygments")`) - which one chat code-block
  highlighting needs (`get_lexer_by_name(code_lang)` in `ui/main_window.py`) depends on whatever
  language tag the LLM puts in a ```` ```lang ```` fence, so there's no fixed set to guess at build time.

## What's deliberately left out by default

**`faster-whisper` and `sentence-transformers`** (local/offline speech recognition and semantic
memory) are excluded from the default build. Both are already optional-with-fallback at *runtime*
(`settings/config.py`: `FASTER_WHISPER_ENABLED` / `SEMANTIC_MEMORY_ENABLED` - Wave falls back to
Google STT / plain key-value memory if the import fails, same as if the package were never
installed). Their own dependencies (`ctranslate2`, `torch`/`transformers`) are large native
packages that are a common source of PyInstaller-specific breakage (missing CUDA DLLs it tries to
probe for, multi-hundred-MB to multi-GB exes, long build times) - excluding them keeps the default
build small and reliable instead of trading working voice input for a fragile one. If you want them
in:

```powershell
set WAVE_BUILD_FULL_OFFLINE=1
pip install faster-whisper sentence-transformers
.\build_exe.bat
```

**Tesseract OCR itself** (the `tesseract.exe` binary, not the `pytesseract` Python wrapper) is still
external, exactly like running from source: install it system-wide, or drop `tesseract.exe` +
`tessdata/` next to `wave.spec` before building and `wave.spec` bundles them in automatically.
⚠️ In **--onefile** mode those land inside the temporary `_MEIPASS` extraction folder (which is
also what `config.BASE_DIR` points at while frozen - see `vision/ocr_engine.py`), which is correct
for Wave's own auto-detection but means a user who installed Tesseract system-wide needs nothing
extra, while "just drop tesseract.exe next to WaveAI.exe after the build" (the source-tree
convention) **will not be auto-detected in a onefile build** - only in `--onedir` (see below), where
the bundle folder IS the folder next to the .exe. If you're relying on the bundled-Tesseract route
rather than a system install, build `--onedir`.

## onefile vs onedir

`wave.spec` builds `--onefile` (one `WaveAI.exe`, matches "package it as a .exe"). The trade-off:
onefile re-extracts itself into a temp folder on every launch (a second or so of extra startup
time, and the `_MEIPASS`-vs-"next to the exe" wrinkle above), while `--onedir` starts faster and
makes "drop a file next to the exe" behave the same as running from source, at the cost of handing
out a folder instead of a single file. To build onedir instead:

```powershell
python -m PyInstaller wave.spec --clean --noconfirm
REM then edit wave.spec: remove a.binaries / a.datas from the EXE(...) call and add
REM     coll = COLLECT(exe, a.binaries, a.datas, name="WaveAI")
REM right after it - PyInstaller's own docs call this pattern "one-folder mode".
```

## Before you hand the .exe to anyone

1. **Run it from a different machine, or at least a different folder**, than this source
   checkout. It's easy for a build to accidentally work only because some file from the project
   folder (a stray `.env`, an already-populated `database/`) is still sitting next to it.
2. **Delete your own test data first** - `database\`, `trash\` and `wave_system.log` get created
   next to the .exe the moment you run it (see `settings/config.py`'s `resolve_dirs` /
   `docs/PHASE4.md`'s "Data folders" section); nobody else wants your chat history and memories
   bundled in.
3. **Expect a Windows SmartScreen / antivirus warning** the first time anyone runs it - the .exe
   isn't code-signed (that needs a paid certificate, not something PyInstaller does), and an
   unsigned, freshly-built, self-extracting exe is exactly the shape of file AV heuristics flag on
   sight. This is normal for an indie PyInstaller app, not a sign the build is broken.

## Troubleshooting

**Volume control (`pycaw`/`comtypes`) throws on the frozen .exe but works from source.**
The most likely cause: `comtypes` generates its COM wrapper classes (`comtypes.gen.*`) the *first
time* some code touches that COM interface, normally caching them in `%LOCALAPPDATA%\Temp\comtypes_cache`
or `site-packages\comtypes\gen` - a fresh frozen build has neither. Fix: run Wave from source once
on the **build machine** so that cache gets populated before you freeze, or trigger the same call
manually (`from pycaw.pycaw import AudioUtilities; AudioUtilities.GetSpeakers()`) right before
building. `wave.spec` already does `collect_submodules("comtypes")`, which bundles whatever's
*already generated* - it can't invent interfaces that don't exist yet.

**"No module named 'X'" at runtime, but `pip list` shows it installed.**
You built in a different environment than the one you installed into (a global Python vs. the
venv, most often). Re-activate the venv and rebuild; `pyinstaller wave.spec --clean` to be sure
nothing stale from a previous attempt is cached.

**pywin32 (`win32com`, SAPI fallback in `voice/tts.py`) errors about missing `pywintypes3x.dll` /
`pythoncom3x.dll`.** A known, occasional PyInstaller + pywin32 gap. Copy the two DLLs from
`<venv>\Lib\site-packages\pywin32_system32\` into `dist\` (onedir) or pass them as
`--add-binary` in the spec's `Analysis(binaries=[...])` (onefile).

**The build works but is enormous (500MB+).** You likely have `WAVE_BUILD_FULL_OFFLINE=1` set, or
`torch`/`opencv` pulled in extra backends you don't need. Check `build\WaveAI\warn-WaveAI.txt`
after a build - PyInstaller lists every module it actually bundled.

## Still not done

* No code signing (needs a paid certificate - out of scope for a build script) and no installer/MSI,
  just the raw .exe `build_exe.bat` produces.
* No auto-update mechanism - a new version is a new .exe someone downloads again.
* This has been built and sanity-checked for spec correctness in a non-Windows sandbox (no PyQt6 /
  pycaw / winsdk available there to actually run it end-to-end) but **not yet run and clicked
  through on a real Windows machine**. Treat the first real build as the real test, and work
  through this Troubleshooting section if something the sandbox couldn't catch comes up.
