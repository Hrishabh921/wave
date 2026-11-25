"""
brain/memory_consolidation.py — Tier 2 item #6: a consolidation pass.
================================================================================
Wave's memory has been explicit-only since Phase 3: db.save_memory()/
get_memory() exist, but the only thing that ever calls them is the user
directly asking ("remember that...") via remember_this/recall_memory in
brain/groq_core.py. Nothing reads the ordinary back-and-forth of a
conversation and notices, on its own, "the user just mentioned their
sister's name" unless they explicitly say "remember this."

This is that: a background pass that periodically reads the last stretch
of chat_history, asks the model - through the SAME brain/llm_provider.py
plumbing the main chat and reminders/composer.py already use, never a
second/separate client - which of it is actually worth remembering long
term, and writes anything durable into the SAME db.save_memory() table
remember_this writes to. recall_memory, get_all_memory() (folded into the
system prompt) and semantic recall all pick it up exactly as if the user
had asked Wave to remember it themselves.

DESIGN, adapted from SARA's own core/memory_consolidation.py:
  - Same overall shape as automation/proactive.py - this module's closest
    sibling, not SARA's - rather than ported file-for-file: tick() is
    pure and independently testable (no sleeping, no thread), start()
    wraps that in a loop on its own daemon thread, config-gated, one bad
    tick logged and swallowed rather than killing the thread. Wired into
    ui/main_window.py the same way (started/stopped alongside
    proactive_engine). SARA's own memory_consolidation.py already follows
    its own proactive.py almost exactly, so "match Wave's closest
    sibling instead" lands in nearly the same place anyway.
  - Reads via db.get_chat_history(), not a new queue fed by explicit
    submit() calls at every turn's end the way telemetry/store.py's
    writer thread is fed. chat_history is already durably written by
    every think()/think_stream() call regardless of this module
    (db.save_message()) - there's no risk of losing a turn the way trace
    rows would be lost without telemetry's queue, because the queue's
    actual job there (getting a write off the hot path) is already done,
    by something else, for free, before this module ever runs. What
    chat_history doesn't give this module is a row id or count to use as
    a precise cursor, so staleness is tracked with a cheap in-memory
    fingerprint of the last-seen window instead (see tick()) rather than
    a true cursor - see that method for what this trades away.
  - PRIVACY: if db.history_enabled is False (Settings > Privacy > "Don't
    save my chats to disk"), tick() skips entirely rather than reading
    db's RAM-only session-buffer fallback and persisting anything derived
    from it to the memory table. get_chat_history() itself degrades
    gracefully to that buffer for ordinary chat continuity, which is
    right for that call's normal job - but writing a DURABLE fact to disk
    because of something that only still exists in RAM would quietly
    defeat the actual point of that setting. Neither roadmap doc called
    this out explicitly; it followed directly from db_manager.py's own
    get_chat_history() docstring once this module needed to call it.
  - The extraction prompt shows the model what's ALREADY in memory
    (db.get_all_memory()) and asks it to skip anything already covered,
    rather than this module tracking a precise "already consolidated"
    cursor: Wave's memory is key-value with last-write-wins
    (ON CONFLICT(key) DO UPDATE - see database/db_manager.py's
    save_memory()), not SARA's free-text RAG store, so the model also has
    to pick a short existing-or-new KEY per fact, not just phrase a
    sentence - showing it the existing keys is what lets "the user's
    sister's name came up in two unrelated conversations three days apart"
    update one row instead of creating a near-duplicate.
"""
from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from brain import llm_provider
from brain.model_router import background_model
from database.db_manager import db
from settings.config import config

logger = logging.getLogger("MemoryConsolidation")

# Bullet/number prefixes a model sometimes adds despite being asked for
# bare "key: value" lines - stripped before parsing, same idea as SARA's
# own _BULLET_PREFIX_RE.
_PREFIX_RE = re.compile(r"^[\-\*\d\.\)\s]+")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")

