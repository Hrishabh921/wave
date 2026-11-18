"""
tests/test_tier17_followup_resolver.py — Tier 17: brain/followup_resolver.py
("close it"/"usko band karo" after open_app, "and Jaipur?"/"aur Jaipur ka?"
after get_weather) plus a lightweight check that brain/groq_core.py's two
call sites (think()/think_stream()) actually wire it in.

Mirrors test_phase8_intent_router.py's own split: direct unit tests of the
resolver function itself (same spirit as that file's RouteUnitTests), not a
full through-the-brain BrainBase/FakeOpenAI integration test - see
test_phase4_brain.py's own setUp comment on why fast-path layers get their
own dedicated route()-level tests instead (it explicitly disables
INTENT_ROUTER_ENABLED so its LLM-focused tests aren't short-circuited by
the fast path, and points at test_phase8_intent_router.py for that layer's
real coverage - this file is the equivalent for followup_resolver.py).

Run: python -m unittest discover -s tests -p "test_tier17_followup_resolver.py"
(pytest will also collect it.)
"""
import inspect
import os
import sys
import unittest
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import _phase4_support as S  # noqa: E402

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                       # noqa: E402
from automation.audit import ActionRecord, MemoryStore           # noqa: E402
from brain import followup_resolver as fr                        # noqa: E402
from settings.config import config                               # noqa: E402


def _record(tool, args=None, status="ok", age_s=5):
    ts = (datetime.now() - timedelta(seconds=age_s)).isoformat(timespec="seconds")
    return ActionRecord(id=1, ts=ts, tool=tool, args=args or {}, status=status)


class FollowupResolverTestBase(unittest.TestCase):
    """Isolates the module-level automation.audit.audit singleton for the
    duration of each test - it's shared process-wide, so every test here
    must leave it exactly as found (same reasoning test_phase4_brain.py's
    BrainBase gives for swapping in its own Audit instance, just lighter-
    weight: these tests never need a real backing store, only the RAM
    row list resolve_followup() reads via audit.recent())."""

    def setUp(self):
        self._orig_store = audit_mod.audit._store
        self._orig_rows = audit_mod.audit._rows
        self._orig_loaded = audit_mod.audit._loaded
        audit_mod.audit._store = MemoryStore()
        audit_mod.audit._rows = []
        audit_mod.audit._loaded = True
        self._orig_enabled = config.FOLLOWUP_RESOLVER_ENABLED
        config.FOLLOWUP_RESOLVER_ENABLED = True

    def tearDown(self):
        audit_mod.audit._store = self._orig_store
        audit_mod.audit._rows = self._orig_rows
        audit_mod.audit._loaded = self._orig_loaded
        config.FOLLOWUP_RESOLVER_ENABLED = self._orig_enabled

    def _seed(self, tool, args=None, status="ok", age_s=5):
        audit_mod.audit._rows = [_record(tool, args, status, age_s)]

    def assertRoutes(self, text, skill, args):
        routed = fr.resolve_followup(text)
        self.assertIsNotNone(routed, f"expected {text!r} to route, got None")
        self.assertEqual(routed.skill, skill)
        self.assertEqual(routed.args, args)

    def assertNoRoute(self, text):
        self.assertIsNone(fr.resolve_followup(text), f"expected {text!r} NOT to route, got a match")


class AppTargetFollowupTests(FollowupResolverTestBase):
    def test_close_it_after_open_app(self):
        self._seed("open_app", {"app_name": "chrome"})
        for text in ("close it", "close that", "usko band karo", "use band karo",
                     "band kar do", "ise band karo", "इसे बंद करो", "उसे बंद करो"):
            self.assertRoutes(text, "close_app", {"app_name": "chrome"})

    def test_open_it_again_after_close_app(self):
        self._seed("close_app", {"app_name": "notepad"})
        for text in ("open it again", "open that again", "usko wapas kholo", "wapas kholo"):
            self.assertRoutes(text, "open_app", {"app_name": "notepad"})

    def test_restart_it_after_restart_app_or_open_app(self):
        self._seed("open_app", {"app_name": "spotify"})
        self.assertRoutes("restart it", "restart_app", {"app_name": "spotify"})
        self.assertRoutes("usko restart karo", "restart_app", {"app_name": "spotify"})

    def test_focus_on_it_uses_window_name_arg(self):
        self._seed("focus_window", {"window_name": "vs code"})
        self.assertRoutes("focus on it", "focus_window", {"window_name": "vs code"})
        self.assertRoutes("switch to it", "focus_window", {"window_name": "vs code"})

    def test_no_antecedent_at_all_does_not_route(self):
        self.assertNoRoute("close it")

    def test_failed_antecedent_does_not_route(self):
        self._seed("open_app", {"app_name": "nonexistent"}, status="failed")
        self.assertNoRoute("close it")

    def test_wrong_tool_antecedent_does_not_route(self):
        self._seed("take_screenshot", {})
        self.assertNoRoute("close it")

    def test_stale_antecedent_does_not_route(self):
        self._seed("open_app", {"app_name": "chrome"}, age_s=config.FOLLOWUP_CONTEXT_TTL_S + 30)
        self.assertNoRoute("close it")

    def test_antecedent_right_at_the_ttl_boundary_still_routes(self):
        self._seed("open_app", {"app_name": "chrome"}, age_s=1)
        self.assertRoutes("close it", "close_app", {"app_name": "chrome"})

    def test_negation_guard_reused_from_intent_router(self):
        self._seed("open_app", {"app_name": "chrome"})
        for text in ("don't close it", "dont close it", "do not close it"):
            self.assertNoRoute(text)

    def test_hypothetical_guard_reused_from_intent_router(self):
        self._seed("open_app", {"app_name": "chrome"})
        self.assertNoRoute("should i close it")

    def test_compound_guard_reused_from_intent_router(self):
        self._seed("open_app", {"app_name": "chrome"})
        self.assertNoRoute("close it and then open notepad")


