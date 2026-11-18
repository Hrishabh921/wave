"""SARA-roadmap Tier 9 - realistic HTML fixtures for brain/content_trust.py,
instead of only the short synthetic strings test_phase7_confirm_gate.py's
ContentTrustScannerTests already covers.

tests/fixtures/injected_page.html and normal_page.html are full, readable
pages (nav, article body, a forum-reply thread) rather than an isolated
one-line string: injected_page.html buries its injection attempt inside an
otherwise-ordinary-looking troubleshooting tip (the way a real indirect
prompt-injection attempt against a browsing/summarizing assistant is
actually disguised), and normal_page.html deliberately uses two of
content_trust.py's own LOW-WEIGHT trigger words ("secretly", "acts as") in
completely innocent context, so these tests exercise the WEIGHTED score
threshold on realistic prose, not just keyword presence on a short string.

    python -m unittest discover -s tests -p "test_content_trust_fixtures.py"
"""
import os
import unittest

import _phase4_support as S  # noqa: F401  (adds the project root to sys.path)

from brain import content_trust  # noqa: E402

_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _load_fixture(name: str) -> str:
    with open(os.path.join(_FIXTURES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


class ContentTrustFixtureTests(unittest.TestCase):
    def setUp(self):
        self.normal_page = _load_fixture("normal_page.html")
        self.injected_page = _load_fixture("injected_page.html")

    def test_fixtures_actually_loaded_something(self):
        # A sanity check on the harness itself: if a fixture file goes
        # missing or empty, every other test here would pass vacuously -
        # catch that directly instead.
        self.assertGreater(len(self.normal_page), 1000)
        self.assertGreater(len(self.injected_page), 1000)

    def test_realistic_benign_page_is_not_flagged_despite_trigger_words(self):
        result = content_trust.scan(self.normal_page)
        self.assertFalse(
            result.flagged,
            f"normal_page.html should score under threshold; got score={result.score}, hits={result.hits}",
        )
        # The low-weight words ARE present and may still register as hits -
        # what matters is they alone don't clear the flagging threshold.
        self.assertLess(result.score, content_trust.config.CONTENT_TRUST_SCAN_THRESHOLD)

    def test_realistic_injection_buried_in_a_forum_reply_is_flagged(self):
        result = content_trust.scan(self.injected_page)
        self.assertTrue(
            result.flagged,
            f"injected_page.html should score at/over threshold; got score={result.score}, hits={result.hits}",
        )

    def test_injection_fixture_trips_several_distinct_patterns(self):
        """Not just flagged on a technicality - the disguised tip should
        trip multiple independent patterns across English/Hinglish, the
        way a real multi-sentence injection attempt would."""
        result = content_trust.scan(self.injected_page)
        self.assertGreaterEqual(
            len(set(result.hits)), 3,
            f"expected several distinct pattern hits, got: {result.hits}",
        )
        self.assertIn("ignore-previous-instructions", result.hits)
        self.assertIn("hinglish-run-command", result.hits)
        self.assertIn("hinglish-hide-from-user", result.hits)

    def test_wrap_untrusted_keeps_the_full_page_for_the_injected_fixture(self):
        wrapped = content_trust.wrap_untrusted(self.injected_page, source="a web search")
        self.assertIn(self.injected_page, wrapped)  # nothing stripped, ever
        self.assertIn("UNTRUSTED CONTENT", wrapped)
        self.assertIn("prompt-injection", wrapped)  # extra suspicion noted

    def test_wrap_untrusted_does_not_add_the_flagged_suffix_for_the_normal_fixture(self):
        wrapped = content_trust.wrap_untrusted(self.normal_page, source="a web search")
        self.assertIn(self.normal_page, wrapped)
        self.assertIn("UNTRUSTED CONTENT", wrapped)
        self.assertNotIn("prompt-injection", wrapped)

    def test_the_benign_tip_exchange_in_the_injected_page_does_not_mask_the_attack(self):
        """The injected fixture also contains several fully ordinary replies
        (the chipset-driver fix, the thread being marked solved) - confirms
        the scanner isn't somehow diluted by the surrounding genuine
        content, the way a naive "percentage of bad words" heuristic might
        be."""
        ordinary_only = self.injected_page.split("Pro tip while you're in there")[0]
        self.assertFalse(content_trust.scan(ordinary_only).flagged)
        self.assertTrue(content_trust.scan(self.injected_page).flagged)