_EXTRACTION_PROMPT = (
    "You are reading a short snippet of an ongoing conversation between Wave "
    "(a desktop voice assistant) and its user, to decide what's worth "
    "remembering about the user long-term.\n\n"
    "Only extract facts that would still matter weeks from now: stable "
    "preferences, names of people/pets, ongoing projects, important dates, "
    "allergies or health notes they volunteered, recurring habits. NOT small "
    "talk, greetings, one-off questions, or anything about THIS conversation "
    "itself.\n\n"
    "Already remembered about this user (skip anything already covered here "
    "unless the conversation clearly updates it):\n{existing}\n\n"
    "Conversation snippet:\n{conversation}\n\n"
    "Reply with up to {max_facts} facts, one per line, each formatted EXACTLY as:\n"
    "key: value\n"
    "- key: a short label, lowercase letters/digits/underscores only, under 30 "
    "characters (e.g. favorite_color, sisters_name, allergy)\n"
    "- value: the fact itself, under 20 words, no markdown\n\n"
    "If there is nothing durable and new worth remembering, reply with exactly: NONE"
)


@dataclass(frozen=True)
class ExtractedFact:
    key: str
    value: str


# ---------------------------------------------------------------------------
# Client acquisition - same lazy-cached-with-lock shape as
# reminders/composer.py's _get_client_and_cfg(), deliberately not shared
# code with it: a module this small duplicating six lines is simpler to
# read standalone than a new shared helper would be to introduce, and it
# keeps this module's only dependency on composer.py being "the same
# pattern," never an import of composer's private cache.
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_cached_client = None
_cached_cfg = None
_client_resolved = False


def _get_client_and_cfg():
    global _cached_client, _cached_cfg, _client_resolved
    with _client_lock:
        if not _client_resolved:
            _client_resolved = True
            try:
                cfg = llm_provider.resolve_provider()
                _cached_cfg = cfg
                _cached_client = llm_provider.build_client(cfg) if cfg.configured else None
            except Exception as e:
                logger.info(f"no LLM provider available ({type(e).__name__}: {e})")
                _cached_client, _cached_cfg = None, None
        return _cached_client, _cached_cfg


def _parse_facts(raw: str, max_facts: int) -> List[ExtractedFact]:
    """Turns the model's raw "key: value" lines into validated
    ExtractedFacts - never raises, a malformed line is just dropped
    rather than failing the whole tick. db.save_memory() itself also
    lowercases/strips the key, so _KEY_RE here is about rejecting
    obviously-not-a-label garbage (a whole sentence, stray punctuation),
    not normalization - that still happens once, in save_memory()."""
    if not raw or not raw.strip():
        return []
    if raw.strip().upper().startswith("NONE"):
        return []

    facts: List[ExtractedFact] = []
    for line in raw.strip().splitlines():
        cleaned = _PREFIX_RE.sub("", line).strip().strip("`")
        if not cleaned or cleaned.upper() == "NONE" or ":" not in cleaned:
            continue
        key, _, value = cleaned.partition(":")
        key, value = key.strip().lower().replace(" ", "_"), value.strip()
        # count("_") > 4 catches what _KEY_RE alone can't: replacing spaces
        # with underscores makes a whole mangled clause ("the_user_mentioned_
        # their_city_is") pass a plain lowercase/underscore/length check just
        # as easily as a real short label does - a real label is a word or
        # two, never six.
        if not key or not value or not _KEY_RE.match(key) or key.count("_") > 4:
            continue
        # Drop truncated/garbage values the same way SARA's extractor does
        # ("User's name is" with nothing after it) - too short, or trails
        # off on a word that can't end a sentence.
        words = value.rstrip(" .:;,").split()
        if len(words) < 1 or len(value) > 200:
            continue
        if words[-1].lower() in ("is", "are", "was", "has", "the", "a", "an", "of", "to", "in", "and"):
            continue
        facts.append(ExtractedFact(key=key, value=value))
    return facts[:max_facts]


