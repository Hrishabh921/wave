import os
import shutil
import logging
from pathlib import Path

from automation import audit as audit_mod
from automation import undo as undo_mod

logger = logging.getLogger("FileManager")

class FileManager:
    """Handles file system operations (Create, Delete, Search).

    Phase 4: every create/delete is written to the action audit log
    (automation/audit.py) and made undoable - a deleted file goes to Wave's own
    recoverable trash instead of being destroyed, and a file that's about to be
    replaced is backed up first (automation/undo.py). Everything that was true
    before is unchanged: names are sanitised, only plain files are ever deleted,
    and nothing recurses into folders.
    """

    @staticmethod
    def get_default_path() -> str:
        """Returns the current user's desktop path as default."""
        return os.path.join(os.path.expanduser("~"), "Desktop")

    @staticmethod
    def _sanitize_name(name: str) -> str:
        """🔧 FIX: strips path separators/drive letters so a name like
        'C:\\Windows\\System32\\evil.txt' or '../../secrets.txt' can't
        escape the intended folder - os.path.join() would otherwise
        happily follow an absolute/relative-parent path straight past
        the `path` argument entirely. Handles both '/' and '\\' regardless
        of which OS this happens to run on."""
        name = (name or "").strip()
        name = name.replace("\\", "/").split("/")[-1]
        if len(name) == 2 and name[1] == ":":  # a bare leftover drive letter, e.g. "C:"
            name = "untitled"
        return name.strip() or "untitled"

    @classmethod
    def create_folder(cls, folder_name: str, path: str = None) -> str:
        """Creates a new directory."""
        if not path:
            path = cls.get_default_path()

        safe_name = cls._sanitize_name(folder_name)
        full_path = os.path.join(path, safe_name)
        with audit_mod.audit.action("create_folder", {"folder_name": safe_name, "path": path}) as act:
            try:
                existed = os.path.isdir(full_path)
                os.makedirs(full_path, exist_ok=True)
                if existed:
                    return act.ok(f"Folder '{safe_name}' already exists at {path}.")
                act.set_undo("remove_created_folder", {"target": os.path.abspath(full_path)},
                             f"Remove the folder '{safe_name}'")
                return act.ok(f"Folder '{safe_name}' created at {path}.")
            except Exception as e:
                logger.error(f"Error creating folder: {e}")
                return act.fail(f"Failed to create folder: {str(e)}")

    @classmethod
    def create_file(cls, file_name: str, content: str = "", path: str = None) -> str:
        """Creates a new file with optional content. A file that's already there
        is replaced (as it always was) - but its old version is backed up first,
        so the replacement can be undone."""
        if not path:
            path = cls.get_default_path()

        safe_name = cls._sanitize_name(file_name)
        full_path = os.path.join(path, safe_name)
        with audit_mod.audit.action("create_file", {"file_name": safe_name, "path": path}) as act:
            try:
                existed = os.path.isfile(full_path)
                backup = None
                if existed and undo_mod.trash_enabled():
                    backup = undo_mod.trash.stash(full_path, copy=True)   # None if too big to keep
                with open(full_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                abs_path = os.path.abspath(full_path)
                if not existed:
                    act.set_undo("remove_created_file", {"target": abs_path}, f"Remove the file '{safe_name}'")
                    return act.ok(f"File '{safe_name}' created successfully.")
                if backup is not None:
                    act.set_undo("restore_overwritten", {"target": abs_path, "backup": backup.trash_path},
                                 f"Bring back the previous '{safe_name}'")
                    return act.ok(f"File '{safe_name}' already existed, so I replaced it "
                                  f"(the old version is kept - say \"undo\" to bring it back).")
                return act.ok(f"File '{safe_name}' already existed and has been replaced "
                              f"(no backup was kept, so this can't be undone).")
            except Exception as e:
                logger.error(f"Error creating file: {e}")
                return act.fail(f"Failed to create file: {str(e)}")

    @classmethod
    def delete_file(cls, file_name: str, path: str = None) -> str:
        """Deletes a single named file. Deliberately narrow: only ever
        removes one file it can positively identify inside the target
        folder, and refuses anything that isn't a plain file - a voice- or
        LLM-triggered delete is too easy to get slightly wrong (misheard
        name, wrong folder) to risk it recursing into a whole directory."""
        if not path:
            path = cls.get_default_path()

        safe_name = cls._sanitize_name(file_name)
        full_path = os.path.join(path, safe_name)
        with audit_mod.audit.action("delete_file", {"file_name": safe_name, "path": path}) as act:
            try:
                if not os.path.exists(full_path):
                    return act.fail(f"I couldn't find a file named '{safe_name}' in {path}.")
                if not os.path.isfile(full_path):
                    return act.blocked(f"'{safe_name}' is a folder, not a file - I won't delete folders this way.")
                if os.path.islink(full_path) or not undo_mod.trash_enabled():
                    os.remove(full_path)    # a link (only the link goes) or undo switched off in .env
                    return act.ok(f"Deleted '{safe_name}' from {path}.")
                stashed = undo_mod.trash.stash(full_path)      # moves it; None if too big to keep
                if stashed is None:
                    os.remove(full_path)
                    return act.ok(f"Deleted '{safe_name}' from {path}. It was too big to keep a recovery "
                                  f"copy, so this can't be undone.")
                act.set_undo("restore_from_trash",
                             {"trash": stashed.trash_path, "original": os.path.abspath(full_path)},
                             f"Restore '{safe_name}'")
                return act.ok(f"Deleted '{safe_name}' from {path}. (Say \"undo\" to bring it back.)")
            except Exception as e:
                logger.error(f"Error deleting file: {e}")
                return act.fail(f"Failed to delete file: {str(e)}")

    @classmethod
    def search_file(cls, file_name: str, path: str = None) -> str:
        """Looks for a file by partial, case-insensitive name. Searches the
        given folder if one is passed, otherwise the common places people
        actually save things (Desktop, Documents, Downloads) - each just
        one level deep, not a full-disk crawl."""
        query = (file_name or "").strip().lower()
        if not query:
            return "Please tell me a file name to search for."

        search_dirs = [path] if path else [
            cls.get_default_path(),
            os.path.join(os.path.expanduser("~"), "Documents"),
            os.path.join(os.path.expanduser("~"), "Downloads"),
        ]

        matches = []
        for d in search_dirs:
            if not d or not os.path.isdir(d):
                continue
            try:
                for entry in os.listdir(d):
                    if query in entry.lower() and os.path.isfile(os.path.join(d, entry)):
                        matches.append(os.path.join(d, entry))
            except Exception as e:
                logger.error(f"Error searching {d}: {e}")

        if not matches:
            return f"I couldn't find any file matching '{file_name}'."
        if len(matches) == 1:
            return f"Found it: {matches[0]}"
        shown = "; ".join(matches[:5])
        more = f" (+{len(matches) - 5} more)" if len(matches) > 5 else ""
        return f"Found {len(matches)} matches: {shown}{more}"