"""Tier 50 - the held-out command set (bench/heldout/) and the fast-path bugs it found.

RouterFixTests: brain/intent_router.py no longer sets the volume to 2% for "volume 2 of the book",
takes a screenshot for a story about one, raises the brightness for a how-to question, closes an
"app" called "sound bilkul", or minimizes a window called "all the windows".
HeldoutBenchTests: bench/heldout_bench.py's bookkeeping, and the gate on the real sentence model
(skipped when it isn't downloaded).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402,F401

from bench import heldout_bench as hb  # noqa: E402
from brain import intent_router as ir  # noqa: E402


def _skill(text):
    r = ir.route(text)
    return (r.skill, r.args) if r is not None else None


class RouterFixTests(unittest.TestCase):
    def test_a_number_must_end_a_volume_or_brightness_command(self):
        self.assertIsNone(_skill("volume 2 of the book"))
        self.assertEqual(_skill("set volume to 50"), ("set_volume", {"percent": 50}))
        self.assertEqual(_skill("volume 30 percent"), ("set_volume", {"percent": 30}))
        self.assertEqual(_skill("awaaz 40 pe kar do"), ("set_volume", {"percent": 40}))
        self.assertEqual(_skill("brightness 70 kar do"), ("set_brightness", {"percent": 70}))

    def test_how_to_questions_and_stories_run_nothing(self):
        self.assertIsNone(_skill("how do i increase brightness on a mac"))
        self.assertIsNone(_skill("screenshot kaise karte hain"))
        self.assertIsNone(_skill("my sister took a screenshot of our chat"))
        self.assertIsNone(_skill("i took a screenshot yesterday"))
        self.assertEqual(_skill("take a screenshot")[0], "take_screenshot")
        self.assertEqual(_skill("increase brightness")[0], "brightness_up")

    def test_a_sound_word_is_not_an_app_name(self):
        self.assertIsNone(_skill("sound bilkul band kar do"))
        self.assertIsNone(_skill("close the music"))
        self.assertEqual(_skill("chrome band kar do"), ("close_app", {"app_name": "chrome"}))
        self.assertEqual(_skill("close notepad"), ("close_app", {"app_name": "notepad"}))

    def test_all_the_windows_is_the_desktop(self):
        for text in ["minimize all the windows", "sari windows minimize kar do", "minimize all windows"]:
            self.assertEqual(_skill(text)[0], "show_desktop", text)
        self.assertEqual(_skill("minimize notepad"), ("minimize_window", {"window_name": "notepad"}))


class HeldoutBenchTests(unittest.TestCase):
    def test_the_set(self):
        rows = hb.load()
        self.assertEqual(sum(1 for r in rows if r["i"] != "chat"), 76)
        self.assertEqual(sum(1 for r in rows if r["i"] == "chat"), 41)

    def test_the_bookkeeping(self):
        records = [
            {"t": "a", "i": "volume_up", "l": "en", "fast": "volume_up", "semantic": None},
            {"t": "b", "i": "volume_up", "l": "en", "fast": None, "semantic": "volume_up"},
            {"t": "c", "i": "volume_up", "l": "hinglish", "fast": None, "semantic": "volume_down"},
            {"t": "d", "i": "minimize_all_windows", "l": "en", "fast": "show_desktop", "semantic": None},
            {"t": "e", "i": "chat", "l": "en", "fast": None, "semantic": "get_news"},
            {"t": "f", "i": "chat", "l": "en", "fast": None, "semantic": None},
        ]
        m = hb.metrics(records)
        self.assertEqual((m["fast_right"], m["both_right"], m["semantic_wrong"], m["semantic_false_triggers"]),
                         (2, 3, 1, 1))
        self.assertEqual(len(hb.gate_failures(m)), 2)


def _model_downloaded() -> bool:
    try:
        import importlib.util  # noqa: PLC0415
        if importlib.util.find_spec("sentence_transformers") is None:
            return False
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
        hit = try_to_load_from_cache("sentence-transformers/all-MiniLM-L6-v2", "config.json")
        return isinstance(hit, str) and os.path.exists(hit)
    except Exception:
        return False


@unittest.skipUnless(_model_downloaded(), "all-MiniLM-L6-v2 isn't downloaded on this machine")
class HeldoutGateTests(unittest.TestCase):
    def test_no_wrong_action_and_no_false_trigger(self):
        from settings.config import config  # noqa: PLC0415
        from brain import semantic_intents as si  # noqa: PLC0415
        saved = (config.SEMANTIC_INTENTS, config.SEMANTIC_MEMORY_ENABLED)
        config.SEMANTIC_INTENTS = config.SEMANTIC_MEMORY_ENABLED = True
        try:
            m = hb.metrics(hb.run(hb.load(), si.SemanticIntents()))
        finally:
            config.SEMANTIC_INTENTS, config.SEMANTIC_MEMORY_ENABLED = saved
        self.assertEqual(hb.gate_failures(m), [])
        self.assertEqual(m["fast_wrong"], 0)
        self.assertLessEqual(m["fast_false_triggers"], 1)        # "minimize the risk" - a harmless look-up
        self.assertGreaterEqual(m["both_right"], 55)


if __name__ == "__main__":
    unittest.main()
