"""
automation/notes_rag.py — Phase 6: "mere notes se batao" (search_my_notes).

Before this, Wave could only answer from facts it had been explicitly told
out loud (remember_this/recall_memory) - it had no way to read the user's
OWN files and answer from them. This indexes .txt/.md/.pdf files from the
same common folders brain/automation/file_manager.py's search_file already
looks in (Desktop, Documents, Downloads), plus an optional WAVE_NOTES_DIR
override, chunks each file, and embeds each chunk with the exact same
optional sentence-transformers pipeline semantic memory already uses (see
database/embeddings.py) - so this degrades the same graceful way the rest
of the app does when that package isn't installed: search still works via
a plain keyword fallback, just without semantic ranking.

NotesIndex is a small piece of real state (an in-process lock, nothing
else - everything persisted lives in the note_chunks table via
database/db_manager.py), so unlike CalendarManager this is a singleton
instance (`notes_index`, at the bottom of this file) rather than a
classmethod-only class - same shape as database.db_manager's `db` and
automation.audit's `audit`.
"""
import logging
import os
import threading
import time

from automation.file_manager import FileManager
from database import embeddings
from database.db_manager import db
from settings.config import config

logger = logging.getLogger("NotesIndex")

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

_NOTE_EXTENSIONS = (".txt", ".md", ".markdown")
_SKIP_DIR_NAMES = {".git", "__pycache__", "node_modules", "$RECYCLE.BIN", "System Volume Information"}
_MAX_FILES_PER_RUN = 500  # safety cap so a huge Downloads folder can't make reindex_all() run forever
_MAX_WALK_DEPTH = 4


def _search_dirs() -> list:
    """Desktop, Documents, Downloads - the exact same common folders
    search_file already searches - plus WAVE_NOTES_DIR if the user's set
    one. Reuses FileManager.get_default_path() for Desktop instead of
    hand-resolving it a second way."""
    home = os.path.expanduser("~")
    dirs = [FileManager.get_default_path(), os.path.join(home, "Documents"), os.path.join(home, "Downloads")]
    if config.NOTES_DIR and os.path.isdir(config.NOTES_DIR):
        dirs.append(config.NOTES_DIR)
    seen, unique = set(), []
    for d in dirs:
        real = os.path.realpath(d)
        if os.path.isdir(d) and real not in seen:
            seen.add(real)
            unique.append(d)
    return unique


def _iter_note_files():
    """Every indexable file under the search dirs - .txt/.md always, plus
    .pdf when pypdf is available - skipping junk directories and files over
    NOTES_MAX_FILE_MB, capped at _MAX_FILES_PER_RUN so one huge folder can't
    turn a reindex into an unbounded scan."""
    extensions = _NOTE_EXTENSIONS + ((".pdf",) if HAS_PYPDF else ())
    max_bytes = config.NOTES_MAX_FILE_MB * 1024 * 1024
    count = 0
    for base in _search_dirs():
        base_depth = base.rstrip(os.sep).count(os.sep)
        for root, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
            if root.count(os.sep) - base_depth >= _MAX_WALK_DEPTH:
                dirnames[:] = []
            for name in filenames:
                if count >= _MAX_FILES_PER_RUN:
                    return
                if not name.lower().endswith(extensions):
                    continue
                path = os.path.join(root, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_size > max_bytes or stat.st_size == 0:
                    continue
                count += 1
                yield path, stat.st_mtime, stat.st_size


def _read_file_text(path: str) -> str:
    """Plain text for a .txt/.md/.pdf file, or '' if it can't be read -
    never raises, so one unreadable/corrupt file can't abort a whole
    reindex run."""
    lower = path.lower()
    try:
        if lower.endswith(".pdf"):
            if not HAS_PYPDF:
                return ""
            reader = PdfReader(path)
            pages = reader.pages[:config.NOTES_MAX_PDF_PAGES]
            return "\n".join((p.extract_text() or "") for p in pages)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        logger.warning(f"Couldn't read '{path}' for indexing: {e}")
        return ""


def _chunk_text(text: str, chunk_chars: int = None, overlap: int = None) -> list:
    """Splits `text` into ~chunk_chars-sized pieces with `overlap`
    characters of repeat between consecutive chunks (so a fact sitting
    right on a chunk boundary is still fully present in at least one
    chunk). Prefers to break on a paragraph boundary near the target size
    rather than a hard mid-word cut, for more coherent, embeddable chunks."""
    chunk_chars = chunk_chars or config.NOTES_CHUNK_CHARS
    overlap = overlap if overlap is not None else config.NOTES_CHUNK_OVERLAP
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        if end < n:
            break_at = text.rfind("\n\n", start + int(chunk_chars * 0.5), end)
            if break_at == -1:
                break_at = text.rfind(". ", start + int(chunk_chars * 0.5), end)
            if break_at != -1:
                end = break_at + 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)  # +1 guarantees forward progress even if overlap >= chunk size
    return chunks


