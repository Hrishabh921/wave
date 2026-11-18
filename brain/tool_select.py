"""
brain/tool_select.py — Tier 28: send the LLM only the tools a turn can use.

Wave registers 180+ tools. Groq and OpenAI reject any request carrying more
than 128 of them ("'tools': maximum number of items is 128"), and even 128
is too big for Groq's free tier: every tool definition costs ~55 tokens,
so 128 of them plus the system prompt came to ~8,500 tokens against a
limit of 8,000 tokens a MINUTE ("413 Request too large"). Either way every
reply that needed the LLM failed. select_tool_specs() keeps each request
at config.LLM_MAX_TOOLS (default 20, ~2,700 tokens), choosing in this order:

  1. tools this conversation already called (its later rounds must still
     be able to see them),
  2. tools the caller always wants - Wave passes the user's own dropped-in
     skills, which they added on purpose,
  3. the tools the turn's own words point at most strongly (the last user
     message, plus the one before for follow-ups like "aur thoda"),
     matched against each tool's name and description with a little
     Hinglish help (awaaz -> volume, kholo -> open, ...) - up to half the
     limit,
  4. ESSENTIAL_TOOLS, the handful almost any question might need,
  5. then the remaining matches - and nothing else: Tier 29 stopped padding
     the list up to the limit with tools the turn gave no sign of needing
     (that padding cost ~700 tokens on every chatty question).
     CORE_TOOLS, the everyday set, now only breaks ties between matches.

Most plain Hinglish commands never get here: brain/intent_router.py answers
them directly without the LLM. This is for everything free-form.

Pure Python, no new dependencies; a few hundred microseconds per request.
"""
import logging
import math
import re

logger = logging.getLogger("ToolSelect")

# What almost any open question might need - always sent (every name here
# and in CORE_TOOLS must be a registered skill; tests/test_tier28_tool_select.py
# checks).
ESSENTIAL_TOOLS = (
    "get_live_information", "search_google", "get_weather", "get_current_time", "get_current_date", "calculate",
    "set_reminder", "open_app", "open_website", "play_youtube", "take_note", "remember_this",
)
# The everyday set - sent next, while there's room.
CORE_TOOLS = (
    "open_app", "close_app", "open_website", "search_google", "get_live_information", "get_weather", "get_news",
    "summarize_page", "daily_briefing", "play_youtube", "play_on_spotify", "media_play_pause", "media_next_track",
    "media_previous_track", "volume_up", "volume_down", "set_volume", "mute_volume", "unmute_volume",
    "set_brightness", "brightness_up", "brightness_down", "set_reminder", "list_reminders", "cancel_reminder",
    "set_timer", "list_timers", "cancel_timer", "take_note", "read_notes", "add_todo", "list_todos",
    "complete_todo", "search_my_notes", "remember_this", "recall_memory", "forget_memory",
    "create_calendar_event", "list_calendar_events", "get_current_time", "get_current_date", "calculate",
    "convert_units", "get_joke", "take_screenshot", "read_screen_text", "describe_screen", "type_text",
    "press_key", "lock_pc", "create_folder", "create_file", "search_file", "send_whatsapp_message",
    "undo_last_action", "list_my_skills", "why_did_you_do_that", "show_ui", "hide_ui", "get_battery_status",
    "get_pc_info",
)

# Hinglish (and a few English) words -> what tool names/descriptions say.
_SYNONYMS = {
    "awaaz": "volume sound audio", "awaz": "volume sound audio", "aawaz": "volume sound audio",
    "roshni": "brightness screen", "ujala": "brightness",
    "gaana": "song music play", "gana": "song music play", "gaane": "song music play", "bajao": "play music",
    "kholo": "open", "khol": "open", "kholna": "open", "chalu": "open start enable turn",
    "band": "close stop off", "bandh": "close stop off",
    "chalao": "play run start", "chala": "play run start",
    "yaad": "remind reminder remember memory", "dilana": "remind reminder", "dilao": "remind reminder",
    "likho": "write note type", "likh": "write note type",
    "dhundo": "search find", "dhoondo": "search find", "khojo": "search find", "dhundho": "search find",
    "samay": "time", "baje": "time", "waqt": "time", "tareekh": "date", "tarikh": "date",
    "mausam": "weather", "barish": "weather rain", "khabar": "news", "khabren": "news", "samachar": "news",
    "tasveer": "photo picture screenshot", "photo": "photo picture camera",
    "mitao": "delete remove", "hatao": "delete remove cancel", "hata": "delete remove cancel",
    "padho": "read", "padh": "read", "suno": "listen",
    "bhejo": "send message", "bhej": "send message",
    "hisaab": "calculate math", "jodo": "calculate add", "badlo": "change set convert", "badal": "change set convert",
    "tez": "increase up", "badhao": "increase up", "zyada": "increase up", "kam": "decrease down",
    "dheere": "decrease down", "dikhao": "show", "chhupao": "hide",
    "kal": "tomorrow", "aaj": "today", "ghanta": "hour", "alarm": "reminder timer alarm",
    "laptop": "pc system computer", "computer": "pc system", "khidki": "window",
    "kaam": "task todo", "sound": "volume audio", "song": "music play", "picture": "photo screenshot",
}
_STOP = frozenset((
    "the a an to of for and or is are be it this that my me you your please can could would will do does what "
    "how with on in at from by as so just now karo kar de dena hai hain ho ki ka ke ko se mein main mujhe mera "
    "meri mere aur ya toh na nahi bhi wala wali ek kya kaise ye yeh vo woh us plz pls zara thoda jara i"
).split())
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def _stem(w: str) -> str:
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _terms(text: str):
    return [_stem(w) for w in _WORD.findall((text or "").lower()) if len(w) > 1 and w not in _STOP]


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(str(_get(p, "text", "") or "") for p in content if _get(p, "type") == "text")
    return ""


