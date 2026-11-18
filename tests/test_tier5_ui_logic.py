"""Tier 5 / item 17 - UI-rendering logic tests for ui/workers.py and
ui/workflows_panel.py, WITHOUT a display or PyQt6.

Extends test_phase4_ui_logic.py's stand-in rather than duplicating it:
reuses its install_stub()/_Signal/_Any/_QtModule machinery and the same
fake-missing-package retry loop it uses to import ui.main_window, but adds
two widget stand-ins that file didn't need - QListWidget and QLineEdit, both
with real tracked state (items/current row, text) - since WorkflowsPanel's
step-builder is built entirely on top of those two. QInputDialog/QMessageBox
are left as the generic absorber and mocked per-test instead (mock.patch
works on a stand-in class exactly like a real one), since the handful of
methods that pop a modal dialog are better verified by controlling exactly
what the "dialog" returns than by teaching the stand-in to simulate one.

As with test_phase4_ui_logic.py: this verifies Python logic (filtering,
signal wiring, list mutation, which backend calls happen with which
arguments) - NOT rendering, drag-and-drop, or real dialog behavior. If real
PyQt6 is installed, these tests skip themselves (see REAL_QT below).
"""
import os
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

# Reuse the Phase 4 stand-in's building blocks instead of redefining them.
import test_phase4_ui_logic as P4


# ------------------------------------------------------------------ new stand-ins
class QListWidgetItem(P4._Any):
    """Tracks text and one piece of UserRole-style data - everything
    WorkflowsPanel and CommandPalette-style code actually use a list item
    for. Accepts the same (text) constructor QListWidgetItem(text) uses."""

    def __init__(self, text="", *a, **k):
        self._text = text
        self._data = {}

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def setData(self, role, value):
        self._data[role] = value

    def data(self, role):
        return self._data.get(role)


class QListWidget(P4._Any):
    """Tracks a real list of QListWidgetItem and a current row, so filtering/
    add/remove/move/select logic can be asserted on directly instead of
    against an absorber that forgets everything it's told."""

    def __init__(self, *a, **k):
        self._items = []
        self._current_row = -1
        self._blocked = False
        self.currentTextChanged = P4._BoundSignal()
        self.currentRowChanged = P4._BoundSignal()
        self.itemDoubleClicked = P4._BoundSignal()
        self.itemActivated = P4._BoundSignal()

    def addItem(self, item_or_text):
        item = item_or_text if isinstance(item_or_text, QListWidgetItem) else QListWidgetItem(str(item_or_text))
        self._items.append(item)
        if self._current_row == -1:
            self._set_row(0, emit=False)
        return item

    def clear(self):
        self._items = []
        self._current_row = -1

    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeItem(self, i):
        item = self._items.pop(i)
        if self._current_row >= len(self._items):
            self._current_row = len(self._items) - 1
        return item

    def insertItem(self, i, item):
        self._items.insert(i, item)

    def currentRow(self):
        return self._current_row

    def setCurrentRow(self, i):
        self._set_row(i, emit=True)

    def _set_row(self, i, emit):
        self._current_row = i
        if emit and not self._blocked:
            text = self._items[i].text() if 0 <= i < len(self._items) else ""
            self.currentTextChanged.emit(text)
            self.currentRowChanged.emit(i)

    def currentItem(self):
        return self.item(self._current_row)

    def blockSignals(self, flag):
        prev, self._blocked = self._blocked, flag
        return prev

    def clearSelection(self):
        self._current_row = -1

    def model(self):
        return types.SimpleNamespace(rowsMoved=P4._BoundSignal())

    def setDragDropMode(self, *a, **k):
        pass


class QLineEdit(P4._Any):
    """Tracks text and fires textChanged, same contract as the real widget
    for everything CommandPalette/WorkflowsPanel's step_input rely on."""

    def __init__(self, *a, **k):
        self._text = ""
        self.textChanged = P4._BoundSignal()
        self.returnPressed = P4._BoundSignal()

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t
        self.textChanged.emit(t)

    def clear(self):
        self.setText("")

    def setPlaceholderText(self, *a, **k):
        pass


def install_stub_with_list_widgets():
    """install_stub() plus the two overrides above, layered into the same
    OVERRIDES dict install_stub() builds - this IS "extending the stand-in",
    not a parallel one: everything else (QWidget, QLabel, QPushButton,
    QVBoxLayout/QHBoxLayout, signals, Qt.* constants) stays exactly what
    test_phase4_ui_logic.py already set up."""
    widgets = P4.install_stub()
    widgets.OVERRIDES["QListWidget"] = QListWidget
    widgets.OVERRIDES["QListWidgetItem"] = QListWidgetItem
    widgets.OVERRIDES["QLineEdit"] = QLineEdit
    return widgets


def import_with_fakes(module_name):
    """Same retry loop test_phase4_ui_logic.py uses for ui.main_window:
    import module_name, and for each ModuleNotFoundError bubbled up from a
    package that isn't installed here (voice/audio/vision packages, mainly),
    install a generic stand-in for just that package and retry.

    Also evicts every already-cached ui.* module first, not just the target:
    `from ui.common import *` binds QListWidget/QLineEdit/etc. at ui.common's
    OWN first-import time, so if test_phase4_ui_logic.py already imported
    ui.common against its (QListWidget-less) stand-in earlier in this same
    process, workflows_panel/workers would silently inherit that stale
    binding instead of the richer one install_stub_with_list_widgets() just
    installed - a fresh sys.modules entry for the target alone isn't enough."""
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


THEME = types.SimpleNamespace(
    bg_panel="#111", border_soft="#222", text_muted="#888", text_main="#eee", accent_1="#00f",
)


