"""
brain/semantic_intents.py — Tier 45: everyday commands said in other words.

The fast path (brain/intent_router.py) knows exact phrasings. "Make it
louder", "the screen is too dark", "move on to the next song" missed it and
went to the language model - a round trip of a second or more, and a slice
of Groq's per-minute quota, for a volume nudge. SARA adds a semantic
fallback: when no pattern matches, compare the sentence with example
phrases of every low-risk intent and run the closest one.

Wave does it with the sentence embeddings it already uses for memory
(database/embeddings.py, all-MiniLM-L6-v2), but an embedding alone is not
safe to act on. Measured on that model: "reduce the volume" sits closer to
"make it louder" than to "make it quieter", "brightness down" closer to
brightness UP, "record my screen" next to "take a screenshot", "set a
reminder" next to "list my reminders". So the embedding only picks a
FAMILY (volume, brightness, media, screenshot, time, ...), at least
MIN_SCORE similar and MARGIN ahead of the next family, and then the words
decide:
  - every word must be one that family uses (its examples, a list of its
    own words, a few fillers) - "cricket ki khabar", "weather in Pune",
    "time complexity", "record", "close", "not", "max" all fall out here;
  - the family's key word must be there (sound/volume/awaaz/loud...);
  - inside a family the direction comes from cue words, complaints first:
    "too loud" means DOWN, "too dark" means brighter; no cue, or two
    conflicting ones, and nothing runs.
Sentences that negate, question, condition or join commands (intent_router's
own markers, plus "and", "how do", "why", "kaise"), carry a number, or ask
whether something is so ("is the volume muted?") are never tried, nor is
Devanagari (the model is English-centric). Only T0/T1 tools are in the
table, always run with no argument (a sentence naming a city or a news
topic falls out on its words) - nothing that locks, closes, deletes or sends.
Anything not matched goes to the model exactly as before.
tests/test_tier45_semantic_intents.py keeps the measured cases.
SEMANTIC_INTENTS=false turns it off.
"""
import logging
import re
import threading

from brain import intent_router
from settings.config import config

logger = logging.getLogger("SemanticIntents")

MIN_SCORE = 0.60
MARGIN = 0.05
MAX_CHARS = 120

EXAMPLES = {
    "volume_up": ("make it louder", "turn the sound up a bit", "louder please, i can barely hear it",
                  "sound thoda aur badha do", "awaaz zyada karo", "pump up the volume", "it's too quiet"),
    "volume_down": ("make it quieter", "it's too loud", "lower the sound a little", "sound thoda kam kar do",
                    "awaaz dheemi karo", "turn the volume down a notch", "that's way too noisy"),
    "mute_volume": ("silence the speakers", "kill the sound", "no sound please", "sound band kar do",
                    "make it completely silent"),
    "unmute_volume": ("bring the sound back", "turn the sound back on", "sound wapas chalu karo",
                      "let me hear the audio again"),
    "brightness_up": ("the screen is too dark", "make the screen brighter", "brighter screen please, it's hard to see",
                      "screen ki roshni badha do", "light up the display more", "the display is too dim"),
    "brightness_down": ("the screen is too bright", "dim the display", "my eyes hurt, make the screen darker",
                        "screen ki roshni kam kar do", "lower the screen light", "the display is blinding me"),
    "take_screenshot": ("capture my screen", "grab a picture of the screen", "screen ki photo le lo",
                        "save what's on my screen as an image", "snap the screen"),
    "media_play_pause": ("pause the music", "stop the song for a moment", "resume the music", "gaana roko",
                         "gaana dobara chalao", "hold the playback"),
    "media_next_track": ("skip this song", "next track please", "agla gaana chalao", "move on to the next song"),
    "media_previous_track": ("go back to the last song", "play the previous track again", "pichla gaana chalao",
                             "replay the earlier song"),
    "list_reminders": ("what am i supposed to remember", "what have i asked you to remind me about",
                       "mere reminders kya hain", "anything pending to remind me", "show my upcoming reminders"),
    "daily_briefing": ("brief me on my day", "what does my day look like", "aaj ka plan batao",
                       "give me a rundown of today", "morning summary please"),
    "get_weather": ("will it rain today", "how hot is it outside", "do i need an umbrella",
                    "bahar mausam kaisa hai", "what's it like outside right now", "is it cold outside"),
    "get_current_time": ("what's the time now", "how late is it", "time kitna hua", "kitne baj gaye",
                         "tell me the clock time"),
    "get_current_date": ("what's today's date", "which date is it today", "aaj kaunsi tarikh hai",
                         "what day of the month is it"),
    "get_battery_status": ("how much charge is left", "is my laptop charged", "battery kitni bachi hai",
                           "how long will the battery last", "how's the battery doing"),
    "minimize_all_windows": ("hide all the windows", "clear my screen of windows", "sab windows chhoti kar do",
                             "minimize everything"),
    "get_news": ("any headlines today", "aaj ki khabar sunao", "tell me the latest news stories",
                 "what's the latest news"),
    "list_todos": ("what's on my task list", "what's still pending on my list", "mere kaam kya baaki hain",
                   "show my pending tasks"),
    "get_clipboard": ("what did i copy", "read out what's copied", "clipboard mein kya hai",
                      "what's in my copy buffer"),
}