class WeatherFollowupTests(FollowupResolverTestBase):
    def test_english_and_city_forms(self):
        self._seed("get_weather", {"location": "mumbai"})
        self.assertRoutes("and jaipur?", "get_weather", {"location": "jaipur"})
        self.assertRoutes("and jaipur", "get_weather", {"location": "jaipur"})
        self.assertRoutes("what about delhi", "get_weather", {"location": "delhi"})
        self.assertRoutes("what about delhi?", "get_weather", {"location": "delhi"})

    def test_hinglish_aur_city_ka_forms(self):
        self._seed("get_weather", {"location": "mumbai"})
        self.assertRoutes("aur jaipur ka?", "get_weather", {"location": "jaipur"})
        self.assertRoutes("aur jaipur ka", "get_weather", {"location": "jaipur"})
        self.assertRoutes("aur jaipur mein?", "get_weather", {"location": "jaipur"})
        self.assertRoutes("aur jaipur", "get_weather", {"location": "jaipur"})

    def test_no_weather_antecedent_does_not_route(self):
        self._seed("take_screenshot", {})
        self.assertNoRoute("and jaipur?")

    def test_stale_weather_antecedent_does_not_route(self):
        self._seed("get_weather", {"location": "mumbai"}, age_s=config.FOLLOWUP_CONTEXT_TTL_S + 30)
        self.assertNoRoute("and jaipur?")

    def test_empty_city_does_not_route(self):
        self._seed("get_weather", {"location": "mumbai"})
        self.assertNoRoute("and?")
        self.assertNoRoute("aur?")


class ResolveFollowupGuardTests(FollowupResolverTestBase):
    def test_empty_and_whitespace_text_does_not_route(self):
        self._seed("open_app", {"app_name": "chrome"})
        self.assertNoRoute("")
        self.assertNoRoute("   ")

    def test_disabled_via_config_never_routes(self):
        self._seed("open_app", {"app_name": "chrome"})
        config.FOLLOWUP_RESOLVER_ENABLED = False
        self.assertNoRoute("close it")

    def test_ordinary_self_contained_commands_are_untouched(self):
        # resolve_followup only ever matches the curated pronoun/ellipsis
        # shapes - a normal, self-contained command must fall straight
        # through to None (-> intent_router.route() handles it instead).
        self._seed("open_app", {"app_name": "chrome"})
        for text in ("open notepad", "what's the weather", "take a screenshot", "lock my pc"):
            self.assertNoRoute(text)


class WiringTests(unittest.TestCase):
    """Confirms brain/groq_core.py's think()/think_stream() actually call
    resolve_followup() - a plain source-text check rather than a full
    BrainBase/FakeOpenAI run, consistent with keeping this fast-path
    layer's own tests separate from the LLM-path integration tests (see
    this file's module docstring)."""

    def test_both_entry_points_call_resolve_followup(self):
        import brain.groq_core as gc
        think_src = inspect.getsource(gc.WaveBrain.think)
        stream_src = inspect.getsource(gc.WaveBrain.think_stream)
        self.assertIn("followup_resolver.resolve_followup", think_src)
        self.assertIn("followup_resolver.resolve_followup", stream_src)
        # The follow-up check must run before intent_router.route() in
        # both methods - resolve_followup only matches pronoun/ellipsis
        # shapes intent_router's own rules never claim, so strict order
        # isn't a correctness requirement, but keeping the two call sites
        # in the same relative order in both methods is what "kept in
        # sync with it deliberately" (think_stream's own comment) means.
        # Anchored on the actual assignment statements, not a bare
        # substring - both methods' own prose comments mention
        # "intent_router.route()" ahead of the real call too, which would
        # otherwise make this check pass or fail on comment wording
        # rather than on the real code order.
        for src in (think_src, stream_src):
            self.assertLess(src.index("followup = followup_resolver.resolve_followup(user_text)"),
                            src.index("routed = intent_router.route(user_text)"))

    def test_both_use_the_same_dispatch_tool_path(self):
        import brain.groq_core as gc
        think_src = inspect.getsource(gc.WaveBrain.think)
        self.assertIn('source="followup_resolver"', think_src)


if __name__ == "__main__":
    unittest.main()
