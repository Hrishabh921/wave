"""Tier 45 - everyday commands said in other words (brain/semantic_intents.py).

ResolveTests / GuardTests: the word checks that make an embedding match
safe to act on - pure functions, no model.
MatchTests: SemanticIntents.match() with a fake embedder whose opposites
read alike, the way all-MiniLM-L6-v2's do.
TableTests: every example is a registered, low-risk, argument-free tool and
passes its own checks.
RealModelTests: the measured cases on the real sentence model (skipped
when it isn't downloaded on this machine).
WiredIntoThinkTests: think()/think_stream() run a match through
_dispatch_tool() without a model call, after the fast path, and not at all
when switched off.
"""
import os
import sys
import threading
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: E402,F401
from _fake_openai import Reply, completion  # noqa: E402
from test_phase4_brain import BrainBase  # noqa: E402

import brain.groq_core  # noqa: E402,F401  (registers the skills)
from brain import semantic_intents as si  # noqa: E402
from brain import tiers  # noqa: E402
from brain.skills_registry import skills  # noqa: E402
from settings.config import config  # noqa: E402


def _on(testcase):
    saved = config.SEMANTIC_INTENTS
    config.SEMANTIC_INTENTS = True
    testcase.addCleanup(setattr, config, "SEMANTIC_INTENTS", saved)


class ResolveTests(unittest.TestCase):
    def check(self, family, text, expected):
        self.assertEqual(si.resolve(family, text), expected, f"{family}: {text!r}")

    def test_the_words_decide_the_direction_the_embedding_cant(self):
        for family, text, expected in [
            ("volume", "increase the volume a little", "volume_up"),
            ("volume", "reduce the volume", "volume_down"),
            ("volume", "bring the volume down", "volume_down"),
            ("volume", "awaaz kam karo", "volume_down"),
            ("volume", "volume thoda tez karo", "volume_up"),
            ("volume", "turn off the sound", "mute_volume"),
            ("volume", "shut the audio off", "mute_volume"),
            ("volume", "turn audio back on", "unmute_volume"),
            ("brightness", "lower the brightness", "brightness_down"),
            ("brightness", "increase screen brightness", "brightness_up"),
            ("media", "skip to the next track", "media_next_track"),
            ("media", "go back a song", "media_previous_track"),
            ("media", "play the previous track again", "media_previous_track"),
            ("media", "pause the video", "media_play_pause"),
            ("media", "continue the song", "media_play_pause"),
        ]:
            self.check(family, text, expected)

    def test_a_complaint_means_the_opposite_of_its_word(self):
        self.check("volume", "it's too loud", "volume_down")
        self.check("volume", "the music is way too loud", "volume_down")
        self.check("volume", "the sound is too low", "volume_up")
        self.check("volume", "louder please, i can barely hear it", "volume_up")
        self.check("brightness", "the screen is too dark", "brightness_up")
        self.check("brightness", "the screen is too dim", "brightness_up")
        self.check("brightness", "the screen is too bright", "brightness_down")
        self.check("brightness", "my eyes hurt, make the screen darker", "brightness_down")

    def test_no_cue_or_two_conflicting_ones_runs_nothing(self):
        self.check("volume", "the sound of music", None)
        self.check("volume", "turn the volume back down", None)
        self.check("volume", "shut down the sound", None)
        self.check("brightness", "brightness status", None)
        self.check("media", "play some music", None)

    def test_a_word_the_tool_cant_take_sends_it_to_the_model(self):
        self.check("get_weather", "what's the weather in paris", None)
        self.check("get_weather", "delhi ka mausam batao", None)
        self.check("get_news", "cricket ki khabar sunao", None)
        self.check("get_current_time", "what's the time complexity of quicksort", None)
        self.check("get_current_date", "what will the date be next friday", None)
        self.check("volume", "max volume please", None)
        self.check("volume", "it's not loud enough", None)
        self.check("take_screenshot", "record my screen", None)
        self.check("list_todos", "show all my tasks including completed ones", None)
        self.check("list_reminders", "i need to remember to buy milk", None)

    def test_the_familys_own_words_must_be_there(self):
        self.check("volume", "turn it down", None)
        self.check("media", "skip it", None)
        self.check("take_screenshot", "take a photo", None)
        self.check("take_screenshot", "what's on my screen", None)
        self.check("brightness", "the light is too bright", None)
        self.check("get_current_time", "it's late", None)

    def test_sentences_that_ask_for_something_else(self):
        self.check("get_clipboard", "copy this to clipboard", None)
        self.check("list_reminders", "what do you remember about me", None)
        self.check("get_current_time", "is it time", None)
        self.check("get_battery_status", "how much do you charge", None)
        self.check("daily_briefing", "what a day", None)

    def test_a_yes_no_question_never_changes_anything(self):
        self.check("volume", "is it too loud", None)
        self.check("volume", "is the sound muted", None)
        # ...but a question is how one asks for information.
        self.check("get_battery_status", "is my laptop charged", "get_battery_status")
        self.check("get_weather", "is it cold outside", "get_weather")

    def test_single_tool_families(self):
        self.check("take_screenshot", "save a snapshot of my screen", "take_screenshot")
        self.check("list_todos", "what tasks are pending", "list_todos")
        self.check("get_clipboard", "what text did i copy", "get_clipboard")
        self.check("get_news", "read me the news", "get_news")
        self.check("minimize_all_windows", "hide everything on screen", "minimize_all_windows")


