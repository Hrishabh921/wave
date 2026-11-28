"""Shared helpers for the Phase 4 tests. Stdlib only (unittest) - no pytest or
extra installs needed:   python -m unittest discover -s tests -p "test_phase4_*.py"
(pytest will also collect them if you have it)."""
import atexit
import os
import socket
import sys
import tempfile
import threading
import traceback
import shutil
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Tier 11: the time-to-first-audio log (telemetry/ttfa.py) must never be
# written into the repo's own database/ folder by a test run.
_TEST_DATA = tempfile.mkdtemp(prefix="wave_tests_")
os.environ.setdefault("TTFA_LOG_PATH", os.path.join(_TEST_DATA, "ttfa.jsonl"))
atexit.register(shutil.rmtree, _TEST_DATA, ignore_errors=True)

# Tests never open the real microphone. With the default capture engine,
# SpeechReceiver.listen() started the continuous stream (voice/capture.py) on
# the developer's real mic, transcribed whatever the room said - 25 streams
# in six STT test files, one transcript "Kareed lawai" - and its tests,
# written against the mocked recognizer, failed or passed by what was
# heard. The legacy path uses the recognizer the tests mock; tests of the
# stream itself build CaptureEngine on a fake sounddevice and set
# CAPTURE_ENGINE themselves.
os.environ.setdefault("CAPTURE_ENGINE", "legacy")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.CAPTURE_ENGINE = "legacy"

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
# Tier 42: and never keep a fake voice process's audio in a real voice_cache.
os.environ.setdefault("TTS_CLONE_PREWARM", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.TTS_CLONE_PREWARM = False
# Tier 43: and never ask Windows for the real app list (or launch from it).
os.environ.setdefault("APP_INDEX", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.APP_INDEX = False
# Tier 44: and never switch a real radio or the real theme.
os.environ.setdefault("SYSTEM_TOGGLES", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.SYSTEM_TOGGLES = False
# Tier 45: and never load the sentence model to second-guess a scripted
# conversation. tests/test_tier45_semantic_intents.py switches it on itself.
os.environ.setdefault("SEMANTIC_INTENTS", "false")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.SEMANTIC_INTENTS = False
# Tier 46: and never learn people from a scripted conversation, add the
# chat history to a test's prompt, or set aside a database's facts.
# tests/test_tier46_memory.py switches them on itself.
# Tier 47: and never search the real web or read the real foreground window.
# Tier 48: and never drop a scripted transcript as an echo of a test's speech.
# Tier 49: and never read what the developer's speakers are playing.
# And never have the real model reword a proactive nudge or a due reminder:
# both build their own client from the developer's .env, so every nudge
# test_phase6_proactive fired was a request to api.groq.com (see the guard
# below). test_tier18_proactive_triggers.py and test_tier3_reminders.py
# switch them on with a fake client.
# And never index the developer's real Desktop/Documents/Downloads: with notes
# search on, GroqBrain's background start reads and embeds every file there.
# The owner's .env happens to switch it off, which also made the notes tests
# (test_phase6_notes_rag, test_tier20_policy_matrix) fail on their PC - so it's
# off for every machine now, and those tests switch it on with a temp folder.
# Tier 55: and never load a real Whisper model in the background when a test
# starts a mic worker (FASTER_WHISPER_PRELOAD).
for _flag in ("MEMORY_PEOPLE_GRAPH", "MEMORY_EPISODIC", "MEMORY_DECAY", "WEB_GROUNDING", "CONTEXT_LINE",
              "ECHO_GUARD", "AEC_SYSTEM_REFERENCE", "PROACTIVE_LLM_REWORD_ENABLED", "CONTEXTUAL_REMINDER_LLM",
              "NOTES_RAG_ENABLED", "FASTER_WHISPER_PRELOAD"):
    os.environ.setdefault(_flag, "false")
    if "settings.config" in sys.modules:
        setattr(sys.modules["settings.config"].config, _flag, False)
# Tier 55: nor try the developer's GPU - their .env may say
# FASTER_WHISPER_DEVICE=auto. tests/test_tier55_stt_gpu.py sets it itself.
os.environ.setdefault("FASTER_WHISPER_DEVICE", "cpu")
if "settings.config" in sys.modules:
    sys.modules["settings.config"].config.FASTER_WHISPER_DEVICE = "cpu"


# Tests never touch the developer's real volume, screen brightness, keyboard
# or mouse. On the owner's laptop every full run turned the master volume up
# (test_phase8's think("volume up"), twice per run - it was at 100% after a
# day of runs) and pyautogui, which IS installed there, would have pressed
# real keys for any media/typing tool a test dispatched. Every volume path
# in Wave goes through pycaw's AudioUtilities.GetSpeakers().EndpointVolume
# and every brightness path through screen_brightness_control, so those two
# calls get an in-memory stand-in for the whole run, and pyautogui's input
# functions record instead of acting. A test that checks one of these
# itself still patches it as before.
class _FakeEndpointVolume:
    def __init__(self):
        self.level, self.muted = 0.5, 0

    def GetMasterVolumeLevelScalar(self):
        return self.level

    def SetMasterVolumeLevelScalar(self, value, _context=None):
        self.level = max(0.0, min(1.0, float(value)))

    def GetMute(self):
        return self.muted

    def SetMute(self, value, _context=None):
        self.muted = 1 if value else 0


class _FakeSpeakers:
    def __init__(self):
        self.EndpointVolume = _FakeEndpointVolume()


FAKE_SPEAKERS = _FakeSpeakers()
FAKE_BRIGHTNESS = [60]
PYAUTOGUI_CALLS = []

try:
    from pycaw.pycaw import AudioUtilities as _AudioUtilities
    _AudioUtilities.GetSpeakers = staticmethod(lambda: FAKE_SPEAKERS)
except Exception:          # no pycaw (not Windows): nothing real to protect
    pass
try:
    import screen_brightness_control as _sbc
    _sbc.get_brightness = lambda *a, **k: list(FAKE_BRIGHTNESS)
    _sbc.set_brightness = lambda value, *a, **k: FAKE_BRIGHTNESS.__setitem__(0, int(value))
except Exception:
    pass
try:
    import pyautogui as _pyautogui
    for _name in ("press", "hotkey", "typewrite", "write", "keyDown", "keyUp", "click", "doubleClick",
                  "tripleClick", "rightClick", "middleClick", "mouseDown", "mouseUp", "moveTo", "moveRel",
                  "move", "dragTo", "dragRel", "drag", "scroll", "hscroll", "vscroll"):
        if hasattr(_pyautogui, _name):
            setattr(_pyautogui, _name, (lambda n: lambda *a, **k: PYAUTOGUI_CALLS.append((n, a, k)))(_name))
except BaseException:      # not installed / no display: the tests' own fakes stand in
    pass


# Nor does a test run make the other real requests it used to (found by
# tracing every DNS lookup a full run made, and the test making it):
# - importing pywhatkit (installed on the owner's PC) fetched google.com:
#   its whats.py checks the connection at import time. It is imported here
#   once with that check answered locally, so install_fake_module() and
#   web/web_ops.py get the real module without the request;
# - a prefetch a test started could still be synthesizing after the test
#   put its fake edge_tts.Communicate back, and then reached Microsoft's
#   edge-tts service. Outside a test's own fake, Communicate now fails the
#   way it does on an offline PC, which every caller already falls back from;
# - database/embeddings.py's sentence model asked huggingface.co for a newer
#   copy on every load (22 requests). Offline it loads from the local cache;
#   the tests that need the real model skip when it isn't downloaded.
try:
    import requests as _requests
    _requests_get, _requests.get = _requests.get, (lambda *a, **k: None)
    try:
        import pywhatkit  # noqa: F401
    finally:
        _requests.get = _requests_get
except BaseException:      # not installed / no display: install_fake_module() stands in
    pass


class _OfflineCommunicate:
    def __init__(self, *args, **kwargs):
        raise ConnectionError("tests have no edge-tts service")


try:
    import edge_tts as _edge_tts
    _edge_tts.Communicate = _OfflineCommunicate
except Exception:
    pass
os.environ["HF_HUB_OFFLINE"] = "1"
if "huggingface_hub.constants" in sys.modules:
    sys.modules["huggingface_hub.constants"].HF_HUB_OFFLINE = True


# And nothing else can reach the network: every client looks a host up
# with socket.getaddrinfo first, so looking up anything but this machine
# now fails at once - nothing leaves it - and the test that tried fails,
# naming the host and the Wave code that called it. It began with the LLM:
# settings/config.py's load_dotenv() puts the developer's GROQ_API_KEY into
# every run, and the helpers that build their own client from it (the
# proactive nudge reword, the reminder composer, the health check) spent
# the owner's free-tier quota - 22 requests to api.groq.com per full run.
# Point an LLM test at tests/_fake_openai.py (BrainBase in
# test_phase4_brain.py); fake or mock anything else.
REAL_NETWORK_REQUESTS = []      # (host, thread name, Wave's part of the stack) per blocked lookup
_running_test = None
try:
    _OWN_HOST = socket.gethostname().lower()
except OSError:
    _OWN_HOST = "localhost"


def _network_host(host):
    """`host`, normalized, when looking it up would leave this machine; else None."""
    name = host.decode("ascii", "replace") if isinstance(host, (bytes, bytearray)) else str(host or "")
    name = name.strip().strip("[]").rstrip(".").lower()
    if name in ("", "localhost", "0.0.0.0", "::", "::1", _OWN_HOST) or name.startswith("127.") \
            or name.endswith(".localhost"):
        return None
    return name


def _caller_stack() -> str:
    """The lookup's stack without the library frames (httpx, the groq SDK,
    unittest, threading) - what is left is the test and the Wave code."""
    frames = traceback.extract_stack()[:-2]
    skip = tuple({os.path.normcase(sys.prefix), os.path.normcase(sys.base_prefix)}) + ("<",)
    here = os.path.normcase(os.path.abspath(__file__))
    own = [f for f in frames if not os.path.normcase(f.filename).startswith(skip)
           and "site-packages" not in f.filename and os.path.normcase(f.filename) != here]
    return "".join(traceback.format_list(own or frames))


def _offline_getaddrinfo(host, *args, **kwargs):
    name = _network_host(host)
    if name is None:
        return _real_getaddrinfo(host, *args, **kwargs)
    thread, stack = threading.current_thread().name, _caller_stack()
    REAL_NETWORK_REQUESTS.append((name, thread, stack))
    sys.stderr.write(f"\n*** BLOCKED a real network request to {name} ({_running_test or 'outside any test'}, "
                     f"thread {thread}) - tests use local fakes (tests/_fake_openai.py for an LLM).\n")
    if _running_test is None:      # no test to fail, so the stack goes to the log
        sys.stderr.write(stack)
    raise socket.gaierror(socket.EAI_NONAME, f"tests may not reach the network ({name})")


def _run_offline(self, result=None):
    global _running_test
    first = len(REAL_NETWORK_REQUESTS)

    def no_network_requests():
        made = REAL_NETWORK_REQUESTS[first:]
        if made:
            name, thread, stack = made[0]
            raise AssertionError(f"{len(made)} real network request(s) blocked during this test, the first "
                                 f"to {name} on thread {thread}:\n{stack}")

    self.addCleanup(no_network_requests)   # added first, so it runs last: after the test's own cleanups
    outer, _running_test = _running_test, self.id()
    try:
        return _real_testcase_run(self, result)
    finally:
        _running_test = outer


if not getattr(socket.getaddrinfo, "_offline", False):   # once per process, even if re-imported
    _real_getaddrinfo, _real_testcase_run = socket.getaddrinfo, unittest.TestCase.run
    _offline_getaddrinfo._offline = True
    socket.getaddrinfo = _offline_getaddrinfo
    unittest.TestCase.run = _run_offline


def silence_tts(testcase) -> None:
    """Every VoiceEngine reachable as some module's `tts_engine` stays silent
    for this test: speak_stream() reads its input the way the voice would and
    speak() does nothing. Some test files evict and re-import voice.tts, so a
    process can hold several engines - patching only voice.tts.tts_engine let
    the web UI's reply worker (still holding the first one) say "hello world"
    out loud on the owner's laptop."""
    from unittest import mock  # noqa: PLC0415
    engines = {}
    for name, module in list(sys.modules.items()):
        # Wave's own modules only, read straight from __dict__: some
        # libraries' module __getattr__ (torch._classes) invent an attribute
        # for any name and raise on hasattr().
        if not name.split(".")[0] in ("voice", "webui", "ui", "brain", "avatar", "remote"):
            continue
        engine = getattr(module, "__dict__", {}).get("tts_engine")
        if engine is not None and type(engine).__name__ == "VoiceEngine":
            engines[id(engine)] = engine

    def speak_stream(chunks):
        for _piece in chunks:
            pass
        return True

    for engine in engines.values():
        for name, fake in (("speak_stream", speak_stream), ("speak", lambda *a, **k: None)):
            patcher = mock.patch.object(engine, name, side_effect=fake)
            patcher.start()
            testcase.addCleanup(patcher.stop)


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
