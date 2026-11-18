"""
tests/test_tier18_calendar_sync.py - Tier 18b: calendar sync (Google OAuth +
ICS), automation/calendar_sync.py.

ICS parsing and Google-time parsing are pure functions - no mocking needed
at all. _sync_ics/_sync_google/CalendarSync's own methods are tested
against a tmp DB with the actual network/OAuth calls mocked out, via
mock.patch.object on this module's own names (_fetch_ics,
_GoogleCredentials, _build_google_service, ...) - same "force the HAS_X
flag and patch the library names, regardless of what's actually installed
in this environment" shape as tests/test_tier13_piper_download.py's own
HAS_PIPER tests (and why calendar_sync.py binds every Google name to None
on ImportError rather than leaving it undefined - see that file's own
comment: mock.patch.object requires the attribute to already exist).

    python -m unittest discover -s tests -p "test_tier18_calendar_sync.py"
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

from database.db_manager import db                                # noqa: E402
from settings.config import config                                  # noqa: E402
import automation.calendar_sync as cs                                 # noqa: E402
from automation.calendar_sync import CalendarSync                      # noqa: E402


class UnfoldIcsLinesTests(unittest.TestCase):
    def test_a_folded_continuation_line_is_joined(self):
        # RFC 5545 folding inserts exactly one whitespace character that is
        # NOT part of the content and must be dropped on unfolding - so a
        # fold that lands right at a real inter-word space needs TWO
        # spaces on the continuation line (the real one, then the fold's
        # own marker) for the space between "quarterly" and "budget" to
        # survive the round trip; see the RFC's own DESCRIPTION/"descrip"+
        # "tion" example for a fold landing mid-word (no space at all).
        text = "BEGIN:VEVENT\nSUMMARY:Team meeting about the quarterly\n  budget review\nEND:VEVENT"
        lines = cs._unfold_ics_lines(text)
        self.assertIn("SUMMARY:Team meeting about the quarterly budget review", lines)

    def test_a_fold_landing_mid_word_inserts_no_space(self):
        text = "BEGIN:VEVENT\nDESCRIPTION:This is a long descrip\n tion that exists on a long line.\nEND:VEVENT"
        lines = cs._unfold_ics_lines(text)
        self.assertIn("DESCRIPTION:This is a long description that exists on a long line.", lines)

    def test_blank_lines_are_dropped(self):
        text = "BEGIN:VEVENT\n\nSUMMARY:X\n\nEND:VEVENT"
        self.assertEqual(cs._unfold_ics_lines(text), ["BEGIN:VEVENT", "SUMMARY:X", "END:VEVENT"])

    def test_handles_crlf_and_bare_cr(self):
        self.assertEqual(cs._unfold_ics_lines("BEGIN:VEVENT\r\nSUMMARY:X\r\nEND:VEVENT"),
                         ["BEGIN:VEVENT", "SUMMARY:X", "END:VEVENT"])
        self.assertEqual(cs._unfold_ics_lines("BEGIN:VEVENT\rSUMMARY:X\rEND:VEVENT"),
                         ["BEGIN:VEVENT", "SUMMARY:X", "END:VEVENT"])


class ParseIcsLineTests(unittest.TestCase):
    def test_simple_name_value(self):
        self.assertEqual(cs._parse_ics_line("SUMMARY:Team sync"), ("SUMMARY", {}, "Team sync"))

    def test_params_are_parsed(self):
        name, params, value = cs._parse_ics_line("DTSTART;TZID=America/New_York:20260615T090000")
        self.assertEqual(name, "DTSTART")
        self.assertEqual(params, {"TZID": "America/New_York"})
        self.assertEqual(value, "20260615T090000")

    def test_value_date_param(self):
        _name, params, _value = cs._parse_ics_line("DTSTART;VALUE=DATE:20260615")
        self.assertEqual(params, {"VALUE": "DATE"})

    def test_no_colon_is_unparseable(self):
        self.assertEqual(cs._parse_ics_line("NOCOLONATALL"), (None, {}, ""))

    def test_empty_name_is_unparseable(self):
        self.assertEqual(cs._parse_ics_line(":value with no name"), (None, {}, ""))


class UnescapeIcsTextTests(unittest.TestCase):
    def test_escaped_comma_semicolon_backslash_and_newline(self):
        self.assertEqual(cs._unescape_ics_text("Team\\, sync\\; weekly\\nat 9am"), "Team, sync; weekly\nat 9am")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(cs._unescape_ics_text("Plain text"), "Plain text")

    def test_does_not_double_unescape_an_escaped_backslash(self):
        # ICS source bytes: \\n (an escaped backslash, then a literal 'n') -
        # must unescape to a literal backslash followed by 'n', NOT a newline.
        self.assertEqual(cs._unescape_ics_text("\\\\n"), "\\n")


class ParseIcsDatetimeTests(unittest.TestCase):
    def test_all_day_value_date(self):
        self.assertEqual(cs._parse_ics_datetime("20260615", {"VALUE": "DATE"}),
                         datetime(2026, 6, 15, 0, 0, 0).isoformat())

    def test_bare_8_digit_value_is_treated_as_all_day_even_without_the_param(self):
        self.assertEqual(cs._parse_ics_datetime("20260615", {}), datetime(2026, 6, 15, 0, 0, 0).isoformat())

    def test_utc_z_suffix_is_converted_to_local(self):
        expected = datetime(2026, 6, 15, 13, 0, 0).replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(cs._parse_ics_datetime("20260615T130000Z", {}), expected.isoformat())

    def test_floating_time_with_no_tzid_is_taken_as_already_local(self):
        self.assertEqual(cs._parse_ics_datetime("20260615T090000", {}), datetime(2026, 6, 15, 9, 0, 0).isoformat())

    def test_tzid_qualified_time_is_converted_to_local(self):
        if not cs.HAS_ZONEINFO:
            self.skipTest("zoneinfo not available")
        from zoneinfo import ZoneInfo
        expected = (datetime(2026, 6, 15, 9, 0, 0).replace(tzinfo=ZoneInfo("America/New_York"))
                   .astimezone().replace(tzinfo=None))
        self.assertEqual(cs._parse_ics_datetime("20260615T090000", {"TZID": "America/New_York"}),
                         expected.isoformat())

    def test_unrecognized_tzid_falls_back_to_treating_the_value_as_local(self):
        self.assertEqual(cs._parse_ics_datetime("20260615T090000", {"TZID": "Not/ARealZone"}),
                         datetime(2026, 6, 15, 9, 0, 0).isoformat())

    def test_garbage_value_returns_none(self):
        self.assertIsNone(cs._parse_ics_datetime("not-a-date", {}))

    def test_invalid_calendar_date_returns_none(self):
        self.assertIsNone(cs._parse_ics_datetime("99999999", {"VALUE": "DATE"}))


class SimplifyRruleTests(unittest.TestCase):
    def test_plain_daily(self):
        self.assertEqual(cs._simplify_rrule("FREQ=DAILY"), "daily")

    def test_plain_weekly(self):
        self.assertEqual(cs._simplify_rrule("FREQ=WEEKLY"), "weekly")

    def test_daily_with_count_is_not_simplified(self):
        self.assertIsNone(cs._simplify_rrule("FREQ=DAILY;COUNT=10"))

    def test_weekly_with_until_is_not_simplified(self):
        self.assertIsNone(cs._simplify_rrule("FREQ=WEEKLY;UNTIL=20261231T000000Z"))

    def test_interval_excludes_even_when_explicitly_one(self):
        self.assertIsNone(cs._simplify_rrule("FREQ=DAILY;INTERVAL=1"))

    def test_monthly_or_yearly_is_not_simplified(self):
        self.assertIsNone(cs._simplify_rrule("FREQ=MONTHLY"))
        self.assertIsNone(cs._simplify_rrule("FREQ=YEARLY"))

    def test_byday_alone_with_weekly_is_still_simplified(self):
        """Only COUNT/UNTIL/INTERVAL are excluded - see _simplify_rrule's
        own docstring for why BYDAY isn't a reason to give up on 'weekly'."""
        self.assertEqual(cs._simplify_rrule("FREQ=WEEKLY;BYDAY=MO"), "weekly")


