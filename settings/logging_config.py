"""
settings/logging_config.py — Tier 2: centralized logging configuration

Before this file, settings/config.py ended with a single inline
`logging.basicConfig(...)` call: a plain FileHandler writing straight to
wave_system.log, forever, with nothing to stop it growing without bound
for as long as Wave keeps running (and it's meant to - this is a
long-lived voice assistant, not a script that exits). A machine left
running for weeks could accumulate a many-hundred-MB log for no benefit
over the last few days of it.

This module is the one place that decides how Wave logs, and is the only
thing settings/config.py now calls for it:

    from settings import logging_config
    logging_config.setup()

`setup()` is idempotent - safe to call more than once (tests do, and a
module reload shouldn't double up handlers) - and replaces whatever
handlers were already on the root logger with freshly-built ones, rather
than appending to them.

What changed from the old basicConfig call
--------------------------------------------
* The file handler now ROTATES (stdlib RotatingFileHandler) instead of
  growing forever - once wave_system.log passes LOG_MAX_BYTES it's
  rolled to wave_system.log.1 (then .2, ...), keeping at most
  LOG_BACKUP_COUNT old files. Same log format, same file name, same
  directory (config.DATA_DIR) - a reader tailing the old path sees no
  difference day to day, it just stops growing without bound.
* The level and the two knobs above are configurable (LOG_LEVEL,
  LOG_MAX_BYTES, LOG_BACKUP_COUNT) with the exact same defaults the old
  hardcoded call used, so a fresh checkout behaves identically unless you
  deliberately change something in .env.
* The console handler is still skipped under a --windowed PyInstaller
  build (sys.stderr is None there) - unchanged from before - and can also
  be turned off explicitly with LOG_CONSOLE_ENABLED=false for a terminal
  run that only wants the file.
* Tier 19: every record now carries a %(turn_id)s field - the same id
  telemetry/trace.py's current_turn_id() is tracking for whichever thread
  emitted the line ("-" when none is active). A _TurnIdFilter is attached
  to each handler this module builds (not the root logger itself -
  logging.Logger.callHandlers() walks ancestor LOGGERS' HANDLERS directly
  without consulting ancestor loggers' own filters, so a filter on the
  root logger object would silently never run for the vast majority of
  records, which originate from per-module logging.getLogger("X")
  children, not the root logger directly). This lets one turn's whole
  story - STT -> router -> LLM -> tools -> TTS, however many threads it
  crosses - be pulled out of wave_system.log with a single grep for its id.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional

LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(turn_id)s] - %(message)s"
LOG_FILENAME = "wave_system.log"

# Same defaults the old logging.basicConfig() call effectively had
# (unbounded size, no rotation) except size/backups, which previously
# didn't exist at all - 10 MB x 5 backups is a generous window (tens of
# thousands of lines) without ever letting the file run away unbounded.
DEFAULT_LEVEL = "INFO"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

_configured = False


class _RedactionFilter(logging.Filter):
    """Tier 20: scrubs text shaped like a secret (API keys, a JWT, card
    numbers, Aadhaar/PAN/UPI/IFSC/phone numbers - see brain/redaction.py)
    out of a record's rendered message before a handler formats/emits it.
    Rewrites record.msg to the already-redacted, fully-rendered message
    and clears record.args, so a %-style logger.info("...%s...", secret)
    call is covered too, not just an f-string already baked into msg -
    getMessage() does the % substitution once here; Formatter.format()
    then just uses msg as-is. A second handler's filter redacting the
    same (now already-redacted) text again is a harmless no-op - redacting
    an already-redacted string finds nothing left to redact.

    Attached per-HANDLER (see this module's own docstring on why,
    alongside Tier 19's _TurnIdFilter) and gated by
    config.REDACT_SECRETS_ENABLED, same kill-switch brain/redaction.py's
    own redact() already honors - this filter just calls it, so the one
    toggle controls both."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from brain import redaction
            record.msg = redaction.redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class _TurnIdFilter(logging.Filter):
    """Stamps record.turn_id from telemetry.trace.current_turn_id() (or
    "-" when no turn is active on the emitting thread) just before a
    handler formats/emits the record. A filter, not something set at the
    call site, so every log line gets it with zero changes to the
    hundreds of existing logger.info(...)/logger.warning(...) calls across
    the codebase. Imports telemetry.trace lazily (inside filter(), not at
    module import time) to avoid a circular import - settings/config.py
    (which telemetry.trace imports) is what calls this module's setup()."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from telemetry import trace
            record.turn_id = trace.current_turn_id() or "-"
        except Exception:
            record.turn_id = "-"
        return True


def _env_bool(env, name: str, default: bool) -> bool:
    return (env.get(name, str(default)) or "").strip().lower() not in ("false", "0", "no")


def _env_int(env, name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(env.get(name, default)))
    except (TypeError, ValueError):
        logging.getLogger("LoggingConfig").warning(f"{name}={env.get(name)!r} isn't a number - using {default}.")
        return default


def _resolve_level(name: str) -> int:
    level = logging.getLevelName((name or DEFAULT_LEVEL).strip().upper())
    # getLevelName(garbage) returns the string "Level <garbage>" rather
    # than raising - catch that instead of silently logging nothing.
    if not isinstance(level, int):
        logging.getLogger("LoggingConfig").warning(f"LOG_LEVEL={name!r} isn't a known level - using {DEFAULT_LEVEL}.")
        return logging.INFO
    return level


def build_handlers(data_dir: str, env=None):
    """The handler list setup() installs, built from `env` (defaults to
    os.environ) - split out from setup() so a test can inspect exactly
    what would be attached without touching the real root logger."""
    env = os.environ if env is None else env
    log_path = os.path.join(data_dir, LOG_FILENAME)
    max_bytes = _env_int(env, "LOG_MAX_BYTES", DEFAULT_MAX_BYTES, minimum=0)
    backup_count = _env_int(env, "LOG_BACKUP_COUNT", DEFAULT_BACKUP_COUNT, minimum=0)

    file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handlers = [file_handler]

    # A --windowed PyInstaller build has no console (sys.stderr is None
    # there) - unchanged from the pre-Tier-2 basicConfig call. LOG_CONSOLE_
    # ENABLED=false additionally silences it for a normal terminal run
    # that only wants the file.
    if sys.stderr is not None and _env_bool(env, "LOG_CONSOLE_ENABLED", True):
        handlers.append(logging.StreamHandler())

    formatter = logging.Formatter(LOG_FORMAT)
    for h in handlers:
        h.setFormatter(formatter)
        # Order doesn't matter functionally (independent record attributes)
        # but redact first, by convention, so a turn_id is never itself
        # treated as secret-shaped by a future, broader redaction pattern.
        h.addFilter(_RedactionFilter())
        h.addFilter(_TurnIdFilter())
    return handlers


def setup(data_dir: Optional[str] = None, env=None, force: bool = False) -> None:
    """Configures the root logger: a rotating file handler in `data_dir`
    (config.DATA_DIR if not given) plus a console handler (unless
    suppressed - see build_handlers), at LOG_LEVEL. Idempotent - a second
    call without force=True is a no-op, so importing settings.config more
    than once (tests routinely do) never doubles up handlers. force=True
    tears down and rebuilds anyway - used when a test wants to point
    logging at a fresh tmp dir, or LOG_* settings changed at runtime."""
    global _configured
    if _configured and not force:
        return
    env = os.environ if env is None else env
    if data_dir is None:
        from settings.config import config  # local import: config imports this module
        data_dir = config.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    root.setLevel(_resolve_level(env.get("LOG_LEVEL", DEFAULT_LEVEL)))
    for h in build_handlers(data_dir, env=env):
        root.addHandler(h)
    _configured = True
