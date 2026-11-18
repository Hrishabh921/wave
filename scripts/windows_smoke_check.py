"""Windows-only smoke checks for Wave (see WINDOWS_SMOKE_TEST.md).

Run from the extracted project folder in the same Python environment used by Wave:
    py scripts\\windows_smoke_check.py
    py scripts\\windows_smoke_check.py --mic     (also opens the microphone for 3 s)
By default this script enumerates audio devices but does not record audio or play sound.
With --mic it opens Wave's continuous capture stream (Tier 12) for a few seconds and
reports what it got - the audio stays in memory and is discarded.
It creates and closes the real Qt MainWindow without starting the app's event loop.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import sys
import time
import traceback


def mic_check(seconds: float = 3.0) -> list:
    """Tier 12: open the continuous mic stream briefly and report on it."""
    problems = []
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from voice.aec import aec_processor
        from voice.capture import CaptureEngine
        from voice.endpoint import EndpointConfig
        levels = []
        engine = CaptureEngine(EndpointConfig(), aec=aec_processor)
        engine.set_level_callback(levels.append)
        if not engine.start():
            problems.append(f"Continuous mic stream would not open: {engine.last_error or 'no sounddevice'}")
            return problems
        print(f"Mic stream: listening for {seconds:.0f} s (nothing is saved)...")
        try:
            time.sleep(seconds)
        finally:
            engine.stop()
        rate = engine.capture_rate
        print(f"Mic stream: opened at {rate} Hz{' (resampled to 16 kHz)' if rate != 16000 else ''}; "
              f"{len(levels)} level updates; {engine.frames_dropped} frames dropped; "
              f"noise floor {engine.noise_floor_dbfs:.1f} dBFS; "
              f"echo cancellation {'ON' if getattr(aec_processor, 'enabled', False) else 'off'}")
        if not levels:
            problems.append("The mic stream opened but delivered no audio.")
    except Exception as exc:
        problems.append(f"Mic stream check failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Wave Windows smoke checks")
    ap.add_argument("--mic", action="store_true", help="also open the microphone stream for 3 seconds")
    args = ap.parse_args(argv)
    print("Wave Windows smoke check")
    print(f"OS: {platform.platform()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    if os.name != "nt":
        print("BLOCKED: This check must be run on Windows for real Windows APIs/devices.")
        return 2

    failures = []
    for module in ("PyQt6", "sounddevice", "pygame"):
        available = importlib.util.find_spec(module) is not None
        print(f"Dependency {module}: {'OK' if available else 'MISSING'}")
        if module == "PyQt6" and not available:
            failures.append("PyQt6 is missing; install the project requirements first.")

    # Actual host audio-device enumeration (no recording/playback).
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [d for d in devices if int(d.get("max_input_channels", 0)) > 0]
        outputs = [d for d in devices if int(d.get("max_output_channels", 0)) > 0]
        print(f"Audio devices: {len(devices)} total; {len(inputs)} input; {len(outputs)} output")
        for kind, rows in (("INPUT", inputs), ("OUTPUT", outputs)):
            for device in rows:
                print(f"  {kind}: {device.get('name')} (index={device.get('index')})")
        if not inputs:
            print("WARNING: No microphone/input device reported by sounddevice.")
        if not outputs:
            print("WARNING: No speaker/output device reported by sounddevice.")
    except Exception as exc:
        failures.append(f"Audio enumeration failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # Real Qt widget construction with the offscreen platform, not a fake UI.
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        from ui.main_window import MainWindow
        app = QApplication.instance() or QApplication(["wave-smoke-check"])
        window = MainWindow()
        window.show()
        app.processEvents()
        print(f"Qt MainWindow construction: OK ({type(window).__name__})")
        window.close()
        app.processEvents()
        del window
    except Exception as exc:
        failures.append(f"Qt UI smoke test failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # pygame mixer check is non-invasive: initialize/query/quit only.
    try:
        import pygame
        pygame.mixer.init()
        mixer_state = pygame.mixer.get_init()
        print(f"pygame mixer initialization: {'OK' if mixer_state else 'NO DEVICE'} ({mixer_state})")
        pygame.mixer.quit()
        if not mixer_state:
            failures.append("pygame mixer initialized without an audio format/device.")
    except Exception as exc:
        failures.append(f"pygame mixer check failed: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # Tier 11: the on-device wake word (optional - only reported, never a
    # failure: Wave falls back to transcript-based wake words without it).
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model = os.path.join(root, "voice_models", "wakewords", "hey_wave.onnx")
        have_oww = importlib.util.find_spec("openwakeword") is not None
        have_ort = importlib.util.find_spec("onnxruntime") is not None
        print(f"Wake word: model {'present' if os.path.isfile(model) else 'MISSING'}; "
              f"openwakeword {'OK' if have_oww else 'not installed'}; onnxruntime {'OK' if have_ort else 'not installed'}")
        if os.path.isfile(model) and have_ort:
            import numpy as np
            import onnxruntime as ort
            sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
            out = sess.run(None, {sess.get_inputs()[0].name: np.zeros((1, 16, 96), np.float32)})[0]
            print(f"  hey_wave.onnx loads and runs on this onnxruntime ({ort.__version__}): score on silence {float(out[0][0]):.3f}")
        if not (have_oww and have_ort):
            print("  (optional) pip install -r requirements-voice-local.txt for the on-device 'Hey Wave' model")
    except Exception as exc:
        print(f"Wake word check skipped: {type(exc).__name__}: {exc}")

    if args.mic:
        failures.extend(mic_check())

    if failures:
        print("\nRESULT: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("\nRESULT: PASS (enumeration/UI initialization only; still manually test mic capture and audible playback in Wave.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
