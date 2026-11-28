"""
brain/handlers/media.py - playback/volume control, plus the pycaw volume
helpers (_get_audio_interface/adjust_system_volume/toggle_system_mute) moved
here WITH their only AI-tool-calling callers. groq_core.py's keyword/
deterministic dispatch path (_execute_single_action_inner) needs these same
two functions too - it imports them back from here rather than the other way
around, since groq_core.py already has to import this whole package (to
trigger registration) and the reverse direction would be a circular import.

Tier 4: pulled out of brain/groq_core.py's WaveBrain class, which had grown to
1942+ lines with 50 _skill_* methods - a "no behavior change, pure refactor" per
the roadmap. Each function here is registered via the exact same @skill(...)
decorator at module-import time (brain/skills_registry.py's register() has
always documented its handlers as plain `fn(brain, args: dict) -> str` functions,
not bound methods - both _dispatch_tool() and _replay_confirmed() already call
handler(self, args) with the WaveBrain instance passed explicitly, so moving a
method out of the class into a plain top-level function taking `brain` instead of
`self` needed no change to either call site). See brain/handlers/__init__.py for
why these 8 files, and groq_core.py's own top for why it still imports this
package even though nothing in it is referenced by name.
"""
import logging

import pyautogui
from automation.app_control import AppController
from web.web_ops import WebManager
from brain.skills_registry import skill

try:
    from pycaw.pycaw import AudioUtilities
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

logger = logging.getLogger("GroqBrain")


def _get_audio_interface():
    devices = AudioUtilities.GetSpeakers()
    return devices.EndpointVolume


def adjust_system_volume(delta_percent: int) -> bool:
    """Nudges system volume via the real Windows Core Audio API - far more
    reliable than a simulated media-key press, which some Windows setups
    and audio drivers silently swallow. Returns True if pycaw handled it."""
    if not HAS_PYCAW:
        return False
    try:
        vol = _get_audio_interface()
        current = round(vol.GetMasterVolumeLevelScalar() * 100)
        new_val = max(0, min(100, current + delta_percent))
        vol.SetMasterVolumeLevelScalar(new_val / 100.0, None)
        return True
    except Exception as e:
        logger.error(f"adjust_system_volume error: {e}")
        return False


def toggle_system_mute() -> bool:
    """Toggles mute via pycaw. Returns True if it succeeded."""
    if not HAS_PYCAW:
        return False
    try:
        vol = _get_audio_interface()
        vol.SetMute(0 if vol.GetMute() else 1, None)
        return True
    except Exception as e:
        logger.error(f"toggle_system_mute error: {e}")
        return False


_BROWSERS = ("brave", "chrome", "edge", "firefox", "opera")
_MEDIA_WORDS = ("youtube", "song", "music", "video", "gaana", "gana", "media", "play")


def preferred_media_browser():
    """Tier 57: the browser the user told Wave to play songs/videos in. The
    model saves such a wish with remember_this under a key it picks itself
    (on the owner's PC: "preferred_browser_for_youtube: Brave") - and the
    play path never read it, so songs kept opening in the default browser.
    A key about YouTube/songs/music/videos wins over a general one
    ("preferred_browser"); None when nothing like that is remembered."""
    try:
        from database.db_manager import db
        facts = db.get_all_memory(include_archived=False)
    except Exception:
        logger.debug("preferred browser lookup failed", exc_info=True)
        return None
    general = None
    for key, value in facts:
        k, v = str(key).lower(), str(value).lower()
        if "browser" not in k:
            continue
        name = next((b for b in _BROWSERS if b in v), None)
        if not name:
            continue
        if any(w in k for w in _MEDIA_WORDS):
            return name
        general = general or name
    return general


@skill("play_youtube", "Plays a song or video on YouTube. Use this whenever the user asks to play, put on, or listen "
       "to any song/music/video. If they name a browser ('on Brave'), pass it as `browser`; without one, the browser "
       "they asked you earlier to use for songs/YouTube is used automatically.",
       {"query": {"type": "string", "description": "Song name, artist, or video to search and play."},
        "browser": {"type": "string", "description": "Only if the user named a browser: brave, chrome, edge, firefox "
                                                     "or opera."}}, ["query"])
