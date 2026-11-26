"""
brain/intent_router.py — Tier 2: fast deterministic intent layer
==================================================================

Sits in front of WaveBrain.think() / think_stream(), right before the
tool-calling loop would otherwise make a Groq/OpenAI round-trip. For a
curated set of short, unambiguous, zero-creativity commands - "pause the
music", "volume up", "take a screenshot", "lock my pc" - there is exactly
one sane interpretation and exactly one registered skill that should run.
Sending that whole exchange through the LLM only adds latency and API
cost for an answer that was never really in question.

    routed = intent_router.route(user_text)
    if routed is not None:
        reply = self._dispatch_tool(routed.skill, routed.args, source="intent_router")

route() NEVER touches AppController/FileManager/WebManager/etc. directly
and never decides anything about confirmation - it only maps raw text to
(skill name, args dict) for an ALREADY-REGISTERED skill (see
brain/skills_registry.py). The caller runs that through the exact same
_dispatch_tool() every LLM-chosen tool call goes through, so audit
logging, Phase 7's confirm-before-sensitive-action gate, and required-
argument validation all apply identically either way. A matched
confirm=True skill (close_app, delete_latest_screenshot, ...) still arms
a pending confirmation and asks first - it is NOT run silently just
because the router found it instead of the model. This is what makes a
fairly broad rule set here safe: a wrong match is never worse than the
LLM itself choosing the same tool, because both paths end at the same
gate.

Scope, on purpose
------------------
This is a curated v1, not a port of every branch
brain/groq_core.py's older _execute_single_action_inner() keyword matcher
covers (that one is untouched - it remains the deterministic fallback
used when no LLM is configured at all, or the LLM call itself errors out,
and it calls backends directly rather than going through a registered
skill, so it has no confirm-gate of its own to lean on the way this
module does). intent_router.py instead covers the highest-frequency,
lowest-ambiguity slice: media/volume/mute, window & screenshot
management, pc info/clipboard/screen-OCR, lock, undo, simple
open/close <app>, a plain search or YouTube play, basic wifi/bluetooth/
hotspot toggling, and "create a folder". Anything needing more context
(file names with unpredictable phrasing, reminders, calendar, notes,
workflows, remember/forget, WhatsApp) is deliberately left to the model
for now - each is a reasonable future rule, just not worth folding in
here without its own tests and its own look at the ambiguity trade-off.

A rule only belongs on this list if BOTH hold:
  * there's no real argument-extraction ambiguity (a fixed phrase, or a
    trivial "everything after this verb" split), and
  * firing on the wrong message is harmless - the user gets an odd/
    unhelpful short reply and can just rephrase, never anything
    destructive (confirm-gated skills stay gated regardless of who
    called them - see above).

Matching a trigger phrase is not the same as matching INTENT
-----------------------------------------------------------------
"lock the pc" and "don't lock the pc" both contain the phrase "lock the
pc", but they mean opposite things - a _contains/_prefix_arg rule that
only checks for the phrase can't tell them apart, and "harmless if wrong"
stops being true the moment "wrong" means doing the thing the user
explicitly said not to. route() guards against this ONCE, centrally,
before any rule runs: a message that's negated, conditional, or merely a
question/hypothetical about a trigger phrase (see _NEGATION_MARKERS /
_CONDITIONAL_MARKERS / _HYPOTHETICAL_MARKERS below) is treated as
ambiguous, same as a compound one, and falls through to the LLM instead.
The _prefix_arg rules (open_app/close_app/play_youtube) get one more,
narrower guard of their own (_PREFIX_ARG_IDIOM_STARTS /
_MAX_PREFIX_ARG_WORDS): "open"/"close"/"play" as the first word of a
message is also how several common idioms start ("close enough", "play
devil's advocate") without being commands at all.
"""
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

RouteResult = Optional[Tuple[str, Dict[str, str]]]
_Matcher = Callable[[str], RouteResult]


@dataclass(frozen=True)
class RoutedIntent:
    """route()'s result: an already-registered skill name plus the args
    to call it with (see brain/skills_registry.py), and which rule
    matched (for logging/tests only - never sent to a tool)."""
    skill: str
    args: Dict[str, str] = field(default_factory=dict)
    rule: str = ""


@dataclass(frozen=True)
class _Rule:
    name: str
    matcher: _Matcher


# ---------------------------------------------------------------------------
# Matcher builders - each returns a function (lowercased, trimmed text) ->
# (skill, args) | None, so _RULES below stays a plain, readable table.
# ---------------------------------------------------------------------------
def _exact(skill: str, *phrases: str) -> _Matcher:
    """Matches only when the ENTIRE message is one of `phrases` - for
    short commands ("pause", "mute") too risky to match as a mere
    substring of a longer, unrelated sentence."""
    phrase_set = frozenset(phrases)

    def m(lower: str) -> RouteResult:
        return (skill, {}) if lower in phrase_set else None
    return m


def _contains(skill: str, *phrases: str) -> _Matcher:
    """Matches when any of `phrases` appears anywhere in the message."""
    def m(lower: str) -> RouteResult:
        return (skill, {}) if any(p in lower for p in phrases) else None
    return m


def _phrase_as_words(lower: str, phrase: str) -> bool:
    """Word-boundary-safe phrase containment: True only if `phrase` appears
    as a run of CONSECUTIVE whole words in `lower`, never merely as a
    substring - same reasoning _has_standalone_negator (below) already
    documents for why a bare 'mat' can't be a plain substring check
    ('automated', 'format', 'climate', ...). Needed for a trailing word
    that's itself a real-word PREFIX of something unrelated: 'tab' is a
    prefix of 'tablet', so a plain substring check on 'new tab' would
    wrongly fire on the ordinary sentence 'I got a new tablet'."""
    words = lower.split()
    phrase_words = phrase.split()
    n = len(phrase_words)
    return any(words[i:i + n] == phrase_words for i in range(len(words) - n + 1))


def _contains_words(skill: str, *phrases: str) -> _Matcher:
    """Like _contains() above, but each phrase must match as whole,
    consecutive words (_phrase_as_words) rather than a bare substring -
    for the handful of Tier 15 browser-tab rules where this actually
    matters (see new_tab/next_tab/previous_tab below and their shared
    'tab'-is-a-prefix-of-'tablet' comment)."""
    def m(lower: str) -> RouteResult:
        return (skill, {}) if any(_phrase_as_words(lower, p) for p in phrases) else None
    return m


def _exact_or_contains(skill: str, exact_phrases: Tuple[str, ...] = (),
                        contains_phrases: Tuple[str, ...] = ()) -> _Matcher:
    exact_set = frozenset(exact_phrases)

    def m(lower: str) -> RouteResult:
        if lower in exact_set or any(p in lower for p in contains_phrases):
            return (skill, {})
        return None
    return m


# _prefix_arg below fires on "open "/"close "/"play "/... being the FIRST
# word of the message - which these common idioms also are, without being
# commands at all. Checked against what's left AFTER the verb (not the
# whole message), so a startswith check is enough and a genuine app name
# that happens to contain "enough" elsewhere isn't caught by accident.
# Not exhaustive - idioms are a long tail no fixed list fully covers - but
# these are the everyday ones; anything else slipping through here still
# isn't a safety issue (see the module docstring's second paragraph),
# just an occasional "couldn't find that app" instead of the LLM
# correctly recognizing small talk.
_PREFIX_ARG_IDIOM_STARTS = (
    "enough", "to ", "along", "fair", "safe", "dumb", "hard to get", "hooky",
    "devil's advocate", "devils advocate", "the deal", "a deal", "it cool",
    "it safe", "catch", "favorites", "ball",
)

# A real app name or short search/media query is essentially never longer
# than this - past it, `value` is far more likely to be the tail end of an
# ordinary sentence that happened to start with a trigger verb ("close the
# deal with the client") than an actual command. Bailing out here can only
# fall back to the LLM handling it, same as any other non-match.
_MAX_PREFIX_ARG_WORDS = 5


def _prefix_arg(skill: str, arg_name: str, *prefixes: str) -> _Matcher:
    """Matches a leading verb phrase ('open ', 'play ', ...) and feeds
    whatever non-empty text follows it to `arg_name`. No match if the
    prefix is the whole message (nothing left to act on), what follows is
    one of _PREFIX_ARG_IDIOM_STARTS, or it's longer than
    _MAX_PREFIX_ARG_WORDS words."""
    def m(lower: str) -> RouteResult:
        for p in prefixes:
            if lower.startswith(p):
                value = lower[len(p):].strip()
                if not value:
                    return None
                if any(value.startswith(idiom) for idiom in _PREFIX_ARG_IDIOM_STARTS):
                    return None
                if len(value.split()) > _MAX_PREFIX_ARG_WORDS:
                    return None
                return (skill, {arg_name: value})
        return None
    return m


