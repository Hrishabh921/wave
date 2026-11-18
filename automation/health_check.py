"""
automation/health_check.py — Tier 19: startup health check.

docs/SARA_GAP_ANALYSIS.md named this gap precisely: "Nothing checks for a
missing mic, models or LLM at startup and reports it loudly (SARA
health_check.py). Wave's run_self_diagnostics runs only when asked." This
module is the loud, at-startup version - five checks (mic, speaker, local
voice models, LLM provider, Tesseract OCR), each OK/WARN/PROBLEM with a
one-line reason, run from two places:

* ui/main_window.py, on a background thread right after startup (never the
  GUI thread - a few of these open real audio devices / make a network
  call) - each non-OK result becomes a toast, so a broken mic or an
  unreachable LLM is something the user SEES, not something buried in a
  log file they'd only open after noticing voice doesn't work.
* wave_start.pyw's `--healthcheck` CLI mode - no QApplication, no window,
  just these checks run synchronously and printed as a plain-text report,
  exiting 0 (all OK/WARN) or 1 (any PROBLEM) - SARA has nothing like this
  at all (see the roadmap's Tier 19 section).

The LLM and voice-stack (Piper/faster-whisper) checks are NOT reimplemented
here - they're skills/self_diagnostics.py's own _check_llm_provider() and
_check_voice_stack(), imported directly. Both already build their own
independent clients rather than depending on a live WaveBrain/MainWindow
(see that module's own docstring for why - the same reasoning applies here,
doubly so: this runs BEFORE MainWindow exists at all in the --healthcheck
path), so reusing them costs nothing and keeps "is the LLM reachable" one
single answer instead of two that could quietly drift apart.

Mic/speaker use the same sounddevice calls voice/capture.py and voice/tts.py
already make for their own purposes (sd.query_devices(kind=...)) - no new
dependency, same "missing optional package -> WARN, not a crash" posture
as everywhere else in this codebase.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import List

logger = logging.getLogger("HealthCheck")


class Status(Enum):
    OK = "OK"
    WARN = "WARN"
    PROBLEM = "PROBLEM"


@dataclass
class CheckResult:
    name: str
    status: Status
    message: str


def _check_microphone() -> CheckResult:
    try:
        import sounddevice as sd
    except Exception as e:
        return CheckResult("Microphone", Status.WARN, f"sounddevice not installed ({e}) - voice input unavailable.")
    try:
        info = sd.query_devices(kind="input")
        name = (info or {}).get("name") or "default input device"
        return CheckResult("Microphone", Status.OK, f"OK - {name}")
    except Exception as e:
        return CheckResult("Microphone", Status.PROBLEM, f"No working microphone found ({e}) - voice input won't work.")


def _check_speaker() -> CheckResult:
    try:
        import sounddevice as sd
    except Exception as e:
        return CheckResult("Speaker", Status.WARN, f"sounddevice not installed ({e}) - local audio playback may be limited.")
    try:
        info = sd.query_devices(kind="output")
        name = (info or {}).get("name") or "default output device"
        return CheckResult("Speaker", Status.OK, f"OK - {name}")
    except Exception as e:
        return CheckResult("Speaker", Status.PROBLEM, f"No working audio output found ({e}) - you won't hear replies.")


def _check_voice_models() -> CheckResult:
    # Reuses skills/self_diagnostics.py's own voice-stack string so there's
    # one description of "what's installed", not two - just adds the
    # OK/WARN verdict that module doesn't need for its own (chat-facing)
    # purposes.
    from skills.self_diagnostics import _check_voice_stack
    try:
        from voice import tts, stt
        has_piper = bool(getattr(tts, "HAS_PIPER", False))
        has_whisper = bool(getattr(stt, "HAS_FASTER_WHISPER", False))
        status = Status.OK if (has_piper and has_whisper) else Status.WARN
    except Exception:
        status = Status.WARN
    return CheckResult("Voice models", status, _check_voice_stack())


def _check_llm() -> CheckResult:
    from skills.self_diagnostics import _check_llm_provider
    message = _check_llm_provider()
    status = Status.OK if message.startswith("OK") else Status.PROBLEM
    return CheckResult("LLM provider", status, message)


def _check_tesseract() -> CheckResult:
    try:
        from vision.ocr_engine import ocr
        if ocr.tesseract_path:
            return CheckResult("Tesseract OCR", Status.OK, f"OK - {ocr.tesseract_path}")
        return CheckResult("Tesseract OCR", Status.WARN,
                            "Not found - reading text from screenshots/images won't work until it's installed.")
    except Exception as e:
        return CheckResult("Tesseract OCR", Status.WARN, f"Couldn't check ({e})")


# Order matches the roadmap's own listing: "Mic, speaker, models, LLM, Tesseract".
_CHECKS = (_check_microphone, _check_speaker, _check_voice_models, _check_llm, _check_tesseract)


def run_health_check() -> List[CheckResult]:
    """Never raises - a check that blows up becomes a PROBLEM result
    naming itself, same as any other failure it might have reported."""
    results = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as e:
            logger.exception(f"Health check {check.__name__} raised")
            results.append(CheckResult(check.__name__, Status.PROBLEM, f"Check itself failed: {e}"))
    return results


def format_report(results: List[CheckResult]) -> str:
    """Plain-text report for --healthcheck's stdout. One line per check,
    then a one-line summary - script-friendly (grep PROBLEM) without being
    unreadable for a person running it directly."""
    lines = [f"[{r.status.value:7}] {r.name}: {r.message}" for r in results]
    problems = [r.name for r in results if r.status is Status.PROBLEM]
    warnings = [r.name for r in results if r.status is Status.WARN]
    if problems:
        summary = f"PROBLEM with: {', '.join(problems)}."
    elif warnings:
        summary = f"All critical systems OK. Worth a look: {', '.join(warnings)}."
    else:
        summary = "All systems OK."
    report = "\n".join(lines) + "\n\n" + summary
    # Tier 20: a check's own message (a provider error, a stack trace) is
    # the likeliest leak path for a secret into this report - scrub it
    # before it hits stdout, same as logging/telemetry.
    try:
        from brain import redaction
        report = redaction.redact(report)
    except Exception:
        pass
    return report


def all_critical_ok(results: List[CheckResult]) -> bool:
    """The --healthcheck CLI's exit-code question. WARN never fails the
    run - a missing optional local voice model or Tesseract install is a
    real gap to mention, same as self_diagnostics.py already treats it,
    but not a reason for a script to treat Wave as broken."""
    return not any(r.status is Status.PROBLEM for r in results)
