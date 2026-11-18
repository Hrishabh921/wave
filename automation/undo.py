"""
automation/undo.py — Phase 4: a real "undo" for what Wave does to your files

Two pieces:

TrashStore   Wave's own recoverable trash (database/trash/). Instead of
             destroying a file, Wave moves it here (or, when it's about to
             overwrite one, copies it here first). Each item gets its own
             sub-folder so original names never collide. Items are purged after
             TRASH_RETENTION_DAYS; files over UNDO_MAX_FILE_MB are never copied
             (they're deleted for real and the reply says they can't be undone).

UndoManager  Executes the "undo recipe" an action stored in the audit log
             (automation/audit.py). Every handler is defensive: it re-checks the
             disk before touching anything, never deletes a non-empty folder,
             never overwrites a file it can't back up, and refuses to move
             anything that isn't inside the trash folder.

Recipes (undo_kind -> payload):
    remove_created_file    {"target": path}                     file Wave created   -> moved to trash
    remove_created_folder  {"target": path}                     folder Wave made    -> removed only if empty
    restore_from_trash     {"trash": path, "original": path}    file Wave deleted   -> put back
    restore_overwritten    {"target": path, "backup": path}     file Wave replaced  -> old version put back

Adding another undoable action elsewhere is two steps: call
`act.set_undo("my_kind", {...}, "label")` inside the audited operation, and
`undo_manager.register("my_kind", handler)` where handler(payload) -> (ok, message).
"""
import logging
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from automation import audit as audit_mod
from settings.config import config

logger = logging.getLogger("Undo")

_STAMP_FMT = "%Y%m%d-%H%M%S"


@dataclass
class StashResult:
    trash_path: str
    original_path: str
    size: int


@dataclass
class UndoResult:
    ok: bool
    message: str
    action_id: Optional[int] = None


def _unique_sibling(path: str, tag: str = "restored") -> str:
    base, ext = os.path.splitext(path)
    candidate, n = f"{base} ({tag}){ext}", 2
    while os.path.exists(candidate):
        candidate = f"{base} ({tag} {n}){ext}"
        n += 1
    return candidate


