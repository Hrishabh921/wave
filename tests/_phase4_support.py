"""Shared helpers for the Phase 4 tests. Stdlib only (unittest) - no pytest or
extra installs needed:   python -m unittest discover -s tests -p "test_phase4_*.py"
(pytest will also collect them if you have it)."""
import atexit
import os
import sys
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Tier 11: the time-to-first-audio log (telemetry/ttfa.py) must never be
# written into the repo's own database/ folder by a test run.
_TEST_DATA = tempfile.mkdtemp(prefix="wave_tests_")
os.environ.setdefault("TTFA_LOG_PATH", os.path.join(_TEST_DATA, "ttfa.jsonl"))
atexit.register(shutil.rmtree, _TEST_DATA, ignore_errors=True)

# Tier 32: the speaker check must never judge test audio against the
# developer's own voiceprint (avatar_rig/voiceprint.npz, a real person's
# voice training). tests/test_tier32_speaker_id.py turns it back on with its
# own temporary voiceprint.
os.environ.setdefault("SPEAKER_VERIFY", "off")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.SPEAKER_VERIFY = "off"
# Tier 37: nor read the developer's remembered names into Whisper's prompt.
# tests/test_tier37_vocabulary.py turns it back on with its own facts.
os.environ.setdefault("STT_VOCABULARY", "off")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.STT_VOCABULARY = "off"
# Tier 38: nor summarise a conversation with a real model call in the
# background. tests/test_tier38_rolling_summary.py turns it on with its own.
os.environ.setdefault("LLM_ROLLING_SUMMARY", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.LLM_ROLLING_SUMMARY = False
# Tier 39: and never turn the developer's real apps down (a mic worker going
# AWAKE in a test would). tests/test_tier39_ducking.py uses a fake mixer.
os.environ.setdefault("AUDIO_DUCKING", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.AUDIO_DUCKING = False
# Tier 40: and never store a fake model's answer in a real database's
# response cache (or serve one). tests/test_tier40_response_cache.py uses its own.
os.environ.setdefault("LLM_RESPONSE_CACHE", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.LLM_RESPONSE_CACHE = False
# Tier 41: and never teach a developer's style profile from test messages.
os.environ.setdefault("LLM_STYLE_PROFILE", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.LLM_STYLE_PROFILE = False


def make_tmpdir(testcase) -> str:
    d = tempfile.mkdtemp(prefix="wave_p4_")
    testcase.addCleanup(shutil.rmtree, d, ignore_errors=True)
    return d


def install_fake_module(name, **attrs):
    """Insert a stand-in module (for packages that aren't installed in the test
    environment, e.g. pyautogui on a headless box) unless the real one imports.

    Tier 23: catches BaseException, not just Exception - the exact same fix
    bench/redteam_runner.py's _install_headless_stub_if_needed() needed, and
    for the exact same reason. pyautogui's mouseinfo sub-dependency raises a
    plain KeyError('DISPLAY') when DISPLAY is unset (an ordinary Exception,
    always caught here before this fix) - but when this whole suite runs
    under xvfb-run (DISPLAY *is* set) on a sandbox with no tkinter compiled
    in, mouseinfo instead calls sys.exit(...), which is SystemExit, not an
    Exception subclass. Depending on which test file happens to be the
    first in the whole discovered run to call this function for "pyautogui"
    - which shifts every time a new test_tier*.py file is added, as adding
    tests/test_tier23_remote_dashboard.py itself demonstrated by turning a
    previously-clean `python -m unittest discover -s tests` run under
    xvfb-run into an uncaught SystemExit that aborted the whole suite -
    that first caller used to get no fake installed and the crash instead.
    Once this function installs this fake, it's cached in sys.modules for
    the rest of the process, so only the very first call per process name
    was ever actually at risk; a plain `except Exception` is also still
    enough for the ordinary, display-less case every non-xvfb test run
    exercises - this widening only matters under xvfb-run, but it's not
    conditional on that: it costs nothing either way."""
    import types
    try:
        __import__(name)
        return sys.modules[name]
    except BaseException:
        sys.modules.pop(name, None)
        sys.modules.pop("mouseinfo", None)  # pyautogui's own failed sub-import may have half-registered it
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod
