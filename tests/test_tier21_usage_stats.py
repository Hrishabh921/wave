"""Tier 21 roadmap item: "Local-only usage stats, opt-in."

Tests telemetry/usage_stats.py directly - pure Python, no PyQt6 - covering:
  * the opt-in gate (summarize() touches nothing when USAGE_STATS_ENABLED
    is off, which is the default);
  * when enabled, it correctly aggregates turn_trace rows it reads via
    database.db_manager.db.get_recent_traces() (mocked here - this module
    adds no new collection, only a read+summary of that existing table);
  * the days-window cutoff, success-rate math, and skill-name extraction
    from each row's meta JSON column;
  * it never raises, even on a DB read failure or malformed meta JSON.
"""
import json
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)
import telemetry.usage_stats as US

# Checked at module IMPORT time (collection), same convention every other
# fake-Qt test file in this project uses - NOT inside a setUpClass, which
# runs at test-execution time, by which point an earlier test's
# import_with_fakes() may already have registered a stand-in top-level
# "PyQt6" module in sys.modules (that's how it fakes PyQt6 not being
# installed), which would make a same-process `import PyQt6` succeed and
# give a false "real Qt is here" reading.
try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False


def _iso(dt):
    return dt.isoformat(timespec="milliseconds")


def _row(source="voice", outcome="ok", skill=None, started_at=None):
    return {
        "started_at": started_at or _iso(datetime.now(timezone.utc)),
        "source": source,
        "outcome": outcome,
        "meta": json.dumps({"skill": skill} if skill else {}),
    }


class EnabledGateTests(unittest.TestCase):
    def test_disabled_by_default_returns_without_touching_the_db(self):
        with mock.patch.object(US.config, "USAGE_STATS_ENABLED", False):
            with mock.patch("database.db_manager.db") as db:
                result = US.summarize()
        self.assertEqual(result, {"enabled": False})
        db.get_recent_traces.assert_not_called()

    def test_enabled_flag_reflects_config(self):
        with mock.patch.object(US.config, "USAGE_STATS_ENABLED", True):
            self.assertTrue(US.enabled())
        with mock.patch.object(US.config, "USAGE_STATS_ENABLED", False):
            self.assertFalse(US.enabled())


class SummarizeTests(unittest.TestCase):
    def _summarize_with_rows(self, rows, **kwargs):
        with mock.patch.object(US.config, "USAGE_STATS_ENABLED", True):
            with mock.patch("database.db_manager.db") as db:
                db.get_recent_traces.return_value = rows
                return US.summarize(**kwargs)

    def test_counts_turns_and_splits_by_source(self):
        rows = [_row(source="voice"), _row(source="voice"), _row(source="text")]
        result = self._summarize_with_rows(rows)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["total_turns"], 3)
        self.assertEqual(result["by_source"], {"voice": 2, "text": 1})

    def test_success_rate_counts_ok_outcomes_only(self):
        rows = [_row(outcome="ok"), _row(outcome="ok"), _row(outcome="error")]
        result = self._summarize_with_rows(rows)
        self.assertAlmostEqual(result["success_rate"], round(2 / 3, 3))

    def test_success_rate_is_none_with_no_turns(self):
        result = self._summarize_with_rows([])
        self.assertEqual(result["total_turns"], 0)
        self.assertIsNone(result["success_rate"])

    def test_top_skills_extracted_from_meta_json_and_ranked(self):
        rows = [_row(skill="open_app"), _row(skill="open_app"), _row(skill="set_reminder")]
        result = self._summarize_with_rows(rows)
        self.assertEqual(result["top_skills"][0], ("open_app", 2))
        self.assertIn(("set_reminder", 1), result["top_skills"])

    def test_rows_without_a_skill_in_meta_are_not_counted(self):
        rows = [_row(skill=None), _row(skill=None)]
        result = self._summarize_with_rows(rows)
        self.assertEqual(result["top_skills"], [])

    def test_rows_older_than_the_window_are_excluded(self):
        old = _iso(datetime.now(timezone.utc) - timedelta(days=30))
        recent = _iso(datetime.now(timezone.utc))
        rows = [_row(started_at=old), _row(started_at=recent)]
        result = self._summarize_with_rows(rows, days=7)
        self.assertEqual(result["total_turns"], 1)

    def test_malformed_meta_json_does_not_crash(self):
        rows = [{"started_at": _iso(datetime.now(timezone.utc)), "source": "voice",
                 "outcome": "ok", "meta": "{not valid json"}]
        result = self._summarize_with_rows(rows)   # must not raise
        self.assertEqual(result["total_turns"], 1)
        self.assertEqual(result["top_skills"], [])

    def test_missing_started_at_is_still_counted_not_dropped(self):
        rows = [{"source": "voice", "outcome": "ok", "meta": "{}", "started_at": None}]
        result = self._summarize_with_rows(rows)
        self.assertEqual(result["total_turns"], 1)

    def test_db_read_failure_does_not_raise(self):
        with mock.patch.object(US.config, "USAGE_STATS_ENABLED", True):
            with mock.patch("database.db_manager.db") as db:
                db.get_recent_traces.side_effect = RuntimeError("disk error")
                result = US.summarize()   # must not raise
        self.assertTrue(result["enabled"])
        self.assertEqual(result["total_turns"], 0)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class SettingsPanelHandlerTests(unittest.TestCase):
    """ui/settings_panel.py's _on_usage_stats_toggle handler - same
    bare-instance pattern test_tier9_settings_panel.py already uses for
    this class's other handler methods (_panel() = object.__new__(...))."""

    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        import test_phase4_ui_logic as P4
        P4.install_stub()
        import test_tier9_settings_panel as SP
        cls.sp = SP.import_with_fakes("ui.settings_panel")

    def test_toggling_on_sets_the_config_flag(self):
        panel = object.__new__(self.sp.SettingsPanel)
        with mock.patch.object(self.sp.config, "USAGE_STATS_ENABLED", False):
            panel._on_usage_stats_toggle(True)
            self.assertTrue(self.sp.config.USAGE_STATS_ENABLED)

    def test_toggling_off_clears_the_config_flag(self):
        panel = object.__new__(self.sp.SettingsPanel)
        with mock.patch.object(self.sp.config, "USAGE_STATS_ENABLED", True):
            panel._on_usage_stats_toggle(False)
            self.assertFalse(self.sp.config.USAGE_STATS_ENABLED)


if __name__ == "__main__":
    unittest.main()