# Intents the embedding can't tell apart (opposites read alike to it):
# it picks the family, cue words pick the intent.
FAMILIES = {
    "volume": ("volume_up", "volume_down", "mute_volume", "unmute_volume"),
    "brightness": ("brightness_up", "brightness_down"),
    "media": ("media_play_pause", "media_next_track", "media_previous_track"),
}
_FAMILY_OF = {i: f for f, intents in FAMILIES.items() for i in intents}

# A word that must appear for the family to be meant at all (a tuple: one
# from each - a screenshot needs the screen AND the capturing).
_ANCHOR = {
    "volume": r"sound|volume|audio|awaaz|aawaz|awaz|speakers?|loud\w*|quiet\w*|noisy|noise|"
              r"mute|unmute|silen\w*|hear",
    "brightness": r"screen|display|brightness|roshni|monitor",
    "media": r"songs?|music|tracks?|gaana|gaane|gana|playback|video|tune|playlist|spotify",
    "take_screenshot": (r"screenshot|screen|display", r"screenshot|capture|snap\w*|photo|picture|image|grab|shot"),
    "list_reminders": r"remind\w*|remember|yaad",
    "daily_briefing": r"day|briefing|brief|rundown|summary|plan|din",
    "get_weather": r"weather|mausam|rain\w*|sunny|cloudy|hot|cold|warm|temperature|umbrella|jacket|outside|"
                   r"baarish|garmi|sardi|thand",
    "get_current_time": r"time|clock|baj\w*|samay|waqt|how late",
    "get_current_date": r"date|tarikh|tareekh|day|month",
    "get_battery_status": r"battery|charge\w*|charging",
    "minimize_all_windows": r"minimi[sz]e|hide|windows|chhoti",
    "get_news": r"news|headlines?|khabar\w*|samachar|stories",
    "list_todos": r"todos?|to-do|tasks?|kaam|pending|baaki|baki",
    "get_clipboard": r"clipboard|copied|did i copy|copy buffer",
}
# Sentences a family's words allow that still ask for something else.
_NEVER = {
    "get_clipboard": r"^(?:copy|paste|clear|empty|set)\b|\bto (?:the |my )?clipboard\b",
    "list_reminders": r"\byou remember\b|\bremember (?:about|that|my)\b|\bremind me to\b",
    "get_current_time": r"^(?:is|it's|its|it is)\b",
    "get_battery_status": r"\byou charge\b",
    "daily_briefing": r"^what a\b",
}

