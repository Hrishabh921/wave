"""tests/test_tier57_smarter_turns.py - Tier 57: songs in the browser the
user wants, and requests small enough for Groq's free tier.

The owner, 2026-11-28 (voice, through Wave's own STT): "Play Jeene Laga Hoon
on Brave." searched YouTube for "jeene laga hoon on brave" in the default
browser; they told Wave to play songs on Brave (it remembered:
"preferred_browser_for_youtube: Brave") but the play path never read that;
"Jo Brave par YouTube search kiya hai na, us YouTube ke andar jee ne laga hoon
song play karo." went to the model - and with one tool-using command costing
two ~3.5k-token requests against 8,000 tokens a minute, it ended in "That's
taking longer than expected". Nothing here plays anything or calls a model.
"""
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402,F401  (adds the repo root to sys.path)

from brain import intent_router as ir  # noqa: E402
from brain.groq_core import WaveBrain  # noqa: E402
from brain.handlers import media  # noqa: E402
from web.web_ops import WebManager  # noqa: E402


def _route(text):
    r = ir.route(text)
    return (r.skill, r.args) if r else None


class PlayRuleTests(unittest.TestCase):
    def test_the_owners_own_sentences(self):
        self.assertEqual(_route("Play Jeene Laga Hoon on Brave."),
                         ("play_youtube", {"query": "jeene laga hoon", "browser": "brave"}))
        self.assertEqual(_route("Jo Brave par YouTube search kiya hai na, us YouTube ke andar jee ne laga hoon "
                                "song play karo."),
                         ("play_youtube", {"query": "jee ne laga hoon", "browser": "brave"}))
        self.assertEqual(_route("Play jee ne laga hoon."), ("play_youtube", {"query": "jee ne laga hoon"}))

    def test_browser_youtube_and_song_words_leave_the_name(self):
        for text, args in (
                ("play the song kesariya on youtube in brave", {"query": "kesariya", "browser": "brave"}),
                ("play kesariya, please", {"query": "kesariya"}),
                ("brave par kesariya song play karo", {"query": "kesariya", "browser": "brave"}),
                ("jee ne laga hoon song play karo", {"query": "jee ne laga hoon"}),
                ("kesariya gaana bajao", {"query": "kesariya"}),
                ("youtube par arijit ke gaane chalao", {"query": "arijit ke gaane"}),
                ("play despacito", {"query": "despacito"})):
            with self.subTest(text=text):
                self.assertEqual(_route(text), ("play_youtube", args))

    def test_what_isnt_a_song_stays_out(self):
        for text in ("workflow chalao", "chrome chalao", "gaana chalao", "play devils advocate", "play it cool",
                     "play on youtube", "brave par kesariya chalao"):     # "chalao" alone runs apps too: the model decides
            with self.subTest(text=text):
                self.assertNotEqual((_route(text) or ("",))[0], "play_youtube")
        self.assertEqual(_route("koi joke sunao")[0], "get_joke")
        self.assertEqual(_route("next song play karo")[0], "media_next_track")

    def test_track_moves_and_vague_asks_name_no_song(self):
        # found by bench/heldout/: "pichla song lagao" is the previous track
        for text in ("pichla song lagao", "agla gaana chalao", "koi song lagao", "ek gaana bajao", "play next"):
            with self.subTest(text=text):
                self.assertNotEqual((_route(text) or ("",))[0], "play_youtube")
        self.assertEqual(_route("play some jazz"), ("play_youtube", {"query": "some jazz"}))   # golden set
        self.assertEqual(_route("play a song by arijit"), ("play_youtube", {"query": "arijit"}))


class PreferredBrowserTests(unittest.TestCase):
    def _with(self, facts):
        return mock.patch("database.db_manager.db.get_all_memory", return_value=facts)

    def test_what_the_model_remembered_is_used(self):
        with self._with([("assistant_name", "Dodo"), ("preferred_browser_for_youtube", "Brave")]):
            self.assertEqual(media.preferred_media_browser(), "brave")

    def test_a_media_specific_wish_beats_a_general_one(self):
        with self._with([("preferred_browser", "Chrome"), ("song_browser", "brave browser please")]):
            self.assertEqual(media.preferred_media_browser(), "brave")
        with self._with([("default_browser", "Edge")]):
            self.assertEqual(media.preferred_media_browser(), "edge")

    def test_nothing_usable_is_none(self):
        with self._with([("browser_tabs", "too many"), ("favourite_colour", "blue")]):
            self.assertIsNone(media.preferred_media_browser())

    def test_the_skill_uses_a_named_browser_else_the_remembered_one(self):
        with mock.patch.object(WebManager, "play_on_youtube", return_value="ok") as play, \
                self._with([("preferred_browser_for_youtube", "Brave")]):
            media._skill_play_youtube(None, {"query": "kesariya"})
            media._skill_play_youtube(None, {"query": "kesariya", "browser": "Chrome"})
        self.assertEqual(play.call_args_list, [mock.call("kesariya", "brave"), mock.call("kesariya", "chrome")])