class FakeToast:
    def __init__(self):
        self.shown = []

    def show(self, message, icon="info", duration=3000):
        self.shown.append((message, icon))


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class WorkflowsPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub_with_list_widgets()
        cls.wp = import_with_fakes("ui.workflows_panel")

    def setUp(self):
        self.db_patch = mock.patch.object(self.wp, "db", autospec=False)
        self.mock_db = self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.mock_db.list_workflows_full.return_value = [("coffee", ["open chrome", "search coffee near me"])]

        main = types.SimpleNamespace(toast=FakeToast(), worker_pool=None, brain=None)
        self.panel = self.wp.WorkflowsPanel(main, THEME)
        self.main = main

    # ---------- import / construction ----------
    def test_module_imports_and_panel_builds_with_one_saved_workflow_listed(self):
        self.assertEqual(self.panel.name_list.count(), 1)
        self.assertEqual(self.panel.name_list.item(0).text(), "coffee")
        # _new_workflow() runs at the end of _setup_ui(), so a fresh panel
        # starts on an empty, unsaved workflow rather than the saved one.
        self.assertIsNone(self.panel._current_name)
        self.assertEqual(self.panel._current_steps(), [])

    # ---------- add / current_steps ----------
    def test_add_step_appends_text_and_clears_the_input(self):
        self.panel.step_input.setText("open chrome")
        self.panel._add_step()
        self.assertEqual(self.panel._current_steps(), ["open chrome"])
        self.assertEqual(self.panel.step_input.text(), "")
        self.assertTrue(self.panel._dirty)

    def test_add_step_ignores_blank_or_whitespace_only_input(self):
        self.panel.step_input.setText("   ")
        self.panel._add_step()
        self.assertEqual(self.panel._current_steps(), [])
        self.assertFalse(self.panel._dirty)

    # ---------- remove ----------
    def test_remove_step_takes_the_selected_row(self):
        for s in ("one", "two", "three"):
            self.panel.steps_list.addItem(s)
        self.panel.steps_list.setCurrentRow(1)
        self.panel._remove_step()
        self.assertEqual(self.panel._current_steps(), ["one", "three"])

    def test_remove_step_is_a_no_op_with_nothing_selected(self):
        self.panel.steps_list.clearSelection()
        self.panel._remove_step()  # must not raise
        self.assertEqual(self.panel._current_steps(), [])

    # ---------- move ----------
    def test_move_step_up_and_down_reorders_and_keeps_selection(self):
        for s in ("one", "two", "three"):
            self.panel.steps_list.addItem(s)
        self.panel.steps_list.setCurrentRow(2)
        self.panel._move_step(-1)
        self.assertEqual(self.panel._current_steps(), ["one", "three", "two"])
        self.assertEqual(self.panel.steps_list.currentRow(), 1)

    def test_move_step_clamps_at_both_ends(self):
        for s in ("one", "two"):
            self.panel.steps_list.addItem(s)
        self.panel.steps_list.setCurrentRow(0)
        self.panel._move_step(-1)  # already at top
        self.assertEqual(self.panel._current_steps(), ["one", "two"])
        self.panel.steps_list.setCurrentRow(1)
        self.panel._move_step(1)  # already at bottom
        self.assertEqual(self.panel._current_steps(), ["one", "two"])

    # ---------- selecting a saved workflow ----------
    def test_selecting_a_saved_workflow_loads_its_steps(self):
        self.panel._on_select_workflow("coffee")
        self.assertEqual(self.panel._current_name, "coffee")
        self.assertEqual(self.panel._current_steps(), ["open chrome", "search coffee near me"])
        self.assertFalse(self.panel._dirty)
        self.assertEqual(self.panel.status_lbl.text(), "2 step(s) · saved")

    def test_selecting_an_unknown_name_is_ignored(self):
        self.panel._on_select_workflow("not-a-real-workflow")
        self.assertIsNone(self.panel._current_name)

    # ---------- save ----------
    def test_save_workflow_prompts_for_a_name_when_new_and_saves_it(self):
        self.panel.step_input.setText("open chrome")
        self.panel._add_step()
        self.mock_db.save_workflow.return_value = True
        with mock.patch.object(self.wp.QInputDialog, "getText", return_value=("Coffee Run", True)):
            self.panel._save_workflow()
        self.mock_db.save_workflow.assert_called_once_with("Coffee Run", ["open chrome"])
        self.assertEqual(self.panel._current_name, "coffee run")
        self.assertIn(("Workflow 'coffee run' saved.", "success"), self.main.toast.shown)

    def test_save_workflow_without_steps_warns_instead_of_calling_db(self):
        with mock.patch.object(self.wp, "QMessageBox") as mock_box:
            self.panel._save_workflow()
        self.mock_db.save_workflow.assert_not_called()
        mock_box.warning.assert_called_once()

    def test_save_workflow_cancelled_name_prompt_does_not_call_db(self):
        self.panel.step_input.setText("open chrome")
        self.panel._add_step()
        with mock.patch.object(self.wp.QInputDialog, "getText", return_value=("", False)):
            self.panel._save_workflow()
        self.mock_db.save_workflow.assert_not_called()

    # ---------- delete ----------
    def test_delete_workflow_confirms_then_deletes_and_resets(self):
        self.panel._on_select_workflow("coffee")
        self.mock_db.delete_workflow.return_value = True
        with mock.patch.object(self.wp, "QMessageBox") as mock_box:
            mock_box.StandardButton.Yes, mock_box.StandardButton.No = 1, 2  # plain ints: real code ORs them together
            mock_box.question.return_value = 1
            self.panel._delete_workflow()
        self.mock_db.delete_workflow.assert_called_once_with("coffee")
        self.assertIn(("Workflow deleted.", "info"), self.main.toast.shown)
        self.assertIsNone(self.panel._current_name)  # _new_workflow() reset it

    def test_delete_workflow_declined_confirmation_keeps_it(self):
        self.panel._on_select_workflow("coffee")
        with mock.patch.object(self.wp, "QMessageBox") as mock_box:
            mock_box.StandardButton.Yes, mock_box.StandardButton.No = 1, 2
            mock_box.question.return_value = 2
            self.panel._delete_workflow()
        self.mock_db.delete_workflow.assert_not_called()
        self.assertEqual(self.panel._current_name, "coffee")


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ReminderWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.workers = import_with_fakes("ui.workers")

    def setUp(self):
        self.worker = self.workers.ReminderWorker(interval=1)
        # A composer.start() that returns the message unchanged, synchronously -
        # the real one runs the LLM reword on its own thread and returns a
        # waiter; tests only care that _check_* pass the right args through
        # and emit the right signal, not composer.py's own wording logic
        # (that's test_tier3_reminders.py's job).
        self.composer_patch = mock.patch.object(
            self.workers.reminder_composer, "start",
            side_effect=lambda msg, stage, **extra: (lambda: types.SimpleNamespace(text=f"[{stage}] {msg}")))
        self.composer_patch.start()
        self.addCleanup(self.composer_patch.stop)

    def _collect(self, signal):
        seen = []
        signal.connect(lambda *a: seen.append(a))
        return seen

    def test_check_due_emits_reminder_due_with_composed_text_and_marks_it_handled(self):
        with mock.patch.object(self.workers.db, "get_due_reminders", return_value=[(7, "take out the trash", None)]), \
             mock.patch.object(self.workers.db, "complete_or_reschedule_reminder") as mock_complete, \
             mock.patch.object(self.workers.ctx, "classify", return_value="chore"), \
             mock.patch.object(self.workers.ctx, "effective_policy",
                               return_value=types.SimpleNamespace(post_due_followup=False)):
            seen = self._collect(self.worker.reminder_due)
            self.worker._check_due()
        self.assertEqual(seen, [("[due] take out the trash", 7)])
        mock_complete.assert_called_once_with(7, recurrence=None, followup_at_iso=None)

    def test_check_due_schedules_a_followup_when_policy_says_to(self):
        with mock.patch.object(self.workers.db, "get_due_reminders", return_value=[(9, "study for the exam", None)]), \
             mock.patch.object(self.workers.db, "complete_or_reschedule_reminder") as mock_complete, \
             mock.patch.object(self.workers.ctx, "classify", return_value="study"), \
             mock.patch.object(self.workers.ctx, "effective_policy",
                               return_value=types.SimpleNamespace(post_due_followup=True)):
            self.worker._check_due()
        self.assertIsNotNone(mock_complete.call_args.kwargs["followup_at_iso"])

    def test_check_upcoming_emits_with_minutes_until(self):
        with mock.patch.object(self.workers.db, "get_reminders_due_soon", return_value=[(3, "call mom", "iso-time")]), \
             mock.patch.object(self.workers.db, "mark_upcoming_notified") as mock_mark, \
             mock.patch.object(self.workers.ctx, "minutes_until", return_value=5):
            seen = self._collect(self.worker.reminder_upcoming)
            self.worker._check_upcoming()
        self.assertEqual(seen, [("[upcoming] call mom", 3, 5)])
        mock_mark.assert_called_once_with(3)

    # 🔧 Tier 18: test_check_followups_emits_for_each_due_followup removed -
    # _check_followups/reminder_followup no longer exist on ReminderWorker
    # (moved to automation/proactive.py's _check_reminder_followups(), see
    # that class's own docstring in ui/workers.py). Equivalent coverage now
    # lives in tests/test_tier18_proactive_triggers.py.

    def test_check_due_with_nothing_due_emits_nothing(self):
        with mock.patch.object(self.workers.db, "get_due_reminders", return_value=[]):
            seen = self._collect(self.worker.reminder_due)
            self.worker._check_due()
        self.assertEqual(seen, [])


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MicAndSimpleWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.workers = import_with_fakes("ui.workers")

    def _collect(self, signal):
        seen = []
        signal.connect(lambda *a: seen.append(a))
        return seen

    def test_mic_worker_wakes_on_phrase_then_routes_commands_then_sleeps(self):
        worker = self.workers.ContinuousMicWorker(wake_phrases=["hello wave"])
        # An acoustic gate trained on the user's own wake phrase (see
        # ContinuousMicWorker._gate_in_use) - handed to listen() while asleep.
        worker.wake_gate = types.SimpleNamespace(wanted=lambda: True, engine="auto", phrase="hello wave")
        modes = self._collect(worker.mode_update)
        texts = self._collect(worker.text_ready)
        interruptions = self._collect(worker.interruption_signal)
        script = iter(["hello wave", "what's the weather", "go to sleep"])
        gates = []

        def fake_listen(wake_gate=None):
            gates.append(wake_gate is not None)
            try:
                return next(script)
            except StopIteration:
                worker.stop()
                return None

        with mock.patch.object(self.workers.stt_engine, "listen", side_effect=fake_listen), \
                mock.patch.object(self.workers.tts_engine, "_utterance_active", False), \
                mock.patch.object(self.workers.tts_engine, "_stream_active", False):
            worker.run()

        self.assertEqual(worker.mode, "SLEEPING")
        self.assertEqual(texts, [("what's the weather",)])
        # Tier 11 barge-in gate: with Wave silent, waking and asking don't
        # "interrupt" anything; only the sleep command does (it stops
        # whatever Wave might be saying). Before Tier 11 all three did.
        self.assertEqual(len(interruptions), 1)
        self.assertIn(("AWAKE",), modes)
        self.assertIn(("SLEEPING",), modes)
        # The wake-word gate is handed to listen() only while asleep.
        self.assertEqual(gates[:3], [True, False, False])

    def test_mic_worker_reports_recognition_failure_only_while_awake(self):
        worker = self.workers.ContinuousMicWorker()
        failures = self._collect(worker.recognition_failed)
        script = iter(["", "hello wave", ""])  # unclear speech while asleep, then wake, then unclear again

        def fake_listen(wake_gate=None):
            try:
                return next(script)
            except StopIteration:
                worker.stop()
                return None

        with mock.patch.object(self.workers.stt_engine, "listen", side_effect=fake_listen):
            worker.run()

        self.assertEqual(len(failures), 1)  # only the one while AWAKE counted

    def test_tts_worker_emits_false_when_speak_and_wait_raises(self):
        worker = self.workers.TTSWorker("hello")
        seen = self._collect(worker.finished_speaking)
        with mock.patch.object(self.workers.tts_engine, "speak_and_wait", side_effect=RuntimeError("boom")):
            worker.run()
        self.assertEqual(seen, [(False,)])

    def test_tts_worker_emits_true_on_normal_playback(self):
        worker = self.workers.TTSWorker("hello")
        seen = self._collect(worker.finished_speaking)
        with mock.patch.object(self.workers.tts_engine, "speak_and_wait", return_value=True):
            worker.run()
        self.assertEqual(seen, [(True,)])

    def test_action_worker_turns_an_exception_into_a_friendly_result(self):
        def boom():
            raise ValueError("bad input")
        worker = self.workers.ActionWorker(boom)
        seen = self._collect(worker.result_ready)
        worker.run()
        self.assertEqual(seen, [("Something went wrong: bad input",)])

    def test_action_worker_falls_back_to_done_for_a_non_string_result(self):
        worker = self.workers.ActionWorker(lambda: None)
        seen = self._collect(worker.result_ready)
        worker.run()
        self.assertEqual(seen, [("Done.",)])

    def test_action_worker_passes_through_a_real_string_result(self):
        worker = self.workers.ActionWorker(lambda x: f"opened {x}", "chrome")
        seen = self._collect(worker.result_ready)
        worker.run()
        self.assertEqual(seen, [("opened chrome",)])


if __name__ == "__main__":
    unittest.main()
