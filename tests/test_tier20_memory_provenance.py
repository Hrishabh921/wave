"""Tier 20 item: memory - semantic fact supersede + provenance.

Covers:
  * database/db_manager.py's save_memory() - semantic supersede (>= 0.92
    similarity updates an existing DIFFERENT key instead of inserting a
    near-duplicate) and the new source/turn_id/updated_at columns.
  * database/db_manager.py's get_memory_provenance().
  * brain/handlers/memory.py's remember_this/why_remembered skills.
  * brain/memory_consolidation.py's auto-save path tagging
    source='auto_consolidated'.

Real embeddings (sentence-transformers) aren't installed in this sandbox
(database/embeddings.embed() returns None, same as a real install missing
the optional package) - so the supersede tests mock at the same boundary
save_memory() itself calls through (database.embeddings.embed() and
db.semantic_recall()), which is the right seam either way: this tests
save_memory()'s OWN supersede logic, not whether a particular embedding
model is installed on this machine (that's database/embeddings.py's own
concern, untouched by this tier).
"""
import os
import sys
import unittest
from unittest import mock

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                       hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                       typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                       size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from database import db_manager
from database.db_manager import db


class MemorySupersedeAndProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

    def tearDown(self):
        db.db_path = self.old_path

    def test_exact_key_still_updates_in_place_unchanged(self):
        db.save_memory("favorite_color", "blue")
        db.save_memory("favorite_color", "green")
        self.assertEqual(db.get_memory("favorite_color"), "green")
        self.assertEqual(len(db.get_all_memory()), 1)

    def test_new_key_without_embeddings_available_just_inserts_normally(self):
        """Real behavior in this sandbox (no sentence-transformers): no
        supersede is attempted at all, same as before Tier 20."""
        db.save_memory("name", "Alex")
        db.save_memory("favorite_color", "blue")
        self.assertEqual(len(db.get_all_memory()), 2)

    def test_semantically_similar_new_key_supersedes_the_existing_one(self):
        with mock.patch.object(db_manager.embeddings, "embed", return_value=[0.1, 0.2, 0.3]), \
             mock.patch.object(db, "semantic_recall", return_value=[("favorite_colour", "blue", 0.95)]):
            db.save_memory("favorite_color", "blue")
        self.assertEqual(len(db.get_all_memory()), 1)
        all_memory = dict(db.get_all_memory())
        self.assertIn("favorite_colour", all_memory)  # the OLD key, updated in place
        self.assertNotIn("favorite_color", all_memory)  # no new key inserted

    def test_similarity_below_threshold_does_not_supersede(self):
        with mock.patch.object(db_manager.embeddings, "embed", return_value=[0.1, 0.2, 0.3]), \
             mock.patch.object(db, "semantic_recall", return_value=[]):  # below min_similarity -> no hits
            db.save_memory("favorite_color", "blue")
            db.save_memory("favorite_food", "pizza")
        self.assertEqual(len(db.get_all_memory()), 2)

    def test_supersede_only_consulted_for_a_genuinely_new_key(self):
        """An exact-match key never goes through the supersede lookup at
        all - semantic_recall should not even be called."""
        db.save_memory("favorite_color", "blue")
        with mock.patch.object(db, "semantic_recall") as mock_recall:
            db.save_memory("favorite_color", "green")
        mock_recall.assert_not_called()

    def test_provenance_round_trips(self):
        db.save_memory("name", "Alex", source="explicit", turn_id="abc123")
        info = db.get_memory_provenance("name")
        self.assertEqual(info["key"], "name")
        self.assertEqual(info["value"], "Alex")
        self.assertEqual(info["source"], "explicit")
        self.assertEqual(info["turn_id"], "abc123")
        self.assertIsNotNone(info["updated_at"])

    def test_provenance_is_none_for_an_unknown_key(self):
        self.assertIsNone(db.get_memory_provenance("nope"))

    def test_provenance_defaults_to_none_when_not_passed(self):
        db.save_memory("name", "Alex")
        info = db.get_memory_provenance("name")
        self.assertIsNone(info["source"])
        self.assertIsNone(info["turn_id"])

    def test_resaving_updates_updated_at(self):
        db.save_memory("name", "Alex")
        first = db.get_memory_provenance("name")["updated_at"]
        db.save_memory("name", "Alexandra")
        second = db.get_memory_provenance("name")["updated_at"]
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)


class MemoryHandlerSkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        import brain.handlers.memory as mh
        self.mh = mh

    def tearDown(self):
        db.db_path = self.old_path

    def test_remember_this_tags_explicit_provenance(self):
        self.mh._skill_remember_this(None, {"key": "name", "value": "Alex"})
        info = db.get_memory_provenance("name")
        self.assertEqual(info["source"], "explicit")

    def test_why_remembered_explains_an_explicit_fact(self):
        self.mh._skill_remember_this(None, {"key": "name", "value": "Alex"})
        out = self.mh._skill_why_remembered(None, {"key": "name"})
        self.assertIn("directly asked", out)
        self.assertIn("Alex", out)

    def test_why_remembered_explains_an_auto_consolidated_fact(self):
        db.save_memory("name", "Alex", source="auto_consolidated")
        out = self.mh._skill_why_remembered(None, {"key": "name"})
        self.assertIn("on my own", out)

    def test_why_remembered_handles_pre_tier20_rows_honestly(self):
        db.save_memory("name", "Alex")  # no source/turn_id - as if saved before this migration
        out = self.mh._skill_why_remembered(None, {"key": "name"})
        self.assertIn("predates this feature", out)

    def test_why_remembered_on_unknown_key(self):
        out = self.mh._skill_why_remembered(None, {"key": "nope"})
        self.assertIn("don't have anything remembered", out)


class MemoryConsolidationProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

    def tearDown(self):
        db.db_path = self.old_path

    def test_consolidation_save_path_tags_auto_consolidated(self):
        import brain.memory_consolidation as mc
        fact = mc.ExtractedFact(key="favorite_food", value="pizza")
        saved = []
        if db.save_memory(fact.key, fact.value, source="auto_consolidated"):
            saved.append(fact)
        self.assertEqual(len(saved), 1)
        info = db.get_memory_provenance("favorite_food")
        self.assertEqual(info["source"], "auto_consolidated")
        self.assertIsNone(info["turn_id"])  # honestly no single turn applies to a batch job


if __name__ == "__main__":
    unittest.main()