class TrashStore:
    def __init__(self, root: str, retention_days: int = 7, max_bytes: int = 200 * 1024 * 1024):
        self.root = os.path.abspath(root)
        self.retention_days = max(0, int(retention_days))
        self.max_bytes = int(max_bytes)

    # ---- safety ----
    def contains(self, path: str) -> bool:
        try:
            root = os.path.realpath(self.root)
            p = os.path.realpath(path)
            return os.path.commonpath([root, p]) == root and p != root
        except ValueError:          # different drives on Windows
            return False

    # ---- stash / restore ----
    def stash(self, path: str, copy: bool = False) -> Optional[StashResult]:
        """Move (or copy) a regular file into the trash. Returns None if it's too big to keep."""
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError(f"not a regular file: {path}")
        size = os.path.getsize(path)
        if size > self.max_bytes:
            return None
        folder = os.path.join(self.root, f"{time.strftime(_STAMP_FMT)}-{uuid.uuid4().hex[:8]}")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, os.path.basename(path))
        try:
            if copy:
                shutil.copy2(path, dest)
            else:
                shutil.move(path, dest)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)   # don't leave an empty/partial item behind
            raise
        return StashResult(dest, os.path.abspath(path), size)

    def restore(self, trash_path: str, target: str, *, replace_existing: bool = False) -> Tuple[str, bool]:
        """Put a trashed file back. Returns (final_path, was_renamed). Never overwrites unless
        `replace_existing` (used only to roll an overwrite back)."""
        if not self.contains(trash_path):
            raise ValueError("refusing to restore from outside Wave's trash folder")
        if not os.path.isfile(trash_path):
            raise FileNotFoundError("the recovery copy is gone")
        folder = os.path.dirname(target)
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"the folder it was in no longer exists ({folder})")
        final, renamed = target, False
        if os.path.exists(final) and not replace_existing:
            final, renamed = _unique_sibling(final), True
        if replace_existing and os.path.isfile(final):
            os.remove(final)
        shutil.move(trash_path, final)
        self._tidy(os.path.dirname(trash_path))
        return final, renamed

    def discard(self, trash_path: str) -> None:
        """Delete one trashed item for good (only ever inside the trash folder)."""
        if self.contains(trash_path):
            folder = os.path.dirname(trash_path)
            if self.contains(folder) and os.path.dirname(os.path.realpath(folder)) == os.path.realpath(self.root):
                shutil.rmtree(folder, ignore_errors=True)

    def _tidy(self, folder: str):
        try:
            if self.contains(folder) and not os.listdir(folder):
                os.rmdir(folder)
        except OSError:
            pass

    # ---- housekeeping ----
    def _items(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        out = []
        for name in names:
            p = os.path.join(self.root, name)
            if os.path.isdir(p) and not os.path.islink(p):
                out.append(p)
        return out

    def purge_expired(self, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        cutoff = now - self.retention_days * 86400
        gone = 0
        for p in self._items():
            try:
                stamp = os.path.basename(p)[:15]
                born = time.mktime(time.strptime(stamp, _STAMP_FMT))
            except (ValueError, OverflowError):
                born = os.path.getmtime(p)
            if born < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                gone += 1
        return gone

    def empty(self) -> int:
        items = self._items()
        for p in items:
            shutil.rmtree(p, ignore_errors=True)
        return len(items)

    def size_bytes(self) -> int:
        total = 0
        for p in self._items():
            for dirpath, _, files in os.walk(p):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(dirpath, f))
                    except OSError:
                        pass
        return total


class UndoManager:
    def __init__(self, audit, trash: TrashStore):
        self.audit = audit
        self.trash = trash
        self._lock = threading.Lock()
        self._handlers: Dict[str, Callable[[dict], Tuple[bool, str]]] = {
            "remove_created_file": self._remove_created_file,
            "remove_created_folder": self._remove_created_folder,
            "restore_from_trash": self._restore_from_trash,
            "restore_overwritten": self._restore_overwritten,
        }

    def register(self, kind: str, handler: Callable[[dict], Tuple[bool, str]]):
        self._handlers[kind] = handler

    # ---- handlers: each returns (ok, message) and re-checks the disk first ----
    def _remove_created_file(self, p: dict) -> Tuple[bool, str]:
        target = p.get("target", "")
        name = os.path.basename(target)
        if not os.path.lexists(target):
            return True, f"'{name}' was already gone - nothing to undo."
        if os.path.isdir(target) or os.path.islink(target):
            return False, f"'{name}' isn't a plain file any more, so I left it alone."
        try:
            res = self.trash.stash(target)
        except PermissionError:
            return False, f"'{name}' is open in another program. Close it and try the undo again."
        if res is None:
            return False, f"'{name}' is too large to move to Wave's trash safely, so I left it where it is."
        days = self.trash.retention_days
        return True, f"Removed '{name}'. A copy is kept in Wave's trash for {days} day{'s' if days != 1 else ''}."

    def _remove_created_folder(self, p: dict) -> Tuple[bool, str]:
        target = p.get("target", "")
        name = os.path.basename(target)
        if not os.path.lexists(target):
            return True, f"Folder '{name}' was already gone - nothing to undo."
        if not os.path.isdir(target) or os.path.islink(target):
            return False, f"'{name}' isn't a plain folder any more, so I left it alone."
        if os.listdir(target):
            return False, f"Folder '{name}' isn't empty now, so I left it alone rather than risk your files."
        os.rmdir(target)
        return True, f"Removed the empty folder '{name}'."

    def _restore_from_trash(self, p: dict) -> Tuple[bool, str]:
        original, trashed = p.get("original", ""), p.get("trash", "")
        name = os.path.basename(original)
        try:
            final, renamed = self.trash.restore(trashed, original)
        except FileNotFoundError as e:
            return False, f"Couldn't restore '{name}': {e}."
        except ValueError:
            return False, f"Couldn't restore '{name}': that recovery record doesn't look right, so I didn't touch anything."
        if renamed:
            return True, (f"Restored it as '{os.path.basename(final)}' - something named '{name}' "
                          f"is already there, and I didn't want to overwrite it.")
        return True, f"Restored '{name}' to {os.path.dirname(final)}."

    def _restore_overwritten(self, p: dict) -> Tuple[bool, str]:
        target, backup = p.get("target", ""), p.get("backup", "")
        name = os.path.basename(target)
        try:
            self.trash.restore(backup, target, replace_existing=True)
        except FileNotFoundError as e:
            return False, f"Couldn't bring back the old '{name}': {e}."
        except ValueError:
            return False, f"Couldn't bring back the old '{name}': that backup record doesn't look right, so I didn't touch anything."
        return True, f"Put the previous version of '{name}' back."

    # ---- public ----
    def undo(self, action_id: int, source: str = "ui") -> UndoResult:
        with self._lock:                         # a double-click must not run the recipe twice
            rec = self.audit.get(action_id)
            if rec is None:
                return UndoResult(False, "I can't find that action any more.", action_id)
            if rec.undone_at:
                return UndoResult(False, "That one was already undone.", action_id)
            if not rec.undo_kind:
                return UndoResult(False, "That action can't be undone.", action_id)
            handler = self._handlers.get(rec.undo_kind)
            if handler is None:
                return UndoResult(False, "I don't know how to undo that kind of action.", action_id)

            with self.audit.action("undo", {"of": rec.id, "target": rec.summary()}, source=source) as act:
                try:
                    ok, message = handler(rec.undo_payload)
                except Exception as e:
                    logger.exception("Undo handler crashed")
                    ok, message = False, f"Couldn't undo that: {e}"
                if ok:
                    self.audit.mark_undone(rec.id)
                    act.ok(message)
                else:
                    act.fail(message)
            return UndoResult(ok, message, action_id)

    def undo_last(self, source: str = "ai") -> UndoResult:
        rec = self.audit.last_reversible()
        if rec is None:
            return UndoResult(False, "There's nothing I can undo right now.")
        result = self.undo(rec.id, source=source)
        if result.ok:
            result.message = f"Undid '{rec.summary()}'. {result.message}"
        return result


def startup_maintenance():
    """Purge expired trash and warm the audit cache. Safe to run on any thread."""
    try:
        purged = trash.purge_expired()
        if purged:
            logger.info(f"Purged {purged} expired item(s) from Wave's trash.")
    except Exception:
        logger.exception("Trash maintenance failed")
    try:
        audit_mod.audit.warm()
    except Exception:
        logger.exception("Audit warm-up failed")


def trash_enabled() -> bool:
    return bool(config.UNDO_TRASH_ENABLED)


trash = TrashStore(config.TRASH_DIR, config.TRASH_RETENTION_DAYS, config.UNDO_MAX_FILE_MB * 1024 * 1024)
undo_manager = UndoManager(audit_mod.audit, trash)
