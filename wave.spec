# -*- mode: python ; coding: utf-8 -*-
"""
wave.spec - PyInstaller build spec for Wave AI (Phase 4).

    pip install -r requirements.txt pyinstaller
    pyinstaller wave.spec --clean

produces dist/WaveAI.exe - see docs/PACKAGING.md for the full walkthrough,
the known Windows-packaging gotchas (comtypes/pycaw, winsdk, pywin32), and
how to test the result. Build ON WINDOWS, in the SAME venv Wave normally
runs in (PyInstaller bundles whatever is importable in the environment it
runs from - it does not cross-compile).

Two build knobs, read from the environment so nobody has to edit this file
for the common cases:

    WAVE_BUILD_CONSOLE=1       keep the console window (see tracebacks/prints
                                while you're getting a build working; drop it
                                for the build you actually hand out)
    WAVE_BUILD_FULL_OFFLINE=1  also bundle faster-whisper + sentence-transformers
                                (local STT / semantic memory). Off by default -
                                see docs/PACKAGING.md for why.
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

PROJECT_ROOT = os.path.abspath(os.path.dirname(os.path.abspath(SPEC)))
block_cipher = None

# ---------------------------------------------------------------------------
# datas: non-.py files Wave needs at runtime
# ---------------------------------------------------------------------------
datas = [
    # The hot-reload-skills template (docs/PHASE4.md: "copy _example_skill.py to
    # dice.py"). From source it's just sitting in the repo; in a frozen build
    # DATA_DIR starts out empty, so brain/skill_loader.py's _seed_example_skill()
    # copies this bundled copy into place the first time Wave runs.
    ("user_skills/_example_skill.py", "user_skills"),
]

# qtawesome resolves icons like "fa6s.microphone" against its own bundled font +
# charmap files at runtime (ui/main_window.py's ICON() helper) - without these
# as data files every icon silently falls back to emoji.
datas += collect_data_files("qtawesome")

# dateparser (reminders/alarms, brain/groq_core.py) ships its locale/calendar
# data as package data (sometimes in a separate dateparser_data package) - collect
# binaries+datas+submodules together rather than guessing which this install has.
dateparser_all = collect_all("dateparser")
datas += dateparser_all[0]

# winsdk (now-playing media info, monitoring/media_info.py) is a WinRT projection:
# collect_all pulls in its per-namespace native modules that plain import analysis
# can't see.
winsdk_all = collect_all("winsdk")
datas += winsdk_all[0]

# If you've dropped tesseract.exe + tessdata/ next to this spec (the same "drop it
# in the project root" trick the source tree already supports - see
# vision/ocr_engine.py), bundle them too. Harmless no-op if they're not there;
# Wave just falls back to a system-wide Tesseract install at runtime either way.
# NOTE: in --onefile mode these land in the temp _MEIPASS folder, which
# config.BASE_DIR also points at while frozen - see docs/PACKAGING.md if you'd
# rather they live next to the .exe instead (onedir, or WAVE_DATA_DIR).
if os.path.isfile(os.path.join(PROJECT_ROOT, "tesseract.exe")):
    datas.append(("tesseract.exe", "."))
if os.path.isdir(os.path.join(PROJECT_ROOT, "tessdata")):
    datas.append(("tessdata", "tessdata"))

# Tier 11: Wave's own "Hey Wave" wake-word model + its card (MIT, ~0.8 MB -
# see docs/WAKE_WORD.md). voice/wakeword.py looks for it under APP_DIR. It
# only runs when openwakeword + onnxruntime are in this venv
# (requirements-voice-local.txt); openWakeWord's own feature files are
# downloaded on first use into DATA_DIR, never bundled.
for _name in ("hey_wave.onnx", "hey_wave.json"):
    if os.path.isfile(os.path.join(PROJECT_ROOT, "voice_models", "wakewords", _name)):
        datas.append((os.path.join("voice_models", "wakewords", _name), os.path.join("voice_models", "wakewords")))
if os.environ.get("WAVE_BUILD_FULL_OFFLINE") == "1":
    try:
        datas += collect_data_files("openwakeword")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# hiddenimports: modules nothing *statically* imports by name
# ---------------------------------------------------------------------------
hiddenimports = []
hiddenimports += dateparser_all[1]
hiddenimports += winsdk_all[1]

# Chat code blocks are highlighted by looking up a Pygments lexer/style FROM A
# STRING the LLM put in a ```fenced block (ui/main_window.py: get_lexer_by_name,
# get_style_by_name) - which lexer that turns out to be isn't known until
# runtime, so static analysis only ever sees the one or two you happened to
# test with. Bundle all of them.
hiddenimports += collect_submodules("pygments")

# comtypes (brain/groq_core.py + ui/main_window.py: real volume control via
# pycaw/AudioUtilities) generates its COM wrapper classes on first use rather
# than importing them by name - collect_submodules only helps with the parts
# that already exist as real modules. See docs/PACKAGING.md for the manual
# step this still needs (populating/bundling comtypes' gen cache).
hiddenimports += collect_submodules("comtypes")

# win32com.client.Dispatch("SAPI.SpVoice") (voice/tts.py's offline fallback) is
# late-bound COM, which doesn't need a gen cache - but pywin32's own time-zone
# support module is a textbook PyInstaller hidden-import that's easy to miss.
hiddenimports += ["win32timezone"]

# groq (official SDK) and edge_tts/aiohttp are imported statically (groq inside
# a try/except in brain/llm_provider.py, which PyInstaller's analysis still
# sees) - no manual help needed as long as they're installed in this venv.

excludes = ["tkinter", "PyQt5", "PySide2", "PySide6"]

# faster-whisper (-> ctranslate2, a sizeable native dependency) and
# sentence-transformers (-> torch/transformers, hundreds of MB and a frequent
# source of its own PyInstaller issues) are BOTH already optional-with-
# fallback at runtime (settings/config.py: FASTER_WHISPER_ENABLED /
# SEMANTIC_MEMORY_ENABLED - Wave falls back to Google STT / plain key-value
# memory if either import fails). Excluding them from the default build keeps
# that exact same graceful-degradation behaviour instead of trading it for a
# much bigger, more fragile .exe. Set WAVE_BUILD_FULL_OFFLINE=1 to include them
# (and make sure both packages are actually installed in this venv first).
if os.environ.get("WAVE_BUILD_FULL_OFFLINE") != "1":
    excludes += ["faster_whisper", "ctranslate2", "sentence_transformers", "torch",
                 "torchaudio", "torchvision", "transformers"]

a = Analysis(
    ["wave_start.pyw"],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WaveAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX + some pywin32/comtypes DLLs is a known source of
                         # false antivirus flags on top of the usual unsigned-exe
                         # SmartScreen warning - see docs/PACKAGING.md.
    upx_exclude=[],
    runtime_tmpdir=None,
    console=os.environ.get("WAVE_BUILD_CONSOLE") == "1",
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["assets/icon.ico"] if os.path.isfile(os.path.join(PROJECT_ROOT, "assets", "icon.ico")) else None,
)
