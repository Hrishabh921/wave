"""
tests/test_tier35_memory.py — Tier 35: Memory 2.0.

SARA's Memory 2.0 keeps what a fact used to be, understands when things
happen, joins related facts, and makes "forget" stick (its own bench: 0%
false memories, 0% forgetting leaks). Wave's memory was a flat key -> value
table where a new value simply overwrote the old one. Now:

  * a changed fact keeps its history ("lived in Jaipur, now Delhi");
  * relative dates in a fact ("kal", "next monday") get their real date;
  * recall follows one hop ("my sister" -> Priya -> "priya_birthday");
  * a forgotten fact stays forgotten: it isn't re-learned from old chat,
    and it's left out of the conversation sent to the model;
  * the model is told to say it doesn't know rather than guess.

    python -m unittest discover -s tests -p "test_tier35_memory.py"
"""
import os
import unittest
from datetime import date
from unittest import mock

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from brain import memory_time  # noqa: E402
from database import db_manager  # noqa: E402
from database.db_manager import db  # noqa: E402

WED = date(2026, 11, 25)


class RelativeDateTests(unittest.TestCase):
    def test_relative_dates_get_their_real_date(self):
        cases = {
            "doctor appointment kal": "doctor appointment kal (Thu 26 Nov 2026)",
            "kal Jaipur gaya tha": "kal (Tue 24 Nov 2026) Jaipur gaya tha",
            "exam next monday": "exam next monday (Mon 30 Nov 2026)",
            "3 din baad flight hai": "3 din baad (Sat 28 Nov 2026) flight hai",
            "2 days ago mila": "2 days ago (Mon 23 Nov 2026) mila",
            "parso party hai": "parso (Fri 27 Nov 2026) party hai",
            "parso party thi": "parso (Mon 23 Nov 2026) party thi",
            "meeting friday ko": "meeting friday ko (Fri 27 Nov 2026)",
            "next week trip": "next week (Mon 30 Nov 2026) trip",
            "last tuesday call kiya": "last tuesday (Tue 24 Nov 2026) call kiya",
            "आज मीटिंग है": "आज (Wed 25 Nov 2026) मीटिंग है",
            "कल डॉक्टर के पास जाना है": "कल (Thu 26 Nov 2026) डॉक्टर के पास जाना है",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(memory_time.annotate(value, WED), expected)

    def test_things_that_are_not_relative_dates_are_left_alone(self):
        for value in ("lives in Delhi", "the sun is bright", "he sat down", "aaj kal busy hoon",
                      "birthday 12 march", "may be later", "already (Thu 26 Nov 2026) set"):
            with self.subTest(value=value):
                self.assertEqual(memory_time.annotate(value, WED), value)

    def test_annotating_twice_changes_nothing(self):
        once = memory_time.annotate("doctor kal", WED)
        self.assertEqual(memory_time.annotate(once, WED), once)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

    def tearDown(self):
        db.db_path = self.old_path


class FactHistoryTests(_DbCase):
    def test_a_changed_fact_keeps_what_it_was(self):
        db.save_memory("city", "Jaipur")
        db.save_memory("city", "Delhi")
        db.save_memory("city", "Delhi")                  # the same again: nothing new
        self.assertEqual(db.get_memory("city"), "Delhi")
        history = db.get_memory_history("city")
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]["old_value"], history[0]["new_value"]), ("Jaipur", "Delhi"))

    def test_history_is_newest_first(self):
        for value in ("Jaipur", "Delhi", "Pune"):
            db.save_memory("city", value)
        self.assertEqual([h["new_value"] for h in db.get_memory_history("city")], ["Pune", "Delhi"])

    def test_a_relative_date_is_saved_with_its_real_date(self):
        class _Wednesday(date):
            @classmethod
            def today(cls):
                return WED

        with mock.patch.object(db_manager.memory_time, "date", _Wednesday):
            db.save_memory("doctor_appointment", "kal shaam 5 baje")
        self.assertEqual(db.get_memory("doctor_appointment"), "kal (Thu 26 Nov 2026) shaam 5 baje")


class ForgetTests(_DbCase):
    def test_forgetting_removes_the_fact_and_its_history(self):
        db.save_memory("address", "12 MG Road")
        db.save_memory("address", "7 Park Street")
        self.assertTrue(db.delete_memory("address"))
        self.assertIsNone(db.get_memory("address"))
        self.assertEqual(db.get_memory_history("address"), [])
        self.assertIn("7 Park Street", db.get_forgotten_values())

    def test_a_forgotten_fact_is_not_learned_again_from_old_chat(self):
        db.save_memory("address", "7 Park Street")
        db.delete_memory("address")
        self.assertFalse(db.save_memory("address", "7 Park Street", source="auto_consolidated"))
        self.assertIsNone(db.get_memory("address"))

    def test_telling_wave_again_on_purpose_works(self):
        db.save_memory("address", "7 Park Street")
        db.delete_memory("address")
        self.assertTrue(db.save_memory("address", "9 Lake Road", source="explicit"))
        self.assertEqual(db.get_memory("address"), "9 Lake Road")
        self.assertNotIn("address", db.get_forgotten_keys())


class MultiHopTests(_DbCase):
    def test_recall_follows_one_hop_from_a_name(self):
        facts = {"sister_name": "Priya", "priya_birthday": "12 March", "favorite_color": "blue",
                 "car": "Swift", "city": "Delhi", "job": "student", "pet": "dog Bruno",
                 "bruno_vet": "Dr Rao", "phone_brand": "Samsung", "laptop": "HP"}
        for k, v in facts.items():
            db.save_memory(k, v)
        # Tier 46: the ranking is db.rank_memory() (similarity + shared words) now.
        with mock.patch.object(db, "rank_memory", return_value=[("sister_name", "Priya", 0.8, 0.0)]):
            relevant = dict(db.get_relevant_memory("meri behen ka birthday kab hai", limit=8))
        self.assertIn("sister_name", relevant)
        self.assertIn("priya_birthday", relevant)        # one hop from "Priya"
        self.assertNotIn("bruno_vet", relevant)


class PromptTests(_DbCase):
    def _prompt(self, user_text):
        from brain.groq_core import WaveBrain  # noqa: PLC0415
        brain = object.__new__(WaveBrain)
        return brain.get_dynamic_system_prompt(user_text)["content"]

    def test_the_prompt_shows_what_a_fact_used_to_be_and_says_not_to_guess(self):
        db.save_memory("city", "Jaipur")
        db.save_memory("city", "Delhi")
        content = self._prompt("where do i live?")
        self.assertIn("city: Delhi", content)
        self.assertIn("before: Jaipur", content)
        self.assertIn("don't know", content)

    def test_forgotten_things_are_left_out_of_the_chat_sent_to_the_model(self):
        from brain.groq_core import WaveBrain  # noqa: PLC0415
        db.save_message("user", "my address is 7 Park Street")
        db.save_message("assistant", "Got it.")
        db.save_message("user", "what's the weather?")
        db.save_memory("address", "7 Park Street")
        db.delete_memory("address")
        history = WaveBrain._recent_history(limit=16)
        self.assertNotIn("7 Park Street", " ".join(m for _r, m in history))
        self.assertIn("what's the weather?", [m for _r, m in history])


if __name__ == "__main__":
    unittest.main()
