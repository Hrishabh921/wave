"""
brain/skills_registry.py — Phase 3, extended in Phase 4 for hot reload

Before this file existed, a tool lived in two places 400 lines apart: its
Groq-facing JSON schema sat in the module-level TOOLS list near the top of
groq_core.py, and its actual behavior sat in an elif branch inside
WaveBrain._dispatch_tool() near the bottom. Adding, renaming, or removing a
tool meant touching both spots and keeping them in sync by hand — nothing
enforced that every TOOLS entry had a matching elif, or vice versa.

SkillRegistry fixes that by making the schema an argument to a decorator
that sits directly on the handler it describes, so a tool's name,
description, parameters, and implementation are all one unit of code:

    @skill("open_app", "Opens any application...",
           {"app_name": {"type": "string", ...}}, ["app_name"])
    def _skill_open_app(self, args: dict) -> str:
        return AppController.open_app(args.get("app_name", ""))

`skills.tool_specs()` then builds the Groq/Ollama-facing TOOLS list from
whatever got registered, and `skills.get_handler(name)` is what
_dispatch_tool() uses to find the right function at call time.

Phase 4 (hot reload)
--------------------
The built-in skills still register once, at import time. On top of that the
registry can now have skills added and removed while Wave is running, which
is what brain/skill_loader.py does when you drop a file into user_skills/:

  * Every skill remembers its *source* ("builtin" or "user:<file>.py").
  * `staging(source)` collects the registrations a file makes while it is
    being executed and only swaps them in if the whole file ran cleanly -
    a half-written or broken file can never leave the registry half-updated,
    and the previous good version of that file keeps working.
  * Skills coming from staging are validated strictly (name charset/length,
    JSON-serialisable schema, `required` ⊆ properties), because the tool
    list is sent to the LLM on EVERY request: one malformed entry would
    make the provider reject every message Wave sends until it was fixed.
  * A user file may not shadow a built-in skill or another file's skill.
  * All reads/writes are guarded by a lock so the watcher thread can mutate
    the registry while the AI worker thread is building a request.
"""
import json
import logging
import re
import threading
from contextlib import contextmanager

logger = logging.getLogger("SkillRegistry")

BUILTIN_SOURCE = "builtin"

# What OpenAI-compatible tool APIs accept for a function name.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SkillValidationError(ValueError):
    """A skill's name or schema isn't something an LLM tool API will accept
    (or it collides with an existing skill from a different source)."""


