"""
tests/test_tier18_proactive_triggers.py - Tier 18d: fake-clock/mocked-
dependency coverage for the six proactive triggers automation/proactive.py
grew this tier (reminder follow-ups, morning briefing, idle/break, streak
milestones, habit detection, low disk) plus the centralized LLM-reword-
with-template-fallback (_reword()/_emit()) that now sits in front of every
trigger except reminder follow-ups (see ProactiveNudge.already_worded).

Deliberately split from tests/test_phase6_proactive.py (which owns the
original three: battery, calendar, download) and tests/
test_tier18_workflow_scheduler.py (which owns scheduled-workflow execution
and the DND/focus gate) - this file calls each trigger's own _check_*()
method directly rather than through check_once(), so each test only has to
think about ONE trigger's own dependencies (a mocked db/audit/SystemMonitor
call, a frozen clock where the trigger reads one), not every other trigger
that might also be wired into check_once()'s loop. The last class below is
the one exception - a cheap source-level check that check_once() really
does register every _check_* method this file otherwise tests in isolation.

    python -m unittest discover -s tests -p "test_tier18_proactive_triggers.py"
"""
import inspect
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

# Same reason every Phase 4+/Tier 18 test file stubs these before the first
# import of anything under brain/ or automation/ - automation.proactive's
# check_once() does a local `from automation.app_control import
# AppController` (its DND/focus gate), and app_control ultimately reaches
# pyautogui; _check_morning_briefing's local `from skills.daily_briefing
# import run_daily_briefing` reaches web.web_ops's hard `import pywhatkit`.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                        # noqa: E402
from automation.audit import ActionRecord, Audit, DbStore          # noqa: E402
from database.db_manager import db                                  # noqa: E402
from reminders.composer import ComposeResult                         # noqa: E402
from settings.config import config                                    # noqa: E402
import automation.proactive as proactive_mod                           # noqa: E402
from automation.proactive import ProactiveEngine, ProactiveNudge         # noqa: E402


def _frozen_datetime(fixed_now):
    """A mock.patch target for automation.proactive's own `datetime` name
    (that module does `from datetime import datetime, timedelta`, so the
    name IS the class - unlike tests/test_recent_actions_polish.py's
    _frozen_now, whose target module does a plain `import datetime` and so
    needs the longer `datetime.datetime` dotted path). Freezes .now() at
    `fixed_now` while .strptime/.fromisoformat/arithmetic/comparisons all
    keep working via normal subclassing - nothing else in this module
    touches `datetime` in a way that cares which exact subclass it gets."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now
    return mock.patch("automation.proactive.datetime", _Frozen)


def _record(tool="open_app", status="ok", ts=None, args=None, rec_id=1):
    return ActionRecord(id=rec_id, ts=ts or datetime.now().isoformat(), tool=tool, args=args or {}, status=status)


def _partitions(device="/dev/sda1", percent=50, free_gb=100.0, total_gb=200.0):
    return [{"device": device, "percent": percent, "used_gb": total_gb - free_gb,
            "total_gb": total_gb, "free_gb": free_gb}]


def _fake_cfg(model="fake-model"):
    return types.SimpleNamespace(model=model, configured=True)


def _fake_client(content):
    """A MagicMock standing in for the real groq/openai-shaped client -
    only client.chat.completions.create(...).choices[0].message.content is
    ever read by _reword(), so that's all this fakes."""
    client = mock.MagicMock()
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])
    client.chat.completions.create.return_value = response
    return client


