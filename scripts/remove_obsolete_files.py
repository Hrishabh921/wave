"""
scripts/remove_obsolete_files.py — deletes files that older versions of
Wave shipped but the current version no longer has.

WHY THIS EXISTS
---------------
Unzipping a new Wave package ON TOP of an existing checkout adds and
replaces files, but it can never delete the ones the new version dropped.
That is exactly how the Tier 1-9 merge ended up with a red test suite: five
test files and five modules from an older, parallel line of work were left
behind, still collected by the test runner, testing code that no longer
existed (see docs/SARA_GAP_ANALYSIS.md section 1 and NEXT_STEPS.md).

tests/test_repo_hygiene.py fails, naming these files, if any of them is
still present - so this kind of leftover can't go unnoticed again.

USAGE (from the project folder)
    python scripts/remove_obsolete_files.py            # just list what's there
    python scripts/remove_obsolete_files.py --delete   # actually delete them

The safest way to upgrade is still to extract a new package into a FRESH
folder and copy your .env, database/ and voice models across - this
script is for when that wasn't done.
"""
import argparse
import os
import sys

# Paths are relative to the project root, always written with "/".
# Add to this list whenever a release removes a file.
OBSOLETE_PATHS = (
    # Removed in Tier 10 - leftovers of the Oct 18-26 parallel effort,
    # superseded by the Tier 1-9 implementations named next to each.
    "tests/test_taint_and_guard.py",     # -> tests/test_taint.py, test_security_tiers.py, test_phase7_confirm_gate.py
    "tests/test_tier5_additions.py",     # -> tests/test_timers_and_stopwatch.py, test_tier8_fun.py
    "tests/test_piper_auto_download.py", # -> Piper download returns in Tier 13 with its own tests
    "tests/test_stt_vad.py",             # -> tests/test_voice_buffers.py, test_phase5_stt_aec_wiring.py
    "automation/timer_manager.py",       # -> brain/handlers/timers.py
    "voice/stt_vad.py",                  # -> voice/buffers.py + SpeechReceiver._passes_vad()
    "skills/joke.py",                    # -> brain/handlers/fun.py
    "skills/streak.py",                  # -> automation/streak_manager.py
    "planning/test_doubles.py",          # -> tests/test_doubles.py
    "MERGE_VALIDATION_REPORT.md",        # described a merge that no longer applies
    # Removed in Tier 10 - found by tests/test_repo_hygiene.py's orphan
    # check: a hard-coded "JARVIS" stylesheet nothing has imported since
    # ui/theme.py's ThemeManager started generating the stylesheet itself.
    "ui/styles.py",
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def present(root: str = PROJECT_ROOT) -> list:
    """The OBSOLETE_PATHS entries that currently exist under `root`."""
    return [p for p in OBSOLETE_PATHS if os.path.exists(os.path.join(root, *p.split("/")))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="List or delete files older Wave versions left behind.")
    parser.add_argument("--delete", action="store_true", help="delete them (default: only list)")
    args = parser.parse_args(argv)

    found = present()
    if not found:
        print("Nothing to clean up - no obsolete files found.")
        return 0
    print(f"{len(found)} obsolete file(s) found:")
    for p in found:
        print(f"  {p}")
    if not args.delete:
        print("\nRun again with --delete to remove them.")
        return 1
    for p in found:
        os.remove(os.path.join(PROJECT_ROOT, *p.split("/")))
        print(f"deleted {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