class ParseVeventsTests(unittest.TestCase):
    def test_a_simple_single_event(self):
        text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:abc-123\nSUMMARY:Team sync\n"
                "DTSTART:20260615T090000\nDTEND:20260615T100000\nLOCATION:Conference Room A\n"
                "END:VEVENT\nEND:VCALENDAR\n")
        events = cs._parse_vevents(text)
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertEqual(ev["uid"], "abc-123")
        self.assertEqual(ev["summary"], "Team sync")
        self.assertEqual(ev["start_iso"], datetime(2026, 6, 15, 9, 0, 0).isoformat())
        self.assertEqual(ev["end_iso"], datetime(2026, 6, 15, 10, 0, 0).isoformat())
        self.assertEqual(ev["location"], "Conference Room A")

    def test_multiple_events(self):
        text = ("BEGIN:VEVENT\nUID:1\nSUMMARY:One\nDTSTART:20260615T090000\nEND:VEVENT\n"
                "BEGIN:VEVENT\nUID:2\nSUMMARY:Two\nDTSTART:20260616T090000\nEND:VEVENT\n")
        self.assertEqual([e["uid"] for e in cs._parse_vevents(text)], ["1", "2"])

    def test_event_missing_uid_is_skipped(self):
        text = "BEGIN:VEVENT\nSUMMARY:No UID\nDTSTART:20260615T090000\nEND:VEVENT\n"
        self.assertEqual(cs._parse_vevents(text), [])

    def test_event_missing_dtstart_is_skipped(self):
        text = "BEGIN:VEVENT\nUID:1\nSUMMARY:No start\nEND:VEVENT\n"
        self.assertEqual(cs._parse_vevents(text), [])

    def test_event_with_unparseable_dtstart_is_skipped(self):
        text = "BEGIN:VEVENT\nUID:1\nSUMMARY:Bad start\nDTSTART:not-a-date\nEND:VEVENT\n"
        self.assertEqual(cs._parse_vevents(text), [])

    def test_recurrence_rrule_is_captured(self):
        text = "BEGIN:VEVENT\nUID:1\nSUMMARY:Standup\nDTSTART:20260615T090000\nRRULE:FREQ=DAILY\nEND:VEVENT\n"
        self.assertEqual(cs._parse_vevents(text)[0]["recurrence"], "daily")

    def test_properties_outside_a_vevent_block_are_ignored(self):
        text = "SUMMARY:Not in an event\nBEGIN:VEVENT\nUID:1\nDTSTART:20260615T090000\nEND:VEVENT\n"
        events = cs._parse_vevents(text)
        self.assertEqual(len(events), 1)
        self.assertNotIn("summary", events[0])


