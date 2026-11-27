"""Phase 6 - automation/notes_rag.py (Tier 3 gap #7, "mere notes se batao").

No PyQt6/groq needed. Run with:
    python -m unittest discover -s tests -p "test_phase6_*.py"
"""
import os
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

from database.db_manager import db           # noqa: E402
from settings.config import config            # noqa: E402
import automation.notes_rag as nr             # noqa: E402


class ChunkingTests(unittest.TestCase):
    def test_short_text_is_a_single_chunk(self):
        self.assertEqual(nr._chunk_text("hello world"), ["hello world"])

    def test_empty_text_has_no_chunks(self):
        self.assertEqual(nr._chunk_text("   "), [])

    def test_long_text_splits_into_overlapping_chunks(self):
        paragraph = "Sentence number %d provides some filler content for testing. "
        text = "\n\n".join(paragraph % i for i in range(60))  # well over 1200 chars
        chunks = nr._chunk_text(text, chunk_chars=500, overlap=50)
        self.assertGreater(len(chunks), 1)
        # every chunk individually respects (roughly) the target size
        for c in chunks:
            self.assertLessEqual(len(c), 500 + 50)
        # reassembling covers the original content (no chunk is empty/junk)
        self.assertTrue(all(c.strip() for c in chunks))

    def test_chunking_always_terminates_even_with_overlap_bigger_than_chunk(self):
        text = "x" * 3000
        chunks = nr._chunk_text(text, chunk_chars=100, overlap=500)  # pathological overlap
        self.assertGreater(len(chunks), 0)  # must not hang / infinite loop


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_db_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()

        self.notes_dir = os.path.join(self.tmp, "notes")
        os.makedirs(self.notes_dir)
        old_notes_dir = config.NOTES_DIR
        config.NOTES_DIR = self.notes_dir

        # Point _search_dirs at ONLY our temp notes dir (not the real
        # Desktop/Documents/Downloads of whatever machine runs this test).
        patcher = mock.patch.object(nr, "_search_dirs", return_value=[self.notes_dir])
        patcher.start()
        # tests/_phase4_support.py keeps notes search off for the whole run
        # (it would index the real Desktop etc.); here it only sees the temp dir.
        old_enabled = config.NOTES_RAG_ENABLED
        config.NOTES_RAG_ENABLED = True

        def cleanup():
            patcher.stop()
            db.db_path = self.old_db_path
            config.NOTES_DIR = old_notes_dir
            config.NOTES_RAG_ENABLED = old_enabled
        self.addCleanup(cleanup)

        self.index = nr.NotesIndex()

    def _write(self, name, content):
        path = os.path.join(self.notes_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_reindex_finds_and_search_locates_a_note(self):
        self._write("wifi.txt", "The wifi password for the guest network is hunter2horse.")
        summary = self.index.reindex_all()
        self.assertIn("Indexed 1 file", summary)

        result = self.index.search("wifi password")
        self.assertIn("wifi.txt", result)
        self.assertIn("hunter2horse", result)

    def test_search_with_no_notes_indexed_says_so_clearly(self):
        result = self.index.search("anything")
        self.assertIn("haven't indexed", result.lower())

    def test_empty_question_asks_for_clarification_not_a_crash(self):
        result = self.index.search("   ")
        self.assertIn("like me to look for", result.lower())

    def test_unchanged_file_is_skipped_on_second_reindex(self):
        self._write("a.txt", "some content here about python programming")
        self.index.reindex_all()
        summary2 = self.index.reindex_all()
        self.assertIn("1 unchanged", summary2)
        self.assertIn("Indexed 0 file", summary2)

    def test_changed_file_is_reindexed_not_skipped(self):
        path = self._write("a.txt", "version one content")
        self.index.reindex_all()
        time.sleep(0.02)
        with open(path, "w", encoding="utf-8") as f:
            f.write("version two content, totally different and much longer now")
        summary2 = self.index.reindex_all()
        self.assertIn("Indexed 1 file", summary2)
        result = self.index.search("totally different")
        self.assertIn("a.txt", result)

    def test_deleted_file_is_dropped_from_the_index(self):
        path = self._write("temp.txt", "temporary note about deleting things")
        self.index.reindex_all()
        self.assertIn(path, db.indexed_note_files())
        os.remove(path)
        self.index.reindex_all()
        self.assertNotIn(path, db.indexed_note_files())

    def test_notes_rag_disabled_short_circuits_cleanly(self):
        old = config.NOTES_RAG_ENABLED
        config.NOTES_RAG_ENABLED = False
        try:
            self.assertIn("turned off", self.index.search("anything"))
            self.assertIn("turned off", self.index.reindex_all())
        finally:
            config.NOTES_RAG_ENABLED = old


if __name__ == "__main__":
    unittest.main()