def _suffix_arg(skill: str, arg_name: str, *suffixes: str) -> _Matcher:
    """🔧 Tier 8: the mirror image of _prefix_arg, for verb-FINAL phrasing -
    Hindi/Hinglish puts the verb last ('chrome kholo'/'क्रोम खोलो' = lit.
    'chrome open', not 'open chrome'), where English puts it first.
    Matches a trailing verb phrase anchored to the END of the message and
    feeds whatever non-empty text precedes it to `arg_name`. No match if
    the suffix is the whole message (nothing left to act on) or what
    precedes it is longer than _MAX_PREFIX_ARG_WORDS words - same word-
    count safety net _prefix_arg uses, for the same reason (past that
    length it's far more likely to be the tail of an ordinary sentence
    that happens to end the same way, e.g. '... maine already khol diya
    tha', than an actual short command). Deliberately no idiom-exclusion
    list to match _PREFIX_ARG_IDIOM_STARTS - this covers two narrow,
    specific verb phrases per caller rather than a common verb like
    'open'/'close' on its own, so the same long-tail idiom collision risk
    _PREFIX_ARG_IDIOM_STARTS exists for isn't really present here; if one
    ever turns up in practice, the fix is the same kind of list, not a
    structural change to this function."""
    def m(lower: str) -> RouteResult:
        for suf in suffixes:
            if lower.endswith(suf):
                value = lower[:-len(suf)].strip()
                if not value:
                    return None
                if len(value.split()) > _MAX_PREFIX_ARG_WORDS:
                    return None
                return (skill, {arg_name: value})
        return None
    return m


def _prefix_or_suffix_arg(skill: str, arg_name: str, prefixes: Tuple[str, ...] = (),
                           suffixes: Tuple[str, ...] = ()) -> _Matcher:
    """Tier 8: combines _prefix_arg and _suffix_arg under one rule, same
    shape as _exact_or_contains combining _exact+_contains above -
    open_app/close_app need BOTH a verb-first English form ('open chrome')
    and a verb-final Hindi/Hinglish one ('chrome kholo') to reach the
    exact same skill+arg."""
    prefix_m = _prefix_arg(skill, arg_name, *prefixes) if prefixes else (lambda lower: None)
    suffix_m = _suffix_arg(skill, arg_name, *suffixes) if suffixes else (lambda lower: None)

    def m(lower: str) -> RouteResult:
        return prefix_m(lower) or suffix_m(lower)
    return m


_SOUND_WORDS = frozenset("sound awaaz aawaz awaz volume audio music gaana gaane gana speaker speakers".split())


def _not_sound(matcher: _Matcher) -> _Matcher:
    """Tier 50 (bench/heldout/): "sound bilkul band kar do" means mute, not
    close an app called "sound bilkul" - an "app" named by a sound word
    isn't routed (the semantic fallback and the model take it)."""
    def m(lower: str) -> RouteResult:
        result = matcher(lower)
        if result is not None and set(str(result[1].get("app_name", "")).split()) & _SOUND_WORDS:
            return None
        return result
    return m


def _delete_screenshot(lower: str) -> RouteResult:
    return ("delete_latest_screenshot", {}) if "delete" in lower and "screenshot" in lower else None


# Tier 15: press_key normally takes a free-form chord and is deliberately
# left to the LLM entirely (same reasoning that already keeps move_window/
# resize_window off this list - a real argument to extract). This is the
# one exception: a short list of single, harmless, totally unambiguous
# named keys where "press enter" IS the whole command, nothing to extract -
# same derived-fixed-args shape as _toggle_hardware above, not a window
# into press_key generally.
_PRESS_FIXED_KEYS = {
    "enter": ("press enter", "hit enter", "press the enter key"),
    "escape": ("press escape", "hit escape", "press esc", "hit esc"),
    "tab": ("press tab", "hit tab"),
    "space": ("press space", "hit space", "press spacebar", "hit spacebar"),
    "backspace": ("press backspace", "hit backspace"),
}


def _press_fixed_key(lower: str) -> RouteResult:
    for key, phrases in _PRESS_FIXED_KEYS.items():
        if any(p in lower for p in phrases):
            return "press_key", {"key_combo": key}
    return None


def _toggle_hardware(lower: str) -> RouteResult:
    devices = ("wifi", "wi-fi", "bluetooth", "hotspot", "वाईफाई", "ब्लूटूथ", "हॉटस्पॉट")
    # Tier 11: "enable/disable wifi" and the Hinglish/Devanagari verb-final
    # forms used to be caught only by the UI's own keyword layer (removed -
    # it bypassed the confirm gate and the audit log). Without them here,
    # "wifi band karo" fell through to close_app's " band karo" suffix and
    # asked to close an app called "wifi".
    verbs = ("turn on", "turn off", "switch on", "switch off", "enable", "disable",
             "band karo", "band kar do", "chalu karo", "chalu kar do", "on karo", "off karo",
             "on kar do", "off kar do", "बंद करो", "चालू करो", "ऑन करो", "ऑफ करो")
    if any(d in lower for d in devices) and any(v in lower for v in verbs):
        instruction = (lower.replace("wi-fi", "wifi").replace("वाईफाई", "wifi").replace("ब्लूटूथ", "bluetooth")
                       .replace("हॉटस्पॉट", "hotspot"))
        # Tier 44: one radio and a clear on/off -> the skill that really
        # switches it (and asks first before turning one OFF); the hotspot,
        # both radios at once, or no clear direction stay toggle_hardware.
        kinds = [k for k in ("wifi", "bluetooth") if k in instruction]
        on = any(v in lower for v in _RADIO_ON_VERBS)
        off = any(v in lower for v in _RADIO_OFF_VERBS)
        if len(kinds) == 1 and "hotspot" not in instruction and on != off:
            return f"turn_{kinds[0]}_{'on' if on else 'off'}", {}
        if len(kinds) > 1:
            return None          # both radios: the model calls each one's own (confirm-gated) tool
        return "toggle_hardware", {"instruction": instruction}
    return None


_RADIO_ON_VERBS = ("turn on", "switch on", "enable", "chalu karo", "chalu kar do", "on karo", "on kar do",
                   "चालू करो", "ऑन करो")
_RADIO_OFF_VERBS = ("turn off", "switch off", "disable", "band karo", "band kar do", "off karo", "off kar do",
                    "बंद करो", "ऑफ करो")


# Tier 44: the light/dark theme. A bare "dark mode" is a command; a sentence
# merely mentioning it ("what is dark mode?") isn't - so the phrase has to
# be the whole message or come with a verb. Light first: "dark mode band
# karo" means light mode.
_LIGHT_EXACT = ("light mode", "light theme", "dark mode off", "लाइट मोड")
_LIGHT_VERBS = ("turn on light mode", "switch to light mode", "enable light mode", "turn off dark mode",
                "disable dark mode", "dark mode off karo", "dark mode band karo", "dark mode band kar do",
                "dark mode hatao", "light mode lagao", "light mode on karo", "light mode kar do",
                "light mode chalu karo", "डार्क मोड बंद करो", "लाइट मोड चालू करो", "लाइट मोड लगाओ")
_DARK_EXACT = ("dark mode", "dark theme", "dark mode on", "डार्क मोड")
_DARK_VERBS = ("turn on dark mode", "switch to dark mode", "enable dark mode", "set dark mode", "dark mode on karo",
               "dark mode chalu karo", "dark mode lagao", "dark mode kar do", "dark theme lagao",
               "डार्क मोड चालू करो", "डार्क मोड लगाओ")


def _theme_mode(lower: str) -> RouteResult:
    text = lower.strip().rstrip(".!")
    if text in _LIGHT_EXACT or any(p in lower for p in _LIGHT_VERBS):
        return "switch_to_light_mode", {}
    if text in _DARK_EXACT or any(p in lower for p in _DARK_VERBS):
        return "switch_to_dark_mode", {}
    return None


# Tier 11: "open display settings" used to reach open_app("display settings")
# here (and only the UI's removed keyword layer sent it to the settings app).
# The backends match a settings page by substring of this value, so the
# phrase is passed through with a few spoken spellings normalized.
_SETTING_ALIASES = (
    ("night light", "nightlight"), ("wi-fi", "wifi"), ("wi fi", "wifi"),
    ("personalisation", "personalization"), ("wallpaper", "personalization"),
    ("theme", "personalization"), ("background", "personalization"),
    ("brightness", "display"), ("volume", "sound"), ("windows update", "update"),
    ("app ", "apps "), ("system", ""), ("windows", ""), ("pc", ""), ("computer", ""),
)


def _open_settings(lower: str) -> RouteResult:
    value = None
    for p in ("open ", "launch ", "show ", "go to "):
        if lower.startswith(p):
            value = lower[len(p):].strip()
            break
    if value is None:
        for suf in (" kholo", " khol do", " खोलो"):
            if lower.endswith(suf):
                value = lower[:-len(suf)].strip()
                break
    if not value:
        return None
    if value.startswith("the "):
        value = value[4:]
    if value.startswith("my "):
        value = value[3:]
    words = value.split()
    if not words or words[-1] not in ("settings", "setting", "सेटिंग्स", "सेटिंग") or len(words) > 4:
        return None
    setting = " ".join(words[:-1]).strip()
    for spoken, key in _SETTING_ALIASES:
        setting = (setting + " ").replace(spoken, key).strip()
    if setting and not any(key in setting for key in _SETTING_KEYS):
        return None   # "open chrome settings" is about an app, not a system page - LLM's call
    return "open_system_setting", {"setting": setting or "settings"}


# The settings pages every backend knows (automation/backends/*.py).
_SETTING_KEYS = (
    "display", "sound", "bluetooth", "network", "wifi", "update", "apps", "personalization",
    "privacy", "storage", "battery", "power", "about", "nightlight", "airplane",
)


