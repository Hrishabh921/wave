"""
automation/calendar_sync.py — Tier 18: calendar sync (Google OAuth + ICS).

Before this, automation/calendar_manager.py's calendar was entirely local -
every event was one the user told Wave about by voice/chat. Most people's
real calendar lives somewhere else already (Google Calendar, a work
calendar that publishes an ICS feed) - this module is what pulls THOSE
events into the same local calendar_events table, so list_calendar_events,
the daily briefing, and the proactive engine's upcoming-event nudge all see
them for free without any of those three needing to know or care where an
event actually came from.

Two independent sources, one shared pipeline:
  - ICS: any calendar that publishes a .ics feed URL (an "subscribe via
    URL"/webcal link - most calendar apps, including Google Calendar
    itself, can export one). Fetched and parsed by hand below rather than
    with a third-party ics-parsing library - RFC 5545's actual format is a
    short, well-defined line-based grammar, and pulling in a new dependency
    for it would be a worse trade than ~100 lines of straightforward
    parsing. Deliberately does NOT implement RRULE/recurring-instance
    expansion - see _simplify_rrule()'s own docstring for exactly how far
    recurrence support goes and why.
  - Google Calendar: the real Calendar API (v3) over OAuth2, using Google's
    own official client libraries (google-auth-oauthlib,
    google-api-python-client) - guarded by HAS_GOOGLE_CALENDAR, same
    optional-dependency shape as voice/tts.py's HAS_PIPER, since neither is
    in requirements.txt's "Core" section. setup_google() runs a real
    browser-based OAuth consent flow - something this sandbox has no way to
    exercise end to end (no browser, no real Google account, no network
    path to accounts.google.com) - so, like Google OAuth everywhere else in
    this project's own docs discipline, it's write-only here: correct by
    inspection and by Google's own documented client library behavior, but
    genuinely unverified against a real account. See NEXT_STEPS.md's Tier
    18 section.

Both sources upsert into calendar_events via database/db_manager.py's
upsert_synced_calendar_event() (keyed on (source, external_id), so
re-syncing never duplicates) and are bounded to the same sync window
(config.CALENDAR_SYNC_WINDOW_DAYS/CALENDAR_SYNC_LOOKBACK_HOURS) - see
sync_all()'s own comment for why a window at all.

CalendarSync is a stateless, classmethod-based backend - same shape as
CalendarManager/WorkflowScheduler/StreakManager - every public method
returns a plain string meant to be handed straight back to the model as a
tool result.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from database.db_manager import db
from settings.config import config

logger = logging.getLogger("CalendarSync")

try:
    from zoneinfo import ZoneInfo
    HAS_ZONEINFO = True
except ImportError:  # pragma: no cover - stdlib since Python 3.9, requirements.txt targets 3.11
    HAS_ZONEINFO = False

try:
    from google.auth.transport.requests import Request as _GoogleAuthRequest
    from google.oauth2.credentials import Credentials as _GoogleCredentials
    from google_auth_oauthlib.flow import InstalledAppFlow as _GoogleInstalledAppFlow
    from googleapiclient.discovery import build as _build_google_service
    HAS_GOOGLE_CALENDAR = True
except ImportError:
    # Same "bind a None placeholder, don't just leave the name undefined"
    # shape as voice/tts.py's own HAS_PIPER/PiperVoice guard - every name
    # below is always a valid module attribute (None when the package
    # isn't installed), so code (and tests, via mock.patch.object, which
    # requires the attribute to already exist) can always reference it
    # regardless of what's installed in a given environment.
    _GoogleAuthRequest = _GoogleCredentials = _GoogleInstalledAppFlow = _build_google_service = None
    HAS_GOOGLE_CALENDAR = False
    logger.info("google-auth-oauthlib/google-api-python-client not installed - "
                "Google Calendar sync unavailable (ICS feeds still work).")

_GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


# ---- ICS: fetch ----
def _build_ics_session() -> requests.Session:
    """A small dedicated retry session for fetching ICS feeds - deliberately
    NOT web/web_ops.py's shared module-level session: that module hard-
    imports pywhatkit at the top level (a WhatsApp-specific dependency with
    nothing to do with calendar sync), and importing anything from it here
    would drag that import cost into every caller of this otherwise-lean
    module - the same reason automation/proactive.py's _check_morning_
    briefing does a local import of skills/daily_briefing instead of a
    module-level one, just solved here by not sharing the module at all
    rather than by deferring the import. Same Retry/HTTPAdapter shape as
    web_ops.py's own _build_session(), a second independent instance."""
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(("GET", "HEAD")), respect_retry_after_header=True,
                  raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_ics_session = _build_ics_session()


