"""
skills/self_diagnostics.py — "run a self check" / "is everything working?".

Checks the four things most likely to silently break Wave without any
single error message pointing at the real cause - the LLM provider, the
database file, free disk space, and the voice stack - and reports which
ones are actually fine and which ones need attention, in one pass.

Builds its own LLM client via brain/llm_provider.py directly (resolve_provider
-> build_client -> check_connection) rather than depending on a live
WaveBrain instance, the same independence reminders/composer.py already
has from WaveBrain for its own LLM reword step - a diagnostic that needed a
healthy WaveBrain already running to check whether the LLM is reachable
would be circular reasoning for the one case (e.g. a brain boot had partly
failed) where you'd most want this to work anyway.
"""
import logging
import os
import shutil

from database.db_manager import db
from settings.config import config

logger = logging.getLogger("SelfDiagnostics")


def _check_llm_provider() -> str:
    try:
        from brain import llm_provider
        cfg = llm_provider.resolve_provider()
        client = llm_provider.build_client(cfg)
        ok, message, latency_ms = llm_provider.check_connection(client, cfg)
        if ok:
            return f"OK - {cfg.name} ({cfg.model}), {latency_ms}ms"
        return f"PROBLEM - {cfg.name}: {message}"
    except Exception as e:
        logger.debug(f"Self-diagnostics: LLM provider check failed: {e}")
        return f"PROBLEM - couldn't even start a provider check ({e})"


def _check_database() -> str:
    try:
        db.list_workflows()  # cheap, exercises a real connection + query
        return f"OK - reachable at {config.DB_PATH}"
    except Exception as e:
        return f"PROBLEM - {e}"


def _check_disk() -> str:
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        free_gb = usage.free / (1024 ** 3)
        if free_gb < 1:
            return f"LOW - only {free_gb:.1f} GB free on the drive holding {config.DATA_DIR}"
        return f"OK - {free_gb:.1f} GB free"
    except Exception as e:
        return f"PROBLEM - {e}"


def _check_voice_stack() -> str:
    parts = []
    try:
        from voice import tts
        parts.append("TTS: Piper (local)" if getattr(tts, "HAS_PIPER", False) else "TTS: system voice only (Piper not installed)")
    except Exception as e:
        parts.append(f"TTS: couldn't check ({e})")
    try:
        from voice import stt
        parts.append("STT: faster-whisper" if getattr(stt, "HAS_FASTER_WHISPER", False) else "STT: not installed")
    except Exception as e:
        parts.append(f"STT: couldn't check ({e})")
    return "; ".join(parts)


def run_self_diagnostics() -> str:
    checks = [
        ("LLM provider", _check_llm_provider()),
        ("Database", _check_database()),
        ("Disk space", _check_disk()),
        ("Voice stack", _check_voice_stack()),
    ]
    problems = [name for name, result in checks if result.startswith(("PROBLEM", "LOW"))]
    lines = [f"{name}: {result}" for name, result in checks]
    header = "All systems checked out fine." if not problems else f"Found an issue with: {', '.join(problems)}."
    report = header + " " + " | ".join(lines)
    # Tier 20: an LLM provider's own error text ("invalid API key: sk-...")
    # is the likeliest leak path here, and this report may be spoken or
    # shown on-screen verbatim - scrub it before returning.
    try:
        from brain import redaction
        report = redaction.redact(report)
    except Exception:
        pass
    return report
