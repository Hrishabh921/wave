import json
import sqlite3
import logging
from datetime import datetime, timedelta
from settings.config import config
from database import embeddings
from brain import memory_time

logger = logging.getLogger("DBManager")

class DatabaseManager:
    """Manages SQLite database operations for Wave's memory."""
    
    def __init__(self):
        self.db_path = config.DB_PATH
        # 🔧 FIX: Settings > Privacy > "Save chat history locally" used to be
        # purely decorative - save_message() below now actually checks this.
        self.history_enabled = True
        # Rolling copy of this session's messages, RAM only - feeds the AI's short-term
        # context when saving to disk is switched off in Settings > Privacy.
        self._session_buffer = []
        self.init_db()

    def get_connection(self) -> sqlite3.Connection:
        """Returns a database connection."""
        return sqlite3.connect(self.db_path)

    def init_db(self):
        """Initializes the database schema for history, preferences, and workflows."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # Chat History
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        role TEXT NOT NULL,
                        message TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # User Preferences & Memory
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS memory (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                ''')

                # 🔧 Phase 3: semantic memory. A nullable column added onto
                # the existing key/value table rather than a new one, so every
                # row keeps working exactly as before whether or not it has an
                # embedding yet (older rows, or rows saved while embeddings
                # were unavailable, just have embedding = NULL). Safe to run
                # every startup: SQLite has no 'ADD COLUMN IF NOT EXISTS', so
                # the expected "duplicate column" error on every run after the
                # first is caught and ignored; anything else is logged.
                try:
                    cursor.execute('ALTER TABLE memory ADD COLUMN embedding TEXT')
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e).lower():
                        logger.error(f'memory.embedding migration failed: {e}')

                # Tier 20: provenance on each remembered fact - same
                # additive-column, swallow-"duplicate column" pattern as
                # memory.embedding above. `source` is 'explicit' (the user
                # directly said "remember X") or 'auto_consolidated'
                # (brain/memory_consolidation.py pulled it out of ordinary
                # conversation on its own) - NULL on any row saved before
                # this migration, since there's no honest way to know which
                # one it was after the fact. `turn_id` reuses
                # telemetry.trace.current_turn_id() (the same id Tier 19's
                # log-line correlation already tags everything with) so a
                # fact can be traced back to the exact turn/log lines that
                # produced it. `updated_at` is set on every save (insert OR
                # supersede), not just the first one - see save_memory()'s
                # own comment on why a supersede counts as an update.
                for _col, _ddl in (
                    ("source", "ALTER TABLE memory ADD COLUMN source TEXT"),
                    ("turn_id", "ALTER TABLE memory ADD COLUMN turn_id TEXT"),
                    ("updated_at", "ALTER TABLE memory ADD COLUMN updated_at TEXT"),
                ):
                    try:
                        cursor.execute(_ddl)
                    except sqlite3.OperationalError as e:
                        if 'duplicate column' not in str(e).lower():
                            logger.error(f'memory.{_col} migration failed: {e}')

                # Tier 35 (Memory 2.0): what a fact used to be, and the facts
                # the user asked Wave to forget - so a change keeps its past
                # ("lived in Jaipur, now Delhi") and a forgotten fact isn't
                # picked up again from old conversation.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS memory_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        key TEXT NOT NULL,
                        old_value TEXT NOT NULL,
                        new_value TEXT NOT NULL,
                        source TEXT,
                        changed_at TEXT NOT NULL
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS memory_forgotten (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        forgotten_at TEXT NOT NULL
                    )
                ''')

                # Workflows / Automation
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS workflows (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        actions TEXT NOT NULL
                    )
                ''')

                # Reminders / Alarms
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS reminders (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message TEXT NOT NULL,
                        due_at TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        triggered INTEGER NOT NULL DEFAULT 0
                    )
                ''')

                # 🔧 Tier 3 / item 8 ("reminders deepening"): five additive
                # columns, same ALTER-and-swallow-duplicate-column pattern as
                # memory.embedding above - safe on every startup, never
                # touches an existing row.
                #   recurrence        - NULL (one-off, the only kind before
                #                       this) or 'daily'. A daily reminder
                #                       reschedules itself instead of ending -
                #                       see complete_or_reschedule_reminder().
                #   notified_upcoming - whether the heads-up ("coming up in
                #                       N minutes") notice already went out
                #                       for this occurrence.
                #   followup_at       - when to gently check in after a
                #                       reminder fires, for categories where
                #                       that makes sense (sleep, study, break
                #                       - see reminders/context.py's
                #                       effective_policy). NULL = no follow-up.
                #   followup_sent     - whether that follow-up already went out.
                #   delivered_at      - when the reminder actually fired, used
                #                       to say "N minutes ago" in follow-ups.
                for _col, _ddl in (
                    ("recurrence", "ALTER TABLE reminders ADD COLUMN recurrence TEXT"),
                    ("notified_upcoming", "ALTER TABLE reminders ADD COLUMN notified_upcoming INTEGER NOT NULL DEFAULT 0"),
                    ("followup_at", "ALTER TABLE reminders ADD COLUMN followup_at TEXT"),
                    ("followup_sent", "ALTER TABLE reminders ADD COLUMN followup_sent INTEGER NOT NULL DEFAULT 0"),
                    ("delivered_at", "ALTER TABLE reminders ADD COLUMN delivered_at TEXT"),
                ):
                    try:
                        cursor.execute(_ddl)
                    except sqlite3.OperationalError as e:
                        if 'duplicate column' not in str(e).lower():
                            logger.error(f'reminders.{_col} migration failed: {e}')

                # Tier 5: a single-row stopwatch, separate from reminders/timers
                # (which live in the `reminders` table above). One stopwatch at
                # a time is all a voice assistant ever needs, so this is a
                # single CHECKed row rather than a table of many - laps are a
                # JSON array of elapsed-seconds floats, same "small JSON blob in
                # a TEXT column" pattern workflows.actions already uses.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS stopwatch (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        started_at TEXT,
                        stopped_at TEXT,
                        laps TEXT NOT NULL DEFAULT '[]'
                    )
                ''')
                cursor.execute("INSERT OR IGNORE INTO stopwatch (id, laps) VALUES (1, '[]')")

                # Phase 4: audit trail of what Wave actually did, plus the
                # recipe needed to undo it where that's possible. Written by
                # automation/audit.py (which keeps a RAM copy and only
                # persists here while Settings > Privacy > "Save chat history
                # locally" is on).
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS action_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        args TEXT,
                        result TEXT,
                        status TEXT NOT NULL,
                        source TEXT,
                        undo_kind TEXT,
                        undo_payload TEXT,
                        undo_label TEXT,
                        undone_at TEXT
                    )
                ''')

                # Phase 6: local calendar (brain/groq_core.py's
                # create_calendar_event/list_calendar_events/cancel_calendar_event
                # tools, plus the proactive engine's upcoming-event nudge).
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS calendar_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        title TEXT NOT NULL,
                        start_at TEXT NOT NULL,
                        end_at TEXT,
                        location TEXT,
                        notes TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # 🔧 Tier 8 (calendar audit, done against this file's own
                # reminders table rather than SARA - no SARA source was
                # available to compare against, so the fairest honest
                # standard was Wave's own more mature reminders feature;
                # see Readme.MD's Tier 8 section for the full methodology
                # note). One additive column, same ALTER-and-swallow
                # pattern as reminders.recurrence above:
                #   recurrence - NULL (one-off, the only kind before this)
                #                or 'daily'/'weekly'. Unlike
                #                reminders.due_at (which
                #                complete_or_reschedule_reminder mutates
                #                forward each time a *poll* fires it - see
                #                that method's docstring), this column
                #                never changes the stored row: calendar
                #                events are read via range queries
                #                (list_calendar_events's "today"/"this
                #                week"/...) with no equivalent always-on
                #                poll driving that core use case, so
                #                get_calendar_events_between() expands
                #                recurrence into virtual occurrences at
                #                QUERY TIME instead - see its docstring.
                #                'weekly' has no reminders equivalent
                #                (reminders only ever supports 'daily') -
                #                a deliberate, flagged exception to the
                #                reminders-parity methodology, since a
                #                calendar with no weekly-recurring event
                #                support would leave the single most
                #                common calendar use case (a weekly
                #                standup/1:1) unmet.
                try:
                    cursor.execute("ALTER TABLE calendar_events ADD COLUMN recurrence TEXT")
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e).lower():
                        logger.error(f'calendar_events.recurrence migration failed: {e}')

                # Tier 18: calendar sync (Google/ICS). source is NULL for an
                # event the user created locally (unchanged, existing
                # behavior) or 'google'/'ics:<feed name>' for one pulled in
                # from a synced calendar; external_id is that source's own
                # stable id for the event (Google's event id, or an ICS
                # VEVENT's UID), used to dedup on re-sync - see
                # upsert_synced_calendar_event() below. Both are plain
                # ADD COLUMN migrations, same swallow-duplicate-column
                # pattern as recurrence just above. Deliberately NOT added
                # to get_calendar_events_between()'s existing SELECT/the
                # (id, title, start_at, end_at, location, notes, recurrence)
                # row shape every caller of that method already unpacks
                # (CalendarManager._format_event_line, the proactive
                # engine's dedup key, daily_briefing) - they're internal
                # sync bookkeeping, never needed for display, so touching
                # that shape would be a wider, riskier change for no
                # benefit. A synced event is otherwise an ordinary row:
                # existing queries, the proactive engine's upcoming-event
                # nudge, and get_events_for_prompt() all already pick it up
                # for free.
                try:
                    cursor.execute("ALTER TABLE calendar_events ADD COLUMN source TEXT")
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e).lower():
                        logger.error(f'calendar_events.source migration failed: {e}')
                try:
                    cursor.execute("ALTER TABLE calendar_events ADD COLUMN external_id TEXT")
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e).lower():
                        logger.error(f'calendar_events.external_id migration failed: {e}')

                # Tier 18: registered calendar sources to re-sync later -
                # "add this ICS feed" shouldn't mean re-typing the URL every
                # time the user wants fresh events. kind is 'ics' or
                # 'google'; url is the feed address for 'ics' and NULL for
                # 'google' (that one's "address" is credentials.json plus
                # the OAuth token cache, not a URL - see
                # automation/calendar_sync.py). name is UNIQUE so
                # add_ics_calendar_feed re-adding the same name updates
                # the URL in place rather than creating a second row.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS calendar_feeds (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        kind TEXT NOT NULL,
                        url TEXT,
                        last_synced_at TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Tier 18: time-triggered workflows - a workflow (see the
                # workflows table above) that should run on its own, on a
                # schedule, rather than only ever by voice/chat/the UI's
                # Run Now button. Persisted (not RAM-only, unlike e.g. the
                # proactive engine's download watch) specifically so a
                # schedule SURVIVES a restart - automation/workflow_scheduler.py
                # owns the actual due/advance logic; this is plain storage,
                # same CalendarManager/StreakManager split as everywhere
                # else in this file. next_run_at is the one field that
                # changes after creation (advanced forward on each run for
                # a repeating schedule; left alone and enabled flipped to 0
                # for a 'once' schedule) - everything else describes the
                # schedule itself. workflow_name is NOT a foreign key (the
                # workflows table has no id this could reference cleanly
                # pre-Tier-18, and a schedule for a since-deleted workflow
                # should just fail loudly and specifically when it's due,
                # not be silently cascade-deleted) - get_due_workflow_schedules()'s
                # caller is expected to handle a missing workflow by name.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS workflow_schedules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        workflow_name TEXT NOT NULL,
                        time_text TEXT NOT NULL,
                        repeat TEXT NOT NULL DEFAULT 'once',
                        next_run_at TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        last_run_at TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Phase 6: chunked + (optionally) embedded text from the user's
                # own files, for the search_my_notes tool (automation/notes_rag.py).
                # file_mtime/file_size are the re-index signature - the same
                # (mtime, size) trick brain/skill_loader.py uses to tell whether
                # a file needs re-reading without hashing its whole content.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS note_chunks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        file_path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        embedding TEXT,
                        file_mtime REAL NOT NULL,
                        file_size INTEGER NOT NULL,
                        indexed_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Tier 4 item #13: one row per finished think()/think_stream()
                # turn (telemetry/trace.py in-memory-tracks a turn's stage
                # marks, then hands the finished summary to telemetry/store.py,
                # which batches rows in here on its own writer thread - see
                # that module for why this stays off think()'s hot path).
                # stages/meta are JSON text, same convention as action_log's
                # args/result columns - no new column type precedent needed.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS turn_trace (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        turn_id TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        source TEXT NOT NULL,
                        route TEXT,
                        outcome TEXT NOT NULL,
                        model TEXT,
                        provider TEXT,
                        rounds INTEGER,
                        tools_called INTEGER,
                        total_ms REAL,
                        llm_ms REAL,
                        tool_ms REAL,
                        stages TEXT,
                        meta TEXT,
                        error TEXT
                    )
                ''')
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_turn_trace_started_at ON turn_trace(started_at)"
                )

                # 🔧 Tier 8: daily check-in streaks ("I meditated today",
                # "did my workout") - automation/streak_manager.py owns the
                # day-boundary logic (same/consecutive/missed day); this
                # table is plain storage, one row per habit, same split as
                # calendar_events (CalendarManager) vs this file. habit is
                # UNIQUE - one streak per habit name, upserted by
                # save_streak() below rather than ever accumulating
                # duplicate rows for the same habit.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS streaks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        habit TEXT NOT NULL UNIQUE,
                        current_streak INTEGER NOT NULL DEFAULT 0,
                        best_streak INTEGER NOT NULL DEFAULT 0,
                        last_checkin_date TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Tier 16: quick notes ("take a note: ...") - a lightweight,
                # DB-backed scratchpad, deliberately separate from
                # automation/notes_rag.py's note_chunks table above: that one
                # INDEXES real .txt/.md/.pdf files already on disk for
                # semantic search; this one IS the user's note, dictated
                # straight into Wave with nowhere else it lives. No habit-
                # style UNIQUE constraint - taking the same note twice is a
                # normal, unremarkable thing (same reasoning get_joke's
                # module docstring gives for not deduplicating jokes).
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS quick_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        content TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                # Tier 16: to-dos - add/list/complete/delete, with an
                # optional free-text due date. due_text is stored AS THE
                # USER SAID IT (e.g. "tomorrow at 5pm"), not a parsed
                # datetime - the reminder created alongside it (see
                # automation/todo_manager.py) is what actually gets parsed
                # and fires; this column is just for display in list_todos.
                # completed_at is NULL until complete_todo() sets it.
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS todos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        text TEXT NOT NULL,
                        due_text TEXT,
                        completed INTEGER NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        completed_at DATETIME
                    )
                ''')

                conn.commit()
                logger.info("Database schemas initialized successfully.")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")

    def save_message(self, role: str, message: str):
        """Saves a chat message to history - unless the user has turned
        this off in Settings > Privacy, in which case it only lives in RAM
        for the current session (see _session_buffer)."""
        self._session_buffer.append((role, message))
        del self._session_buffer[:-40]  # only ever keep the last 40
        if not self.history_enabled:
            return
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO chat_history (role, message) VALUES (?, ?)", 
                    (role, message)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to save message: {e}")

    def get_chat_history(self, limit: int = 10) -> list:
        """Retrieves recent chat history for AI context."""
        if not self.history_enabled:
            # "Don't save my chats to disk" shouldn't also mean "forget what
            # I said ten seconds ago" - serve the current session from RAM.
            return self._session_buffer[-limit:]
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, message FROM chat_history ORDER BY id DESC LIMIT ?", 
                    (limit,)
                )
                # Reverse to get chronological order
                return cursor.fetchall()[::-1]
        except Exception as e:
            logger.error(f"Failed to fetch history: {e}")
            return []

    def clear_all(self) -> bool:
        """🔧 FIX: Settings > Privacy > 'Clear All Local Data' was already
        calling self.clear_all() before this fix existed - since the method
        didn't exist, that call raised AttributeError every time, which was
        being silently swallowed, so the button always told the user their
        data was cleared when nothing had actually been deleted. Wipes chat
        history, remembered preferences, and workflows. Returns whether it
        actually succeeded, so the caller can show an honest result."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM chat_history")
                cursor.execute("DELETE FROM memory")
                cursor.execute("DELETE FROM workflows")
                cursor.execute("DELETE FROM reminders")
                cursor.execute("DELETE FROM action_log")
                cursor.execute("DELETE FROM calendar_events")
                cursor.execute("DELETE FROM note_chunks")
                conn.commit()
            self._session_buffer.clear()
            logger.info("All local data cleared.")
            return True
        except Exception as e:
            logger.error(f"Failed to clear all data: {e}")
            return False

    # ---- Memory: key-value facts/preferences ----
    # 🔧 FIX: this table existed in the schema from the very start but
    # nothing anywhere in the codebase ever read or wrote to it - Wave had
    # no way to actually remember anything about you across sessions. These
    # four methods, plus the remember_this/recall_memory/forget_memory tools
    # in brain/groq_core.py, are what actually wire it up.
    # Tier 20: a new fact's embedding needs to be at least this similar to
    # an EXISTING, different key's own "key: value" embedding before
    # save_memory() will treat them as the same fact and supersede that
    # key instead of inserting a new one beside it. High on purpose - 0.92
    # is "this is obviously the same fact, reworded" (e.g. "favorite
    # colour"="blue" vs "fav_color"="blue"), not merely "a related topic";
    # a looser threshold would risk silently overwriting two genuinely
    # different facts that just happen to use similar words.
    MEMORY_SUPERSEDE_SIMILARITY = 0.92

    def save_memory(self, key: str, value: str, source: str = None, turn_id: str = None) -> bool:
        """`source` ('explicit' | 'auto_consolidated') and `turn_id` are
        Tier 20 provenance - optional so every existing call site (and
        every test) keeps working unchanged; a caller that cares passes
        them (see brain/groq_core.py's remember_this skill and
        brain/memory_consolidation.py's auto-save path).

        Tier 20 also adds semantic supersede: if `key` isn't ALREADY an
        exact match for an existing row, but its embedding is >=
        MEMORY_SUPERSEDE_SIMILARITY similar to a DIFFERENT existing key's
        own embedding, that existing key is updated in place instead of a
        near-duplicate fact piling up beside it under a new key. An exact
        key match (the common case - saving the same key again) always
        wins over this and is never affected by it."""
        clean_key = key.strip().lower()
        # Tier 35: "kal" / "next monday" in a fact get the date they meant
        # today (brain/memory_time.py), so the fact stays true next week.
        value = memory_time.annotate(value)
        # Tier 35: a fact the user asked Wave to forget isn't learned again
        # from old conversation; telling Wave again on purpose clears that.
        if source == "auto_consolidated" and clean_key in self.get_forgotten_keys():
            logger.info(f"save_memory: not re-learning '{clean_key}' - the user asked to forget it.")
            return False
        # 🔧 Phase 3: embed "key: value" (not just the value alone) so a
        # query like "what's my favorite color" can match on the key's own
        # wording too, not only whatever value happens to be stored under it.
        # None (embeddings unavailable, or this environment hasn't got the
        # package installed) just means the embedding column stays NULL for
        # this row - everything else about saving still works unchanged.
        embedding_vec = embeddings.embed(f"{clean_key}: {value}")
        embedding_json = embeddings.serialize(embedding_vec)
        now_iso = datetime.now().isoformat()
        target_key = clean_key
        try:
            if embedding_vec is not None and self.get_memory(clean_key) is None:
                hits = self.semantic_recall(f"{clean_key}: {value}", top_k=1,
                                             min_similarity=self.MEMORY_SUPERSEDE_SIMILARITY)
                if hits:
                    target_key = hits[0][0]
                    logger.info(f"save_memory: superseding existing key '{target_key}' "
                                f"(semantically same as new key '{clean_key}', similarity {hits[0][2]:.3f})")
            if source == "auto_consolidated" and target_key in self.get_forgotten_keys():
                logger.info(f"save_memory: not re-learning '{target_key}' - the user asked to forget it.")
                return False
            previous = self.get_memory(target_key)
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if previous is not None and previous.strip().lower() != str(value).strip().lower():
                    cursor.execute(
                        "INSERT INTO memory_history (key, old_value, new_value, source, changed_at) "
                        "VALUES (?, ?, ?, ?, ?)", (target_key, previous, value, source, now_iso))
                if source != "auto_consolidated":
                    cursor.execute("DELETE FROM memory_forgotten WHERE key = ?", (target_key,))
                cursor.execute(
                    "INSERT INTO memory (key, value, embedding, source, turn_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, embedding=excluded.embedding, "
                    "source=excluded.source, turn_id=excluded.turn_id, updated_at=excluded.updated_at",
                    (target_key, value, embedding_json, source, turn_id, now_iso)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save memory '{key}': {e}")
            return False

    def get_memory(self, key: str):
        """Returns the remembered value for this key, or None if nothing's
        been saved under it."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM memory WHERE key = ?", (key.strip().lower(),))
                row = cursor.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"Failed to get memory '{key}': {e}")
            return None

    def get_memory_provenance(self, key: str):
        """Tier 20: {key, value, source, turn_id, updated_at} for one
        remembered fact, or None if it doesn't exist - the data behind
        "why do you remember that?" (brain/handlers/memory.py). source/
        turn_id/updated_at are all None for any row saved before this
        migration, or for a call site that didn't pass them - that's
        itself an honest answer ("I don't have provenance for that one"),
        not an error."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT key, value, source, turn_id, updated_at FROM memory WHERE key = ?",
                    (key.strip().lower(),)
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return {"key": row[0], "value": row[1], "source": row[2],
                        "turn_id": row[3], "updated_at": row[4]}
        except Exception as e:
            logger.error(f"Failed to get memory provenance for '{key}': {e}")
            return None

    def get_all_memory(self) -> list:
        """All (key, value) pairs - folded into the system prompt so the AI
        just knows these things without needing to explicitly ask for each
        one every time."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value FROM memory ORDER BY key")
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch memory: {e}")
            return []

    # ---- Semantic recall (Phase 3) ----
    # Both of these degrade gracefully with no special-casing needed by
    # callers: semantic_recall() returns [] whenever embeddings aren't
    # available (package not installed, model failed to load, or nothing
    # has been embedded yet), and get_relevant_memory() falls back to
    # get_all_memory() whenever semantic_recall() comes back empty - the
    # exact behavior Wave had before either method existed.
    def semantic_recall(self, query: str, top_k: int = 8, min_similarity: float = 0.35) -> list:
        """Up to top_k (key, value, similarity) tuples whose stored
        embedding is most similar to `query`'s, above min_similarity,
        highest similarity first."""
        query_vec = embeddings.embed(query)
        if query_vec is None:
            return []
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key, value, embedding FROM memory WHERE embedding IS NOT NULL")
                rows = cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch memory for semantic recall: {e}")
            return []

        scored = []
        for key, value, blob in rows:
            vec = embeddings.deserialize(blob)
            if vec is None:
                continue
            similarity = embeddings.cosine_similarity(query_vec, vec)
            if similarity >= min_similarity:
                scored.append((key, value, similarity))
        scored.sort(key=lambda row: row[2], reverse=True)
        return scored[:top_k]

    def get_relevant_memory(self, query: str, limit: int = 8) -> list:
        """(key, value) pairs to fold into the system prompt for this
        particular message. While there's only a handful of remembered
        facts total, returns all of them (identical to get_all_memory() -
        filtering a short list buys nothing and risks leaving something
        out). Once there's more than `limit`, switches to semantic
        recall so the prompt grows with how relevant something is, not
        just with how much the user has ever told Wave to remember."""
        all_facts = self.get_all_memory()
        if len(all_facts) <= limit:
            return all_facts
        hits = self.semantic_recall(query, top_k=limit)
        if hits:
            chosen = [(key, value) for key, value, _similarity in hits]
            return chosen + self._one_hop(chosen, all_facts)
        return all_facts[:limit]

    @staticmethod
    def _one_hop(chosen: list, all_facts: list, extra: int = 4) -> list:
        """Tier 35: facts one step from the chosen ones - "sister_name:
        Priya" brings "priya_birthday" along, so "meri behen ka birthday kab
        hai" has both halves of the answer. A short value's words (a name, a
        place) are looked for in the other facts' keys."""
        import re as _re
        have = {k for k, _ in chosen}
        words = set()
        for _k, v in chosen:
            v = str(v or "")
            if len(v) <= 40:
                words.update(w for w in _re.findall(r"[a-zऀ-ॿ]{3,}", v.lower()))
        found = []
        for k, v in all_facts:
            if k in have:
                continue
            if any(w in k for w in words):
                found.append((k, v))
                if len(found) >= extra:
                    break
        return found

    def delete_memory(self, key: str) -> bool:
        """Forgets a fact - Tier 35: with its history, and remembered as
        forgotten, so it isn't learned again from old conversation and is
        left out of the conversation sent to the model."""
        clean_key = key.strip().lower()
        try:
            previous = self.get_memory(clean_key)
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM memory WHERE key = ?", (clean_key,))
                cursor.execute("DELETE FROM memory_history WHERE key = ?", (clean_key,))
                cursor.execute(
                    "INSERT INTO memory_forgotten (key, value, forgotten_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, forgotten_at=excluded.forgotten_at",
                    (clean_key, previous, datetime.now().isoformat()))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to delete memory '{key}': {e}")
            return False

    # ---- Tier 35: Memory 2.0 - history and forgetting ----
    def get_memory_history(self, key: str, limit: int = 5) -> list:
        """What a fact used to be, newest change first: dicts with
        old_value, new_value, source, changed_at."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT old_value, new_value, source, changed_at FROM memory_history "
                               "WHERE key = ? ORDER BY id DESC LIMIT ?", (key.strip().lower(), int(limit)))
                return [{"old_value": r[0], "new_value": r[1], "source": r[2], "changed_at": r[3]}
                        for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to read the history of '{key}': {e}")
            return []

    def get_forgotten_keys(self) -> set:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT key FROM memory_forgotten")
                return {r[0] for r in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Failed to read forgotten facts: {e}")
            return set()

    def get_forgotten_values(self, limit: int = 50) -> list:
        """The values of facts the user asked Wave to forget, newest first -
        WaveBrain._recent_history() leaves messages containing them out."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT value FROM memory_forgotten WHERE value IS NOT NULL AND value != '' "
                               "ORDER BY forgotten_at DESC LIMIT ?", (int(limit),))
                return [r[0] for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to read forgotten facts: {e}")
            return []

    # ---- Workflows: named sequences of actions ----
    # 🔧 FIX: same story as memory above - the table existed, nothing used
    # it. A workflow's actions are stored "||"-joined and replayed through
    # the same action executor a single voice command already goes through
    # (WaveBrain.process_actions), so no new execution path was needed.
    def save_workflow(self, name: str, actions: list) -> bool:
        try:
            actions_str = "||".join(a.strip() for a in actions if a and a.strip())
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # A workflow name is unique - saving again replaces it.
                cursor.execute("DELETE FROM workflows WHERE name = ?", (name.strip().lower(),))
                cursor.execute(
                    "INSERT INTO workflows (name, actions) VALUES (?, ?)",
                    (name.strip().lower(), actions_str)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to save workflow '{name}': {e}")
            return False

    def get_workflow(self, name: str) -> list:
        """The list of action strings for a named workflow, or [] if there's
        no workflow by that name."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT actions FROM workflows WHERE name = ?", (name.strip().lower(),))
                row = cursor.fetchone()
                return row[0].split("||") if row and row[0] else []
        except Exception as e:
            logger.error(f"Failed to get workflow '{name}': {e}")
            return []

    def list_workflows(self) -> list:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM workflows ORDER BY name")
                return [r[0] for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list workflows: {e}")
            return []

    # Tier 3 item 9: the Workflows UI page (ui/workflows_panel.py) needs to
    # list every workflow WITH its steps in one query (list_workflows()
    # above only returns names), and needs to actually delete/rename one -
    # neither existed before since only the voice path (create/run/list by
    # name) was wired up.
    def list_workflows_full(self) -> list:
        """[(name, [action, ...]), ...] for every saved workflow, name order."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT name, actions FROM workflows ORDER BY name")
                return [(name, actions.split("||") if actions else []) for name, actions in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list workflows (full): {e}")
            return []

    def delete_workflow(self, name: str) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM workflows WHERE name = ?", (name.strip().lower(),))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete workflow '{name}': {e}")
            return False

    def rename_workflow(self, old_name: str, new_name: str) -> bool:
        old_name, new_name = old_name.strip().lower(), new_name.strip().lower()
        if not new_name or old_name == new_name:
            return False
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # A workflow name is unique - renaming onto an existing name
                # would violate that, so refuse rather than silently merge.
                cursor.execute("SELECT 1 FROM workflows WHERE name = ?", (new_name,))
                if cursor.fetchone():
                    return False
                cursor.execute("UPDATE workflows SET name = ? WHERE name = ?", (new_name, old_name))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to rename workflow '{old_name}' -> '{new_name}': {e}")
            return False

    # ---- Tier 18: time-triggered workflow schedules ----
    # automation/workflow_scheduler.py owns the actual due/advance logic;
    # this is plain storage, same split as every other domain in this file.
    def save_workflow_schedule(self, workflow_name: str, time_text: str, repeat: str,
                                next_run_at_iso: str) -> int:
        """Returns the new schedule's row id, or None on failure. One
        workflow can have more than one schedule (e.g. a morning AND an
        evening run) - there's no uniqueness constraint on workflow_name."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO workflow_schedules (workflow_name, time_text, repeat, next_run_at) "
                    "VALUES (?, ?, ?, ?)",
                    (workflow_name, time_text, repeat, next_run_at_iso)
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save a schedule for workflow '{workflow_name}': {e}")
            return None

    def list_workflow_schedules(self, enabled_only: bool = False) -> list:
        """(id, workflow_name, time_text, repeat, next_run_at, enabled, last_run_at)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                sql = ("SELECT id, workflow_name, time_text, repeat, next_run_at, enabled, last_run_at "
                       "FROM workflow_schedules")
                if enabled_only:
                    sql += " WHERE enabled = 1"
                cursor.execute(sql + " ORDER BY next_run_at")
                return [tuple(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list workflow schedules: {e}")
            return []

    def get_due_workflow_schedules(self, now_iso: str) -> list:
        """Enabled schedules whose next_run_at has arrived - same shape as
        list_workflow_schedules(), used by the proactive engine's
        per-tick check. REPEATABLE: calling this again without first
        advancing/disabling a returned row would return it again - that's
        deliberate (it mirrors get_due_reminders()'s own contract); the
        caller is the one responsible for calling advance_workflow_schedule
        or disable_workflow_schedule right after running it."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, workflow_name, time_text, repeat, next_run_at, enabled, last_run_at "
                    "FROM workflow_schedules WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
                    (now_iso,)
                )
                return [tuple(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to fetch due workflow schedules: {e}")
            return []

    def advance_workflow_schedule(self, schedule_id: int, next_run_at_iso: str, last_run_at_iso: str) -> None:
        """A repeating schedule just ran - push next_run_at forward and
        record when it last ran. Stays enabled."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE workflow_schedules SET next_run_at = ?, last_run_at = ? WHERE id = ?",
                    (next_run_at_iso, last_run_at_iso, schedule_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to advance workflow schedule {schedule_id}: {e}")

    def disable_workflow_schedule(self, schedule_id: int, last_run_at_iso: str) -> None:
        """A one-off ('once') schedule just ran - it's done; turn it off
        rather than deleting it, so list_workflow_schedules() can still
        show the user it ran and when."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE workflow_schedules SET enabled = 0, last_run_at = ? WHERE id = ?",
                    (last_run_at_iso, schedule_id)
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to disable workflow schedule {schedule_id}: {e}")

    def cancel_workflow_schedules_for(self, workflow_name: str) -> int:
        """Deletes every schedule for `workflow_name` (there can be more
        than one - see save_workflow_schedule). Returns how many."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM workflow_schedules WHERE workflow_name = ?", (workflow_name,))
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cancel schedules for workflow '{workflow_name}': {e}")
            return 0

    # ---- Reminders / Alarms ----
    # Natural-language time parsing (dateparser) happens in brain/groq_core.py,
    # right before save_reminder() is called - this layer only ever stores/reads
    # plain ISO-8601 strings, same as everything else in this file.
    def save_reminder(self, message: str, due_at_iso: str, recurrence: str = None) -> int:
        """Saves a new reminder and returns its row id, or None on failure.
        recurrence is None for a one-off reminder (unchanged default) or
        'daily' to have it reschedule itself a day later each time it fires
        instead of ending - see complete_or_reschedule_reminder()."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO reminders (message, due_at, recurrence) VALUES (?, ?, ?)",
                    (message.strip(), due_at_iso, (recurrence or None))
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save reminder: {e}")
            return None

    def get_due_reminders(self) -> list:
        """(id, message, recurrence) triples for every untriggered reminder
        whose due time has arrived - polled by ReminderWorker in
        ui/workers.py. 🔧 Tier 3: extended from (id, message) with
        `recurrence` so the worker can hand it straight to
        complete_or_reschedule_reminder() without a second query."""
        try:
            now_iso = datetime.now().isoformat()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, message, recurrence FROM reminders WHERE triggered = 0 AND due_at <= ?",
                    (now_iso,)
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch due reminders: {e}")
            return []

    def mark_reminder_triggered(self, reminder_id: int) -> bool:
        """Unchanged one-off completion - still here for anything (a user
        skill, a test) that wants the plain old behavior. ReminderWorker
        itself now calls complete_or_reschedule_reminder() instead, since
        that also handles recurrence and sets up the follow-up window."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE reminders SET triggered = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to mark reminder {reminder_id} triggered: {e}")
            return False

    def complete_or_reschedule_reminder(self, reminder_id: int, recurrence: str = None,
                                         followup_at_iso: str = None) -> bool:
        """Called once, right when a reminder fires - the Tier 3 replacement
        for 'mark_reminder_triggered then forget it'.

        One-off (recurrence falsy): sets triggered = 1, exactly as
        mark_reminder_triggered did before.

        recurrence == 'daily': the row is NOT closed out - due_at moves
        forward a day (computed in Python with datetime/timedelta, not
        SQLite's datetime(), since due_at is stored as Python's .isoformat()
        and this file's own convention - see the comment above
        save_reminder() - is that date arithmetic on these strings happens
        here, not in SQL) and triggered/notified_upcoming reset to 0, so the
        next poll treats it as a fresh occurrence under the SAME id (its
        edit/cancel history stays attached to one row instead of a new one
        being created daily). If Wave was closed over one or more due dates,
        this walks forward past `now` rather than leaving it overdue.

        Either way delivered_at is stamped now (follow-up wording's "N
        minutes ago" needs it) and followup_at/followup_sent are set up for
        get_due_followups() to find later - or left NULL/0 if this
        reminder's category gets no follow-up at all (the caller decides
        that via reminders/context.py's effective_policy before calling
        this)."""
        try:
            now = datetime.now()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if recurrence == "daily":
                    cursor.execute("SELECT due_at FROM reminders WHERE id = ?", (reminder_id,))
                    row = cursor.fetchone()
                    if row is None:
                        return False
                    try:
                        next_due = datetime.fromisoformat(row[0])
                    except (TypeError, ValueError):
                        next_due = now
                    next_due += timedelta(days=1)
                    while next_due <= now:
                        next_due += timedelta(days=1)
                    cursor.execute(
                        "UPDATE reminders SET due_at = ?, triggered = 0, notified_upcoming = 0, "
                        "delivered_at = ?, followup_at = ?, followup_sent = 0 WHERE id = ?",
                        (next_due.isoformat(), now.isoformat(), followup_at_iso, reminder_id)
                    )
                else:
                    cursor.execute(
                        "UPDATE reminders SET triggered = 1, delivered_at = ?, "
                        "followup_at = ?, followup_sent = 0 WHERE id = ?",
                        (now.isoformat(), followup_at_iso, reminder_id)
                    )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to complete/reschedule reminder {reminder_id}: {e}")
            return False

    def get_reminders_due_soon(self, window_minutes: int) -> list:
        """(id, message, due_at) for untriggered reminders due between now
        and now + window_minutes that haven't had their heads-up notice sent
        yet - polled by ReminderWorker to fire reminder_upcoming a few
        minutes before the actual alarm. Deliberately excludes anything
        already <= now (get_due_reminders()'s job, not this one) so a
        reminder is never announced as both 'coming up' and 'due' in the
        same poll tick."""
        try:
            now = datetime.now()
            horizon_iso = (now + timedelta(minutes=max(0, window_minutes))).isoformat()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, message, due_at FROM reminders WHERE triggered = 0 "
                    "AND notified_upcoming = 0 AND due_at > ? AND due_at <= ? ORDER BY due_at ASC",
                    (now.isoformat(), horizon_iso)
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch reminders due soon: {e}")
            return []

    def mark_upcoming_notified(self, reminder_id: int) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE reminders SET notified_upcoming = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to mark reminder {reminder_id} upcoming-notified: {e}")
            return False

    def get_due_followups(self) -> list:
        """(id, message, delivered_at) for reminders whose follow-up time has
        arrived and hasn't been sent yet - polled right after
        get_due_reminders(). followup_at is only ever set by
        complete_or_reschedule_reminder() for categories
        reminders/context.py's effective_policy marks as follow-up-worthy
        (sleep, study, break, ...), so most reminders simply never produce a
        row here."""
        try:
            now_iso = datetime.now().isoformat()
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, message, delivered_at FROM reminders WHERE followup_sent = 0 "
                    "AND followup_at IS NOT NULL AND followup_at <= ?",
                    (now_iso,)
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch due follow-ups: {e}")
            return []

    def mark_followup_sent(self, reminder_id: int) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE reminders SET followup_sent = 1 WHERE id = ?", (reminder_id,))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to mark reminder {reminder_id} follow-up sent: {e}")
            return False

    def get_upcoming_reminders(self) -> list:
        """(id, message, due_at, recurrence) for every untriggered reminder,
        soonest first - used by the list_reminders tool. 🔧 Tier 3: extended
        with `recurrence` so the listing can mark daily reminders as such."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, message, due_at, recurrence FROM reminders "
                    "WHERE triggered = 0 ORDER BY due_at ASC"
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch upcoming reminders: {e}")
            return []

    def cancel_reminder(self, text_query: str) -> int:
        """Cancels every untriggered reminder whose message contains
        text_query (case-insensitive). Returns how many were cancelled."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM reminders WHERE triggered = 0 AND message LIKE ?",
                    (f"%{text_query.strip()}%",)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cancel reminder '{text_query}': {e}")
            return 0

    def cancel_all_reminders(self) -> int:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM reminders WHERE triggered = 0")
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cancel all reminders: {e}")
            return 0

    def cancel_reminder_by_id(self, reminder_id: int) -> bool:
        """Cancels exactly one untriggered reminder by its row id. Tier 5:
        added for timers (brain/handlers/timers.py), which look a specific
        timer up first and then need to cancel exactly that one - cancel_reminder()'s
        substring match on message text is the wrong tool here, since two
        timers could share a label (or a timer's label could be a substring
        of an unrelated real reminder's text) and a text match could catch
        more than the one the caller already identified."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM reminders WHERE id = ? AND triggered = 0", (reminder_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to cancel reminder {reminder_id}: {e}")
            return False

    # ---- Tier 5: stopwatch (separate from reminders/timers above) ----
    def start_stopwatch(self) -> bool:
        """Starts the stopwatch from zero. Returns False (does nothing) if
        it's already running - there's only one stopwatch (single row,
        id=1), so starting again without stopping first would silently
        discard the running one's start time and laps."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT started_at, stopped_at FROM stopwatch WHERE id = 1")
                row = cursor.fetchone()
                if row and row[0] and not row[1]:
                    return False  # already running
                cursor.execute(
                    "UPDATE stopwatch SET started_at = ?, stopped_at = NULL, laps = '[]' WHERE id = 1",
                    (datetime.now().isoformat(),)
                )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to start stopwatch: {e}")
            return False

    def stop_stopwatch(self):
        """Stops the running stopwatch and returns the total elapsed seconds
        (float), or None if it wasn't running (never started, or already
        stopped)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT started_at, stopped_at FROM stopwatch WHERE id = 1")
                row = cursor.fetchone()
                if not row or not row[0] or row[1]:
                    return None
                now = datetime.now()
                cursor.execute("UPDATE stopwatch SET stopped_at = ? WHERE id = 1", (now.isoformat(),))
                conn.commit()
                return (now - datetime.fromisoformat(row[0])).total_seconds()
        except Exception as e:
            logger.error(f"Failed to stop stopwatch: {e}")
            return None

    def lap_stopwatch(self):
        """Records a lap on the running stopwatch and returns the elapsed
        seconds (float) at this lap, or None if it isn't running."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT started_at, stopped_at, laps FROM stopwatch WHERE id = 1")
                row = cursor.fetchone()
                if not row or not row[0] or row[1]:
                    return None
                elapsed = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds()
                try:
                    laps = json.loads(row[2]) if row[2] else []
                    if not isinstance(laps, list):
                        laps = []
                except ValueError:
                    laps = []
                laps.append(elapsed)
                cursor.execute("UPDATE stopwatch SET laps = ? WHERE id = 1", (json.dumps(laps),))
                conn.commit()
                return elapsed
        except Exception as e:
            logger.error(f"Failed to record stopwatch lap: {e}")
            return None

    def stopwatch_status(self) -> dict:
        """{'running': bool, 'elapsed_s': float|None, 'laps': [float, ...]}.
        elapsed_s is the live elapsed time while running, the final elapsed
        time once stopped, or None if the stopwatch has never been started -
        calling this never changes any state (unlike lap/stop), so checking
        the time left doesn't itself count as a lap."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT started_at, stopped_at, laps FROM stopwatch WHERE id = 1")
                row = cursor.fetchone()
                if not row or not row[0]:
                    return {"running": False, "elapsed_s": None, "laps": []}
                running = not row[1]
                end = datetime.now() if running else datetime.fromisoformat(row[1])
                try:
                    laps = json.loads(row[2]) if row[2] else []
                    if not isinstance(laps, list):
                        laps = []
                except ValueError:
                    laps = []
                return {"running": running, "elapsed_s": (end - datetime.fromisoformat(row[0])).total_seconds(),
                        "laps": laps}
        except Exception as e:
            logger.error(f"Failed to read stopwatch status: {e}")
            return {"running": False, "elapsed_s": None, "laps": []}

    # ---- Phase 6: calendar events ----
    # Natural-language time parsing (dateparser) happens in
    # automation/calendar_manager.py, same split as reminders above - this
    # layer only ever stores/reads plain ISO-8601 strings.
    def save_calendar_event(self, title: str, start_at_iso: str, end_at_iso: str = None,
                            location: str = None, notes: str = None, recurrence: str = None) -> int:
        """Saves a new calendar event and returns its row id, or None on failure.
        recurrence is None for a one-off event (unchanged default), or
        'daily'/'weekly' to have it recur - see get_calendar_events_between()
        for how recurring events are expanded back out again."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO calendar_events (title, start_at, end_at, location, notes, recurrence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (title.strip(), start_at_iso, end_at_iso, location, notes, (recurrence or None))
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save calendar event: {e}")
            return None

    def get_calendar_events_between(self, start_iso: str, end_iso: str) -> list:
        """(id, title, start_at, end_at, location, notes, recurrence) for
        every event occurring in [start_iso, end_iso), soonest first.
        🔧 Tier 8: extended with `recurrence`, and now also expands a
        recurring event into one virtual row per occurrence that falls in
        the window, rather than only ever matching its original start_at.

        A recurring event's stored start_at/end_at are its FIRST
        occurrence and are never rewritten (unlike reminders.due_at, which
        complete_or_reschedule_reminder mutates forward each time a *poll*
        fires it). Calendar events have no equivalent always-on poll
        behind their core "what's on my calendar" use case -
        list_calendar_events is a pull, not a push - so a row-mutation
        design would leave a recurring event stuck on its original date
        on any day nothing happened to poll it forward. Expanding
        occurrences here, at query time, is correct regardless of
        whether the proactive engine (or anything else) is even running.

        Two passes, deliberately not one query: (1) rows whose stored
        start_at already falls in the window - the original behavior,
        covers one-off events and a recurring event's own first
        occurrence; (2) recurring rows whose stored start_at is BEFORE
        the window, walked forward in Python (this file's standing
        convention for date arithmetic on these ISO strings - see the
        comment above save_reminder()) to find any later occurrences
        that land inside it. The `start_at < ?` filter on pass 2 can never
        overlap pass 1's `start_at >= ?`, so no occurrence is ever
        double-counted.

        Cancelling a recurring event (cancel_calendar_event) still
        deletes its one real row, ending the whole series at once - the
        same granularity as cancelling a daily reminder. Neither feature
        supports ending just one occurrence without ending the series."""
        try:
            start_dt = datetime.fromisoformat(start_iso)
            end_dt = datetime.fromisoformat(end_iso)
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, title, start_at, end_at, location, notes, recurrence "
                    "FROM calendar_events WHERE start_at >= ? AND start_at < ?",
                    (start_iso, end_iso)
                )
                rows = [list(r) for r in cursor.fetchall()]

                cursor.execute(
                    "SELECT id, title, start_at, end_at, location, notes, recurrence "
                    "FROM calendar_events WHERE recurrence IS NOT NULL AND start_at < ?",
                    (start_iso,)
                )
                for _id, title, start_at, end_at, location, notes, recurrence in cursor.fetchall():
                    step = timedelta(days=7) if recurrence == "weekly" else timedelta(days=1)
                    try:
                        occ_start = datetime.fromisoformat(start_at)
                    except (TypeError, ValueError):
                        continue
                    duration = None
                    if end_at:
                        try:
                            duration = datetime.fromisoformat(end_at) - occ_start
                        except (TypeError, ValueError):
                            duration = None
                    # Walk forward from the ORIGINAL occurrence (never
                    # mutated) to the first repeat on/after this window's
                    # start. Always terminates: every caller's window is
                    # bounded (today/this week/365-day "upcoming" cap in
                    # calendar_manager.py's _resolve_range), so this is at
                    # most window_length / step iterations, never unbounded.
                    occ_start += step
                    while occ_start < start_dt:
                        occ_start += step
                    while occ_start < end_dt:
                        occ_end = (occ_start + duration).isoformat() if duration else None
                        rows.append([_id, title, occ_start.isoformat(), occ_end, location, notes, recurrence])
                        occ_start += step

                rows.sort(key=lambda r: r[2])
                return [tuple(r) for r in rows]
        except Exception as e:
            logger.error(f"Failed to fetch calendar events: {e}")
            return []

    def get_upcoming_calendar_events(self, within_minutes: int) -> list:
        """(id, title, start_at, end_at, location, notes, recurrence) for
        events starting between now and `within_minutes` from now - used
        by the proactive engine's heads-up nudge. Never looks backward, so
        an event that's already started doesn't re-trigger a 'starting
        soon' nudge. 🔧 Tier 8: a recurring event can appear here more
        than once over time (once per occurrence, each with a different
        start_at but the SAME id - see get_calendar_events_between()) -
        automation/proactive.py's dedup key accounts for this."""
        try:
            now = datetime.now()
            until = now + timedelta(minutes=max(0, within_minutes))
            return self.get_calendar_events_between(now.isoformat(), until.isoformat())
        except Exception as e:
            logger.error(f"Failed to fetch upcoming calendar events: {e}")
            return []

    def cancel_calendar_event(self, text_query: str) -> int:
        """Cancels every event whose title contains text_query (case-insensitive).
        Returns how many were cancelled."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM calendar_events WHERE title LIKE ?",
                    (f"%{text_query.strip()}%",)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to cancel calendar event '{text_query}': {e}")
            return 0

    def cancel_calendar_event_by_id(self, event_id: int) -> bool:
        """Tier 21: id-precise counterpart to cancel_calendar_event's
        substring match, for the new Calendar management page's delete
        button. Deletes the one real row - for a recurring event that
        means every occurrence (see get_calendar_events_between's own
        comment: a recurring event's occurrences all share one real id,
        and this project has never supported ending a single occurrence
        without ending the series)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM calendar_events WHERE id = ?", (event_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to cancel calendar event #{event_id}: {e}")
            return False

    # ---- Tier 18: calendar sync (Google/ICS) ----
    def upsert_synced_calendar_event(self, source: str, external_id: str, title: str,
                                      start_at_iso: str, end_at_iso: str = None,
                                      location: str = None, notes: str = None,
                                      recurrence: str = None) -> str:
        """INSERTs a synced event, or UPDATEs it in place if a row with the
        same (source, external_id) already exists - so re-syncing a feed
        never duplicates events, and one the remote calendar moved/renamed
        updates instead of leaving a stale duplicate behind. Returns
        'inserted'/'updated' (so a sync can tally each), or '' on failure.
        Mirrors save_streak()'s own plain SELECT-then-INSERT/UPDATE style.

        Deliberately does NOT remove a local row whose source event has
        since been deleted upstream - sync only ever adds/updates; removing
        a synced event that disappeared from the feed is a real gap, left
        open (cancel_calendar_event still works by hand) rather than
        guessed at with a "haven't seen this external_id this sync" rule
        that would need its own careful testing this tier doesn't have
        room for."""
        if not source or not external_id:
            return ""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id FROM calendar_events WHERE source = ? AND external_id = ?",
                    (source, external_id)
                )
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "UPDATE calendar_events SET title=?, start_at=?, end_at=?, location=?, "
                        "notes=?, recurrence=? WHERE id=?",
                        (title.strip(), start_at_iso, end_at_iso, location, notes, recurrence, row[0])
                    )
                    conn.commit()
                    return "updated"
                cursor.execute(
                    "INSERT INTO calendar_events (title, start_at, end_at, location, notes, "
                    "recurrence, source, external_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title.strip(), start_at_iso, end_at_iso, location, notes, recurrence, source, external_id)
                )
                conn.commit()
                return "inserted"
        except Exception as e:
            logger.error(f"Failed to upsert synced calendar event ({source}/{external_id}): {e}")
            return ""

    def save_calendar_feed(self, name: str, kind: str, url: str = None) -> bool:
        """Upserts a registered calendar source by name (re-adding the
        same name updates its URL rather than erroring or duplicating)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM calendar_feeds WHERE name = ?", (name,))
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "UPDATE calendar_feeds SET kind = ?, url = ? WHERE name = ?",
                        (kind, url, name)
                    )
                else:
                    cursor.execute(
                        "INSERT INTO calendar_feeds (name, kind, url) VALUES (?, ?, ?)",
                        (name, kind, url)
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save calendar feed '{name}': {e}")
            return False

    def list_calendar_feeds(self) -> list:
        """(id, name, kind, url, last_synced_at), oldest-added first."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, name, kind, url, last_synced_at FROM calendar_feeds ORDER BY id")
                return [tuple(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list calendar feeds: {e}")
            return []

    def get_calendar_feed(self, name: str):
        """(id, name, kind, url, last_synced_at) for one feed, or None."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, name, kind, url, last_synced_at FROM calendar_feeds WHERE name = ?", (name,)
                )
                row = cursor.fetchone()
                return tuple(row) if row else None
        except Exception as e:
            logger.error(f"Failed to fetch calendar feed '{name}': {e}")
            return None

    def delete_calendar_feed(self, name: str) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM calendar_feeds WHERE name = ?", (name,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete calendar feed '{name}': {e}")
            return False

    def mark_calendar_feed_synced(self, name: str, when_iso: str) -> None:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("UPDATE calendar_feeds SET last_synced_at = ? WHERE name = ?", (when_iso, name))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to mark calendar feed '{name}' synced: {e}")

    # ---- Tier 8: daily check-in streaks ----
    # Plain storage only - see automation/streak_manager.py for the actual
    # day-boundary logic (same-day/consecutive-day/missed-day), same split
    # as calendar_events above (CalendarManager owns the logic, this file
    # just stores/reads rows).
    def get_streak(self, habit: str):
        """(id, habit, current_streak, best_streak, last_checkin_date) for
        one habit, or None if it has no streak started yet."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, habit, current_streak, best_streak, last_checkin_date "
                    "FROM streaks WHERE habit = ?",
                    (habit,)
                )
                return cursor.fetchone()
        except Exception as e:
            logger.error(f"Failed to fetch streak for '{habit}': {e}")
            return None

    def save_streak(self, habit: str, current_streak: int, best_streak: int, last_checkin_date_iso: str) -> bool:
        """Upserts one habit's streak row - a plain SELECT-then-INSERT/
        UPDATE in Python rather than SQLite's ON CONFLICT syntax, matching
        this file's existing style everywhere else (e.g. cancel_reminder,
        save_calendar_event - no upsert-specific SQL elsewhere in this
        class). habit's UNIQUE constraint (see init_db) is the safety net
        if that ever raced; under this app's single-connection-at-a-time
        usage it never actually does."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM streaks WHERE habit = ?", (habit,))
                row = cursor.fetchone()
                if row:
                    cursor.execute(
                        "UPDATE streaks SET current_streak = ?, best_streak = ?, last_checkin_date = ? "
                        "WHERE habit = ?",
                        (current_streak, best_streak, last_checkin_date_iso, habit)
                    )
                else:
                    cursor.execute(
                        "INSERT INTO streaks (habit, current_streak, best_streak, last_checkin_date) "
                        "VALUES (?, ?, ?, ?)",
                        (habit, current_streak, best_streak, last_checkin_date_iso)
                    )
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save streak for '{habit}': {e}")
            return False

    def list_streaks(self) -> list:
        """(id, habit, current_streak, best_streak, last_checkin_date) for
        every habit with a streak, highest current streak first."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT id, habit, current_streak, best_streak, last_checkin_date "
                    "FROM streaks ORDER BY current_streak DESC, habit ASC"
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to list streaks: {e}")
            return []

    def delete_streak(self, habit: str) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM streaks WHERE habit = ?", (habit,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete streak for '{habit}': {e}")
            return False

    # ---- Tier 16: quick notes (see init_db's own comment on how this
    # differs from the note_chunks RAG index just below) ----
    def add_quick_note(self, content: str) -> bool:
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("INSERT INTO quick_notes (content) VALUES (?)", (content.strip(),))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Failed to save quick note: {e}")
            return False

    def list_quick_notes(self) -> list:
        """(id, content, created_at) for every quick note, oldest first -
        same order you'd want to read a running scratchpad back in."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, content, created_at FROM quick_notes ORDER BY id ASC")
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to list quick notes: {e}")
            return []

    def clear_quick_notes(self) -> int:
        """Deletes every quick note. Returns how many were cleared - no
        undo path, same reasoning as reset_streak (T2 in brain/tiers.py)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM quick_notes")
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to clear quick notes: {e}")
            return 0

    # ---- Tier 16: to-dos ----
    def add_todo(self, text: str, due_text: str = None) -> int:
        """Saves a new to-do and returns its row id, or None on failure.
        due_text is free text exactly as given (see init_db's comment) -
        automation/todo_manager.py is what, separately, turns it into an
        actual reminder when one is wanted."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO todos (text, due_text) VALUES (?, ?)",
                    (text.strip(), (due_text or None))
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save to-do: {e}")
            return None

    def list_todos(self, include_completed: bool = False) -> list:
        """(id, text, due_text, completed, created_at), oldest first.
        include_completed=False (the default, what "what's on my to-do
        list" means) hides anything already checked off."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                if include_completed:
                    cursor.execute(
                        "SELECT id, text, due_text, completed, created_at FROM todos ORDER BY id ASC"
                    )
                else:
                    cursor.execute(
                        "SELECT id, text, due_text, completed, created_at FROM todos "
                        "WHERE completed = 0 ORDER BY id ASC"
                    )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to list to-dos: {e}")
            return []

    def complete_todo(self, text_query: str) -> int:
        """Marks every NOT-YET-completed to-do whose text contains
        text_query (case-insensitive) as done. Scoped to completed = 0 so
        re-completing an already-done item (e.g. saying it twice) doesn't
        keep bumping completed_at - matches cancel_reminder's "only touch
        the live ones" scoping just above."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE todos SET completed = 1, completed_at = CURRENT_TIMESTAMP "
                    "WHERE completed = 0 AND text LIKE ?",
                    (f"%{text_query.strip()}%",)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to complete to-do '{text_query}': {e}")
            return 0

    def delete_todo(self, text_query: str) -> int:
        """Deletes every to-do (completed or not) whose text contains
        text_query. Returns how many were deleted. T1 in brain/tiers.py -
        same reasoning as cancel_reminder: removing a tracked item/marker,
        not information that was hard to reproduce (see clear_quick_notes'
        own T2 comment for that distinction)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM todos WHERE text LIKE ?", (f"%{text_query.strip()}%",))
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to delete to-do '{text_query}': {e}")
            return 0

    def delete_todo_by_id(self, todo_id: int) -> bool:
        """Tier 21: deletes exactly one to-do by its row id - same reasoning
        as cancel_reminder_by_id above delete_todo's own substring match:
        the new Notes & To-dos management page already has the exact row
        the user clicked, and a text-substring delete is the wrong tool
        once you already know precisely which row that is (two to-dos
        could share wording, or one's text could be a substring of
        another's)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete to-do #{todo_id}: {e}")
            return False

    def complete_todo_by_id(self, todo_id: int) -> bool:
        """Tier 21: id-precise counterpart to complete_todo's substring
        match, for the same reason delete_todo_by_id exists next to
        delete_todo."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE todos SET completed = 1, completed_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND completed = 0",
                    (todo_id,)
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to complete to-do #{todo_id}: {e}")
            return False

    def delete_quick_note_by_id(self, note_id: int) -> bool:
        """Tier 21: deletes exactly one quick note. clear_quick_notes()
        (T2, deliberately all-or-nothing - see its own comment) stays as
        the voice-command tool; this is the new Notes & To-dos management
        page's precise single-row delete."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM quick_notes WHERE id = ?", (note_id,))
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to delete quick note #{note_id}: {e}")
            return False

    # ---- Phase 6: note chunks (search_my_notes RAG index) ----
    def get_note_file_signature(self, file_path: str):
        """(file_mtime, file_size) this file was last indexed at, or None if
        it's never been indexed - lets the indexer skip unchanged files."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT file_mtime, file_size FROM note_chunks WHERE file_path = ? LIMIT 1",
                    (file_path,)
                )
                row = cursor.fetchone()
                return (row[0], row[1]) if row else None
        except Exception as e:
            logger.error(f"Failed to read note index signature for '{file_path}': {e}")
            return None

    def replace_note_chunks(self, file_path: str, file_mtime: float, file_size: int, chunks: list) -> bool:
        """Replaces every stored chunk for `file_path` with `chunks` - a list
        of (content, embedding_json_or_None) tuples - in one transaction, so a
        re-index can never leave half the old version and half the new one."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM note_chunks WHERE file_path = ?", (file_path,))
                cursor.executemany(
                    "INSERT INTO note_chunks (file_path, chunk_index, content, embedding, file_mtime, file_size) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(file_path, i, content, emb, file_mtime, file_size) for i, (content, emb) in enumerate(chunks)]
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to index notes for '{file_path}': {e}")
            return False

    def remove_note_file(self, file_path: str) -> bool:
        """Drops every chunk for a file that's been deleted/moved since it was indexed."""
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM note_chunks WHERE file_path = ?", (file_path,))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to remove note index for '{file_path}': {e}")
            return False

    def indexed_note_files(self) -> list:
        """Every distinct file_path currently indexed - lets the indexer
        notice files that were deleted since the last run."""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT DISTINCT file_path FROM note_chunks")
                return [r[0] for r in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to list indexed note files: {e}")
            return []

    def semantic_search_notes(self, query: str, top_k: int = 6, min_similarity: float = 0.3) -> list:
        """Up to top_k (file_path, chunk_index, content, similarity) tuples
        whose stored embedding is most similar to `query`'s, above
        min_similarity, highest similarity first. [] if embeddings aren't
        available - callers fall back to keyword_search_notes()."""
        query_vec = embeddings.embed(query)
        if query_vec is None:
            return []
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT file_path, chunk_index, content, embedding FROM note_chunks "
                    "WHERE embedding IS NOT NULL"
                )
                rows = cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch note chunks for semantic search: {e}")
            return []

        scored = []
        for file_path, chunk_index, content, blob in rows:
            vec = embeddings.deserialize(blob)
            if vec is None:
                continue
            similarity = embeddings.cosine_similarity(query_vec, vec)
            if similarity >= min_similarity:
                scored.append((file_path, chunk_index, content, similarity))
        scored.sort(key=lambda row: row[3], reverse=True)
        return scored[:top_k]

    def keyword_search_notes(self, query: str, top_k: int = 6) -> list:
        """Plain case-insensitive substring fallback for when semantic search
        found nothing (embeddings unavailable, or genuinely no close match) -
        (file_path, chunk_index, content) for chunks containing `query`."""
        q = (query or "").strip()
        if not q:
            return []
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT file_path, chunk_index, content FROM note_chunks "
                    "WHERE content LIKE ? LIMIT ?",
                    (f"%{q}%", top_k)
                )
                return cursor.fetchall()
        except Exception as e:
            logger.error(f"Failed keyword search over notes: {e}")
            return []

    # ---- Phase 4: action audit log (policy lives in automation/audit.py) ----
    _ACTION_COLUMNS = ("ts", "tool", "args", "result", "status", "source",
                       "undo_kind", "undo_payload", "undo_label", "undone_at")

    def insert_action(self, row: dict) -> int:
        """Stores one audit row and returns its id, or None on failure."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "INSERT INTO action_log (" + ", ".join(self._ACTION_COLUMNS) + ") VALUES ("
                    + ", ".join("?" for _ in self._ACTION_COLUMNS) + ")",
                    tuple(row.get(c) for c in self._ACTION_COLUMNS)
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save action log row: {e}")
            return None

    def update_action(self, action_id: int, **fields) -> bool:
        cols = {k: v for k, v in fields.items() if k in self._ACTION_COLUMNS}
        if not cols:
            return False
        try:
            with self.get_connection() as conn:
                conn.execute(
                    "UPDATE action_log SET " + ", ".join(f"{k} = ?" for k in cols) + " WHERE id = ?",
                    (*cols.values(), action_id)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to update action {action_id}: {e}")
            return False

    def get_recent_actions(self, limit: int = 200) -> list:
        """Newest-first list of dicts (id + every column in _ACTION_COLUMNS)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, " + ", ".join(self._ACTION_COLUMNS)
                    + " FROM action_log ORDER BY id DESC LIMIT ?", (limit,)
                )
                names = ["id", *self._ACTION_COLUMNS]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to read action log: {e}")
            return []

    def prune_actions(self, keep: int) -> int:
        """Deletes everything but the newest `keep` rows; returns how many went."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM action_log WHERE id NOT IN "
                    "(SELECT id FROM action_log ORDER BY id DESC LIMIT ?)", (keep,)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to prune action log: {e}")
            return 0

    def clear_actions(self) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM action_log")
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to clear action log: {e}")
            return False

    # ---- Tier 4 item #13: turn_trace (telemetry/store.py is the only caller) ----
    _TRACE_COLUMNS = ("turn_id", "started_at", "ended_at", "source", "route",
                      "outcome", "model", "provider", "rounds", "tools_called",
                      "total_ms", "llm_ms", "tool_ms", "stages", "meta", "error")

    def insert_trace(self, row: dict) -> int:
        """Stores one finished-turn trace row and returns its id, or None on failure."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "INSERT INTO turn_trace (" + ", ".join(self._TRACE_COLUMNS) + ") VALUES ("
                    + ", ".join("?" for _ in self._TRACE_COLUMNS) + ")",
                    tuple(row.get(c) for c in self._TRACE_COLUMNS)
                )
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Failed to save turn_trace row: {e}")
            return None

    def insert_traces(self, rows: list) -> int:
        """Batched insert_trace - one commit for the whole list. Returns how many were written."""
        if not rows:
            return 0
        try:
            with self.get_connection() as conn:
                conn.executemany(
                    "INSERT INTO turn_trace (" + ", ".join(self._TRACE_COLUMNS) + ") VALUES ("
                    + ", ".join("?" for _ in self._TRACE_COLUMNS) + ")",
                    [tuple(row.get(c) for c in self._TRACE_COLUMNS) for row in rows]
                )
                conn.commit()
                return len(rows)
        except Exception as e:
            logger.error(f"Failed to save a batch of turn_trace rows: {e}")
            return 0

    def get_recent_traces(self, limit: int = 200) -> list:
        """Newest-first list of dicts (id + every column in _TRACE_COLUMNS)."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "SELECT id, " + ", ".join(self._TRACE_COLUMNS)
                    + " FROM turn_trace ORDER BY id DESC LIMIT ?", (limit,)
                )
                names = ["id", *self._TRACE_COLUMNS]
                return [dict(zip(names, row)) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Failed to read turn_trace: {e}")
            return []

    def prune_traces(self, keep: int) -> int:
        """Deletes everything but the newest `keep` rows; returns how many went."""
        try:
            with self.get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM turn_trace WHERE id NOT IN "
                    "(SELECT id FROM turn_trace ORDER BY id DESC LIMIT ?)", (keep,)
                )
                conn.commit()
                return cursor.rowcount
        except Exception as e:
            logger.error(f"Failed to prune turn_trace: {e}")
            return 0

    def clear_traces(self) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM turn_trace")
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to clear turn_trace: {e}")
            return False


db = DatabaseManager()