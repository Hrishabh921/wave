import asyncio
import logging

logger = logging.getLogger("MediaInfo")

try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    HAS_WINSDK = True
except ImportError:
    HAS_WINSDK = False


async def _get_media_info_async():
    manager = await MediaManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return None

    props = await session.try_get_media_properties_async()
    playback_info = session.get_playback_info()
    timeline = session.get_timeline_properties()

    is_playing = (
        playback_info is not None
        and playback_info.playback_status == PlaybackStatus.PLAYING
    )

    position = 0
    duration = 0
    if timeline is not None:
        try:
            position = max(0, timeline.position.total_seconds())
            duration = max(0, timeline.end_time.total_seconds())
        except Exception:
            pass

    return {
        "title": (props.title if props and props.title else "") or "Unknown Title",
        "artist": (props.artist if props and props.artist else ""),
        "is_playing": is_playing,
        "position": position,
        "duration": duration,
    }


def get_now_playing():
    """Synchronous wrapper. Call from a background thread only — never the
    Qt GUI thread, since this spins up its own asyncio event loop and can
    take a moment to respond."""
    if not HAS_WINSDK:
        return None
    try:
        return asyncio.run(_get_media_info_async())
    except Exception as e:
        logger.error(f"get_now_playing error: {e}")
        return None


async def _seek_async(position_seconds: float) -> bool:
    manager = await MediaManager.request_async()
    session = manager.get_current_session()
    if session is None:
        return False
    ticks = int(max(0.0, position_seconds) * 10_000_000)  # seconds -> 100ns ticks
    return bool(await session.try_change_playback_position_async(ticks))


def seek_to(position_seconds: float) -> bool:
    """🚨 NEW: Jumps the currently-playing media to an absolute position (in
    seconds), for the draggable progress bar / skip-forward-back buttons.
    Call from a background thread only, same reason as get_now_playing().
    Whether this actually works depends on the player — most (Spotify,
    browser YouTube, VLC, etc.) support it via Windows' Media Session API,
    but a few apps ignore the request."""
    if not HAS_WINSDK:
        return False
    try:
        return asyncio.run(_seek_async(position_seconds))
    except Exception as e:
        logger.error(f"seek_to error: {e}")
        return False


def format_time(seconds) -> str:
    """Formats seconds as M:SS, or H:MM:SS for anything over an hour."""
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "0:00"
    if seconds < 0:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"