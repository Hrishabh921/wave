"""
tests/test_tier38_rolling_summary.py — Tier 38: "the conversation so far".

Each request carries only the newest 16 messages; older ones vanished. SARA
folds the turns that leave its window into a short running summary (one
small background model call) and puts it in the system prompt. Wave now does
the same (brain/rolling_summary.py).

    python -m unittest discover -s tests -p "test_tier38_rolling_summary.py"
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                      typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

from brain import rolling_summary as rs  # noqa: E402
from database.db_manager import db  # noqa: E402
from settings.config import config  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", self.old_path)
        p = mock.patch.object(config, "LLM_ROLLING_SUMMARY", True)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.summary = rs.RollingSummary(path=os.path.join(self.tmp, "conversation_summary.json"))

    def _chat(self, n, start=0):
        for i in range(start, start + n):
            db.save_message("user" if i % 2 == 0 else "assistant", f"message {i}")

    def _summarize(self, previous, messages):
        self.calls.append((previous, messages))
        return f"The user talked about {messages[0][1]} to {messages[-1][1]}."


class FoldTests(_Case):
    def test_nothing_is_folded_while_everything_fits_in_the_window(self):
        self._chat(rs.WINDOW)
        self.assertFalse(self.summary.update_now(self._summarize))
        self.assertEqual(self.calls, [])

    def test_messages_that_leave_the_window_are_folded_once_there_are_enough(self):
        self._chat(rs.WINDOW + rs.FOLD_AFTER - 1)
        self.assertFalse(self.summary.update_now(self._summarize))     # not enough yet
        self._chat(1, start=rs.WINDOW + rs.FOLD_AFTER - 1)
        self.assertTrue(self.summary.update_now(self._summarize))
        self.assertEqual(len(self.calls[0][1]), rs.FOLD_AFTER)
        self.assertEqual(self.summary.summary_line(), "The user talked about message 0 to message 5.")
        # The next fold builds on the summary so far, and only takes the new messages.
        self._chat(rs.FOLD_AFTER, start=rs.WINDOW + rs.FOLD_AFTER)
        self.summary.update_now(self._summarize)
        self.assertEqual(self.calls[1][0], "The user talked about message 0 to message 5.")
        self.assertEqual(self.calls[1][1][0][1], "message 6")

    def test_it_is_saved_across_restarts_and_kept_short(self):
        self._chat(rs.WINDOW + rs.FOLD_AFTER)
        with mock.patch.object(config, "LLM_SUMMARY_MAX_CHARS", 20):
            self.summary.update_now(self._summarize)
        again = rs.RollingSummary(path=self.summary.path())
        self.assertEqual(again.summary_line(), "The user talked abou")

    def test_an_old_conversation_is_not_summarised_or_shown(self):
        self._chat(rs.WINDOW + rs.FOLD_AFTER)
        later = rs.RollingSummary(path=self.summary.path(),
                                  now=lambda: datetime.now() + timedelta(hours=rs.SESSION_GAP_H + 1))
        later.update_now(self._summarize)
        self.assertEqual(self.calls, [])                           # those messages are from another session
        self.summary.update_now(self._summarize)
        self.assertEqual(later.summary_line(), "")

    def test_something_the_user_asked_to_forget_stays_out(self):
        db.save_message("user", "my pin is 4321 remember it")
        self._chat(rs.WINDOW + rs.FOLD_AFTER)
        db.save_memory("pin", "4321")
        db.delete_memory("pin")
        self.summary.update_now(self._summarize)
        self.assertNotIn("4321", " ".join(m for _r, m in self.calls[0][1]))
        # ...and a summary that mentions it isn't used.
        self.summary._state["summary"] = "The user's pin is 4321."
        self.assertEqual(self.summary.summary_line(), "")

    def test_clearing_the_chat_clears_the_summary(self):
        self._chat(rs.WINDOW + rs.FOLD_AFTER)
        self.summary.update_now(self._summarize)
        self.assertTrue(self.summary.summary_line())
        with db.get_connection() as conn:
            conn.execute("DELETE FROM chat_history")
            conn.commit()
        self.assertEqual(self.summary.summary_line(), "")

    def test_off_means_off(self):
        self._chat(rs.WINDOW + rs.FOLD_AFTER)
        self.summary.update_now(self._summarize)
        with mock.patch.object(config, "LLM_ROLLING_SUMMARY", False):
            self.assertEqual(self.summary.summary_line(), "")


class PromptTests(_Case):
    def test_the_summary_goes_in_the_system_prompt(self):
        from brain.groq_core import WaveBrain  # noqa: PLC0415
        brain = object.__new__(WaveBrain)
        with mock.patch.object(rs.rolling_summary, "summary_line", return_value="The user planned a Goa trip."), \
                mock.patch.object(rs.rolling_summary, "schedule_update") as schedule:
            content = brain.get_dynamic_system_prompt("hi")["content"]
        self.assertIn("EARLIER IN THIS CONVERSATION", content)
        self.assertIn("The user planned a Goa trip.", content)
        schedule.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
