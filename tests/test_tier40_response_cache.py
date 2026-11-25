"""
tests/test_tier40_response_cache.py — Tier 40: instant answers to questions already answered.

SARA caches final answers to stable, non-personal knowledge questions.
brain/response_cache.py does that for Wave, conservatively: a wrong cached
answer is worse than a slow one.

    python -m unittest discover -s tests -p "test_tier40_response_cache.py"
"""
import os
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from brain import response_cache as rc  # noqa: E402
from database.db_manager import db  # noqa: E402
from settings.config import config  # noqa: E402

MODEL = "openai/gpt-oss-20b"


class CacheableTests(unittest.TestCase):
    def test_stable_knowledge_questions(self):
        for q in ("What is photosynthesis?", "how does a refrigerator work", "explain quantum entanglement simply",
                  "who wrote the Ramayana?", "blockchain kya hota hai?", "difference between RAM and ROM"):
            with self.subTest(q=q):
                self.assertTrue(rc.is_cacheable_question(q))

    def test_anything_personal_timely_or_contextual_is_never_cached(self):
        for q in ("what is my name?", "what's the weather today?", "who won the match yesterday?",
                  "what is the latest iPhone price?", "what is it?", "uska matlab kya hai?", "who are you?",
                  "open chrome", "what time is it?", "kya haal hai", "mujhe kya karna chahiye?",
                  "what does this file do?", "hello", "thanks a lot"):
            with self.subTest(q=q):
                self.assertFalse(rc.is_cacheable_question(q))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", self.old_path)
        p = mock.patch.object(config, "LLM_RESPONSE_CACHE", True)
        p.start()
        self.addCleanup(p.stop)

    def test_an_answer_is_stored_and_found_again_however_it_is_typed(self):
        self.assertIsNone(rc.get("What is photosynthesis?", MODEL))
        self.assertTrue(rc.put("What is photosynthesis?", "Plants turning light into food.", MODEL))
        self.assertEqual(rc.get("what is  photosynthesis", MODEL), "Plants turning light into food.")

    def test_another_model_or_question_misses(self):
        rc.put("What is photosynthesis?", "Plants turning light into food.", MODEL)
        self.assertIsNone(rc.get("What is photosynthesis?", "openai/gpt-oss-120b"))
        self.assertIsNone(rc.get("What is respiration?", MODEL))

    def test_personal_questions_are_never_stored(self):
        self.assertFalse(rc.put("what is my name?", "Rahul", MODEL))
        self.assertIsNone(rc.get("what is my name?", MODEL))

    def test_old_answers_expire(self):
        rc.put("What is photosynthesis?", "old answer", MODEL)
        with rc._conn() as conn:
            conn.execute("UPDATE response_cache SET created_at = ?",
                         ((datetime.now() - timedelta(days=30)).isoformat(),))
            conn.commit()
        self.assertIsNone(rc.get("What is photosynthesis?", MODEL))

    def test_only_the_newest_entries_are_kept(self):
        with mock.patch.object(rc, "MAX_ENTRIES", 3):
            for i in range(5):
                rc.put(f"what is element number {i}?", f"answer {i}", MODEL)
        with rc._conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM response_cache").fetchone()[0], 3)

    def test_off_means_off(self):
        rc.put("What is photosynthesis?", "Plants turning light into food.", MODEL)
        with mock.patch.object(config, "LLM_RESPONSE_CACHE", False):
            self.assertIsNone(rc.get("What is photosynthesis?", MODEL))

    def test_the_brain_asks_the_cache_with_the_turns_model(self):
        import test_tier29_reply_reliability as t29  # noqa: PLC0415
        brain = t29._brain({})
        brain._route_tls = threading.local()
        brain._route_tls.tier = "fast"
        rc.put("What is photosynthesis?", "Plants turning light into food.", MODEL)
        self.assertEqual(brain._cached_answer("What is photosynthesis?"), "Plants turning light into food.")
        self.assertIsNone(brain._cached_answer("What is photosynthesis?", attachment={"kind": "document"}))
        brain._route_tls.used_model = MODEL
        brain._remember_answer("how does rain form?", "Water vapour condenses.")
        self.assertEqual(rc.get("how does rain form?", MODEL), "Water vapour condenses.")


if __name__ == "__main__":
    unittest.main()
