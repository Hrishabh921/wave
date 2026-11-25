"""Tier 46 - memory: people graph, ranking by shared words, dated messages, decay, the memory bench.

PeopleGraphTests / RankTests / EpisodicTests: the pure parts.
DbTests: a temporary database - ranking, people first, decay (archive, not
delete), messages by local date and by subject.
WiredIntoThinkTests: a live turn learns a person; the prompt carries the
user's dated messages for a question about the past.
BenchTests: bench/memory_bench.py on its scenarios and on the held-out set,
on the real sentence model (skipped when it isn't downloaded).
"""
import os
import sys
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402
from test_phase4_brain import BrainBase  # noqa: E402

from brain import episodic_recall, memory_rank, people_graph  # noqa: E402
from database import embeddings  # noqa: E402
from database.db_manager import db  # noqa: E402
from settings.config import config  # noqa: E402

WED = date(2026, 3, 25)


def _flags(testcase, **values):
    saved = {k: getattr(config, k) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    testcase.addCleanup(lambda: [setattr(config, k, v) for k, v in saved.items()])


class PeopleGraphTests(unittest.TestCase):
    def setUp(self):
        _flags(self, MEMORY_PEOPLE_GRAPH=True)

    def test_clear_statements_about_a_person(self):
        for text, expected in [
            ("my sister's name is Priya and she works at Infosys",
             [("priya_relation", "sister"), ("priya_works_at", "Infosys")]),
            ("My sister Priya lives in Pune", [("priya_relation", "sister"), ("priya_lives_in", "Pune")]),
            ("meri behen ka naam Priya hai", [("priya_relation", "sister")]),
            ("mere dost Rohan Google mein kaam karte hain", [("rohan_relation", "friend"), ("rohan_works_at", "Google")]),
            ("mere dost Rohan ki job Google mein hai", [("rohan_relation", "friend"), ("rohan_works_at", "Google")]),
            ("meri didi Neha Delhi mein rehti hai", [("neha_relation", "sister"), ("neha_lives_in", "Delhi")]),
            ("my mother is Sunita", [("sunita_relation", "mother")]),
            ("my best friend's name is aman", [("aman_relation", "friend")]),
        ]:
            self.assertEqual(people_graph.relation_facts(text), expected, text)

    def test_no_person_is_made_up(self):
        for text in ["my sister is sick", "my friend is busy today", "my boss Is angry", "I live in Jaipur",
                     "my brother And I went out", "what's my sister's name?", "bhai volume badha do"]:
            self.assertEqual(people_graph.relation_facts(text), [], text)

    def test_the_graph_reads_the_models_own_keys_too(self):
        graph = people_graph.people([("sister_name", "Priya"), ("priya_birthday", "12 March"),
                                     ("best_friend", "Rohan"), ("brother_city", "Mumbai"), ("car", "Swift")])
        self.assertEqual(graph["priya"]["relations"], {"sister"})
        self.assertIn(("priya_birthday", "12 March"), graph["priya"]["facts"])
        self.assertEqual(graph["rohan"]["relations"], {"friend"})
        self.assertNotIn("mumbai", graph)      # "brother_city" is about the brother, not his name

    def test_a_question_brings_the_people_it_names(self):
        facts = [("priya_relation", "sister"), ("priya_works_at", "Infosys"), ("rohan_relation", "friend"),
                 ("rohan_lives_in", "Pune"), ("brother_city", "Mumbai"), ("city", "Jaipur")]
        self.assertEqual(people_graph.related_facts("where does my sister work?", facts),
                         [("priya_relation", "sister"), ("priya_works_at", "Infosys")])
        self.assertEqual(people_graph.related_facts("meri behen kahan kaam karti hai", facts)[1],
                         ("priya_works_at", "Infosys"))
        self.assertIn(("priya_works_at", "Infosys"), people_graph.related_facts("what does Priya do?", facts))
        self.assertEqual(people_graph.related_facts("where does my brother live?", facts), [("brother_city", "Mumbai")])
        self.assertEqual(people_graph.related_facts("bhai volume badha do", facts), [])   # "bhai" as a filler
        self.assertEqual(people_graph.related_facts("what's my uncle's name?", facts), [])

    def test_switched_off(self):
        config.MEMORY_PEOPLE_GRAPH = False
        saved = []
        self.assertEqual(people_graph.learn("my sister's name is Priya", lambda k, v: saved.append(k)), [])
        self.assertEqual(saved, [])
        self.assertEqual(people_graph.related_facts("my sister", [("priya_relation", "sister")]), [])


class RankTests(unittest.TestCase):
    def test_the_words_a_label_uses_for_the_same_thing(self):
        words = memory_rank.content_words("where do I live now?")
        self.assertEqual(words, ["live"])
        self.assertEqual(memory_rank.lexical(words, "city", "Jaipur"), 1.0)
        self.assertEqual(memory_rank.lexical(memory_rank.content_words("mujhe kaunsi mithai pasand hai?"),
                                             "favorite_sweet", "gulab jamun"), 1.0)
        self.assertEqual(memory_rank.lexical(memory_rank.content_words("do you remember what I'm called?"),
                                             "name", "Arjun"), 1.0)
        self.assertEqual(memory_rank.lexical(words, "favorite_color", "blue"), 0.0)
        self.assertEqual(memory_rank.lexical([], "city", "Jaipur"), 0.0)


class EpisodicTests(unittest.TestCase):
    def test_questions_about_the_past(self):
        for text in ["what did I tell you last Monday?", "kal maine kya kharida tha?", "parso maine kisko call kiya tha?",
                     "what did I do two weeks ago?", "maine exam ke baare mein kya bataya tha?"]:
            self.assertTrue(episodic_recall.is_recall_question(text), text)
        for text in ["aaj maine naya phone kharida", "what's the weather", "when is my exam", "I did it"]:
            self.assertFalse(episodic_recall.is_recall_question(text), text)

    def test_the_time_a_question_names(self):
        w = episodic_recall.window
        self.assertEqual(w("what did I tell you last Monday?", WED)[1:], (date(2026, 3, 23), date(2026, 3, 23)))
        self.assertEqual(w("kal maine kya kharida tha?", WED)[1:], (date(2026, 3, 24),) * 2)
        self.assertEqual(w("parso maine kisko call kiya tha?", WED)[1:], (date(2026, 3, 23),) * 2)
        self.assertEqual(w("what did I do two weeks ago?", WED)[1:], (date(2026, 3, 8), date(2026, 3, 14)))
        self.assertEqual(w("what did we talk about last week", WED)[1:], (date(2026, 3, 16), date(2026, 3, 22)))
        self.assertIsNone(w("what's my plan for next monday", WED))          # the future isn't recalled
        self.assertEqual(episodic_recall.topic("what did I tell you about the cricket match?")[1], ["cricket", "match"])
        self.assertEqual(episodic_recall.topic("maine exam ke baare mein kya bataya tha?")[1], ["exam"])

    def test_dated_lines_leave_out_forgotten_things_and_other_questions(self):
        store = mock.Mock()
        store.get_user_messages_on.return_value = [
            ("2026-03-24 19:00", "aaj maine naya phone kharida"),
            ("2026-03-24 19:05", "my locker hint is the blue door"),
            ("2026-03-24 19:10", "what did I tell you yesterday?"),
        ]
        store.get_forgotten_values.return_value = ["the blue door"]
        _flags(self, MEMORY_EPISODIC=True)
        label, lines = episodic_recall.recall("kal maine kya kharida tha?", today=WED, store=store)
        self.assertEqual(label, "ON Tue 24 Mar 2026")
        self.assertEqual(lines, ["- Tue 24 Mar 19:00: aaj maine naya phone kharida"])
        config.MEMORY_EPISODIC = False
        self.assertIsNone(episodic_recall.recall("kal maine kya kharida tha?", today=WED, store=store))


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        old = (db.db_path, db.history_enabled)
        db.db_path = os.path.join(self.tmp, "t.db")
        db.history_enabled = True
        db.init_db()
        self.addCleanup(lambda: setattr(db, "db_path", old[0]) or setattr(db, "history_enabled", old[1]))
        _flags(self, MEMORY_PEOPLE_GRAPH=True)
        # no sentence model: the shared-word half of the ranking alone
        patcher = mock.patch.object(embeddings, "embed", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fill(self, n=12, source="explicit"):
        for i in range(n):
            db.save_memory(f"filler_{i}", f"value {i}", source=source)

    def test_shared_words_find_a_fact_similarity_misses(self):
        self._fill()
        db.save_memory("city", "Jaipur")
        db.save_memory("favorite_sweet", "gulab jamun")
        self.assertEqual(db.get_relevant_memory("where do I live now?")[0], ("city", "Jaipur"))
        self.assertEqual(db.get_relevant_memory("mujhe kaunsi mithai pasand hai?")[0],
                         ("favorite_sweet", "gulab jamun"))

    def test_the_people_a_question_names_come_first(self):
        self._fill()
        db.save_memory("priya_relation", "sister", source="auto_consolidated")
        db.save_memory("priya_works_at", "Infosys", source="auto_consolidated")
        relevant = db.get_relevant_memory("where does my sister work?")
        self.assertEqual(relevant[:2], [("priya_relation", "sister"), ("priya_works_at", "Infosys")])

    def test_decay_sets_aside_only_long_unused_picked_up_facts(self):
        old = (datetime.now() - timedelta(days=400)).isoformat()
        recent = datetime.now().isoformat()
        self._fill(45)                                            # explicit: never archived
        for key, source, when in [("old_auto", "auto_consolidated", old), ("old_explicit", "explicit", old),
                                  ("new_auto", "auto_consolidated", recent),
                                  ("priya_relation", "auto_consolidated", old)]:
            db.save_memory(key, "x" if key != "priya_relation" else "sister", source=source)
            with db.get_connection() as conn:
                conn.execute("UPDATE memory SET updated_at = ? WHERE key = ?", (when, key))
                conn.commit()
        self.assertEqual(db.archive_weak_memories(), 1)
        active = dict(db.get_all_memory(include_archived=False))
        self.assertNotIn("old_auto", active)
        for key in ("old_explicit", "new_auto", "priya_relation"):
            self.assertIn(key, active)
        self.assertIn("old_auto", dict(db.get_all_memory()))          # set aside, not deleted
        db.touch_memory(["old_auto"])                                 # recalled -> back
        self.assertIn("old_auto", dict(db.get_all_memory(include_archived=False)))

    def test_a_short_list_is_never_pruned(self):
        old = (datetime.now() - timedelta(days=400)).isoformat()
        db.save_memory("old_auto", "x", source="auto_consolidated")
        with db.get_connection() as conn:
            conn.execute("UPDATE memory SET updated_at = ?", (old,))
            conn.commit()
        self.assertEqual(db.archive_weak_memories(), 0)

    def test_saving_again_revives_a_set_aside_fact(self):
        db.save_memory("hobby", "chess", source="auto_consolidated")
        with db.get_connection() as conn:
            conn.execute("UPDATE memory SET archived_at = ? WHERE key = 'hobby'", (datetime.now().isoformat(),))
            conn.commit()
        self.assertNotIn("hobby", dict(db.get_all_memory(include_archived=False)))
        db.save_memory("hobby", "chess and guitar", source="auto_consolidated")
        self.assertIn("hobby", dict(db.get_all_memory(include_archived=False)))

    def test_messages_by_local_day_and_by_subject(self):
        with db.get_connection() as conn:
            for when, text in [("-2 days", "I played cricket today"), ("-1 days", "bought a new phone"),
                               ("-1 days", "the phone case is blue")]:
                conn.execute("INSERT INTO chat_history (role, message, timestamp) VALUES "
                             "('user', ?, datetime('now', ?))", (text, when))
            conn.execute("INSERT INTO chat_history (role, message) VALUES ('assistant', 'nice phone!')")
            conn.commit()
        yesterday = date.today() - timedelta(days=1)
        self.assertEqual([m for _w, m in db.get_user_messages_on(yesterday, yesterday)],
                         ["bought a new phone", "the phone case is blue"])
        found = [m for _w, m in db.search_user_messages(["phone", "case"])]
        self.assertEqual(found, ["the phone case is blue", "bought a new phone"])
        db.history_enabled = False
        self.assertEqual(db.get_user_messages_on(yesterday, yesterday), [])


class WiredIntoThinkTests(BrainBase):
    def setUp(self):
        super().setUp()
        _flags(self, MEMORY_PEOPLE_GRAPH=True, MEMORY_EPISODIC=True)
        db.history_enabled = True

    def test_a_turn_learns_the_people_it_names(self):
        self.brain._learn_people("My sister's name is Priya and she works at Infosys")
        self.assertEqual(db.get_memory("priya_works_at"), "Infosys")
        self.assertEqual(db.get_memory_provenance("priya_relation")["source"], "auto_consolidated")

    def test_not_when_chats_arent_kept(self):
        db.history_enabled = False
        self.addCleanup(setattr, db, "history_enabled", True)
        self.brain._learn_people("My sister's name is Priya")
        self.assertIsNone(db.get_memory("priya_relation"))

    def test_the_prompt_carries_the_dated_messages_a_past_question_asks_about(self):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO chat_history (role, message, timestamp) VALUES "
                         "('user', 'I watched Interstellar with my cousin', datetime('now', '-1 days'))")
            conn.commit()
        content = self.brain.get_dynamic_system_prompt("what did I do yesterday?")["content"]
        self.assertIn("TOLD YOU ON", content)
        self.assertIn("I watched Interstellar with my cousin", content)
        plain = self.brain.get_dynamic_system_prompt("what's the capital of France?")["content"]
        self.assertNotIn("Interstellar", plain)


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
class BenchTests(unittest.TestCase):
    def setUp(self):
        _flags(self, SEMANTIC_MEMORY_ENABLED=True)

    def _check(self, path):
        from bench import memory_bench as mb  # noqa: PLC0415
        records = mb.run(mb.load(path), tier46=True)
        m = mb.metrics(records)
        self.assertEqual(mb.gate_failures(m), [], [r["id"] for r in records if not r["passed"]])

    def test_the_scenarios_pass_the_gate(self):
        self._check(os.path.join(os.path.dirname(__file__), "..", "bench", "memory", "scenarios.jsonl"))

    def test_the_held_out_questions_pass_the_gate(self):
        self._check(os.path.join(os.path.dirname(__file__), "..", "bench", "memory", "heldout.jsonl"))


if __name__ == "__main__":
    unittest.main()
