"""
automation/audit.py — Phase 4: "what did Wave just do?"

One place that remembers every action Wave takes, and - for the ones that can
be taken back - the recipe for taking them back (see automation/undo.py).

How rows get written
--------------------
Backends (FileManager, AppController) wrap their primary operations like this:

    with audit.action("delete_file", {"file_name": name, "path": path}) as act:
        ...do the work...
        act.set_undo("restore_from_trash", {...}, "Restore 'report.txt'")
        return act.ok("Deleted 'report.txt'.")     # or act.fail(...) / act.done(result)

and the AI dispatcher wraps every tool call in an *adoptable* scope of the same
name. When the backend runs inside such a scope it adopts it (one row per
user-visible action, carrying the backend's richer details + undo recipe); when
nothing is wrapping it (a UI button, a palette command) the backend scope
simply becomes the row itself. Scopes of a different name nest as independent
rows (a workflow's individual steps). `collapse_if_children=True` (the keyword
matcher) drops its own row when its steps already logged themselves.

Privacy
-------
Rows always live in RAM for the session (the Activity page and "undo" read from
there, so nothing touches SQLite on the GUI thread). They're also written to the
`action_log` table - unless Settings > Privacy > "Save chat history locally" is
off, in which case they stay RAM-only and vanish when Wave exits, exactly like
the chat history does.

Tier 19: the actual SQLite write for that `action_log` row (and its later
update, if the action gets undone, and its occasional prune once the table
passes AUDIT_MAX_ENTRIES) now goes through DbStore's own
database.db_writer.BackgroundDbWriter instead of running inline inside
_commit()/mark_undone()/clear() - see DbStore's docstring for the id-
reconciliation trick that keeps this correct (an UPDATE enqueued for a row
whose INSERT hasn't actually landed yet still targets the right id, because
both run, in order, on the SAME worker thread). This is what makes the
"nothing touches SQLite on the GUI thread" line above fully true rather
than true-in-practice-because-tool-dispatch-happens-on-a-worker-thread-
anyway: a UI-button-triggered action (FileManager/AppController called
directly, not through the AI tool loop) used to commit its audit row
inline on whatever thread called it, GUI thread included.
"""
import itertools
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from database.db_writer import BackgroundDbWriter

logger = logging.getLogger("Audit")

# Read-only look-ups that would only add noise to a "what did you do" timeline.
QUIET_TOOLS = frozenset({
    "search_file", "list_workflows", "list_reminders", "recall_memory", "get_pc_info",
    "get_live_information", "list_recent_actions", "list_my_skills",
    "list_calendar_events", "search_my_notes",  # Phase 6: read-only look-ups, same as list_reminders/recall_memory
    "list_timers", "check_timer", "stopwatch_status",  # Tier 5: same reasoning, read-only look-ups
    "get_brightness_status",  # Tier 14: a plain read, same reasoning as get_pc_info just above
})

