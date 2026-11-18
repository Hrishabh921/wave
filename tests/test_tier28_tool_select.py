"""
tests/test_tier28_tool_select.py — never send the LLM more tools than it accepts.

Wave registers 180+ tools; Groq and OpenAI reject a request carrying more
than 128 of them, which made every reply that needed the LLM fail.
brain/tool_select.py now picks at most config.LLM_MAX_TOOLS (default 128)
per request. These tests check the limit holds on Wave's real registry,
that what a turn needs is always in the list (its own words, Hinglish
included; tools already called; the user's own skills; the everyday set),
and that WaveBrain's request paths actually use it.

    python -m unittest discover -s tests -p "test_tier28_tool_select.py"
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from brain import tool_select  # noqa: E402
from brain.groq_core import WaveBrain  # noqa: E402
from brain.skills_registry import skills  # noqa: E402
from settings.config import config  # noqa: E402

PROVIDER_LIMIT = 128      # Groq's and OpenAI's own maximum


def _msgs(*user_lines, tool_calls=()):
    msgs = [{"role": "system", "content": "You are Wave."}]
    for i, line in enumerate(user_lines):
        msgs.append({"role": "user", "content": line})
        if i < len(user_lines) - 1:
            msgs.append({"role": "assistant", "content": "ok"})
    if tool_calls:
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": "{}"}}
            for i, n in enumerate(tool_calls)]})
        msgs.append({"role": "tool", "tool_call_id": "c0", "content": "done"})
    return msgs


def _names(specs):
    return [s["function"]["name"] for s in specs]


def _spec(name, description):
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": {}}}}


class RealRegistryTests(unittest.TestCase):
    def test_wave_has_more_tools_than_the_provider_accepts(self):
        # The reason this tier exists - if it ever stops being true the
        # selection simply becomes a no-op, which is fine.
        self.assertGreater(len(skills.tool_specs()), PROVIDER_LIMIT)

    def test_every_request_stays_within_the_limit(self):
        for text in ("hi", "snap this window to the left", "wifi ki settings kholo", "kya haal hai"):
            with self.subTest(text=text):
                self.assertLessEqual(len(WaveBrain._tools_for(_msgs(text))), config.LLM_MAX_TOOLS)
        self.assertLessEqual(config.LLM_MAX_TOOLS, PROVIDER_LIMIT)

    def test_the_everyday_tools_are_real_skills(self):
        missing = [n for n in tool_select.ESSENTIAL_TOOLS + tool_select.CORE_TOOLS if n not in skills]
        self.assertEqual(missing, [])

    def test_a_request_stays_well_inside_groqs_free_tier(self):
        # Groq's free tier allows 8,000 tokens a minute, and a reply often
        # takes two requests. ~55 tokens per tool + ~1,100 of system prompt.
        import json
        for text in ("hi", "kal delhi mein mausam kaisa rahega", "set a reminder and a timer and play music"):
            with self.subTest(text=text):
                self.assertLess(len(json.dumps(WaveBrain._tools_for(_msgs(text)))), 20000)

    def test_what_the_words_ask_for_is_always_sent(self):
        for text, tool in (("snap this window to the left", "snap_window"),
                           ("wifi ki settings kholo", "open_wifi_settings"),
                           ("roshni max karo", "brightness_max"),
                           ("start a stopwatch", "start_stopwatch"),
                           ("gpu ka temperature batao", "get_gpu_status"),
                           ("schedule my backup workflow every morning", "schedule_workflow"),
                           ("empty the recycle bin", "empty_recycle_bin"),
                           ("open the bluetooth settings", "open_bluetooth_settings"),
                           ("volume thoda kam karo", "volume_down"),
                           ("mera battery kitna hai", "get_battery_status"),
                           ("remind me to call mom at 5", "set_reminder"),
                           ("convert 5 miles to km", "convert_units"),
                           ("what's on my calendar tomorrow", "list_calendar_events"),
                           ("close chrome", "close_app"),
                           ("lock my computer", "lock_pc"),
                           ("play some music on spotify", "play_on_spotify"),
                           ("take a screenshot", "take_screenshot"),
                           ("what's my ip address", "get_local_ip"),
                           ("add milk to my todo list", "add_todo"),
                           ("set a timer for 10 minutes", "set_timer"),
                           ("whatsapp pe mummy ko message bhejo", "send_whatsapp_message"),
                           ("kal delhi mein mausam kaisa rahega", "get_weather")):
            with self.subTest(text=text):
                self.assertIn(tool, _names(WaveBrain._tools_for(_msgs(text))))

    def test_a_follow_up_keeps_the_previous_requests_tools(self):
        self.assertIn("brightness_max", _names(WaveBrain._tools_for(_msgs("roshni max karo", "aur thoda"))))

    def test_tools_already_called_this_turn_stay_available(self):
        names = _names(WaveBrain._tools_for(_msgs("hi", tool_calls=["lap_stopwatch", "list_services"])))
        self.assertIn("lap_stopwatch", names)
        self.assertIn("list_services", names)

    def test_the_essentials_go_out_whatever_was_said(self):
        names = set(_names(WaveBrain._tools_for(_msgs("zzz qqq"))))
        self.assertTrue(set(tool_select.ESSENTIAL_TOOLS) <= names)


class SelectionRuleTests(unittest.TestCase):
    def setUp(self):
        self.specs = [_spec(f"tool_{i}", f"does thing number {i}") for i in range(40)]
        self.specs.append(_spec("open_garage_door", "Opens the garage door."))
        self.specs.append(_spec("my_user_skill", "Something the user added."))

    def test_under_the_limit_everything_goes_unchanged(self):
        self.assertEqual(tool_select.select_tool_specs(self.specs, _msgs("hi"), limit=100), self.specs)
        self.assertEqual(tool_select.select_tool_specs(self.specs, _msgs("hi"), limit=0), self.specs)

    def test_over_the_limit_only_what_the_turn_needs_goes_in_registry_order(self):
        out = tool_select.select_tool_specs(self.specs, _msgs("open the garage door"), limit=10,
                                            always=["my_user_skill"])
        names = _names(out)
        self.assertLessEqual(len(out), 10)
        self.assertIn("open_garage_door", names)
        self.assertIn("my_user_skill", names)
        self.assertNotIn("tool_7", names)          # Tier 29: no padding with tools nothing asked for
        order = _names(self.specs)
        self.assertEqual(names, sorted(names, key=order.index))

    def test_small_talk_carries_only_the_essentials(self):
        names = _names(WaveBrain._tools_for(_msgs("tum kaise ho dost")))
        self.assertLessEqual(len(names), len(tool_select.ESSENTIAL_TOOLS) + 4)

    def test_called_tools_win_even_over_the_limit_pressure(self):
        out = tool_select.select_tool_specs(self.specs, _msgs("open the garage door", tool_calls=["tool_39"]), limit=3)
        self.assertIn("tool_39", _names(out))

    def test_sdk_style_message_objects_work_too(self):
        class Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)
        msgs = [Obj(role="user", content="open the garage door"),
                Obj(role="assistant", content=None, tool_calls=[Obj(function=Obj(name="tool_5", arguments="{}"))])]
        names = _names(tool_select.select_tool_specs(self.specs, msgs, limit=4))
        self.assertIn("tool_5", names)
        self.assertIn("open_garage_door", names)

    def test_multimodal_user_content_is_read(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "open the garage door"},
                                              {"type": "image_url", "image_url": {"url": "data:,"}}]}]
        self.assertIn("open_garage_door", _names(tool_select.select_tool_specs(self.specs, msgs, limit=3)))


class RequestPathTests(unittest.TestCase):
    """WaveBrain's own Groq-facing calls go through the selection."""

    class _Client:
        def __init__(self):
            self.kwargs = []
            outer = self

            class Completions:
                def create(self, **kw):
                    outer.kwargs.append(kw)
                    return "response"

            class Chat:
                completions = Completions()
            self.chat = Chat()

    def test_chat_completion_and_its_stream_send_at_most_the_limit(self):
        fake = object.__new__(WaveBrain)          # the real methods, no provider/network setup
        fake.model = "m"
        fake.provider = None
        fake.client = self._Client()
        fake._model_cooldown = {}
        fake._chat_completion(_msgs("snap this window to the left"))
        fake._chat_completion_stream(_msgs("snap this window to the left"))
        self.assertEqual(len(fake.client.kwargs), 2)
        for kw in fake.client.kwargs:
            self.assertLessEqual(len(kw["tools"]), config.LLM_MAX_TOOLS)
            self.assertIn("snap_window", _names(kw["tools"]))


if __name__ == "__main__":
    unittest.main()