class ProactiveTriggerTestBase(unittest.TestCase):
    """Tier 18's six new triggers (plus _reword) all live here, OFF by
    default - the mirror image of test_phase6_proactive.py's
    ProactiveTestBase, which turns these six off so ITS pre-Tier-18
    battery/calendar tests aren't polluted by them. Each test below turns
    on just the one flag it's exercising."""

    _FLAGS = (
        "PROACTIVE_BATTERY_ENABLED", "PROACTIVE_CALENDAR_ENABLED", "PROACTIVE_DOWNLOAD_WATCH_ENABLED",
        "PROACTIVE_WORKFLOW_SCHEDULE_ENABLED", "PROACTIVE_REMINDER_FOLLOWUPS_ENABLED",
        "PROACTIVE_MORNING_BRIEFING_ENABLED", "PROACTIVE_IDLE_BREAK_ENABLED",
        "PROACTIVE_STREAK_MILESTONES_ENABLED", "PROACTIVE_HABIT_DETECTION_ENABLED",
        "PROACTIVE_LOW_DISK_ENABLED", "PROACTIVE_LLM_REWORD_ENABLED",
    )

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

        self.old_audit = audit_mod.audit
        audit_mod.audit = Audit(store=DbStore(db), max_entries=200)
        proactive_mod.audit = audit_mod.audit  # the module imported `audit` by name directly

        self._saved = {name: getattr(config, name) for name in self._FLAGS}
        for name in self._FLAGS:
            setattr(config, name, False)

        def cleanup():
            db.db_path = self.old_db_path
            audit_mod.audit = self.old_audit
            proactive_mod.audit = self.old_audit
            for name, value in self._saved.items():
                setattr(config, name, value)
        self.addCleanup(cleanup)

        self.engine = ProactiveEngine(interval=9999)  # never used - tests call _check_*()/_reword()/_emit() directly


class ReminderFollowupTriggerTests(ProactiveTriggerTestBase):
    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = False
        with mock.patch.object(proactive_mod.db, "get_due_followups") as m:
            nudges = self.engine._check_reminder_followups()
        m.assert_not_called()
        self.assertEqual(nudges, [])

    def test_no_due_followups_is_a_noop(self):
        config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = True
        with mock.patch.object(proactive_mod.db, "get_due_followups", return_value=[]):
            nudges = self.engine._check_reminder_followups()
        self.assertEqual(nudges, [])

    def test_due_followups_compose_mark_sent_and_are_already_worded(self):
        config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = True
        delivered_at = (datetime.now() - timedelta(minutes=20)).isoformat()
        result = ComposeResult(text="It's been a bit - still planning to drink some water?",
                               source="template", latency_ms=0, category="health", level="gentle")
        with mock.patch.object(proactive_mod.db, "get_due_followups",
                               return_value=[(1, "drink water", delivered_at)]), \
             mock.patch.object(proactive_mod.reminder_composer, "start",
                               return_value=(lambda: result)) as mock_start, \
             mock.patch.object(proactive_mod.db, "mark_followup_sent", return_value=True) as mock_mark:
            nudges = self.engine._check_reminder_followups()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "reminder_followup")
        self.assertTrue(nudges[0].already_worded)
        self.assertEqual(nudges[0].message, result.text)
        mock_mark.assert_called_once_with(1)
        self.assertEqual(mock_start.call_args.kwargs.get("stage"), "followup")

    def test_get_due_followups_exception_is_handled(self):
        config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = True
        with mock.patch.object(proactive_mod.db, "get_due_followups", side_effect=Exception("boom")):
            nudges = self.engine._check_reminder_followups()
        self.assertEqual(nudges, [])

    def test_mark_followup_sent_exception_skips_just_that_one(self):
        config.PROACTIVE_REMINDER_FOLLOWUPS_ENABLED = True
        delivered_at = datetime.now().isoformat()
        ok_result = ComposeResult(text="second one's wording", source="template", latency_ms=0,
                                  category="misc", level="gentle")
        bad_result = ComposeResult(text="first one's wording", source="template", latency_ms=0,
                                   category="misc", level="gentle")

        def fake_start(message, stage="due", minutes_until=None, minutes_since=None, now=None):
            return (lambda: bad_result) if message == "first" else (lambda: ok_result)

        def fake_mark(reminder_id):
            if reminder_id == 1:
                raise Exception("db locked")
            return True

        with mock.patch.object(proactive_mod.db, "get_due_followups",
                               return_value=[(1, "first", delivered_at), (2, "second", delivered_at)]), \
             mock.patch.object(proactive_mod.reminder_composer, "start", side_effect=fake_start), \
             mock.patch.object(proactive_mod.db, "mark_followup_sent", side_effect=fake_mark):
            nudges = self.engine._check_reminder_followups()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].message, ok_result.text)