TOOL_LABELS = {
    "create_file": "Created file", "create_folder": "Created folder", "delete_file": "Deleted file",
    "delete_folder": "Deleted folder", "take_screenshot": "Took a screenshot",
    "delete_latest_screenshot": "Deleted the latest screenshot", "open_app": "Opened",
    "open_website": "Opened website", "close_app": "Closed", "close_current_window": "Closed the current window",
    "close_current_tab": "Closed the current tab", "write_notepad": "Typed into Notepad",
    "send_whatsapp_message": "Sent a WhatsApp message to", "toggle_hardware": "Toggled",
    "open_system_setting": "Opened settings:", "lock_pc": "Locked the PC", "play_youtube": "Played",
    "media_play_pause": "Play/pause", "media_next_track": "Next track", "media_previous_track": "Previous track",
    "volume_up": "Volume up", "volume_down": "Volume down",
    "search_google": "Searched Google for", "set_reminder": "Set a reminder:", "cancel_reminder": "Cancelled reminder",
    "remember_this": "Remembered", "forget_memory": "Forgot", "create_workflow": "Saved workflow",
    "run_workflow": "Ran workflow", "read_screen_text": "Read the screen", "take_photo": "Took a photo",
    "get_clipboard": "Read the clipboard", "undo": "Undid", "undo_last_action": "Undid the last action",
    "keyword_command": "Command:",
    # Phase 6
    "create_calendar_event": "Added calendar event:", "cancel_calendar_event": "Cancelled calendar event",
    "reindex_notes": "Reindexed notes", "proactive_battery": "Proactive nudge (battery)",
    "proactive_calendar": "Proactive nudge (calendar)",
    # Tier 14: power/session, volume, brightness, and the rest of
    # per-window management. volume_up/volume_down above cover TWO cases
    # now, not just the pre-Tier-14 media-key press: AppController's own
    # volume_up/brightness_up etc. mostly delegate to set_volume/
    # set_brightness instead (reporting the resulting percent, which a
    # bare direction never could) - see automation/app_control.py's own
    # comment on _nudge_volume/_nudge_brightness for exactly when each
    # name gets used. volume_max/volume_min/brightness_max/brightness_min
    # have NO entry here - they always delegate to set_volume/
    # set_brightness, never audited under their own name.
    "set_volume": "Set volume", "mute_volume": "Muted the volume", "unmute_volume": "Unmuted the volume",
    "set_brightness": "Set brightness",
    "brightness_up": "Brightness up", "brightness_down": "Brightness down",
    "move_window": "Moved", "resize_window": "Resized", "maximize_window": "Maximized",
    "restore_window": "Restored", "set_always_on_top": "Set always-on-top", "set_fullscreen": "Toggled fullscreen",
    "show_desktop": "Showed the desktop", "minimize_all_windows": "Minimized all windows",
    "switch_window": "Switched window", "restart_app": "Restarted",
    "shutdown_pc": "Scheduled a shutdown", "restart_pc": "Scheduled a restart",
    "sleep_pc": "Scheduled sleep", "hibernate_pc": "Scheduled hibernate", "log_off": "Scheduled log off",
    "cancel_shutdown": "Cancelled a scheduled power action",
}
_MAIN_ARG_KEYS = ("file_name", "folder_name", "app_name", "site", "query", "contact_name", "name", "instruction",
                  "setting", "message", "text", "key", "target", "of", "label",
                  "window_name", "percent")  # Tier 14

_FAIL_PREFIXES = ("error", "failed", "couldn't", "could not", "i couldn't", "i can't", "i cannot", "unable",
                  "sorry", "something went wrong", "hardware toggle not recognized", "no screenshots",
                  "you don't have any", "i don't have a", "that time has already passed", "i need ")
_BLOCK_PREFIXES = ("for safety", "i won't", "sleep isn't available")


def infer_status(result: str) -> str:
    """Display-only guess ("ok" | "failed" | "blocked") for tools that only return text."""
    r = (result or "").strip().lower().lstrip("'\"")
    if r.startswith(_BLOCK_PREFIXES):
        return "blocked"
    if r.startswith(_FAIL_PREFIXES):
        return "failed"
    return "ok"


def _shorten(value: Any, limit: int = 200) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, dict):
        return {str(k): _shorten(v, limit) for k, v in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_shorten(v, limit) for v in list(value)[:20]]
    return value


def summarize(tool: str, args: Dict[str, Any]) -> str:
    """'Deleted file report.txt' / 'Opened chrome' - used by the voice skill and the UI."""
    label = TOOL_LABELS.get(tool, tool.replace("_", " ").capitalize())
    detail = ""
    for key in _MAIN_ARG_KEYS:
        v = (args or {}).get(key)
        if v not in (None, ""):
            detail = str(v)
            break
    detail = detail if len(detail) <= 60 else detail[:57] + "…"
    return f"{label} {detail}".strip() if detail else label