class NotesIndex:
    def __init__(self):
        self._lock = threading.Lock()
        self._last_reindex_summary = "Notes haven't been indexed yet."

    def reindex_all(self) -> str:
        """Walks every search dir, (re)indexes any file that's new or
        changed since it was last indexed (by mtime+size signature - same
        trick brain/skill_loader.py uses), and drops chunks for files that
        no longer exist. Safe to call anytime (startup, or the explicit
        reindex_notes tool) - only touched files actually get re-embedded,
        so a no-op run over an unchanged folder is cheap."""
        if not config.NOTES_RAG_ENABLED:
            return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
        if not self._lock.acquire(blocking=False):
            return "Already reindexing notes - try again in a moment."
        try:
            t0 = time.monotonic()
            seen_paths = set()
            indexed, skipped, failed = 0, 0, 0

            for path, mtime, size in _iter_note_files():
                seen_paths.add(path)
                existing = db.get_note_file_signature(path)
                if existing and existing[0] == mtime and existing[1] == size:
                    skipped += 1
                    continue

                text = _read_file_text(path)
                if not text.strip():
                    failed += 1
                    continue
                chunks = [(c, embeddings.serialize(embeddings.embed(c))) for c in _chunk_text(text)]
                if db.replace_note_chunks(path, mtime, size, chunks):
                    indexed += 1
                else:
                    failed += 1

            # Files that used to be indexed but weren't seen this run have
            # been deleted/moved - drop their stale chunks.
            for old_path in db.indexed_note_files():
                if old_path not in seen_paths:
                    db.remove_note_file(old_path)

            elapsed = time.monotonic() - t0
            summary = (f"Indexed {indexed} file(s), {skipped} unchanged, {failed} skipped/failed "
                      f"({elapsed:.1f}s).")
            self._last_reindex_summary = summary
            logger.info(f"Notes reindex: {summary}")
            return summary
        except Exception as e:
            logger.error(f"Notes reindex failed: {e}")
            return f"Reindexing notes failed: {e}"
        finally:
            self._lock.release()

    def search(self, question: str, top_k: int = 5) -> str:
        """Semantic search first (if embeddings are available), falling
        back to a plain keyword match - formatted as a tool result the
        model can quote/summarize from, grouped by which file each snippet
        came from so the answer can say where it found it."""
        if not config.NOTES_RAG_ENABLED:
            return "Notes search is turned off (NOTES_RAG_ENABLED=false)."
        question = (question or "").strip()
        if not question:
            return "What would you like me to look for in your notes?"

        results = db.semantic_search_notes(question, top_k=top_k)
        used_semantic = bool(results)
        if not results:
            results = [(fp, idx, content, None) for fp, idx, content in db.keyword_search_notes(question, top_k=top_k)]

        if not results:
            if not db.indexed_note_files():
                return ("I haven't indexed any notes yet - make sure there are .txt/.md/.pdf files in your "
                       "Desktop, Documents, or Downloads folder, then ask me to reindex your notes.")
            return f"Nothing in your indexed notes matches '{question}'."

        # Tier 20: a note file is usually the user's own words, but it's
        # still a FILE on disk - something else (another app, a shared
        # download, an old attachment someone dropped into Documents)
        # could have planted a prompt-injection-shaped payload in one.
        # Scan each hit individually and drop only the flagged ones,
        # rather than treating the whole notes corpus as untrusted - most
        # people's notes are entirely their own writing, and wrapping
        # every search in content_trust.wrap_untrusted() would taint the
        # turn on every single notes lookup for no reason.
        from brain import content_trust
        from automation import security_log
        filtered_count = 0
        lines = []
        for row in results:
            file_path = row[0]
            content = row[2]
            if content_trust.scan(content).flagged:
                filtered_count += 1
                security_log.record(
                    "rag_filter",
                    reason=f"Left out a note excerpt from '{os.path.basename(file_path)}' - it "
                           f"scanned as flagged (prompt-injection-shaped) content.",
                )
                continue
            snippet = content if len(content) <= 300 else content[:300].rsplit(" ", 1)[0] + "..."
            lines.append(f"[{os.path.basename(file_path)}] {snippet}")

        if not lines:
            return (f"Found {filtered_count} match(es) for '{question}', but left all of them out - "
                    f"they scanned as flagged (prompt-injection-shaped) content.") if filtered_count else \
                   f"Nothing in your indexed notes matches '{question}'."

        method = "semantically" if used_semantic else "by keyword"
        header = f"Found {len(lines)} relevant note excerpt(s) ({method} matched)"
        if filtered_count:
            header += f" - left out {filtered_count} more that scanned as flagged content"
        return header + ":\n" + "\n".join(lines)


notes_index = NotesIndex()