# Tier 11: "write <text> in notepad" / "notepad mein <text> likho". The text
# is restored to its original casing by route() - see _CASE_PRESERVING_ARGS.
def _write_notepad(lower: str) -> RouteResult:
    for verb in ("write ", "type "):
        if not lower.startswith(verb):
            continue
        rest = lower[len(verb):]
        for tail in (" in notepad", " on notepad", " to notepad", " into notepad"):
            if rest.endswith(tail):
                text = rest[:-len(tail)].strip()
                return ("write_notepad", {"text": text}) if text else None
    for head in ("notepad mein ", "notepad me ", "notepad par ", "notepad pe ", "नोटपैड में "):
        if lower.startswith(head):
            rest = lower[len(head):]
            for tail in (" likho", " likh do", " type karo", " लिखो", " लिख दो"):
                if rest.endswith(tail):
                    text = rest[:-len(tail)].strip()
                    return ("write_notepad", {"text": text}) if text else None
    return None


def _search_or_play(lower: str) -> RouteResult:
    """'search '/'search for ' is one prefix shared by two different
    skills, decided by what follows: a named browser needs
    open_website's site+browser handling (left to the LLM - not attempted
    here), 'youtube' anywhere in the query means play_youtube, anything
    else means a plain search_google."""
    query = None
    for p in ("search for ", "search "):
        if lower.startswith(p):
            query = lower[len(p):].strip()
            break
    if not query:
        return None
    for b in ("brave", "chrome", "edge", "firefox", "opera"):
        if any(query.endswith(f" {prep} {b}") for prep in ("on", "in", "using")):
            return None
    if "youtube" in query:
        return "play_youtube", {"query": query.replace("on youtube", "").strip() or query}
    return "search_google", {"query": query}


def _create_folder(lower: str) -> RouteResult:
    base_phrases = ("create a folder", "make a folder", "new folder")
    if not any(b in lower for b in base_phrases):
        return None
    variants = sorted(
        [f"{b} called" for b in base_phrases] + [f"{b} named" for b in base_phrases] + list(base_phrases),
        key=len, reverse=True,
    )
    name = lower
    for phrase in variants:
        if phrase in name:
            name = name.split(phrase, 1)[1].strip()
            break
    return "create_folder", {"folder_name": name or "New Folder"}


# ---------------------------------------------------------------------------
# Tier 17: custom matchers for the intent_router expansion (63 -> ~135
# fast-path rules, see docs/ROADMAP_BEAT_SARA.md's Tier 17 section for the
# full gap analysis: 166 skills total, 63 already routed before this tier).
# Each one below needed more than a plain _exact/_contains/_prefix_arg/
# _suffix_arg call - either a two-sided prefix+suffix extraction (same
# shape _write_notepad above already established), or a collision guard
# against an existing broader rule (open_app's "start "/"turn on "
# prefixes, play_youtube's bare "play " prefix). ~15 other missing skills
# deliberately get NO new rule at all: the 15 open_X_settings tools are
# already reachable through the existing generic _open_settings rule
# below (verified empirically against ~20 English/Hinglish/Devanagari
# phrasings - byte-for-byte the same AppController.open_system_setting()
# call either way), and minimize_all_windows is already reachable through
# show_desktop's existing "minimize all windows"/"minimize everything"
# phrases (the two skills do the same action per minimize_all_windows's
# own description). Writing dedicated rules for any of these would just
# duplicate a path that already works, inflating this file's rule count
# without actually covering anything new.
# ---------------------------------------------------------------------------
_ENABLE_MODE_NAMES = ("dnd", "focus", "gaming", "sleep", "study")


def _enable_mode(lower: str) -> RouteResult:
    """name is required (dnd/focus/gaming/sleep/study - the exact set
    enable_mode itself documents). "turn on " is also one of open_app's
    own prefixes (its _Rule sits right after this one's in _RULES - see
    the insertion point below) and "enable"/"disable" are also two of
    _toggle_hardware's verbs above - _toggle_hardware only fires when a
    wifi/bluetooth/hotspot device word is ALSO present and it runs
    earlier in _RULES, so "turn on wifi" is already claimed there and
    never reaches this far. open_app is the real hazard: an earlier
    version of this function matched ANY "turn on X"/"enable X" and
    returned it as a (wrong, made-up) mode name - caught by
    test_phase8_intent_router.py's
    test_turn_on_app_name_falls_back_to_open_app_when_not_hardware
    ("turn on the projector" must still reach open_app). Fixed by only
    ever matching a bare name when it's one of the five real ones, or
    any name at all when "mode" is literally also present - "turn on
    spotify"/"turn on the projector" (no recognized name, no " mode")
    correctly return None and fall through to open_app."""
    for prefix in ("enable ", "turn on "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):].strip()
            if rest.endswith(" mode"):
                name = rest[:-len(" mode")].strip()
                return ("enable_mode", {"name": name}) if name else None
            if rest in _ENABLE_MODE_NAMES:
                return ("enable_mode", {"name": rest})
            return None
    if lower.endswith(" mode on"):
        name = lower[:-len(" mode on")].strip()
        return ("enable_mode", {"name": name}) if name else None
    return None


def _start_service(lower: str) -> RouteResult:
    """service_name is required. Deliberately narrower than open_app's own
    "start " prefix (its _Rule sits right after this one's - see the
    insertion point below): every branch here requires the word
    "service" literally present, so "start spotify" still falls through
    to open_app correctly and only "start the bluetooth service"/"start
    service bluetooth" (etc.) are claimed here."""
    for prefix in ("start service ", "start the service "):
        if lower.startswith(prefix):
            name = lower[len(prefix):].strip()
            return ("start_service", {"service_name": name}) if name else None
    for prefix in ("start the ", "start "):
        if lower.startswith(prefix) and lower.endswith(" service"):
            name = lower[len(prefix):-len(" service")].strip()
            return ("start_service", {"service_name": name}) if name else None
    return None


def _stop_service(lower: str) -> RouteResult:
    """service_name is required. Mirrors _start_service above for
    symmetry - no existing rule currently uses a bare "stop " prefix the
    way open_app uses "start ", but requiring the word "service"
    explicitly here too is defensive rather than relying on that staying
    true forever."""
    for prefix in ("stop service ", "stop the service "):
        if lower.startswith(prefix):
            name = lower[len(prefix):].strip()
            return ("stop_service", {"service_name": name}) if name else None
    for prefix in ("stop the ", "stop "):
        if lower.startswith(prefix) and lower.endswith(" service"):
            name = lower[len(prefix):-len(" service")].strip()
            return ("stop_service", {"service_name": name}) if name else None
    return None


def _play_on_spotify(lower: str) -> RouteResult:
    """query is required. Placed right before play_youtube's own bare
    "play " _Rule (see the insertion point below) - play_youtube's
    _prefix_arg has no suffix requirement at all, so without this rule
    positioned first, "play X on spotify" would already fully match
    play_youtube's own prefix (" on spotify" is well under
    _MAX_PREFIX_ARG_WORDS) and never reach Spotify at all."""
    if not lower.startswith("play "):
        return None
    rest = lower[len("play "):]
    for suffix in (" on spotify", " spotify par", " spotify pe"):
        if rest.endswith(suffix):
            query = rest[:-len(suffix)].strip()
            return ("play_on_spotify", {"query": query}) if query else None
    return None


def _complete_todo(lower: str) -> RouteResult:
    """query is required, matched by the registry as a partial match (see
    the skill's own description) - a stray leading article surviving
    extraction is a cosmetic imperfection, not a correctness bug, unlike
    recall_memory/forget_memory's exact-key lookup below."""
    for prefix in ("complete the todo ", "complete todo ", "finish the todo "):
        if lower.startswith(prefix):
            query = lower[len(prefix):].strip()
            return ("complete_todo", {"query": query}) if query else None
    if lower.startswith("mark "):
        rest = lower[len("mark "):]
        for suffix in (" as done", " as complete", " as completed", " done", " complete"):
            if rest.endswith(suffix):
                query = rest[:-len(suffix)].strip()
                return ("complete_todo", {"query": query}) if query else None
    return None


def _delete_todo(lower: str) -> RouteResult:
    """query is required (partial match, same as complete_todo above).
    Placed before delete_file's own _Rule (see the insertion order below)
    - delete_file's bare "delete "/"remove " prefix has no suffix
    requirement, so a short todo-removal phrase like "delete buy milk
    todo" (3 words after "delete ") is well under delete_file's own
    5-word cap and would otherwise be wrongly claimed as a request to
    delete a file literally named "buy milk todo"."""
    for prefix in ("delete ", "remove "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):]
            for suffix in (" from my to-do list", " from my todo list", " from the to-do list",
                           " from the todo list", " to-do", " todo"):
                if rest.endswith(suffix):
                    query = rest[:-len(suffix)].strip()
                    return ("delete_todo", {"query": query}) if query else None
    return None


def _cancel_reminder(lower: str) -> RouteResult:
    """query is required ('all' cancels every pending reminder, per the
    skill's own description)."""
    if lower in ("cancel all reminders", "cancel all my reminders", "cancel every reminder"):
        return ("cancel_reminder", {"query": "all"})
    for prefix in ("cancel my reminder about ", "cancel the reminder about ", "cancel reminder about ",
                   "cancel my reminder for ", "cancel the reminder for ", "cancel reminder for "):
        if lower.startswith(prefix):
            query = lower[len(prefix):].strip()
            return ("cancel_reminder", {"query": query}) if query else None
    for prefix in ("cancel the ", "cancel my "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):]
            for suffix in (" reminder", " alarm"):
                if rest.endswith(suffix):
                    query = rest[:-len(suffix)].strip()
                    return ("cancel_reminder", {"query": query}) if query else None
    return None