class GuardTests(unittest.TestCase):
    def test_sentences_never_tried(self):
        for text in ["don't skip this song", "awaaz mat badhao", "should i lower the brightness",
                     "what happens if i mute the volume", "volume up and take a screenshot",
                     "pause the music, then lock the pc", "how do i take a screenshot",
                     "how to increase volume on windows", "why is the screen so dark",
                     "screenshot kaise lete hain", "set volume to 50", "what is a screenshot",
                     "explain how brightness works"]:
            self.assertTrue(si.sentence_blocked(text.lower()), text)

    def test_ordinary_commands_are_tried(self):
        for text in ["make it louder", "the screen is too dark", "move on to the next song",
                     "how hot is it outside", "how late is it"]:
            self.assertFalse(si.sentence_blocked(text), text)


# A fake sentence model: opposites land on the same point, as they nearly do
# for all-MiniLM-L6-v2 - so only the words can tell up from down.
_VOLUME, _BRIGHT, _SHOT, _OTHER = np.eye(4)
_VECTORS = {
    "make it louder": _VOLUME, "make it quieter": _VOLUME,
    "make the screen brighter": _BRIGHT, "capture my screen": _SHOT,
    "turn the volume down a notch": _VOLUME * 0.95 + _OTHER * 0.3,
    "it's too loud": _VOLUME,
    "louder please": _VOLUME * 0.55 + _OTHER * 0.835,              # 0.55 similar: too far
    "volume up on the display": _VOLUME * 0.7 + _BRIGHT * 0.68,    # two families neck and neck
    "make the bass louder": _VOLUME,
}
_EXAMPLES = {"volume_up": ("make it louder",), "volume_down": ("make it quieter",),
             "brightness_up": ("make the screen brighter",), "take_screenshot": ("capture my screen",)}


def _fake_embed(text):
    return list(_VECTORS.get(text, _OTHER))


