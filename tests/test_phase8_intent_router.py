"""Phase 8 / Tier 2 item 4 - the fast deterministic intent layer.

RouteUnitTests exercises brain/intent_router.py entirely on its own - no
brain, no fake server, just text in and (skill, args) out. This is where
most of the coverage belongs: it's a pure function, so every rule and
edge case can be checked directly and quickly.

WiredIntoThinkTests then checks the three things a unit test of route()
alone can't: that WaveBrain.think()/think_stream() actually consult it
before the LLM, that a confirm-gated match (close_app) is STILL gated
exactly like an LLM-chosen call would be, and that INTENT_ROUTER_ENABLED
really does switch the whole thing off. It reuses BrainBase from
test_phase4_brain.py rather than duplicating that setup.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _phase4_support as S  # noqa: F401
from _fake_openai import Reply, completion, tool_call

from brain import intent_router as ir
from settings.config import config
# test_phase4_brain installs fake pyautogui/pywhatkit modules as an import
# side effect (brain.groq_core imports the real ones otherwise, which
# aren't installed on this headless box) - import it, and BrainBase with
# it, before anything that would import brain.groq_core directly.
from test_phase4_brain import BrainBase
import brain.groq_core  # noqa: F401  (import side effect: populates `skills` via @skill(...) decorators -
                         # RouteUnitTests must not depend on some OTHER test module happening to import this first)
from brain.skills_registry import skills


class RouteUnitTests(unittest.TestCase):
    """Pure brain/intent_router.py tests - no I/O, no brain instance."""

    def assertRoutes(self, text, skill, args=None):
        routed = ir.route(text)
        self.assertIsNotNone(routed, f"expected {text!r} to route, got None")
        self.assertEqual(routed.skill, skill)
        self.assertEqual(routed.args, args or {})

    def assertNoRoute(self, text):
        self.assertIsNone(ir.route(text), f"expected {text!r} NOT to route, got a match")

    # ---------- no-arg phrase/exact rules ----------
    def test_undo_phrases_including_hinglish(self):
        for text in ("undo", "undo that", "undo it", "undo last action", "wapas kar do", "undo karo"):
            self.assertRoutes(text, "undo_last_action")

    def test_close_window_handles_the_article(self):
        for text in ("close window", "close the window", "close current window",
                     "close the current window", "close this window", "close all windows"):
            self.assertRoutes(text, "close_current_window")

    def test_close_tab_handles_the_article(self):
        for text in ("close tab", "close the tab", "close current tab", "close the current tab"):
            self.assertRoutes(text, "close_current_tab")

    def test_delete_screenshot_beats_take_screenshot(self):
        """Ordering regression: 'delete ... screenshot' must not be caught
        by the broader 'screenshot' substring meant for take_screenshot."""
        self.assertRoutes("please delete the screenshot", "delete_latest_screenshot")
        self.assertRoutes("delete my last screenshot", "delete_latest_screenshot")

    def test_take_screenshot(self):
        for text in ("take a screenshot", "take screenshot", "capture screen", "screenshot",
                     "screenshot le lo"):
            self.assertRoutes(text, "take_screenshot")

    def test_read_screen_text(self):
        self.assertRoutes("what's on my screen", "read_screen_text")
        self.assertRoutes("read the screen", "read_screen_text")

    def test_pc_info(self):
        self.assertRoutes("pc info", "get_pc_info")
        self.assertRoutes("how's my pc", "get_pc_info")

    def test_clipboard(self):
        self.assertRoutes("what's in my clipboard", "get_clipboard")
        self.assertRoutes("read my clipboard", "get_clipboard")

    def test_lock_pc_including_hinglish(self):
        for text in ("lock my pc", "lock the screen", "pc lock kar do"):
            self.assertRoutes(text, "lock_pc")

    def test_media_controls(self):
        self.assertRoutes("pause", "media_play_pause")
        self.assertRoutes("resume", "media_play_pause")
        self.assertRoutes("pause the music", "media_play_pause")
        self.assertRoutes("next song", "media_next_track")
        self.assertRoutes("previous track", "media_previous_track")

    def test_media_play_pause_beats_play_prefix(self):
        """'play pause' must hit the exact-phrase media rule, not the
        generic 'play <query>' -> play_youtube prefix rule."""
        self.assertRoutes("play pause", "media_play_pause")

    def test_volume_including_hinglish(self):
        self.assertRoutes("volume up", "volume_up")
        self.assertRoutes("louder", "volume_up")
        self.assertRoutes("awaaz badhao", "volume_up")
        self.assertRoutes("volume down", "volume_down")
        self.assertRoutes("quieter", "volume_down")
        self.assertRoutes("awaaz kam karo", "volume_down")

    def test_mute_exact_vs_substring(self):
        """Tier 14: toggle_mute is retired, split into one-directional
        mute_volume/unmute_volume rules - see their own comment in
        brain/intent_router.py for why "unmute" (checked first) can never
        land on mute_volume even though it contains "mute" as a literal
        substring."""
        self.assertRoutes("mute", "mute_volume")
        self.assertRoutes("unmute", "unmute_volume")
        self.assertRoutes("mute the sound", "mute_volume")
        self.assertRoutes("unmute the sound", "unmute_volume")

    def test_toggle_hardware_needs_both_a_device_and_a_verb(self):
        # Tier 44: one radio and a clear on/off goes to the tool that really
        # switches it; the hotspot stays toggle_hardware.
        self.assertRoutes("turn on wifi", "turn_wifi_on", {})
        self.assertRoutes("switch off bluetooth", "turn_bluetooth_off", {})
        self.assertRoutes("switch on the hotspot", "toggle_hardware", {"instruction": "switch on the hotspot"})
        self.assertNoRoute("wifi is great")          # device word alone, no verb -> no toggle_hardware match
        # ("turn on the lamp" DOES still route - to open_app, via the separate
        # open_app prefix rule below it in the table. That's intentional, not
        # this rule's concern - see test_turn_on_app_name_falls_back_to_open_app_when_not_hardware.)

    # ---------- prefix / extraction rules ----------
    def test_open_app_extracts_the_name(self):
        self.assertRoutes("open chrome", "open_app", {"app_name": "chrome"})
        self.assertRoutes("start spotify", "open_app", {"app_name": "spotify"})
        self.assertRoutes("launch vs code", "open_app", {"app_name": "vs code"})

    def test_turn_on_app_name_falls_back_to_open_app_when_not_hardware(self):
        self.assertRoutes("turn on the projector", "open_app", {"app_name": "the projector"})

    def test_open_with_nothing_after_it_does_not_route(self):
        self.assertNoRoute("open")
        self.assertNoRoute("open ")

    def test_close_app_extracts_the_name(self):
        self.assertRoutes("close notepad", "close_app", {"app_name": "notepad"})

    def test_play_youtube_extracts_the_query(self):
        self.assertRoutes("play shape of you", "play_youtube", {"query": "shape of you"})

    def test_search_google_vs_play_youtube_vs_named_browser(self):
        self.assertRoutes("search for best pizza near me", "search_google", {"query": "best pizza near me"})
        self.assertRoutes("search cats on youtube", "play_youtube", {"query": "cats"})
        self.assertNoRoute("search for restaurants on chrome")   # needs open_website - left to the LLM

    def test_create_folder_with_and_without_a_name(self):
        self.assertRoutes("create a folder called project notes", "create_folder",
                          {"folder_name": "project notes"})
        self.assertRoutes("make a folder", "create_folder", {"folder_name": "New Folder"})

    # ---------- Tier 8: Devanagari-script coverage ----------
    # Every phrase below previously fell through to "chat" - see
    # bench/golden/intents_hi.jsonl's own notes for the gap each one
    # documents (that file is the authoritative spec these mirror).
    def test_devanagari_volume_mute_lock_screenshot_undo(self):
        self.assertRoutes("आवाज़ बढ़ाओ", "volume_up")
        self.assertRoutes("आवाज़ कम करो", "volume_down")
        self.assertRoutes("म्यूट करो", "mute_volume")
        self.assertRoutes("कंप्यूटर लॉक करो", "lock_pc")
        self.assertRoutes("स्क्रीनशॉट लो", "take_screenshot")
        self.assertRoutes("यह पूर्ववत करो", "undo_last_action")

    def test_media_previous_track_had_zero_phrase_in_any_script_before_tier_8(self):
        self.assertRoutes("पिछला गाना", "media_previous_track")
        self.assertRoutes("pichla gaana", "media_previous_track")

    # ---------- Tier 8: verb-FINAL open/close phrasing (_suffix_arg) ----------
    # Hindi/Hinglish puts the verb last ("chrome kholo" = lit. "chrome
    # open"), where English puts it first ("open chrome") - open_app/
    # close_app now match both shapes via _prefix_or_suffix_arg.
    def test_open_app_matches_verb_final_hinglish_and_devanagari(self):
        self.assertRoutes("chrome khol do", "open_app", {"app_name": "chrome"})
        self.assertRoutes("spotify kholo", "open_app", {"app_name": "spotify"})
        self.assertRoutes("क्रोम खोलो", "open_app", {"app_name": "क्रोम"})

    def test_close_app_matches_verb_final_hinglish_and_devanagari(self):
        self.assertRoutes("notepad band karo", "close_app", {"app_name": "notepad"})
        self.assertRoutes("नोटपैड बंद करो", "close_app", {"app_name": "नोटपैड"})

    def test_open_close_prefix_form_still_wins_unaffected_by_the_new_suffix_path(self):
        """Regression: adding the verb-final suffix path must not disturb
        the original, far more common verb-first English form."""
        self.assertRoutes("open chrome", "open_app", {"app_name": "chrome"})
        self.assertRoutes("close notepad", "close_app", {"app_name": "notepad"})

    def test_suffix_arg_with_nothing_before_the_verb_does_not_route(self):
        self.assertNoRoute("khol do")
        self.assertNoRoute("band karo")
        self.assertNoRoute("खोलो")

    def test_mute_volume_devanagari_is_not_shadowed_by_the_close_app_suffix(self):
        """'आवाज़ बंद करो' ends in the exact same ' बंद करो' close_app's
        new suffix rule looks for, but mute_volume (Tier 14's replacement
        for the retired toggle_mute) is earlier in _RULES and must win -
        this should mute, not try to close_app('आवाज़')."""
        self.assertRoutes("आवाज़ बंद करो", "mute_volume")

    # ---------- things that must NOT route ----------
    def test_conversational_text_does_not_route(self):
        # Tier 17: "tell me a joke" used to belong in this list - it's now
        # a deliberate get_joke fast-path match (see
        # tests/test_tier17_intent_router_expansion.py), so it moved out
        # rather than being a regression here.
        for text in ("hello", "how are you", "what's the weather like",
                     "remember my favorite color is blue", "", "   "):
            self.assertNoRoute(text)

    def test_compound_commands_are_left_to_the_llm(self):
        """Regression: these used to match a SUBSTRING (e.g. 'pc info')
        and silently drop the rest of a multi-part request. A compound
        request needs the governed tool-calling loop's multiple rounds
        (see brain/groq_core.py's _ToolLoopGovernor), not a single
        skill call."""
        for text in (
            "check my pc info, then remind me to check battery in an hour",
            "open chrome and then play some music",
            "take a screenshot after that email it to me",
        ):
            self.assertNoRoute(text)

    def test_and_inside_ordinary_content_is_not_treated_as_compound(self):
        """Only sequencing words bail out - plain 'and' inside a query
        (a song/search term, say) must not lose fast-path handling."""
        self.assertRoutes("play salt and pepper", "play_youtube", {"query": "salt and pepper"})

    def test_negated_phrasing_does_not_route(self):
        """Regression: a _contains/_prefix_arg rule matches the PHRASE
        regardless of what's around it - without this guard, "don't lock
        the pc" and "I dont want to take a screenshot right now" used to
        route to lock_pc/take_screenshot and do the exact opposite of
        what was said."""
        for text in (
            "don't lock the pc", "I dont want to take a screenshot right now",
            "please don't mute the volume", "do not close this window",
        ):
            self.assertNoRoute(text)

    def test_trailing_standalone_mat_does_not_route(self):
        """Regression: 'mat' trailing the WHOLE phrase ('awaaz badhao
        mat') is also valid Hindi negation, but every pre-existing
        _NEGATION_MARKERS entry only covers 'mat' leading into/attached
        to a verb ('mat karo'/'mat kar') - this used to incorrectly fire
        volume_up (see bench/golden/intents_hinglish.jsonl's own note on
        this exact row). Devanagari 'मत' is covered the same way, any
        position, since it's always its own space-delimited word."""
        self.assertNoRoute("awaaz badhao mat")
        self.assertNoRoute("पीसी लॉक मत करो")

    def test_mat_as_part_of_another_word_is_not_treated_as_negation(self):
        """Word-boundary safety, same reasoning as _PREFIX_ARG_IDIOM_STARTS
        elsewhere in this file: a bare substring check for 'mat' would
        wrongly fire on ordinary words that merely contain those letters
        ('automated', 'format', 'climate', ...)."""
        self.assertRoutes("take a screenshot, it's for the automated report", "take_screenshot")

    def test_hypothetical_and_question_phrasing_does_not_route(self):
        """Regression: "should I lock the pc" and "what happens if I lock
        the pc" used to lock the pc in response to a QUESTION about
        locking it, never an instruction to do so."""
        for text in (
            "should I lock the pc or not", "what happens if I lock the pc",
            "what if I take a screenshot right now", "do I need to mute the volume",
        ):
            self.assertNoRoute(text)

    def test_polite_phrasing_of_a_real_command_still_routes(self):
        """The negation/hypothetical guard must not over-correct: "can
        you .../could you ..." is the normal polite phrasing of a direct
        request, not a real question, and should keep fast-pathing."""
        self.assertRoutes("can you lock the pc", "lock_pc")
        self.assertRoutes("could you take a screenshot", "take_screenshot")
        self.assertRoutes("please mute the volume", "mute_volume")

    def test_conditional_phrasing_does_not_route(self):
        """Regression: "lock the pc after I leave in 10 minutes" used to
        lock it immediately instead of respecting the "after I leave"
        condition - exactly the kind of request that needs the LLM (or a
        reminder/scheduling skill), not an instant action."""
        for text in ("lock the pc after I leave in 10 minutes", "mute the volume before the call starts"):
            self.assertNoRoute(text)

    def test_common_verb_idioms_do_not_route_as_open_close_play(self):
        """Regression: 'open'/'close'/'play' as the first word of a
        message is also how several everyday idioms start, with no app
        or query in sight - these used to route to open_app/close_app/
        play_youtube with a nonsense app_name/query."""
        for text in (
            "open to suggestions here", "close enough, lets go with that",
            "close the deal with the client", "play it cool with the new manager",
            "play devils advocate here",
        ):
            self.assertNoRoute(text)

    # ---------- every matched skill must be a real, registered skill ----------
    def test_every_routable_skill_name_is_actually_registered(self):
        """Guards against drift: if a skill this router names is ever
        renamed in brain/groq_core.py, this fails loudly here instead of
        intent_router.py silently routing to a name _dispatch_tool() no
        longer recognizes."""
        sample_inputs = [
            "undo", "close window", "close tab", "delete the screenshot", "take a screenshot",
            "read the screen", "pc info", "what's in my clipboard", "lock my pc", "pause",
            "next song", "previous song", "volume up", "volume down", "mute",
            "turn on wifi", "open chrome", "close notepad", "play shape of you",
            "search for pizza", "create a folder",
        ]
        registered = set(skills.names()) if hasattr(skills, "names") else None
        for text in sample_inputs:
            routed = ir.route(text)
            self.assertIsNotNone(routed, f"{text!r} should have routed")
            if registered is not None:
                self.assertIn(routed.skill, registered,
                              f"intent_router routes {text!r} to {routed.skill!r}, which isn't registered")
            else:
                self.assertIsNotNone(skills.get_handler(routed.skill),
                                     f"intent_router routes {text!r} to {routed.skill!r}, which isn't registered")


class WiredIntoThinkTests(BrainBase):
    """think()/think_stream() actually consulting the router, with the
    confirm-gate and INTENT_ROUTER_ENABLED behaving as documented.

    BrainBase.setUp() (test_phase4_brain.py) deliberately sets
    INTENT_ROUTER_ENABLED = False, so ITS OWN AI-path tests aren't
    affected by the router - exactly the opposite of what this class
    needs, since testing that the router actually fires is the whole
    point here. Re-enabled right after super().setUp(); BrainBase's own
    cleanup still restores the true pre-test original value afterward."""

    def setUp(self):
        super().setUp()
        config.INTENT_ROUTER_ENABLED = True

    def test_simple_command_never_touches_the_llm(self):
        # No script needed - a round-trip to self.srv would mean the test
        # itself is broken (nothing scripted to answer it).
        reply = self.brain.think("volume up")
        self.assertEqual(len(self.srv.posts()), 0)
        self.assertNotIn("tool available", reply.lower())

    def test_confirm_gated_match_still_asks_first(self):
        """close_app is confirm=True - matching it via the router must
        arm pending_action and block, exactly like an LLM tool_call would,
        not run it silently just because a model didn't choose it."""
        reply = self.brain.think("close notepad")
        self.assertEqual(len(self.srv.posts()), 0)                       # router handled it, no LLM call
        self.assertIsNotNone(self.brain.pending_action)
        self.assertEqual(self.brain.pending_action["tool"], "close_app")
        self.assertEqual(self.brain.pending_action["args"], {"app_name": "notepad"})
        self.assertEqual(self.brain.pending_action["source"], "intent_router")
        self.assertEqual(self.audit.recent(1)[0].status, "blocked")
        self.assertEqual(self.audit.recent(1)[0].source, "intent_router")
        self.assertIn("confirm", reply.lower())

    def test_disabling_the_router_restores_the_old_all_through_the_llm_behavior(self):
        config.INTENT_ROUTER_ENABLED = False
        try:
            self.srv.script = [Reply(200, completion("Turning it up now."))]
            reply = self.brain.think("volume up")
            self.assertEqual(reply, "Turning it up now.")
            self.assertEqual(len(self.srv.posts()), 1)
        finally:
            config.INTENT_ROUTER_ENABLED = True

    def test_compound_command_still_reaches_the_llm_with_the_router_on(self):
        self.assertTrue(config.INTENT_ROUTER_ENABLED)
        self.srv.script = [
            Reply(200, completion(None, [tool_call("get_pc_info", {})])),
            Reply(200, completion(None, [tool_call("set_reminder", {"title": "charge it", "minutes": 60})])),
            Reply(200, completion("Checked your PC and set that reminder.")),
        ]
        reply = self.brain.think("check my pc info, then remind me to charge it in an hour")
        self.assertEqual(reply, "Checked your PC and set that reminder.")
        self.assertEqual(len(self.srv.posts()), 3)

    def test_think_stream_also_consults_the_router_and_yields_one_chunk(self):
        chunks = list(self.brain.think_stream("volume up"))
        self.assertEqual(len(self.srv.posts()), 0)
        self.assertEqual(len(chunks), 1)
        self.assertNotIn("tool available", chunks[0].lower())


if __name__ == "__main__":
    unittest.main()