@dataclass
class ActionRecord:
    id: int
    ts: str
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    result: str = ""
    status: str = "ok"                 # ok | failed | blocked
    source: str = "app"                # ai | keyword | ui | app
    undo_kind: Optional[str] = None
    undo_payload: Dict[str, Any] = field(default_factory=dict)
    undo_label: str = ""
    undone_at: Optional[str] = None

    @property
    def reversible(self) -> bool:
        return bool(self.undo_kind) and not self.undone_at

    @property
    def time_text(self) -> str:
        try:
            return datetime.fromisoformat(self.ts).strftime("%I:%M %p").lstrip("0")
        except ValueError:
            return self.ts

    def summary(self) -> str:
        return summarize(self.tool, self.args)


# =============================================================================
# Persistence back-ends
# =============================================================================
class MemoryStore:
    """No persistence at all (tests, or when saving to disk is switched off).

    Tier 19: exposes the SAME insert_async/update_async/prune_async shape
    DbStore does (Audit calls these without knowing or caring which store
    backs it) - here they're synchronous no-ops (there's never anything
    to actually write), but insert_async still calls on_done(rec, None)
    right away so Audit._on_insert_done sees the exact same "never
    persisted" outcome a disabled or failed DbStore insert already
    produces, just without a background thread involved. Deliberately
    does NOT expose is_alive()/start()/stop() - there's no thread here for
    automation/thread_supervisor.py to watch (see Audit.store_for_
    supervision's own hasattr check)."""

    def insert_async(self, rec: ActionRecord,
                      on_done: Optional[Callable[[ActionRecord, Optional[int]], None]] = None) -> None:
        if on_done is not None:
            on_done(rec, None)

    def update_async(self, rec: ActionRecord) -> None:
        pass

    def load_recent(self, limit: int) -> List[ActionRecord]:
        return []

    def prune_async(self, keep: int) -> None:
        pass

    def clear(self) -> None:
        pass


