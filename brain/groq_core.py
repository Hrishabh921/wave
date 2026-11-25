import logging
import datetime
import platform
import getpass
import sys
import os
import re
import ctypes
import time
import json
import threading
import base64
import io
import unicodedata
import pyautogui
from brain import llm_provider
from brain import confirm_gate
from brain import content_trust
from brain import followup_resolver, intent_router, unmatched_log
from brain import tiers
from brain import taint
from brain import exfiltration
# Tier 4: importing this registers every built-in skill - see its own
# __init__.py. adjust_system_volume/toggle_system_mute are the two plain
# (non-@skill) functions the keyword dispatch path below still calls
# directly; see brain/handlers/media.py's docstring for why the import
# runs in this direction.
from brain import handlers  # noqa: F401
from brain.handlers.media import adjust_system_volume, toggle_system_mute
import planning
from telemetry import trace
from settings.config import config
from database.db_manager import db
from brain.skills_registry import skills, skill, BUILTIN_SOURCE
from brain import tool_select
from brain.model_router import FAST, fast_model_for, model_router
from brain.rolling_summary import rolling_summary
from brain import response_cache
from brain import exact_answers
from brain import memory_context, people_graph
from brain import context_line, grounding
from brain.semantic_intents import semantic_intents
from brain.style_profile import style_profile
from automation.audit import audit
from automation import security_log
from automation.undo import undo_manager, startup_maintenance

from automation.app_control import AppController
from automation.file_manager import FileManager
from web.web_ops import WebManager
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr
from monitoring.sys_monitor import SystemMonitor
from automation.calendar_manager import CalendarManager
from automation.notes_rag import notes_index

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

try:
    import dateparser
    HAS_DATEPARSER = True
except ImportError:
    HAS_DATEPARSER = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

logger = logging.getLogger("GroqBrain")

# 🔧 FIX: process_actions() used to do a blind text.split(" and "), which cuts
# a single command in half whenever "and" is just a word inside it - e.g.
# "search for salt and pepper recipes" became "search for salt" + "pepper
# recipes" (the second half matches nothing, so only "salt" got searched).
# Now it only splits on " and " when what follows actually starts a new
# command, using the verbs _execute_single_action really understands.
_COMMAND_VERBS = (
    "open|start|launch|turn|switch|close|search|find|play|pause|stop|resume|next|previous|skip|"
    "take|capture|click|create|make|new|delete|write|type|lock|volume|increase|decrease|lower|"
    "mute|unmute|read|check|remember|forget|show|what|what's|whats|tell|send|set"
)
_COMMAND_SPLIT_RE = re.compile(r"\s+and\s+(?=(?:" + _COMMAND_VERBS + r")\b)")


def split_compound_command(text: str) -> list:
    """Splits 'open chrome and play music' into two commands, but leaves
    'search for salt and pepper recipes' as one."""
    return [part.strip() for part in _COMMAND_SPLIT_RE.split(text) if part.strip()]


# 🚨 REMOVED: the Sleep feature has been taken out entirely. Every attempt
# (rundll32, direct ctypes API, Force on/off, simulated hardware key) still
# either did nothing or actually shut the PC down on this hardware, which is
# a real firmware-level limitation of this system, not something fixable
# from an app.


# Exact phrases the keyword fallback treats as "undo the last thing you did".
_UNDO_PHRASES = frozenset({
    "undo", "undo that", "undo it", "undo last", "undo last action", "undo the last action",
    "undo my last action", "undo kar do", "undo karo", "wapas kar do", "revert that", "take that back",
})


class _TurnCancelled(Exception):
    """Tier 11: raised inside think_stream() when the caller's cancel_event
    is set (a voice "stop", a barge-in, a newer message superseding this
    one) - caught by think_stream itself, never seen by a caller. Tier 19:
    think() raises and catches the same exception for the same reason."""


# Appended to a reply that was cut off part-way, both in the saved chat
# history (so the model's next turn knows it didn't finish) and nowhere
# else - the bubble on screen shows its own "stopped" marker.
INTERRUPTED_MARKER = " … (interrupted)"


# ── Tier 29: rate limits ─────────────────────────────────────────────────
# Groq's free tier allows 8,000 tokens a minute PER MODEL and a reply often
# needs two requests, so "429 Too Many Requests" is routine. The SDK used to
# sit out each one (Retry-After, ~30 s, twice) before failing - replies took a
# minute or ended in "System Error". Now a rate-limited request goes straight
# to a backup model with its own quota (WaveBrain._llm_create), and only if
# both are limited does the user hear a plain "busy, ask again in N s".
_RATE_LIMIT_WORDS = ("rate limit", "rate_limit", "too many requests", "request too large",
                     "tokens per minute", "tokens per day", "requests per day", "requests per minute")


def _status_of(e):
    for obj in (e, getattr(e, "response", None)):
        for attr in ("status_code", "status"):
            v = getattr(obj, attr, None)
            if isinstance(v, int):
                return v
    return None


def _is_rate_limited(e) -> bool:
    """A 429 / 413 (or a provider error that says so in words)."""
    if _status_of(e) in (413, 429):
        return True
    s = str(e).lower()
    return any(w in s for w in _RATE_LIMIT_WORDS)


def _is_transient(e) -> bool:
    """Worth one quick retry on the same model: a 5xx or a dropped connection
    (never a timeout - a voice turn can't afford to wait twice)."""
    status = _status_of(e)
    if status is not None:
        return status >= 500
    name = type(e).__name__.lower()
    return "connection" in name and "timeout" not in name


