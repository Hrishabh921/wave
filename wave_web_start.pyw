"""Tier 24: launches Wave's new pywebview + HTML/CSS/JS UI.

This is a SIBLING to wave_start.pyw (the original PyQt6 UI), not a
replacement of it - see webui/__init__.py's module docstring for why both
exist. Run this instead of wave_start.pyw to get the new UI; everything
else (brain, voice, automation, database, settings file) is shared, so
switching between them loses nothing.
"""
import os
import sys
from settings.config import config

# Same reasoning as wave_start.pyw: run from the writable data folder, not
# wherever the launcher's current directory happened to be.
os.chdir(config.DATA_DIR)


def main():
    if config.POWER_THROTTLING_OPT_OUT:
        from monitoring.power import keep_full_speed
        keep_full_speed()
    from webui.app import run
    run()


if __name__ == "__main__":
    main()
    