class DbStore:
    """SQLite via database.db_manager (imported lazily so importing this module stays cheap).

    Tier 19: insert/update/prune are fire-and-forget through a per-instance
    BackgroundDbWriter (one worker thread, one FIFO queue - see
    database/db_writer.py) instead of running the actual sqlite3 call
    inline on the caller's thread. clear() is the one exception - it still
    blocks the caller (via the writer's submit_and_wait(), not a direct
    call) because it's rare/user-initiated and Audit.clear() already
    clears its own in-memory rows immediately either way; letting it
    linger asynchronously would risk a slightly-stale write "resurrecting"
    a row the user just watched disappear from the Activity page.

    The id-reconciliation trick: insert_async()'s caller (Audit._commit())
    assigns the record a placeholder id synchronously and immediately
    (self._neg_ids - the SAME fallback already used when persistence is
    off or a write fails), appends it to self._rows, and notifies
    listeners right away, exactly as before Tier 19. The real id - only
    known once the INSERT actually runs - gets patched onto that SAME
    ActionRecord object, in place, from the `on_done` callback once the
    background job completes. update_async() reads `rec.id` fresh, at RUN
    time (not enqueue time), straight off that same object - since both
    jobs go through the SAME single worker thread's FIFO queue, an update
    enqueued after a still-pending insert for the same row is GUARANTEED
    to run after it, so by the time it actually runs, the real id (if any)
    is already there to read."""

    def __init__(self, dbm=None, writer: Optional[BackgroundDbWriter] = None):
        self._dbm = dbm
        self._writer = writer if writer is not None else BackgroundDbWriter(name="WaveDbWriter")

    @property
    def dbm(self):
        if self._dbm is None:
            from database.db_manager import db
            self._dbm = db
        return self._dbm

    # ---- automation/thread_supervisor.py's duck-type contract ----
    def start(self):
        self._writer.start()

    def stop(self):
        self._writer.stop()

    def is_alive(self) -> bool:
        return self._writer.is_alive()

    @staticmethod
    def _to_row(rec: ActionRecord) -> dict:
        return {"ts": rec.ts, "tool": rec.tool, "args": json.dumps(rec.args, ensure_ascii=False),
                "result": rec.result, "status": rec.status, "source": rec.source, "undo_kind": rec.undo_kind,
                "undo_payload": json.dumps(rec.undo_payload, ensure_ascii=False) if rec.undo_kind else None,
                "undo_label": rec.undo_label, "undone_at": rec.undone_at}

    @staticmethod
    def _from_row(row: dict) -> ActionRecord:
        def loads(s):
            try:
                v = json.loads(s) if s else {}
                return v if isinstance(v, dict) else {}
            except ValueError:
                return {}
        return ActionRecord(id=row["id"], ts=row["ts"], tool=row["tool"], args=loads(row.get("args")),
                            result=row.get("result") or "", status=row.get("status") or "ok",
                            source=row.get("source") or "app", undo_kind=row.get("undo_kind"),
                            undo_payload=loads(row.get("undo_payload")), undo_label=row.get("undo_label") or "",
                            undone_at=row.get("undone_at"))

    def insert_async(self, rec: ActionRecord,
                      on_done: Optional[Callable[[ActionRecord, Optional[int]], None]] = None) -> None:
        """Enqueue rec's INSERT. The row's field values are snapshotted
        NOW (not when the job actually runs) - this should record the
        action as it was at commit time, same as the old synchronous
        insert() did; only the id is reconciled later. `on_done(rec,
        new_id)` - if given - runs on the WORKER thread once the write
        completes (new_id is None on failure, same contract db_manager.
        insert_action() already has), so it must only do cheap,
        thread-safe work (Audit._on_insert_done just takes its own lock
        briefly)."""
        row = self._to_row(rec)

        def _job():
            new_id = self.dbm.insert_action(row)
            if on_done is not None:
                on_done(rec, new_id)
        self._writer.submit(_job)

    def update_async(self, rec: ActionRecord) -> None:
        """Enqueue rec's UPDATE. Reads rec.id/result/status/undone_at/
        undo_kind at RUN time, straight off the live object - see this
        class's own docstring for why that's what makes the ordering
        against a same-row insert_async() correct."""
        def _job():
            if rec.id <= 0:
                return  # never reached (or will never reach) disk - nothing to update there
            self.dbm.update_action(rec.id, result=rec.result, status=rec.status,
                                   undone_at=rec.undone_at, undo_kind=rec.undo_kind)
        self._writer.submit(_job)

    def load_recent(self, limit):
        return [self._from_row(r) for r in reversed(self.dbm.get_recent_actions(limit))]   # oldest -> newest

    def prune_async(self, keep):
        self._writer.submit(lambda: self.dbm.prune_actions(keep))

    def clear(self):
        """Still synchronous from the caller's point of view (see this
        class's own docstring for why) - just routed through the writer's
        queue so it can't run out of order against an insert/update/prune
        enqueued just before it."""
        self._writer.submit_and_wait(lambda: self.dbm.clear_actions())

    def flush(self, timeout: float = 2.0) -> bool:
        """Used at app shutdown - see Audit.shutdown()."""
        return self._writer.flush(timeout=timeout)


