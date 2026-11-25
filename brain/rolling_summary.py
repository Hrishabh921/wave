"""
brain/rolling_summary.py — Tier 38: "the conversation so far", for long chats.

Each request carries only the newest 16 messages (and at most
LLM_HISTORY_CHARS of them - every extra token counts against Groq's
per-minute quota). Anything older simply vanished: twenty minutes into a
conversation Wave no longer knew what it had started with. SARA folds the
turns that fall out of its window into a short running summary with one
small background model call, and puts that one line in the system prompt.

Wave does the same: once at least FOLD_AFTER messages have dropped out of
the window, one background call to the fast model (brain/model_router.py's
background_model) folds them into the existing summary, kept under
LLM_SUMMARY_MAX_CHARS. The prompt gets it as "EARLIER IN THIS CONVERSATION:
...". It only belongs to the current conversation: messages older than
SESSION_GAP_H hours aren't folded, and a summary whose newest message is
that old isn't used. Nothing containing something the user asked Wave to
forget (Tier 35) goes in, or out. Saved in <DATA_DIR>/conversation_summary.json.
LLM_ROLLING_SUMMARY=false turns it off.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from settings.config import config

logger = logging.getLogger("RollingSummary")

WINDOW = 16                # messages still sent as history (WaveBrain._recent_history)
FOLD_AFTER = 6             # fold once this many have dropped out
MIN_INTERVAL_S = 30.0      # at most one summary call this often
SESSION_GAP_H = 12.0
_PROMPT = (
    "You keep a running summary of a conversation between a user and their voice assistant, Wave.\n"
    "Update the summary with the new messages below. Keep what matters later: names, numbers, dates, "
    "decisions, what the user wants or asked for, and anything left open. Drop small talk. Write in "
    "English, in the third person (\"The user ...\"), at most {max_chars} characters. Reply with ONLY the "
    "summary.\n\nSummary so far:\n{summary}\n\nNew messages:\n{messages}\n"
)


def _parse_ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


class RollingSummary:
    def __init__(self, path: str = None, clock=time.monotonic, now=datetime.now):
        self._path = path
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._state = None
        self._running = False
        self._last_call = -1e9

    # -- state -----------------------------------------------------------
    @staticmethod
    def enabled() -> bool:
        return bool(getattr(config, "LLM_ROLLING_SUMMARY", True))

    def path(self) -> str:
        return self._path or os.path.join(config.DATA_DIR, "conversation_summary.json")

    def _load(self) -> dict:
        if self._state is None:
            try:
                with open(self.path(), encoding="utf-8") as f:
                    self._state = json.load(f)
            except Exception:
                self._state = {}
            self._state.setdefault("summary", "")
            self._state.setdefault("through_id", 0)
            self._state.setdefault("last_message_at", None)
        return self._state

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path()) or ".", exist_ok=True)
            tmp = self.path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.path())
        except Exception as e:
            logger.warning(f"Couldn't save the conversation summary ({e}).")

    def reset(self) -> None:
        with self._lock:
            self._state = {"summary": "", "through_id": 0, "last_message_at": None}
            self._save()

    # -- for the prompt -------------------------------------------------------
    def summary_line(self) -> str:
        """The summary for the system prompt, or "" (off, none yet, from an
        old conversation, or it mentions something the user asked to forget)."""
        if not self.enabled():
            return ""
        with self._lock:
            state = self._load()
            summary = (state.get("summary") or "").strip()
            last = _parse_ts(state.get("last_message_at"))
        if not summary or last is None or self._now() - last > timedelta(hours=SESSION_GAP_H):
            return ""
        if self._history_was_cleared(state.get("through_id") or 0):
            self.reset()                      # "clear chat history" clears this too
            return ""
        if self._mentions_forgotten(summary):
            return ""
        return summary

    @staticmethod
    def _history_was_cleared(through_id: int) -> bool:
        try:
            from database.db_manager import db  # noqa: PLC0415
            latest = db.latest_chat_id()
        except Exception:
            return False
        return latest is None or latest < through_id

    @staticmethod
    def _forgotten():
        try:
            from database.db_manager import db  # noqa: PLC0415
            return [v.lower() for v in db.get_forgotten_values() if v and len(v) >= 3]
        except Exception:
            return []

    def _mentions_forgotten(self, text: str) -> bool:
        low = (text or "").lower()
        return any(v in low for v in self._forgotten())

    # -- folding --------------------------------------------------------------
    def schedule_update(self) -> None:
        """Called at the start of each LLM turn; folds in the background
        when enough messages have dropped out of the window."""
        if not self.enabled():
            return
        with self._lock:
            if self._running or self._clock() - self._last_call < MIN_INTERVAL_S:
                return
            self._running = True
        threading.Thread(target=self._update_safely, name="RollingSummary", daemon=True).start()

    def _update_safely(self):
        try:
            self.update_now()
        except Exception as e:
            logger.info(f"Conversation summary skipped ({type(e).__name__}: {e})")
        finally:
            with self._lock:
                self._running = False

    def update_now(self, summarize=None) -> bool:
        """Folds the messages that left the window into the summary. Returns
        True when the summary changed. `summarize(previous, messages)` is the
        model call (tests pass their own)."""
        from database.db_manager import db  # noqa: PLC0415
        start = db.chat_window_start_id(WINDOW)
        if start is None:
            return False
        with self._lock:
            state = dict(self._load())
        rows = db.get_chat_messages_between(state.get("through_id") or 0, start)
        if len(rows) < FOLD_AFTER:
            return False
        cutoff = self._now() - timedelta(hours=SESSION_GAP_H)
        forgotten = self._forgotten()
        fresh = []
        for _id, role, message, ts in rows:
            when = _parse_ts(ts)
            if when is not None and when < cutoff:
                continue                      # an earlier conversation
            if any(v in (message or "").lower() for v in forgotten):
                continue
            fresh.append((role, (message or "")[:1200]))
        newest_id, newest_ts = rows[-1][0], rows[-1][3]
        previous = (state.get("summary") or "").strip()
        last = _parse_ts(state.get("last_message_at"))
        if last is not None and last < cutoff:
            previous = ""                     # that summary was another conversation
        summary = previous
        if fresh:
            self._last_call = self._clock()
            text = (summarize or self._summarize)(previous, fresh)
            if text:
                limit = int(getattr(config, "LLM_SUMMARY_MAX_CHARS", 600) or 600)
                summary = text.strip()[:limit]
        with self._lock:
            self._state = {"summary": summary, "through_id": newest_id,
                           "last_message_at": newest_ts if fresh else state.get("last_message_at")}
            self._save()
        if fresh:
            logger.info(f"Conversation summary updated with {len(fresh)} earlier message(s) ({len(summary)} chars).")
        return summary != (state.get("summary") or "").strip()

    @staticmethod
    def _summarize(previous: str, messages: list) -> str:
        from brain import llm_provider  # noqa: PLC0415
        from brain.model_router import background_model  # noqa: PLC0415
        cfg = llm_provider.resolve_provider()
        client = llm_provider.build_client(cfg)
        if client is None:
            return ""
        limit = int(getattr(config, "LLM_SUMMARY_MAX_CHARS", 600) or 600)
        lines = "\n".join(f"{'User' if role == 'user' else 'Wave'}: {msg}" for role, msg in messages)
        kwargs = dict(messages=[{"role": "user", "content": _PROMPT.format(
                          max_chars=limit, summary=previous or "(nothing yet)", messages=lines)}],
                      model=background_model(cfg), temperature=0.2, max_tokens=700, timeout=20)
        try:
            # gpt-oss would otherwise spend the budget reasoning about a summary
            response = client.chat.completions.create(extra_body={"reasoning_effort": "low"}, **kwargs)
        except Exception as e:
            if "429" in str(e) or "rate" in str(e).lower():
                raise
            response = client.chat.completions.create(**kwargs)
        return ((response.choices[0].message.content if response and response.choices else "") or "").strip()


rolling_summary = RollingSummary()