def _cancel_calendar_event(lower: str) -> RouteResult:
    """query is required ('all' cancels every event, mirroring
    cancel_reminder above). Uses "event"/"appointment"/"meeting" as its
    nouns rather than "reminder"/"alarm", so the two rules never share a
    trigger string and their relative order doesn't matter."""
    if lower in ("cancel all events", "cancel all my events", "cancel every event",
                 "cancel all appointments", "cancel all my appointments"):
        return ("cancel_calendar_event", {"query": "all"})
    for prefix in ("cancel my event about ", "cancel the event about ", "cancel event about ",
                   "cancel my appointment about ", "cancel the appointment about ", "cancel appointment about ",
                   "cancel my event for ", "cancel the event for ", "cancel event for ",
                   "cancel my appointment for ", "cancel the appointment for ", "cancel appointment for "):
        if lower.startswith(prefix):
            query = lower[len(prefix):].strip()
            return ("cancel_calendar_event", {"query": query}) if query else None
    for prefix in ("cancel the ", "cancel my "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):]
            for suffix in (" appointment", " event", " meeting"):
                if rest.endswith(suffix):
                    query = rest[:-len(suffix)].strip()
                    return ("cancel_calendar_event", {"query": query}) if query else None
    return None


def _reset_streak(lower: str) -> RouteResult:
    """habit is required. Placed before forget_memory's own _Rule (see
    the insertion order below) - "forget my " is also one of
    forget_memory's prefixes, so "forget my workout streak" must be
    claimed here first or forget_memory would wrongly take the whole
    "workout streak" as a memory key instead."""
    for prefix in ("reset my ", "clear my ", "forget my ", "reset the ", "clear the "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):]
            for suffix in (" streak", " habit streak"):
                if rest.endswith(suffix):
                    habit = rest[:-len(suffix)].strip()
                    return ("reset_streak", {"habit": habit}) if habit else None
    return None


def _summarize_page(lower: str) -> RouteResult:
    """url is required. The startswith-http(s) check is deliberate rather
    than incidental - it's what keeps this rule from ever competing with
    summarize_my_notes/quiz_me_on below (neither "summarize my notes on
    X" nor any plain-English topic starts with a URL scheme), so rule
    order between the three doesn't matter here."""
    for prefix in ("summarize ", "summarise ", "tldr "):
        if lower.startswith(prefix):
            rest = lower[len(prefix):].strip()
            if rest.startswith("http://") or rest.startswith("https://"):
                return ("summarize_page", {"url": rest})
    return None


_PCT_RE = re.compile(r"\b(\d{1,3})\s*(?:%|percent)?\b")


def _set_percent_cmd(lower: str, skill: str, trigger_words: Tuple[str, ...]) -> RouteResult:
    """Shared by set_volume/set_brightness below. Requires a trigger word
    AND a 0-100 number AND a short-ish message (<=8 words) - the number
    requirement alone rules out nearly every unrelated sentence that
    happens to mention "volume"/"brightness", and the length cap (same
    reasoning as _MAX_PREFIX_ARG_WORDS above) catches the rest. Neither
    skill is confirm-gated or destructive, so an occasional wrong guess
    here is a one-word correction, not a safety issue."""
    if not any(w in lower for w in trigger_words):
        return None
    if len(lower.split()) > 8:
        return None
    m = _PCT_RE.search(lower)
    if not m:
        return None
    # Tier 50 (found by bench/heldout/): "volume 2 of the book" set the
    # volume to 2%. Nothing but "percent"/"kar do"-style words may follow
    # the number.
    if any(w not in _PCT_TAIL_OK for w in re.findall(r"[^\s]+", lower[m.end():])):
        return None
    pct = int(m.group(1))
    return (skill, {"percent": pct}) if 0 <= pct <= 100 else None


_PCT_TAIL_OK = frozenset("% percent pe par kar karo kardo krdo do please pls rakho set kijiye".split())


def _set_volume_percent(lower: str) -> RouteResult:
    return _set_percent_cmd(lower, "set_volume", ("volume", "awaaz", "आवाज़", "वॉल्यूम"))


def _set_brightness_percent(lower: str) -> RouteResult:
    return _set_percent_cmd(lower, "set_brightness", ("brightness", "roshni", "चमक", "ब्राइटनेस"))