def _fetch_ics(url: str) -> str:
    """Raises on any failure (bad URL, network error, non-2xx) - callers
    decide how to turn that into a user-facing message."""
    response = _ics_session.get(url, timeout=15)
    response.raise_for_status()
    return response.text


# ---- ICS: parsing (RFC 5545, hand-rolled - see this module's own top docstring) ----
_ICS_ESCAPE_RE = re.compile(r"\\(.)")
_ICS_ESCAPES = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


def _unescape_ics_text(value: str) -> str:
    """A single left-to-right pass undoing RFC 5545 TEXT escaping (\\n,
    \\,, \\;, \\\\) - a sequence of independent str.replace() calls would
    risk double-unescaping (e.g. a literal '\\\\n' in the source becoming
    a newline instead of a backslash followed by 'n'), so this does it in
    one regex substitution instead, each backslash pair consumed exactly
    once."""
    return _ICS_ESCAPE_RE.sub(lambda m: _ICS_ESCAPES.get(m.group(1), m.group(1)), value)


def _unfold_ics_lines(text: str) -> list:
    """RFC 5545 line folding: a line starting with a single space/tab is a
    continuation of the previous line, not a new one - has to be undone
    before any property can be parsed. Blank lines are dropped."""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = []
    for line in raw_lines:
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        elif line.strip():
            lines.append(line)
    return lines


def _parse_ics_line(line: str):
    """(NAME, {PARAM: value}, raw_value) for one unfolded content line, or
    (None, {}, "") if it doesn't even look like 'NAME[;PARAMS]:VALUE'."""
    if ":" not in line:
        return None, {}, ""
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].strip().upper()
    if not name:
        return None, {}, ""
    params = {}
    for part in parts[1:]:
        if "=" in part:
            key, val = part.split("=", 1)
            params[key.strip().upper()] = val.strip()
    return name, params, value