def _retry_after_s(e, default: float = 20.0) -> float:
    """How long the provider asked us to stay away, clamped to 1..120 s."""
    value = getattr(e, "retry_after", None)
    if value is None:
        headers = getattr(getattr(e, "response", None), "headers", None)
        try:
            value = headers.get("retry-after") if headers is not None else None
        except Exception:
            value = None
    if value is None:
        m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(e))
        if m:
            value = int(m.group(1) or 0) * 60 + float(m.group(2))
    try:
        return min(120.0, max(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class _ToolLoopGovernor:
    """Phase 6: governance for the tool-calling loop in think() and
    _think_with_ollama(). Before this, a turn made exactly one round of
    tool calls no matter what the model wanted - a request needing two
    dependent steps ("check my battery, and if it's low remind me to
    charge it") had to be guessed in a single shot or not at all, since
    the old follow-up pass always forced tool_choice="none".

    This tracks a step count and a wall-clock deadline across however
    many rounds the model chains together, so a confused or genuinely-
    looping model can never hang a single turn - two independent bounds,
    either of which ends the loop. One instance per turn (per call to
    think()/_think_with_ollama()); never reused across turns, so there's
    no state to reset between them."""

    def __init__(self, max_steps: int = None, timeout_s: float = None):
        self.max_steps = config.TOOL_LOOP_MAX_STEPS if max_steps is None else max_steps
        self.deadline = time.monotonic() + (config.TOOL_LOOP_TIMEOUT_S if timeout_s is None else timeout_s)
        self.step = 0
        self.hit_limit = None  # "steps" or "time" once a bound stops the loop - for logging only

    def remaining_time(self) -> float:
        """Seconds left in the wall-clock budget - never negative."""
        return max(0.0, self.deadline - time.monotonic())

    def can_continue(self) -> bool:
        """False once either bound is hit. Call BEFORE starting a new round -
        a round already in flight is always allowed to finish rather than
        being cut off mid-call."""
        if self.step >= self.max_steps:
            self.hit_limit = self.hit_limit or "steps"
            return False
        if self.remaining_time() <= 0:
            self.hit_limit = self.hit_limit or "time"
            return False
        return True

    def start_round(self):
        """Call once per round, right before making the LLM request."""
        self.step += 1


# Guardrail appended to the system prompt for the final natural-language
# pass once every tool call is done (or the loop's step/time budget ran
# out) - the model's only job at this point is to phrase what already
# happened, in the user's own language and style, not re-decide anything
# or call more tools (tool_choice is "none" for this pass regardless).
# Factored out of _compose_reply_from_tools so both that pass and any
# future one can share identical wording instead of drifting apart.
TOOL_RESULT_GUIDANCE = (
    "\n\nTOOL RESULTS ARE NOW AVAILABLE. Reply to the user in one or two short, "
    "natural sentences, in the same language and style they used (Hinglish if they "
    "wrote Hinglish). Report exactly what the tool results say: never claim something "
    "succeeded unless a result says so, never invent extra actions, and if a result says "
    "something failed or could not be confirmed, say that plainly. If the results are "
    "informational (system info, clipboard, search results, files found, remembered "
    "facts), include the actual details rather than just saying it's done."
)


class WaveBrain:
    def __init__(self):
        # Set first so the Ollama fallback is available even when the
        # configured provider isn't usable at all.
        self._ollama_client = None

        # Phase 7: the one pending confirm-before-sensitive-action request,
        # if any - None, or a dict {"tool", "args", "source", "armed_at",
        # "expires_at"} armed by _guard()/_guard_keyword() and consumed by
        # _check_pending_confirmation() at the top of the next think() call.
        # Deliberately a single slot, not a queue: Tier 1's job is to make
        # sure a sensitive action never runs silently, not to manage several
        # simultaneous pending confirmations - see _skill_run_workflow's
        # comment for how a workflow with more than one gated step is
        # handled given that.
        self.pending_action = None

        # Tier 1 item #4: a freshly constructed brain starts with a clean
        # taint record on its thread - mainly so a test that builds a new
        # WaveBrain() in setUp() and then calls _dispatch_tool() directly
        # (bypassing think()/think_stream(), which also clear_turn() below)
        # never inherits taint an earlier, unrelated call left on this
        # same thread. See brain/taint.py's module docstring.
        taint.clear_turn()

        # Phase 4: WHICH LLM answers is decided by LLM_PROVIDER in .env (see
        # brain/llm_provider.py). With no LLM_* variables a legacy GROQ_API_KEY
        # still selects Groq - same models, same SDK - exactly as before.
        self.provider = llm_provider.resolve_provider()
        self.client = llm_provider.build_client(self.provider)
        self.model = self.provider.model
        self.vision_model = self.provider.vision_model
        # Tier 29: model -> time.monotonic() until which it's rate-limited,
        # and when local Ollama was last found not running.
        self._model_cooldown = {}
        self._ollama_down_until = 0.0
        # Tier 34: which model tier (brain/model_router.py) the turn on this
        # thread was routed to, and the model that last answered on it.
        self._route_tls = threading.local()
        if self.client is None:
            logger.error(f"LLM provider '{self.provider.name}' isn't usable: {self.provider.problem}")
        else:
            backup, fast = self._backup_model(), self._fast_model()
            logger.info(f"LLM provider: {self.provider.name} / {self.model} @ {self.provider.host}"
                        + (f" (backup when rate-limited: {backup})" if backup else "")
                        + (f"; everyday turns: {fast} (LLM_ROUTER={model_router.mode()})"
                           if fast and model_router.mode() != "off" else ""))

        # Purge expired trash + warm the audit cache without delaying startup.
        threading.Thread(target=startup_maintenance, name="WaveMaintenance", daemon=True).start()
        # Tier 43: the installed-apps index "open X" launches from (Windows;
        # the saved one at once, a fresh one in the background).
        try:
            from automation.app_index import app_index  # noqa: PLC0415
            app_index.warm_up()
        except Exception:
            logger.debug("app index warm-up failed", exc_info=True)
        # Tier 45: the sentence model + example phrases behind "make it
        # louder"-style commands, loaded in the background.
        semantic_intents.warm_up()
        # Tier 46: set aside long-unused picked-up facts (memory decay).
        if getattr(config, "MEMORY_DECAY", True):
            threading.Thread(target=db.archive_weak_memories, name="WaveMemoryDecay", daemon=True).start()

        # Phase 6: build/refresh the search_my_notes index in the background
        # too - first run over a notes-heavy folder can take a few seconds
        # (reading + embedding every file), which should never delay the UI
        # appearing.
        if config.NOTES_RAG_ENABLED:
            threading.Thread(target=notes_index.reindex_all, name="WaveNotesIndex", daemon=True).start()

    def provider_info(self) -> dict:
        """What Settings shows: provider, model, endpoint host, masked key, and the problem if any."""
        return self.provider.describe()

    def check_provider(self):
        """(ok, message, latency_ms). Blocks on the network - call from a worker thread."""
        return llm_provider.check_connection(self.client, self.provider)

    def get_dynamic_system_prompt(self, user_text: str = ""):
        now = datetime.datetime.now()
        current_time = now.strftime("%I:%M %p")
        current_date = now.strftime("%A, %d %B %Y")
        user_name = getpass.getuser()
        
        os_system = platform.system()
        os_release = platform.release()
        if os_system == "Windows" and os_release == "10":
            if hasattr(sys, 'getwindowsversion') and sys.getwindowsversion().build >= 22000:
                os_release = "11"
        os_name = f"{os_system} {os_release}"

        # 🔧 FIX: the `memory` table existed with nothing reading from it -
        # this is what actually gives the AI ambient awareness of anything
        # it's been asked to remember, without a separate tool call needed
        # just to check.
        #
        # 🔧 Phase 3: get_relevant_memory() still returns every remembered
        # fact (same as get_all_memory() always did) while the list is short
        # enough that dumping all of it is cheap - it only switches to
        # semantic filtering once there's enough remembered that doing so
        # stops being free, and it falls back to the exact old behavior on
        # its own if semantic embeddings aren't available in this
        # environment. See database/embeddings.py and db_manager.py.
        # Tier 35 (Memory 2.0): a changed fact shows what it was before, and
        # the model is told not to make up personal details it wasn't given.
        # Tier 46: the people the message names come along (people graph),
        # and a question about the past gets the user's own dated messages
        # from then - all in brain/memory_context.py, which the memory
        # bench reads too.
        memory_section = memory_context.section(user_text, user_name)

        # Phase 6: same ambient-awareness idea as memory_section above, but
        # for today's calendar - so "what's next on my day" or "am I free
        # at 3" doesn't need an explicit list_calendar_events call just to
        # check, the same way remembered facts don't need recall_memory.
        todays_events = CalendarManager.get_events_for_prompt()
        calendar_section = f"\nTODAY'S CALENDAR:\n{todays_events}\n" if todays_events else ""
        # Tier 38: what was said before the messages that still go with this
        # request (brain/rolling_summary.py) - and fold the ones that just
        # dropped out, in the background, for next time.
        summary = rolling_summary.summary_line()
        if summary:
            calendar_section += f"\nEARLIER IN THIS CONVERSATION (older messages, summarised):\n{summary}\n"
        rolling_summary.schedule_update()
        # Tier 41: the reply's language (this message's script) and the length
        # this user prefers (brain/style_profile.py).
        style = style_profile.style_lines(user_text)
        style_section = f"\nSTYLE:\n{style}\n" if style else ""
        # Tier 47: the app in front and the battery (brain/context_line.py).
        pc_line = context_line.build()
        pc_line = f"{pc_line}\n" if pc_line else ""

        return {
            "role": "system",
            "content": (
                f"You are Wave, an elite AI Desktop Assistant for {user_name}.\n"
                f"System: {os_name} | Date: {current_date} | Time: {current_time}.\n"
                f"{pc_line}"
                f"{memory_section}"
                f"{calendar_section}"
                f"{style_section}"
                f"RULES:\n"
                f"1. Never hallucinate sending messages or taking actions unless explicitly shown in chat history.\n"
                f"2. NEVER invent or fake system information. If you don't have the data, say you don't know. "
                f"For anything time-sensitive - latest, current, today's, prices, scores, news, who holds a "
                f"post now - call get_live_information first instead of answering from memory: what you "
                f"learned in training is months old.\n"
                f"3. Match your depth to the question: answer simple questions in a couple of sentences, "
                f"but for anything technical, multi-step, or open-ended, think it through properly and give "
                f"a complete, well-reasoned answer - don't sacrifice correctness or thoroughness just to be brief.\n"
                f"4. When a command names a WEBSITE and/or a specific BROWSER together — e.g. "
                f"'search youtube on brave', 'open google in chrome', 'search amazon for shoes using edge' — "
                f"always call open_website with every piece the user gave you filled in at once (site, and "
                f"query if they gave search terms, and browser if they named one). Never drop the site to "
                f"satisfy the browser or vice versa, and never call open_app for this — open_app is only for "
                f"launching a plain application/program by name, not a website.\n"
                f"5. Whenever you share code, commands, or config, always put it inside a fenced markdown "
                f"code block with the language name right after the opening ``` (e.g. ```python), so it "
                f"renders with proper formatting and syntax highlighting.\n"
                f"6. send_whatsapp_message already opens/focuses WhatsApp itself if needed - never call "
                f"open_app for WhatsApp first and wait. When one command bundles opening WhatsApp together "
                f"with sending someone a message (e.g. 'whatsapp open karke Rohan ko hi bhejo', 'open "
                f"whatsapp and message Rohan hi'), call send_whatsapp_message directly in that same turn "
                f"with both the contact and the message already filled in - don't split it into two turns.\n"
                f"7. If the user tells you something worth remembering for later (their name, a stated "
                f"preference, a recurring detail), call remember_this to actually save it - don't just "
                f"acknowledge it in the reply and lose it next session. The facts already remembered are "
                f"listed above; use recall_memory only if you need to check something not already listed.\n"
                f"8. create_workflow saves a named sequence of commands so the user can replay all of them "
                f"at once later with run_workflow (e.g. a 'good morning' routine) - use it when they ask to "
                f"save/create a routine, macro, or workflow, not for a normal one-off request.\n"
                f"9. set_reminder covers both 'remind me to...' and 'set an alarm for...' - pass the time "
                f"expression through to the `when` argument exactly as the user said it (e.g. 'in 10 "
                f"minutes', 'tomorrow at 5pm', '7am') rather than converting it yourself; dateparser on the "
                f"other end handles the actual parsing.\n"
                f"10. undo_last_action reverses the most recent file/folder/screenshot change you made. Call it ONLY "
                f"when the user explicitly asks to undo, revert or take back what you just did (e.g. 'undo that', "
                f"'wapas kar do') - never on your own initiative. list_recent_actions answers 'what did you just do?'.\n"
                f"11. create_calendar_event/list_calendar_events/cancel_calendar_event manage actual scheduled "
                f"events (meetings, appointments, classes) - different from set_reminder, which is a one-off "
                f"alarm/nudge with no duration or location. Today's events are already listed above if there are "
                f"any; only call list_calendar_events for a day you don't already have listed.\n"
                f"12. search_my_notes searches the user's own files (txt/md/PDF notes on their Desktop, Documents, "
                f"Downloads, and any configured notes folder) - call it whenever they ask you to check, search, or "
                f"answer from 'my notes'/'my files', instead of saying you can't access their files.\n"
                f"13. daily_briefing combines weather + today's calendar + today's reminders into one summary - "
                f"use it for 'good morning'/'what's my day look like' style requests rather than calling three "
                f"separate tools yourself. run_self_diagnostics checks whether the LLM provider, database, disk, "
                f"and voice stack are actually working - use it for 'is everything okay'/'why isn't voice "
                f"working' style requests.\n"
                f"14. schedule_workflow is for 'run my X workflow every morning at 7'/'run the backup workflow "
                f"next Monday' - a workflow that should fire on its OWN later, with no further request from the "
                f"user. It's separate from run_workflow (which runs one right now, on demand) and from "
                f"set_reminder (which just tells the user something, rather than running a saved sequence of "
                f"commands) - don't try to pass a time into run_workflow's own arguments, it doesn't take one. "
                f"Pass when_text through exactly as the user said it, same as set_reminder's `when`."
            )
        }

    def _execute_single_action(self, cmd: str) -> str:
        # Phase 4: one audit row per matched command. When the command's backend
        # call logged itself (create_file, take_screenshot, ...) this wrapper row
        # collapses away; when nothing matched it records nothing at all.
        with audit.action("keyword_command", {"text": cmd}, source="keyword", collapse_if_children=True) as act:
            result = self._execute_single_action_inner(cmd)
            if result is None:
                act.skip()
                return None
            return act.done(result)

    def _execute_single_action_inner(self, cmd: str) -> str:
        cmd_lower = cmd.lower().strip()
        if cmd_lower in _UNDO_PHRASES:
            return undo_manager.undo_last(source="keyword").message
        
        if cmd_lower in ["hi", "hii", "hello", "hey", "ok", "thanks"]:
            return None 

        if any(phrase in cmd_lower for phrase in ["close window", "close current window", "close this window", "close all windows"]):
            return AppController.close_current_window()
            
        elif "close tab" in cmd_lower:
            return AppController.close_current_tab()

        elif any(phrase in cmd_lower for phrase in ["pc info", "system info", "about my pc", "system stats", "hardware info", "how's my pc", "check my system"]):
            return SystemMonitor.get_hardware_info()

        elif any(phrase in cmd_lower for phrase in ["read the screen", "read screen", "screen text", "what does the screen say", "read text on screen", "what's on my screen"]):
            path = VisionCapture.take_screenshot()
            if not path:
                return "I couldn't capture the screen to read it."
            extracted = ocr.extract_text(path)
            return extracted if extracted else "I captured the screen but couldn't find any readable text on it."

        elif any(phrase in cmd_lower for phrase in ["lock the pc", "lock my pc", "lock the computer", "lock my computer", "lock windows", "lock the screen"]):
            return AppController.lock_pc()

        elif any(phrase in cmd_lower for phrase in ["sleep the pc", "sleep my pc", "put the pc to sleep", "put my pc to sleep", "sleep mode", "sleep my computer"]):
            return ("Sleep isn't available on this PC — the hardware doesn't support it reliably "
                    "(it either does nothing or shuts down instead), so this feature has been removed.")

        elif any(phrase in cmd_lower for phrase in ["restart the pc", "restart my pc", "restart the computer", "restart my computer", "reboot"]):
            return ("For safety, I won't restart your PC directly from chat — head to the "
                    "Apps & Power page and use the Restart button there, which will ask you to confirm first.")

        elif any(phrase in cmd_lower for phrase in ["shutdown the pc", "shut down the pc", "shutdown my pc", "shut down my pc",
                                                       "turn off the pc", "turn off my computer", "power off"]):
            return ("For safety, I won't shut down your PC directly from chat — head to the "
                    "Apps & Power page and use the Shutdown button there, which will ask you to confirm first.")

        elif any(phrase in cmd_lower for phrase in ["pause music", "pause the music", "pause song", "stop the music",
                                                       "resume music", "resume the music", "unpause music", "resume song"]):
            pyautogui.press('playpause')
            return "Toggled play/pause on your media player."

        elif any(phrase in cmd_lower for phrase in ["next song", "next track", "skip song", "skip track", "skip this song"]):
            pyautogui.press('nexttrack')
            return "Skipped to the next track."

        elif any(phrase in cmd_lower for phrase in ["previous song", "previous track", "last song", "go back a song", "play the last track"]):
            pyautogui.press('prevtrack')
            return "Went back to the previous track."

        elif any(phrase in cmd_lower for phrase in ["volume up", "increase volume", "turn up the volume", "louder"]):
            if not adjust_system_volume(10):
                pyautogui.press('volumeup')
            return "Turned the volume up."

        elif any(phrase in cmd_lower for phrase in ["volume down", "decrease volume", "turn down the volume", "quieter", "lower the volume"]):
            if not adjust_system_volume(-10):
                pyautogui.press('volumedown')
            return "Turned the volume down."

        elif any(phrase in cmd_lower for phrase in ["mute the volume", "mute the sound", "mute audio", "unmute"]):
            if not toggle_system_mute():
                pyautogui.press('volumemute')
            return "Toggled mute on your system audio."

        elif any(phrase in cmd_lower for phrase in ["what's in my clipboard", "what is in my clipboard", "read my clipboard", "read clipboard", "what did i copy"]):
            if not HAS_PYPERCLIP:
                return "Clipboard reading needs the 'pyperclip' package installed."
            try:
                content = pyperclip.paste()
                return f"Your clipboard has: {content}" if content else "Your clipboard is empty right now."
            except Exception:
                return "I couldn't read the clipboard right now."

        elif any(phrase in cmd_lower for phrase in ["create a folder", "make a folder", "new folder"]):
            folder_name = cmd_lower
            for phrase in ["create a folder called", "create a folder named", "create a folder",
                           "make a folder called", "make a folder named", "make a folder", "new folder"]:
                if phrase in folder_name:
                    folder_name = folder_name.split(phrase, 1)[1].strip()
                    break
            return FileManager.create_folder(folder_name if folder_name else "New Folder")

        elif any(phrase in cmd_lower for phrase in ["create a file", "make a file", "new file"]):
            file_name = cmd_lower
            for phrase in ["create a file called", "create a file named", "create a file",
                           "make a file called", "make a file named", "make a file", "new file"]:
                if phrase in file_name:
                    file_name = file_name.split(phrase, 1)[1].strip()
                    break
            final_file_name = file_name if file_name else "New File.txt"
            gate = self._guard_keyword("create_file", {"file_name": final_file_name})
            if gate:
                return gate
            return FileManager.create_file(final_file_name)

        elif "delete" in cmd_lower and "screenshot" in cmd_lower:
            gate = self._guard_keyword("delete_latest_screenshot", {})
            if gate:
                return gate
            return AppController.delete_latest_screenshot()

        elif "delete" in cmd_lower and "file" in cmd_lower:
            file_name = cmd_lower
            for phrase in ["delete the file called", "delete the file named", "delete file called",
                           "delete file named", "delete the file", "delete file"]:
                if phrase in file_name:
                    file_name = file_name.split(phrase, 1)[1].strip()
                    break
            if file_name:
                gate = self._guard_keyword("delete_file", {"file_name": file_name})
                if gate:
                    return gate
                return FileManager.delete_file(file_name)
            return "Which file would you like me to delete?"

        elif any(phrase in cmd_lower for phrase in ["search for a file", "find a file", "search file", "find file"]):
            file_name = cmd_lower
            for phrase in ["search for a file called", "search for a file named", "search for a file",
                           "find a file called", "find a file named", "find a file",
                           "search file", "find file"]:
                if phrase in file_name:
                    file_name = file_name.split(phrase, 1)[1].strip()
                    break
            if file_name:
                return FileManager.search_file(file_name)
            return "Which file would you like me to search for?"

        elif any(phrase in cmd_lower for phrase in ["take screenshot", "take a screenshot", "capture screen", "screenshot"]):
            return AppController.take_screenshot()

        elif "click" in cmd_lower and ("picture" in cmd_lower or "photo" in cmd_lower):
            gate = self._guard_keyword("take_photo", {})
            if gate:
                return gate
            return VisionCapture.take_photo()

        elif "check windows update" in cmd_lower or "windows update" in cmd_lower:
            return AppController.open_system_setting("update")
            
        elif any(hw in cmd_lower for hw in ["hotspot", "bluetooth", "wifi"]) and any(action in cmd_lower for action in ["turn on", "turn off", "switch on", "switch off"]):
            return AppController.toggle_hardware(cmd_lower)

        elif any(word in cmd_lower for word in ["display", "sound", "battery", "settings"]):
            return AppController.open_system_setting(cmd_lower)

        # 🚨 THE NEW PLAY LOGIC 🚨
        elif cmd_lower.startswith("play "):
            song_query = cmd_lower.replace("play ", "", 1).strip()
            return WebManager.play_on_youtube(song_query)

        elif "write a note" in cmd_lower or "type in notepad" in cmd_lower:
            note_text = cmd_lower.replace("write a note saying", "").replace("type in notepad", "").strip()
            return AppController.write_in_notepad(note_text)

        elif any(cmd_lower.startswith(w) for w in ["open ", "start ", "launch ", "turn on "]):
            app_name = ""
            for w in ["open ", "start ", "launch ", "turn on "]:
                if cmd_lower.startswith(w):
                    app_name = cmd_lower.replace(w, "", 1).strip()
                    break
            return AppController.open_app(app_name)
            
        elif cmd_lower.startswith("close "):
            app_name = cmd_lower.replace("close ", "").strip()
            gate = self._guard_keyword("close_app", {"app_name": app_name})
            if gate:
                return gate
            return AppController.close_app(app_name)
            
        elif cmd_lower.startswith("search "):
            query = cmd_lower.replace("search for", "").replace("search", "", 1).strip()
            # 🚨 NEW: pull out a trailing named browser ("... on brave",
            # "... in chrome") so this simple keyword fallback also opens
            # the right browser, not just the AI tool-calling path.
            browser = None
            for b in ("brave", "chrome", "edge", "firefox", "opera"):
                for prep in (f" on {b}", f" in {b}", f" using {b}"):
                    if query.endswith(prep):
                        browser = b
                        query = query[: -len(prep)].strip()
                        break
                if browser:
                    break
            if browser:
                site, _, rest = query.partition(" for ")
                return AppController.open_website(site.strip() or query, rest.strip() or None, browser)
            if "youtube" in query:
                return WebManager.play_on_youtube(query.replace("on youtube", "").strip())
            else:
                return WebManager.search_google(query)
                
        elif cmd_lower.startswith("remember "):
            rest = cmd_lower[len("remember "):].strip()
            if rest.startswith("that "):
                rest = rest[len("that "):].strip()
            if rest.startswith("my "):
                rest = rest[len("my "):].strip()
            if " is " in rest:
                key, _, value = rest.partition(" is ")
                if key.strip() and value.strip():
                    # Tier 20 provenance - same as brain/handlers/memory.py's
                    # remember_this skill, for the keyword-path equivalent.
                    ok = db.save_memory(key.strip(), value.strip(), source="explicit",
                                         turn_id=trace.current_turn_id())
                    return f"Got it, I'll remember that {key.strip()} is {value.strip()}." if ok else "Couldn't save that to memory."
            return "Tell me what to remember, like 'remember my favorite color is blue'."

        elif cmd_lower.startswith("forget "):
            key = cmd_lower[len("forget "):].strip()
            if key.startswith("my "):
                key = key[len("my "):].strip()
            if key:
                gate = self._guard_keyword("forget_memory", {"key": key})
                if gate:
                    return gate
                return f"Forgot what I knew about '{key}'." if db.delete_memory(key) else f"Couldn't forget '{key}'."
            return "Tell me what to forget."

        return None

    def process_actions(self, user_text: str) -> str:
        text_lower = user_text.lower()
        if "whatsapp" in text_lower and "send" in text_lower and " to " in text_lower:
            try:
                part1 = text_lower.split("send ")[1] 
                message = part1.split(" to ")[0].strip() 
                contact = part1.split(" to ")[1].replace("on whatsapp", "").strip()
                gate = self._guard_keyword("send_whatsapp_message", {"contact_name": contact, "message": message})
                if gate:
                    return gate
                return AppController.send_whatsapp_message(contact, message)
            except Exception: pass

        commands = split_compound_command(text_lower)
        results = [res for cmd in commands if (res := self._execute_single_action(cmd))]
        return " & ".join(results) if results else None 

    def _think_fallback(self, user_text: str) -> str:
        """Deterministic keyword matching — no LLM/network needed. Used as a
        safety net if the smart tool-calling call errors out, and used
        directly when there's no API key at all."""
        text_lower = user_text.lower()

        action_result = self.process_actions(user_text)
        if action_result:
            return action_result

        if not self.client:
            return "I don't have an API key configured, so I can only handle direct commands like 'open chrome' or 'take a screenshot' right now."

        internet_context = ""
        search_keywords = ["search", "latest", "news", "who won", "price", "weather", "who is", "what is"]
        skip_words = ["time", "date", "today", "now", "day", "play"]
        if any(k in text_lower for k in search_keywords) and not any(skip in text_lower for skip in skip_words):
            query = text_lower.replace("search for", "").replace("search", "").strip()
            if live_data := WebManager.ask_internet(query):
                # Phase 7: same reasoning as _skill_get_live_information -
                # this is web content, not the user's own words.
                internet_context = "\n\n" + content_trust.wrap_untrusted(live_data, source="a web search")

        messages = [self.get_dynamic_system_prompt(user_text)]
        history = self._recent_history(limit=8)
        for role, msg in history:
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": user_text + internet_context})

        chat_completion = self._llm_create(
            messages=messages, model=self.model, temperature=0.6, max_tokens=1600,
        )
        return chat_completion.choices[0].message.content

    # ---------------------------------------------------------------
    def _dispatch_tool(self, name: str, arguments_json, source: str = "ai") -> str:
        """Executes exactly one tool call against the real backend and
        returns a short natural-language result string.

        FIX: arguments_json arrives in two different shapes depending on
        which brain produced the tool call. Groq (OpenAI-style) hands back a
        JSON-encoded string, which is what this used to assume unconditionally.
        The ollama python client (verified against the real ollama==0.6.3
        package) hands back an already-parsed dict instead - running that
        through json.loads() raises TypeError, which the bare except below
        used to swallow silently, discarding every argument and making each
        Ollama-dispatched tool call run as if no arguments were given at all.
        Both shapes are handled explicitly now, so this one dispatcher serves
        both the Groq path and the Ollama fallback path unchanged.

        Phase 3: this used to be a 150-line if/elif chain checking `name`
        against all 38 tools one by one, with each tool's behavior living
        here and its Groq-facing schema living ~400 lines away in the TOOLS
        list. Both now live together as one @skill(...)-decorated method
        each - see the _skill_* methods below, and brain/skills_registry.py
        for the registry itself. This method is now just argument
        normalization plus a single table lookup - adding tool #39 means
        writing one new decorated method here, nothing else to keep in
        sync.

        Tier 2: `source` defaults to "ai" (an LLM tool_call, same as
        always) but brain/intent_router.py's pre-LLM fast path passes
        "intent_router" instead, so the audit trail can tell the two
        apart - the gate/validation/execution below are identical either
        way; only the recorded source changes.
        """
        if isinstance(arguments_json, dict):
            args = arguments_json
        else:
            try:
                args = json.loads(arguments_json) if arguments_json else {}
            except Exception:
                args = {}

        handler = skills.get_handler(name)
        if handler is None:
            return f"I don't have a '{name}' tool available."
        # Phase 6: catch a tool call missing a required argument before it
        # ever reaches the handler. Previously a missing arg silently became
        # "" deep inside whichever _skill_* method happened to read it (e.g.
        # args.get("title", "")) - now the model gets told immediately, as a
        # tool result it can react to on the next round, instead of the
        # action quietly running wrong or empty.
        validation_error = self._validate_tool_args(name, args)
        if validation_error:
            return validation_error
        # Phase 4: one audit row per tool call. A backend that knows more (the undo
        # recipe for a file delete, say) adopts this scope - see automation/audit.py.
        with audit.action(name, args, source=source, adoptable=True) as act:
            # Phase 7 / Tier 1: gated tools (delete_file,
            # send_whatsapp_message, ...) never run on this first ask, no
            # matter whether the model decided to call them on its own,
            # something it read (a web result, OCR'd screen text, an
            # attached file) suggested it, or the Tier 2 intent router
            # matched it deterministically - see _guard() above and
            # brain/content_trust.py/brain/taint.py for the other half of
            # that defense. A "deny" outcome reads identically to
            # "confirm" here (both are a blocked row - the message text is
            # what tells the person which one happened); what differs is
            # entirely inside _guard() itself, in whether self.pending_action
            # got armed.
            gate = self._guard(name, args, source=source)
            if gate.action != "run":
                return act.blocked(gate.message)
            try:
                return act.done(handler(self, args))
            except Exception as e:
                logger.error(f"Tool '{name}' failed: {e}")
                return act.fail(f"Something went wrong while trying to do that: {e}")

    # Tier 20 (#89): docs/SARA_GAP_ANALYSIS.md names SARA's
    # validate_app_target() as a cut feature; planning/schema.py's own
    # docstring already explains why it was never duplicated wholesale
    # (the concern it serves - a tool about to act on an app/process/
    # window/service name it shouldn't - is handled at the shared choke
    # point, automation/app_control.py's _CRITICAL_PROCESS_NAMES/
    # _CRITICAL_SERVICE_NAMES, not re-checked here). What WAS still
    # missing is the "(Unicode-aware)" half: those two frozensets are
    # matched with a plain .lower() against the arg text, which treats
    # e.g. the fullwidth/compatibility form "ｅｘｐｌｏｒｅｒ" or a
    # Unicode-tag-smuggled "explorer" as a totally different string from
    # plain "explorer" - NOT a bypass of the critical-process check
    # itself (close_app only ever acts on a REAL, already-plain-ASCII
    # process name resolved via psutil - see that function's own
    # comments), but a real gap for anything that matches the arg text
    # directly, like stop_service()'s _normalize_service_name(), and for
    # the window-name substring matches focus_window/minimize_window/
    # maximize_window do against real (plain) window titles, where a
    # disguised target could fail to match a window it should have, or
    # (via some other substring) match one it shouldn't.
    #
    # Normalizing the handful of app/process/window/service-target
    # arguments HERE, before they reach any handler, fixes that for every
    # tool that takes one at once - no second copy of either frozenset,
    # no new function SARA-style; just the one shared choke point
    # _validate_tool_args() already is, made Unicode-aware. NFKC folds
    # compatibility variants (fullwidth forms, ligatures, etc.) down to
    # their plain equivalent; reusing brain/content_trust.py's own
    # _INVISIBLE_CHARS_RE (Tier 17) strips zero-width/bidi/Unicode-tag
    # characters the same way that scanner already does for untrusted
    # content, rather than writing a second copy of that regex here too.
    # Deliberately NOT attempting cross-script homoglyph folding (Cyrillic
    # "е" for Latin "e" and so on) - there is no bundled confusables table
    # to lean on without a new dependency, and unlike the compatibility
    # forms above, a genuine cross-script homoglyph still can't actually
    # BYPASS anything downstream, since every one of these checks either
    # needs the literal plain-ASCII name to match a real OS object, or (the
    # critical-name frozensets) only ever gets reached via that same real,
    # already-plain name.
    _APP_TARGET_ARGS = {
        "open_app": ("app_name",), "close_app": ("app_name",),
        "focus_window": ("window_name",), "minimize_window": ("window_name",),
        "maximize_window": ("window_name",),
        "start_service": ("service_name",), "stop_service": ("service_name",),
    }

    @staticmethod
    def _normalize_app_target(value: str) -> str:
        """Unicode-compatibility-normalize + strip invisible characters from
        an app/process/window/service name argument. Never raises - worst
        case returns the value unchanged, same fail-open posture as every
        other Tier 20 text-safety helper."""
        try:
            text = content_trust._INVISIBLE_CHARS_RE.sub("", value or "")
            return unicodedata.normalize("NFKC", text).strip()
        except Exception:
            return value

    def _validate_tool_args(self, name: str, args: dict) -> str:
        """None if `args` satisfies `name`'s required parameters; otherwise a
        short message naming what's missing. Blank/whitespace-only strings
        count as missing, same as not being passed at all. Unknown tool
        names are not this function's concern - _dispatch_tool already
        reports those before this ever runs.

        Tier 20 (#89): also Unicode-normalizes any app/process/window/
        service-target argument IN PLACE (see _APP_TARGET_ARGS above) -
        `args` is the same dict object _dispatch_tool() goes on to pass to
        _guard() and the handler, so this one pass is enough for every
        downstream critical-name check to see the normalized form too."""
        for key in self._APP_TARGET_ARGS.get(name, ()):
            if key in args and isinstance(args[key], str):
                args[key] = self._normalize_app_target(args[key])
        spec = skills.get_spec(name)
        if not spec:
            return None
        required = spec.get("function", {}).get("parameters", {}).get("required", []) or []
        missing = [p for p in required if not str(args.get(p, "")).strip()]
        if not missing:
            return None
        return f"Missing required argument(s) for '{name}': {', '.join(missing)}."

    # ---------------------------------------------------------------
    # Phase 7 / Tier 1: confirm-before-sensitive-action gate.
    #
    # _guard() is the one decision point both dispatch paths call through -
    # _dispatch_tool() (every AI/Ollama tool call) and the deterministic
    # keyword path (_execute_single_action_inner / process_actions), which
    # otherwise call straight into AppController/FileManager/db with no
    # shared choke point at all. A tool replayed via _skill_run_workflow
    # goes through process_actions() exactly like a typed command does, so
    # gating the keyword path here is also what "redirect the
    # workflow-replay path's sensitive branches through the same gate"
    # means - nothing workflow-specific was needed once both dispatchers
    # call _guard()/_guard_keyword() themselves.
    #
    # Tier 1 items #3-#5: _guard() used to be a flat confirm-or-not read
    # off skills.requires_confirm(). It now asks two questions instead of
    # one - "how risky IS this tool" (brain/tiers.py) and "did this turn
    # already read something untrusted" (brain/taint.py) - and can refuse
    # outright, not just ask, when both answers are bad at once. See
    # brain/confirm_gate.py's GuardOutcome and module docstring for what
    # the three outcomes mean to a caller.
    # ---------------------------------------------------------------
    def _guard(self, tool_name: str, args: dict, source: str) -> confirm_gate.GuardOutcome:
        """Decides whether `tool_name` runs now, needs the user to confirm
        it first, or gets refused outright. Never executes anything
        itself - purely a decision, left for the caller
        (_dispatch_tool()/_guard_keyword()) to act on.

        RISK_TIER_GUARD_ENABLED=false restores the exact pre-Tier-1
        behaviour: a flat confirm-or-not read straight off
        skills.requires_confirm(), nothing about tiers or taint consulted
        at all - a full rollback if the tiered version ever needs to come
        out in a hurry.

        With it on (the default), brain/tiers.py's tier for this tool
        decides the BASELINE for an ordinary, untainted turn - chosen so
        every tool that already required confirmation before this file
        existed still does, no behaviour change there - and
        brain/taint.py's turn_tainted() can escalate that baseline for
        the rest of a turn that already read something untrusted:

            tier   not tainted     tainted this turn
            T0     run             run      (nothing to protect - a pure read)
            T1     run             confirm  (the new property item #4 was
                                              for: normally-auto-run, but
                                              something this turn wasn't
                                              fully trusted, so ask first)
            T2     confirm         confirm  (already asks; taint adds nothing
                                              further at this tier)
            T3     confirm         deny     (item #5: refusing outright beats
                                              asking, when the only reason a
                                              destructive call is happening
                                              might be a web page/OCR'd
                                              screen/attachment, not the
                                              person talking to Wave)

        Tier 15 adds one exception on top of that table: the keyboard
        surface (tiers.TAINT_DENY_TOOLS - press_key/type_text/copy_text/
        paste_text/select_all_text/undo_keystroke/redo_keystroke) gets
        T3's "tainted -> deny" even though its own baseline tier is T1, so
        ordinary use stays confirm-free while a tainted turn still can't
        use it to type/send keystrokes into whatever's focused. See that
        set's own comment in brain/tiers.py.
        """
        if not config.RISK_TIER_GUARD_ENABLED:
            if not skills.requires_confirm(tool_name):
                return confirm_gate.GuardOutcome("run")
            return self._arm_confirm(tool_name, args, source)

        tier = tiers.resolve_tier(tool_name, is_registered=tool_name in skills.names(),
                                  legacy_confirm=skills.requires_confirm(tool_name))
        tainted = taint.turn_tainted()

        # Tier 15: the keyboard surface sends real input to whatever
        # currently has focus, which Wave has no way to identify - a
        # stronger risk than its own T1 tier implies for the (hopefully
        # rare) turn that also read something untrusted. Checked before
        # the normal tier branch, and only for the tools in
        # tiers.TAINT_DENY_TOOLS - every other T1 tool keeps the ordinary
        # "ask first" escalation from the table in this method's own
        # docstring.
        if tool_name in tiers.TAINT_DENY_TOOLS and tainted:
            message = confirm_gate.denied_message(tool_name, args or {}, taint.turn_sources())
            security_log.record("guard_deny", reason=message, tool=tool_name, source=source,
                                 tier="T1(keyboard)", tainted=True, taint_sources=taint.turn_sources())
            return confirm_gate.GuardOutcome("deny", message)

        # Tier 20: the exfiltration-URL check - part of the policy matrix
        # (tier x origin x taint x mode). Checked before the normal tier
        # branch, same pattern as TAINT_DENY_TOOLS above, and only for
        # tools in tiers.URL_ARG_TOOLS (see that set's own comment for why
        # a T0 "always runs" tool like summarize_page still needs this).
        # SECURITY_MODE="off" disables this specific guard (the underlying
        # tier/taint gate above is unaffected either way).
        mode = security_log.effective_mode()
        url_arg = tiers.URL_ARG_TOOLS.get(tool_name)
        if url_arg and tainted and mode != "off":
            url = (args or {}).get(url_arg) or ""
            reason = exfiltration.looks_like_exfiltration(url)
            if reason:
                message = (f"I'm not going to do that - {reason}, and this conversation already "
                           f"read something untrusted ({', '.join(taint.turn_sources())}), so this "
                           f"looks like it could be trying to send data somewhere it shouldn't.")
                security_log.record("guard_deny", reason=message, tool=tool_name, source=source,
                                     tier="exfiltration", tainted=True, taint_sources=taint.turn_sources())
                return confirm_gate.GuardOutcome("deny", message)

        if tier == tiers.T0:
            return confirm_gate.GuardOutcome("run")
        if tier == tiers.T1:
            # Tier 20 strict mode: treat T1 as tainted-sensitive even on an
            # ordinary, untainted turn - the most cautious posture Wave can
            # offer, for anyone who wants every normally-auto-run tool to
            # ask first. standard/off behave exactly as before (untainted
            # T1 just runs).
            if not tainted and mode != "strict":
                return confirm_gate.GuardOutcome("run")
            reason = (f"Asked before running '{tool_name}' - normally auto-run, but this turn "
                      f"read something untrusted first." if tainted else
                      f"Asked before running '{tool_name}' - security mode is 'strict', which asks "
                      f"before every normally-auto-run tool.")
            security_log.record("guard_confirm", reason=reason, tool=tool_name, source=source,
                                 tier="T1", tainted=tainted,
                                 taint_sources=taint.turn_sources() if tainted else [])
            return self._arm_confirm(tool_name, args, source)
        if tier == tiers.T3:
            if tainted:
                message = confirm_gate.denied_message(tool_name, args or {}, taint.turn_sources())
                security_log.record("guard_deny", reason=message, tool=tool_name, source=source,
                                     tier="T3", tainted=True, taint_sources=taint.turn_sources())
                return confirm_gate.GuardOutcome("deny", message)
            security_log.record("guard_confirm",
                                 reason=f"Asked before running '{tool_name}' - it's a sensitive/"
                                        f"destructive action.",
                                 tool=tool_name, source=source, tier="T3", tainted=False)
            return self._arm_confirm(tool_name, args, source)
        # T2, and any tier value this build doesn't otherwise recognize -
        # confirm either way, the same safe middle ground UNKNOWN_TIER
        # already picks in brain/tiers.py.
        security_log.record("guard_confirm",
                             reason=f"Asked before running '{tool_name}' - requires confirmation.",
                             tool=tool_name, source=source, tier=str(tier), tainted=tainted,
                             taint_sources=taint.turn_sources() if tainted else [])
        return self._arm_confirm(tool_name, args, source)

    def _arm_confirm(self, tool_name: str, args: dict, source: str) -> confirm_gate.GuardOutcome:
        """Shared by every "confirm" branch in _guard() above: arms
        self.pending_action exactly as Phase 7 always has (unchanged -
        this is still the only thing a later plain "yes" can replay), and
        returns the matching GuardOutcome."""
        self.pending_action = {
            "tool": tool_name, "args": dict(args or {}), "source": source,
            "armed_at": time.time(), "expires_at": time.time() + config.CONFIRM_PENDING_TTL_S,
        }
        return confirm_gate.GuardOutcome("confirm", confirm_gate.pending_message(tool_name, args or {}))

    def _guard_keyword(self, tool_name: str, args: dict) -> str:
        """_guard() for the keyword/deterministic path specifically - that
        path has no per-branch audit scope of its own the way _dispatch_tool
        already does (it only gets the generic 'keyword_command' wrapper
        several frames up), so this also logs a proper 'blocked' row under
        the real tool name when the gate fires (confirm OR deny - both are
        a blocked row; only the message text tells them apart), rather
        than leaving it completely unlogged. Still returns a plain
        Optional[str] - None on "run" - so none of this method's existing
        callers (`if gate: return gate`) need to change for a 3-outcome
        _guard() underneath."""
        gate = self._guard(tool_name, args, source="keyword")
        if gate.action == "run":
            return None
        audit.record(tool_name, args, result=gate.message, status="blocked", source="keyword")
        return gate.message

    def _check_pending_confirmation(self, user_text: str) -> str:
        """Called first thing in think(), before anything else this turn
        is interpreted as a new request. None means "nothing pending (or
        it was stale/unrelated and has now been dropped) - think() should
        handle user_text normally"; otherwise this IS the full reply for
        the turn and think() should return it as-is."""
        pending = self.pending_action
        if not pending:
            return None
        if time.time() > pending.get("expires_at", 0):
            # Expired: just drop it, silently - no "that confirmation timed
            # out" message, since that would itself be a reply to a message
            # that may have nothing to do with the old request. user_text
            # still gets handled normally below.
            self.pending_action = None
            return None
        verdict = confirm_gate.classify_reply(user_text)
        if verdict == "yes":
            self.pending_action = None
            return self._replay_confirmed(pending)
        if verdict == "no":
            self.pending_action = None
            audit.record(pending["tool"], pending.get("args"), result="User cancelled.",
                         status="blocked", source=pending.get("source", "ai"))
            return "Okay, cancelled - I won't do that."
        # Anything else: the user said something that isn't a plain yes/no
        # (changed their mind, asked something unrelated, or this is voice
        # input that misheard a real yes/no). Drop the stale pending
        # confirmation rather than forcing it on them, and let user_text be
        # handled as a brand-new message - this is what keeps the gate from
        # feeling intrusive instead of just safe.
        self.pending_action = None
        return None

    def cancel_pending_confirmation(self) -> bool:
        """Tier 11: a voice "stop"/"never mind" never reaches think() (the
        mic worker handles it), so a confirmation Wave just asked for would
        otherwise stay armed until its TTL - and a "yes" said a few seconds
        later about something else could still fire it. Drops it, logs the
        cancel like a spoken "no" would, and says whether anything was
        pending."""
        pending = self.pending_action
        self.pending_action = None
        if not pending:
            return False
        try:
            audit.record(pending.get("tool", ""), pending.get("args"), result="User cancelled (stop).",
                         status="blocked", source=pending.get("source", "ai"))
        except Exception:
            logger.debug("couldn't audit a cancelled confirmation", exc_info=True)
        return True

    def _replay_confirmed(self, pending: dict) -> str:
        """Actually runs a tool call the user just said yes to. Reuses the
        registered skill handler directly (the same one _dispatch_tool()
        would have called) rather than re-deriving which backend function
        to call, so this one replay path is correct for a tool gated from
        EITHER dispatch path - no need to remember which one armed it."""
        name, args = pending.get("tool", ""), pending.get("args") or {}
        handler = skills.get_handler(name)
        if handler is None:
            return "Sorry, I lost track of what I was about to do - could you ask again?"
        # Keep the ORIGINAL source (ai/keyword), not a generic third bucket -
        # the audit trail should still show whether this was something the
        # AI decided to do or a typed command, just now also confirmed.
        with audit.action(name, args, source=pending.get("source", "ai"), adoptable=True) as act:
            try:
                return act.done(handler(self, args))
            except Exception as e:
                logger.error(f"Confirmed tool '{name}' failed: {e}")
                return act.fail(f"Something went wrong while trying to do that: {e}")

    @staticmethod
    def _tools_for(messages: list) -> list:
        """Tier 28: the tool list for one request - at most
        config.LLM_MAX_TOOLS of them, picked for this turn by
        brain/tool_select.py (Groq/OpenAI refuse more than 128, and Wave
        has more than that). The user's own skills always go along."""
        user_skills = [n for n in skills.names() if skills.source_of(n) != BUILTIN_SOURCE]
        return tool_select.select_tool_specs(skills.tool_specs(), messages, always=user_skills)

    def _chat_completion(self, messages: list, *, tool_choice: str = "auto", temperature: float = 0.4,
                         max_tokens: int = 1600, reasoning_effort: str = "high", timeout: float = None):
        """Every Groq/OpenAI-compatible chat-completions call in the tool
        loop (think()) and its final wording pass (_compose_reply_from_tools)
        goes through here. Centralizes the 'some providers reject
        reasoning_effort entirely - retry once without it' dance that used
        to be duplicated at both call sites, and adds Phase 6 governance's
        optional per-call `timeout`, bounding this one request against the
        loop's remaining wall-clock budget (brain/llm_provider.py's
        OpenAICompatClient honors it; the real groq SDK supports it
        natively too).

        Not every provider path is guaranteed to accept a `timeout=` kwarg -
        belt-and-braces: if passing one ever raises a TypeError anyway, this
        drops it and relies on the client's own already-configured timeout
        (LLM_TIMEOUT) instead of failing the whole turn over an optional
        guardrail."""
        kwargs = dict(messages=messages, model=self.model, tools=self._tools_for(messages),
                      tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens)
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._create_negotiated(kwargs, reasoning_effort)

    # ---- Tier 29: rate-limit failover --------------------------------------

    def _backup_model(self):
        """The model a rate-limited request moves to: LLM_FALLBACK_MODEL, or
        on Groq (where every model has its own per-minute quota) the smaller
        sibling of the default one. None when there isn't a different one."""
        m = (getattr(config, "LLM_FALLBACK_MODEL", "") or "").strip()
        if m.lower() in ("off", "none", "0", "false"):
            return None
        if not m and self.provider is not None and self.provider.name == "groq":
            m = "openai/gpt-oss-20b"
        return m if m and m != self.model else None

    def _fast_model(self):
        """Tier 34: the model everyday turns go to - LLM_FAST_MODEL, or on
        Groq openai/gpt-oss-20b. None when there isn't a different one."""
        provider = getattr(self, "provider", None)
        return fast_model_for(getattr(provider, "name", "") if provider is not None else "", self.model)

    def _turn_tier(self):
        tls = getattr(self, "_route_tls", None)
        return getattr(tls, "tier", None) if tls is not None else None

    def _models_for_request(self) -> list:
        """The models to try, in order. Tier 34: a turn routed to the fast
        tier starts with the fast model, then the main one; otherwise the
        main model, then its backup (Tier 29). A model sitting out a rate
        limit goes to the end, after the ones that are free."""
        primary, backup = self.model, self._backup_model()
        fast = self._fast_model() if self._turn_tier() == FAST else None
        order = []
        for m in (fast, primary, backup):
            if m and m not in order:
                order.append(m)
        now = time.monotonic()
        ready = [m for m in order if self._model_cooldown.get(m, 0) <= now]
        return ready + [m for m in order if m not in ready]

    def _route_turn(self, user_text: str, attachment=None) -> None:
        """Tier 34: picks this turn's model tier (brain/model_router.py) -
        everything the turn asks the LLM from here on uses it."""
        tls = getattr(self, "_route_tls", None)
        if tls is None or model_router.mode() == "off" or not self._fast_model():
            return
        route = model_router.choose(user_text, attachment)
        tier = route.tier if model_router.mode() == "on" else None
        tls.tier = tier
        model = self._fast_model() if tier == FAST else self.model
        logger.info(f"Model route: {route.tier} ({route.reason}; {route.bucket})"
                    + (f" -> {model}" if tier else " - shadow mode, answering with the main model"))
        trace.annotate(model_tier=route.tier, model_route=route.reason)

    def _begin_turn_routing(self, user_text: str) -> None:
        """Tier 34: a new user turn - forget the last turn's tier, and let
        the router learn from what this turn says about the last answer."""
        tls = getattr(self, "_route_tls", None)
        if tls is not None:
            tls.tier = None
            tls.used_model = None
        try:
            model_router.observe_user_turn(user_text)
            style_profile.observe_user_turn(user_text)    # Tier 41
        except Exception:
            logger.debug("model router / style profile: observe failed", exc_info=True)

    def _ground(self, messages: list, user_text: str, attachment=None) -> None:
        """Tier 47 (brain/grounding.py): a time-sensitive question ("petrol ka
        rate aaj?", "who is the current RBI governor?") gets one web search
        before the model is asked, and the model answers from it. The result
        is someone else's web content: wrapped like get_live_information's
        (so the turn counts as having read the web) and given as a system
        message of its own - never mixed into the user's words. (Like that
        tool's own look-ups, a search isn't an action for the audit log.)"""
        if attachment or not grounding.needs_grounding(user_text):
            return
        found = grounding.fetch(grounding.search_query(user_text), WebManager.ask_internet)
        if not found:
            logger.info("Grounding: the web search found nothing in time - the model answers on its own.")
            return
        wrapped = content_trust.wrap_untrusted(found, source="a web search")
        messages.append({"role": "system", "content": (
            "Fresh web search results for the user's question (newer than what you learned in training): "
            "answer from them, say briefly where the answer comes from, and say so if they don't contain it.\n"
            + wrapped)})
        trace.annotate(grounded=True)
        logger.info("Grounding: searched the web first for a time-sensitive question.")

    def _learn_people(self, user_text: str) -> None:
        """Tier 46 (brain/people_graph.py): "my sister's name is Priya and she
        works at Infosys" is remembered as Priya's relation and workplace, as
        a picked-up fact (forgettable; shown as picked up). Not when chats
        aren't kept on disk - same rule as memory consolidation."""
        if not user_text or not db.history_enabled:
            return
        try:
            found = people_graph.learn(user_text, lambda k, v: db.save_memory(
                k, v, source="auto_consolidated", turn_id=trace.current_turn_id()))
            if found:
                logger.info(f"People graph: remembered {len(found)} fact(s) about someone the user named.")
        except Exception:
            logger.debug("people graph: learning failed", exc_info=True)

    def _retry_empty_answer(self, messages: list, governor=None) -> str:
        """Tier 34: gpt-oss models sometimes spend the whole token budget
        reasoning and come back with no text at all (seen live: "explain in
        detail how a refrigerator keeps food cold" -> ""). One more try, with
        little reasoning and more room, before giving up on the turn."""
        try:
            timeout = max(8.0, governor.remaining_time()) if governor is not None else None
            response = self._chat_completion(messages, tool_choice="none", max_tokens=2400,
                                             reasoning_effort="low", timeout=timeout)
            text = (response.choices[0].message.content or "").strip()
            logger.info(f"The model answered with nothing - asked again: {'got an answer' if text else 'still nothing'}.")
            return text
        except Exception as e:
            logger.info(f"Asking again after an empty answer failed: {type(e).__name__}: {e}")
            return ""

    def _cached_answer(self, user_text: str, attachment=None):
        """Tier 40 (brain/response_cache.py): the stored answer to a stable,
        non-personal question this model already answered - or None."""
        if attachment:
            return None
        model = self._fast_model() if self._turn_tier() == FAST else self.model
        answer = response_cache.get(user_text, model)
        if answer:
            logger.info("Answered from the response cache (a stable question asked before) - no model call.")
        return answer

    def _semantic_intent(self, user_text: str, attachment=None):
        """Tier 45 (brain/semantic_intents.py): an everyday low-risk command
        said in other words ("make it louder", "the screen is too dark") run
        straight away, through the same _dispatch_tool() gate/audit as the
        fast path - or None, and the model answers as before."""
        if attachment:
            return None
        try:
            hit = semantic_intents.match(user_text)
        except Exception:
            logger.debug("semantic intents: match failed", exc_info=True)
            return None
        if hit is None:
            return None
        intent, score = hit
        logger.info(f"Semantic intent: {intent} (similarity {score:.2f}) - no model call.")
        trace.annotate(skill=intent, rule=f"semantic:{score:.2f}")
        return self._dispatch_tool(intent, {}, source="semantic_intent")

    def _remember_answer(self, user_text: str, reply: str) -> None:
        try:
            response_cache.put(user_text, reply, self._answered_by())
        except Exception:
            logger.debug("response cache: put failed", exc_info=True)

    def _answered_by(self) -> str:
        tls = getattr(self, "_route_tls", None)
        return (getattr(tls, "used_model", None) if tls is not None else None) or self.model

    def _llm_create(self, **kwargs):
        """client.chat.completions.create() with Tier 29's failover: a
        rate-limited model (429/413) is skipped for as long as the provider
        asked and the request goes straight to the backup model; a 5xx or a
        dropped connection gets one quick retry on the same model. Raises
        what the last attempt raised."""
        max_wait = float(getattr(config, "LLM_RATE_LIMIT_WAIT_S", 10) or 0)
        waited = False
        fast = self._fast_model() if self._turn_tier() == FAST else None
        while True:
            models = self._models_for_request()
            limited = None
            for i, model in enumerate(models):
                for attempt in (1, 2):
                    try:
                        response = self.client.chat.completions.create(**dict(kwargs, model=model))
                        tls = getattr(self, "_route_tls", None)
                        if tls is not None:
                            tls.used_model = model
                        return response
                    except Exception as e:
                        if _is_rate_limited(e):
                            wait = _retry_after_s(e)
                            self._model_cooldown[model] = time.monotonic() + wait
                            limited = e
                            if i + 1 < len(models):
                                logger.warning(f"{model} is rate-limited ({wait:.0f}s) - answering with {models[i + 1]}.")
                            break
                        if attempt == 1 and _is_transient(e):
                            logger.info(f"{model}: transient error ({type(e).__name__}), retrying once")
                            time.sleep(0.8)
                            continue
                        if model == fast and i + 1 < len(models) and not isinstance(e, TypeError):
                            # Tier 34: the fast model failed outright - the
                            # main one answers instead of the turn failing.
                            logger.warning(f"{model} failed ({type(e).__name__}: {str(e)[:120]}) - "
                                           f"answering with {models[i + 1]}.")
                            break
                        raise
            # Every model is rate-limited. If one frees up within a few
            # seconds, wait for it once rather than giving up on the turn.
            now = time.monotonic()
            soonest = min(self._model_cooldown.get(m, now) for m in models) - now
            if waited or soonest > max_wait:
                raise limited
            logger.info(f"All models rate-limited - waiting {soonest:.1f}s for the first to free up")
            time.sleep(max(0.0, soonest) + 0.3)
            waited = True

    def _create_negotiated(self, kwargs: dict, reasoning_effort: str):
        """_llm_create() plus the old parameter negotiation: a client that
        refuses `timeout=` (TypeError) or `reasoning_effort` (any other
        non-rate-limit error) is asked again without it."""
        try:
            return self._llm_create(extra_body={"reasoning_effort": reasoning_effort}, **kwargs)
        except TypeError:
            kwargs.pop("timeout", None)
            return self._llm_create(extra_body={"reasoning_effort": reasoning_effort}, **kwargs)
        except Exception as e:
            if _is_rate_limited(e):
                raise                              # both models are limited - another call won't help
            # This account/model combo may not accept reasoning_effort - retry without it.
            return self._llm_create(**kwargs)

    def _busy_reply(self) -> str:
        """What the user hears when every model is rate-limited right now."""
        now = time.monotonic()
        waits = [until - now for until in self._model_cooldown.values() if until > now]
        seconds = int(min(waits)) + 1 if waits else 20
        return (f"The AI service is busy right now (its free per-minute limit is used up) - "
                f"please ask me again in about {seconds} seconds.")

    def _ollama_reachable(self) -> bool:
        """Tier 29: a 0.4 s check before using the local Ollama fallback. On
        Windows a refused localhost connection takes ~2 s per attempt, and
        the ollama client made several, so a turn whose cloud call failed
        sat ~9 s on an Ollama that wasn't even running. Remembered for 60 s."""
        now = time.monotonic()
        if now < self._ollama_down_until:
            return False
        try:
            import socket
            from urllib.parse import urlsplit
            raw = (config.OLLAMA_HOST or "http://127.0.0.1:11434").strip()
            parts = urlsplit(raw if "://" in raw else "http://" + raw)
            host = parts.hostname or "127.0.0.1"
            host = "127.0.0.1" if host == "localhost" else host
            socket.create_connection((host, parts.port or 11434), timeout=0.4).close()
            return True
        except Exception:
            self._ollama_down_until = now + 60.0
            return False

    def _reply_after_failure(self, error, user_text: str, attachment=None, cancel_event=None) -> str:
        """Tier 29: what a turn says when its cloud call failed. Local Ollama
        first (skipped within 0.4 s when it isn't running); then, for a rate
        limit, a plain "busy - ask again in N s" (asking the same busy service
        again only fails again); otherwise the no-tools fallback. Never a raw
        "System Error: <exception text>" and never an empty reply."""
        try:
            reply = self._think_with_ollama(user_text, attachment, cancel_event=cancel_event)
            if reply:
                return reply
            if _is_rate_limited(error):
                return self._busy_reply()
            return self._think_fallback(user_text) or \
                "Sorry, I didn't get an answer back just now - please ask me again."
        except Exception as e2:
            if _is_rate_limited(e2):
                return self._busy_reply()
            logger.error(f"Fallback reply failed too: {e2}")
            return "Sorry, I couldn't reach the AI service just now - please try again in a moment."

    @staticmethod
    def _recent_history(limit: int = 16):
        """Tier 29: the chat history sent with a request, newest kept first,
        within LLM_HISTORY_CHARS (and no single message over 1,200 chars) -
        a few long replies in the history used to add thousands of tokens
        to every request."""
        budget = int(getattr(config, "LLM_HISTORY_CHARS", 4000) or 0)
        kept, used = [], 0
        # Tier 35: what the user asked Wave to forget stays out of the
        # conversation the model sees too (annotated dates aside, so "doctor
        # kal (Thu 26 Nov 2026)" still matches a message that said "doctor kal").
        forgotten = [re.sub(r"\s*\((?:mon|tue|wed|thu|fri|sat|sun)\w* \d{1,2} \w+ \d{4}\)", "", v, flags=re.I).strip().lower()
                     for v in db.get_forgotten_values()]
        forgotten = [v for v in forgotten if len(v) >= 3]
        for role, msg in reversed(db.get_chat_history(limit=limit)):
            msg = msg or ""
            if forgotten and any(v in msg.lower() for v in forgotten):
                continue
            if len(msg) > 1200:
                msg = msg[:1200].rstrip() + " …"
            if budget and kept and used + len(msg) > budget:
                break
            used += len(msg)
            kept.append((role, msg))
        return list(reversed(kept))

    def _chat_completion_stream(self, messages: list, *, tool_choice: str = "auto", temperature: float = 0.4,
                                max_tokens: int = 1600, reasoning_effort: str = "high", timeout: float = None):
        """Tier 2: streaming counterpart to _chat_completion, used only by
        think_stream()'s round loop. Same signature, same parameter-
        negotiation dance (some providers reject reasoning_effort
        entirely - retry once without it), but returns a generator of
        chunks (brain/llm_provider.py's ChatCompletionChunk, or the real
        groq SDK's own equivalent when LLM_PROVIDER=groq and the SDK is
        installed) instead of one completed response. Both client shapes
        expose chunk.choices[0].delta.content / .tool_calls / the round's
        finish_reason identically, so think_stream() doesn't need to know
        which one produced them."""
        kwargs = dict(messages=messages, model=self.model, tools=self._tools_for(messages),
                      tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens, stream=True)
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self._create_negotiated(kwargs, reasoning_effort)

    # Tier 4: the ~50 @skill(...) tool handlers that used to live here
    # (_skill_open_app, _skill_delete_file, _skill_get_weather, ...) now live
    # in brain/handlers/, grouped by domain - automation.py, media.py,
    # memory.py, reminders.py, calendar.py, vision.py, web.py, system.py.
    # Each one is still registered by the exact same @skill(...) decorator at
    # import time (see brain/handlers/__init__.py, imported near the top of
    # this file) and still reached through the exact same skills.get_handler(name)
    # lookup in _dispatch_tool()/_replay_confirmed() below - nothing about
    # dispatch changed, only where the handler functions themselves live.

    def _set_reminder(self, message: str, when_text: str, recurrence: str = "") -> str:
        """Parses when_text with dateparser and saves a reminder for it.
        Called from the set_reminder tool's _dispatch_tool branch - shared by
        both the Groq and Ollama tool-calling paths since they both route
        through the same _dispatch_tool(). Not wired into the deterministic
        keyword fallback in _execute_single_action(): splitting free-form
        text like 'remind me in 20 minutes to check the oven' into a message
        field and a when field in any order is exactly the kind of slot-
        filling an LLM does reliably and regex-based phrase matching does
        not, so this needs an actual tool-calling turn (Groq or Ollama) to
        reach, same as e.g. create_workflow already does.

        🔧 Tier 3 / item 8: `recurrence` is new - 'daily' saves a reminder
        that reschedules itself a day forward each time it fires instead of
        ending (see database/db_manager.py's complete_or_reschedule_reminder,
        called from ui/workers.py's ReminderWorker), anything else means the
        original one-off behavior, unchanged."""
        if not HAS_DATEPARSER:
            return "Reminders need the 'dateparser' package installed (pip install dateparser)."
        message = (message or "").strip() or "Reminder"
        when_text = (when_text or "").strip()
        if not when_text:
            return "I need to know when to remind you."
        recurrence = (recurrence or "").strip().lower()
        recurrence = recurrence if recurrence == "daily" else None

        # 🔧 Restricting languages avoids a well-documented dateparser
        # performance trap: with no `languages` filter it tries dozens of
        # locales before settling, often adding several hundred ms to every
        # single call. 'en' and 'hi' match the Hinglish support this
        # assistant already has elsewhere (see get_dynamic_system_prompt).
        due_dt = dateparser.parse(
            when_text,
            languages=["en", "hi"],
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": datetime.datetime.now()},
        )
        if not due_dt:
            return f"I couldn't understand the time '{when_text}' - could you rephrase it?"
        if due_dt <= datetime.datetime.now():
            return "That time has already passed - please give me a future time."

        reminder_id = db.save_reminder(message, due_dt.isoformat(), recurrence=recurrence)
        if reminder_id is None:
            return "Sorry, I couldn't save that reminder right now."
        friendly_time = due_dt.strftime("%I:%M %p on %B %d")
        if recurrence == "daily":
            return f"Got it - I'll remind you to {message} at {friendly_time.split(' on ')[0]}, every day."
        return f"Got it - I'll remind you to {message} at {friendly_time}."

    def _encode_image_for_vision(self, image_path: str) -> str:
        """Base64-encodes an image as a data: URI for the Groq vision model,
        downscaling large images first (via Pillow, when available) so
        requests stay fast and comfortably under the API's size limits."""
        if HAS_PIL:
            try:
                with Image.open(image_path) as img:
                    img = img.convert("RGB")
                    max_side = 1568
                    if max(img.size) > max_side:
                        img.thumbnail((max_side, max_side), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=87)
                    encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
                    return f"data:image/jpeg;base64,{encoded}"
            except Exception as e:
                logger.warning(f"Image downscale failed, sending original file: {e}")

        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        return f"data:image/{ext};base64,{encoded}"

    def _vision_describe_image(self, image_path: str, prompt_text: str) -> str:
        """Tier 18: the side-effect-free CORE of asking the provider's
        vision model (self.vision_model - separate from the text/tool-
        calling model above, which may be text-only) about one image.

        Pulled out of _think_with_image (below) unchanged in behavior, so
        brain/handlers/vision.py's new describe_screen tool can reuse the
        exact same encode/call/error-handling logic for a freshly-taken
        SCREENSHOT instead of a user-attached image. The two call sites
        differ in what happens to the text this returns, which is exactly
        why this method itself does NOT call db.save_message: a tool
        result (describe_screen's case) is fed back to the LLM as one more
        thing to reason about and isn't the turn's own final reply, while
        _think_with_image's attachment flow bypasses tool-calling entirely
        and so DOES need to save+return its own final reply - that stays
        _think_with_image's own job, right below."""
        if not self.client:
            # Tier 10: this used to say "no Groq API key is configured" even
            # though self.client is provider-agnostic since Phase 4.
            problem = ""
            try:
                problem = (self.provider_info() or {}).get("problem", "")
            except Exception:
                pass
            return ("I can't look at images right now — no AI provider is configured"
                    + (f" ({problem})." if problem else
                       " (check LLM_PROVIDER and its API key in your .env)."))
        try:
            data_uri = self._encode_image_for_vision(image_path)
            messages = [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }]
            response = self.client.chat.completions.create(
                messages=messages, model=self.vision_model,
                temperature=0.4, max_tokens=1200,
            )
            return response.choices[0].message.content or "I looked at the image but didn't have anything to add."
        except Exception as e:
            logger.error(f"Vision request failed: {e}")
            return f"I couldn't process that image — something went wrong talking to the vision model: {e}"

    def _think_with_image(self, user_text: str, attachment: dict) -> str:
        """Answers a question about an attached image - the full-chat-turn
        wrapper around _vision_describe_image above (saves+returns the
        final reply, which that shared core deliberately doesn't do)."""
        prompt_text = user_text.strip() or "What's in this image? Describe it, and point out anything notable."
        # .get(), not ["path"]: _vision_describe_image's own "no client
        # configured" early-return guard must run before this ever touches
        # the attachment dict's contents (an attachment-less call with no
        # client configured, e.g. the test fixtures below, must still hit
        # that guard rather than KeyError on a key that was never needed).
        reply = self._vision_describe_image(attachment.get("path"), prompt_text)
        db.save_message("assistant", reply)
        return reply

    def _get_ollama_client(self):
        """Lazily builds and caches the local Ollama client. Returns None -
        never raises - if the feature is switched off, the 'ollama' package
        isn't installed, or the client can't be constructed for any other
        reason, so callers can treat "no Ollama" as just another normal,
        expected state rather than an error to handle."""
        if not config.OLLAMA_ENABLED:
            return None
        if self.client is not None and self.provider.name == "ollama":
            return None   # the primary provider already IS local Ollama - a second try would only repeat the failure
        if self._ollama_client is not None:
            return self._ollama_client
        try:
            import ollama
            self._ollama_client = ollama.Client(host=config.OLLAMA_HOST, timeout=config.OLLAMA_TIMEOUT)
        except ImportError:
            logger.debug("Ollama fallback unavailable: 'ollama' package not installed.")
            return None
        except Exception as e:
            logger.warning(f"Ollama client init failed: {e}")
            return None
        return self._ollama_client

    def _think_with_ollama(self, user_text: str, attachment: dict = None, cancel_event=None) -> str:
        """Local-model fallback for whenever Groq is unreachable, erroring,
        or not configured at all - used by think() below, which tries this
        before giving up to the fully-deterministic _think_fallback().

        Mirrors the Groq tool-calling turn in think() as closely as
        possible: same system prompt, same chat history, same TOOLS schema
        (the ollama python client - verified against the real ollama==0.6.3
        package - accepts the identical OpenAI-style {"type": "function",
        ...} dicts Groq uses, no conversion needed), and the same
        _dispatch_tool() execution path, so every tool behaves identically
        regardless of which brain is answering.

        Unlike the Groq path, a tool result here is NOT run through a
        second "word this naturally" pass (_compose_reply_from_tools) -
        that pass makes its own Groq API call, and making a second cloud
        call from inside the LOCAL fallback would defeat the point of
        having one. Results come back as the same plain, joined string
        process_actions() already returns elsewhere in this file - correct
        and clear, just a little less conversational than the Groq path.

        Returns None - never raises - on any failure (Ollama not running,
        model not pulled, timeout, malformed response, anything), so
        think() can carry on down its existing fallback chain exactly as it
        did before this method existed.

        Phase 6: this now loops across multiple rounds of tool calls too,
        governed by the same _ToolLoopGovernor as the Groq path in think() -
        minus a preemptive per-call timeout (the ollama client's call shape
        isn't assumed to accept one); the deadline is still checked between
        rounds. Each round's assistant message is appended back exactly as
        the client returned it (rather than hand-rebuilding its tool_calls
        wire shape), so this round-trips through whatever that client
        version actually expects.

        Tier 19: cancel_event (optional threading.Event), checked before
        every round and before every tool call - same granularity as
        think_stream()'s loop. Returns None the moment it's set, exactly
        like any other way this method already comes up empty: think() and
        think_stream() both already treat a None here as "no answer, try
        the next fallback" (Ollama down, model missing, whatever) - the
        caller is the one that knows a cancelled turn shouldn't go on to
        _think_fallback() either, and both callers are being taught that
        alongside this change rather than this method guessing it itself."""
        client = self._get_ollama_client()
        if not client:
            return None
        if type(client).__module__.split(".")[0] == "ollama" and not self._ollama_reachable():
            return None                       # the real client, and nothing is listening - don't wait on it

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        try:
            messages = [self.get_dynamic_system_prompt(user_text)]
            history = self._recent_history(limit=16)
            for role, msg in history:
                messages.append({"role": role, "content": msg})

            if attachment and attachment.get("kind") == "document":
                doc_note = " (truncated)" if attachment.get("truncated") else ""
                # Phase 7: see the matching comment in think() above.
                wrapped_text = content_trust.wrap_untrusted(
                    attachment.get('text_content', ''),
                    source=f"an attached file ({attachment.get('name', 'file')})",
                )
                user_content = (
                    f"{user_text}\n\n"
                    f"--- Attached file: {attachment.get('name', 'file')}{doc_note} ---\n"
                    f"{wrapped_text}"
                )
            else:
                user_content = user_text
            messages.append({"role": "user", "content": user_content})

            governor = _ToolLoopGovernor()
            all_results = []
            reply = None

            while governor.can_continue():
                if cancelled():
                    return None
                governor.start_round()
                chat_kwargs = dict(
                    model=config.OLLAMA_MODEL, messages=messages, tools=self._tools_for(messages),
                    options={"temperature": 0.4, "num_predict": 1200},
                    keep_alive=config.OLLAMA_KEEP_ALIVE,
                )
                try:
                    response = client.chat(think=False, **chat_kwargs)
                except Exception:
                    # Some ollama-python versions / models reject `think=` -
                    # retry once without it rather than losing the whole
                    # fallback over one optional kwarg.
                    response = client.chat(**chat_kwargs)

                reply_msg = response.message
                tool_calls = getattr(reply_msg, "tool_calls", None)

                if not tool_calls:
                    reply = (reply_msg.content or "").strip()
                    break

                if cancelled():
                    return None   # never act on a turn the user already called off
                messages.append(reply_msg)
                for call in tool_calls:
                    if cancelled():
                        return None
                    result = self._dispatch_tool(call.function.name, call.function.arguments)
                    all_results.append(result)
                    messages.append({"role": "tool", "content": str(result) if result else "Done."})

            if reply is None:
                if governor.hit_limit:
                    logger.warning(f"Ollama tool loop stopped early ({governor.hit_limit} limit) "
                                   f"after {governor.step} round(s).")
                reply = " & ".join(str(r) for r in all_results if r)

            if not reply:
                return None
            logger.info("Groq unavailable - answered via local Ollama fallback instead.")
            return reply
        except Exception as e:
            logger.warning(f"Ollama fallback failed: {e}")
            return None

    def _compose_reply_from_tools(self, messages: list, results: list) -> str:
        """Final pass of the tool-calling loop in think(): hands the full
        transcript - which by now already ends with every tool call and
        result from EVERY round the loop ran, not just the last one - back
        to the model so it can word the reply naturally (in the user's own
        language/style) instead of the user getting raw strings, or worse,
        only the last round's results.

        This can only ever make things better or equal, never worse: any
        failure at all (API error, empty answer, odd response shape) falls
        straight back to the plain joined tool results - i.e. exactly what
        the user got before this pass existed."""
        raw = " ".join(str(r) for r in results if r)
        try:
            followup = list(messages)
            # The guardrail rides on the existing system message (rather than
            # being a second system message dropped in mid-conversation).
            followup[0] = {"role": "system", "content": messages[0]["content"] + TOOL_RESULT_GUIDANCE}
            resp = self._chat_completion(followup, tool_choice="none", temperature=0.3,
                                         max_tokens=800, reasoning_effort="low")
            text = (resp.choices[0].message.content or "").strip()
            return text or raw
        except Exception as e:
            logger.warning(f"Follow-up pass failed, using raw tool results instead: {e}")
            return raw

    def think(self, user_text: str, attachment: dict = None, cancel_event=None) -> str:
        """Tier 19: cancel_event (optional threading.Event) mirrors
        think_stream()'s Tier 11 parameter of the same name - checked
        before every round of the tool loop below and before every tool
        call, so a turn called off mid-flight doesn't go on to
        open/close/send anything. No live caller in this UI passes one
        today (ui/workers.py's AIStreamWorker only ever calls
        think_stream(), never this method) - this exists so the two
        methods stay in parity, for this method's own tests, and for any
        future caller of this non-streaming path."""
        trace.begin_turn(source="think")
        taint.clear_turn()      # Tier 1 item #4: fresh taint record for this turn
        self._begin_turn_routing(user_text)   # Tier 34
        db.save_message("user", user_text)
        self._learn_people(user_text)         # Tier 46

        # Phase 7: if a sensitive tool call is awaiting a yes/no from last
        # turn, THIS turn's job is to answer that, not to be freshly
        # interpreted - checked before the image/restart/shutdown/anything
        # below so it can't be routed around by any of those earlier exits.
        # Returns None (falls through to everything below unchanged) unless
        # something was actually pending and still within its TTL.
        pending_reply = self._check_pending_confirmation(user_text)
        if pending_reply is not None:
            db.save_message("assistant", pending_reply)
            trace.end_turn(outcome="ok", route="pending_confirmation")
            return pending_reply

        if attachment and attachment.get("kind") == "image":
            reply = self._think_with_image(user_text, attachment)
            trace.end_turn(outcome="ok", route="image")
            return reply

        # Restart/shutdown must NEVER be silently auto-executed from chat.
        text_lower = user_text.lower()
        if any(p in text_lower for p in ["restart the pc", "restart my pc", "restart the computer",
                                           "restart my computer", "reboot"]):
            reply = ("For safety, I won't restart your PC directly from chat — head to the "
                     "Apps & Power page and use the Restart button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            return reply
        if any(p in text_lower for p in ["shutdown the pc", "shut down the pc", "shutdown my pc",
                                           "shut down my pc", "turn off the pc", "turn off my computer", "power off"]):
            reply = ("For safety, I won't shut down your PC directly from chat — head to the "
                     "Apps & Power page and use the Shutdown button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            return reply

                # 🚨 REMOVED: Sleep isn't reliable on this PC's hardware (either does
        # nothing or shuts the PC down), so it's been taken out entirely
        # instead of pretending to do something it can't.
        if any(p in text_lower for p in ["sleep the pc", "sleep my pc", "put the pc to sleep",
                                           "put my pc to sleep", "sleep mode", "sleep my computer",
                                           "sleep the computer", "go to sleep", "make the pc sleep"]) \
           or text_lower.strip() in ("sleep", "sleep pc", "sleep computer"):
            reply = ("Sleep isn't available on this PC — the hardware doesn't support it reliably "
                     "(it either does nothing or shuts down instead), so this feature has been removed.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            return reply

        if not self.client:
            reply = self._think_with_ollama(user_text, attachment, cancel_event=cancel_event) or self._think_fallback(user_text)
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="no_provider_fallback")
            return reply

        # Tier 17: "close it"/"usko band karo" after open_app, "and
        # Jaipur?" after get_weather - a pronoun/elliptical follow-up
        # whose meaning comes from the action log, not the message
        # itself. Tried before intent_router.route() below (which has no
        # way to resolve "it" on its own - a bare "close it" matches none
        # of its rules), on the same _dispatch_tool() path, so the
        # confirm-gate/audit/validation are unchanged here too. See
        # brain/followup_resolver.py for the full rationale and the
        # (deliberately small) set of covered shapes.
        followup = followup_resolver.resolve_followup(user_text)
        if followup is not None:
            trace.annotate(skill=followup.skill, rule=followup.rule)
            reply = self._dispatch_tool(followup.skill, followup.args, source="followup_resolver")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="followup_resolver")
            return reply

        # Tier 2: a curated set of short, unambiguous commands ("volume
        # up", "lock my pc", "open chrome") is caught here and dispatched
        # directly - same _dispatch_tool() a model-chosen call goes
        # through (so the confirm-gate/audit/validation are unchanged),
        # just without paying for an LLM round-trip to learn what was
        # already obvious. See brain/intent_router.py for the full
        # rationale and the (deliberately small) rule set.
        if config.INTENT_ROUTER_ENABLED:
            routed = intent_router.route(user_text)
            if routed is not None:
                trace.annotate(skill=routed.skill, rule=routed.rule)
                reply = self._dispatch_tool(routed.skill, routed.args, source="intent_router")
                db.save_message("assistant", reply)
                trace.end_turn(outcome="ok", route="intent_router")
                return reply

        # Tier 17: neither followup_resolver nor intent_router recognized
        # this turn - log it (best-effort, local-only) so repeated misses
        # can surface as new intent_router rules later. See
        # brain/unmatched_log.py for the full rationale.
        unmatched_log.log_unmatched(user_text)

        try:
            # Tier 4 item #11: tried before building the normal message
            # history at all, and skipped outright for a document upload -
            # a plan's single propose_plan() call never sees attachment
            # content, so a request about an uploaded file always belongs
            # to the normal loop (which does pass that content along)
            # rather than a plan proposed blind to it.
            if attachment is None:
                plan_outcome = planning.try_plan_and_execute(self, user_text)
                if plan_outcome is not None:
                    db.save_message("assistant", plan_outcome.final_message)
                    trace.annotate(
                        route="planner", plan_steps=len(plan_outcome.plan.steps),
                        plan_dropped=plan_outcome.plan.dropped,
                        plan_ok=sum(1 for r in plan_outcome.results if r.status == "ok"),
                        plan_failed=sum(1 for r in plan_outcome.results if r.status == "failed"))
                    trace.end_turn(outcome="blocked" if plan_outcome.pending_confirmation else "ok")
                    return plan_outcome.final_message

            exact = exact_answers.answer(user_text) if attachment is None else None   # Tier 42
            if exact:
                db.save_message("assistant", exact)
                trace.annotate(route="exact_answer")
                trace.end_turn(outcome="ok")
                return exact
            semantic = self._semantic_intent(user_text, attachment)   # Tier 45
            if semantic is not None:
                db.save_message("assistant", semantic)
                trace.end_turn(outcome="ok", route="semantic_intent")
                return semantic
            self._route_turn(user_text, attachment)   # Tier 34: fast or main model
            cached = self._cached_answer(user_text, attachment)   # Tier 40
            if cached:
                db.save_message("assistant", cached)
                trace.annotate(route="response_cache")
                trace.end_turn(outcome="ok")
                return cached
            messages = [self.get_dynamic_system_prompt(user_text)]
            history = self._recent_history(limit=16)
            for role, msg in history:
                messages.append({"role": role, "content": msg})

            if attachment and attachment.get("kind") == "document":
                doc_note = " (truncated)" if attachment.get("truncated") else ""
                # Phase 7: the upload itself is the user's own action, but
                # its TEXT could be anything (a saved web page, a forwarded
                # document) - same untrusted-content treatment as OCR'd
                # screen text and web results. See brain/content_trust.py.
                wrapped_text = content_trust.wrap_untrusted(
                    attachment.get('text_content', ''),
                    source=f"an attached file ({attachment.get('name', 'file')})",
                )
                user_content = (
                    f"{user_text}\n\n"
                    f"--- Attached file: {attachment.get('name', 'file')}{doc_note} ---\n"
                    f"{wrapped_text}"
                )
            else:
                user_content = user_text
            messages.append({"role": "user", "content": user_content})
            self._ground(messages, user_text, attachment)   # Tier 47: time-sensitive -> search first

            # 🔧 Phase 6: this used to be exactly one round of tool calls,
            # forced closed by the old follow-up pass always using
            # tool_choice="none" - a request needing two dependent steps
            # ("check my battery, and if it's low remind me to charge it")
            # had to be guessed in a single shot or not at all. Now the
            # model can see a tool's result and decide to call another tool
            # based on it - bounded by _ToolLoopGovernor two independent
            # ways (a step-count cap and a wall-clock deadline) so a
            # confused/looping model can never hang this turn.
            governor = _ToolLoopGovernor()
            all_results = []
            final_text = None

            def cancelled() -> bool:
                return cancel_event is not None and cancel_event.is_set()

            while governor.can_continue():
                if cancelled():
                    raise _TurnCancelled()
                governor.start_round()
                _llm_t0 = time.perf_counter()
                response = self._chat_completion(messages, tool_choice="auto", max_tokens=1600,
                                                  timeout=governor.remaining_time())
                trace.add_duration("llm_ms", time.perf_counter() - _llm_t0)
                choice_msg = response.choices[0].message

                if not choice_msg.tool_calls:
                    final_text = choice_msg.content
                    break

                if cancelled():
                    raise _TurnCancelled()
                tool_calls_payload = [
                    {"id": call.id, "type": "function",
                     "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"}}
                    for call in choice_msg.tool_calls
                ]
                messages.append({"role": "assistant", "content": choice_msg.content or "",
                                 "tool_calls": tool_calls_payload})
                for call in choice_msg.tool_calls:
                    if cancelled():
                        raise _TurnCancelled()
                    _tool_t0 = time.perf_counter()
                    result = self._dispatch_tool(call.function.name, call.function.arguments)
                    trace.add_duration("tool_ms", time.perf_counter() - _tool_t0)
                    trace.count("tools_called")
                    all_results.append(result)
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "content": str(result) if result else "Done."})

            if final_text is not None and not final_text.strip() and not all_results:
                final_text = self._retry_empty_answer(messages, governor) or None   # Tier 34
            if final_text is not None and final_text.strip():
                reply = final_text
                if not all_results:
                    self._remember_answer(user_text, reply)   # Tier 40
            elif all_results:
                if governor.hit_limit:
                    logger.warning(f"Tool loop stopped early ({governor.hit_limit} limit) after "
                                   f"{governor.step} round(s) - composing from {len(all_results)} result(s) gathered so far.")
                # 🔧 FIX: the raw tool output used to go straight back to the
                # user as-is (several tools = strings glued together with a
                # space). Now the model gets to see what actually happened and
                # word the reply properly - see _compose_reply_from_tools.
                reply = self._compose_reply_from_tools(messages, all_results) or "Done."
            else:
                # Governor stopped the loop before the model got a single
                # chance to answer at all - vanishingly rare (would need the
                # very first round to blow the whole timeout budget).
                reply = "That's taking longer than expected - could you try again or narrow the request a bit?"

            db.save_message("assistant", reply)
            trace.annotate(route="tool_loop", rounds=governor.step, model=self._answered_by(),
                           provider=self.provider.name if self.provider else None)
            trace.end_turn(outcome="ok")
            return reply

        except _TurnCancelled:
            # Tier 19: nothing is saved to history - unlike think_stream(),
            # this method never hands the caller anything until it
            # returns, so there's no partial text on screen to preserve
            # (think_stream's save_interrupted() only saves when there IS
            # one). The caller already knows it cancelled this turn; this
            # return value just keeps this method's "always returns a
            # string, never None" contract for whoever's holding it.
            logger.info("think: turn cancelled by the caller")
            trace.end_turn(outcome="interrupted", route="tool_loop")
            return INTERRUPTED_MARKER.strip()

        except Exception as e:
            logger.error(f"Tool-calling think() failed, falling back: {e}")
            trace.mark("fallback_triggered", reason=type(e).__name__)
            reply = self._reply_after_failure(e, user_text, attachment, cancel_event)
            db.save_message("assistant", reply)
            trace.end_turn(outcome="error", route="exception_fallback", error=str(e)[:300])
            return reply

    def think_stream(self, user_text: str, attachment: dict = None, cancel_event=None):
        """Tier 2: generator counterpart to think() - yields the reply's
        text in chunks as they're produced instead of returning one
        finished string, so a caller (ui/workers.py's AIStreamWorker) can
        start speaking the first sentence while the model is still
        writing the rest (see voice/tts.py's speak_stream()). Joining
        every yielded chunk gives the exact same string think() would
        have returned, and this generator saves that full string to the
        chat history exactly once before it ends - same contract as
        think(), delivered incrementally instead of all at once.

        Every short-circuit think() has (a pending confirmation, an image
        attachment, the restart/shutdown/sleep safety replies, no
        provider configured, an intent-router match) yields its whole
        reply as a single chunk here - there's nothing to stream about a
        canned or backend-produced string, and it keeps this generator's
        contract simple: at least one chunk, always, for every path.

        Only the model's own wording is actually streamed token by token,
        and only once no more tool calls are coming - see the loop below
        for why a round that might still call a tool stays non-streaming
        at the decision level (its content, if any, is still streamed as
        it arrives; it just isn't assumed to be the final answer until
        the round turns out to have no tool_calls after all).

        Tier 11:
          * cancel_event (optional threading.Event): checked before every
            LLM round and before any tool runs, so a turn the user called
            off ("stop", a barge-in, a newer message) doesn't go on to
            open/close/send anything. Whatever was already streamed is
            saved to the history with INTERRUPTED_MARKER, and the trace
            row records outcome="interrupted".
          * the same happens when the caller simply stops iterating and
            closes this generator (GeneratorExit) part-way through a reply.
          * text from an earlier round (a model "thinking out loud" before
            a tool call) and the final answer are separated by a blank
            line instead of being glued together mid-word.
          * the image path no longer saves its reply twice
            (_think_with_image already saves it)."""
        trace.begin_turn(source="think_stream")
        taint.clear_turn()      # Tier 1 item #4: fresh taint record for this turn
        self._begin_turn_routing(user_text)   # Tier 34
        db.save_message("user", user_text)
        self._learn_people(user_text)         # Tier 46

        pending_reply = self._check_pending_confirmation(user_text)
        if pending_reply is not None:
            db.save_message("assistant", pending_reply)
            trace.end_turn(outcome="ok", route="pending_confirmation")
            yield pending_reply
            return

        if attachment and attachment.get("kind") == "image":
            reply = self._think_with_image(user_text, attachment)   # saves the reply itself
            trace.end_turn(outcome="ok", route="image")
            yield reply
            return

        text_lower = user_text.lower()
        if any(p in text_lower for p in ["restart the pc", "restart my pc", "restart the computer",
                                           "restart my computer", "reboot"]):
            reply = ("For safety, I won't restart your PC directly from chat — head to the "
                     "Apps & Power page and use the Restart button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            yield reply
            return
        if any(p in text_lower for p in ["shutdown the pc", "shut down the pc", "shutdown my pc",
                                           "shut down my pc", "turn off the pc", "turn off my computer", "power off"]):
            reply = ("For safety, I won't shut down your PC directly from chat — head to the "
                     "Apps & Power page and use the Shutdown button there, which will ask you to confirm first.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            yield reply
            return
        if any(p in text_lower for p in ["sleep the pc", "sleep my pc", "put the pc to sleep",
                                           "put my pc to sleep", "sleep mode", "sleep my computer",
                                           "sleep the computer", "go to sleep", "make the pc sleep"]) \
           or text_lower.strip() in ("sleep", "sleep pc", "sleep computer"):
            reply = ("Sleep isn't available on this PC — the hardware doesn't support it reliably "
                     "(it either does nothing or shuts down instead), so this feature has been removed.")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="safety_refusal")
            yield reply
            return

        if not self.client:
            reply = self._think_with_ollama(user_text, attachment, cancel_event=cancel_event) or self._think_fallback(user_text)
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="no_provider_fallback")
            yield reply
            return

        # Tier 17: see the matching block in think() above for the full
        # rationale - kept in sync with it deliberately.
        followup = followup_resolver.resolve_followup(user_text)
        if followup is not None:
            trace.annotate(skill=followup.skill, rule=followup.rule)
            reply = self._dispatch_tool(followup.skill, followup.args, source="followup_resolver")
            db.save_message("assistant", reply)
            trace.end_turn(outcome="ok", route="followup_resolver")
            yield reply
            return

        if config.INTENT_ROUTER_ENABLED:
            routed = intent_router.route(user_text)
            if routed is not None:
                trace.annotate(skill=routed.skill, rule=routed.rule)
                reply = self._dispatch_tool(routed.skill, routed.args, source="intent_router")
                db.save_message("assistant", reply)
                trace.end_turn(outcome="ok", route="intent_router")
                yield reply
                return

        # Tier 17: neither followup_resolver nor intent_router recognized
        # this turn - log it (best-effort, local-only) so repeated misses
        # can surface as new intent_router rules later. See
        # brain/unmatched_log.py for the full rationale.
        unmatched_log.log_unmatched(user_text)

        # Tier 11: every piece handed to the caller from here on, in order -
        # what gets saved (with INTERRUPTED_MARKER) if the turn is cut off,
        # and how the fallback below knows it is appending to a partial reply.
        streamed_parts = []
        finished = False

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        def save_interrupted():
            partial = "".join(streamed_parts).strip()
            if partial:
                db.save_message("assistant", partial + INTERRUPTED_MARKER)
            trace.end_turn(outcome="interrupted", route="tool_loop")

        try:
            # Tier 4 item #11: see think()'s identical guard for why this
            # is skipped outright for a document upload.
            if attachment is None:
                plan_outcome = planning.try_plan_and_execute(self, user_text)
                if plan_outcome is not None:
                    db.save_message("assistant", plan_outcome.final_message)
                    trace.annotate(
                        route="planner", plan_steps=len(plan_outcome.plan.steps),
                        plan_dropped=plan_outcome.plan.dropped,
                        plan_ok=sum(1 for r in plan_outcome.results if r.status == "ok"),
                        plan_failed=sum(1 for r in plan_outcome.results if r.status == "failed"))
                    trace.end_turn(outcome="blocked" if plan_outcome.pending_confirmation else "ok")
                    finished = True
                    yield plan_outcome.final_message
                    return

            exact = exact_answers.answer(user_text) if attachment is None else None   # Tier 42
            if exact:
                db.save_message("assistant", exact)
                trace.annotate(route="exact_answer")
                trace.end_turn(outcome="ok")
                finished = True
                yield exact
                return
            semantic = self._semantic_intent(user_text, attachment)   # Tier 45
            if semantic is not None:
                db.save_message("assistant", semantic)
                trace.end_turn(outcome="ok", route="semantic_intent")
                finished = True
                yield semantic
                return
            self._route_turn(user_text, attachment)   # Tier 34: fast or main model
            cached = self._cached_answer(user_text, attachment)   # Tier 40
            if cached:
                db.save_message("assistant", cached)
                trace.annotate(route="response_cache")
                trace.end_turn(outcome="ok")
                finished = True
                yield cached
                return
            messages = [self.get_dynamic_system_prompt(user_text)]
            history = self._recent_history(limit=16)
            for role, msg in history:
                messages.append({"role": role, "content": msg})

            if attachment and attachment.get("kind") == "document":
                doc_note = " (truncated)" if attachment.get("truncated") else ""
                wrapped_text = content_trust.wrap_untrusted(
                    attachment.get('text_content', ''),
                    source=f"an attached file ({attachment.get('name', 'file')})",
                )
                user_content = (
                    f"{user_text}\n\n"
                    f"--- Attached file: {attachment.get('name', 'file')}{doc_note} ---\n"
                    f"{wrapped_text}"
                )
            else:
                user_content = user_text
            messages.append({"role": "user", "content": user_content})
            self._ground(messages, user_text, attachment)   # Tier 47: time-sensitive -> search first

            governor = _ToolLoopGovernor()
            all_results = []
            final_text = None
            # Tier 2: tracks whether ANY content has actually been handed
            # to the caller yet. Normally the per-chunk yield below covers
            # it - this only matters for the rare case where a round
            # legitimately streams zero usable content (a provider quirk,
            # or a retry that lands on a differently-shaped response) and
            # ends with neither more tool calls nor any text: without
            # this, that round's `final_text = ""` would short-circuit
            # straight to `reply = final_text` with NOTHING ever yielded,
            # breaking this generator's own "at least one chunk, always"
            # contract. See the final_text-is-empty check below.
            streamed_anything = False

            while governor.can_continue():
                if cancelled():
                    raise _TurnCancelled()
                governor.start_round()
                _llm_t0 = time.perf_counter()
                chunk_stream = self._chat_completion_stream(messages, tool_choice="auto", max_tokens=1600,
                                                            timeout=governor.remaining_time())
                content_parts = []
                tool_calls_acc = {}   # index -> {"id", "name", "arguments"} (arguments accumulate)
                role = "assistant"

                try:
                    for chunk in chunk_stream:
                        choice = chunk.choices[0]
                        delta = choice.delta
                        if delta.role:
                            role = delta.role
                        if delta.content:
                            if not content_parts and streamed_parts:
                                # A later round's text after an earlier
                                # round's "thinking out loud" - keep them
                                # apart (Tier 11).
                                streamed_parts.append("\n\n")
                                yield "\n\n"
                            content_parts.append(delta.content)
                            # Streamed as it arrives on every round, not just a
                            # round we already know is final - see docstring.
                            # The rare case where this round turns out to end
                            # in tool_calls after all just means a few words of
                            # "thinking out loud" were already shown/spoken;
                            # they're simply not reused below, and nothing
                            # about the saved/returned reply depends on them.
                            streamed_anything = True
                            streamed_parts.append(delta.content)
                            yield delta.content
                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                slot = tool_calls_acc.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                                if tc.id:
                                    slot["id"] = tc.id
                                if tc.function:
                                    if tc.function.name:
                                        slot["name"] += tc.function.name
                                    if tc.function.arguments:
                                        slot["arguments"] += tc.function.arguments
                finally:
                    # Releases the HTTP stream right away when the turn is
                    # cut off mid-reply, instead of whenever it's collected.
                    close_stream = getattr(chunk_stream, "close", None)
                    if callable(close_stream):
                        try:
                            close_stream()
                        except Exception:
                            pass

                trace.add_duration("llm_ms", time.perf_counter() - _llm_t0)
                if tool_calls_acc:
                    if cancelled():
                        raise _TurnCancelled()   # never act on a turn the user already called off
                    ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                    tool_calls_payload = [
                        {"id": (t["id"] or f"call_{idx}"), "type": "function",
                         "function": {"name": t["name"], "arguments": t["arguments"] or "{}"}}
                        for idx, t in enumerate(ordered)
                    ]
                    messages.append({"role": role, "content": "".join(content_parts) or "",
                                     "tool_calls": tool_calls_payload})
                    for call in tool_calls_payload:
                        if cancelled():
                            raise _TurnCancelled()
                        _tool_t0 = time.perf_counter()
                        result = self._dispatch_tool(call["function"]["name"], call["function"]["arguments"])
                        trace.add_duration("tool_ms", time.perf_counter() - _tool_t0)
                        trace.count("tools_called")
                        all_results.append(result)
                        messages.append({"role": "tool", "tool_call_id": call["id"],
                                         "content": str(result) if result else "Done."})
                    continue

                final_text = "".join(content_parts)
                break

            to_yield = None
            if final_text:
                reply = final_text
                if not all_results:
                    self._remember_answer(user_text, reply)   # Tier 40
            elif final_text is not None and not streamed_anything:
                # The round ended with no more tool calls AND no text ever
                # arrived (final_text == "") - nothing was yielded above,
                # so this generator would otherwise end silently empty.
                # Compose from whatever tool results exist, if any,
                # exactly like the all_results branch below; otherwise
                # fall back to the same "taking longer than expected"
                # message think() itself uses when it truly has nothing.
                reply = (self._compose_reply_from_tools(messages, all_results) if all_results
                         else self._retry_empty_answer(messages, governor)) or \
                    "That's taking longer than expected - could you try again or narrow the request a bit?"
                to_yield = reply
            elif all_results:
                if governor.hit_limit:
                    logger.warning(f"Tool loop stopped early ({governor.hit_limit} limit) after "
                                   f"{governor.step} round(s) - composing from {len(all_results)} result(s) gathered so far.")
                reply = self._compose_reply_from_tools(messages, all_results) or "Done."
                to_yield = ("\n\n" + reply) if streamed_parts else reply
            else:
                reply = "That's taking longer than expected - could you try again or narrow the request a bit?"
                to_yield = ("\n\n" + reply) if streamed_parts else reply

            # Saved BEFORE the last yield: a caller that stops iterating as
            # soon as it has the final text must not lose the history row.
            db.save_message("assistant", reply)
            trace.annotate(route="tool_loop", rounds=governor.step, model=self._answered_by(),
                           provider=self.provider.name if self.provider else None)
            trace.end_turn(outcome="ok")
            finished = True
            if to_yield:
                yield to_yield

        except _TurnCancelled:
            logger.info("think_stream: turn cancelled by the caller")
            save_interrupted()
            finished = True
            return

        except GeneratorExit:
            # The caller stopped reading part-way (cancel, newer message,
            # app closing). Record what the user actually got, then let the
            # generator close - nothing may be yielded from here.
            if not finished:
                save_interrupted()
            raise

        except Exception as e:
            logger.error(f"Streaming think() failed, falling back: {e}")
            trace.mark("fallback_triggered", reason=type(e).__name__)
            reply = self._reply_after_failure(e, user_text, attachment, cancel_event)
            db.save_message("assistant", reply)
            trace.end_turn(outcome="error", route="exception_fallback", error=str(e)[:300])
            finished = True
            # A reply that broke off mid-stream gets the fallback as a new
            # paragraph, not glued onto its last half-word.
            yield ("\n\n" + reply) if streamed_parts else reply


# Built from whatever every @skill(...)-decorated WaveBrain method
# registered above, in the same order they were defined - this is the
# exact same {"type": "function", ...} schema list the old hand-written
# TOOLS = [...] produced, just generated instead of hand-kept-in-sync.
# Both the Groq path (think()) and the Ollama fallback path
# (_think_with_ollama()) import this one name, same as before.
# NOTE (Phase 4): this is only a snapshot of the built-in skills, kept for anything
# that still imports the name. Live code calls skills.tool_specs() on every request,
# so skills hot-loaded from user_skills/ are included without a restart.
TOOLS = skills.tool_specs()
