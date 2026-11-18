"""Records exactly which OS-level calls AppController makes for a set of scenarios.

Used by tests/test_phase4_platform.py to pin the Windows behaviour: the call log
(keystrokes, sleeps, shell commands, subprocesses, browser opens, spoken reply)
for every scenario below was captured from the ORIGINAL pre-Phase-4 AppController
and is compared, call for call, with the refactored facade + WindowsBackend.
"""
import contextlib
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from unittest import mock

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None, typewrite=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None)
S.install_fake_module("pywhatkit")

import psutil  # noqa: E402


class FakeProc:
    def __init__(self, pid, name):
        self.info = {"pid": pid, "name": name}


class Recorder:
    def __init__(self):
        self.log = []

    def add(self, *entry):
        self.log.append(entry)


@contextlib.contextmanager
def instrumented(rec: Recorder, which_map=None, procs=None, press_raises=False):
    import pyautogui
    which_map = which_map or {}
    real_strftime = time.strftime

    def strftime(fmt, *a):
        return "20260101_000000" if fmt == "%Y%m%d_%H%M%S" else real_strftime(fmt, *a)

    def press(*a, **k):
        if press_raises:
            raise RuntimeError("keyboard unavailable")
        rec.add("pyautogui.press", a, k)

    patches = [
        mock.patch.object(pyautogui, "press", press, create=True),
        mock.patch.object(pyautogui, "write", lambda *a, **k: rec.add("pyautogui.write", a, k), create=True),
        mock.patch.object(pyautogui, "hotkey", lambda *a, **k: rec.add("pyautogui.hotkey", a, k), create=True),
        mock.patch.object(pyautogui, "screenshot", lambda *a, **k: rec.add("pyautogui.screenshot", a, k), create=True),
        mock.patch.object(os, "system", lambda c: rec.add("os.system", c) or 0),
        mock.patch.object(subprocess, "Popen", lambda *a, **k: rec.add("Popen", a) or mock.MagicMock()),
        mock.patch.object(webbrowser, "open", lambda *a, **k: rec.add("webbrowser.open", a) or True),
        mock.patch.object(time, "sleep", lambda s: rec.add("sleep", s)),
        mock.patch.object(time, "strftime", strftime),
        mock.patch.object(shutil, "which", lambda n, *a, **k: which_map.get(n)),
        mock.patch.object(psutil, "process_iter", lambda attrs=None: list(procs or [])),
        mock.patch.object(psutil, "Process",
                          lambda pid: mock.MagicMock(terminate=lambda: rec.add("terminate", pid))),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def run_all(controller, patch_focus, data_dir, whatsapp_flows=True):
    """controller: the AppController class under test.
    patch_focus(seq): context manager making the WhatsApp focus check return values from `seq`.
    data_dir: the folder that class will treat as its project root (for the screenshot scenarios)."""
    import vision.screen_capture as sc
    import vision.ocr_engine as oe
    results = {}

    def scenario(name, fn, **inst):
        rec = Recorder()
        with instrumented(rec, **inst):
            try:
                out = fn()
            except Exception as e:                               # a scenario must never abort the run
                out = f"RAISED {type(e).__name__}: {e}"
        results[name] = {"returns": out, "calls": rec.log}

    c = controller
    scenario("open_app website", lambda: c.open_app("  YouTube "))
    scenario("open_app win-key", lambda: c.open_app("Notepad++"))
    scenario("open_app key press raises", lambda: c.open_app("calc"), press_raises=True)
    scenario("open_website known+query+browser found", lambda: c.open_website("youtube", "lofi beats", "brave"),
             which_map={"brave.exe": "C:/fake/brave.exe"})
    scenario("open_website browser missing", lambda: c.open_website("youtube", None, "brave"))
    scenario("open_website raw url", lambda: c.open_website("https://example.org/x"))
    scenario("open_website unknown site+query", lambda: c.open_website("my site", "cats"))
    scenario("open_website unknown site", lambda: c.open_website("My Site"))
    for word in ("turn on wifi", "bluetooth off", "hotspot please", "do a barrel roll"):
        scenario(f"toggle_hardware {word}", lambda w=word: c.toggle_hardware(w))
    procs = [FakeProc(1, "Chrome.exe"), FakeProc(2, "code.exe"), FakeProc(3, "codecov.exe"), FakeProc(4, "")]
    scenario("close_app exact", lambda: c.close_app("chrome"), procs=procs)
    scenario("close_app substring picks first", lambda: c.close_app("cod"), procs=procs)
    scenario("close_app not running", lambda: c.close_app("ghost"), procs=procs)
    scenario("close window", lambda: c.close_current_window())
    scenario("close tab", lambda: c.close_current_tab())
    scenario("write_in_notepad", lambda: c.write_in_notepad("hello world"))
    scenario("system setting display", lambda: c.open_system_setting("Display"))
    scenario("system setting unknown", lambda: c.open_system_setting("bios"))

    if whatsapp_flows:
        def wa(seq, shot, text, name):
            def go():
                with patch_focus(seq), \
                        mock.patch.object(sc.VisionCapture, "take_screenshot", staticmethod(lambda: shot)), \
                        mock.patch.object(oe.ocr, "extract_text", lambda p: text):
                    return c.send_whatsapp_message("Riya", "hello there")
            scenario(name, go)
        wa([True], "shot.png", "chat with Riya open", "whatsapp confirmed")
        wa([True], "shot.png", "some other chat", "whatsapp wrong chat")
        wa([True], "", "", "whatsapp cannot verify")
        wa([False] * 20, "", "", "whatsapp not installed -> web fallback")
        wa([False, False, True], "shot.png", "Riya", "whatsapp launches then focuses")

    # screenshots (real files, real _enforce_retention) in an isolated folder
    shots = os.path.join(data_dir, "screenshots")
    os.makedirs(shots, exist_ok=True)
    for old in os.listdir(shots):
        os.remove(os.path.join(shots, old))
    scenario("delete_latest with none", lambda: c.delete_latest_screenshot())
    scenario("take_screenshot", lambda: c.take_screenshot())
    for n in ("Wave_20260101_000001.png", "Wave_20260101_000002.png", "wave_internal_x.png"):
        with open(os.path.join(shots, n), "wb") as fh:
            fh.write(b"png")
    scenario("delete_latest picks newest user shot", lambda: c.delete_latest_screenshot())
    results["screenshots left"] = sorted(os.listdir(shots))
    return results


def normalise(results, roots):
    """Make logs comparable across checkouts: swap project roots for <ROOT>, drop the new undo hint."""
    import json
    text = json.dumps(results, default=str, sort_keys=True)
    for r in roots:
        text = text.replace(json.dumps(r)[1:-1], "<ROOT>")
    text = text.replace(' (Say \\"undo\\" to bring it back.)', "")
    return json.loads(text)