def _extract_facts(client, cfg, conversation_text: str, existing: List[Tuple[str, str]],
                    max_facts: int) -> List[ExtractedFact]:
    """ONE bounded call - deliberately NOT cfg.timeout, which is sized for
    a full conversational turn (see reminders/composer.py's _call_llm()
    for the same reasoning): this is background housekeeping, not
    something a person is waiting on, so it should fail fast and just
    skip the tick rather than hold a thread open. Never raises - any
    failure here degrades to "no facts extracted this tick," same as a
    skipped reminder reword."""
    existing_block = "\n".join(f"- {k}: {v}" for k, v in existing) if existing else "(nothing yet)"
    prompt = _EXTRACTION_PROMPT.format(existing=existing_block, conversation=conversation_text,
                                       max_facts=max_facts)
    try:
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=background_model(cfg),  # Tier 34: the fast model, not the main one
            temperature=0.2,         # extraction, not conversation - low variance is the point
            max_tokens=200,
            timeout=config.MEMORY_CONSOLIDATION_TIMEOUT_S,
        )
        raw = ((response.choices[0].message.content if response and response.choices else "") or "")
    except Exception as e:
        logger.info(f"extraction call failed, skipping this tick ({type(e).__name__}: {e})")
        return []
    return _parse_facts(raw, max_facts)


class MemoryConsolidator:
    """Pure check logic lives in tick() - synchronous, no sleep,
    independently testable without waiting on a real thread, same split
    as automation/proactive.py's check_once()/start(). start() wraps
    that in a loop on its own daemon thread."""

    def __init__(self, interval: float = None):
        self.interval = interval if interval is not None else config.MEMORY_CONSOLIDATION_INTERVAL_S
        self._thread = None
        self._stop_event = threading.Event()
        self._last_seen = None   # fingerprint of the chat_history window tick() last acted on

    def tick(self) -> List[ExtractedFact]:
        """One consolidation pass. Returns whatever facts were written
        (possibly empty) - mainly so tests can assert on it directly
        rather than only on its side effects."""
        if not config.MEMORY_CONSOLIDATION_ENABLED:
            return []
        if not db.history_enabled:
            logger.debug("chat history is off (Settings > Privacy) - skipping this tick")
            return []

        rows = db.get_chat_history(limit=config.MEMORY_CONSOLIDATION_BATCH_SIZE)
        if len(rows) < 2:
            return []  # not enough conversation yet to be worth summarizing

        fingerprint = tuple(rows)
        if fingerprint == self._last_seen:
            return []  # nothing new since the last tick - don't spend an LLM call on it
        self._last_seen = fingerprint

        client, cfg = _get_client_and_cfg()
        if client is None:
            return []

        conversation_text = "\n".join(f"{role}: {message}" for role, message in rows)
        if not conversation_text.strip():
            return []

        existing = db.get_all_memory()
        facts = _extract_facts(client, cfg, conversation_text, existing, config.MEMORY_CONSOLIDATION_MAX_FACTS)
        saved: List[ExtractedFact] = []
        for fact in facts:
            try:
                # Tier 20 provenance: this runs on the background
                # consolidation thread over a BATCH of past messages, not
                # inside any single live turn, so there's no one turn_id
                # that honestly applies - left None rather than attaching
                # whatever this thread's current_turn_id() happens to be
                # (which would misleadingly point at an unrelated turn).
                if db.save_memory(fact.key, fact.value, source="auto_consolidated"):
                    saved.append(fact)
            except Exception:
                logger.exception(f"save_memory failed for key '{fact.key}'")
        if saved:
            logger.info(f"consolidated {len(saved)} fact(s): {', '.join(f.key for f in saved)}")
        return saved

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("consolidation tick failed")
            self._stop_event.wait(self.interval)

    def start(self):
        if not config.MEMORY_CONSOLIDATION_ENABLED:
            logger.info("Memory consolidation disabled (MEMORY_CONSOLIDATION_ENABLED=false).")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="WaveMemoryConsolidation", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)

    def is_alive(self) -> bool:
        """Tier 19: lets automation/thread_supervisor.py watch this thread
        and restart it if it ever dies unexpectedly (see that module's
        own docstring for why a dead background thread can otherwise go
        unnoticed)."""
        return self._thread is not None and self._thread.is_alive()


memory_consolidator = MemoryConsolidator()
