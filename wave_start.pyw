import argparse
import os
import sys
import logging
from settings.config import config

# Run from the writable data folder: the project folder when run from source, the folder next to
# the .exe (or %LOCALAPPDATA%\\WaveAI) in a PyInstaller build - never the temporary unpack dir.
os.chdir(config.DATA_DIR)


def _parse_args():
    # parse_known_args(), not parse_args(): before this flag existed,
    # nothing in wave_start.pyw read sys.argv except handing the whole
    # thing to QApplication(sys.argv), which quietly ignores whatever it
    # doesn't recognize. An unrelated argument some launcher happens to
    # pass along (a file association, a desktop-shortcut quirk) should
    # keep working exactly as it did before - only --healthcheck itself is
    # new, everything else stays Qt's problem, not an error here.
    parser = argparse.ArgumentParser(prog="wave_start", add_help=False)
    parser.add_argument("--healthcheck", action="store_true",
                         help="Run Wave's startup health check (mic, speaker, voice models, "
                              "LLM provider, Tesseract OCR) and exit - no GUI opens. "
                              "Exit code 0 if everything critical is OK, 1 otherwise.")
    args, _unknown = parser.parse_known_args()
    return args


def _run_healthcheck() -> int:
    # Tier 19: no PyQt6/MainWindow import anywhere on this path - a health
    # check needs to work even when the very thing broken is the GUI
    # stack, and it has no window to show anyway.
    from automation.health_check import run_health_check, format_report, all_critical_ok
    from brain.ollama_lifecycle import ollama_lifecycle
    ollama_lifecycle.ensure_ready(wait=True)  # inline is fine here - nothing else is waiting on this process
    results = run_health_check()
    print(format_report(results))
    return 0 if all_critical_ok(results) else 1


def main():
    logging.info("Initializing Wave AI GUI in Background...")
    from PyQt6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    window = MainWindow()
    # Tier 25: once an avatar rig has been built (Settings -> Avatar) and
    # the "show dashboard on startup" toggle is left off (the default),
    # the dashboard stays fully hidden - the avatar window (already shown
    # by MainWindow._init_avatar(), inside the constructor above) is the
    # only thing visible. Anyone who hasn't set up a photo yet sees
    # exactly the old behaviour: the dashboard starts minimized.
    if window.should_start_hidden():
        window.hide()
    else:
        window.showMinimized()

    sys.exit(app.exec())


if __name__ == "__main__":
    args = _parse_args()
    if args.healthcheck:
        sys.exit(_run_healthcheck())
    main()
