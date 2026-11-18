"""Phase 6 - automation/proactive.py (Tier 3 gap #5, the proactive engine).

No PyQt6/groq needed - check_once() is pure/synchronous by design
specifically so it's testable without a real thread or a real sleep. Run
with:
    python -m unittest discover -s tests -p "test_phase6_*.py"
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402

# Tier 18: check_once() now does a local `from automation.app_control
# import AppController` for its DND/focus gate (see that method's own
# docstring) - automation.app_control hard-imports pyautogui at module
# level, so it has to be faked here too, same as every other test file
# that ends up touching that module transitively.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                    # noqa: E402
from automation.audit import Audit, DbStore                   # noqa: E402
from database.db_manager import db                             # noqa: E402
from settings.config import config                              # noqa: E402
import automation.proactive as proactive_mod                    # noqa: E402
from automation.proactive import ProactiveEngine                # noqa: E402


class ProactiveTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

        self.old_audit = audit_mod.audit
        audit_mod.audit = Audit(store=DbStore(db), max_entries=100)
        proactive_mod.audit = audit_mod.audit  # the module imported `audit` by name directly

        self.cfg_saved = (config.PROACTIVE_BATTERY_ENABLED, config.PROACTIVE_BATTERY_THRESHOLD,
                          config.PROACTIVE_BATTERY_COOLDOWN_MIN, config.PROACTIVE_CALENDAR_ENABLED,
                          config.PROACTIVE_CALENDAR_LEAD_MINUTES)

        # Tier 18: this file predates (and knows nothing about) the 6
        # proactive triggers added that tier - without this, e.g.
        # _check_low_disk firing off this SANDBOX's real (and possibly
        # genuinely low) disk usage, or _check_morning_briefing making a
        # real network call, would make battery/calendar tests below
        # fail/flake for reasons that have nothing to do with what they're
        # testing. tests/test_tier18_proactive_triggers.py is where those 6
        # get their own real coverage.
        self._tier18_flags = ("PROACTIVE_WORKFLOW_SCHEDULE_ENABLED", "PROACTIVE_MORNING_BRIEFING_ENABLED",
                              "PROACTIVE_IDLE_BREAK_ENABLED", "PROACTIVE_STREAK_MILESTONES_ENABLED",
                              "PROACTIVE_HABIT_DETECTION_ENABLED", "PROACTIVE_REMINDER_FOLLOWUPS_ENABLED",
                              "PROACTIVE_LOW_DISK_ENABLED")
        self._tier18_saved = {name: getattr(config, name) for name in self._tier18_flags}
        for name in self._tier18_flags:
            setattr(config, name, False)

        def cleanup():
            db.db_path = self.old_db_path
            audit_mod.audit = self.old_audit
            proactive_mod.audit = self.old_audit
            (config.PROACTIVE_BATTERY_ENABLED, config.PROACTIVE_BATTERY_THRESHOLD,
             config.PROACTIVE_BATTERY_COOLDOWN_MIN, config.PROACTIVE_CALENDAR_ENABLED,
             config.PROACTIVE_CALENDAR_LEAD_MINUTES) = self.cfg_saved
            for name, value in self._tier18_saved.items():
                setattr(config, name, value)
        self.addCleanup(cleanup)

        self.engine = ProactiveEngine(interval=9999)  # never used - tests call check_once() directly


def _stats(percent, plugged):
    return {"cpu_percent": 10, "ram_percent": 20, "ram_used_gb": 1, "ram_total_gb": 8,
            "disk_percent": 30, "battery_percent": percent, "is_plugged": plugged}


class BatteryTriggerTests(ProactiveTestBase):
    def test_low_unplugged_battery_fires_a_nudge(self):
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(15, False)):
            nudges = self.engine.check_once()
        kinds = [n.kind for n in nudges]
        self.assertIn("battery", kinds)
        self.assertIn("15", nudges[0].message)

    def test_plugged_in_never_nudges_even_if_low(self):
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(10, True)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_healthy_battery_never_nudges(self):
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(80, False)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_no_battery_sensor_na_is_handled_without_crashing(self):
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats("N/A", False)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_repeated_low_battery_only_nudges_once_until_cooldown_passes(self):
        """Hysteresis: once nudged, stays quiet on every subsequent check
        while still low - the whole point of the cooldown/armed state."""
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        config.PROACTIVE_BATTERY_COOLDOWN_MIN = 30
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(15, False)):
            first = self.engine.check_once()
            second = self.engine.check_once()
            third = self.engine.check_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(third, [])

    def test_recharging_above_threshold_rearms_the_nudge(self):
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(15, False)):
            first = self.engine.check_once()
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(90, True)):
            self.engine.check_once()  # charged back up - re-arms
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(15, False)):
            third = self.engine.check_once()  # dropped low again - should nudge again
        self.assertEqual(len(first), 1)
        self.assertEqual(len(third), 1)

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_BATTERY_ENABLED = False
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(5, False)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])


class CalendarTriggerTests(ProactiveTestBase):
    def test_event_starting_soon_fires_a_nudge(self):
        config.PROACTIVE_CALENDAR_LEAD_MINUTES = 10
        soon = datetime.now() + timedelta(minutes=5)
        db.save_calendar_event("Team standup", soon.isoformat(), None, None, None)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats("N/A", False)):
            nudges = self.engine.check_once()
        kinds_titles = [(n.kind, n.message) for n in nudges]
        self.assertTrue(any(k == "calendar" and "Team standup" in m for k, m in kinds_titles))

    def test_same_event_never_nudges_twice(self):
        soon = datetime.now() + timedelta(minutes=5)
        db.save_calendar_event("Only once", soon.isoformat(), None, None, None)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats("N/A", False)):
            first = self.engine.check_once()
            second = self.engine.check_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_event_outside_lead_window_does_not_nudge_yet(self):
        config.PROACTIVE_CALENDAR_LEAD_MINUTES = 10
        far = datetime.now() + timedelta(hours=5)
        db.save_calendar_event("Later today", far.isoformat(), None, None, None)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats("N/A", False)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_disabled_trigger_never_fires(self):
        config.PROACTIVE_CALENDAR_ENABLED = False
        soon = datetime.now() + timedelta(minutes=2)
        db.save_calendar_event("Should be ignored", soon.isoformat(), None, None, None)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats("N/A", False)):
            nudges = self.engine.check_once()
        self.assertEqual(nudges, [])

    def test_a_recurring_events_later_occurrence_still_nudges(self):
        """Tier 8: get_calendar_events_between() now expands a recurring
        event into one virtual row per occurrence, all sharing the SAME
        real row id (see its docstring in database/db_manager.py) but with
        a different start_at each day. _check_calendar()'s dedup key must
        be (id, start_at), not bare id - a bare-id key would suppress
        every occurrence after the very first one, forever."""
        config.PROACTIVE_CALENDAR_LEAD_MINUTES = 10
        day1 = (datetime.now() + timedelta(minutes=5)).isoformat()
        day2 = (datetime.now() + timedelta(days=1, minutes=5)).isoformat()
        with mock.patch.object(proactive_mod.db, "get_upcoming_calendar_events",
                                return_value=[(1, "Standup", day1, None, None, None, "daily")]):
            first = self.engine.check_once()
        with mock.patch.object(proactive_mod.db, "get_upcoming_calendar_events",
                                return_value=[(1, "Standup", day2, None, None, None, "daily")]):
            second = self.engine.check_once()
        self.assertEqual(len([n for n in first if n.kind == "calendar"]), 1)
        self.assertEqual(len([n for n in second if n.kind == "calendar"]), 1)

    def test_the_exact_same_occurrence_seen_twice_still_never_double_nudges(self):
        config.PROACTIVE_CALENDAR_LEAD_MINUTES = 10
        same = (datetime.now() + timedelta(minutes=5)).isoformat()
        with mock.patch.object(proactive_mod.db, "get_upcoming_calendar_events",
                                return_value=[(1, "Standup", same, None, None, None, "daily")]):
            first = self.engine.check_once()
            second = self.engine.check_once()
        self.assertEqual(len([n for n in first if n.kind == "calendar"]), 1)
        self.assertEqual(len([n for n in second if n.kind == "calendar"]), 0)


class ListenerAndAuditTests(ProactiveTestBase):
    def test_listeners_receive_every_nudge(self):
        received = []
        self.engine.add_listener(received.append)
        config.PROACTIVE_BATTERY_THRESHOLD = 20
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(5, False)):
            self.engine.check_once()
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].kind, "battery")

    def test_removed_listener_stops_receiving(self):
        received = []
        self.engine.add_listener(received.append)
        self.engine.remove_listener(received.append)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(5, False)):
            self.engine.check_once()
        self.assertEqual(received, [])

    def test_a_raising_listener_does_not_stop_other_listeners_or_crash(self):
        received = []

        def bad(nudge):
            raise RuntimeError("boom")
        self.engine.add_listener(bad)
        self.engine.add_listener(received.append)
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(5, False)):
            self.engine.check_once()  # must not raise
        self.assertEqual(len(received), 1)

    def test_nudges_are_written_to_the_audit_log(self):
        with mock.patch.object(proactive_mod.SystemMonitor, "get_stats", return_value=_stats(5, False)):
            self.engine.check_once()
        rows = audit_mod.audit.recent(10)
        self.assertTrue(any(r.tool == "proactive_battery" for r in rows))


if __name__ == "__main__":
    unittest.main()