# =============================================================================
# Scopes
# =============================================================================
class ActionScope:
    """Context manager for one action. Methods return the result string so
    `return act.ok(msg)` / `return act.done(msg)` read naturally."""

    def __init__(self, audit: "Audit", tool: str, args: Optional[dict], source: str,
                 adoptable: bool = False, collapse_if_children: bool = False):
        self._audit = audit
        self.tool = tool
        self.args: Dict[str, Any] = dict(args or {})
        self.source = source
        self.adoptable = adoptable
        self.collapse_if_children = collapse_if_children
        self.status: Optional[str] = None
        self.result = ""
        self.undo_kind: Optional[str] = None
        self.undo_payload: Dict[str, Any] = {}
        self.undo_label = ""
        self.children = 0
        self.skipped = False

    def skip(self) -> None:
        """Nothing happened worth recording (e.g. the keyword matcher found no command)."""
        self.skipped = True

    # ---- outcome ----
    def ok(self, result: str = "") -> str:
        self.status, self.result = "ok", str(result or "")
        return result

    def fail(self, result: str = "") -> str:
        self.status, self.result = "failed", str(result or "")
        return result

    def blocked(self, result: str = "") -> str:
        self.status, self.result = "blocked", str(result or "")
        return result

    def done(self, result: str) -> str:
        """Outcome inferred from the text - unless ok()/fail()/blocked() already decided."""
        if self.status is None:
            self.status, self.result = infer_status(result), str(result or "")
        return result

    def set_undo(self, kind: str, payload: Dict[str, Any], label: str = "") -> None:
        self.undo_kind, self.undo_payload, self.undo_label = kind, dict(payload), label

    # ---- context manager ----
    def __enter__(self):
        self._audit._stack().append(self)
        # Tier 21: "started" notification - see add_listener's own docstring
        # for why this exists and why it's deliberately not paired with a
        # guaranteed "finished" counterpart. A lightweight, never-committed
        # ActionRecord (id=0, status=None) - just enough for a listener to
        # read .tool/.args/.source, same as summarize(rec.tool, rec.args)
        # already does for committed rows.
        placeholder = ActionRecord(id=0, ts=datetime.now().isoformat(timespec="seconds"),
                                    tool=self.tool, args=self.args, result="", status=None,
                                    source=self.source, undo_kind=None, undo_payload={}, undo_label="")
        self._audit._notify("started", placeholder)
        return self

    def __exit__(self, exc_type, exc, tb):
        stack = self._audit._stack()
        if stack and stack[-1] is self:
            stack.pop()
        if exc_type is not None and self.status is None:
            self.fail(f"{exc_type.__name__}: {exc}")
        self._audit._commit(self, parent=stack[-1] if stack else None)
        return False


class _JoinedScope:
    """What a backend gets when it runs inside an adoptable dispatcher scope."""

    def __init__(self, target: ActionScope, args: Optional[dict]):
        self._t = target
        if args:
            target.args.update(args)
        target.adoptable = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    ok = property(lambda self: self._t.ok)
    fail = property(lambda self: self._t.fail)
    blocked = property(lambda self: self._t.blocked)
    done = property(lambda self: self._t.done)
    set_undo = property(lambda self: self._t.set_undo)