def _name(spec) -> str:
    return (spec.get("function") or {}).get("name", "")


def called_tools(messages) -> list:
    """Tool names any assistant message in `messages` already asked for."""
    out = []
    for m in messages or ():
        for tc in _get(m, "tool_calls") or ():
            fn = _get(tc, "function")
            name = _get(fn, "name") if fn is not None else None
            if name and name not in out:
                out.append(name)
    return out


def query_weights(messages) -> dict:
    """{term: weight} from the last user message (1.0) and the one before
    (0.5), plus Hinglish synonyms at a little less."""
    users = [_text_of(_get(m, "content")) for m in messages or () if _get(m, "role") == "user"]
    weights = {}
    for text, w in ((users[-1] if users else "", 1.0), (users[-2] if len(users) > 1 else "", 0.5)):
        for raw in _WORD.findall((text or "").lower()):
            extra = _SYNONYMS.get(raw)
            for t in _terms(raw):
                weights[t] = max(weights.get(t, 0.0), w)
            if extra:
                for t in _terms(extra):
                    weights[t] = max(weights.get(t, 0.0), 0.8 * w)
    return weights


class _Index:
    def __init__(self, specs):
        self.docs = []
        df = {}
        for spec in specs:
            fn = spec.get("function") or {}
            doc = {}
            for t in _terms(fn.get("description", "")):
                doc[t] = max(doc.get(t, 0.0), 1.0)
            for t in _terms((fn.get("name") or "").replace("_", " ")):
                doc[t] = 3.0
            for p in ((fn.get("parameters") or {}).get("properties") or {}):
                for t in _terms(str(p).replace("_", " ")):
                    doc[t] = max(doc.get(t, 0.0), 0.5)
            self.docs.append(doc)
            for t in doc:
                df[t] = df.get(t, 0) + 1
        n = max(1, len(specs))
        self.idf = {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}

    def scores(self, weights):
        return [sum(qw * self.idf.get(t, 0.0) * doc.get(t, 0.0) for t, qw in weights.items()) for doc in self.docs]


_cache = {}


def _index_for(specs):
    key = tuple(id(s) for s in specs)
    idx = _cache.get(key)
    if idx is None:
        if len(_cache) > 8:
            _cache.clear()
        idx = _cache[key] = _Index(specs)
    return idx


_logged_trim = False


def select_tool_specs(specs, messages, limit=None, *, always=()) -> list:
    """At most `limit` of `specs` (config.LLM_MAX_TOOLS when None; 0 = no
    limit), in their original order - see this module's docstring for what
    gets in first."""
    global _logged_trim
    specs = list(specs or ())
    if limit is None:
        from settings.config import config
        limit = int(getattr(config, "LLM_MAX_TOOLS", 20) or 0)
    if not limit or len(specs) <= limit:
        return specs
    names = [_name(s) for s in specs]
    known = set(names)
    order = {n: i for i, n in enumerate(names)}
    scores = _index_for(specs).scores(query_weights(messages))
    core = set(CORE_TOOLS)
    ranked = sorted((n for n, sc in zip(names, scores) if sc > 0),
                    key=lambda n: (-scores[order[n]], n not in core, order[n]))
    chosen = []
    seen = set()

    def take(n):
        if n in known and n not in seen and len(chosen) < limit:
            seen.add(n)
            chosen.append(n)

    first = max(8, limit // 2)           # strongest matches go in ahead of the essentials
    for n in called_tools(messages):
        take(n)
    for n in always or ():
        take(n)
    for n in ranked[:first]:
        take(n)
    for n in ESSENTIAL_TOOLS:
        take(n)
    for n in ranked[first:]:
        take(n)
    if not _logged_trim:
        _logged_trim = True
        logger.info(f"Sending at most {limit} of {len(specs)} tools per LLM request (LLM_MAX_TOOLS={limit}).")
    return [s for s, n in zip(specs, names) if n in seen]