class MatchTests(unittest.TestCase):
    def setUp(self):
        _on(self)
        self.calls = []

        def embed(text):
            self.calls.append(text)
            return _fake_embed(text)
        self.m = si.SemanticIntents(embed=embed, examples=_EXAMPLES)

    def test_the_family_comes_from_the_embedding_the_direction_from_the_words(self):
        self.assertEqual(self.m.match("turn the volume down a notch")[0], "volume_down")
        self.assertEqual(self.m.match("it's too loud")[0], "volume_down")
        self.assertEqual(self.m.match("make it louder")[0], "volume_up")

    def test_too_far_or_too_close_to_call_goes_to_the_model(self):
        self.assertIsNone(self.m.match("louder please"))
        self.assertIsNone(self.m.match("volume up on the display"))

    def test_the_words_still_have_to_fit(self):
        self.assertIsNone(self.m.match("make the bass louder"))   # no volume word knows "bass"

    def test_never_embedded_when_the_sentence_rules_it_out(self):
        for text in ["louder", "don't make it louder", "आवाज़ बढ़ाओ", "make it louder " * 20,
                     "make it louder and brighter", ""]:
            self.assertIsNone(self.m.match(text), text)
        self.assertEqual(self.calls, [])

    def test_switched_off(self):
        config.SEMANTIC_INTENTS = False
        self.assertIsNone(self.m.match("make it louder"))
        self.assertEqual(self.calls, [])

    def test_no_sentence_model_means_it_quietly_stays_off(self):
        m = si.SemanticIntents(embed=lambda t: None, examples=_EXAMPLES)
        self.assertIsNone(m.match("make it louder"))
        self.assertIsNone(m.match("make it quieter"))
        self.assertTrue(m._failed)

        def broken(text):
            raise RuntimeError("model crashed")
        m2 = si.SemanticIntents(embed=broken, examples=_EXAMPLES)
        self.assertIsNone(m2.match("make it louder"))

    def test_a_turn_never_waits_for_the_model_to_finish_loading(self):
        self.m._lock.acquire()     # a warm-up still loading
        try:
            self.assertIsNone(self.m.match("make it louder"))
        finally:
            self.m._lock.release()
        self.assertEqual(self.m.match("make it louder")[0], "volume_up")

    def test_warm_up_loads_in_the_background_once(self):
        self.m.warm_up()
        for t in threading.enumerate():
            if t.name == "SemanticIntents":
                t.join(5)
        self.assertIsNotNone(self.m._table)
        n = len(self.calls)
        self.m.match("make it louder")
        self.assertEqual(len(self.calls), n + 1)   # only the sentence, not the examples again


