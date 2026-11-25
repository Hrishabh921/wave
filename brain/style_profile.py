"""
brain/style_profile.py — Tier 41: answers in the user's own style.

SARA learns how its user likes to be answered from what they do: asking
for detail ("detail me", "samjhao") means longer answers, asking for brevity
("chhota batao") or cutting the reply off means shorter ones, and writing
Hindi in Roman letters means answering in Hinglish. Wave's prompt said none
of it - and live, Groq's model answered "hello wave, kaise ho?" in
Devanagari.

Two lines for the system prompt:

  * the language of THIS message decides the reply's (romanized Hinglish ->
    Hinglish in Roman letters, never Devanagari; Devanagari -> Hindi;
    English -> English); a message too short to tell goes by the user's
    usual script;
  * the length the user prefers, from counts of "detail" / "short" requests
    and interruptions, recent ones weighing more (each turn scales the
    counts by DECAY) - said only once a clear lean (|score| >= LEAN) shows.

Counts live in <DATA_DIR>/style_profile.json. LLM_STYLE_PROFILE=false stops
the learning and the length line (the language line always applies).
"""
import json
import logging
import os
import re
import threading

from settings.config import config

logger = logging.getLogger("StyleProfile")

DECAY = 0.97
LEAN = 2.0
_DETAIL = re.compile(r"\b(in detail|detail (me|mein|se)|detailed|elaborate|explain more|more detail|samjhao|samjhaiye|"
                     r"thoda lamba|vistar|vistaar se|poora batao|step by step)\b", re.I)
_BRIEF = re.compile(r"\b(short (me|mein)|in short|briefly|be brief|keep it short|too long|shorter|chhota|chota|"
                    r"kam shabdon|seedha batao|sidha batao|tl;?dr|one line|ek line)\b", re.I)


def _language_of(text: str) -> str:
    from brain.model_router import language_of  # noqa: PLC0415
    return language_of(text)


class StyleProfile:
    def __init__(self, path: str = None):
        self._path = path
        self._lock = threading.Lock()
        self._counts = None

    def path(self) -> str:
        return self._path or os.path.join(config.DATA_DIR, "style_profile.json")

    def _load(self) -> dict:
        if self._counts is None:
            try:
                with open(self.path(), encoding="utf-8") as f:
                    self._counts = {k: float(v) for k, v in json.load(f).items()}
            except Exception:
                self._counts = {}
        return self._counts

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path()) or ".", exist_ok=True)
            tmp = self.path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._counts, f)
            os.replace(tmp, self.path())
        except Exception as e:
            logger.debug(f"couldn't save the style profile: {e}")

    # -- what the user does ---------------------------------------------------
    @staticmethod
    def learning() -> bool:
        return bool(getattr(config, "LLM_STYLE_PROFILE", True))

    def observe_user_turn(self, text: str) -> None:
        text = (text or "").strip()
        if not text or not self.learning():
            return
        with self._lock:
            c = self._load()
            for k in list(c):
                c[k] *= DECAY
            if _DETAIL.search(text):
                c["detail"] = c.get("detail", 0.0) + 1
            if _BRIEF.search(text):
                c["brief"] = c.get("brief", 0.0) + 1
            if len(text.split()) >= 3:
                lang = _language_of(text)
                c[f"script_{lang}"] = c.get(f"script_{lang}", 0.0) + 1
            self._save()

    def note_interrupt(self) -> None:
        """The user cut Wave's reply off (barge-in) - a vote for shorter."""
        if not self.learning():
            return
        with self._lock:
            c = self._load()
            c["interrupts"] = c.get("interrupts", 0.0) + 1
            self._save()

    # -- for the prompt ---------------------------------------------------------
    def _usual_script(self, c: dict):
        scores = {k[len("script_"):]: v for k, v in c.items() if k.startswith("script_")}
        if not scores or max(scores.values()) < 2:
            return None
        return max(scores, key=scores.get)

    def style_lines(self, text: str) -> str:
        with self._lock:
            c = dict(self._load())
        words = len((text or "").split())
        lang = _language_of(text or "") if words >= 2 else (self._usual_script(c) or "en")
        lines = []
        if lang == "hinglish":
            lines.append("The user writes Hindi in Roman letters (Hinglish): reply the same way - Hinglish in "
                         "Roman/English letters, never in Devanagari script.")
        elif lang == "hi":
            lines.append("The user wrote in Hindi (Devanagari): reply in Hindi.")
        if self.learning():
            score = c.get("detail", 0.0) - c.get("brief", 0.0) - 0.5 * c.get("interrupts", 0.0)
            if score >= LEAN:
                lines.append("This user likes detailed, thorough answers - explain fully.")
            elif score <= -LEAN:
                lines.append("This user prefers short, to-the-point answers - keep replies brief unless they "
                             "ask for detail.")
        return "\n".join(lines)


style_profile = StyleProfile()