# ---------------------------------------------------------------------------
# The rule table - order matters: more specific phrase/condition checks
# come before broader prefix-based ones that could otherwise shadow them
# (e.g. "close window" before the generic "close <app>" prefix, and
# "play pause" before the generic "play <query>" prefix).
# ---------------------------------------------------------------------------
_RULES: List[_Rule] = [
    _Rule("undo", _exact(
        "undo_last_action",
        "undo", "undo that", "undo it", "undo last", "undo last action", "undo the last action",
        "undo my last action", "undo kar do", "undo karo", "wapas kar do", "revert that", "take that back",
        "यह पूर्ववत करो", "पूर्ववत करो",  # Tier 8: Devanagari - "undo this" / "undo"
    )),
    # Tier 14: the "fix it" half of power/session - same "a confirm-gated
    # skill found by the router still asks first" safety argument as the
    # module docstring makes for every T2/T3 match here, so fast-pathing a
    # T3 shutdown_pc/restart_pc below is no less safe than fast-pathing a
    # T1 lock_pc already was; cancel_shutdown itself is T1 (see
    # brain/tiers.py) and reads naturally right alongside undo above.
    _Rule("cancel_shutdown", _contains(
        "cancel_shutdown",
        "cancel shutdown", "cancel the shutdown", "cancel restart", "cancel the restart",
        "stop the shutdown", "stop the restart", "shutdown cancel karo", "shutdown cancel kar do",
        "शटडाउन कैंसिल करो", "शटडाउन रद्द करो",  # Devanagari - "cancel shutdown"
    )),
    _Rule("close_window", _contains(
        "close_current_window",
        "close window", "close current window", "close the current window", "close this window",
        "close the window", "close all windows",
    )),
    _Rule("close_tab", _contains(
        "close_current_tab",
        "close tab", "close current tab", "close the current tab", "close this tab", "close the tab",
    )),
    _Rule("delete_screenshot", _delete_screenshot),
    _Rule("take_screenshot", _contains(
        "take_screenshot",
        "take a screenshot", "take screenshot", "capture screen", "screenshot",
        "screenshot le lo", "screenshot lo",
        "स्क्रीनशॉट लो", "स्क्रीनशॉट ले लो",  # Tier 8: Devanagari
    )),
    _Rule("read_screen", _contains(
        "read_screen_text",
        "read the screen", "read screen", "screen text", "what does the screen say",
        "read text on screen", "what's on my screen", "whats on my screen",
    )),
    _Rule("pc_info", _contains(
        "get_pc_info",
        "pc info", "system info", "about my pc", "system stats", "hardware info",
        "how's my pc", "hows my pc", "check my system",
    )),
    _Rule("clipboard", _contains(
        "get_clipboard",
        "what's in my clipboard", "what is in my clipboard", "read my clipboard",
        "read clipboard", "what did i copy",
    )),
    _Rule("lock_pc", _contains(
        "lock_pc",
        "lock the pc", "lock my pc", "lock the computer", "lock my computer",
        "lock windows", "lock the screen", "pc lock kar do", "lock kar do", "screen lock kar do",
        "कंप्यूटर लॉक करो", "पीसी लॉक करो", "स्क्रीन लॉक करो",  # Tier 8: Devanagari
    )),
    # Tier 14: power/session. Each is T3/confirm-gated (brain/tiers.py) -
    # matching here only skips the LLM round-trip, never the confirmation
    # itself (see this module's own docstring). Phrasing deliberately
    # avoids "band karo"/"band kar do"/"बंद करो" and " kholo"/"खोलो" -
    # close_app's and open_app's own suffixes a few rules down - so e.g.
    # "pc shutdown karo" can't be mistaken for either.
    _Rule("shutdown_pc", _contains(
        "shutdown_pc",
        "shut down the pc", "shut down my pc", "shutdown the pc", "shutdown my pc",
        "shut down the computer", "shut down my computer", "turn off the pc", "turn off my computer",
        "pc shutdown karo", "pc shutdown kar do", "shutdown karo", "shutdown kar do",
        "पीसी शटडाउन करो", "कंप्यूटर शटडाउन करो",  # Devanagari
    )),
    _Rule("restart_pc", _contains(
        "restart_pc",
        "restart the pc", "restart my pc", "restart the computer", "restart my computer",
        "reboot the pc", "reboot my pc", "reboot the computer", "pc restart karo", "restart karo",
        "पीसी रीस्टार्ट करो", "कंप्यूटर रीस्टार्ट करो",  # Devanagari
    )),
    _Rule("sleep_pc", _contains(
        "sleep_pc",
        "put the pc to sleep", "put my pc to sleep", "put the computer to sleep", "sleep the pc",
        "pc ko sleep karo", "pc sleep karo",
        "पीसी को सुलाओ", "पीसी स्लीप करो",  # Devanagari
    )),
    _Rule("hibernate_pc", _contains(
        "hibernate_pc",
        "hibernate the pc", "hibernate my pc", "hibernate the computer", "pc hibernate karo",
        "पीसी हाइबरनेट करो",  # Devanagari
    )),
    _Rule("log_off", _contains(
        "log_off",
        "log off the pc", "log off my pc", "log off windows", "log me off",
        "sign out of windows", "sign out of the pc", "pc se log off karo",
        "लॉग ऑफ करो",  # Devanagari
    )),
    # Tier 14: the other half of per-window management - show_desktop/
    # minimize_all_windows/switch_window are the parameter-free ones (no
    # window name to extract), so they fit this module's curated-rule bar
    # the same way close_window/close_tab already do; move/resize/
    # maximize/restore/always-on-top/fullscreen/restart_app all take a
    # window or app name and are deliberately left to the LLM, same as
    # Tier 8's own focus_window/minimize_window/snap_window never got a
    # fast-path rule either.
    _Rule("show_desktop", _contains(
        "show_desktop",
        "show the desktop", "show desktop", "minimize all windows", "minimize everything",
        "sab window minimize karo", "desktop dikhao",
        # Tier 50 (bench/heldout/): these reached minimize_window("all the windows")
        "minimize all the windows", "minimize all my windows", "sab windows minimize",
        "sari windows minimize", "saari windows minimize",
        "डेस्कटॉप दिखाओ",  # Devanagari
    )),
    _Rule("switch_window", _contains(
        "switch_window",
        "switch window", "switch windows", "next window", "alt tab", "window badlo",
        "विंडो बदलो",  # Devanagari
    )),
    # Tier 15: keyboard. copy/paste/select-all are parameter-free (a fixed
    # chord, nothing to extract), so they clear this module's curated-rule
    # bar the same way close_window/close_tab do. undo_keystroke/
    # redo_keystroke deliberately use DIFFERENT trigger words than the
    # "undo" rule at the very top of this list: that one is already
    # "undo"/"undo that"/"undo it"/"undo last" for undo_last_action (Wave
    # reversing its OWN last action) - a bare "undo" here would be
    # genuinely ambiguous between the two skills, not just a different
    # phrasing of the same one, so undo_keystroke only matches phrases
    # ("press undo"/"send undo"/"ctrl z") that one never claims. redo has
    # no such competing skill, so redo_keystroke's set is more generous.
    _Rule("copy_text", _contains(
        "copy_text",
        "copy that", "copy this", "copy it", "copy selected text", "copy selection",
        "ise copy karo", "copy kar do",
        "इसे कॉपी करो",  # Devanagari - "copy this"
    )),
    _Rule("paste_text", _contains(
        "paste_text",
        "paste that", "paste this", "paste it", "paste here", "paste kar do", "ise paste karo",
        "इसे पेस्ट करो",  # Devanagari - "paste this"
    )),
    _Rule("select_all_text", _contains(
        "select_all_text",
        "select all", "select everything", "sab select karo", "select all text",
        "सब सेलेक्ट करो",  # Devanagari - "select all"
    )),
    _Rule("undo_keystroke", _contains(
        "undo_keystroke",
        "press undo", "send undo", "ctrl z", "ctrl+z",
    )),
    _Rule("redo_keystroke", _contains(
        "redo_keystroke",
        "redo", "redo that", "redo it", "press redo", "send redo", "ctrl y", "ctrl+y",
    )),
    _Rule("press_key", _press_fixed_key),
    # Tier 15: browser control - eleven parameter-free hotkey tools, same
    # "a fixed shortcut, nothing to extract" bar as the keyboard group
    # just above. These three specifically use _contains_words, not plain
    # _contains: every phrase here ends in a bare "tab", and "tab" is a
    # real-word PREFIX of "tablet" - a plain substring check would wrongly
    # fire new_tab on the entirely unrelated, very ordinary "I got a new
    # tablet" ("new tab" + "let"), and likewise next/previous "tablet".
    # Found and fixed while writing this tier's own bench golden rows -
    # see bench/golden/negatives.jsonl.
    _Rule("new_tab", _contains_words(
        "new_tab",
        "new tab", "open a new tab", "open new tab", "naya tab kholo",
        "नया टैब खोलो",  # Devanagari
    )),
    _Rule("next_tab", _contains_words(
        "next_tab",
        "next tab", "go to the next tab", "agla tab",
        "अगला टैब",  # Devanagari
    )),
    _Rule("previous_tab", _contains_words(
        "previous_tab",
        "previous tab", "go to the previous tab", "last tab", "pichla tab",
        "पिछला टैब",  # Devanagari
    )),
    _Rule("reload_page", _contains(
        "reload_page",
        "reload the page", "reload page", "refresh the page", "refresh page",
        "page reload karo", "page refresh karo",
        "पेज रीलोड करो",  # Devanagari
    )),
    _Rule("zoom_in", _contains("zoom_in", "zoom in", "zoom in karo", "ज़ूम इन करो")),
    _Rule("zoom_out", _contains("zoom_out", "zoom out", "zoom out karo", "ज़ूम आउट करो")),
    _Rule("zoom_reset", _contains(
        "zoom_reset",
        "reset zoom", "reset the zoom", "zoom reset karo",
        "ज़ूम रीसेट करो",  # Devanagari
    )),
    # scroll_to_top/scroll_to_bottom are checked BEFORE the plainer
    # scroll_up/scroll_down just below, not after - "sabse upar scroll
    # karo" ("scroll ALL THE WAY up") contains "upar scroll karo" ("scroll
    # up") as a substring, so first-match-wins (see route()) means the
    # more specific "all the way" phrasing has to be tried first or it
    # can never win. English has no such overlap, but the ordering is
    # cheap insurance either way. Found via bench's own hinglish golden
    # rows failing - see bench/golden/intents_hinglish.jsonl.
    _Rule("scroll_to_top", _contains(
        "scroll_to_top",
        "scroll to the top", "scroll to top", "go to the top of the page",
        "sabse upar scroll karo",
    )),
    _Rule("scroll_to_bottom", _contains(
        "scroll_to_bottom",
        "scroll to the bottom", "scroll to bottom", "go to the bottom of the page",
        "sabse neeche scroll karo",
    )),
    _Rule("scroll_up", _contains(
        "scroll_up",
        "scroll up", "scroll the page up", "upar scroll karo",
        "ऊपर स्क्रॉल करो",  # Devanagari
    )),
    _Rule("scroll_down", _contains(
        "scroll_down",
        "scroll down", "scroll the page down", "neeche scroll karo",
        "नीचे स्क्रॉल करो",  # Devanagari
    )),
    # Tier 15: folder shortcuts - parameter-free, same bar as
    # open_display_settings/open_sound_settings/... below. Checked before
    # open_app's generic prefix rule further down this list (first match
    # wins - see route()), so "open downloads"/"open recycle bin" land
    # here rather than open_app trying to launch an app by that name.
    _Rule("open_downloads", _contains(
        "open_downloads",
        "open downloads", "open my downloads", "open the downloads folder", "downloads kholo",
        "डाउनलोड्स खोलो",  # Devanagari
    )),
    _Rule("open_documents", _contains(
        "open_documents",
        "open documents", "open my documents", "open the documents folder", "documents kholo",
        "डॉक्यूमेंट्स खोलो",  # Devanagari
    )),
    _Rule("open_pictures", _contains(
        "open_pictures",
        "open pictures", "open my pictures", "open the pictures folder", "pictures kholo",
    )),
    _Rule("open_music", _contains(
        "open_music",
        "open music", "open my music", "open the music folder", "music folder kholo",
    )),
    _Rule("open_videos", _contains(
        "open_videos",
        "open videos", "open my videos", "open the videos folder", "videos kholo",
    )),
    _Rule("open_this_pc", _contains(
        "open_this_pc",
        "open this pc", "open my computer", "open computer",
    )),
    _Rule("open_recycle_bin", _contains(
        "open_recycle_bin",
        "open recycle bin", "open the recycle bin", "open trash", "open the trash",
        "recycle bin kholo",
        "रीसायकल बिन खोलो",  # Devanagari
    )),
    _Rule("open_task_manager", _contains(
        "open_task_manager",
        "open task manager", "open the task manager", "task manager kholo",
        "टास्क मैनेजर खोलो",  # Devanagari
    )),
    _Rule("open_control_panel", _contains(
        "open_control_panel",
        "open control panel", "open the control panel", "control panel kholo",
        "कंट्रोल पैनल खोलो",  # Devanagari
    )),
    # Tier 15: destructive but confirm-gated (T3, brain/tiers.py) - fast-
    # pathing only skips the LLM round-trip, never the confirmation, same
    # safety argument the module docstring makes for shutdown_pc/restart_pc
    # above, so this is no less safe to match here than those.
    _Rule("empty_recycle_bin", _contains(
        "empty_recycle_bin",
        "empty the recycle bin", "empty recycle bin", "empty the trash", "empty trash",
        "recycle bin khali karo", "trash khali karo",
        "रीसायकल बिन खाली करो",  # Devanagari
    )),
    # Tier 15: download watch - both parameter-free in their common phrasing
    # (watch_for_download's folder/timeout args are optional, so routing
    # with no args is a perfectly valid call, same as every other no-arg
    # rule here).
    _Rule("watch_for_download", _contains(
        "watch_for_download",
        "watch for a download", "watch for my download", "watch for downloads",
        "let me know when the download finishes", "let me know when my download finishes",
        "tell me when the download finishes", "tell me when my download finishes",
        "tell me when it's done downloading", "notify me when the download finishes",
    )),
    _Rule("cancel_download_watch", _contains(
        "cancel_download_watch",
        "stop watching for the download", "stop watching for downloads",
        "cancel the download watch", "cancel download watch",
    )),
    _Rule("media_play_pause", _exact_or_contains(
        "media_play_pause",
        exact_phrases=("pause", "resume", "unpause", "play pause", "pause play"),
        contains_phrases=(
            "pause music", "pause the music", "pause song", "stop the music",
            "resume music", "resume the music", "unpause music", "resume song",
        ),
    )),
    _Rule("media_next", _contains(
        "media_next_track", "next song", "next track", "skip song", "skip track", "skip this song",
    )),
    _Rule("media_previous", _contains(
        "media_previous_track", "previous song", "previous track", "last song",
        "go back a song", "play the last track",
        "pichla gaana", "pichla track",  # Tier 8: romanized - had zero phrase in any script before
        "पिछला गाना", "पिछला ट्रैक",  # Tier 8: Devanagari
    )),
    _Rule("volume_up", _contains(
        "volume_up", "volume up", "increase volume", "turn up the volume", "louder",
        "awaaz badhao", "volume badhao", "awaaz tez karo",
        "आवाज़ बढ़ाओ", "वॉल्यूम बढ़ाओ", "आवाज़ तेज़ करो",  # Tier 8: Devanagari
    )),
    _Rule("volume_down", _contains(
        "volume_down", "volume down", "decrease volume", "turn down the volume", "quieter", "lower the volume",
        "awaaz kam karo", "volume kam karo", "awaaz dheere karo",
        "आवाज़ कम करो", "वॉल्यूम कम करो", "आवाज़ धीरे करो",  # Tier 8: Devanagari
    )),
    # Tier 14: toggle_mute is RETIRED (see brain/handlers/media.py's own
    # module comment on this) - split into two explicit, one-directional
    # rules so "unmute" can never again land on mute_volume's skill by
    # mistake. "unmute"/"अनम्यूट करो" are checked FIRST and as a
    # SEPARATE rule, not folded into mute_volume's _contains list, since
    # "unmute" contains "mute" as a literal substring - were both
    # phrases on one _contains(mute_volume, ...) list, "unmute" would
    # match it too (startswith-unrelated substring checks don't know
    # about word boundaries), exactly reintroducing the bug this split
    # exists to fix.
    _Rule("unmute_volume", _exact_or_contains(
        "unmute_volume", exact_phrases=("unmute",),
        contains_phrases=(
            "unmute the volume", "unmute the sound", "unmute audio", "unmute kar do",
            "awaaz on karo", "आवाज़ चालू करो", "अनम्यूट करो",  # Devanagari
        ),
    )),
    _Rule("mute_volume", _exact_or_contains(
        "mute_volume", exact_phrases=("mute",),
        contains_phrases=(
            "mute the volume", "mute the sound", "mute audio", "mute kar do", "awaaz band karo",
            "म्यूट करो", "आवाज़ बंद करो",  # Devanagari
        ),
    )),
    _Rule("toggle_hardware", _toggle_hardware),
    _Rule("theme_mode", _theme_mode),          # Tier 44
    # Tier 11: before open_app, which would otherwise take "open display
    # settings" as an app called "display settings".
    _Rule("open_settings", _open_settings),
    _Rule("write_notepad", _write_notepad),
    # Tier 8: both now also match the verb-FINAL Hindi/Hinglish shape
    # ("chrome kholo"/"क्रोम खोलो", "notepad band karo"/"नोटपैड बंद करो")
    # via _suffix_arg, alongside the original verb-first English shape -
    # see _prefix_or_suffix_arg's docstring.
    # Tier 17: before open_app, which also claims "turn on "/"start " as
    # two of its own prefixes - see _enable_mode/_start_service's own
    # docstrings above for exactly why each needs to run first.
    # start_stopwatch is a plain zero-arg _contains rule, not a custom
    # matcher, but it needs the same early placement for the same
    # reason: "start the stopwatch" is well under open_app's own
    # 5-word/idiom-start cap, so without this it would be wrongly routed
    # to open_app as a request to open an app named "the stopwatch"
    # (caught by this tier's self-verifying golden-row generator before
    # it ever shipped - see scratchpad notes referenced in NEXT_STEPS.md).
    _Rule("start_stopwatch", _contains("start_stopwatch", "start the stopwatch", "start stopwatch",
                                        "stopwatch shuru karo")),
    _Rule("enable_mode", _enable_mode),
    _Rule("start_service", _start_service),
    _Rule("open_app", _prefix_or_suffix_arg(
        "open_app", "app_name",
        prefixes=("open ", "start ", "launch ", "turn on "),
        suffixes=(" khol do", " kholo", " खोलो"),
    )),
    # Tier 11: + "kill <app>" (the UI's removed keyword layer handled it).
    # close_app is T2, so this still asks before closing anything. "quit"
    # is deliberately not added: "quit smoking tips" is not a command.
    _Rule("close_app", _not_sound(_prefix_or_suffix_arg(
        "close_app", "app_name",
        prefixes=("close ", "kill "),
        suffixes=(" band karo", " band kar do", " बंद करो"),
    ))),
    # Tier 17: before play_youtube, whose own bare "play " prefix has no
    # suffix requirement at all - see _play_on_spotify's own docstring
    # above for exactly why this needs to run first.
    _Rule("play_on_spotify", _play_on_spotify),
    _Rule("play_youtube", _prefix_arg("play_youtube", "query", "play ")),
    # Tier 17: both before _search_or_play, whose own "search "/"search
    # for " prefixes have no suffix/topic requirement either - without
    # this order, "search my notes for X"/"search for file X" would
    # already fully match _search_or_play's generic prefix and go to
    # search_google instead.
    _Rule("search_file", _prefix_arg(
        "search_file", "file_name",
        "find file ", "find the file ", "search for file ", "search for a file called ", "search file ",
    )),
    _Rule("search_my_notes", _prefix_arg(
        "search_my_notes", "question",
        "search my notes for ", "search my notes about ", "search my notes ",
        "ask my notes about ", "ask my notes ",
    )),
    _Rule("search_or_play", _search_or_play),
    _Rule("create_folder", _create_folder),
    # Tier 16: "deterministic time and date answers" - the one intent_router
    # addition this tier's own roadmap bullet calls for by name (modes and
    # the rest of this tier's new tools get their fast-path phrasings in
    # Tier 17's sweep instead - see brain/handlers/productivity.py's own
    # module docstring on why these two specifically are worth it now).
    # get_dynamic_system_prompt() (brain/groq_core.py) already puts the
    # current time/date into every turn's system prompt, so an LLM round-
    # trip would still answer correctly - matching here is purely a
    # latency win, never a correctness one, which is exactly this file's
    # "harmless if wrong" bar for inclusion, with room to spare.
    _Rule("get_current_time", _contains(
        "get_current_time",
        "what time is it", "what's the time", "whats the time", "tell me the time", "current time",
        "do you know the time", "time kya hai", "kitne baje hain", "kitna baja hai",
        "abhi kitne baje hain", "abhi kitna baja hai",
        "क्या समय हुआ है", "अभी कितने बजे हैं", "टाइम क्या है",  # Devanagari
    )),
    _Rule("get_current_date", _contains(
        "get_current_date",
        "what's the date", "whats the date", "what is the date", "what's today's date",
        "what day is it today", "what's the date today", "today's date", "current date",
        "aaj ki tarikh kya hai", "aaj ki tareekh kya hai", "aaj kaunsa din hai", "aaj kya date hai",
        "आज की तारीख क्या है", "आज कौन सा दिन है",  # Devanagari
    )),

    # -----------------------------------------------------------------
    # Tier 17: the ~67 remaining new rules (72 total this tier, minus the
    # 5 placed earlier in the table above for collision reasons - see
    # each custom matcher's own docstring). Zero-arg bare phrases first,
    # then optional-arg-only bare phrases (_exact, never _contains - see
    # each one's own comment on why), then the required-single-arg
    # prefix/suffix rules, then the two numeric ones.
    # -----------------------------------------------------------------
    _Rule("brightness_up", _contains("brightness_up", "brightness up", "increase brightness",
                                      "roshni badhao", "ब्राइटनेस बढ़ाओ")),
    _Rule("brightness_down", _contains("brightness_down", "brightness down", "decrease brightness",
                                        "roshni kam karo", "ब्राइटनेस कम करो")),
    _Rule("brightness_max", _contains("brightness_max", "brightness max", "full brightness",
                                       "maximum brightness", "roshni full karo")),
    _Rule("brightness_min", _contains("brightness_min", "brightness min", "minimum brightness",
                                       "lowest brightness", "roshni kam se kam")),
    _Rule("volume_max", _contains("volume_max", "volume max", "full volume", "maximum volume",
                                   "awaaz full karo", "आवाज़ पूरी करो")),
    _Rule("volume_min", _contains("volume_min", "volume min", "minimum volume", "lowest volume",
                                   "awaaz kam se kam")),
    _Rule("get_battery_status", _contains(
        "get_battery_status", "battery status", "battery percentage", "how much battery",
        "what's my battery", "whats my battery", "battery kitni hai", "battery kitna hai",
        "बैटरी कितनी है",  # Devanagari
    )),
    _Rule("get_brightness_status", _contains(
        "get_brightness_status", "brightness status", "what's my brightness", "current brightness",
        "brightness kitni hai",
    )),
    _Rule("get_cpu_temperature", _contains(
        "get_cpu_temperature", "cpu temperature", "cpu temp", "how hot is my cpu", "processor temperature",
    )),
    _Rule("get_disk_usage", _contains(
        "get_disk_usage", "disk usage", "disk space", "how much disk space", "storage usage",
        "hard drive space",
    )),
    _Rule("get_gpu_status", _contains(
        "get_gpu_status", "gpu status", "gpu temperature", "gpu usage", "graphics card status",
    )),
    _Rule("get_local_ip", _contains(
        "get_local_ip", "local ip", "my ip address", "what's my ip", "whats my ip", "network ip address",
    )),
    _Rule("get_uptime", _contains(
        "get_uptime", "system uptime", "how long has my pc been on", "how long has this been running",
        "pc kitne der se chal raha hai",
    )),
    _Rule("list_services", _contains(
        "list_services", "list services", "what services are running", "show me the services",
        "background services",
    )),
    _Rule("list_open_windows", _contains(
        "list_open_windows", "what windows are open", "list open windows", "what do i have open",
        "what do i have open right now", "show me my open windows",
    )),
    _Rule("list_my_skills", _contains(
        "list_my_skills", "list my skills", "what custom skills do i have", "my custom skills",
        "what skills are loaded",
    )),
    _Rule("run_self_diagnostics", _contains(
        "run_self_diagnostics", "run a self check", "run self diagnostics", "diagnose yourself",
        "is everything working", "why isn't voice working", "why isnt voice working",
    )),
    _Rule("daily_briefing", _contains(
        "daily_briefing", "give me my briefing", "daily briefing", "what's my day look like",
        "whats my day look like", "good morning wave", "morning briefing",
    )),
    _Rule("get_joke", _contains(
        "get_joke", "tell me a joke", "make me laugh", "say something funny", "tell a joke",
        "koi joke sunao", "ek joke batao",
    )),
    _Rule("take_photo", _contains("take_photo", "take a photo", "take a picture", "click a photo",
                                   "capture a photo", "webcam photo")),
    _Rule("list_reminders", _contains(
        "list_reminders", "list my reminders", "what reminders do i have", "show me my reminders",
        "mere reminders dikhao",
    )),
    _Rule("list_timers", _contains(
        "list_timers", "list my timers", "what timers do i have", "show me my timers", "active timers",
    )),
    _Rule("list_workflows", _contains(
        "list_workflows", "list my workflows", "what workflows do i have", "show me my workflows",
        "list workflows",
    )),
    _Rule("lap_stopwatch", _contains("lap_stopwatch", "lap the stopwatch", "stopwatch lap", "record a lap")),
    _Rule("stop_stopwatch", _contains("stop_stopwatch", "stop the stopwatch", "stop stopwatch",
                                       "stopwatch band karo")),
    _Rule("stopwatch_status", _contains("stopwatch_status", "stopwatch status", "how long has the stopwatch been running",
                                         "check the stopwatch")),
    _Rule("read_notes", _contains(
        "read_notes", "what are my notes", "read my notes back to me", "do i have any notes",
        "read my notes", "read back my notes",
    )),
    _Rule("clear_notes", _contains(
        "clear_notes", "clear all my notes", "delete all my notes", "erase all my notes", "clear my notes",
    )),
    _Rule("reindex_notes", _contains(
        "reindex_notes", "reindex my notes", "refresh my notes", "rescan my notes", "reindex notes",
    )),
    # disable_mode takes no args - it turns off whichever mode is
    # currently on, so there's no "which mode" to extract (unlike
    # enable_mode above, which does need one). Trigger words are "off"/
    # "disable"/"stop", never "on"/"enable" - no overlap with enable_mode.
    _Rule("disable_mode", _contains(
        "disable_mode", "turn off dnd", "turn off focus mode", "turn off study mode",
        "turn off gaming mode", "turn off sleep mode", "disable dnd", "disable focus mode",
        "disable study mode", "disable gaming mode", "disable sleep mode", "mode off",
        "stop focus mode", "stop study mode", "stop dnd mode", "stop gaming mode", "stop sleep mode",
        "turn off the mode",
    )),
    _Rule("get_active_mode", _contains(
        "get_active_mode", "what mode am i in", "is focus mode on", "am i in dnd", "which mode is on",
        "what mode is active",
    )),

    # Optional-arg-only tools - _exact (whole message), never _contains,
    # so a more specific request ("what's the weather in mumbai") isn't
    # wrongly matched here and silently stripped of its argument. See
    # this tier's own notes in docs/ROADMAP_BEAT_SARA.md for the full
    # reasoning (same hazard class for all nine of these).
    _Rule("get_weather", _exact(
        "get_weather", "what's the weather", "whats the weather", "what is the weather",
        "how's the weather", "hows the weather", "weather kaisa hai", "मौसम कैसा है",
    )),
    _Rule("get_news", _exact(
        "get_news", "what's the news", "whats the news", "tell me the news", "give me the news",
        "latest news", "news sunao",
    )),
    _Rule("list_calendar_events", _exact(
        "list_calendar_events", "what's on my calendar", "whats on my calendar", "show me my calendar",
        "list my calendar events", "what do i have on my calendar",
    )),
    _Rule("list_recent_actions", _exact(
        "list_recent_actions", "what have you done recently", "list recent actions",
        "show me recent actions", "what did you just do",
    )),
    _Rule("list_todos", _exact(
        "list_todos", "what's on my to-do list", "whats on my to-do list", "show me my to-do list",
        "list my todos", "what's on my todo list", "whats on my todo list", "mera to-do list dikhao",
    )),
    _Rule("get_top_processes", _exact(
        "get_top_processes", "what's using my cpu", "whats using my cpu", "top processes",
        "what's using the most resources", "whats using the most resources",
    )),
    _Rule("get_streak_status", _exact(
        "get_streak_status", "what's my streak", "whats my streak", "how's my streak", "hows my streak",
        "check my streak",
    )),
    _Rule("cancel_timer", _exact(
        "cancel_timer", "cancel the timer", "cancel timer", "stop the timer", "cancel my timer",
    )),
    _Rule("check_timer", _exact(
        "check_timer", "check the timer", "check timer", "how much time is left", "timer status",
    )),

    # Required-single-arg tools - prefix/suffix extraction.
    _Rule("quiz_me_on", _prefix_arg("quiz_me_on", "topic", "quiz me on ")),
    _Rule("summarize_my_notes", _prefix_arg(
        "summarize_my_notes", "topic", "summarize my notes on ", "summarize my notes about ",
    )),
    _Rule("restart_app", _prefix_or_suffix_arg(
        "restart_app", "app_name",
        prefixes=("restart ", "relaunch "),
        suffixes=(" restart karo", " relaunch karo"),
    )),
    _Rule("stop_service", _stop_service),
    # Tier 17: reset_streak before forget_memory - "forget my " is also
    # one of forget_memory's own prefixes (see _reset_streak's docstring
    # above for why).
    _Rule("reset_streak", _reset_streak),
    _Rule("forget_memory", _prefix_arg("forget_memory", "key", "forget my ", "forget the ", "forget ")),
    _Rule("recall_memory", _prefix_arg(
        "recall_memory", "key", "recall my ", "recall the ", "recall ", "do you remember my ",
    )),
    # Tier 17: delete_todo before delete_file - delete_file's bare
    # "delete "/"remove " prefix has no suffix requirement, so a short
    # todo-removal phrase would otherwise be wrongly claimed as a file
    # deletion (see _delete_todo's docstring above).
    _Rule("delete_todo", _delete_todo),
    _Rule("delete_file", _prefix_arg("delete_file", "file_name", "delete ", "remove ")),
    _Rule("create_file", _prefix_arg(
        "create_file", "file_name",
        "create a file called ", "create a file named ", "create a file ", "create file ",
    )),
    _Rule("run_workflow", _prefix_arg("run_workflow", "name", "run workflow ", "run the workflow ")),
    _Rule("complete_todo", _complete_todo),
    _Rule("cancel_reminder", _cancel_reminder),
    _Rule("cancel_calendar_event", _cancel_calendar_event),
    # take_note's content is reproduced word for word (see the skill's
    # own description) - case-preserving, like write_notepad's text arg.
    _Rule("take_note", _prefix_or_suffix_arg(
        "take_note", "content",
        prefixes=("take a note: ", "take a note that ", "take a note ", "note down that ", "note down ",
                   "make a note that ", "make a note "),
        suffixes=(" likho", " likh lo"),
    )),
    # type_text's rule is appended after write_notepad's (positioned much
    # earlier in this table) on purpose - "type <text> in notepad" must
    # still match write_notepad first; only a bare "type <text>" with no
    # notepad suffix reaches this rule. Case-preserving, same reason as
    # write_notepad's own text arg.
    _Rule("type_text", _prefix_arg("type_text", "text", "type ")),
    _Rule("summarize_page", _summarize_page),
    _Rule("focus_window", _prefix_or_suffix_arg(
        "focus_window", "window_name",
        prefixes=("focus on ", "switch to the ", "switch to "),
        suffixes=(" par jao", " pe jao"),
    )),
    _Rule("maximize_window", _prefix_or_suffix_arg(
        "maximize_window", "window_name",
        prefixes=("maximize ", "maximize the "),
        suffixes=(" maximize karo", " ko maximize karo"),
    )),
    _Rule("minimize_window", _prefix_or_suffix_arg(
        "minimize_window", "window_name",
        prefixes=("minimize ", "minimize the "),
        suffixes=(" minimize karo", " ko minimize karo"),
    )),
    _Rule("restore_window", _prefix_or_suffix_arg(
        "restore_window", "window_name",
        prefixes=("restore ", "restore the "),
        suffixes=(" restore karo", " ko restore karo"),
    )),
    _Rule("set_fullscreen", _prefix_or_suffix_arg(
        "set_fullscreen", "window_name",
        prefixes=("fullscreen ",),
        suffixes=(" fullscreen",),
    )),
    _Rule("set_clipboard", _prefix_arg("set_clipboard", "text", "set the clipboard to ", "set clipboard to ")),
    _Rule("calculate", _prefix_arg("calculate", "expression", "calculate ", "compute ", "evaluate ")),

    # Numeric: a device word (English/Hinglish/Devanagari) + a 0-100
    # number + a short-ish message - see _set_percent_cmd's own docstring
    # above for the full safety reasoning.
    _Rule("set_volume", _set_volume_percent),
    _Rule("set_brightness", _set_brightness_percent),
]