def _skill_play_youtube(brain, args: dict) -> str:
    browser = (args.get("browser") or "").strip().lower() or preferred_media_browser()
    return WebManager.play_on_youtube(args.get("query", ""), browser)


# Tier 16: opens a Spotify search in the browser rather than a real Web API
# integration - that would need the user's own OAuth app/API key, which
# cuts against this project's "no API key needed" posture for an optional
# feature like this one (play_youtube above needs no key either). Goes
# through AppController.open_website(), the same general "search on a
# known site" path open_website/search_google already use - "spotify" just
# needed a search URL template added alongside its existing bare-site one
# (see automation/app_control.py's _SEARCH_URL_TEMPLATES).
@skill("play_on_spotify", "Opens a search for a song/artist/album on Spotify in the browser. Use this "
       "when the user specifically asks for Spotify ('play X on Spotify') - use play_youtube instead "
       "for an unqualified 'play X'.",
       {"query": {"type": "string", "description": "Song, artist, or album to search for on Spotify."}},
       ["query"])
def _skill_play_on_spotify(brain, args: dict) -> str:
    return AppController.open_website("spotify", args.get("query", ""))


@skill("media_play_pause", "Toggles play/pause on whatever media player is currently active (does not search for a new song).")
def _skill_media_play_pause(brain, args: dict) -> str:
    pyautogui.press('playpause')
    return "Toggled play/pause."


@skill("media_next_track", "Skips to the next track in the current media player.")
def _skill_media_next_track(brain, args: dict) -> str:
    pyautogui.press('nexttrack')
    return "Skipped to the next track."


@skill("media_previous_track", "Goes back to the previous track in the current media player.")
def _skill_media_previous_track(brain, args: dict) -> str:
    pyautogui.press('prevtrack')
    return "Went to the previous track."


@skill("volume_up", "Increases system volume by about 10%.")
def _skill_volume_up(brain, args: dict) -> str:
    return AppController.volume_up()


@skill("volume_down", "Decreases system volume by about 10%.")
def _skill_volume_down(brain, args: dict) -> str:
    return AppController.volume_down()


# Tier 14: set_volume/volume_max/volume_min/mute_volume/unmute_volume are
# new; volume_up/volume_down just above now go through AppController too
# (they used to call adjust_system_volume directly - see this file's own
# module docstring for why that function stays put, used only by
# groq_core.py's separate legacy keyword-dispatch path) - every volume
# change from the AI tool-calling / intent_router path now lands on the
# one audited, undoable timeline. 🏆
#
# toggle_mute is RETIRED, not kept alongside these: it used to mute OR
# unmute depending on a blind flip of whatever pycaw's current mute state
# happened to be, which is exactly the bug the roadmap calls out ("mutes
# when you say 'unmute' while unmuted"). mute_volume/unmute_volume below
# are each an explicit, one-directional ask - there's no ambiguous middle
# state left for a "toggle" version to get wrong, so there's nothing a
# kept-around toggle_mute would still be for.
@skill("set_volume", "Sets system volume to an exact percentage.",
       {"percent": {"type": "integer", "description": "Target volume, 0-100."}}, ["percent"])
def _skill_set_volume(brain, args: dict) -> str:
    return AppController.set_volume(args.get("percent", 50))


@skill("volume_max", "Sets system volume to 100% (full volume).")
def _skill_volume_max(brain, args: dict) -> str:
    return AppController.volume_max()


@skill("volume_min", "Sets system volume to 0% (silent, without muting).")
def _skill_volume_min(brain, args: dict) -> str:
    return AppController.volume_min()


@skill("mute_volume", "Mutes system audio. Unlike a toggle, this always results in muted, even if "
       "it already was.")
def _skill_mute_volume(brain, args: dict) -> str:
    return AppController.mute_volume()


@skill("unmute_volume", "Unmutes system audio. Unlike a toggle, this always results in unmuted, even if "
       "it already was.")
def _skill_unmute_volume(brain, args: dict) -> str:
    return AppController.unmute_volume()
