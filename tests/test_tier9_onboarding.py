"""Tier 9 - logic tests for ui/onboarding.py (OnboardingWizard), WITHOUT a
display or PyQt6.

Nothing in test_phase4_ui_logic.py or test_tier5_ui_logic.py touches this
file. Covers the step-sequencing guards (_go_back/_go_next, including the
on-Finish completion callback), the progress-dot highlighting and
back/next-button state _show_step() drives per step, the theme-picker's
mutual-exclusivity logic (_select_theme), and the AI-provider connectivity
check's success/failure paths plus the try/except RuntimeError guard in
_on_ai_provider_checked - a real, deliberate guard (see its comment in
ui/onboarding.py) against the user clicking past this step before the
background check returns, which had never been exercised by any test.

Reuses test_phase4_ui_logic.py's stand-in.
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_phase4_ui_logic as P4
# Reuse the order-independent fake worker pool rather than redefining it -
# test_phase4_ui_logic.SyncPool only replays a failure correctly if .error is
# connected before the job fails, which isn't how _test_ai_provider (or
# SettingsPanel._test_provider_clicked) connects its two signals.
from test_tier9_settings_panel import ImmediatePool


def import_with_fakes(module_name):
    for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
        del sys.modules[name]
    for _ in range(30):
        try:
            return __import__(module_name, fromlist=["_"])
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if not missing or missing in sys.modules:
                raise
            sys.modules[missing] = P4._QtModule(missing)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class OnboardingWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.ob = import_with_fakes("ui.onboarding")

    def _wizard(self, parent=None):
        w = object.__new__(self.ob.OnboardingWizard)
        w._steps = [
            ("Welcome", "d0", "i0"), ("AI Brain", "d1", "i1"), ("Microphone", "d2", "i2"),
            ("Wake Word", "d3", "i3"), ("Theme", "d4", "i4"), ("Permissions", "d5", "i5"), ("Done", "d6", "i6"),
        ]
        w._current_step = 0
        w.icon_lbl, w.title_lbl, w.desc_lbl = P4.QLabel(), P4.QLabel(), P4.QLabel()
        w.progress_dots = [mock.Mock() for _ in w._steps]
        w.content_layout = self.ob.QVBoxLayout()
        w.btn_back, w.btn_next = mock.Mock(), mock.Mock()
        w._parent = parent
        w.parent = lambda: w._parent
        return w

    # ---------- back / next guards ----------
    def test_go_back_at_the_first_step_is_a_no_op(self):
        w = self._wizard()
        w._show_step = mock.Mock()
        w._go_back()
        self.assertEqual(w._current_step, 0)
        w._show_step.assert_not_called()

    def test_go_back_from_a_later_step_shows_the_previous_one(self):
        w = self._wizard()
        w._current_step = 3
        w._show_step = mock.Mock()
        w._go_back()
        w._show_step.assert_called_once_with(2)

    def test_go_next_from_an_intermediate_step_advances(self):
        w = self._wizard()
        w._current_step = 2
        w._show_step = mock.Mock()
        w._on_complete = mock.Mock()
        w.accept = mock.Mock()
        w._go_next()
        w._show_step.assert_called_once_with(3)
        w.accept.assert_not_called()
        w._on_complete.assert_not_called()

    def test_go_next_from_the_last_step_finishes_instead_of_advancing(self):
        w = self._wizard()
        w._current_step = len(w._steps) - 1
        w._show_step = mock.Mock()
        w._on_complete = mock.Mock()
        w.accept = mock.Mock()
        w._go_next()
        w._show_step.assert_not_called()
        w.accept.assert_called_once()
        w._on_complete.assert_called_once()

    def test_go_next_on_finish_without_a_completion_callback_does_not_crash(self):
        w = self._wizard()
        w._current_step = len(w._steps) - 1
        w._show_step = mock.Mock()
        w._on_complete = None
        w.accept = mock.Mock()
        w._go_next()   # must not raise
        w.accept.assert_called_once()

    # ---------- _show_step: dots, buttons, step-specific content ----------
    def test_show_step_highlights_dots_up_to_and_including_the_current_one(self):
        w = self._wizard(parent=types.SimpleNamespace())
        w._clear_content = mock.Mock()
        w._show_step(2)
        # Dots 0..2 get one color, dots 3..6 get another - and they differ.
        highlighted = w.progress_dots[2].setStyleSheet.call_args.args[0]
        dim = w.progress_dots[3].setStyleSheet.call_args.args[0]
        self.assertNotEqual(highlighted, dim)

    def test_show_step_back_button_hidden_only_on_the_first_step(self):
        info = {"provider": "groq", "model": "llama", "endpoint": "groq.com", "key": "…9", "configured": True, "problem": ""}
        w = self._wizard(parent=types.SimpleNamespace(brain=types.SimpleNamespace(provider_info=lambda: info)))
        w._clear_content = mock.Mock()
        w._show_step(0)
        w.btn_back.setVisible.assert_called_with(False)
        w._show_step(1)
        w.btn_back.setVisible.assert_called_with(True)

    def test_show_step_next_button_says_finish_only_on_the_last_step(self):
        w = self._wizard(parent=types.SimpleNamespace())
        w._clear_content = mock.Mock()
        w._show_step(len(w._steps) - 2)
        w.btn_next.setText.assert_called_with("Next")
        w._show_step(len(w._steps) - 1)
        w.btn_next.setText.assert_called_with("Finish")

    def test_show_step_ai_brain_step_reads_live_provider_info(self):
        info = {"provider": "groq", "model": "llama", "endpoint": "groq.com", "key": "…9", "configured": True, "problem": ""}
        w = self._wizard(parent=types.SimpleNamespace(brain=types.SimpleNamespace(provider_info=lambda: info)))
        w._show_step(1)
        self.assertIn("groq", w.ai_info_lbl.text())
        self.assertIn("verify it's actually reachable", w.ai_status.text())

    def test_show_step_ai_brain_step_flags_an_unconfigured_provider(self):
        info = {"provider": "groq", "model": "llama", "endpoint": "groq.com", "key": "", "configured": False, "problem": "GROQ_API_KEY missing"}
        w = self._wizard(parent=types.SimpleNamespace(brain=types.SimpleNamespace(provider_info=lambda: info)))
        w._show_step(1)
        self.assertIn("Not usable yet: GROQ_API_KEY missing", w.ai_status.text())

    def test_show_step_theme_step_preselects_the_button_matching_the_current_theme(self):
        parent = types.SimpleNamespace(theme_manager=types.SimpleNamespace(
            get_current=lambda: types.SimpleNamespace(name="Emerald Dark")))
        w = self._wizard(parent=parent)
        w._show_step(4)
        checked = [b for b in w.theme_btns if b._props.get("active") == "true"]
        self.assertEqual([b.text() for b in checked], ["Emerald Dark"])

    # ---------- theme picking ----------
    def test_select_theme_checks_only_the_clicked_button(self):
        w = self._wizard(parent=types.SimpleNamespace(
            theme_manager=types.SimpleNamespace(set_theme=mock.Mock()), apply_theme=mock.Mock()))
        btn_a, btn_b, btn_c = P4.QPushButton("A"), P4.QPushButton("B"), P4.QPushButton("C")
        w.theme_btns = [btn_a, btn_b, btn_c]
        w._select_theme("B", btn_b)
        self.assertEqual(btn_a._props.get("active"), "false")
        self.assertEqual(btn_c._props.get("active"), "false")
        self.assertEqual(btn_b._props.get("active"), "true")
        w._parent.theme_manager.set_theme.assert_called_once_with("B")
        w._parent.apply_theme.assert_called_once_with("B")

    # ---------- AI provider connectivity check ----------
    def test_test_ai_provider_success_shows_a_checkmark_and_timing(self):
        w = self._wizard(parent=types.SimpleNamespace(
            worker_pool=ImmediatePool(), brain=types.SimpleNamespace(check_provider=lambda: (True, "Reachable.", 9))))
        w.ai_test_btn = mock.Mock()
        w.ai_status = P4.QLabel()
        w._test_ai_provider()
        self.assertEqual(w.ai_status.text(), "✓ Reachable. (9 ms)")
        w.ai_test_btn.setEnabled.assert_called_with(True)

    def test_test_ai_provider_failure_shows_an_x_without_timing(self):
        def boom():
            raise TimeoutError("took too long")
        w = self._wizard(parent=types.SimpleNamespace(
            worker_pool=ImmediatePool(), brain=types.SimpleNamespace(check_provider=boom)))
        w.ai_test_btn = mock.Mock()
        w.ai_status = P4.QLabel()
        w._test_ai_provider()
        self.assertEqual(w.ai_status.text(), "✗ took too long")

    def test_on_ai_provider_checked_swallows_runtime_error_from_a_deleted_widget(self):
        """The step was already left (ai_status has been torn down) by the
        time the background check returns - setText on it raises
        RuntimeError in real Qt. Must not propagate, and must not reach
        ai_test_btn.setEnabled(True) either (it's after the raising line)."""
        w = self._wizard()
        w.ai_status = mock.Mock()
        w.ai_status.setText.side_effect = RuntimeError("wrapped C/C++ object of type QLabel has been deleted")
        w.ai_test_btn = mock.Mock()
        w._on_ai_provider_checked((True, "fine", 1))   # must not raise
        w.ai_test_btn.setEnabled.assert_not_called()


if __name__ == "__main__":
    unittest.main()
