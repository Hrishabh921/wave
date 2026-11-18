# Building and running Wave

A practical guide for a contributor (or future-you) setting Wave up from source, running its
tests and bench, and producing a standalone build. For what's actually shipped tier by tier, see
`CHANGELOG.md`; for the roadmap that drove it, `docs/ROADMAP_BEAT_SARA.md`.

## 1. Dev environment

Tested with **Python 3.11** on Windows (`requirements.txt`'s own first line; the CI workflow
below pins the same version). There's no `pyproject.toml`/`setup.py` — Wave runs straight from
the source tree, not as an installed package.

```bash
python -m venv wave_env
# Windows: .\wave_env\Scripts\Activate.ps1   |   Linux/macOS: source wave_env/bin/activate
pip install -r requirements.txt
```

`requirements.txt` has two parts:

- **Core** (Wave won't start without these): PyQt6, `SpeechRecognition`/`PyAudio` (mic capture),
  `pygame`/`edge-tts` (cloud TTS), `groq` (default LLM — optional since Phase 4, only needed while
  `LLM_PROVIDER=groq`), `requests`/`urllib3`, `psutil`, `pyautogui`, `opencv-python`/`pytesseract`
  (OCR), `winsdk` (Windows media info), `pypdf`/`Pillow`/`Markdown`.
- **Optional** (every one of these degrades gracefully if missing — see each module's own
  `HAS_X` flag): `pycaw`/`comtypes`/`pywin32` (real Windows volume/SAPI), `keyboard` (hotkeys),
  `qtawesome`/`Pygments` (UI icons/syntax highlighting), `screen-brightness-control`,
  `duckduckgo-search`, `ollama` (local-LLM fallback), `dateparser`, `faster-whisper`/`numpy`
  (local STT), `sentence-transformers` (semantic memory), `sounddevice`/`aec-audio-processing`
  (real AEC), `yt-dlp`, `beautifulsoup4`, the three `google-*` packages (Calendar sync).

For the on-device voice pieces added in Tier 11 (acoustic wake word, real VAD, local neural
voice), add a second install:

```bash
pip install -r requirements.txt -r requirements-voice-local.txt
```

This pulls in `openwakeword`+`onnxruntime` (Wave's bundled "Hey Wave" model), `webrtcvad-wheels`
(prebuilt wheels — no compiler needed, unlike plain `webrtcvad`), `piper-tts` (GPL-3.0, local
English voice) and `kokoro-onnx` (MIT, the only one of the two that speaks Hindi). Every one of
these is optional; without this file Wave keeps working with transcript-based wake word, energy
VAD, and cloud/SAPI voices. Piper is never bundled into the frozen `.exe` by default (see §5) —
install it yourself if you want it; `voice/tts.py`'s `HAS_PIPER` block explains why it's an
opt-in rather than a default dependency.

## 2. Running it

```bash
python wave_start.pyw
```

Starts Wave minimized to the system tray (`window.showMinimized()`), using `config.DATA_DIR` as
its working directory (the project folder when run from source; the folder next to the `.exe`,
or `%LOCALAPPDATA%\WaveAI`, in a frozen build).

Added in Tier 19 — a no-GUI healthcheck mode that works even without PyQt6 installed (the GUI
import is deferred past this path):

```bash
python wave_start.pyw --healthcheck
```

Checks mic, speaker, voice models, the configured LLM provider and Tesseract OCR, prints a
report, and exits 0 if everything critical is OK, 1 otherwise. Any other argument is passed
through untouched (handled by `parse_known_args()`, not `parse_args()`) so a launcher or desktop
shortcut quirk never breaks startup.

## 3. Running the tests

```bash
cd tests
python3 -m pytest -q          # fast, with subtests
python3 -m unittest discover  # the second cross-check this project runs every tier
```

Both are run after every tier in this project's own history (see `CHANGELOG.md`'s per-tier test
counts) — `pytest` and `unittest discover` report slightly different totals because `pytest`
counts each `subTest()` once per assertion group while `unittest discover`'s count includes a
few more as flat test cases; a healthy run shows the same number of *skips* either way (3, all
`webrtcvad`-gated, in this sandbox — see `tests/test_voice_buffers.py`).

CI (`.github/workflows/ci.yml`) runs on **`windows-latest` only** — `requirements.txt` pins
Windows-only packages (`winsdk`, `pywin32`, `pycaw`, `comtypes`), so a plain `pip install -r
requirements.txt` hard-fails on Linux/macOS runners before a single test can run. It also runs
`pyflakes` (real-issues-only, since this codebase's `try/except HAS_X` optional-import pattern
routinely trips pyflakes' unused-import check) and a non-blocking `mypy --ignore-missing-imports`
pass.

## 4. Running the bench

```bash
python -m bench.run_bench --mode all --compare
```

Flags (from `bench/run_bench.py`'s own `argparse` setup):

| Flag | Meaning |
|---|---|
| `--mode {router,redteam,all}` | which eval to run (default `all`) |
| `--files` | comma-separated golden filenames (default: all of them) |
| `--baseline` | path to the baseline results file to compare against |
| `--thresholds` | path to the pass/fail threshold config |
| `--redteam-dir` | directory of red-team attack rows |
| `--compare` | exit non-zero on a regression vs. the baseline — what CI/this project's own per-tier verification actually uses |
| `--accept-baseline` | write this run's results as the new baseline (use after a deliberate, reviewed change) |
| `--report-md` / `--json` | write a Markdown/JSON report to a path, instead of (or alongside) the printed summary |
| `--quiet` | suppress the printed summary |

## 5. Building a standalone `.exe`

This already exists — `wave.spec` (a PyInstaller spec) plus `build_exe.bat`, documented in full
in `docs/PACKAGING.md`. Must be built **on Windows**, in the same venv Wave normally runs in
(PyInstaller bundles whatever is importable in the environment it runs from — it doesn't
cross-compile, and `pycaw`/`winsdk`/`pywin32` only install on Windows regardless):

```powershell
.\wave_env\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
.\build_exe.bat
```

Produces `dist\WaveAI.exe` (one file, no installer). Two build-time environment knobs
`wave.spec` reads: `WAVE_BUILD_CONSOLE=1` keeps a console window for debugging the build itself,
and `WAVE_BUILD_FULL_OFFLINE=1` also bundles `faster-whisper` + `sentence-transformers` (off by
default — see `docs/PACKAGING.md` for the size/licensing tradeoff). `wave.spec` already handles
the non-obvious data files Wave needs at runtime that plain import analysis can't see: the
hot-reload-skills template, `qtawesome`'s icon font, `dateparser`'s locale data, `winsdk`'s
per-namespace modules, and every Pygments lexer/style.

Tier 22's own roadmap item is a step beyond this: a proper **installer** (Inno Setup `.iss`) that
wraps this `.exe` with a Start Menu/desktop shortcut and launch-on-finish step, plus a merged,
pinned `requirements.lock` — see `docs/INSTALLER.md` and `installer/wave_installer.iss`; this file
only covers the `.exe` itself, which was already built before Tier 22 started.

## 6. Platform support

Windows is the real, tested target (`requirements.txt`'s own header says so, and CI only runs
there). Linux and macOS are **experimental**: every system-control tool goes through
`automation/backends/` (`base.py` defines the interface; `windows.py`/`linux.py`/`macos.py` each
implement it), so a new tool automatically gets a real Windows path and a best-effort
Linux/macOS one — but only the Windows path has been exercised against real hardware in this
project's own tier-by-tier verification (see each tier's "Needs your PC" notes in `CHANGELOG.md`
and `docs/ROADMAP_BEAT_SARA.md`).