class PlayInBrowserTests(unittest.TestCase):
    URL = "https://www.youtube.com/watch?v=abc"

    def _play(self, browser, found):
        backend = mock.Mock()
        backend.find_browser.return_value = "C:/fake/brave.exe" if found else None
        with mock.patch("web.web_ops.HAS_YTDLP", True), \
                mock.patch.object(WebManager, "_ytdlp_first_result", return_value=self.URL), \
                mock.patch("automation.backends.get_backend", return_value=backend), \
                mock.patch("web.web_ops.webbrowser.open") as default:
            reply = WebManager.play_on_youtube("kesariya", browser)
        return reply, backend, default

    def test_the_video_opens_in_the_named_browser(self):
        reply, backend, default = self._play("brave", found=True)
        self.assertEqual(reply, "Playing kesariya on YouTube in Brave.")
        backend.launch_in_browser.assert_called_once_with("C:/fake/brave.exe", self.URL)
        default.assert_not_called()

    def test_a_missing_browser_says_so_and_uses_the_default(self):
        reply, _backend, default = self._play("brave", found=False)
        default.assert_called_once_with(self.URL)
        self.assertIn("Couldn't find brave installed", reply)

    def test_no_browser_is_the_old_behaviour(self):
        reply, backend, default = self._play(None, found=True)
        self.assertEqual(reply, "Playing kesariya on YouTube.")
        default.assert_called_once_with(self.URL)
        backend.find_browser.assert_not_called()


class SmallerRequestTests(unittest.TestCase):
    def _rules(self, text):
        brain = object.__new__(WaveBrain)
        with mock.patch.object(WaveBrain, "_recent_history", staticmethod(lambda limit=16: [])):
            return brain._rules_block(text)

    def test_a_topic_rule_goes_only_with_its_tools(self):
        song = self._rules("kesariya song play karo")
        self.assertNotIn("send_whatsapp_message already opens", song)
        self.assertNotIn("schedule_workflow is for", song)
        self.assertNotIn("create_calendar_event/list_calendar_events", song)
        self.assertIn("call get_live_information first", song)            # general rules always
        self.assertIn("call play_youtube", song)
        self.assertIn("send_whatsapp_message already opens", self._rules("Rohan ko whatsapp pe hi bhejo"))
        numbers = [line.split(".")[0] for line in song.splitlines()[1:]]
        self.assertEqual(numbers, [str(i) for i in range(1, len(numbers) + 1)])

    def test_if_the_tools_cant_be_told_every_rule_goes(self):
        brain = object.__new__(WaveBrain)
        with mock.patch.object(WaveBrain, "_tools_for", staticmethod(mock.Mock(side_effect=RuntimeError))):
            self.assertIn("schedule_workflow is for", brain._rules_block("anything"))

    def test_the_wording_pass_sends_only_the_tools_already_called(self):
        brain = object.__new__(WaveBrain)
        brain.model = "m"
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "kesariya chalao brave par"},
                    {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "1", "type": "function", "function": {"name": "play_youtube", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "1", "content": "Playing kesariya on YouTube in Brave."}]
        with mock.patch.object(WaveBrain, "_create_negotiated", lambda self, kwargs, effort: kwargs):
            wording = brain._chat_completion(messages, tool_choice="none", only_called_tools=True)
            normal = brain._chat_completion(messages)
            plain = brain._chat_completion(messages[:2], tool_choice="none", only_called_tools=True)
        self.assertEqual([t["function"]["name"] for t in wording["tools"]], ["play_youtube"])
        self.assertGreater(len(normal["tools"]), 5)
        self.assertNotIn("tools", plain)
        self.assertNotIn("tool_choice", plain)


class BudgetTests(unittest.TestCase):
    def test_tight_while_a_model_cools_down_and_a_little_after(self):
        brain = object.__new__(WaveBrain)
        self.assertFalse(brain.llm_budget_tight())                         # no failover state at all
        brain._model_cooldown = {"m": time.monotonic() + 20}
        self.assertTrue(brain.llm_budget_tight())
        brain._model_cooldown = {"m": time.monotonic() - 10}
        self.assertTrue(brain.llm_budget_tight())                          # ended 10 s ago
        brain._model_cooldown = {"m": time.monotonic() - 60}
        self.assertFalse(brain.llm_budget_tight())

    def test_the_planner_waits_while_tight(self):
        import planning
        brain = mock.Mock(llm_budget_tight=mock.Mock(return_value=True))
        with mock.patch.object(planning.planner, "propose_plan") as propose:
            self.assertIsNone(planning.try_plan_and_execute(brain, "open chrome and then play kesariya on youtube"))
        propose.assert_not_called()

    def test_the_rolling_summary_waits_while_tight(self):
        from brain import groq_core
        brain = object.__new__(WaveBrain)
        brain._model_cooldown = {"m": time.monotonic() + 20}
        with mock.patch.object(groq_core.rolling_summary, "schedule_update") as update:
            brain.get_dynamic_system_prompt("hello")
            update.assert_not_called()
            brain._model_cooldown = {}
            brain.get_dynamic_system_prompt("hello")
            update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