class ParseGoogleTimeTests(unittest.TestCase):
    def test_all_day(self):
        self.assertEqual(cs._parse_google_time({"date": "2026-06-15"}), datetime(2026, 6, 15, 0, 0, 0).isoformat())

    def test_timed_with_explicit_offset(self):
        expected = datetime.fromisoformat("2026-06-15T09:00:00-04:00").astimezone().replace(tzinfo=None)
        self.assertEqual(cs._parse_google_time({"dateTime": "2026-06-15T09:00:00-04:00"}), expected.isoformat())

    def test_timed_with_z_suffix(self):
        expected = datetime.fromisoformat("2026-06-15T13:00:00+00:00").astimezone().replace(tzinfo=None)
        self.assertEqual(cs._parse_google_time({"dateTime": "2026-06-15T13:00:00Z"}), expected.isoformat())

    def test_missing_info_is_none(self):
        self.assertIsNone(cs._parse_google_time({}))
        self.assertIsNone(cs._parse_google_time(None))

    def test_garbage_date_time_is_none(self):
        self.assertIsNone(cs._parse_google_time({"dateTime": "not-a-time"}))


class InWindowTests(unittest.TestCase):
    def test_inside_window_is_true(self):
        now = datetime.now()
        self.assertTrue(cs._in_window((now + timedelta(hours=1)).isoformat(), now, now + timedelta(days=1)))

    def test_outside_window_is_false(self):
        now = datetime.now()
        self.assertFalse(cs._in_window((now + timedelta(days=2)).isoformat(), now, now + timedelta(days=1)))

    def test_unparseable_is_false(self):
        now = datetime.now()
        self.assertFalse(cs._in_window("garbage", now, now + timedelta(days=1)))