class Audit:
    def __init__(self, store=None, max_entries: int = 1000, persist_enabled: Optional[Callable[[], bool]] = None):
        self._store = store if store is not None else DbStore()
        self._max = max(10, int(max_entries))
        self._persist_enabled = persist_enabled or self._history_switch
        self._rows: List[ActionRecord] = []          # oldest -> newest
        self._lock = threading.RLock()
        self._neg_ids = itertools.count(-1, -1)      # ids for rows that were never persisted
        self._listeners: List[Callable[[str, ActionRecord], None]] = []
        self._tls = threading.local()
        self._loaded = False

    # ---- plumbing ----
    @staticmethod
    def _history_switch() -> bool:
        try:
            from database.db_manager import db
            return bool(getattr(db, "history_enabled", True))
        except Exception:
            return True

    def _stack(self) -> list:
        s = getattr(self._tls, "stack", None)
        if s is None:
            s = self._tls.stack = []
        return s

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                loaded = self._store.load_recent(self._max)
            except Exception:
                logger.exception("Couldn't load the saved action log")
                loaded = []
            self._rows = loaded + self._rows

    def warm(self):
        """Pre-load saved rows (call from a background thread at startup)."""
        self._ensure_loaded()

    # ---- listeners ----
    def add_listener(self, callback: Callable[[str, ActionRecord], None]):
        """callback(event, record) with event in {"started", "added", "updated", "cleared"};
        runs in the thread that caused the change - hop to the GUI thread before touching
        widgets.

        Tier 21: "started" is new - fired the moment a `with audit.action(...)` scope is
        entered, before the tool handler actually runs, with a placeholder ActionRecord
        (id=0, status=None - it was never committed and never will be on its own) carrying
        just `tool`/`args`/`source`. It exists for a live "Wave is doing: X" UI indicator
        (ui/main_window.py's activity banner) that can't wait for the action to finish to
        say what's happening. It is NOT a second copy of "added" - a quiet/collapsed/joined
        action that never produces an "added" row still fires exactly one "started" first;
        a listener that wants to know when things settle again should use a short timeout
        (see _on_tool_activity's auto-hide) rather than expecting a matching "finished"-type
        event for every "started" one."""
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback):
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _notify(self, event: str, rec: Optional[ActionRecord]):
        for cb in list(self._listeners):
            try:
                cb(event, rec)
            except Exception:
                logger.exception("Audit listener failed")

    # ---- writing ----
    def action(self, tool: str, args: Optional[dict] = None, source: Optional[str] = None,
               adoptable: bool = False, collapse_if_children: bool = False):
        """Start recording an action. See the module docstring for adoption rules."""
        stack = self._stack()
        top = stack[-1] if stack else None
        if top is not None and top.adoptable and top.tool == tool and not adoptable:
            return _JoinedScope(top, _shorten(args or {}))
        inherited = top.source if top is not None else None
        return ActionScope(self, tool, _shorten(args or {}), source or inherited or "ui",
                           adoptable=adoptable, collapse_if_children=collapse_if_children)

    def _commit(self, scope: ActionScope, parent: Optional[ActionScope]):
        if scope.skipped:
            return
        if parent is not None:
            parent.children += 1                       # even quiet/collapsed children count as "handled"
        if scope.collapse_if_children and scope.children > 0:
            return
        status = scope.status or "ok"
        if scope.tool in QUIET_TOOLS and status == "ok" and not scope.undo_kind:
            return
        rec = ActionRecord(id=0, ts=datetime.now().isoformat(timespec="seconds"), tool=scope.tool,
                           args=scope.args, result=(scope.result or "")[:400], status=status, source=scope.source,
                           undo_kind=scope.undo_kind, undo_payload=scope.undo_payload, undo_label=scope.undo_label)
        self._ensure_loaded()
        # Tier 19: the real id is only known once the INSERT actually runs
        # on DbStore's background writer, which happens after this method
        # has already returned - assign the SAME placeholder id already
        # used when persistence is off or a write fails (next(self._neg_ids)),
        # so every existing consumer of rec.id that already tolerates a
        # "never persisted" row keeps working completely unchanged.
        # _on_insert_done (below) patches the real id onto this SAME
        # object, in place, once the write completes.
        rec.id = next(self._neg_ids)
        with self._lock:
            self._rows.append(rec)
            overflow = len(self._rows) - self._max
            if overflow > 0:
                del self._rows[:overflow]
        self._notify("added", replace(rec))
        if self._persist_enabled():
            self._store.insert_async(rec, on_done=self._on_insert_done)
            if overflow > 0:
                self._store.prune_async(self._max)

    def _on_insert_done(self, rec: ActionRecord, new_id: Optional[int]) -> None:
        """Runs on DbStore's background-writer thread once rec's INSERT
        completes (see DbStore.insert_async's docstring). Patches the real
        id onto the SAME ActionRecord object already sitting in
        self._rows - if it's still there; it may have already been pruned
        or cleared out in the meantime, in which case there's nothing left
        to patch - and fires an "updated" notification so anything
        tracking the row by id (the Activity page, a later mark_undone())
        sees the real one instead of the placeholder."""
        if not new_id:
            return  # the write failed - db_manager.insert_action already logged why; keep the placeholder id
        with self._lock:
            if not any(r is rec for r in self._rows):  # identity, not equality - see this method's own docstring
                return
            rec.id = new_id
        self._notify("updated", replace(rec))

    def record(self, tool: str, args: Optional[dict] = None, result: str = "", status: Optional[str] = None,
               source: str = "app") -> None:
        """One-shot row for things that have no `with` block."""
        with self.action(tool, args, source=source) as act:
            if status == "failed":
                act.fail(result)
            elif status == "blocked":
                act.blocked(result)
            elif status == "ok":
                act.ok(result)
            else:
                act.done(result)

    # ---- reading ----
    def recent(self, limit: int = 50, reversible_only: bool = False) -> List[ActionRecord]:
        """Newest first. Returns copies - safe to hand to another thread."""
        self._ensure_loaded()
        with self._lock:
            rows = [r for r in reversed(self._rows) if (r.reversible or not reversible_only)]
        return [replace(r) for r in rows[:max(0, limit)]]

    def get(self, action_id: int) -> Optional[ActionRecord]:
        self._ensure_loaded()
        with self._lock:
            for r in reversed(self._rows):
                if r.id == action_id:
                    return replace(r)
        return None

    def last_reversible(self) -> Optional[ActionRecord]:
        found = self.recent(limit=1, reversible_only=True)
        return found[0] if found else None

    # ---- mutation ----
    def mark_undone(self, action_id: int) -> bool:
        self._ensure_loaded()
        with self._lock:
            target = next((r for r in reversed(self._rows) if r.id == action_id), None)
            if target is None or target.undone_at:
                return False
            target.undone_at = datetime.now().isoformat(timespec="seconds")
            snapshot = replace(target)
        if self._persist_enabled():
            # Tier 19: `target` (the live object), not `snapshot` -
            # DbStore.update_async reads its id/result/status/undone_at
            # fresh when the job actually runs, which is what makes this
            # correct even if target's own insert_async() job is still
            # sitting ahead of this one in the SAME writer's FIFO queue
            # (same worker thread, strict order - see DbStore's own
            # docstring). update_async itself skips the actual DB call if
            # target.id is still <= 0 when it runs (never persisted, or
            # not yet - same check the old `if snapshot.id > 0` guard did
            # here, just moved to run time instead of enqueue time).
            self._store.update_async(target)
        self._notify("updated", snapshot)
        return True

    def clear(self):
        with self._lock:
            self._rows.clear()
            self._loaded = True
        try:
            self._store.clear()
        except Exception:
            logger.exception("Couldn't clear the action log")
        self._notify("cleared", None)

    @property
    def store_for_supervision(self):
        """The underlying store, if it's one automation/thread_supervisor.py
        can watch (exposes start()/stop()/is_alive() - DbStore does,
        MemoryStore doesn't), else None. Keeps ui/main_window.py from
        needing to know DbStore/MemoryStore exist at all."""
        return self._store if hasattr(self._store, "is_alive") else None

    def shutdown(self, timeout: float = 2.0) -> None:
        """Tier 19: give the store's background writer (DbStore has one;
        MemoryStore doesn't - hence the getattr/duck-type check) a bounded
        chance to flush any still-queued writes before the app exits, so
        the last few actions taken right before quitting aren't silently
        lost. Best-effort - on timeout, Wave still exits, the same
        already-accepted tradeoff as every other background daemon thread
        this app stops at shutdown. Call this from ui/main_window.py's
        closeEvent."""
        flush = getattr(self._store, "flush", None)
        if not callable(flush):
            return
        try:
            if not flush(timeout=timeout):
                logger.warning("Action log writer didn't finish flushing before the shutdown timeout")
        except Exception:
            logger.exception("Couldn't flush the action log's background writer")


def _make_default() -> Audit:
    try:
        from settings.config import config
        return Audit(store=DbStore(), max_entries=config.AUDIT_MAX_ENTRIES)
    except Exception:  # pragma: no cover - config import problems must never stop Wave starting
        return Audit(store=MemoryStore())


audit = _make_default()