class MorningBriefingTriggerTests(ProactiveTriggerTestBase):
    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = False
        fixed_now = datetime(2026, 6, 15, 9, 0, 0)
        with _frozen_datetime(fixed_now):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(nudges, [])

    def test_already_fired_today_is_a_noop(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = True
        fixed_now = datetime(2026, 6, 15, 9, 0, 0)
        self.engine._morning_briefing_last_date = fixed_now.date()
        with _frozen_datetime(fixed_now):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(nudges, [])

    def test_before_target_time_does_not_fire(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = True
        config.PROACTIVE_MORNING_BRIEFING_TIME = "08:00"
        fixed_now = datetime(2026, 6, 15, 7, 59, 0)
        with _frozen_datetime(fixed_now):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(nudges, [])
        self.assertIsNone(self.engine._morning_briefing_last_date)

    def test_fires_at_target_time_and_records_the_date(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = True
        config.PROACTIVE_MORNING_BRIEFING_TIME = "08:00"
        fixed_now = datetime(2026, 6, 15, 8, 5, 0)
        with _frozen_datetime(fixed_now), \
             mock.patch("skills.daily_briefing.run_daily_briefing",
                       return_value="Good morning! Clear skies, no events, nothing overdue."):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "morning_briefing")
        self.assertIn("Good morning", nudges[0].message)
        self.assertEqual(self.engine._morning_briefing_last_date, fixed_now.date())

    def test_invalid_time_format_is_handled_without_crashing(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = True
        config.PROACTIVE_MORNING_BRIEFING_TIME = "not-a-time"
        fixed_now = datetime(2026, 6, 15, 9, 0, 0)
        with _frozen_datetime(fixed_now):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(nudges, [])
        self.assertIsNone(self.engine._morning_briefing_last_date)

    def test_run_daily_briefing_raising_still_marks_the_date_to_avoid_retry_storms(self):
        config.PROACTIVE_MORNING_BRIEFING_ENABLED = True
        config.PROACTIVE_MORNING_BRIEFING_TIME = "08:00"
        fixed_now = datetime(2026, 6, 15, 8, 5, 0)
        with _frozen_datetime(fixed_now), \
             mock.patch("skills.daily_briefing.run_daily_briefing", side_effect=Exception("no network")):
            nudges = self.engine._check_morning_briefing()
        self.assertEqual(nudges, [])
        self.assertEqual(self.engine._morning_briefing_last_date, fixed_now.date())


class IdleBreakTriggerTests(ProactiveTriggerTestBase):
    def setUp(self):
        super().setUp()
        config.PROACTIVE_IDLE_BREAK_ENABLED = True
        config.PROACTIVE_BREAK_AFTER_MIN = 90
        config.PROACTIVE_IDLE_RESET_GAP_MIN = 5

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_IDLE_BREAK_ENABLED = False
        with mock.patch.object(proactive_mod.audit, "recent") as m:
            nudges = self.engine._check_idle_break()
        m.assert_not_called()
        self.assertEqual(nudges, [])

    def test_empty_audit_resets_session_state(self):
        self.engine._active_session_start = datetime.now()
        self.engine._idle_break_nudged = True
        with mock.patch.object(proactive_mod.audit, "recent", return_value=[]):
            nudges = self.engine._check_idle_break()
        self.assertEqual(nudges, [])
        self.assertIsNone(self.engine._active_session_start)
        self.assertFalse(self.engine._idle_break_nudged)

    def test_latest_record_older_than_reset_gap_is_treated_as_gone_quiet(self):
        fixed_now = datetime(2026, 6, 15, 12, 0, 0)
        old = [_record(ts=(fixed_now - timedelta(minutes=10)).isoformat())]
        with _frozen_datetime(fixed_now), mock.patch.object(proactive_mod.audit, "recent", return_value=old):
            nudges = self.engine._check_idle_break()
        self.assertEqual(nudges, [])
        self.assertIsNone(self.engine._active_session_start)

    def test_continuous_activity_under_threshold_does_not_fire(self):
        fixed_now = datetime(2026, 6, 15, 12, 0, 0)
        # 1-minute gaps, well under the 5-minute reset gap - one unbroken 30-minute run.
        records = [_record(ts=(fixed_now - timedelta(minutes=i)).isoformat(), rec_id=i) for i in range(31)]
        with _frozen_datetime(fixed_now), mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            nudges = self.engine._check_idle_break()
        self.assertEqual(nudges, [])
        self.assertEqual(self.engine._active_session_start, fixed_now - timedelta(minutes=30))

    def test_crossing_threshold_fires_once_then_stays_quiet(self):
        fixed_now = datetime(2026, 6, 15, 12, 0, 0)
        records = [_record(ts=(fixed_now - timedelta(minutes=i)).isoformat(), rec_id=i) for i in range(96)]
        with _frozen_datetime(fixed_now), mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            first = self.engine._check_idle_break()
            second = self.engine._check_idle_break()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].kind, "idle_break")
        self.assertEqual(second, [])

    def test_a_gap_starts_a_fresh_session_and_rearms(self):
        t1 = datetime(2026, 6, 15, 12, 0, 0)
        long_run = [_record(ts=(t1 - timedelta(minutes=i)).isoformat(), rec_id=i) for i in range(96)]
        with _frozen_datetime(t1), mock.patch.object(proactive_mod.audit, "recent", return_value=long_run):
            first = self.engine._check_idle_break()
        self.assertEqual(len(first), 1)

        t2 = t1 + timedelta(minutes=20)  # well past the 5-minute reset gap - a real break happened
        fresh_run = [_record(ts=t2.isoformat(), rec_id=200)]
        with _frozen_datetime(t2), mock.patch.object(proactive_mod.audit, "recent", return_value=fresh_run):
            second = self.engine._check_idle_break()
        self.assertEqual(second, [])  # brand new, short session - nowhere near the threshold yet
        self.assertEqual(self.engine._active_session_start, t2)
        self.assertFalse(self.engine._idle_break_nudged)

    def test_audit_recent_exception_is_handled(self):
        with mock.patch.object(proactive_mod.audit, "recent", side_effect=Exception("boom")):
            nudges = self.engine._check_idle_break()
        self.assertEqual(nudges, [])


class StreakMilestoneTriggerTests(ProactiveTriggerTestBase):
    def setUp(self):
        super().setUp()
        config.PROACTIVE_STREAK_MILESTONES_ENABLED = True

    def _streaks(self, rows):
        return mock.patch.object(proactive_mod.db, "list_streaks", return_value=rows)

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_STREAK_MILESTONES_ENABLED = False
        with mock.patch.object(proactive_mod.db, "list_streaks") as m:
            nudges = self.engine._check_streak_milestones()
        m.assert_not_called()
        self.assertEqual(nudges, [])

    def test_no_streaks_is_a_noop(self):
        with self._streaks([]):
            nudges = self.engine._check_streak_milestones()
        self.assertEqual(nudges, [])

    def test_non_crossing_update_does_not_fire(self):
        with self._streaks([(1, "pushups", 5, 5, "2026-06-14")]):
            self.engine._check_streak_milestones()
        with self._streaks([(1, "pushups", 6, 6, "2026-06-15")]):
            nudges = self.engine._check_streak_milestones()
        self.assertEqual(nudges, [])

    def test_crossing_a_single_milestone_fires_once(self):
        with self._streaks([(1, "pushups", 6, 6, "2026-06-14")]):
            self.engine._check_streak_milestones()
        with self._streaks([(1, "pushups", 7, 7, "2026-06-15")]):
            nudges = self.engine._check_streak_milestones()
        self.assertEqual(len(nudges), 1)
        self.assertIn("7-day streak", nudges[0].message)
        self.assertEqual(self.engine._streak_last_seen["pushups"], 7)

    def test_jumping_several_milestones_at_once_fires_once_for_the_highest(self):
        with self._streaks([(1, "reading", 35, 35, "2026-06-15")]):
            nudges = self.engine._check_streak_milestones()
        self.assertEqual(len(nudges), 1)
        self.assertIn("30-day streak", nudges[0].message)

    def test_repeat_check_with_unchanged_current_is_a_noop(self):
        with self._streaks([(1, "pushups", 7, 7, "2026-06-15")]):
            first = self.engine._check_streak_milestones()
            second = self.engine._check_streak_milestones()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_resetting_to_one_then_climbing_back_rearms_the_milestone(self):
        with self._streaks([(1, "pushups", 7, 7, "2026-06-14")]):
            self.engine._check_streak_milestones()
        with self._streaks([(1, "pushups", 1, 7, "2026-06-15")]):
            reset_nudges = self.engine._check_streak_milestones()
        with self._streaks([(1, "pushups", 7, 7, "2026-06-22")]):
            second_climb = self.engine._check_streak_milestones()
        self.assertEqual(reset_nudges, [])
        self.assertEqual(len(second_climb), 1)
        self.assertIn("7-day streak", second_climb[0].message)

    def test_habit_removed_from_db_is_dropped_from_tracking(self):
        with self._streaks([(1, "pushups", 3, 3, "2026-06-14")]):
            self.engine._check_streak_milestones()
        self.assertIn("pushups", self.engine._streak_last_seen)
        with self._streaks([]):
            self.engine._check_streak_milestones()
        self.assertNotIn("pushups", self.engine._streak_last_seen)

    def test_list_streaks_exception_is_handled(self):
        with mock.patch.object(proactive_mod.db, "list_streaks", side_effect=Exception("boom")):
            nudges = self.engine._check_streak_milestones()
        self.assertEqual(nudges, [])


class HabitDetectionTriggerTests(ProactiveTriggerTestBase):
    def setUp(self):
        super().setUp()
        config.PROACTIVE_HABIT_DETECTION_ENABLED = True

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_HABIT_DETECTION_ENABLED = False
        with mock.patch.object(proactive_mod.audit, "recent") as m:
            nudges = self.engine._check_habit_detection()
        m.assert_not_called()
        self.assertEqual(nudges, [])

    def test_ticks_before_the_20th_skip_without_reading_audit(self):
        with mock.patch.object(proactive_mod.audit, "recent", return_value=[]) as m:
            for _ in range(19):
                nudges = self.engine._check_habit_detection()
                self.assertEqual(nudges, [])
            m.assert_not_called()
            self.engine._check_habit_detection()  # the 20th tick
            m.assert_called_once()

    def test_three_distinct_dates_same_tool_same_hour_is_suggested_once(self):
        records = [
            _record(tool="open_app", ts="2026-06-01T09:15:00", rec_id=1),
            _record(tool="open_app", ts="2026-06-02T09:05:00", rec_id=2),
            _record(tool="open_app", ts="2026-06-03T09:50:00", rec_id=3),
        ]
        self.engine._habit_tick_count = 19
        with mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            first = self.engine._check_habit_detection()
            self.engine._habit_tick_count = 39
            second = self.engine._check_habit_detection()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].kind, "habit")
        self.assertIn("9:00", first[0].message)
        self.assertEqual(second, [])  # same (tool, hour) pair - already suggested

    def test_excluded_tool_is_never_suggested(self):
        records = [
            _record(tool="list_reminders", ts="2026-06-01T09:15:00", rec_id=1),
            _record(tool="list_reminders", ts="2026-06-02T09:05:00", rec_id=2),
            _record(tool="list_reminders", ts="2026-06-03T09:50:00", rec_id=3),
        ]
        self.engine._habit_tick_count = 19
        with mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            nudges = self.engine._check_habit_detection()
        self.assertEqual(nudges, [])

    def test_non_ok_status_is_excluded(self):
        records = [
            _record(tool="open_app", status="failed", ts="2026-06-01T09:15:00", rec_id=1),
            _record(tool="open_app", status="failed", ts="2026-06-02T09:05:00", rec_id=2),
            _record(tool="open_app", status="failed", ts="2026-06-03T09:50:00", rec_id=3),
        ]
        self.engine._habit_tick_count = 19
        with mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            nudges = self.engine._check_habit_detection()
        self.assertEqual(nudges, [])

    def test_fewer_than_three_distinct_dates_is_not_suggested(self):
        records = [
            _record(tool="open_app", ts="2026-06-01T09:15:00", rec_id=1),
            _record(tool="open_app", ts="2026-06-02T09:05:00", rec_id=2),
        ]
        self.engine._habit_tick_count = 19
        with mock.patch.object(proactive_mod.audit, "recent", return_value=records):
            nudges = self.engine._check_habit_detection()
        self.assertEqual(nudges, [])

    def test_audit_recent_exception_is_handled(self):
        self.engine._habit_tick_count = 19
        with mock.patch.object(proactive_mod.audit, "recent", side_effect=Exception("boom")):
            nudges = self.engine._check_habit_detection()
        self.assertEqual(nudges, [])


