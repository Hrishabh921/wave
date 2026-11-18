"""Tier 21 item: "live activity card with Cancel" in the chat page.

Three of the four things that roadmap line asks for already shipped in
earlier tiers, and this file documents that rather than re-implementing
them:
  * Live captions while Wave is talking - Tier 12, ui/main_window.py's
    _caption_row/_caption_label/_on_partial_text.
  * A real waveform - Tier 11's AudioVisualizer, already in the chat
    input bar (self.visualizer) and the music now-playing panel.
  * Basic cancellation - Tier 19's cancel_event; the chat page's send
    button already turns into a Stop button while a reply streams.

The one genuinely new piece is the live activity BANNER added this tier:
"⚙ Doing: X" while a tool call (open_app, create_file, ...) is actually
running - not just that Wave is "thinking" or "speaking", but which
concrete system action is in flight right now. It's driven by
automation/audit.py's new "started" listener event (see
test_phase4_undo.py's StartedEventTests for that event's own contract);
this file tests ui/main_window.py's side of the wiring (_on_audit_activity_event
/_on_tool_activity/_hide_activity_banner_if_current) in isolation, using
the project's standard fake-Qt stand-in since there's no display here.
"""
import unittest

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_tier5_ui_logic as T
import test_phase4_ui_logic as P4


class _FakeLabelWithVisibility(P4.QLabel):
    """P4.QLabel plus real tracked visible state - needed here because
    _on_tool_activity's whole job is toggling visibility, and the base
    stand-in's isVisible()/setVisible() fall through to the generic _Any
    absorber (always truthy), which would hide a real show/hide bug."""
    def __init__(self, text="", *a, **k):
        super().__init__(text)
        self._visible = True

    def setVisible(self, flag):
        self._visible = bool(flag)

    def isVisible(self):
        return self._visible


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class LiveActivityBannerLogicTests(unittest.TestCase):
    """Exercises MainWindow's three small handler methods directly on a
    bare, unconstructed instance (MainWindow.__new__) - building a full
    MainWindow pulls in audio/workers/etc. far beyond what this one piece
    of wiring needs to be tested in isolation, same reasoning
    test_tier5_ui_logic.py's own module docstring gives for testing
    ui/workers.py and ui/workflows_panel.py this way."""

    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                               hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                               typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                               size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        widgets = T.install_stub_with_list_widgets()
        widgets.OVERRIDES["QLabel"] = _FakeLabelWithVisibility
        cls.mod = T.import_with_fakes("ui.main_window")

    def _bare_window(self):
        """A MainWindow instance with NONE of __init__'s side effects - just
        enough attributes set for the three methods under test to run."""
        win = self.mod.MainWindow.__new__(self.mod.MainWindow)
        win.activity_banner = self.mod.QLabel("")
        win.activity_banner.setVisible(False)
        return win

    def test_started_event_emits_a_description_via_summarize(self):
        win = self._bare_window()
        emitted = []
        win.tool_activity = type("FakeSignal", (), {"emit": staticmethod(lambda d: emitted.append(d))})()
        rec = type("Rec", (), {"tool": "open_app", "args": {"app_name": "chrome"}})()
        win._on_audit_activity_event("started", rec)
        self.assertEqual(emitted, ["Opened chrome"])

    def test_non_started_events_are_ignored(self):
        win = self._bare_window()
        emitted = []
        win.tool_activity = type("FakeSignal", (), {"emit": staticmethod(lambda d: emitted.append(d))})()
        rec = type("Rec", (), {"tool": "open_app", "args": {}})()
        for event in ("added", "updated", "cleared"):
            win._on_audit_activity_event(event, rec)
        self.assertEqual(emitted, [])

    def test_none_record_is_ignored_without_crashing(self):
        win = self._bare_window()
        win.tool_activity = type("FakeSignal", (), {"emit": staticmethod(lambda d: (_ for _ in ()).throw(
            AssertionError("should not emit")))})()
        win._on_audit_activity_event("started", None)   # must not raise

    def test_on_tool_activity_shows_the_banner_with_the_description(self):
        win = self._bare_window()
        win._on_tool_activity("Opened chrome")
        self.assertTrue(win.activity_banner.isVisible())
        self.assertIn("Opened chrome", win.activity_banner.text())

    def test_hide_helper_only_hides_for_the_current_generation(self):
        """Two "started" events in quick succession must not let the
        FIRST one's auto-hide timer close the banner while the SECOND
        action is still the one being shown."""
        win = self._bare_window()
        win._on_tool_activity("Opened chrome")
        first_gen = win._activity_banner_timer_gen
        win._on_tool_activity("Took a screenshot")
        second_gen = win._activity_banner_timer_gen
        self.assertNotEqual(first_gen, second_gen)
        win._hide_activity_banner_if_current(first_gen)   # the stale timer firing late
        self.assertTrue(win.activity_banner.isVisible())
        self.assertIn("Took a screenshot", win.activity_banner.text())
        win._hide_activity_banner_if_current(second_gen)  # the current timer firing
        self.assertFalse(win.activity_banner.isVisible())

    def test_handlers_tolerate_a_missing_banner_widget(self):
        """Before _build_chat_column() has run (or in some future refactor
        that removes the attribute), these must not crash - same
        defensive getattr(..., None) pattern _refresh_activity_page uses
        for self.activity_list_layout."""
        win = self.mod.MainWindow.__new__(self.mod.MainWindow)  # no activity_banner attribute at all
        win._on_tool_activity("whatever")                        # must not raise
        win._hide_activity_banner_if_current(0)                  # must not raise


if __name__ == "__main__":
    unittest.main()
