"""
voice/clone_worker.py — Tier 31: XTTS-v2 in a process of its own.

Wave runs the cloned voice here, never in its own process. Measured on the
user's laptop: with one busy pure-Python thread in the same process, a
2-second sentence took more than 4 minutes instead of ~4 seconds. XTTS's token
loop gives up and re-takes Python's GIL around a thousand times per token,
and every time it queued behind whatever other thread held it; the other way
round it starved Wave's audio threads ("Mic stream reopened"). In the real
app one sentence took 568 s. Here XTTS has an interpreter and a GIL to itself.

Started by voice/clone_client.py:
    python -m voice.clone_worker --model NAME --reference PATH [--device auto|cpu|cuda]

Protocol on stdin/stdout - one JSON line per message; a reply with "n" > 0 is
followed by exactly 4*n bytes of little-endian float32 samples:
    out (first): {"ready": true, "device": "cpu", "rate": 24000, "load_s": 13.2, "latents_s": 1.3}
                 or {"ready": false, "error": "..."}
    in:  {"cmd": "say", "id": 3, "text": "...", "lang": "en"}
    out: {"id": 3, "ok": true, "rate": 24000, "n": 52000, "s": 7.4} + samples
         {"id": 3, "ok": false, "error": "..."}
It exits when stdin closes - Wave quitting, or crashing. Everything it (or a
library) prints goes to stderr, which Wave points at wave_voice_clone.log in
its data folder.
"""
import argparse
import json
import logging
import os
import sys
import time


def _claim_stdout():
    """The protocol owns the real stdout; anything a library prints goes to
    stderr instead (Wave's wave_voice_clone.log), so it can't corrupt the
    replies."""
    fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    return os.fdopen(fd, "wb")


def _claim_stdin():
    """The protocol owns the real stdin too. A library that asks a question
    with input() - coqui-tts asks for XTTS-v2's licence that way - would
    otherwise wait forever for an answer on the request pipe, where Wave
    sends nothing until this process says it's ready: the cloned voice
    then never comes, and nothing says why. With stdin on NUL that
    question gets EOFError straight away, which is reported to Wave."""
    fd = os.dup(sys.stdin.fileno())
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, sys.stdin.fileno())
    os.close(null)
    sys.stdin = open(os.devnull, "r", encoding="utf-8")
    return os.fdopen(fd, "rb")


def _send(out, header: dict, payload: bytes = b"") -> None:
    out.write((json.dumps(header) + "\n").encode("ascii"))
    if payload:
        out.write(payload)
    out.flush()


def main(argv=None, stdin=None, stdout=None, load=None) -> int:
    parser = argparse.ArgumentParser(description="Wave's voice-clone process (XTTS-v2)")
    parser.add_argument("--model", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args(argv)
    child = stdout is None
    out = _claim_stdout() if child else stdout
    inp = stdin if stdin is not None else (_claim_stdin() if child else sys.stdin.buffer)
    if child:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        # A windowless child is exactly what Windows' power throttling goes
        # after (monitoring/power.py) - opt this process out too.
        from monitoring.power import keep_full_speed  # noqa: PLC0415
        keep_full_speed()

    import numpy as np  # noqa: PLC0415
    from voice import clone_xtts  # noqa: PLC0415

    started = time.monotonic()
    try:
        voice = (load or clone_xtts.load_xtts)(args.model, args.device)
        loaded = time.monotonic()
        voice.set_reference(args.reference)
    except Exception as e:
        _send(out, {"ready": False, "error": f"{type(e).__name__}: {e}"})
        return 1
    _send(out, {"ready": True, "device": voice.device, "rate": voice.sample_rate,
                "load_s": round(loaded - started, 1), "latents_s": round(time.monotonic() - loaded, 1)})

    for raw in inp:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(msg, dict) or msg.get("cmd") != "say":
            continue
        rid = msg.get("id")
        began = time.monotonic()
        try:
            samples = np.asarray(voice.synthesize(str(msg.get("text") or ""), str(msg.get("lang") or "en")),
                                 dtype="<f4").ravel()
        except Exception as e:
            _send(out, {"id": rid, "ok": False, "error": f"{type(e).__name__}: {e}"})
            continue
        _send(out, {"id": rid, "ok": True, "rate": voice.sample_rate, "n": int(samples.size),
                    "s": round(time.monotonic() - began, 2)}, samples.tobytes())
    return 0


if __name__ == "__main__":
    sys.exit(main())
