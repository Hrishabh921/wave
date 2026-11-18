"""
tests/test_tier19_health_ollama.py — Tier 19: automation/health_check.py,
brain/ollama_lifecycle.py, and their wiring into ui/main_window.py (the
sidebar health dot fed by thread_supervisor, and the toasts for Ollama-
lifecycle/startup-health-check events), WITHOUT a display or PyQt6.

automation/health_check.py and brain/ollama_lifecycle.py themselves have no
PyQt6/GUI dependency at all and are exercised directly, no stub needed (see
their own module docstrings for what each does and why). Only the
ui/main_window.py wiring (_refresh_health_dot, _on_ollama_event,
_on_health_check_done) needs one, and reuses test_phase4_ui_logic.py's
stand-in rather than duplicating it - same install_stub()/_Any/_QtModule
machinery and the same fake-missing-package retry loop it uses to import
ui.main_window (see that file's own docstring for the whole scheme, and
test_tier5_ui_logic.py for the established precedent of reusing it from a
later tier's own test file instead of redefining it). No new widget stand-
ins are needed here - health_dot_lbl is a plain QLabel, already covered.

As with test_phase4_ui_logic.py: the UI-facing tests below verify Python
logic (what text/color/tooltip the health dot ends up with, which toast
each event produces) by calling the handler methods directly on an
uninitialized MainWindow (object.__new__, same trick test_phase4_ui_logic.py
uses) with just the attributes each method touches - not rendering, and not
the full _start_background_workers()/closeEvent() wiring itself (that's a
single, easily-eyeballed line each - see ui/main_window.py). If real PyQt6
is installed, those tests skip themselves (see REAL_QT below).

Run: python -m unittest discover -s tests -p "test_tier19_health_ollama.py"
(pytest will also collect it.)
"""
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: F401  (adds the repo root to sys.path)

from automation.health_check import CheckResult, Status, run_health_check, format_report, all_critical_ok
from brain.ollama_lifecycle import OllamaLifecycle

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_phase4_ui_logic as P4

# P4.THEME doesn't carry text_muted (nothing in that file needed it) -
# _refresh_health_dot's "all OK" branch does (matching ui/theme.py's real
# ThemeColors, same attribute mic_mini_lbl's own default styling already
# uses), so this file needs its own copy of P4.THEME plus that one field
# rather than mutating the shared namespace other test files also import.
import types as _types
THEME = _types.SimpleNamespace(**{**vars(P4.THEME), "text_muted": "#888"})


def _import_main_window_with_fakes():
    """Same retry loop P4.UiLogicTests.setUpClass uses to import
    ui.main_window - safe to run again even after P4's own setUpClass
    already did this once in this same process (no new widget stand-ins
    are needed for this file, so there's nothing to diverge)."""
    for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
        del sys.modules[name]
    for _ in range(30):
        try:
            import ui.main_window as mw
            return mw
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if not missing or missing in sys.modules:
                raise
            sys.modules[missing] = P4._QtModule(missing)


# =============================================================================
# automation/health_check.py - no GUI involved at all
# =============================================================================
class CheckResultPlumbingTests(unittest.TestCase):
    def test_run_health_check_never_raises_and_names_a_check_that_blows_up(self):
        with mock.patch("automation.health_check._CHECKS",
                        (lambda: (_ for _ in ()).throw(RuntimeError("kaboom")),)):
            results = run_health_check()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, Status.PROBLEM)
        self.assertIn("kaboom", results[0].message)

    def test_format_report_summarizes_by_the_worst_status_present(self):
        ok_only = [CheckResult("A", Status.OK, "fine")]
        self.assertIn("All systems OK.", format_report(ok_only))
        warn_only = [CheckResult("A", Status.OK, "fine"), CheckResult("B", Status.WARN, "meh")]
        self.assertIn("Worth a look: B.", format_report(warn_only))
        with_problem = warn_only + [CheckResult("C", Status.PROBLEM, "bad")]
        self.assertIn("PROBLEM with: C.", format_report(with_problem))

    def test_all_critical_ok_ignores_warn_but_not_problem(self):
        self.assertTrue(all_critical_ok([CheckResult("A", Status.OK, "x"), CheckResult("B", Status.WARN, "y")]))
        self.assertFalse(all_critical_ok([CheckResult("A", Status.PROBLEM, "x")]))

    def test_real_run_against_this_sandbox_returns_the_five_roadmap_checks(self):
        # Tier 19's roadmap line names them in this order: "Mic, speaker,
        # models, LLM, Tesseract." Whatever this sandbox's actual verdicts
        # are (no audio hardware, no GROQ_API_KEY, etc.), the five checks
        # themselves must always run and never raise out to the caller.
        results = run_health_check()
        self.assertEqual([r.name for r in results],
                         ["Microphone", "Speaker", "Voice models", "LLM provider", "Tesseract OCR"])
        for r in results:
            self.assertIsInstance(r.status, Status)
            self.assertTrue(r.message)


