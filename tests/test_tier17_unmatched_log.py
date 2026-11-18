"""
tests/test_tier17_unmatched_log.py — Tier 17: brain/unmatched_log.py (the
local, append-only diagnostic log of every turn neither followup_resolver
nor intent_router recognized) plus a lightweight check that
brain/groq_core.py's two call sites (think()/think_stream()) actually wire
it in, and a few direct tests of scripts/review_unmatched_utterances.py's
grouping/correlation logic.

Mirrors test_tier17_followup_resolver.py's own split: direct unit tests of
the module itself, then a WiringTests class doing a plain source-text check
on brain/groq_core.py rather than a full BrainBase/FakeOpenAI run (see that
file's module docstring for why fast-path layers get their own dedicated
tests instead of being folded into the LLM-path integration tests).

Run: python -m unittest discover -s tests -p "test_tier17_unmatched_log.py"
(pytest will also collect it.)
"""
import inspect
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from brain import unmatched_log as ul                            # noqa: E402
from settings.config import config                                # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import review_unmatched_utterances as rev                         # noqa: E402


class UnmatchedLogTestBase(unittest.TestCase):
    """Redirects UNMATCHED_LOG_PATH to a fresh tmpdir for each test, so
    these tests never touch the real data/unmatched_utterances.jsonl -
    same reasoning test_tier17_followup_resolver.py's base class gives for
    isolating the shared automation.audit.audit singleton, just for a
    config-driven file path instead of a module-level object."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="wave_unmatched_log_test_")
        self._orig_path = config.UNMATCHED_LOG_PATH
        self._orig_enabled = config.UNMATCHED_LOG_ENABLED
        config.UNMATCHED_LOG_PATH = os.path.join(self._tmpdir, "unmatched.jsonl")
        config.UNMATCHED_LOG_ENABLED = True

    def tearDown(self):
        config.UNMATCHED_LOG_PATH = self._orig_path
        config.UNMATCHED_LOG_ENABLED = self._orig_enabled
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _read_logged_rows(self):
        path = ul.log_path()
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class LogPathTests(UnmatchedLogTestBase):
    def test_log_path_honors_the_config_override(self):
        self.assertEqual(ul.log_path(), config.UNMATCHED_LOG_PATH)

    def test_log_path_falls_back_to_the_default_when_unset(self):
        config.UNMATCHED_LOG_PATH = ""
        self.assertEqual(ul.log_path(), os.path.join("data", "unmatched_utterances.jsonl"))

    def test_log_path_falls_back_when_set_to_only_whitespace(self):
        config.UNMATCHED_LOG_PATH = "   "
        self.assertEqual(ul.log_path(), os.path.join("data", "unmatched_utterances.jsonl"))


class LogUnmatchedTests(UnmatchedLogTestBase):
    def test_appends_one_json_row_with_ts_and_text(self):
        ul.log_unmatched("open the thingamajig")
        rows = self._read_logged_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "open the thingamajig")
        self.assertIn("ts", rows[0])
        # ts must be a real, parseable timestamp, not a placeholder.
        datetime.fromisoformat(rows[0]["ts"])

    def test_multiple_calls_append_rather_than_overwrite(self):
        ul.log_unmatched("first one")
        ul.log_unmatched("second one")
        ul.log_unmatched("third one")
        rows = self._read_logged_rows()
        self.assertEqual([r["text"] for r in rows], ["first one", "second one", "third one"])

    def test_text_is_stripped_before_logging(self):
        ul.log_unmatched("  padded on both sides  ")
        rows = self._read_logged_rows()
        self.assertEqual(rows[0]["text"], "padded on both sides")

    def test_empty_or_whitespace_only_text_logs_nothing(self):
        ul.log_unmatched("")
        ul.log_unmatched("   ")
        ul.log_unmatched(None)
        self.assertEqual(self._read_logged_rows(), [])

    def test_disabled_via_config_logs_nothing(self):
        config.UNMATCHED_LOG_ENABLED = False
        ul.log_unmatched("this should not be written")
        self.assertFalse(os.path.exists(ul.log_path()))

    def test_creates_parent_directory_if_missing(self):
        nested = os.path.join(self._tmpdir, "nested", "sub", "unmatched.jsonl")
        config.UNMATCHED_LOG_PATH = nested
        ul.log_unmatched("hello")
        self.assertTrue(os.path.exists(nested))

    def test_unicode_hindi_text_round_trips_without_escaping(self):
        ul.log_unmatched("मेरा गाना बजाओ")
        rows = self._read_logged_rows()
        self.assertEqual(rows[0]["text"], "मेरा गाना बजाओ")
        # ensure_ascii=False was used - the raw file should contain the
        # actual Devanagari characters, not a \uXXXX escape sequence.
        with open(ul.log_path(), "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("मेरा", raw)

    def test_never_raises_even_when_the_path_is_unwritable(self):
        # Point at a path whose parent can't be created (a file, not a
        # directory, sitting where a directory component is expected) -
        # log_unmatched() must swallow this, not propagate it.
        blocker = os.path.join(self._tmpdir, "blocker_file")
        with open(blocker, "w") as f:
            f.write("x")
        config.UNMATCHED_LOG_PATH = os.path.join(blocker, "sub", "unmatched.jsonl")
        try:
            ul.log_unmatched("must not raise")
        except Exception as exc:  # pragma: no cover - the whole point is that this never runs
            self.fail(f"log_unmatched() raised {exc!r} instead of failing silently")


class WiringTests(unittest.TestCase):
    """Confirms brain/groq_core.py's think()/think_stream() actually call
    unmatched_log.log_unmatched() - a plain source-text check rather than a
    full BrainBase/FakeOpenAI run, same convention as
    test_tier17_followup_resolver.py's own WiringTests class."""

    def test_both_entry_points_call_log_unmatched(self):
        import brain.groq_core as gc
        think_src = inspect.getsource(gc.WaveBrain.think)
        stream_src = inspect.getsource(gc.WaveBrain.think_stream)
        self.assertIn("unmatched_log.log_unmatched(user_text)", think_src)
        self.assertIn("unmatched_log.log_unmatched(user_text)", stream_src)

    def test_logging_happens_only_after_both_fast_paths_miss(self):
        # The log call must come AFTER intent_router.route() is checked in
        # both methods - logging unconditionally, immediately once both
        # fast-path layers have already returned None for this turn, is
        # the whole design (see brain/unmatched_log.py's own docstring).
        # Anchored on the actual statements, not a bare substring, so this
        # breaks if the real call order ever changes rather than on
        # comment wording.
        import brain.groq_core as gc
        think_src = inspect.getsource(gc.WaveBrain.think)
        stream_src = inspect.getsource(gc.WaveBrain.think_stream)
        for src in (think_src, stream_src):
            self.assertLess(src.index("routed = intent_router.route(user_text)"),
                            src.index("unmatched_log.log_unmatched(user_text)"))

    def test_groq_core_imports_unmatched_log(self):
        import brain.groq_core as gc
        self.assertTrue(hasattr(gc, "unmatched_log"))