# Every word must be one of these (plus the family's example words and
# _FILLER): a word outside them names something the tool can't take - a
# place, a topic, another day, another device - or makes it a remark.
_WORDS = {
    "volume": "sound sounds volume audio awaaz aawaz awaz speaker speakers music song video loud louder quiet "
              "quieter soft softer noise noisy mute unmute silence silent hear barely hardly up down increase "
              "decrease raise reduce lower boost pump crank higher more less kam badha badhao badhado zyada "
              "jyada tez dheema dheemi dheere soften turn make bring back on off again restore wapas chalu "
              "band kill shut no too way so all everything low go",
    "brightness": "screen display brightness bright brighter brighten dim dimmer darker darken dark light "
                  "lighter roshni monitor eyes eye hurt hurts hurting blinding harsh see read hard up down increase "
                  "decrease raise reduce lower more less badha badhao zyada tez kam make turn too so",
    "media": "song songs music track tracks gaana gaane gana playback video tune playlist spotify play next skip "
             "agla agle aage another change previous prev last back pichla pichle earlier replay pause "
             "resume unpause stop continue roko rok ruko hold dobara chalao chala lagao go move on one "
             "moment second",
    "take_screenshot": "screenshot screen display capture snap snapshot photo picture image grab save take le lo lelo "
                       "khicho kheecho",
    "list_reminders": "reminders reminder remind remember pending upcoming list show yaad koi any all",
    "daily_briefing": "brief briefing day today rundown summary summarize plan agenda morning look like "
                      "din kaisa kaisi going",
    "get_weather": "weather mausam rain raining rainy umbrella jacket sweater coat hot cold warm chilly humid "
                   "sunny cloudy windy outside out like temperature bahar kaisa kaisi garmi sardi thand "
                   "baarish need do",
    "get_current_time": "time clock now current exact late kitna kitne baj baje gaye hua samay waqt abhi",
    "get_current_date": "date today's todays which day month tarikh tareekh kaunsi kaun current",
    "get_battery_status": "battery charge charged charging left much how long last laptop percentage level "
                          "status bachi kitni kitna power remaining check doing do have",
    "minimize_all_windows": "minimize minimise everything all windows window hide clear sab chhoti chhota",
    "get_news": "news headlines headline latest top stories story khabar khabrein samachar read current "
                "breaking",
    "list_todos": "todo todos tasks task list kaam pending baaki baki still do have remaining show",
    "get_clipboard": "clipboard copied copy did read out buffer show contents content text",
}
_FILLER = set(
    "a an the it it's its is are am this that to of on in for with me my i you your please now right just "
    "bit little thoda thodi zara bahut aur kar karo kardo krdo kr do de dena dijiye hai hain kya ka ki ke "
    "ko se mein me mera meri mere mujhe liye can could will okay ok hey wave yaar bhai some what what's whats "
    "how how's tell give".split())

# Inside a family: (complaint, cue). A complaint about how things are now
# ("too loud", "hurts my eyes") wins over plain cue words.
_CUES = {
    "volume_up": (r"too (?:quiet|soft|low)|barely hear|hard to hear",
                  r"louder|up|increase|raise|boost|pump|higher|more|badh\w*|zyada|jyada|tez|crank"),
    "volume_down": (r"too (?:loud|noisy)",
                    r"quieter|softer|down|decrease|reduce|lower|less|kam|dheem\w*|dheere|soften"),
    "mute_volume": (None, r"mute|silence|silent|band|off|kill|shut|no sound"),
    "unmute_volume": (None, r"unmute|restore|wapas|back|chalu|on again|hear\b.*\bagain"),
    "brightness_up": (r"too (?:dark|dim)|hard to see",
                      r"brighter|brighten|bright|up|increase|raise|more|higher|badh\w*|zyada|tez|lighter"),
    "brightness_down": (r"too (?:bright|harsh)|eyes hurt|hurts? my eyes|blinding",
                        r"dim|dimmer|darker|darken|down|decrease|reduce|lower|less|kam|harsh"),
    "media_next_track": (None, r"next|skip|agla|agle|aage|another|change"),
    "media_previous_track": (None, r"previous|prev|last|back|pichla|pichle|earlier|replay"),
    "media_play_pause": (None, r"pause|resume|unpause|stop|continue|roko|rok|ruko|hold|dobara"),
}
# Next/previous outrank play/pause ("play the previous track" is not a toggle).
_MEDIA_ORDER = (("media_next_track", "media_previous_track"), ("media_play_pause",))

# Tools that change something: a yes/no question about them ("is the
# volume muted?", "kya gaana chal raha hai") never asks to act.
_ACTIONS = set(_FAMILY_OF) | {"take_screenshot", "minimize_all_windows"}

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_NOT_A_COMMAND = re.compile(
    r"\b(?:and|also|plus|how (?:do|does|can|could|would|should|to)|why|explain|meaning|kaise|kyun?|kyon)\b"
    r"|\bwhat (?:is|are) (?:a|an)\b|\d")
_YES_NO_QUESTION = re.compile(r"^(?:is|are|was|were|does|did|has|have|kya)\b|\bdo you (?:think|know)\b")
_WORD = re.compile(r"[a-z']+")


def _rx(alternatives):
    return re.compile(r"\b(?:" + alternatives + r")\b") if alternatives else None


_ANCHOR_RX = {k: [_rx(a) for a in (v if isinstance(v, tuple) else (v,))] for k, v in _ANCHOR.items()}
_NEVER_RX = {k: re.compile(v) for k, v in _NEVER.items()}
_CUE_RX = {k: (_rx(c), _rx(q)) for k, (c, q) in _CUES.items()}