# =============================================================================
# brain/ollama_lifecycle.py - no GUI involved at all
# =============================================================================
class OllamaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.life = OllamaLifecycle()

    def _patch_relevant(self, enabled=True, autostart=True, auto_pull=True):
        return mock.patch.multiple(
            "brain.ollama_lifecycle.config",
            OLLAMA_ENABLED=enabled, OLLAMA_AUTOSTART=autostart, OLLAMA_AUTO_PULL_MODEL=auto_pull,
            OLLAMA_HOST="http://localhost:11434", OLLAMA_MODEL="llama3.1",
            OLLAMA_STARTUP_TIMEOUT_S=2, OLLAMA_PULL_TIMEOUT_S=5)

    def test_not_relevant_does_nothing_at_all(self):
        # Mock(name="groq") would NOT do what it looks like here - `name=`
        # is a reserved Mock constructor kwarg (sets its repr, not a
        # readable .name attribute: Mock(name="groq").name is itself
        # another Mock, not the string "groq") - a plain attribute-holder
        # avoids that trap.
        import types as _t
        with self._patch_relevant(enabled=False), \
                mock.patch("brain.ollama_lifecycle.llm_provider.resolve_provider",
                           return_value=_t.SimpleNamespace(name="groq")), \
                mock.patch("shutil.which") as which:
            self.life.ensure_ready(wait=True)
        which.assert_not_called()  # not even a PATH lookup - see _is_relevant()'s own docstring

    def test_relevant_via_primary_provider_even_with_the_fallback_disabled(self):
        # The other half of _is_relevant(): OLLAMA_ENABLED=False (the
        # fallback is off) but LLM_PROVIDER=ollama (it's the PRIMARY
        # provider instead) must still count as relevant.
        import types as _t
        with self._patch_relevant(enabled=False), \
                mock.patch("brain.ollama_lifecycle.llm_provider.resolve_provider",
                           return_value=_t.SimpleNamespace(name="ollama")), \
                mock.patch("shutil.which", return_value=None) as which:
            self.life.ensure_ready(wait=True)
        which.assert_called_once()  # got past _is_relevant() and looked for the binary

    def test_autostart_off_still_skips_everything(self):
        with self._patch_relevant(autostart=False), mock.patch("shutil.which") as which:
            self.life.ensure_ready(wait=True)
        which.assert_not_called()

    def test_ollama_not_on_path_is_a_quiet_no_op(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value=None):
            self.life.ensure_ready(wait=True)
        self.assertEqual(events, [])  # nothing to report - not installed is the default, expected case

    def test_already_running_with_model_present_reports_both_as_ready(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=["llama3.1:latest"]):
            self.life.ensure_ready(wait=True)
        self.assertEqual(events, ["already_running", "model_ready"])

    def test_not_reachable_starts_the_server_then_confirms_the_model(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=None), \
                mock.patch.object(self.life, "_start_server", return_value=True) as start, \
                mock.patch.object(self.life, "_wait_until_reachable", return_value=["llama3.1:latest"]):
            self.life.ensure_ready(wait=True)
        start.assert_called_once()
        self.assertEqual(events, ["server_started", "model_ready"])

    def test_server_wont_start_reports_a_problem_and_stops(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=None), \
                mock.patch.object(self.life, "_start_server", return_value=False):
            self.life.ensure_ready(wait=True)
        self.assertEqual(events, ["problem"])

    def test_server_starts_but_never_becomes_reachable_reports_a_problem(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=None), \
                mock.patch.object(self.life, "_start_server", return_value=True), \
                mock.patch.object(self.life, "_wait_until_reachable", return_value=None):
            self.life.ensure_ready(wait=True)
        self.assertEqual(events, ["problem"])

    def test_model_missing_triggers_a_pull(self):
        events = []
        self.life.add_listener(lambda e, m: events.append(e))
        with self._patch_relevant(), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=["some-other-model:latest"]), \
                mock.patch.object(self.life, "_pull_model", return_value=True) as pull:
            self.life.ensure_ready(wait=True)
        pull.assert_called_once_with("http://localhost:11434", "llama3.1")
        self.assertEqual(events, ["already_running", "model_ready"])

    def test_auto_pull_disabled_skips_pulling_even_when_missing(self):
        with self._patch_relevant(auto_pull=False), mock.patch("shutil.which", return_value="/usr/bin/ollama"), \
                mock.patch.object(self.life, "_query_tags", return_value=[]), \
                mock.patch.object(self.life, "_pull_model") as pull:
            self.life.ensure_ready(wait=True)
        pull.assert_not_called()

    def test_model_present_matches_a_bare_name_against_a_tagged_one(self):
        self.assertTrue(OllamaLifecycle._model_present(["llama3.1:latest"], "llama3.1"))
        self.assertTrue(OllamaLifecycle._model_present(["llama3.1"], "llama3.1"))
        self.assertFalse(OllamaLifecycle._model_present(["mistral:latest"], "llama3.1"))

    def test_ensure_ready_async_runs_on_its_own_thread_and_wait_until_done_sees_it_finish(self):
        with self._patch_relevant(enabled=False), \
                mock.patch("brain.ollama_lifecycle.llm_provider.resolve_provider",
                           return_value=mock.Mock(name="groq")):
            self.life.ensure_ready(wait=False)
            self.assertTrue(self.life.wait_until_done(timeout=2))

    def test_shutdown_with_nothing_started_is_a_no_op(self):
        self.life.shutdown()  # must not raise

    def test_shutdown_only_stops_a_server_this_instance_itself_started(self):
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None  # still running
        self.life._process = fake_proc
        self.life.shutdown()
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called()

    def test_shutdown_kills_if_terminate_does_not_exit_in_time(self):
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="ollama", timeout=3), None]
        self.life._process = fake_proc
        self.life.shutdown()
        fake_proc.kill.assert_called_once()