class ReviewScriptNormalizeAndGroupTests(unittest.TestCase):
    def test_normalize_lowercases_strips_punctuation_and_collapses_space(self):
        self.assertEqual(rev.normalize_for_grouping("Close It!!"), "close it")
        self.assertEqual(rev.normalize_for_grouping("  close   it  "), "close it")

    def test_distinct_phrasings_stay_distinct(self):
        self.assertNotEqual(rev.normalize_for_grouping("close it"), rev.normalize_for_grouping("close that"))

    def test_group_rows_buckets_by_normalized_text(self):
        rows = [{"ts": "t1", "text": "Close it!"}, {"ts": "t2", "text": "close it"},
                {"ts": "t3", "text": "open it"}]
        groups = rev.group_rows(rows)
        self.assertEqual(len(groups["close it"]), 2)
        self.assertEqual(len(groups["open it"]), 1)


class ReviewScriptLoadRowsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="wave_review_script_test_")

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _write(self, lines):
        path = os.path.join(self._tmpdir, "log.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n" if lines else "")
        return path

    def test_missing_file_returns_no_rows_and_no_error(self):
        rows, skipped = rev.load_rows(os.path.join(self._tmpdir, "does_not_exist.jsonl"))
        self.assertEqual(rows, [])
        self.assertEqual(skipped, 0)

    def test_valid_rows_are_loaded_in_file_order(self):
        path = self._write([json.dumps({"ts": "t1", "text": "a"}), json.dumps({"ts": "t2", "text": "b"})])
        rows, skipped = rev.load_rows(path)
        self.assertEqual([r["text"] for r in rows], ["a", "b"])
        self.assertEqual(skipped, 0)

    def test_malformed_lines_are_skipped_not_fatal(self):
        path = self._write([json.dumps({"ts": "t1", "text": "a"}), "not valid json at all",
                            json.dumps({"ts": "t2", "text": "b"})])
        rows, skipped = rev.load_rows(path)
        self.assertEqual([r["text"] for r in rows], ["a", "b"])
        self.assertEqual(skipped, 1)

    def test_rows_missing_text_or_ts_are_skipped(self):
        path = self._write([json.dumps({"ts": "t1"}), json.dumps({"text": "no timestamp"}),
                            json.dumps({"ts": "t2", "text": "fine"})])
        rows, skipped = rev.load_rows(path)
        self.assertEqual([r["text"] for r in rows], ["fine"])
        self.assertEqual(skipped, 2)

    def test_blank_lines_are_ignored_without_counting_as_skipped(self):
        path = self._write([json.dumps({"ts": "t1", "text": "a"}), "", "   ",
                            json.dumps({"ts": "t2", "text": "b"})])
        rows, skipped = rev.load_rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(skipped, 0)