# A message containing one of these almost always carries MORE than one
# instruction ("check my pc info, then remind me to charge it") - exactly
# the case Phase 6's governed multi-round tool loop exists for (see
# brain/groq_core.py's _ToolLoopGovernor). A rule above could still match
# a SUBSTRING of a message like that (e.g. "pc info"), which would
# silently drop every instruction after the first - so this is checked
# before any rule, not folded into each one individually.
_COMPOUND_INDICATORS = (
    " then ", "and then ", " after that", " uske baad", " phir ",
)

# Negating ("don't lock the pc"), conditioning ("lock the pc after I
# leave"), or merely questioning/hypothesizing about ("should I lock the
# pc", "what happens if I lock the pc") a trigger phrase changes what a
# message MEANS without changing which phrase it happens to contain - a
# _contains or _prefix_arg rule above has no way to tell "lock the pc" (a
# command) apart from "don't lock the pc" (its opposite) or "what happens
# if I lock the pc" (a question, not a request) by matching the phrase
# alone, since none of them do anything with what comes before/around the
# match. Checked once here, before any rule, exactly like
# _COMPOUND_INDICATORS above (and for the same reason: no individual rule
# should have to reimplement this). Also not exhaustive - negation
# especially has endless phrasings - but it covers the common, everyday
# ways a real person actually negates, conditions, or questions a
# request. "can you .../could you ..." are deliberately NOT here: that's
# the normal polite phrasing of a direct request ("can you lock the pc"
# means lock it), not a real question, and treating it as one would just
# lose fast-path coverage for how people actually talk without fixing
# anything. Bailing out on any of these can only fall back to the LLM
# handling the message instead - never a worse outcome than before this
# module existed.
_NEGATION_MARKERS = (
    "don't", "dont", "do not", "doesn't", "doesnt", "does not",
    "didn't", "didnt", "did not", "won't", "wont", "will not",
    "shouldn't", "shouldnt", "should not", "can't", "cant", "cannot", "can not",
    "wouldn't", "wouldnt", "would not", "never ", "stop trying to",
    "no need to", "not going to", "mat karo", "mat kar", "nahi karna",
)
_HYPOTHETICAL_MARKERS = (
    "should i ", "should we ", "what if", "what happens if", "what happens when",
    "is it ok to", "is it okay to", "do i need to", "do you need to",
    "why would", "why should", "kya mujhe",
    # 🔧 Tier 15: found while adding empty_recycle_bin's fast-path rule -
    # "pc shutdown karu ya nahi" ("should I shut down the pc or not") was
    # already a pre-existing gap for every tool, not just the new ones
    # (_has_standalone_negator only catches a TRAILING "nahi", so "X ya
    # nahi karu" with more words after "nahi" slipped through). "ya nahi"
    # ("...or not") is a genuine question/uncertainty marker in Hindi/
    # Hinglish wherever it appears in the sentence, same spirit as "kya
    # mujhe" just above - safe to add unconditionally, no legitimate
    # direct command says "ya nahi".
    "ya nahi",
    # Tier 50 (found by bench/heldout/): "how do i increase brightness on a
    # mac" raised this PC's brightness - a how-to question isn't a request.
    "how do i ", "how can i ", "how do you ", "how to ", "how does ", "kaise karte", "kaise kare",
    "kaise karu",
)
# Tier 50: and a story isn't a request - "my sister took a screenshot of our
# chat" took one.
_NARRATIVE_MARKERS = (" took ", " taken ", "yesterday", "last night")
_CONDITIONAL_MARKERS = (
    " after ", " before ", " once ", " unless ", " if i ", " if you ", " if we ", " agar ",
)


