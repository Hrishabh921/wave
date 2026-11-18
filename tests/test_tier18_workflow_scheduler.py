"""
tests/test_tier18_workflow_scheduler.py — Tier 18: time-triggered workflows.

Covers three layers, same split as the modules themselves:
  1. database/db_manager.py's workflow_schedules storage methods (exercised
     indirectly through WorkflowScheduler below, same style
     tests/test_phase6_calendar.py already uses for calendar_events).
  2. automation/workflow_scheduler.py's WorkflowScheduler - the actual
     parsing/catch-up LOGIC, with dateparser's own parse step mocked out
     (same "_parse_point mocked, only OUR logic under test" pattern
     test_phase6_calendar.py's ParsingFreeTests class uses) so these pass
     identically whether or not the real `dateparser` package happens to
     be installed.
  3. automation/proactive.py's new _check_scheduled_workflows trigger, its
     set_workflow_runner hook, and the DND/focus gate now sitting at the
     top of check_once() - same ProactiveTestBase pattern
     tests/test_phase6_proactive.py already uses.

Also has ONE explicit test aimed squarely at the roadmap's own Tier 18
"Done when" bar - "scheduled workflows survive a restart" - by scheduling
through one WorkflowScheduler call and then detecting it as due through a
BRAND NEW ProactiveEngine instance that never saw the schedule created,
proving due-ness is tracked entirely in the (persisted) database, not in
any engine instance's RAM.

Run: python -m unittest discover -s tests -p "test_tier18_workflow_scheduler.py"
(pytest will also collect it.)
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                      # noqa: E402
from automation.audit import Audit, DbStore                      # noqa: E402
from database.db_manager import db                                # noqa: E402
from settings.config import config                                 # noqa: E402
import automation.workflow_scheduler as wfs                         # noqa: E402
from automation.workflow_scheduler import WorkflowScheduler          # noqa: E402
import automation.proactive as proactive_mod                          # noqa: E402
from automation.proactive import ProactiveEngine, ProactiveNudge       # noqa: E402
import automation.app_control as app_control_mod                        # noqa: E402
from brain.handlers import automation as automation_handlers             # noqa: E402


class WorkflowScheduleTestBase(unittest.TestCase):
    """Same temp-DB-file pattern as test_phase6_calendar.py's
    CalendarTestBase - a real sqlite file per test, not a mock, so the
    migrations/table creation in database/db_manager.py's init_db() are
    exercised for real."""

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        db.save_workflow("good morning", ["open spotify", "what is the weather today"])

        # automation/app_control.py's _active_mode is plain module state,
        # not reset between tests on its own - save/restore it so a mode
        # left on by some other test (or by one of OUR OWN DND-gate tests
        # below) can never leak into another test's result.
        self.old_mode = dict(app_control_mod._active_mode)

        def cleanup():
            db.db_path = self.old_db_path
            app_control_mod._active_mode.clear()
            app_control_mod._active_mode.update(self.old_mode)
        self.addCleanup(cleanup)


class ScheduleWorkflowTests(WorkflowScheduleTestBase):
    def test_rejects_blank_name(self):
        result = WorkflowScheduler.schedule_workflow("", "7am", "daily")
        self.assertIn("which workflow", result.lower())

    def test_rejects_an_unknown_workflow(self):
        with mock.patch.object(wfs, "_parse_point", return_value=datetime(2026, 11, 7, 7, 0, 0)):
            result = WorkflowScheduler.schedule_workflow("nonexistent", "7am", "daily")
        self.assertIn("don't have a workflow", result.lower())
        self.assertEqual(db.list_workflow_schedules(), [])

    def test_reports_unparseable_time_clearly(self):
        with mock.patch.object(wfs, "_parse_point", return_value=None), \
             mock.patch.object(wfs, "HAS_DATEPARSER", True):
            result = WorkflowScheduler.schedule_workflow("good morning", "whenever o'clock", "once")
        self.assertNotIn("Scheduled", result)
        self.assertIn("couldn't figure out", result.lower())

    def test_reports_missing_dateparser_package(self):
        with mock.patch.object(wfs, "HAS_DATEPARSER", False):
            result = WorkflowScheduler.schedule_workflow("good morning", "7am", "once")
        self.assertIn("dateparser", result.lower())

    def test_successful_once_schedule_is_stored(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            result = WorkflowScheduler.schedule_workflow("Good Morning", "7am", "once")
        self.assertIn("Scheduled", result)
        self.assertIn("good morning", result.lower())
        rows = db.list_workflow_schedules()
        self.assertEqual(len(rows), 1)
        _id, name, time_text, repeat, next_run_at, enabled, _last = rows[0]
        self.assertEqual(name, "good morning")   # normalized, same as save_workflow itself
        self.assertEqual(repeat, "once")
        self.assertEqual(next_run_at, when.isoformat())
        self.assertEqual(enabled, 1)

    def test_daily_and_weekly_repeat_are_stored_verbatim(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "daily")
            WorkflowScheduler.schedule_workflow("good morning", "monday 7am", "weekly")
        repeats = sorted(r[3] for r in db.list_workflow_schedules())
        self.assertEqual(repeats, ["daily", "weekly"])

    def test_invalid_repeat_string_defaults_to_once(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "fortnightly")
        self.assertEqual(db.list_workflow_schedules()[0][3], "once")


class ListSchedulesTests(WorkflowScheduleTestBase):
    def test_empty_list_is_reported_plainly(self):
        result = WorkflowScheduler.list_schedules()
        self.assertIn("don't have any scheduled", result.lower())

    def test_non_empty_list_mentions_name_and_time(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "daily")
        result = WorkflowScheduler.list_schedules()
        self.assertIn("good morning", result)
        self.assertIn("repeats daily", result)


class CancelScheduleTests(WorkflowScheduleTestBase):
    def test_rejects_blank_name(self):
        result = WorkflowScheduler.cancel_schedule("")
        self.assertIn("which workflow", result.lower())

    def test_reports_nothing_to_cancel(self):
        result = WorkflowScheduler.cancel_schedule("good morning")
        self.assertIn("doesn't have any schedules", result.lower())

    def test_successful_cancel_removes_it(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "daily")
        result = WorkflowScheduler.cancel_schedule("Good Morning")  # case-insensitive, like save_workflow
        self.assertIn("cancelled 1", result.lower())
        self.assertEqual(db.list_workflow_schedules(), [])


class GetDueSchedulesTests(WorkflowScheduleTestBase):
    def _schedule_at(self, when: datetime, repeat="once", name="good morning"):
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow(name, "irrelevant - mocked", repeat)

    def test_nothing_due_returns_empty(self):
        self._schedule_at(datetime.now() + timedelta(hours=1))
        self.assertEqual(WorkflowScheduler.get_due_schedules(), [])

    def test_a_due_once_schedule_is_returned_and_then_disabled(self):
        self._schedule_at(datetime.now() - timedelta(minutes=1), repeat="once")
        due = WorkflowScheduler.get_due_schedules()
        self.assertEqual(due, ["good morning"])

        _id, _name, _t, _r, _next, enabled, last_run_at = db.list_workflow_schedules()[0]
        self.assertEqual(enabled, 0)
        self.assertIsNotNone(last_run_at)

        # Disabled -> get_due_workflow_schedules() (enabled=1 only) must not
        # find it again, so a slow/failed runner can never double-fire it.
        self.assertEqual(WorkflowScheduler.get_due_schedules(), [])

    def test_a_due_daily_schedule_advances_past_now_and_stays_enabled(self):
        # 3 days overdue - exercises the catch-up while-loop, not just a
        # single +1 day step (which would still land in the past).
        self._schedule_at(datetime.now() - timedelta(days=3), repeat="daily")
        due = WorkflowScheduler.get_due_schedules()
        self.assertEqual(due, ["good morning"])

        _id, _name, _t, repeat, next_run_at, enabled, _last = db.list_workflow_schedules()[0]
        self.assertEqual(enabled, 1)
        self.assertEqual(repeat, "daily")
        self.assertGreater(datetime.fromisoformat(next_run_at), datetime.now())

        # Advanced into the future -> not due again on an immediate re-check.
        self.assertEqual(WorkflowScheduler.get_due_schedules(), [])

    def test_a_due_weekly_schedule_advances_by_whole_weeks(self):
        start = datetime.now() - timedelta(days=10)  # overdue by more than one week
        self._schedule_at(start, repeat="weekly")
        due = WorkflowScheduler.get_due_schedules()
        self.assertEqual(due, ["good morning"])

        _id, _name, _t, _r, next_run_at, _enabled, _last = db.list_workflow_schedules()[0]
        next_run = datetime.fromisoformat(next_run_at)
        self.assertGreater(next_run, datetime.now())
        # Landed exactly on a 7-day step from the original time, not some
        # arbitrary future time - confirms _advance_past_now adds whole
        # periods rather than jumping straight to "now".
        self.assertEqual((next_run - start).days % 7, 0)

    def test_multiple_due_schedules_are_all_returned(self):
        db.save_workflow("second routine", ["open chrome"])
        self._schedule_at(datetime.now() - timedelta(minutes=5), name="good morning")
        self._schedule_at(datetime.now() - timedelta(minutes=5), name="second routine")
        self.assertEqual(sorted(WorkflowScheduler.get_due_schedules()), ["good morning", "second routine"])

    def test_a_db_error_is_handled_without_crashing(self):
        with mock.patch.object(db, "get_due_workflow_schedules", side_effect=RuntimeError("boom")):
            self.assertEqual(WorkflowScheduler.get_due_schedules(), [])


class RestartSurvivalTests(WorkflowScheduleTestBase):
    """The roadmap's own Tier 18 'Done when' bar, made explicit: a
    schedule created through one call/object must still be found as due
    by a COMPLETELY FRESH ProactiveEngine - simulating the app having
    been closed and reopened, with no RAM state carried over at all."""

    def test_a_fresh_engine_instance_still_finds_a_persisted_due_schedule(self):
        when = datetime.now() - timedelta(minutes=1)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            result = WorkflowScheduler.schedule_workflow("good morning", "a minute ago", "once")
        self.assertIn("Scheduled", result)

        # Nothing above ever touched a ProactiveEngine instance - this is
        # the FIRST one constructed in this test, standing in for the app
        # being relaunched after the schedule was already saved.
        fresh_engine = ProactiveEngine(interval=9999)
        runner_calls = []
        fresh_engine.set_workflow_runner(lambda name: runner_calls.append(name))

        with mock.patch.object(app_control_mod.AppController, "is_dnd_or_focus_active", return_value=False), \
             mock.patch.object(proactive_mod.SystemMonitor, "get_stats",
                               return_value={"battery_percent": "N/A", "is_plugged": False}):
            nudges = fresh_engine.check_once()

        self.assertEqual(runner_calls, ["good morning"])
        self.assertIn("workflow", [n.kind for n in nudges])

    def test_schedule_persists_across_two_independent_db_connections(self):
        # Belt-and-suspenders: list_workflow_schedules() opens its OWN
        # connection (get_connection()) independent of whatever connection
        # save_workflow_schedule used - proving this isn't an in-process
        # cache of any kind.
        when = datetime.now() + timedelta(days=1)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "tomorrow", "once")
        rows = db.list_workflow_schedules()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "good morning")


class ProactiveTriggerTestBase(unittest.TestCase):
    """Mirrors tests/test_phase6_proactive.py's own ProactiveTestBase
    exactly (temp DB, a fresh Audit wired into both automation.audit and
    the already-imported `audit` name inside automation/proactive.py,
    config save/restore) - kept as a separate base in this file rather
    than imported, since that one doesn't reset app_control's
    _active_mode (Tier 18's new DND gate needs it) or save/restore the
    new PROACTIVE_WORKFLOW_SCHEDULE_ENABLED flag."""

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        db.save_workflow("good morning", ["open spotify"])

        self.old_audit = audit_mod.audit
        audit_mod.audit = Audit(store=DbStore(db), max_entries=100)
        proactive_mod.audit = audit_mod.audit

        self.old_workflow_enabled = config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED
        self.old_mode = dict(app_control_mod._active_mode)

        # The other 5 Tier 18 triggers added alongside this one in the same
        # check_once() loop - disabled here so these tests (about scheduled
        # workflows and the DND gate specifically) aren't polluted by e.g.
        # this sandbox's real disk usage or a real-clock morning briefing.
        # tests/test_tier18_proactive_triggers.py covers those 5 directly.
        self._other_tier18_flags = ("PROACTIVE_MORNING_BRIEFING_ENABLED", "PROACTIVE_IDLE_BREAK_ENABLED",
                                    "PROACTIVE_STREAK_MILESTONES_ENABLED", "PROACTIVE_HABIT_DETECTION_ENABLED",
                                    "PROACTIVE_REMINDER_FOLLOWUPS_ENABLED", "PROACTIVE_LOW_DISK_ENABLED")
        self._other_tier18_saved = {name: getattr(config, name) for name in self._other_tier18_flags}
        for name in self._other_tier18_flags:
            setattr(config, name, False)

        def cleanup():
            db.db_path = self.old_db_path
            audit_mod.audit = self.old_audit
            proactive_mod.audit = self.old_audit
            config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = self.old_workflow_enabled
            for name, value in self._other_tier18_saved.items():
                setattr(config, name, value)
            app_control_mod._active_mode.clear()
            app_control_mod._active_mode.update(self.old_mode)
        self.addCleanup(cleanup)

        self.engine = ProactiveEngine(interval=9999)

    def _not_dnd(self):
        return mock.patch.object(app_control_mod.AppController, "is_dnd_or_focus_active", return_value=False)

    def _no_battery_signal(self):
        return mock.patch.object(proactive_mod.SystemMonitor, "get_stats",
                                 return_value={"battery_percent": "N/A", "is_plugged": False})

    def _schedule_due_now(self, name="good morning"):
        with mock.patch.object(wfs, "_parse_point", return_value=datetime.now() - timedelta(seconds=1)):
            WorkflowScheduler.schedule_workflow(name, "irrelevant - mocked", "once")


class CheckScheduledWorkflowsTests(ProactiveTriggerTestBase):
    def test_disabled_by_config_is_a_complete_no_op(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = False
        self._schedule_due_now()
        with self._not_dnd(), self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])
        # Never even peeked at it - still due, unconsumed, for when it's
        # re-enabled (unlike the "no runner wired in" case below, which
        # DOES consume it).
        self.assertEqual(db.list_workflow_schedules()[0][5], 1)  # enabled

    def test_due_with_no_runner_wired_in_still_consumes_it_and_nudges(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = True
        self._schedule_due_now()
        with self._not_dnd(), self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "workflow")
        self.assertEqual(db.list_workflow_schedules()[0][5], 0)  # enabled -> disabled, 'once' ran

    def test_due_with_a_runner_wired_in_calls_it_with_the_workflow_name(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = True
        self._schedule_due_now()
        calls = []
        self.engine.set_workflow_runner(lambda name: calls.append(name))
        with self._not_dnd(), self._no_battery_signal():
            self.engine.check_once()
        self.assertEqual(calls, ["good morning"])

    def test_runner_raising_does_not_crash_check_once(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = True
        self._schedule_due_now()

        def _boom(name):
            raise RuntimeError("GUI side blew up")
        self.engine.set_workflow_runner(_boom)
        with self._not_dnd(), self._no_battery_signal():
            nudges = self.engine.check_once()   # must not raise
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "workflow")

    def test_nothing_due_means_no_runner_calls_and_no_nudges(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = True
        calls = []
        self.engine.set_workflow_runner(lambda name: calls.append(name))
        with self._not_dnd(), self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])
        self.assertEqual(calls, [])


class DndFocusGateTests(ProactiveTriggerTestBase):
    """check_once()'s own top-level gate - added in Tier 18 specifically
    because _check_scheduled_workflows DOES something (not just notifies),
    so running it while the user is in DND/focus/sleep would be the wrong
    call. Gating happens before ANY trigger runs (see check_once()'s own
    docstring for why), so these tests check every trigger is skipped
    together, not just the workflow one."""

    def test_dnd_active_suppresses_every_trigger_without_consuming_state(self):
        config.PROACTIVE_WORKFLOW_SCHEDULE_ENABLED = True
        self._schedule_due_now()
        with mock.patch.object(app_control_mod.AppController, "is_dnd_or_focus_active", return_value=True), \
             mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value={
                 "battery_percent": 5, "is_plugged": False}):
            config.PROACTIVE_BATTERY_THRESHOLD = 20
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])
        # The due workflow schedule was never even looked at - still
        # enabled/due for the very next (non-DND) tick.
        self.assertEqual(db.list_workflow_schedules()[0][5], 1)

    def test_sleep_mode_also_counts_as_dnd(self):
        app_control_mod._active_mode["name"] = "sleep"
        with self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_focus_mode_also_counts_as_dnd(self):
        app_control_mod._active_mode["name"] = "focus"
        with self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_no_mode_active_proceeds_normally(self):
        app_control_mod._active_mode["name"] = None
        with self._no_battery_signal():
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])  # nothing armed, but it RAN (not suppressed) - no crash either

    def test_an_error_checking_dnd_state_fails_open_not_closed(self):
        # "Fails open" here means proactive checks still run (same as if
        # DND were off) rather than the whole engine tick silently dying -
        # a broken DND check shouldn't also take ambient battery/calendar
        # nudges down with it.
        with mock.patch.object(app_control_mod.AppController, "is_dnd_or_focus_active",
                               side_effect=RuntimeError("boom")), self._no_battery_signal():
            nudges = self.engine.check_once()   # must not raise
        self.assertEqual(nudges, [])


class ScheduleWorkflowSkillTests(WorkflowScheduleTestBase):
    """brain/handlers/automation.py's three new tools - thin wrappers, so
    these mainly confirm argument plumbing (args dict -> WorkflowScheduler
    call), not WorkflowScheduler's own logic again."""

    def _brain(self):
        return object.__new__(object)  # these three handlers never touch `brain`

    def test_schedule_workflow_skill_passes_args_through(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            result = automation_handlers._skill_schedule_workflow(
                self._brain(), {"name": "good morning", "when_text": "7am", "repeat": "daily"})
        self.assertIn("Scheduled", result)
        self.assertEqual(db.list_workflow_schedules()[0][3], "daily")

    def test_schedule_workflow_skill_defaults_repeat_to_once(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            automation_handlers._skill_schedule_workflow(
                self._brain(), {"name": "good morning", "when_text": "7am"})
        self.assertEqual(db.list_workflow_schedules()[0][3], "once")

    def test_list_workflow_schedules_skill(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "once")
        result = automation_handlers._skill_list_workflow_schedules(self._brain(), {})
        self.assertIn("good morning", result)

    def test_cancel_workflow_schedule_skill(self):
        when = datetime(2026, 11, 7, 7, 0, 0)
        with mock.patch.object(wfs, "_parse_point", return_value=when):
            WorkflowScheduler.schedule_workflow("good morning", "7am", "once")
        result = automation_handlers._skill_cancel_workflow_schedule(self._brain(), {"name": "good morning"})
        self.assertIn("cancelled", result.lower())
        self.assertEqual(db.list_workflow_schedules(), [])


if __name__ == "__main__":
    unittest.main()