class ReviewScriptCorrelationTests(unittest.TestCase):
    """Direct tests of find_correlated_action() against hand-built,
    duck-typed audit records (a real ActionRecord has more fields than
    this needs - only .ts/.tool/.args/.status are read)."""

    class _FakeRecord:
        def __init__(self, ts, tool, args=None, status="ok"):
            self.ts, self.tool, self.args, self.status = ts, tool, args or {}, status

    def _ts(self, offset_s):
        return (datetime(2026, 1, 1, 12, 0, 0) + timedelta(seconds=offset_s)).isoformat(timespec="seconds")

    def test_finds_the_closest_action_strictly_after(self):
        base = self._ts(0)
        records = [self._FakeRecord(self._ts(5), "open_app"), self._FakeRecord(self._ts(2), "close_app")]
        found = rev.find_correlated_action(base, records, window_s=30)
        self.assertEqual(found.tool, "close_app")

    def test_ignores_actions_before_the_miss(self):
        base = self._ts(10)
        records = [self._FakeRecord(self._ts(0), "open_app")]
        self.assertIsNone(rev.find_correlated_action(base, records, window_s=30))

    def test_ignores_actions_outside_the_window(self):
        base = self._ts(0)
        records = [self._FakeRecord(self._ts(45), "open_app")]
        self.assertIsNone(rev.find_correlated_action(base, records, window_s=30))

    def test_no_records_returns_none(self):
        self.assertIsNone(rev.find_correlated_action(self._ts(0), [], window_s=30))

    def test_unparseable_timestamp_returns_none(self):
        self.assertIsNone(rev.find_correlated_action("not-a-timestamp", [self._FakeRecord(self._ts(1), "open_app")],
                                                      window_s=30))


class ReviewScriptBuildReportTests(unittest.TestCase):
    def test_empty_rows_reports_nothing_to_review(self):
        report = rev.build_report([], 0, [])
        self.assertIn("Nothing to review yet", report)

    def test_min_count_hides_one_off_rows(self):
        rows = [{"ts": "t1", "text": "a one-off remark"}]
        report = rev.build_report(rows, 0, [], min_count=2)
        self.assertNotIn("one-off remark", report)
        self.assertIn("No phrasing repeated", report)

    def test_repeated_phrasing_is_shown_with_its_count(self):
        rows = [{"ts": "t1", "text": "close it"}, {"ts": "t2", "text": "close it"}]
        report = rev.build_report(rows, 0, [], min_count=2)
        self.assertIn("[2x] 'close it'", report)

    def test_skipped_count_is_mentioned_when_nonzero(self):
        report = rev.build_report([], 3, [])
        self.assertIn("3 malformed line(s) skipped", report)


if __name__ == "__main__":
    unittest.main()
