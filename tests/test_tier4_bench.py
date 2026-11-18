"""Tier 4, item #12 - the eval/benchmark harness (bench/dataset.py,
bench/metrics.py, bench/runner.py, bench/redteam_runner.py).

Run with:
    python -m unittest discover -s tests -p "test_tier4_bench.py"
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from bench import dataset, metrics                              # noqa: E402
from bench.runner import run_router                             # noqa: E402
from bench import redteam_runner                                # noqa: E402


# =============================================================================
# bench/metrics.py - pure functions, synthetic records only.
# =============================================================================
class MetricsTests(unittest.TestCase):
    def test_accuracy_counts_exact_matches(self):
        records = [{"pred": "a", "expected": "a"}, {"pred": "b", "expected": "a"},
                  {"pred": "c", "expected": "c"}]
        self.assertAlmostEqual(metrics.accuracy(records), 2 / 3)

    def test_accuracy_empty_is_zero(self):
        self.assertEqual(metrics.accuracy([]), 0.0)

    def test_per_intent_prf_basic_case(self):
        # "x": 2 true positives, 1 false positive (predicted x, was y), 1 false negative (was x, predicted y)
        records = [
            {"pred": "x", "expected": "x"}, {"pred": "x", "expected": "x"},
            {"pred": "x", "expected": "y"}, {"pred": "y", "expected": "x"},
        ]
        prf = metrics.per_intent_prf(records)
        # per_intent_prf() rounds to 4dp for readability (see bench/metrics.py) -
        # compare at that same precision rather than against the raw fraction.
        self.assertAlmostEqual(prf["x"]["precision"], 2 / 3, places=4)
        self.assertAlmostEqual(prf["x"]["recall"], 2 / 3, places=4)
        self.assertEqual(prf["x"]["support"], 3)

    def test_false_trigger_rate_only_over_chat_expected_rows(self):
        records = [
            {"pred": "chat", "expected": "chat"}, {"pred": "volume_up", "expected": "chat"},
            {"pred": "volume_up", "expected": "volume_up"},  # not a chat-expected row - excluded
        ]
        self.assertAlmostEqual(metrics.false_trigger_rate(records), 0.5)

    def test_false_trigger_rate_none_when_no_chat_rows(self):
        self.assertIsNone(metrics.false_trigger_rate([{"pred": "a", "expected": "a"}]))

    def test_args_accuracy_only_over_checked_rows(self):
        records = [{"pred": "x", "expected": "x", "args_ok": True}, {"pred": "x", "expected": "x", "args_ok": False},
                  {"pred": "y", "expected": "y", "args_ok": None}]
        self.assertAlmostEqual(metrics.args_accuracy(records), 0.5)

    def test_latency_stats_percentiles_are_sane(self):
        records = [{"latency_ms": v} for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]
        stats = metrics.latency_stats(records)
        self.assertEqual(stats["n"], 10)
        self.assertAlmostEqual(stats["p50"], 5.5, delta=0.6)
        self.assertEqual(stats["max"], 10)

    def test_latency_score_full_marks_under_budget(self):
        self.assertEqual(metrics.latency_score(1.0, budget_ms=5.0), 1.0)

    def test_latency_score_decays_past_budget(self):
        score = metrics.latency_score(10.0, budget_ms=5.0)   # 2x budget
        self.assertAlmostEqual(score, 0.0, delta=0.01)

    def test_composite_score_treats_missing_ftr_as_perfect_safety(self):
        with_ftr = metrics.composite_score(1.0, 1.0, 0.0, 1.0)
        without_ftr = metrics.composite_score(1.0, 1.0, None, 1.0)
        self.assertAlmostEqual(with_ftr, without_ftr)
        self.assertAlmostEqual(with_ftr, 1.0)


# =============================================================================
# bench/dataset.py - real file I/O against temp jsonl files.
# =============================================================================
class DatasetLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="wave_bench_dataset_test_")

    def _write(self, name: str, content: str) -> Path:
        path = Path(self.tmpdir) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_defaults_merge_into_each_row(self):
        path = self._write("x.jsonl", '{"_defaults": {"l": "en"}}\n{"t": "hi", "i": "chat"}\n'
                                      '{"t": "bonjour", "i": "chat", "l": "fr"}\n')
        rows = dataset.load_jsonl(path)
        self.assertEqual(rows[0].lang, "en")     # picked up the default
        self.assertEqual(rows[1].lang, "fr")     # row's own value wins over the default

    def test_blank_and_comment_lines_are_skipped(self):
        path = self._write("x.jsonl", '\n// a comment\n{"t": "hi", "i": "chat"}\n\n')
        rows = dataset.load_jsonl(path)
        self.assertEqual(len(rows), 1)

    def test_missing_required_key_raises_with_line_number(self):
        path = self._write("x.jsonl", '{"t": "hi"}\n')
        with self.assertRaises(ValueError) as ctx:
            dataset.load_jsonl(path)
        self.assertIn(":1:", str(ctx.exception))

    def test_invalid_json_raises_with_line_number(self):
        path = self._write("x.jsonl", '{"t": "hi", "i": "chat"\n')
        with self.assertRaises(ValueError) as ctx:
            dataset.load_jsonl(path)
        self.assertIn(":1:", str(ctx.exception))

    def test_expected_args_and_note_round_trip(self):
        path = self._write("x.jsonl", '{"t": "open x", "i": "open_app", "a": {"app_name": "x"}, "n": "why"}\n')
        row = dataset.load_jsonl(path)[0]
        self.assertEqual(row.expected_args, {"app_name": "x"})
        self.assertEqual(row.note, "why")


# =============================================================================
# End to end against the REAL shipped golden/redteam data - this is the
# regression guard: a future change to intent_router.py, content_trust.py,
# or skills_registry.py that breaks one of these rows fails here, not
# silently in production.
# =============================================================================
class RealGoldenDataTests(unittest.TestCase):
    def test_full_golden_set_is_100_percent_accurate_right_now(self):
        rows = dataset.load_golden()
        records = run_router(rows)
        acc = metrics.accuracy(records)
        wrong = [r for r in records if r["pred"] != r["expected"]]
        self.assertEqual(acc, 1.0, f"unexpected mismatches: {wrong}")

    def test_full_golden_set_has_zero_false_triggers_right_now(self):
        rows = dataset.load_golden()
        records = run_router(rows)
        ftr = metrics.false_trigger_rate(records)
        self.assertEqual(ftr, 0.0)

    def test_golden_set_covers_every_intent_router_rule_at_least_once(self):
        # Every _Rule.name in brain/intent_router.py's own table should have
        # at least one golden row whose 'rule' comes back matching it -
        # catches a future rule added to intent_router.py with no bench
        # coverage at all, not just a rule that silently broke.
        from brain import intent_router
        rows = dataset.load_golden()
        records = run_router(rows)
        exercised_rules = {r["rule"] for r in records if r["rule"]}
        all_rule_names = {rule.name for rule in intent_router._RULES}
        missing = all_rule_names - exercised_rules
        self.assertEqual(missing, set(), f"intent_router rule(s) with no golden coverage: {missing}")


class HeadlessStubTests(unittest.TestCase):
    """Tier 22: redteam_runner._install_headless_stub_if_needed() - the
    fix that lets check_gate_invariant() (and so `bench.run_bench
    --mode all`) run on a headless Linux box where `import pyautogui`
    dies either with a plain KeyError (no DISPLAY) or - the sneakier
    case this test locks in - a SystemExit raised deep inside its
    mouseinfo dependency when tkinter itself is missing, which a bare
    `except Exception` would NOT catch."""

    def setUp(self):
        self._saved = {}
        for name in ("tier22_fakepkg_ok", "tier22_fakepkg_keyerror", "tier22_fakepkg_systemexit"):
            self._saved[name] = sys.modules.pop(name, None)

    def tearDown(self):
        for name, mod in self._saved.items():
            sys.modules.pop(name, None)
            if mod is not None:
                sys.modules[name] = mod

    def test_a_real_importable_module_is_left_alone(self):
        import types
        real = types.ModuleType("tier22_fakepkg_ok")
        real.marker = "real"
        sys.modules["tier22_fakepkg_ok"] = real
        redteam_runner._install_headless_stub_if_needed("tier22_fakepkg_ok", marker="fake")
        self.assertEqual(sys.modules["tier22_fakepkg_ok"].marker, "real")

    def test_a_module_that_simply_does_not_exist_gets_the_stand_in(self):
        """The baseline case (no DISPLAY -> KeyError deep in mouseinfo,
        or just plain ModuleNotFoundError): any ordinary Exception
        subclass during import falls through to the stand-in."""
        redteam_runner._install_headless_stub_if_needed("tier22_fakepkg_keyerror", marker="fake")
        self.assertEqual(sys.modules["tier22_fakepkg_keyerror"].marker, "fake")

    def test_a_module_raising_systemexit_still_gets_the_stand_in(self):
        """The whole point of this helper: BaseException, not Exception,
        is what's caught - SystemExit (what mouseinfo raises when
        tkinter is missing) must not escape."""
        import importlib.abc
        import importlib.machinery

        class _SystemExitLoader(importlib.abc.Loader):
            def create_module(self, spec):
                return None

            def exec_module(self, module):
                raise SystemExit("NOTE: You must install tkinter on Linux to use MouseInfo.")

        class _SystemExitFinder(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "tier22_fakepkg_systemexit":
                    return importlib.machinery.ModuleSpec(name, _SystemExitLoader())
                return None

        finder = _SystemExitFinder()
        sys.meta_path.insert(0, finder)
        try:
            redteam_runner._install_headless_stub_if_needed("tier22_fakepkg_systemexit", marker="fake")
        finally:
            sys.meta_path.remove(finder)
        self.assertEqual(sys.modules["tier22_fakepkg_systemexit"].marker, "fake")


class RealRedteamDataTests(unittest.TestCase):
    def test_attacks_are_recalled_above_the_threshold_in_thresholds_json(self):
        import json
        rows = dataset.load_jsonl(dataset.BENCH_DIR / "redteam" / "attacks.jsonl")
        outcomes = redteam_runner.run_scanner(rows)
        m = redteam_runner.scanner_metrics(outcomes)
        thresholds = json.loads((dataset.BENCH_DIR / "thresholds.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(m["recall"], thresholds["redteam"]["min_recall"], m["missed_attacks"])

    def test_benign_false_positive_rate_is_within_the_known_bound(self):
        import json
        rows = dataset.load_jsonl(dataset.BENCH_DIR / "redteam" / "benign.jsonl")
        outcomes = redteam_runner.run_scanner(rows)
        m = redteam_runner.scanner_metrics(outcomes)
        thresholds = json.loads((dataset.BENCH_DIR / "thresholds.json").read_text(encoding="utf-8"))
        self.assertLessEqual(m["false_positive_rate"], thresholds["redteam"]["max_false_positive_rate"])

    def test_confirm_gate_invariant_holds_against_the_live_registry(self):
        result = redteam_runner.check_gate_invariant()
        self.assertTrue(result["ok"], result["failures"])


if __name__ == "__main__":
    unittest.main()