class FormatFeedLineTests(unittest.TestCase):
    def test_ics_feed_never_synced(self):
        line = cs._format_feed_line((1, "work", "ics", "http://x", None))
        self.assertIn("work", line)
        self.assertIn("ICS", line)
        self.assertIn("never synced", line)

    def test_google_feed_with_last_synced(self):
        line = cs._format_feed_line((2, "google", "google", None, "2026-06-15T08:00:00"))
        self.assertIn("Google", line)
        self.assertIn("2026-06-15T08:00:00", line)


class CalendarSyncDbTestBase(unittest.TestCase):
    """Every DB-backed test below shares a tmp DB plus a known, restorable
    sync-window config - same shape as every other Tier 18 test base
    (tests/test_tier18_workflow_scheduler.py's WorkflowScheduleTestBase,
    tests/test_tier18_proactive_triggers.py's ProactiveTriggerTestBase)."""

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.addCleanup(lambda: setattr(db, "db_path", self.old_db_path))

        self._cfg_saved = (config.CALENDAR_SYNC_WINDOW_DAYS, config.CALENDAR_SYNC_LOOKBACK_HOURS)
        config.CALENDAR_SYNC_WINDOW_DAYS = 60
        config.CALENDAR_SYNC_LOOKBACK_HOURS = 6

        def _restore_cfg():
            config.CALENDAR_SYNC_WINDOW_DAYS, config.CALENDAR_SYNC_LOOKBACK_HOURS = self._cfg_saved
        self.addCleanup(_restore_cfg)


class SyncIcsTests(CalendarSyncDbTestBase):
    def test_fetch_failure_is_reported_without_crashing(self):
        now = datetime.now()
        with mock.patch.object(cs, "_fetch_ics", side_effect=Exception("connection refused")):
            inserted, updated, error = cs._sync_ics("work", "http://x/feed.ics",
                                                     now - timedelta(hours=1), now + timedelta(days=60))
        self.assertEqual((inserted, updated), (0, 0))
        self.assertIn("connection refused", error)

    def test_events_inside_the_window_are_upserted(self):
        now = datetime.now()
        start = (now + timedelta(days=1)).strftime("%Y%m%dT%H%M%S")
        ics_text = f"BEGIN:VEVENT\nUID:evt-1\nSUMMARY:Soon\nDTSTART:{start}\nEND:VEVENT\n"
        with mock.patch.object(cs, "_fetch_ics", return_value=ics_text):
            inserted, updated, error = cs._sync_ics("work", "http://x/feed.ics",
                                                     now - timedelta(hours=1), now + timedelta(days=60))
        self.assertEqual(error, "")
        self.assertEqual((inserted, updated), (1, 0))
        rows = db.get_calendar_events_between(now.isoformat(), (now + timedelta(days=60)).isoformat())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Soon")

    def test_events_outside_the_window_are_skipped(self):
        now = datetime.now()
        start = (now - timedelta(days=400)).strftime("%Y%m%dT%H%M%S")
        ics_text = f"BEGIN:VEVENT\nUID:evt-old\nSUMMARY:Ancient\nDTSTART:{start}\nEND:VEVENT\n"
        with mock.patch.object(cs, "_fetch_ics", return_value=ics_text):
            inserted, updated, _error = cs._sync_ics("work", "http://x/feed.ics",
                                                      now - timedelta(hours=1), now + timedelta(days=60))
        self.assertEqual((inserted, updated), (0, 0))

    def test_resyncing_the_same_event_updates_not_duplicates(self):
        now = datetime.now()
        start = (now + timedelta(days=1)).strftime("%Y%m%dT%H%M%S")
        window = (now - timedelta(hours=1), now + timedelta(days=60))
        ics_v1 = f"BEGIN:VEVENT\nUID:evt-2\nSUMMARY:Original title\nDTSTART:{start}\nEND:VEVENT\n"
        ics_v2 = f"BEGIN:VEVENT\nUID:evt-2\nSUMMARY:Renamed title\nDTSTART:{start}\nEND:VEVENT\n"
        with mock.patch.object(cs, "_fetch_ics", return_value=ics_v1):
            cs._sync_ics("work", "http://x/feed.ics", *window)
        with mock.patch.object(cs, "_fetch_ics", return_value=ics_v2):
            inserted, updated, _error = cs._sync_ics("work", "http://x/feed.ics", *window)
        self.assertEqual((inserted, updated), (0, 1))
        rows = db.get_calendar_events_between(now.isoformat(), (now + timedelta(days=60)).isoformat())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Renamed title")


