"""
tests/test_tier17_intent_router_expansion.py — Tier 17: the 72 new
brain/intent_router.py fast-path rules that took _RULES from 63 to 135
entries (roadmap task "add English/Hinglish/Devanagari fast-path rules
for the missing tools").

Referenced by a comment in test_phase8_intent_router.py's
test_conversational_text_does_not_route (the "tell me a joke" ->
get_joke relabeling) - this file is that reference's target: the
dedicated regression suite for the router EXPANSION itself, parallel
to how test_tier16_time_date.py/test_tier16_web.py cover Tier 16's own
additions. test_phase8_intent_router.py keeps testing the pre-existing
63 rules and the shared routing machinery (negation/hypothetical/
compound guards, RouteResult shape, etc.) - this file is additive, not
a replacement.

Each check below turns a real collision worked through during design
into a permanent regression test, not just the ad-hoc scratchpad script
it started as. Two genuine bugs were caught this way before anything
shipped (both covered explicitly below, not just incidentally):

  * ModeToggleVsHardwareToggleTests.test_turn_on_app_name_falls_back_to_open_app
    - the first cut of _enable_mode() matched ANY "turn on X"/"enable X",
      silently swallowing "turn on the projector" instead of leaving it
      for open_app. Fixed with the _ENABLE_MODE_NAMES allowlist.
  * ServiceVsAppVsStopwatchTests.test_start_the_stopwatch_is_not_shadowed_by_open_app
    - start_stopwatch's rule originally lived in the bulk-append block,
      AFTER open_app in _RULES, so "start the stopwatch" (well under
      open_app's 5-word safety cap) was wrongly routed to
      open_app(app_name="the stopwatch"). Fixed by relocating the rule
      before open_app.

Run: python -m unittest discover -s tests -p "test_tier17_intent_router_expansion.py"
(pytest will also collect it.)
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from brain import intent_router as ir                               # noqa: E402
from brain.skills_registry import skills                            # noqa: E402
import brain.handlers  # noqa: E402,F401  (side effect: registers every built-in skill)


class RouteAssertionsMixin:
    """Shared check helpers - same spirit as test_phase8_intent_router.py's
    own RouteUnitTests helpers, kept local here so this file has no
    import-order dependency on that one."""

    def assertRoutes(self, text, skill, args=None):
        r = ir.route(text)
        self.assertIsNotNone(r, f"expected {text!r} to route to {skill!r}, got None")
        self.assertEqual(r.skill, skill, f"{text!r} routed to {r.skill!r}, expected {skill!r}")
        if args is not None:
            self.assertEqual(r.args, args, f"{text!r} -> {skill!r} got args {r.args!r}, expected {args!r}")

    def assertNoRoute(self, text):
        r = ir.route(text)
        self.assertIsNone(r, f"expected {text!r} NOT to route, got ({r.skill if r else None}, "
                              f"{r.args if r else None})")


# ---------------------------------------------------------------------------
# enable_mode vs. the pre-existing _toggle_hardware ("turn on") and vs.
# open_app's own bare "turn on "/"start " prefixes.
# ---------------------------------------------------------------------------
class ModeToggleVsHardwareToggleTests(RouteAssertionsMixin, unittest.TestCase):
    def test_turn_on_wifi_is_hardware_not_a_mode(self):
        # Tier 44: the hardware tool is now the one that really switches Wi-Fi.
        self.assertRoutes("turn on wifi", "turn_wifi_on")

    def test_turn_on_named_mode(self):
        self.assertRoutes("turn on study mode", "enable_mode", {"name": "study"})

    def test_enable_named_mode(self):
        self.assertRoutes("enable dnd", "enable_mode", {"name": "dnd"})

    def test_mode_name_on_suffix_form(self):
        self.assertRoutes("gaming mode on", "enable_mode", {"name": "gaming"})

    def test_turn_on_app_name_falls_back_to_open_app(self):
        # Regression for the real bug: an unrecognized "turn on X" must
        # NOT be swallowed by enable_mode - it has to fall through to
        # open_app, same as it would have before this tier existed.
        self.assertRoutes("turn on the projector", "open_app", {"app_name": "the projector"})

    def test_enable_unrecognized_name_falls_back(self):
        self.assertNoRoute("enable the thing")


# ---------------------------------------------------------------------------
# start_service/stop_service vs. open_app's bare "start " and vs.
# start_stopwatch/stop_stopwatch.
# ---------------------------------------------------------------------------
class ServiceVsAppVsStopwatchTests(RouteAssertionsMixin, unittest.TestCase):
    def test_start_spotify_is_open_app_not_start_service(self):
        self.assertRoutes("start spotify", "open_app", {"app_name": "spotify"})

    def test_start_the_x_service_phrase(self):
        self.assertRoutes("start the bluetooth service", "start_service", {"service_name": "bluetooth"})

    def test_start_service_x_phrase(self):
        self.assertRoutes("start service bluetooth", "start_service", {"service_name": "bluetooth"})

    def test_stop_stopwatch_is_not_stop_service(self):
        self.assertRoutes("stop stopwatch", "stop_stopwatch")

    def test_stop_the_x_service_phrase(self):
        self.assertRoutes("stop the print spooler service", "stop_service", {"service_name": "print spooler"})

    def test_start_the_stopwatch_is_not_shadowed_by_open_app(self):
        # Regression for the real bug: this rule originally lived after
        # open_app in _RULES, so "the stopwatch" (2 words, well under
        # open_app's word-count safety cap) was wrongly treated as an
        # app name. Fixed by moving start_stopwatch before open_app.
        self.assertRoutes("start the stopwatch", "start_stopwatch")


# ---------------------------------------------------------------------------
# play_on_spotify (requires an explicit "on spotify"/"spotify par"/
# "spotify pe" suffix) vs. the pre-existing bare play_youtube.
# ---------------------------------------------------------------------------
class SpotifyVsYoutubeTests(RouteAssertionsMixin, unittest.TestCase):
    def test_play_on_spotify(self):
        self.assertRoutes("play shape of you on spotify", "play_on_spotify", {"query": "shape of you"})

    def test_bare_play_is_youtube(self):
        self.assertRoutes("play shape of you", "play_youtube", {"query": "shape of you"})


# ---------------------------------------------------------------------------
# search_file / search_my_notes (both require an explicit "file"/"notes"
# word) vs. the pre-existing generic _search_or_play ("search for " ->
# google, or -> youtube when "on youtube" is also present).
# ---------------------------------------------------------------------------
class SearchDisambiguationTests(RouteAssertionsMixin, unittest.TestCase):
    def test_search_for_file_x(self):
        self.assertRoutes("search for file budget.xlsx", "search_file", {"file_name": "budget.xlsx"})

    def test_search_my_notes_for_x(self):
        self.assertRoutes("search my notes for the johnson project", "search_my_notes",
                          {"question": "the johnson project"})

    def test_bare_search_for_is_google(self):
        self.assertRoutes("search for cat videos", "search_google", {"query": "cat videos"})

    def test_search_for_x_on_youtube_is_youtube(self):
        self.assertRoutes("search for cat videos on youtube", "play_youtube", {"query": "cat videos"})


# ---------------------------------------------------------------------------
# complete_todo/delete_todo (positioned before delete_file in _RULES) vs.
# delete_file itself.
# ---------------------------------------------------------------------------
class TodoVsFileDeletionTests(RouteAssertionsMixin, unittest.TestCase):
    def test_mark_todo_as_done(self):
        self.assertRoutes("mark buy milk as done", "complete_todo", {"query": "buy milk"})

    def test_complete_the_todo_x(self):
        self.assertRoutes("complete the todo buy milk", "complete_todo", {"query": "buy milk"})

    def test_delete_x_todo(self):
        self.assertRoutes("delete buy milk todo", "delete_todo", {"query": "buy milk"})

    def test_delete_todo_does_not_shadow_delete_file(self):
        self.assertRoutes("delete report.txt", "delete_file", {"file_name": "report.txt"})

    def test_remove_the_x_to_do_hyphenated_form(self):
        self.assertRoutes("remove the dentist to-do", "delete_todo", {"query": "the dentist"})


# ---------------------------------------------------------------------------
# cancel_reminder vs. cancel_calendar_event - both support a bare
# "all reminders"/"all events" shorthand alongside prefix+suffix
# extraction of a specific target.
# ---------------------------------------------------------------------------
class ReminderVsCalendarCancelTests(RouteAssertionsMixin, unittest.TestCase):
    def test_cancel_the_x_reminder(self):
        self.assertRoutes("cancel the oven reminder", "cancel_reminder", {"query": "oven"})

    def test_cancel_the_x_appointment_is_calendar_event(self):
        self.assertRoutes("cancel the dentist appointment", "cancel_calendar_event", {"query": "dentist"})

    def test_cancel_all_reminders_shorthand(self):
        self.assertRoutes("cancel all reminders", "cancel_reminder", {"query": "all"})

    def test_cancel_all_events_shorthand(self):
        self.assertRoutes("cancel all events", "cancel_calendar_event", {"query": "all"})


# ---------------------------------------------------------------------------
# reset_streak (positioned before forget_memory in _RULES, so "forget my
# X streak" doesn't get treated as a memory key) vs. recall_memory/
# forget_memory's own exact-key extraction.
# ---------------------------------------------------------------------------
class StreakVsMemoryForgetTests(RouteAssertionsMixin, unittest.TestCase):
    def test_forget_my_x_streak_is_reset_streak(self):
        self.assertRoutes("forget my workout streak", "reset_streak", {"habit": "workout"})

    def test_forget_my_x_password_is_forget_memory(self):
        self.assertRoutes("forget my wifi password", "forget_memory", {"key": "wifi password"})

    def test_recall_my_x(self):
        self.assertRoutes("recall my wifi password", "recall_memory", {"key": "wifi password"})

    def test_recall_the_x_article_consumed_by_prefix(self):
        # "the " is consumed by the "recall the " prefix match itself, so
        # it must never leak into the extracted key.
        self.assertRoutes("recall the password", "recall_memory", {"key": "password"})

    def test_reset_my_x_streak(self):
        self.assertRoutes("reset my meditation streak", "reset_streak", {"habit": "meditation"})


# ---------------------------------------------------------------------------
# summarize_page (requires the remainder to start with http(s)://) vs.
# summarize_my_notes / quiz_me_on.
# ---------------------------------------------------------------------------
class SummarizePageVsNotesTests(RouteAssertionsMixin, unittest.TestCase):
    def test_no_summarize_this_page_colon_prefix_defined(self):
        # Deliberately not a recognized prefix - falls through to the LLM.
        self.assertNoRoute("summarize this page: https://example.com/article")

    def test_summarize_bare_url(self):
        self.assertRoutes("summarize https://example.com/article", "summarize_page",
                          {"url": "https://example.com/article"})

    def test_summarize_my_notes_on_x(self):
        self.assertRoutes("summarize my notes on the johnson project", "summarize_my_notes",
                          {"topic": "the johnson project"})

    def test_quiz_me_on_x(self):
        self.assertRoutes("quiz me on photosynthesis", "quiz_me_on", {"topic": "photosynthesis"})


# ---------------------------------------------------------------------------
# Window-management disambiguation, including the deliberate decision NOT
# to add a separate minimize_all_windows rule (show_desktop already
# claims that phrasing - see brain/intent_router.py's _RULES comment).
# ---------------------------------------------------------------------------
class WindowManagementDisambiguationTests(RouteAssertionsMixin, unittest.TestCase):
    def test_minimize_all_windows_is_show_desktop(self):
        self.assertRoutes("minimize all windows", "show_desktop")

    def test_minimize_everything_is_show_desktop(self):
        self.assertRoutes("minimize everything", "show_desktop")

    def test_minimize_named_window(self):
        self.assertRoutes("minimize chrome", "minimize_window", {"window_name": "chrome"})

    def test_bare_switch_window(self):
        self.assertRoutes("switch window", "switch_window")

    def test_switch_to_named_window(self):
        self.assertRoutes("switch to chrome", "focus_window", {"window_name": "chrome"})

    def test_fullscreen_prefix_form(self):
        self.assertRoutes("fullscreen chrome", "set_fullscreen", {"window_name": "chrome"})

    def test_fullscreen_suffix_form(self):
        self.assertRoutes("chrome fullscreen", "set_fullscreen", {"window_name": "chrome"})


# ---------------------------------------------------------------------------
# type_text vs. the pre-existing write_notepad (which requires an
# explicit "in notepad" target).
# ---------------------------------------------------------------------------
class TypeVsWriteNotepadTests(RouteAssertionsMixin, unittest.TestCase):
    def test_type_x_in_notepad_is_write_notepad(self):
        self.assertRoutes("type hello world in notepad", "write_notepad", {"text": "hello world"})

    def test_bare_type_x_is_type_text(self):
        self.assertRoutes("type hello world", "type_text", {"text": "hello world"})

    def test_write_x_in_notepad(self):
        self.assertRoutes("write hello world in notepad", "write_notepad", {"text": "hello world"})


# ---------------------------------------------------------------------------
# The shared numeric 0-100 parser behind set_volume/set_brightness
# (_PCT_RE + _set_percent_cmd), English and Hinglish.
# ---------------------------------------------------------------------------
class PercentCommandTests(RouteAssertionsMixin, unittest.TestCase):
    def test_set_the_volume_to_n(self):
        self.assertRoutes("set the volume to 50", "set_volume", {"percent": 50})

    def test_volume_n_percent(self):
        self.assertRoutes("volume 70 percent", "set_volume", {"percent": 70})

    def test_hinglish_awaaz_n_pe_karo(self):
        self.assertRoutes("awaaz 50 pe karo", "set_volume", {"percent": 50})

    def test_set_the_brightness_to_n(self):
        self.assertRoutes("set the brightness to 30", "set_brightness", {"percent": 30})


# ---------------------------------------------------------------------------
# restart_app (named target) vs. the pre-existing bare restart_pc,
# English and Hinglish.
# ---------------------------------------------------------------------------
class RestartAppVsRestartPcTests(RouteAssertionsMixin, unittest.TestCase):
    def test_restart_named_app(self):
        self.assertRoutes("restart chrome", "restart_app", {"app_name": "chrome"})

    def test_bare_restart_is_restart_pc(self):
        self.assertRoutes("restart my pc", "restart_pc")

    def test_hinglish_restart_karo_is_restart_pc(self):
        self.assertRoutes("restart karo", "restart_pc")


# ---------------------------------------------------------------------------
# The 9 optional-arg-only tools that deliberately use _exact (never
# _contains) so a more specific phrase correctly falls through to the
# LLM instead of silently dropping the extra detail.
# ---------------------------------------------------------------------------
class OptionalArgExactnessTests(RouteAssertionsMixin, unittest.TestCase):
    def test_bare_whats_the_weather(self):
        self.assertRoutes("what's the weather", "get_weather", {})

    def test_whats_the_weather_in_city_falls_to_llm(self):
        # Must NOT match get_weather's bare-phrase rule and silently drop
        # "mumbai" - falls through so the LLM can pass location=mumbai.
        self.assertNoRoute("what's the weather in mumbai")

    def test_whats_the_news_in_topic_falls_to_llm(self):
        self.assertNoRoute("what's the news in tech")

    def test_bare_whats_the_news(self):
        self.assertRoutes("what's the news", "get_news", {})

    def test_bare_cancel_timer(self):
        self.assertRoutes("cancel timer", "cancel_timer", {})


# ---------------------------------------------------------------------------
# A handful of the remaining new single-arg tools, each exercised at
# least once so every new skill name has a direct regression test
# somewhere in this file (the ground-truth coverage check below
# confirms the full set).
# ---------------------------------------------------------------------------
class MiscNewToolsTests(RouteAssertionsMixin, unittest.TestCase):
    def test_calculate(self):
        self.assertRoutes("calculate 2 plus 2", "calculate", {"expression": "2 plus 2"})

    def test_take_a_note_colon_form(self):
        self.assertRoutes("take a note: buy milk", "take_note", {"content": "buy milk"})

    def test_note_down_that_form(self):
        self.assertRoutes("note down that the wifi password is hunter2", "take_note",
                          {"content": "the wifi password is hunter2"})

    def test_set_the_clipboard_to_x(self):
        self.assertRoutes("set the clipboard to hello", "set_clipboard", {"text": "hello"})


# ---------------------------------------------------------------------------
# _CASE_PRESERVING_ARGS - confirms the lowercased routing match still
# returns the ORIGINAL-case substring for the 10 new entries that need
# it (content/text/url/key/etc. must survive exactly as typed; only the
# matched prefix/suffix tokens themselves are case-insensitive).
# ---------------------------------------------------------------------------
class CasePreservationTests(unittest.TestCase):
    def test_take_note_content_is_case_preserved(self):
        r = ir.route("Take A Note: Buy MILK")
        self.assertIsNotNone(r)
        self.assertEqual(r.skill, "take_note")
        self.assertEqual(r.args.get("content"), "Buy MILK")

    def test_summarize_page_url_is_case_preserved(self):
        r = ir.route("Summarize https://Example.com/Article")
        self.assertIsNotNone(r)
        self.assertEqual(r.skill, "summarize_page")
        self.assertEqual(r.args.get("url"), "https://Example.com/Article")


# ---------------------------------------------------------------------------
# A plain regression pin on the rule count itself, so an accidental
# deletion (or an accidental duplicate) during some future tier's edits
# shows up here directly rather than only as a cryptic routing miss
# somewhere else. 135 = 63 pre-Tier-17 rules + 72 added this tier.
# ---------------------------------------------------------------------------
class RuleCountRegressionTests(unittest.TestCase):
    def test_rule_count_is_136(self):
        # 135 + Tier 44's theme_mode (light/dark mode).
        self.assertEqual(len(ir._RULES), 136,
                         "brain/intent_router.py's _RULES count changed - update this pin "
                         "deliberately if a later tier adds/removes rules on purpose")


# ---------------------------------------------------------------------------
# Ground-truth ROUTER COVERAGE: every positive row in the golden bench
# set is routed and its resulting .skill collected, exactly the method
# used during Tier 17's own gap-enumeration (see this tier's docstring
# notes) - the only reliable way to measure coverage, since _Rule.name
# labels are documentation-only and don't necessarily equal the
# dispatched skill. This is a floor, not an exact-match pin: it only
# asserts that coverage hasn't REGRESSED below Tier 17's own delivered
# number, so a later tier's further growth doesn't need to touch it.
# ---------------------------------------------------------------------------
class GroundTruthCoverageTests(unittest.TestCase):
    def test_at_least_135_distinct_builtin_skills_are_fast_path_reachable(self):
        from bench.dataset import load_golden

        covered = set()
        for row in load_golden():
            if row.intent == "chat":
                continue  # a negative row - deliberately expects no fast-path match
            r = ir.route(row.text)
            if r is not None:
                covered.add(r.skill)
        all_builtin = set(skills.names_by_source("builtin"))
        covered &= all_builtin
        self.assertGreaterEqual(len(covered), 135,
                                f"fast-path-reachable builtin skill count dropped to {len(covered)} "
                                f"(missing vs. Tier 17's own floor: {sorted(all_builtin - covered)[:10]}...)")


if __name__ == "__main__":
    unittest.main()
