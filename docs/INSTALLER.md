# Building the Windows installer (Tier 22)

Wraps the already-built `dist\WaveAI.exe` (`wave.spec`/`build_exe.bat`, see `docs/PACKAGING.md`)
in a real double-click installer: Start Menu shortcut, optional desktop icon and "start at login"
task, an uninstaller, and a launch-on-finish step. This is a 🏆 item — SARA has no installer of
its own either, and model downloading is on SARA's own backlog too, not just Wave's.

**Everything in this doc needs a real Windows machine with Inno Setup installed.** Neither exists
in the sandbox this was written in, so `installer/wave_installer.iss` has been written carefully
against Inno Setup's documented syntax, but has **never been compiled or run**. Compiling it,
running the resulting `WaveAI_Setup.exe`, and clicking through install → first run → uninstall on
a real machine is this tier's own explicit "needs your PC" item — see
`docs/MANUAL_TEST_CHECKLIST.md`'s "Tier 22" section. The `.iss` script and `requirements.lock`
below are the deliverables; a compiled, signed `.exe` is not something this sandbox can produce.

## What you need

- A Windows machine (the installer, like the `.exe` it wraps, cannot be built or tested from
  Linux/macOS — PyInstaller doesn't cross-compile, and Inno Setup itself is Windows-only).
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) installed, with `iscc.exe` on `PATH`.
- A build of `dist\WaveAI.exe` already produced (`docs/PACKAGING.md`'s own quick start).

## Build order

```powershell
# 1. Build the .exe first, in the same venv Wave normally runs in.
.\wave_env\Scripts\Activate.ps1
pip install -r requirements.lock pyinstaller
.\build_exe.bat
# -> dist\WaveAI.exe

# 2. Compile the installer, from the project root.
iscc installer\wave_installer.iss
# -> installer\output\WaveAI_Setup.exe
```

## What the installer actually does

Read top-to-bottom, `installer/wave_installer.iss` is deliberately small and documents its own
reasoning inline; the summary:

- **Per-user install, no admin prompt.** Installs to `%LOCALAPPDATA%\Programs\WaveAI` by default
  (`PrivilegesRequired=lowest`), with a dialog letting someone who wants an all-users install
  under Program Files choose that instead. This matches how `settings/config.py` already resolves
  a writable data directory next to a frozen build — no UAC elevation needed either way.
- **Ships the `.exe`, the license, and a copy of the README.** Nothing else needs bundling
  separately: `wave.spec` already bundles the "Hey Wave" wake-word model
  (`voice_models/wakewords/hey_wave.onnx`) straight into the `.exe` (see `docs/PACKAGING.md`).
- **Start Menu shortcut always; desktop icon and launch-at-login are opt-in checkboxes.**
- **Launches Wave once on finish** (the standard Inno "Launch application" checkbox) — Wave's own
  first run *is* its setup wizard (`ui/onboarding.py`, Tier 21), so this is the natural "get
  started" moment, not a separate step the installer has to build.
- **Never deletes user data on uninstall.** Chat history, memories, reminders, and notes are the
  user's own data (see the README's "Privacy & Data Flow" section) and live in
  `config.DATA_DIR`, not inside the installed program folder — an uninstall removes the program,
  never that data. A user who wants a genuine clean wipe already has an in-app, confirmation-gated
  "Clear All Local Data" action (Settings → Privacy) for exactly that, rather than a surprising,
  unrecoverable installer side effect.

## Why there's no separate "model downloader" step in the installer itself

The original Tier 22 item asked for "a first-run model downloader step." Wave already has one —
just not inside the installer. `ui/onboarding.py`'s setup wizard (Tier 21) has a consent-gated
Local Voice step that downloads a Piper voice on first run, with a pinned SHA-256 checksum
(`config.TTS_LOCAL_AUTO_DOWNLOAD`, see `voice/tts.py`'s `HAS_PIPER` block); openWakeWord's own two
small feature files (~2.5 MB) download automatically the first time Wave goes to sleep (see
`docs/WAKE_WORD.md`); Kokoro's larger bilingual voice files are a separate, also-consent-gated,
off-by-default opt-in (`TTS_KOKORO_AUTO_DOWNLOAD`). Writing a *second*, Pascal-Script version of
any of this inside the `.iss` file would mean two consent flows for the same download that could
drift out of sync with each other over time. The installer's `[Run]` step just launches
`WaveAI.exe` once; the app's own wizard — which runs identically whether someone used this
installer or just copied the bare `.exe` — does the rest.

## `requirements.lock`

A single merged, pinned file (`requirements.txt` + `requirements-voice-local.txt`, same versions,
one file) so a release build has one install step instead of two:

```powershell
pip install -r requirements.lock
```

It is **not** a full `pip-compile`-style transitive closure — see the file's own header for why
(several of Wave's packages are Windows-only wheels that can't resolve in a Linux sandbox) and the
exact command to regenerate a true one, with every sub-dependency pinned and hashed, on a real
Windows machine.

## Known gaps, honestly

- **No code-signing certificate.** `wave_installer.iss` has no `SignTool=` entry, matching
  `docs/PACKAGING.md`'s own note about the bare `.exe`. Windows SmartScreen and most antivirus
  products will warn on first run of an unsigned installer — expected, not a sign anything here is
  broken, but worth knowing before handing `WaveAI_Setup.exe` to someone else.
- **No custom app icon.** `wave.spec` doesn't set one, so neither does the installer; the comment
  in `[Files]` shows where to add `assets\icon.ico` if one gets made later.
- **Entirely unverified against a real Windows installer run.** Every bullet above describes what
  the script is *written* to do, read straight from Inno Setup's own documented directive
  behavior — not what a real install was watched doing. See `docs/MANUAL_TEST_CHECKLIST.md`.