class LowDiskTriggerTests(ProactiveTriggerTestBase):
    def setUp(self):
        super().setUp()
        config.PROACTIVE_LOW_DISK_ENABLED = True
        config.PROACTIVE_LOW_DISK_THRESHOLD = 90
        config.PROACTIVE_LOW_DISK_COOLDOWN_MIN = 60

    def _usage(self, partitions):
        return mock.patch.object(proactive_mod.SystemMonitor, "get_disk_partitions_usage", return_value=partitions)

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_LOW_DISK_ENABLED = False
        with mock.patch.object(proactive_mod.SystemMonitor, "get_disk_partitions_usage") as m:
            nudges = self.engine._check_low_disk()
        m.assert_not_called()
        self.assertEqual(nudges, [])

    def test_below_threshold_stays_armed_and_does_not_fire(self):
        with self._usage(_partitions(percent=50)):
            nudges = self.engine._check_low_disk()
        self.assertEqual(nudges, [])
        self.assertTrue(self.engine._low_disk_armed["/dev/sda1"])

    def test_at_or_above_threshold_fires_once(self):
        with self._usage(_partitions(percent=95, free_gb=5.0)):
            nudges = self.engine._check_low_disk()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "low_disk")
        self.assertIn("95", nudges[0].message)
        self.assertFalse(self.engine._low_disk_armed["/dev/sda1"])

    def test_repeated_high_while_unarmed_is_suppressed(self):
        with self._usage(_partitions(percent=95)):
            first = self.engine._check_low_disk()
            second = self.engine._check_low_disk()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_dropping_below_then_back_above_quickly_is_suppressed_by_cooldown(self):
        """The flapping scenario the cooldown exists for: unlike
        _check_battery (which clears its cooldown timer the moment it
        re-arms - see that method's own comment), _check_low_disk
        deliberately leaves _low_disk_last_nudge_at alone when a device
        drops back below threshold, so a quick dip-then-climb-again within
        the cooldown window still counts as "just told you", not a brand
        new episode. Doesn't need to mock time.monotonic() - real elapsed
        test time (microseconds) is trivially under any real cooldown."""
        with self._usage(_partitions(percent=95)):
            first = self.engine._check_low_disk()
        with self._usage(_partitions(percent=50)):
            self.engine._check_low_disk()  # dips below threshold - re-arms, cooldown timer untouched
        with self._usage(_partitions(percent=95)):
            third = self.engine._check_low_disk()  # straight back above, a moment later
        self.assertEqual(len(first), 1)
        self.assertEqual(third, [])

    def test_recovering_past_the_cooldown_window_fires_again(self):
        with self._usage(_partitions(percent=95)), \
             mock.patch.object(proactive_mod.time, "monotonic", return_value=1000.0):
            first = self.engine._check_low_disk()
        with self._usage(_partitions(percent=50)), \
             mock.patch.object(proactive_mod.time, "monotonic", return_value=1010.0):
            self.engine._check_low_disk()
        later = 1000.0 + config.PROACTIVE_LOW_DISK_COOLDOWN_MIN * 60 + 5
        with self._usage(_partitions(percent=95)), \
             mock.patch.object(proactive_mod.time, "monotonic", return_value=later):
            third = self.engine._check_low_disk()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(third), 1)

    def test_device_disappearing_cleans_up_both_tracking_dicts(self):
        with self._usage(_partitions(device="/dev/sdb1", percent=95)):
            self.engine._check_low_disk()
        self.assertIn("/dev/sdb1", self.engine._low_disk_armed)
        with self._usage([]):
            self.engine._check_low_disk()
        self.assertNotIn("/dev/sdb1", self.engine._low_disk_armed)
        self.assertNotIn("/dev/sdb1", self.engine._low_disk_last_nudge_at)

    def test_get_disk_partitions_usage_exception_is_handled(self):
        with mock.patch.object(proactive_mod.SystemMonitor, "get_disk_partitions_usage",
                               side_effect=Exception("boom")):
            nudges = self.engine._check_low_disk()
        self.assertEqual(nudges, [])