class SyncGoogleTests(CalendarSyncDbTestBase):
    def test_not_installed_is_reported(self):
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", False):
            inserted, updated, error = cs._sync_google(datetime.now(), datetime.now() + timedelta(days=1))
        self.assertEqual((inserted, updated), (0, 0))
        self.assertIn("google-auth-oauthlib", error)

    def test_not_connected_yet_is_reported(self):
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_get_google_credentials", return_value=None):
            inserted, updated, error = cs._sync_google(datetime.now(), datetime.now() + timedelta(days=1))
        self.assertEqual((inserted, updated), (0, 0))
        self.assertIn("setup_google_calendar", error)

    def test_successful_sync_upserts_each_item(self):
        now = datetime.now()
        fake_service = mock.MagicMock()
        fake_service.events.return_value.list.return_value.execute.return_value = {
            "items": [
                {"id": "g1", "summary": "Standup", "start": {"dateTime": (now + timedelta(hours=1)).isoformat()},
                 "end": {"dateTime": (now + timedelta(hours=2)).isoformat()}, "location": "Zoom"},
                {"id": "g2", "summary": "All day thing",
                 "start": {"date": (now + timedelta(days=2)).strftime("%Y-%m-%d")},
                 "end": {"date": (now + timedelta(days=3)).strftime("%Y-%m-%d")}},
            ]
        }
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_get_google_credentials", return_value=mock.MagicMock()), \
             mock.patch.object(cs, "_build_google_service", return_value=fake_service):
            inserted, updated, error = cs._sync_google(now - timedelta(hours=1), now + timedelta(days=60))
        self.assertEqual(error, "")
        self.assertEqual((inserted, updated), (2, 0))

    def test_item_missing_id_or_start_is_skipped(self):
        fake_service = mock.MagicMock()
        fake_service.events.return_value.list.return_value.execute.return_value = {
            "items": [{"summary": "No id or start"}, {"id": "g3"}]
        }
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_get_google_credentials", return_value=mock.MagicMock()), \
             mock.patch.object(cs, "_build_google_service", return_value=fake_service):
            inserted, updated, error = cs._sync_google(datetime.now(), datetime.now() + timedelta(days=1))
        self.assertEqual((inserted, updated), (0, 0))
        self.assertEqual(error, "")

    def test_api_call_raising_is_reported(self):
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_get_google_credentials", return_value=mock.MagicMock()), \
             mock.patch.object(cs, "_build_google_service", side_effect=Exception("401 unauthorized")):
            inserted, updated, error = cs._sync_google(datetime.now(), datetime.now() + timedelta(days=1))
        self.assertEqual((inserted, updated), (0, 0))
        self.assertIn("401", error)


class GetGoogleCredentialsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.token_path = os.path.join(self.tmp, "token.json")
        self.old_token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
        config.GOOGLE_CALENDAR_TOKEN_PATH = self.token_path
        self.addCleanup(lambda: setattr(config, "GOOGLE_CALENDAR_TOKEN_PATH", self.old_token_path))

    def test_no_token_file_returns_none(self):
        self.assertIsNone(cs._get_google_credentials())

    def test_valid_unexpired_token_is_returned_as_is(self):
        with open(self.token_path, "w") as fh:
            fh.write("{}")
        fake_creds = mock.MagicMock(expired=False)
        with mock.patch.object(cs, "_GoogleCredentials") as m_creds_cls:
            m_creds_cls.from_authorized_user_file.return_value = fake_creds
            result = cs._get_google_credentials()
        self.assertIs(result, fake_creds)
        fake_creds.refresh.assert_not_called()

    def test_expired_token_is_refreshed_and_rewritten(self):
        with open(self.token_path, "w") as fh:
            fh.write("{}")
        fake_creds = mock.MagicMock(expired=True, refresh_token="rt")
        fake_creds.to_json.return_value = '{"refreshed": true}'
        with mock.patch.object(cs, "_GoogleCredentials") as m_creds_cls, \
             mock.patch.object(cs, "_GoogleAuthRequest"):
            m_creds_cls.from_authorized_user_file.return_value = fake_creds
            result = cs._get_google_credentials()
        self.assertIs(result, fake_creds)
        fake_creds.refresh.assert_called_once()
        with open(self.token_path) as fh:
            self.assertIn("refreshed", fh.read())

    def test_refresh_failure_returns_none(self):
        with open(self.token_path, "w") as fh:
            fh.write("{}")
        fake_creds = mock.MagicMock(expired=True, refresh_token="rt")
        fake_creds.refresh.side_effect = Exception("revoked")
        with mock.patch.object(cs, "_GoogleCredentials") as m_creds_cls, \
             mock.patch.object(cs, "_GoogleAuthRequest"):
            m_creds_cls.from_authorized_user_file.return_value = fake_creds
            result = cs._get_google_credentials()
        self.assertIsNone(result)

    def test_unreadable_token_file_returns_none(self):
        with open(self.token_path, "w") as fh:
            fh.write("{}")
        with mock.patch.object(cs, "_GoogleCredentials") as m_creds_cls:
            m_creds_cls.from_authorized_user_file.side_effect = Exception("corrupt json")
            result = cs._get_google_credentials()
        self.assertIsNone(result)