def _has_standalone_negator(lower: str) -> bool:
    """🔧 Tier 8: catches trailing post-positive Hindi negation that
    _NEGATION_MARKERS' substring checks miss entirely - e.g. 'awaaz
    badhao mat' really means DON'T raise the volume, but every existing
    marker is a leading/attached form ('mat karo', 'mat kar', glued to
    the verb that follows it), so a plain substring check never fires
    when 'mat' instead trails the whole phrase with nothing after it.
    Checked as a whole SPLIT WORD rather than a substring for exactly the
    reason _PREFIX_ARG_IDIOM_STARTS exists elsewhere in this file: a bare
    substring check for 'mat' would wrongly fire on 'automated', 'format',
    'climate', ... none of which contain it as its own word. Also covers
    the Devanagari particle 'मत' (which carries the same meaning and is
    always written as its own space-delimited word, leading OR trailing -
    'मत करो' and 'करो मत' alike - so one word-level check covers every
    placement in that script without needing separate leading/trailing
    entries the way the romanized _NEGATION_MARKERS tuple does).
    'मत' is occasionally also plain Hindi for 'opinion/vote' rather than
    'don't' (genuinely ambiguous out of context even for a human reader),
    but treating that case as ambiguous too is harmless here - it just
    falls through to the LLM, same as any other false positive this
    module accepts elsewhere (see the module docstring's safety argument)."""
    words = lower.split()
    return "mat" in words or "मत" in words