class CentralRewordTests(ProactiveTriggerTestBase):
    def setUp(self):
        super().setUp()
        config.PROACTIVE_LLM_REWORD_ENABLED = True
        config.PROACTIVE_LLM_REWORD_MAX_WORDS = 40

    def test_reword_disabled_returns_the_template_verbatim(self):
        config.PROACTIVE_LLM_REWORD_ENABLED = False
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                               message="Heads up - your battery's at 15% and not charging.")
        with mock.patch.object(proactive_mod, "_get_nudge_client_and_cfg") as m:
            result = self.engine._reword(nudge)
        m.assert_not_called()
        self.assertEqual(result, nudge.message)

    def test_no_provider_configured_returns_the_template_verbatim(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                               message="Heads up - your battery's at 15% and not charging.")
        with mock.patch.object(proactive_mod, "_get_nudge_client_and_cfg", return_value=(None, None)):
            result = self.engine._reword(nudge)
        self.assertEqual(result, nudge.message)

    def test_successful_reword_is_used(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                               message="Heads up - your battery's at 15% and not charging.")
        client = _fake_client("Your battery's down to 15 percent and isn't charging right now.")
        with mock.patch.object(proactive_mod, "_get_nudge_client_and_cfg",
                               return_value=(client, _fake_cfg())):
            result = self.engine._reword(nudge)
        self.assertEqual(result, "Your battery's down to 15 percent and isn't charging right now.")
        self.assertIn(result, self.engine._recent_reworded)

    def test_a_hallucinated_number_is_rejected_and_falls_back_to_the_template(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                               message="Heads up - your battery's at 15% and not charging.")
        client = _fake_client("Your battery's down to 50 percent, better plug in soon.")
        with mock.patch.object(proactive_mod, "_get_nudge_client_and_cfg",
                               return_value=(client, _fake_cfg())):
            result = self.engine._reword(nudge)
        self.assertEqual(result, nudge.message)

    def test_client_raising_an_exception_falls_back_to_the_template(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low",
                               message="Heads up - your battery's at 15% and not charging.")
        client = mock.MagicMock()
        client.chat.completions.create.side_effect = Exception("timed out")
        with mock.patch.object(proactive_mod, "_get_nudge_client_and_cfg",
                               return_value=(client, _fake_cfg())):
            result = self.engine._reword(nudge)
        self.assertEqual(result, nudge.message)

    def test_already_worded_nudges_skip_reword_entirely(self):
        nudge = ProactiveNudge(kind="reminder_followup", title="Reminder follow-up",
                               message="It's been a bit - still planning to drink some water?",
                               already_worded=True)
        received = []
        self.engine.add_listener(received.append)
        with mock.patch.object(self.engine, "_reword") as m:
            self.engine._emit(nudge)
        m.assert_not_called()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].message, nudge.message)

    def test_non_worded_nudges_go_through_reword(self):
        nudge = ProactiveNudge(kind="battery", title="Battery low", message="template text")
        with mock.patch.object(self.engine, "_reword", return_value="reworded text") as m:
            self.engine._emit(nudge)
        m.assert_called_once_with(nudge)
        self.assertEqual(nudge.message, "reworded text")


class CheckOnceRegistersAllTriggersTests(ProactiveTriggerTestBase):
    """A cheap regression guard that doesn't need to run check_once() at
    all (and so doesn't need to mock ten different dependencies): every
    _check_* method this class defines must appear exactly once in
    check_once()'s own registration tuple - easy to silently forget when a
    future tier adds an eleventh trigger."""

    def test_every_check_method_is_registered_exactly_once(self):
        check_methods = {name for name, _ in inspect.getmembers(ProactiveEngine, predicate=inspect.isfunction)
                         if name.startswith("_check_")}
        self.assertEqual(len(check_methods), 10)  # the roadmap's own "2 -> >= 10" Tier 18 target
        source = inspect.getsource(ProactiveEngine.check_once)
        for name in check_methods:
            self.assertEqual(source.count(f"self.{name}"), 1,
                             f"{name} should appear exactly once in check_once()'s registration tuple")


if __name__ == "__main__":
    unittest.main()