class AddIcsFeedTests(CalendarSyncDbTestBase):
    def test_empty_name_is_rejected(self):
        self.assertIn("name", CalendarSync.add_ics_feed("", "http://x/feed.ics").lower())

    def test_reserved_google_name_is_rejected(self):
        result = CalendarSync.add_ics_feed("google", "http://x/feed.ics")
        self.assertIn("reserved", result.lower())
        self.assertIsNone(db.get_calendar_feed("google"))

    def test_empty_url_is_rejected(self):
        self.assertIn("url", CalendarSync.add_ics_feed("work", "").lower())

    def test_successful_add(self):
        result = CalendarSync.add_ics_feed("Work", "http://x/feed.ics")
        self.assertIn("work", result.lower())
        row = db.get_calendar_feed("work")
        self.assertIsNotNone(row)
        self.assertEqual(row[2], "ics")
        self.assertEqual(row[3], "http://x/feed.ics")


class RemoveFeedTests(CalendarSyncDbTestBase):
    def test_not_found(self):
        self.assertIn("don't have", CalendarSync.remove_feed("nope").lower())

    def test_successful_remove(self):
        db.save_calendar_feed("work", "ics", "http://x/feed.ics")
        result = CalendarSync.remove_feed("work")
        self.assertIn("removed", result.lower())
        self.assertIsNone(db.get_calendar_feed("work"))

    def test_removing_google_also_deletes_the_cached_token_file(self):
        token_path = os.path.join(self.tmp, "token.json")
        with open(token_path, "w") as fh:
            fh.write("{}")
        old_token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
        config.GOOGLE_CALENDAR_TOKEN_PATH = token_path
        self.addCleanup(lambda: setattr(config, "GOOGLE_CALENDAR_TOKEN_PATH", old_token_path))
        db.save_calendar_feed("google", "google", None)

        CalendarSync.remove_feed("google")
        self.assertFalse(os.path.isfile(token_path))

    def test_removing_an_ics_feed_does_not_touch_the_google_token_file(self):
        token_path = os.path.join(self.tmp, "token.json")
        with open(token_path, "w") as fh:
            fh.write("{}")
        old_token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
        config.GOOGLE_CALENDAR_TOKEN_PATH = token_path
        self.addCleanup(lambda: setattr(config, "GOOGLE_CALENDAR_TOKEN_PATH", old_token_path))
        db.save_calendar_feed("work", "ics", "http://x/feed.ics")

        CalendarSync.remove_feed("work")
        self.assertTrue(os.path.isfile(token_path))


class ListFeedsTests(CalendarSyncDbTestBase):
    def test_empty(self):
        self.assertIn("add_ics_calendar_feed", CalendarSync.list_feeds())

    def test_with_rows(self):
        db.save_calendar_feed("work", "ics", "http://x/feed.ics")
        result = CalendarSync.list_feeds()
        self.assertIn("work", result)
        self.assertIn("ICS", result)