class TableTests(unittest.TestCase):
    def test_every_intent_is_a_registered_low_risk_tool_without_arguments(self):
        for intent in si.EXAMPLES:
            spec = skills.get_spec(intent)
            self.assertIsNotNone(spec, intent)
            self.assertLessEqual(tiers.get_tier(intent), tiers.T1, intent)
            self.assertFalse(skills.requires_confirm(intent), intent)
            params = spec["function"].get("parameters", {})
            self.assertFalse(params.get("required"), intent)

    def test_every_example_passes_its_own_checks(self):
        for intent, phrases in si.EXAMPLES.items():
            for phrase in phrases:
                self.assertFalse(si.sentence_blocked(phrase), phrase)
                self.assertEqual(si.resolve(si._family(intent), phrase), intent, phrase)


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
class RealModelTests(unittest.TestCase):
    """Measured on the real model: paraphrases the fast path doesn't know,
    and sentences that must never run anything."""

    @classmethod
    def setUpClass(cls):
        cls._saved = (config.SEMANTIC_INTENTS, config.SEMANTIC_MEMORY_ENABLED)
        config.SEMANTIC_INTENTS = config.SEMANTIC_MEMORY_ENABLED = True
        cls.m = si.SemanticIntents()

    @classmethod
    def tearDownClass(cls):
        config.SEMANTIC_INTENTS, config.SEMANTIC_MEMORY_ENABLED = cls._saved

    def test_paraphrases(self):
        for text, intent in [
            ("increase the volume a little", "volume_up"), ("raise the sound", "volume_up"),
            ("the sound is too low", "volume_up"), ("reduce the volume", "volume_down"),
            ("bring the volume down", "volume_down"), ("the music is way too loud", "volume_down"),
            ("quieter please", "volume_down"), ("turn off the sound", "mute_volume"),
            ("go silent", "mute_volume"), ("turn audio back on", "unmute_volume"),
            ("brighten the screen", "brightness_up"), ("the screen is too dim", "brightness_up"),
            ("reduce screen brightness", "brightness_down"), ("darken the screen", "brightness_down"),
            ("the screen hurts my eyes", "brightness_down"), ("save a snapshot of my screen", "take_screenshot"),
            ("grab my screen", "take_screenshot"), ("pause the video", "media_play_pause"),
            ("resume playback", "media_play_pause"), ("go to the next song", "media_next_track"),
            ("change the song", "media_next_track"), ("previous song please", "media_previous_track"),
            ("play the last track again", "media_previous_track"), ("what are my reminders", "list_reminders"),
            ("what's my day like", "daily_briefing"), ("is it raining outside", "get_weather"),
            ("tell me the time", "get_current_time"), ("what's the date today", "get_current_date"),
            ("charge kitna hai", "get_battery_status"), ("hide everything on screen", "minimize_all_windows"),
            ("latest headlines", "get_news"), ("what tasks are pending", "list_todos"),
            ("what text did i copy", "get_clipboard"),
        ]:
            hit = self.m.match(text)
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit[0], intent, text)

    def test_nothing_runs_for_these(self):
        for text in [
            "i took a screenshot yesterday", "my friend muted me in the group chat", "the concert was so loud",
            "i love loud music", "my battery died on the way home", "i like dark themes",
            "the weather was nice yesterday", "i read the news this morning", "remind me to call mom",
            "add buy milk to my todo list", "what's the time complexity of quicksort", "date ideas for the weekend",
            "i have a date tonight", "skip the small talk", "copy that", "play some music",
            "play despacito on youtube", "what's the weather in paris", "what time is it in london",
            "news about cricket", "cricket ki khabar sunao", "volume full kar do", "max volume please",
            "record my screen", "what's on my screen", "take a photo", "close all windows", "lock my computer",
            "set a reminder for tomorrow", "what do you remember about me", "copy this to clipboard",
            "it's late", "is it time", "how much do you charge", "the light is too bright", "turn it down",
            "music band karo", "switch to dark mode", "turn off the screen", "wapas kar do", "pichla tab",
            "aaj mausam accha hai na", "tumhara favorite khana kya hai", "brightness status",
        ]:
            self.assertIsNone(self.m.match(text), text)


class _Stub:
    def __init__(self, result):
        self.result, self.seen = result, []

    def match(self, text):
        self.seen.append(text)
        return self.result


class WiredIntoThinkTests(BrainBase):
    """The stub answers with a read-only intent: a real volume_up would
    move this machine's volume."""

    def setUp(self):
        super().setUp()
        _on(self)
        self.stub = _Stub(("get_current_date", 0.9))
        patcher = mock.patch.object(self.gc, "semantic_intents", self.stub)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_match_runs_through_the_gate_without_a_model_call(self):
        reply = self.brain.think("which day of the month is it")
        self.assertEqual(len(self.srv.posts()), 0)
        self.assertNotIn("tool available", reply.lower())
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.source, row.status), ("get_current_date", "semantic_intent", "ok"))

    def test_think_stream_too(self):
        chunks = list(self.brain.think_stream("which day of the month is it"))
        self.assertEqual(len(self.srv.posts()), 0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(self.audit.recent(1)[0].source, "semantic_intent")

    def test_no_match_goes_to_the_model_as_before(self):
        self.stub.result = None
        self.srv.script = [Reply(200, completion("Sure."))]
        self.assertEqual(self.brain.think("which day of the month is it"), "Sure.")
        self.assertEqual(len(self.srv.posts()), 1)

    def test_the_fast_path_comes_first(self):
        config.INTENT_ROUTER_ENABLED = True
        self.brain.think("what time is it")
        self.assertEqual(self.stub.seen, [])
        self.assertEqual(self.audit.recent(1)[0].source, "intent_router")

    def test_an_attachment_is_never_second_guessed(self):
        self.assertIsNone(self.brain._semantic_intent("which day of the month is it",
                                                      {"kind": "document", "text": "x"}))
        self.assertEqual(self.stub.seen, [])


if __name__ == "__main__":
    unittest.main()
