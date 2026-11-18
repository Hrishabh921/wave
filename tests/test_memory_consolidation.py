"""
tests/test_memory_consolidation.py — Tier 2 item #6:
brain/memory_consolidation.py.

ParseFactsTests: pure, no DB/server - _parse_facts()'s line-by-line
parsing and junk-filtering in isolation.

MemoryConsolidatorTests: tick() end to end against a real temp SQLite DB
(same db.db_path swap test_phase4_brain.py's BrainBase uses) and a real
fake-OpenAI-compatible server (same _fake_openai.py harness used
throughout the Tier 1 tests) - the module's own module-level LLM-client
cache is bypassed the same way tests/test_tier3_reminders.py bypasses
reminders/composer.py's identical cache, by swapping out
_get_client_and_cfg() for the duration of each test.
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S                                     # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from _fake_openai import FakeOpenAI, Reply, completion            # noqa: E402
from brain import llm_provider                                    # noqa: E402
import brain.memory_consolidation as mc                           # noqa: E402
from database.db_manager import db                                 # noqa: E402
from settings.config import config                                 # noqa: E402


class ParseFactsTests(unittest.TestCase):
    def test_a_clean_line_parses(self):
        facts = mc._parse_facts("favorite_color: blue", max_facts=3)
        self.assertEqual(facts, [mc.ExtractedFact("favorite_color", "blue")])

    def test_multiple_lines(self):
        facts = mc._parse_facts("name: Priya\nsisters_name: Anjali", max_facts=3)
        self.assertEqual([f.key for f in facts], ["name", "sisters_name"])

    def test_none_response_yields_nothing(self):
        self.assertEqual(mc._parse_facts("NONE", max_facts=3), [])
        self.assertEqual(mc._parse_facts("  none  ", max_facts=3), [])
        self.assertEqual(mc._parse_facts("", max_facts=3), [])

    def test_strips_bullet_and_number_prefixes(self):
        facts = mc._parse_facts("- name: Priya\n2) city: Jaipur\n* team: Blue", max_facts=5)
        self.assertEqual([(f.key, f.value) for f in facts], [("name", "Priya"), ("city", "Jaipur"), ("team", "Blue")])

    def test_line_with_no_colon_is_dropped(self):
        self.assertEqual(mc._parse_facts("this is just a sentence", max_facts=3), [])

    def test_key_with_spaces_gets_underscored(self):
        facts = mc._parse_facts("favorite movie: Dune", max_facts=3)
        self.assertEqual(facts[0].key, "favorite_movie")

    def test_key_that_is_actually_a_sentence_is_rejected(self):
        """Guards against the model ignoring the key-format instruction
        and writing a whole clause before the colon - _KEY_RE is what
        catches this, not just the colon-split."""
        facts = mc._parse_facts("the user mentioned their city is: Jaipur", max_facts=3)
        self.assertEqual(facts, [])

    def test_truncated_value_is_rejected(self):
        """Same junk-filter idea as SARA's own extractor: a value ending on
        a word that can't end a sentence is almost certainly a truncated
        generation, not a real fact."""
        facts = mc._parse_facts("name: the", max_facts=3)
        self.assertEqual(facts, [])

    def test_overlong_value_is_rejected(self):
        facts = mc._parse_facts(f"notes: {'x' * 250}", max_facts=3)
        self.assertEqual(facts, [])

    def test_respects_max_facts_cap(self):
        raw = "\n".join(f"key{i}: value{i}" for i in range(10))
        facts = mc._parse_facts(raw, max_facts=3)
        self.assertEqual(len(facts), 3)

    def test_empty_value_is_dropped(self):
        self.assertEqual(mc._parse_facts("key:", max_facts=3), [])
        self.assertEqual(mc._parse_facts("key:   ", max_facts=3), [])


class MemoryConsolidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

        old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        old_history_enabled = db.history_enabled
        db.history_enabled = True

        cfg = llm_provider.resolve_provider(env={
            "LLM_PROVIDER": "custom", "LLM_BASE_URL": self.srv.url,
            "LLM_MODEL": "test-model", "LLM_API_KEY": "sk-test-1234567890",
        })
        client = llm_provider.build_client(cfg)
        self.assertIsNotNone(client, "fake provider should resolve to a real client")
        original_getter = mc._get_client_and_cfg
        mc._get_client_and_cfg = lambda: (client, cfg)

        enabled_before = config.MEMORY_CONSOLIDATION_ENABLED
        config.MEMORY_CONSOLIDATION_ENABLED = True

        def cleanup():
            db.db_path = old_path
            db.history_enabled = old_history_enabled
            mc._get_client_and_cfg = original_getter
            config.MEMORY_CONSOLIDATION_ENABLED = enabled_before
        self.addCleanup(cleanup)

        self.consolidator = mc.MemoryConsolidator()

    def _seed_history(self, *pairs):
        """pairs like ("user", "..."), ("assistant", "...")"""
        for role, message in pairs:
            db.save_message(role, message)

    def test_too_little_history_is_a_no_op(self):
        self._seed_history(("user", "hi"))
        self.srv.script = []     # if this made a call, there'd be nothing scripted to answer it
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])

    def test_happy_path_saves_extracted_facts(self):
        self._seed_history(
            ("user", "by the way my dog's name is Max"),
            ("assistant", "Got it, good to know about Max!"),
            ("user", "what's the weather today"),
            ("assistant", "It's sunny and 24 degrees."),
        )
        self.srv.script = [Reply(200, completion("dogs_name: Max"))]
        saved = self.consolidator.tick()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].key, "dogs_name")
        self.assertEqual(db.get_memory("dogs_name"), "Max")   # the SAME path remember_this writes through

    def test_none_response_saves_nothing(self):
        self._seed_history(("user", "what's 2+2"), ("assistant", "4."))
        self.srv.script = [Reply(200, completion("NONE"))]
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])
        self.assertEqual(db.get_all_memory(), [])

    def test_disabled_flag_is_a_full_no_op(self):
        config.MEMORY_CONSOLIDATION_ENABLED = False
        self._seed_history(("user", "my name is Priya"), ("assistant", "Nice to meet you, Priya!"))
        self.srv.script = []
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])

    def test_history_disabled_skips_even_with_the_feature_on(self):
        """Settings > Privacy > "Don't save my chats to disk" - see the
        module docstring. Nothing should be persisted off the back of
        conversation that's only in the RAM session buffer."""
        db.history_enabled = False
        db.save_message("user", "my name is Priya")        # lands in the RAM buffer only
        db.save_message("assistant", "Nice to meet you!")
        self.srv.script = []
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])

    def test_a_second_tick_with_no_new_messages_makes_no_llm_call(self):
        self._seed_history(("user", "my name is Priya"), ("assistant", "Nice to meet you!"))
        self.srv.script = [Reply(200, completion("name: Priya"))]
        first = self.consolidator.tick()
        self.assertEqual(len(first), 1)

        self.srv.script = []   # nothing scripted - a second call would error
        second = self.consolidator.tick()
        self.assertEqual(second, [])

    def test_new_messages_after_a_tick_do_trigger_another_call(self):
        self._seed_history(("user", "my name is Priya"), ("assistant", "Nice to meet you!"))
        self.srv.script = [Reply(200, completion("name: Priya"))]
        self.consolidator.tick()

        self._seed_history(("user", "my dog is Max"), ("assistant", "Noted!"))
        self.srv.script = [Reply(200, completion("dogs_name: Max"))]
        second = self.consolidator.tick()
        self.assertEqual([f.key for f in second], ["dogs_name"])

    def test_existing_memory_is_shown_to_the_model(self):
        db.save_memory("name", "Priya")
        self._seed_history(("user", "my dog's name is Max"), ("assistant", "Got it!"))
        self.srv.script = [Reply(200, completion("dogs_name: Max"))]
        self.consolidator.tick()
        sent = self.srv.posts()[0].json["messages"]
        prompt_text = sent[0]["content"]
        self.assertIn("name: Priya", prompt_text)

    def test_a_failed_llm_call_degrades_to_no_facts_not_a_crash(self):
        self._seed_history(("user", "my name is Priya"), ("assistant", "Hi Priya!"))
        self.srv.script = []   # exhausted script - the fake server errors on an unscripted request
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])

    def test_no_provider_configured_is_a_graceful_no_op(self):
        mc._get_client_and_cfg = lambda: (None, None)
        self._seed_history(("user", "my name is Priya"), ("assistant", "Hi Priya!"))
        saved = self.consolidator.tick()
        self.assertEqual(saved, [])

    def test_tick_is_independently_callable_without_a_thread(self):
        """Same testability property automation/proactive.py's check_once()
        has - tick() needs no thread, no sleep, nothing started."""
        self._seed_history(("user", "my name is Priya"), ("assistant", "Hi!"))
        self.srv.script = [Reply(200, completion("name: Priya"))]
        self.assertFalse(self.consolidator._thread)
        self.consolidator.tick()
        self.assertEqual(db.get_memory("name"), "Priya")


if __name__ == "__main__":
    unittest.main()