class SetupGoogleTests(CalendarSyncDbTestBase):
    def setUp(self):
        super().setUp()
        self.creds_path = os.path.join(self.tmp, "credentials.json")
        self.token_path = os.path.join(self.tmp, "token.json")
        self.old_creds_path = config.GOOGLE_CALENDAR_CREDENTIALS_PATH
        self.old_token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
        config.GOOGLE_CALENDAR_CREDENTIALS_PATH = self.creds_path
        config.GOOGLE_CALENDAR_TOKEN_PATH = self.token_path

        def _restore():
            config.GOOGLE_CALENDAR_CREDENTIALS_PATH = self.old_creds_path
            config.GOOGLE_CALENDAR_TOKEN_PATH = self.old_token_path
        self.addCleanup(_restore)

    def test_not_installed(self):
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", False):
            result = CalendarSync.setup_google()
        self.assertIn("google-auth-oauthlib", result)

    def test_missing_credentials_file(self):
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True):
            result = CalendarSync.setup_google()
        self.assertIn("credentials", result.lower())
        self.assertIn(self.creds_path, result)

    def test_successful_flow_saves_token_and_registers_feed(self):
        with open(self.creds_path, "w") as fh:
            fh.write("{}")
        fake_creds = mock.MagicMock()
        fake_creds.to_json.return_value = '{"token": "abc"}'
        fake_flow = mock.MagicMock()
        fake_flow.run_local_server.return_value = fake_creds
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_GoogleInstalledAppFlow") as m_flow_cls:
            m_flow_cls.from_client_secrets_file.return_value = fake_flow
            result = CalendarSync.setup_google()
        self.assertIn("connected", result.lower())
        with open(self.token_path) as fh:
            self.assertIn("abc", fh.read())
        self.assertIsNotNone(db.get_calendar_feed("google"))

    def test_flow_raising_is_reported(self):
        with open(self.creds_path, "w") as fh:
            fh.write("{}")
        with mock.patch.object(cs, "HAS_GOOGLE_CALENDAR", True), \
             mock.patch.object(cs, "_GoogleInstalledAppFlow") as m_flow_cls:
            m_flow_cls.from_client_secrets_file.side_effect = Exception("bad client secret")
            result = CalendarSync.setup_google()
        self.assertIn("bad client secret", result)
        self.assertIsNone(db.get_calendar_feed("google"))


class SyncAllTests(CalendarSyncDbTestBase):
    def test_no_feeds(self):
        self.assertIn("add_ics_calendar_feed", CalendarSync.sync_all())

    def test_syncs_every_feed_and_marks_them_synced(self):
        db.save_calendar_feed("work", "ics", "http://x/feed.ics")
        now = datetime.now()
        start = (now + timedelta(days=1)).strftime("%Y%m%dT%H%M%S")
        ics_text = f"BEGIN:VEVENT\nUID:e1\nSUMMARY:Thing\nDTSTART:{start}\nEND:VEVENT\n"
        with mock.patch.object(cs, "_fetch_ics", return_value=ics_text):
            result = CalendarSync.sync_all()
        self.assertIn("1/1", result)
        self.assertIn("1 new", result)
        row = db.get_calendar_feed("work")
        self.assertIsNotNone(row[4])  # last_synced_at now set

    def test_a_failing_feed_does_not_stop_the_others(self):
        db.save_calendar_feed("bad", "ics", "http://bad/feed.ics")
        db.save_calendar_feed("good", "ics", "http://good/feed.ics")
        now = datetime.now()
        start = (now + timedelta(days=1)).strftime("%Y%m%dT%H%M%S")
        good_text = f"BEGIN:VEVENT\nUID:e1\nSUMMARY:Thing\nDTSTART:{start}\nEND:VEVENT\n"

        def fake_fetch(url):
            if url == "http://bad/feed.ics":
                raise Exception("timed out")
            return good_text

        with mock.patch.object(cs, "_fetch_ics", side_effect=fake_fetch):
            result = CalendarSync.sync_all()
        self.assertIn("1/2", result)
        self.assertIn("timed out", result)
        self.assertIsNone(db.get_calendar_feed("bad")[4])
        self.assertIsNotNone(db.get_calendar_feed("good")[4])

    def test_unrecognized_feed_kind_is_skipped_without_crashing(self):
        db.save_calendar_feed("weird", "carrier-pigeon", None)
        self.assertIn("0/1", CalendarSync.sync_all())


if __name__ == "__main__":
    unittest.main()