def validate_skill_spec(name, description, params, required):
    """Strict check applied to hot-loaded skills. Raises SkillValidationError
    with a message that is safe to show the person who wrote the skill."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SkillValidationError(
            f"Invalid skill name {name!r}: use 1-64 letters, digits, '_' or '-' only."
        )
    if not isinstance(description, str) or not description.strip():
        raise SkillValidationError(
            f"Skill '{name}' needs a non-empty description - it's how the AI decides when to use it."
        )
    if params is not None and not isinstance(params, dict):
        raise SkillValidationError(f"Skill '{name}': params must be a dict of {{arg_name: schema}}.")
    for arg, schema in (params or {}).items():
        if not isinstance(arg, str) or not isinstance(schema, dict):
            raise SkillValidationError(
                f"Skill '{name}': parameter {arg!r} must map to a schema dict like {{'type': 'string'}}."
            )
    if required is not None and not isinstance(required, (list, tuple)):
        raise SkillValidationError(f"Skill '{name}': required must be a list of parameter names.")
    missing = [r for r in (required or []) if r not in (params or {})]
    if missing:
        raise SkillValidationError(
            f"Skill '{name}': required parameter(s) {missing} are not declared in params."
        )
    try:
        json.dumps({"description": description, "properties": params or {}, "required": list(required or [])})
    except (TypeError, ValueError) as e:
        raise SkillValidationError(f"Skill '{name}': schema isn't JSON-serialisable ({e}).")


class _Stage:
    """Registrations made by one file while it is being executed. Not live
    until the `staging()` block exits without an exception."""

    def __init__(self, source):
        self.source = source
        self.entries = {}  # name -> entry

    @property
    def names(self):
        return list(self.entries.keys())


class SkillRegistry:
    """A name -> (schema, handler) table. Built-ins are added at import time
    by the @skill decorator as groq_core.py's WaveBrain class body executes;
    user skills are added/replaced/removed at runtime through staging()."""

    def __init__(self):
        self._skills = {}  # name -> {"spec": {...}, "handler": callable, "source": str}
        self._lock = threading.RLock()
        self._tls = threading.local()
        self._revision = 0

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, name, description, params=None, required=None, confirm=False):
        """Decorator factory. Wraps a handler of the form
        `fn(brain, args: dict) -> str` and stores its Groq-style
        tool-calling schema alongside it, keyed by `name`.

        Outside a staging() block (i.e. the built-in skills at import time)
        a duplicate name raises ValueError immediately - almost always a
        copy-paste mistake, and far better to crash loudly on startup than
        to silently shadow one tool with another.

        Inside a staging() block the schema is validated strictly and the
        entry goes to the stage instead of the live table.

        Phase 7: `confirm=True` opts this tool into the confirm-before-
        sensitive-action gate (see WaveBrain._guard() / _guard_keyword() in
        brain/groq_core.py) - the tool is never run on the first ask; Wave
        arms a pending confirmation and asks the user to say yes/no first.
        Deliberately NOT part of `spec` - it's never sent to the LLM (no
        reason for the model, or anything that influenced it, to even know
        which tools are gated) and is looked up separately via
        requires_confirm(). Any skill, built-in or user-dropped, can set it."""
        def decorator(fn):
            stage = getattr(self._tls, "stage", None)
            spec = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": params or {},
                        "required": required or [],
                    },
                },
            }
            if stage is not None:
                validate_skill_spec(name, description, params, required)
                if not callable(fn):
                    raise SkillValidationError(f"Skill '{name}' must decorate a callable.")
                if name in stage.entries:
                    raise SkillValidationError(f"Skill '{name}' is defined twice in {stage.source}.")
                with self._lock:
                    existing = self._skills.get(name)
                    if existing is not None and existing["source"] != stage.source:
                        owner = "a built-in skill" if existing["source"] == BUILTIN_SOURCE \
                            else f"{existing['source']}"
                        raise SkillValidationError(
                            f"Skill name '{name}' is already taken by {owner}; pick a different name."
                        )
                stage.entries[name] = {"spec": spec, "handler": fn, "source": stage.source,
                                        "confirm": bool(confirm)}
                return fn

            with self._lock:
                if name in self._skills:
                    raise ValueError(
                        f"Duplicate skill name '{name}' — already registered by "
                        f"{self._skills[name]['handler'].__name__}()."
                    )
                self._skills[name] = {"spec": spec, "handler": fn, "source": BUILTIN_SOURCE,
                                       "confirm": bool(confirm)}
                self._revision += 1
            return fn
        return decorator

    @contextmanager
    def staging(self, source):
        """Collect everything registered inside the `with` block under
        `source`, then atomically replace that source's previous skills with
        it - or, if the block raises, throw the batch away and leave the
        registry exactly as it was."""
        if getattr(self._tls, "stage", None) is not None:
            raise RuntimeError("skill staging blocks cannot be nested")
        stage = _Stage(source)
        self._tls.stage = stage
        try:
            yield stage
        except BaseException:
            self._tls.stage = None
            raise
        self._tls.stage = None
        self._commit(stage)

    def _commit(self, stage):
        with self._lock:
            for name in stage.entries:  # re-check under the lock: nothing may have sneaked in
                existing = self._skills.get(name)
                if existing is not None and existing["source"] != stage.source:
                    raise SkillValidationError(
                        f"Skill name '{name}' is already taken by {existing['source']}."
                    )
            for old in [n for n, e in self._skills.items() if e["source"] == stage.source]:
                del self._skills[old]
            self._skills.update(stage.entries)
            self._revision += 1

    # ------------------------------------------------------------------
    # Removal
    # ------------------------------------------------------------------
    def unregister(self, name):
        """Remove one skill by name. Returns whether anything was removed."""
        with self._lock:
            if name in self._skills:
                del self._skills[name]
                self._revision += 1
                return True
        return False

    def unregister_source(self, source):
        """Remove every skill that came from `source`; returns their names."""
        with self._lock:
            gone = [n for n, e in self._skills.items() if e["source"] == source]
            for n in gone:
                del self._skills[n]
            if gone:
                self._revision += 1
        return gone

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------
    def tool_specs(self) -> list:
        """The Groq/Ollama-facing TOOLS list — one {"type": "function", ...}
        dict per registered skill, in registration order. Built fresh from a
        locked snapshot, so a skill hot-loaded a second ago is already in it."""
        with self._lock:
            return [entry["spec"] for entry in self._skills.values()]

    def get_handler(self, name: str):
        """The registered handler for `name`, or None if no skill was ever
        registered under that name (an unknown/renamed tool)."""
        with self._lock:
            entry = self._skills.get(name)
        return entry["handler"] if entry else None

    def get_spec(self, name: str):
        """Phase 6: the full Groq-style tool spec dict for `name` (the exact
        {"type": "function", "function": {..., "parameters": {...}}} shape
        from tool_specs()), or None if `name` isn't registered. Used by
        _dispatch_tool()'s argument validation to know which parameters are
        `required` before the handler ever runs."""
        with self._lock:
            entry = self._skills.get(name)
        return entry["spec"] if entry else None

    def requires_confirm(self, name: str) -> bool:
        """Phase 7: True if `name` was registered with confirm=True and
        must go through the confirm-before-sensitive-action gate before it
        runs. False for an unregistered name - _dispatch_tool() already
        reports an unknown tool before this would ever be asked."""
        with self._lock:
            entry = self._skills.get(name)
        return bool(entry and entry.get("confirm"))

    def names(self) -> list:
        """Every registered skill name, in registration order — mainly
        useful for tests and sanity checks against the elif-chain-era
        tool count."""
        with self._lock:
            return list(self._skills.keys())

    def names_by_source(self, source) -> list:
        with self._lock:
            return [n for n, e in self._skills.items() if e["source"] == source]

    def source_of(self, name):
        with self._lock:
            entry = self._skills.get(name)
        return entry["source"] if entry else None

    def describe(self) -> list:
        """[{name, source, description}] for every skill - used by the
        list-my-skills tool and the UI."""
        with self._lock:
            return [
                {"name": n, "source": e["source"],
                 "description": e["spec"]["function"]["description"]}
                for n, e in self._skills.items()
            ]

    @property
    def revision(self) -> int:
        """Bumps every time the set of skills changes."""
        return self._revision

    def __contains__(self, name):
        with self._lock:
            return name in self._skills

    def __len__(self):
        with self._lock:
            return len(self._skills)


# One shared registry for the whole app, and `skill` as the short name
# used at each handler definition — `@skill(...)` reads the same way
# `@app.route(...)`-style decorators do in other frameworks.
skills = SkillRegistry()
skill = skills.register