def _is_compound_or_ambiguous(lower: str) -> bool:
    """True if `lower` is compound (see _COMPOUND_INDICATORS) OR its
    meaning depends on negation/conditioning/questioning that a plain
    phrase match can't see (see the three marker tuples above) - either
    way, route() bails out to the LLM rather than risking any rule below
    firing on a message it was never actually meant to describe."""
    return (any(ind in lower for ind in _COMPOUND_INDICATORS)
            or any(m in lower for m in _NEGATION_MARKERS)
            or any(m in lower for m in _HYPOTHETICAL_MARKERS)
            or any(m in lower for m in _CONDITIONAL_MARKERS)
            or any(m in f" {lower} " for m in _NARRATIVE_MARKERS)
            or _has_standalone_negator(lower))


# Tier 11: arguments that are free text the user wants reproduced as typed
# (rules match on the lowercased message, which is right for app names and
# search queries but would turn "Buy MILK" into "buy milk" in Notepad).
# Tier 17: extended to every new argument that's either typed/stored
# content the user will see again verbatim (take_note/type_text/
# set_clipboard), a filename or URL that can be genuinely case-sensitive
# on disk or on the wire (create_file/delete_file/search_file/
# summarize_page), or an exact-match lookup key/query against something
# stored earlier (recall_memory/forget_memory/run_workflow/
# complete_todo/delete_todo/cancel_reminder/cancel_calendar_event/
# search_my_notes/quiz_me_on/summarize_my_notes/calculate) - anything
# matched against a small fixed vocabulary instead (app/service/window/
# mode names, play_on_spotify's search query) is deliberately left
# lowercased, consistent with open_app/close_app/play_youtube above.
_CASE_PRESERVING_ARGS = {
    ("write_notepad", "text"),
    ("take_note", "content"), ("type_text", "text"), ("set_clipboard", "text"),
    ("create_file", "file_name"), ("delete_file", "file_name"), ("search_file", "file_name"),
    ("summarize_page", "url"),
    ("recall_memory", "key"), ("forget_memory", "key"), ("run_workflow", "name"),
    ("complete_todo", "query"), ("delete_todo", "query"),
    ("cancel_reminder", "query"), ("cancel_calendar_event", "query"),
    ("search_my_notes", "question"), ("quiz_me_on", "topic"), ("summarize_my_notes", "topic"),
    ("calculate", "expression"),
}


def _restore_case(original: str, lower: str, value: str) -> str:
    """`value` is a slice of `lower`; returns the same slice of `original`
    when lowercasing didn't change the string's length (true for ASCII and
    nearly all text), else `value` unchanged."""
    if len(original) != len(lower):
        return value
    idx = lower.find(value)
    return original[idx: idx + len(value)] if idx >= 0 else value


def route(user_text: str) -> Optional[RoutedIntent]:
    """Returns a RoutedIntent if `user_text` unambiguously matches one of
    the curated rules above, else None - meaning: let the normal LLM
    tool-calling loop decide, exactly as before this module existed."""
    if not user_text or not user_text.strip():
        return None
    original = user_text.strip()
    lower = original.lower()
    if _is_compound_or_ambiguous(lower):
        return None
    for rule in _RULES:
        result = rule.matcher(lower)
        if result is not None:
            skill, args = result
            args = dict(args)
            for name, value in list(args.items()):
                if (skill, name) in _CASE_PRESERVING_ARGS and isinstance(value, str):
                    args[name] = _restore_case(original, lower, value)
            return RoutedIntent(skill=skill, args=args, rule=rule.name)
    return None