# =============================================================================
# ui/main_window.py wiring: the sidebar health dot + the two toast handlers
# =============================================================================
@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class HealthDotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        P4.install_stub()
        cls.mw = _import_main_window_with_fakes()

    def setUp(self):
        win = object.__new__(self.mw.MainWindow)
        win.theme = THEME
        win.health_dot_lbl = self.mw.QLabel("")
        self.win = win

    def test_all_healthy_shows_the_neutral_ok_dot(self):
        with mock.patch.object(self.mw.thread_supervisor, "status", return_value={
            "proactive_engine": {"alive": True, "restart_count": 0, "gave_up": False},
        }):
            self.win._refresh_health_dot()
        self.assertEqual(self.win.health_dot_lbl.text(), "●  SYSTEMS OK")
        self.assertEqual(self.win.health_dot_lbl.toolTip(), "All background workers running normally.")

    def test_a_recovered_crash_shows_a_warning_dot_naming_it(self):
        with mock.patch.object(self.mw.thread_supervisor, "status", return_value={
            "proactive_engine": {"alive": True, "restart_count": 1, "gave_up": False},
            "memory_consolidator": {"alive": True, "restart_count": 0, "gave_up": False},
        }):
            self.win._refresh_health_dot()
        self.assertEqual(self.win.health_dot_lbl.text(), "●  RECOVERED FROM A CRASH")
        tip = self.win.health_dot_lbl.toolTip()
        self.assertIn("proactive_engine", tip)
        self.assertNotIn("memory_consolidator", tip)

    def test_a_given_up_entry_shows_the_danger_dot_and_outranks_a_recovered_one(self):
        with mock.patch.object(self.mw.thread_supervisor, "status", return_value={
            "skill_loader": {"alive": False, "restart_count": 5, "gave_up": True},
            "proactive_engine": {"alive": True, "restart_count": 1, "gave_up": False},
        }):
            self.win._refresh_health_dot()
        self.assertEqual(self.win.health_dot_lbl.text(), "●  1 WORKER DOWN")
        tip = self.win.health_dot_lbl.toolTip()
        self.assertIn("skill_loader", tip)
        self.assertIn("proactive_engine", tip)  # mentioned too, as recovered

    def test_more_than_one_given_up_entry_pluralizes(self):
        with mock.patch.object(self.mw.thread_supervisor, "status", return_value={
            "a": {"alive": False, "restart_count": 5, "gave_up": True},
            "b": {"alive": False, "restart_count": 5, "gave_up": True},
        }):
            self.win._refresh_health_dot()
        self.assertEqual(self.win.health_dot_lbl.text(), "●  2 WORKERS DOWN")


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class OllamaEventToastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        P4.install_stub()
        cls.mw = _import_main_window_with_fakes()

    def setUp(self):
        win = object.__new__(self.mw.MainWindow)
        win.toast = P4.FakeToast()
        self.win = win

    def test_problem_shows_a_warning_toast(self):
        self.win._on_ollama_event("problem", "couldn't start ollama serve")
        message, icon = self.win.toast.shown[-1]
        self.assertEqual(icon, "warning")
        self.assertIn("couldn't start ollama serve", message)

    def test_server_started_shows_a_success_toast(self):
        self.win._on_ollama_event("server_started", "Started Ollama (http://localhost:11434).")
        self.assertEqual(self.win.toast.shown[-1][1], "success")

    def test_model_ready_shows_a_success_toast(self):
        self.win._on_ollama_event("model_ready", "Model 'llama3.1' is pulled and ready.")
        self.assertEqual(self.win.toast.shown[-1][1], "success")

    def test_routine_events_are_silent(self):
        self.win._on_ollama_event("already_running", "Ollama is already running at http://localhost:11434.")
        self.win._on_ollama_event("model_pulling", "Pulling Ollama model 'llama3.1'.")
        self.assertEqual(self.win.toast.shown, [])


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class HealthCheckToastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        P4.install_stub()
        cls.mw = _import_main_window_with_fakes()

    def setUp(self):
        win = object.__new__(self.mw.MainWindow)
        win.toast = P4.FakeToast()
        self.win = win

    def test_ok_results_produce_no_toasts(self):
        self.win._on_health_check_done([CheckResult("Tesseract OCR", Status.OK, "OK - /usr/bin/tesseract")])
        self.assertEqual(self.win.toast.shown, [])

    def test_problem_results_produce_an_error_toast(self):
        self.win._on_health_check_done([CheckResult("LLM provider", Status.PROBLEM, "PROBLEM - groq: ...")])
        message, icon = self.win.toast.shown[-1]
        self.assertEqual(icon, "error")
        self.assertIn("LLM provider", message)

    def test_warn_results_produce_a_warning_toast(self):
        self.win._on_health_check_done([CheckResult("Microphone", Status.WARN, "sounddevice not installed")])
        self.assertEqual(self.win.toast.shown[-1][1], "warning")

    def test_mixed_results_only_toast_the_non_ok_ones(self):
        self.win._on_health_check_done([
            CheckResult("Tesseract OCR", Status.OK, "fine"),
            CheckResult("Microphone", Status.WARN, "meh"),
            CheckResult("LLM provider", Status.PROBLEM, "bad"),
        ])
        self.assertEqual(len(self.win.toast.shown), 2)


if __name__ == "__main__":
    unittest.main()