def _parse_ics_datetime(value: str, params: dict):
    """A naive LOCAL-time ISO string (matching every other calendar_events
    row's own storage convention - CalendarManager never stores a
    timezone either), or None if `value` can't be understood. Handles the
    three shapes RFC 5545 actually uses day to day:
      - VALUE=DATE (all-day): 'YYYYMMDD' -> midnight that day.
      - UTC: 'YYYYMMDDTHHMMSSZ' -> converted from UTC to the system's local
        time.
      - A TZID-qualified or "floating" (no Z, no TZID) local date-time:
        'YYYYMMDDTHHMMSS' -> if TZID names a zone Python's stdlib zoneinfo
        recognizes, converted from that zone to local time; otherwise (no
        TZID, or an unrecognized/olson-incompatible name) taken as already
        being local - which is exactly what a floating time is DEFINED to
        mean in the spec, and the most reasonable fallback for a TZID
        zoneinfo doesn't know."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or (len(value) == 8 and value.isdigit()):
        try:
            return datetime.strptime(value, "%Y%m%d").isoformat()
        except ValueError:
            return None

    is_utc = value.endswith("Z")
    core = value[:-1] if is_utc else value
    try:
        dt = datetime.strptime(core, "%Y%m%dT%H%M%S")
    except ValueError:
        return None

    if is_utc:
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None).isoformat()

    tzid = params.get("TZID")
    if tzid and HAS_ZONEINFO:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tzid)).astimezone().replace(tzinfo=None)
        except Exception:
            pass  # unrecognized zone name - fall through and treat the value as already-local
    return dt.isoformat()


def _simplify_rrule(rrule_value: str):
    """'daily'/'weekly' for the plain FREQ=DAILY/FREQ=WEEKLY case (matching
    calendar_events.recurrence's own limited vocabulary - see db_manager.py's
    migration comment), or None for anything else - COUNT, UNTIL, INTERVAL,
    BYDAY, or any other FREQ. Those import as a one-off at their first
    occurrence rather than attempting a recurrence rule this app has no way
    to express or expand - a real RRULE engine is a project of its own, and
    "repeats daily"/"repeats weekly, no end date" already covers the two
    recurring-event shapes CalendarManager.create_event itself supports, so
    this simplification loses nothing a locally-created event could do
    anyway. INTERVAL is excluded even when it's just '1' (same as the
    implicit default) - a source explicit enough to state it is treated as
    possibly meaning something this can't represent, and importing the
    first occurrence only (recurrence=None) is the honest, safe fallback,
    not a guess."""
    parts = dict(p.split("=", 1) for p in rrule_value.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    if "COUNT" in parts or "UNTIL" in parts or "INTERVAL" in parts:
        return None
    if freq == "DAILY":
        return "daily"
    if freq == "WEEKLY":
        return "weekly"
    return None


def _parse_vevents(text: str) -> list:
    """[{'uid', 'summary', 'start_iso', 'end_iso', 'location', 'description',
    'recurrence'}, ...], one dict per VEVENT block, in source order. A
    block missing a UID or a parseable DTSTART is skipped outright - both
    are required for a usable, dedup-able synced event (see
    upsert_synced_calendar_event's own (source, external_id) key)."""
    events = []
    in_event = False
    cur = {}
    for line in _unfold_ics_lines(text):
        upper = line.upper()
        if upper.startswith("BEGIN:VEVENT"):
            in_event = True
            cur = {}
            continue
        if upper.startswith("END:VEVENT"):
            if in_event and cur.get("uid") and cur.get("start_iso"):
                events.append(cur)
            in_event = False
            continue
        if not in_event:
            continue

        name, params, raw_value = _parse_ics_line(line)
        if name == "UID":
            cur["uid"] = raw_value.strip()
        elif name == "SUMMARY":
            cur["summary"] = _unescape_ics_text(raw_value)
        elif name == "LOCATION":
            cur["location"] = _unescape_ics_text(raw_value) or None
        elif name == "DESCRIPTION":
            cur["description"] = _unescape_ics_text(raw_value) or None
        elif name == "DTSTART":
            cur["start_iso"] = _parse_ics_datetime(raw_value, params)
        elif name == "DTEND":
            cur["end_iso"] = _parse_ics_datetime(raw_value, params)
        elif name == "RRULE":
            cur["recurrence"] = _simplify_rrule(raw_value)
    return events


# ---- Google Calendar ----
def _get_google_credentials():
    """The cached, refreshed-if-needed OAuth credentials, or None if
    setup_google() hasn't been completed yet (no token file) or the token
    can't be refreshed (revoked access, say - the user would need to run
    setup_google() again)."""
    token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
    if not os.path.isfile(token_path):
        return None
    try:
        creds = _GoogleCredentials.from_authorized_user_file(token_path, _GOOGLE_SCOPES)
    except Exception:
        logger.exception("Couldn't read the cached Google Calendar token")
        return None
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(_GoogleAuthRequest())
            with open(token_path, "w") as fh:
                fh.write(creds.to_json())
        except Exception:
            logger.exception("Couldn't refresh the Google Calendar token")
            return None
    return creds


def _parse_google_time(info: dict):
    """A naive local-time ISO string from one of events().list()'s own
    {'date': 'YYYY-MM-DD'} (all-day) or {'dateTime': '...'} (timed) shapes,
    or None. dateTime's trailing 'Z' (if present) is normalized to an
    explicit '+00:00' before fromisoformat() - the bare-'Z' shorthand was
    only added to datetime.fromisoformat() in Python 3.11, and this should
    work on anything 3.7+ that has fromisoformat at all."""
    if not info:
        return None
    if "date" in info:
        try:
            return datetime.strptime(info["date"], "%Y-%m-%d").isoformat()
        except ValueError:
            return None
    raw = info.get("dateTime")
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat()


def _sync_google(window_start: datetime, window_end: datetime):
    """(inserted, updated, error). error is '' on success. singleEvents=True
    asks Google's own API to expand any recurring event into individual
    instances server-side, each with its own event id - sidesteps needing
    to parse Google's RRULE representation locally at all, unlike the ICS
    path above (which has no such server-side expansion to lean on)."""
    if not HAS_GOOGLE_CALENDAR:
        return 0, 0, ("Google Calendar sync needs the 'google-auth-oauthlib' and "
                      "'google-api-python-client' packages installed - see requirements.txt.")
    creds = _get_google_credentials()
    if creds is None:
        return 0, 0, "Google Calendar isn't connected yet - run setup_google_calendar first."

    try:
        service = _build_google_service("calendar", "v3", credentials=creds, cache_discovery=False)
        result = service.events().list(
            calendarId="primary",
            timeMin=window_start.astimezone().isoformat(),
            timeMax=window_end.astimezone().isoformat(),
            singleEvents=True, orderBy="startTime", maxResults=2500,
        ).execute()
    except Exception as e:
        logger.exception("Google Calendar events().list() failed")
        return 0, 0, f"Couldn't fetch Google Calendar events: {e}"

    inserted = updated = 0
    for item in result.get("items", []):
        event_id = item.get("id")
        start_iso = _parse_google_time(item.get("start"))
        if not event_id or not start_iso:
            continue
        outcome = db.upsert_synced_calendar_event(
            "google", event_id, item.get("summary") or "(no title)", start_iso,
            _parse_google_time(item.get("end")), item.get("location"), item.get("description"), None)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
    return inserted, updated, ""


# ---- shared sync plumbing ----
def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def _in_window(iso_str, window_start: datetime, window_end: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(iso_str)
    except (TypeError, ValueError):
        return False
    return window_start <= dt < window_end


def _sync_ics(feed_name: str, url: str, window_start: datetime, window_end: datetime):
    """(inserted, updated, error). The whole feed is fetched and parsed (an
    ICS file has no partial/time-ranged fetch the way an API call does) -
    only events landing inside the window are upserted, same reasoning as
    sync_all()'s own comment on why a window exists at all."""
    try:
        text = _fetch_ics(url)
    except Exception as e:
        return 0, 0, f"couldn't fetch the feed ({e})"

    inserted = updated = 0
    source = f"ics:{feed_name}"
    for ev in _parse_vevents(text):
        if not _in_window(ev["start_iso"], window_start, window_end):
            continue
        outcome = db.upsert_synced_calendar_event(
            source, ev["uid"], ev.get("summary") or "(no title)", ev["start_iso"],
            ev.get("end_iso"), ev.get("location"), ev.get("description"), ev.get("recurrence"))
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
    return inserted, updated, ""


def _format_feed_line(row) -> str:
    _id, name, kind, _url, last_synced_at = row
    kind_label = "Google" if kind == "google" else "ICS"
    when = f", last synced {last_synced_at}" if last_synced_at else ", never synced yet"
    return f"'{name}' ({kind_label}{when})"


class CalendarSync:
    @staticmethod
    def add_ics_feed(name: str, url: str) -> str:
        feed_name = _normalize(name)
        url = (url or "").strip()
        if not feed_name:
            return "I need a name for this calendar feed."
        if feed_name == "google":
            return "'google' is reserved for the Google Calendar connection - pick a different name."
        if not url:
            return "I need the feed's .ics URL."
        if not db.save_calendar_feed(feed_name, "ics", url):
            return f"Couldn't save the '{feed_name}' feed - something went wrong writing to the database."
        return f"Added '{feed_name}' as an ICS calendar feed. Run sync_calendars to pull in its events."

    @staticmethod
    def remove_feed(name: str) -> str:
        feed_name = _normalize(name)
        if not feed_name:
            return "I need to know which calendar feed to remove."
        if not db.delete_calendar_feed(feed_name):
            return f"I don't have a calendar feed called '{feed_name}'."
        if feed_name == "google":
            # Tier 18: makes disconnecting Google a true one-command undo
            # for setup_google() (see brain/tiers.py's T1 reasoning for
            # setup_google_calendar) - the cached token alone would
            # otherwise keep letting sync_calendars pull Google events
            # back in even after the feed row is gone.
            try:
                if os.path.isfile(config.GOOGLE_CALENDAR_TOKEN_PATH):
                    os.remove(config.GOOGLE_CALENDAR_TOKEN_PATH)
            except OSError:
                logger.exception("Couldn't remove the cached Google Calendar token file")
        return f"Removed the '{feed_name}' calendar feed. Its already-synced events are left in your calendar."

    @staticmethod
    def list_feeds() -> str:
        rows = db.list_calendar_feeds()
        if not rows:
            return ("You don't have any calendar feeds set up - add one with add_ics_calendar_feed, "
                    "or connect Google Calendar with setup_google_calendar.")
        lines = [_format_feed_line(r) for r in rows]
        return f"{len(rows)} calendar feed(s): " + "; ".join(lines)

    @staticmethod
    def setup_google() -> str:
        """Runs a real, interactive, browser-based OAuth consent flow
        (InstalledAppFlow.run_local_server) - genuinely can't be exercised
        in an automated test or this sandbox; see this module's own top
        docstring. credentials.json is the user's own OAuth client secret
        from Google Cloud Console - never bundled (a per-developer
        credential, not something this app can ship)."""
        if not HAS_GOOGLE_CALENDAR:
            return ("Google Calendar sync needs the 'google-auth-oauthlib' and "
                    "'google-api-python-client' packages installed - see requirements.txt.")
        creds_path = config.GOOGLE_CALENDAR_CREDENTIALS_PATH
        if not os.path.isfile(creds_path):
            return (f"I don't see a Google OAuth credentials file at {creds_path}. In Google Cloud "
                    f"Console, create an OAuth client ID for a Desktop app, download its credentials.json, "
                    f"and save it there, then try again.")
        try:
            flow = _GoogleInstalledAppFlow.from_client_secrets_file(creds_path, _GOOGLE_SCOPES)
            creds = flow.run_local_server(port=0)
        except Exception as e:
            logger.exception("Google Calendar OAuth flow failed")
            return f"The Google sign-in flow didn't complete: {e}"

        try:
            token_dir = os.path.dirname(config.GOOGLE_CALENDAR_TOKEN_PATH)
            if token_dir:
                os.makedirs(token_dir, exist_ok=True)
            with open(config.GOOGLE_CALENDAR_TOKEN_PATH, "w") as fh:
                fh.write(creds.to_json())
        except OSError as e:
            return f"Signed in, but couldn't save the token file: {e}"

        db.save_calendar_feed("google", "google", None)
        return "Google Calendar connected - run sync_calendars to pull in your events."

    @staticmethod
    def sync_all() -> str:
        """Syncs every registered feed (ICS and/or Google) in one pass.
        Both bounded to the same window - how far into the future (plus a
        short look-back, to still catch events earlier today) a sync pulls
        events for. A personal ICS export, or a Google account with years
        of history, could otherwise mean one sync importing every event
        that ever existed; see settings/config.py's own comment on the two
        knobs involved. A problem with one feed (an unreachable URL, a
        revoked Google token) is reported but doesn't stop the others from
        syncing."""
        rows = db.list_calendar_feeds()
        if not rows:
            return ("You don't have any calendar feeds set up - add one with add_ics_calendar_feed, "
                    "or connect Google Calendar with setup_google_calendar.")

        now = datetime.now()
        window_start = now - timedelta(hours=config.CALENDAR_SYNC_LOOKBACK_HOURS)
        window_end = now + timedelta(days=config.CALENDAR_SYNC_WINDOW_DAYS)

        total_inserted = total_updated = synced_count = 0
        problems = []
        for _id, name, kind, url, _last_synced in rows:
            if kind == "google":
                inserted, updated, error = _sync_google(window_start, window_end)
            elif kind == "ics":
                inserted, updated, error = _sync_ics(name, url, window_start, window_end)
            else:
                continue  # an unrecognized kind - shouldn't happen, but never crash a whole sync over it
            if error:
                problems.append(f"{name}: {error}")
                continue
            synced_count += 1
            total_inserted += inserted
            total_updated += updated
            try:
                db.mark_calendar_feed_synced(name, now.isoformat())
            except Exception:
                logger.exception(f"Couldn't mark feed '{name}' as synced")

        summary = (f"Synced {synced_count}/{len(rows)} feed(s): "
                  f"{total_inserted} new, {total_updated} updated event(s).")
        if problems:
            summary += " Problems - " + "; ".join(problems)
        return summary