def _family(intent: str) -> str:
    return _FAMILY_OF.get(intent, intent)


def _vocabulary():
    vocab = {}
    for intent, phrases in EXAMPLES.items():
        fam = _family(intent)
        words = vocab.setdefault(fam, set(_FILLER) | set(_WORDS.get(fam, "").split()))
        for phrase in phrases:
            words.update(_WORD.findall(phrase))
    return vocab


_VOCAB = _vocabulary()


def _enabled() -> bool:
    return bool(getattr(config, "SEMANTIC_INTENTS", True))


def _unit(vec):
    import numpy as np  # noqa: PLC0415
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return v / n if n else v


def sentence_blocked(lower: str) -> bool:
    """True when the sentence as a whole mustn't be mapped to any intent."""
    return intent_router._is_compound_or_ambiguous(lower) or bool(_NOT_A_COMMAND.search(lower))


def _pick(candidates, lower):
    """The one intent among `candidates` whose cue (complaints first) is in
    `lower`; None when none or several are."""
    for slot in (0, 1):
        hits = [i for i in candidates if _CUE_RX[i][slot] is not None and _CUE_RX[i][slot].search(lower)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
    return None


def resolve(family: str, lower: str):
    """Which intent of `family` this sentence asks for, by its words - or None."""
    words = _WORD.findall(lower)
    if not words or any(w not in _VOCAB.get(family, ()) for w in words):
        return None
    anchors = _ANCHOR_RX.get(family)
    if not anchors or not all(a.search(lower) for a in anchors):
        return None
    never = _NEVER_RX.get(family)
    if never is not None and never.search(lower):
        return None
    if family == "media":
        intent = None
        for group in _MEDIA_ORDER:
            intent = _pick(group, lower)
            if intent or any(_CUE_RX[i][1].search(lower) for i in group):
                break
    elif family in FAMILIES:
        intent = _pick(FAMILIES[family], lower)
    else:
        intent = family
    if intent in _ACTIONS and _YES_NO_QUESTION.search(lower):
        return None
    return intent


class SemanticIntents:
    def __init__(self, embed=None, examples=None):
        self._embed = embed
        self._examples = examples or EXAMPLES
        self._lock = threading.Lock()
        self._table = None           # [(family, unit vector)]
        self._failed = False

    def _embedder(self):
        if self._embed is not None:
            return self._embed
        from database import embeddings  # noqa: PLC0415
        return embeddings.embed

    def _ready(self, wait: bool = True) -> bool:
        """Embeds the examples once. wait=False: don't sit out a warm-up
        that's still loading the model - that turn just goes to the model."""
        if self._table is not None:
            return True
        if self._failed or not self._lock.acquire(blocking=wait):
            return False
        try:
            if self._table is None and not self._failed:
                embed = self._embedder()
                table = []
                for intent, phrases in self._examples.items():
                    for phrase in phrases:
                        vec = embed(phrase)
                        if vec is None:
                            self._failed = True
                            logger.info("Semantic intents off: sentence embeddings aren't available.")
                            return False
                        table.append((_family(intent), _unit(vec)))
                self._table = table
        except Exception:
            self._failed = True
            logger.warning("Semantic intents off: embedding the examples failed.", exc_info=True)
        finally:
            self._lock.release()
        return self._table is not None

    def warm_up(self) -> None:
        """Loads the embedding model and the examples in the background."""
        if _enabled():
            threading.Thread(target=self._ready, name="SemanticIntents", daemon=True).start()

    def scores(self, text: str) -> list:
        """[(family, best similarity)] highest first - for tests and tuning."""
        if not self._ready(wait=False):
            return []
        vec = self._embedder()(text)
        if vec is None:
            return []
        q = _unit(vec)
        best: dict = {}
        for family, v in self._table:
            s = float(q @ v)
            if s > best.get(family, -1.0):
                best[family] = s
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)

    def match(self, text: str):
        """(intent, score) when one low-risk intent is clearly meant, else None."""
        text = (text or "").strip()
        if not _enabled() or not text or len(text) > MAX_CHARS or _DEVANAGARI.search(text):
            return None
        lower = text.lower()
        if len(lower.split()) < 2 or sentence_blocked(lower):
            return None
        ranked = self.scores(text)
        if not ranked:
            return None
        family, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else -1.0
        if top < MIN_SCORE or top - second < MARGIN:
            return None
        intent = resolve(family, lower)
        return (intent, top) if intent else None


semantic_intents = SemanticIntents()
