"""Tier 21 items: six new DB-backed management pages - Memory, Reminders &
Timers, Notes & To-dos, Calendar, Skills, Performance - plus their wiring
into ui/main_window.py's NAV_ITEMS / page stack / command palette.

Same convention as every other UI-logic test in this project (see
test_phase4_ui_logic.py's and test_tier5_ui_logic.py's module docstrings):
verifies Python-level behavior (DB round-trips through the panel's own
methods, list contents, button-triggered state changes) with the fake-Qt
stand-in, NOT real rendering - there's no display or PyQt6 in this sandbox.
Skips itself if real PyQt6 is importable.
"""
import os
import sys
import unittest

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_tier5_ui_logic as T
import test_phase4_ui_logic as P4


class _FakeTableItem(P4._Any):
    """Minimal QTableWidgetItem stand-in with real tracked text - PerformancePanel
    only ever constructs one from a string and reads it back with .text()."""
    def __init__(self, text="", *a, **k):
        self._text = text

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t


class _FakeCheckBox(P4._Any):
    """Minimal QCheckBox stand-in with real tracked checked state -
    RemindersPanel's "Repeat daily" checkbox needs this to actually track
    False by default; the generic _Any absorber returns a truthy stub
    object from isChecked() regardless of what setChecked() was called
    with, which would silently hide a real default-recurrence bug."""
    def __init__(self, label="", *a, **k):
        self._checked = False

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self._checked = bool(value)


class _FakeTableWidget(P4._Any):
    """Minimal QTableWidget stand-in with real tracked row count and cell
    items - just enough for PerformancePanel's setRowCount/setItem/rowCount/
    item() usage. Everything else (headers, selection, etc) stays absorbed
    by the generic _Any base class."""
    def __init__(self, rows=0, cols=0, *a, **k):
        self._rows = rows
        self._cols = cols
        self._cells = {}

    def setRowCount(self, n):
        self._rows = n
        self._cells = {k: v for k, v in self._cells.items() if k[0] < n}

    def rowCount(self):
        return self._rows

    def setItem(self, row, col, item):
        self._cells[(row, col)] = item

    def item(self, row, col):
        return self._cells.get((row, col))


def _install_fakes():
    S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                           hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                           typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                           size=lambda: (1, 1))
    S.install_fake_module("pywhatkit")
    widgets = T.install_stub_with_list_widgets()
    # Tier 21: PerformancePanel is the first panel in this project to use
    # QTableWidget - extend the stand-in the same way test_tier5_ui_logic.py
    # extended test_phase4_ui_logic.py's for QListWidget/QLineEdit.
    widgets.OVERRIDES["QTableWidget"] = _FakeTableWidget
    widgets.OVERRIDES["QTableWidgetItem"] = _FakeTableItem
    widgets.OVERRIDES["QCheckBox"] = _FakeCheckBox


class FakeToast:
    def __init__(self):
        self.shown = []

    def show(self, message, icon="info", duration=3000):
        self.shown.append((message, icon))


class _FakeReminderBrain:
    """Stands in for WaveBrain's _set_reminder, same reasoning
    test_tier16_notes_todos.py's _FakeReminderBrain already documents:
    dateparser isn't installed in every environment this suite runs in
    (it's an optional dependency), so RemindersPanel's OWN logic (does it
    clear the inputs and toast on success? does it leave the DB alone on a
    recognized failure message?) is pinned here without depending on
    dateparser's install status or real NLP parsing of 'in 20 minutes'."""
    def __init__(self, db, result_text="Reminder set."):
        self._db = db
        self.result_text = result_text
        self.calls = []

    def _set_reminder(self, message, when_text, recurrence=""):
        self.calls.append((message, when_text, recurrence))
        if not when_text:
            return "I need to know when to remind you."
        self._db.save_reminder(message or "Reminder", self._future_iso(),
                                recurrence if recurrence == "daily" else None)
        return self.result_text

    @staticmethod
    def _future_iso():
        import datetime
        return (datetime.datetime.now() + datetime.timedelta(minutes=20)).isoformat()


class FakeMain:
    """Just enough of MainWindow for these panels: a .toast, and a .brain
    stand-in (see _FakeReminderBrain above) for RemindersPanel."""
    def __init__(self):
        self.toast = FakeToast()
        self.brain = None  # set per-test by RemindersPanelTests where needed


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ManagementPagesTestsBase(unittest.TestCase):
    """Shared tmp-DB isolation, same pattern as test_tier21_db_precise_crud.py."""
    @classmethod
    def setUpClass(cls):
        _install_fakes()
        cls.theme_mod = T.import_with_fakes("ui.theme")
        cls.db_mod = T.import_with_fakes("database.db_manager")

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.db = self.db_mod.db
        self.old_path = self.db.db_path
        self.db.db_path = os.path.join(self.tmp, "t.db")
        self.db.init_db()
        self.theme = self.theme_mod.ThemeColors()
        self.main = FakeMain()

    def tearDown(self):
        self.db.db_path = self.old_path


class MemoryPanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.memory_panel")

    def test_new_fact_appears_in_list_and_db(self):
        panel = self.mod.MemoryPanel(self.main, self.theme)
        panel.key_input.setText("favorite color")
        panel.value_input.setText("blue")
        panel._save_fact()
        self.assertEqual(self.db.get_memory("favorite color"), "blue")
        self.assertIn("favorite color", panel._facts)
        self.assertTrue(any("saved" in msg.lower() or "remembered" in msg.lower()
                             for msg, _ in self.main.toast.shown))

    def test_delete_fact_removes_it(self):
        self.db.save_memory("test key", "test value")
        panel = self.mod.MemoryPanel(self.main, self.theme)
        panel._on_select_key("test key")
        from unittest import mock
        with mock.patch.object(self.mod, "QMessageBox") as mock_box:
            mock_box.question.return_value = mock_box.StandardButton.Yes
            panel._delete_fact()
        self.assertIsNone(self.db.get_memory("test key"))

    def test_search_filters_by_key_or_value(self):
        self.db.save_memory("favorite color", "blue")
        self.db.save_memory("favorite food", "pizza")
        panel = self.mod.MemoryPanel(self.main, self.theme)
        panel._reload_list(filter_text="food")
        self.assertEqual(self.key_list_items(panel), ["favorite food"])

    def test_list_rows_carry_a_provenance_badge(self):
        """Tier 21 roadmap line for this page: "with an auto-vs-explicit
        badge" - each row is prefixed with one of the two badges, not just
        the bare key."""
        self.db.save_memory("explicit one", "x", source="explicit")
        self.db.save_memory("auto one", "y", source="auto_consolidated")
        panel = self.mod.MemoryPanel(self.main, self.theme)
        raw = {panel.key_list.item(i).text() for i in range(panel.key_list.count())}
        self.assertIn(f"{self.mod._BADGE['explicit']}\texplicit one", raw)
        self.assertIn(f"{self.mod._BADGE['auto_consolidated']}\tauto one", raw)

    def test_export_writes_every_fact_with_provenance(self):
        import json
        import tempfile
        import os
        self.db.save_memory("favorite color", "blue", source="explicit")
        panel = self.mod.MemoryPanel(self.main, self.theme)
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            from unittest import mock
            with mock.patch.object(self.mod, "QFileDialog") as dlg:
                dlg.getSaveFileName.return_value = (path, "JSON (*.json)")
                panel._export_facts()
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["key"], "favorite color")
            self.assertEqual(rows[0]["value"], "blue")
            self.assertEqual(rows[0]["source"], "explicit")
        finally:
            os.remove(path)

    def test_export_with_no_facts_does_not_prompt_for_a_file(self):
        from unittest import mock
        panel = self.mod.MemoryPanel(self.main, self.theme)
        with mock.patch.object(self.mod, "QFileDialog") as dlg, \
             mock.patch.object(self.mod, "QMessageBox") as box:
            panel._export_facts()
        dlg.getSaveFileName.assert_not_called()
        box.information.assert_called_once()

    @staticmethod
    def key_list_items(panel):
        return [panel.key_list.item(i).text().split("\t", 1)[-1] for i in range(panel.key_list.count())]

    def test_provenance_label_reflects_source(self):
        self.db.save_memory("k", "v", source="explicit")
        panel = self.mod.MemoryPanel(self.main, self.theme)
        panel._on_select_key("k")
        self.assertIn("told me this directly", panel.provenance_lbl.text())


class RemindersPanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.reminders_panel")

    def test_add_reminder_via_natural_language_saves_and_lists(self):
        self.main.brain = _FakeReminderBrain(self.db)
        panel = self.mod.RemindersPanel(self.main, self.theme)
        panel.message_input.setText("check the oven")
        panel.when_input.setText("in 20 minutes")
        panel._add_reminder()
        self.assertEqual(self.main.brain.calls, [("check the oven", "in 20 minutes", "none")])
        rows = self.db.get_upcoming_reminders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "check the oven")
        self.assertTrue(any("Reminder set" in msg for msg, _ in self.main.toast.shown))

    def test_cancel_selected_removes_the_right_reminder(self):
        self.main.brain = _FakeReminderBrain(self.db)
        a = self.db.save_reminder("alpha", self._future_iso())
        b = self.db.save_reminder("beta", self._future_iso())
        panel = self.mod.RemindersPanel(self.main, self.theme)
        panel._reload_list()
        # Select the row holding id `a`.
        for i in range(panel.list_widget.count()):
            item = panel.list_widget.item(i)
            if item.data(self.mod.Qt.ItemDataRole.UserRole) == a:
                panel.list_widget.setCurrentItem(item)
                break
        panel._cancel_selected()
        remaining_ids = [r[0] for r in self.db.get_upcoming_reminders()]
        self.assertEqual(remaining_ids, [b])

    def test_add_reminder_with_unparseable_time_does_not_save(self):
        panel = self.mod.RemindersPanel(self.main, self.theme)
        panel.message_input.setText("thing")
        panel.when_input.setText("")
        panel._add_reminder()
        self.assertEqual(self.db.get_upcoming_reminders(), [])

    @staticmethod
    def _future_iso():
        import datetime
        return (datetime.datetime.now() + datetime.timedelta(hours=1)).isoformat()


class NotesTodosPanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.notes_todos_panel")

    def test_add_todo_appears_in_pending_list(self):
        panel = self.mod.NotesTodosPanel(self.main, self.theme)
        panel.todo_input.setText("buy milk")
        panel._add_todo()
        self.assertEqual(len(self.db.list_todos()), 1)

    def test_complete_selected_todo_removes_it_from_pending(self):
        a = self.db.add_todo("call mom")
        b = self.db.add_todo("call mom again")
        panel = self.mod.NotesTodosPanel(self.main, self.theme)
        panel._reload_todos()
        for i in range(panel.todo_list.count()):
            item = panel.todo_list.item(i)
            if item.data(self.mod.Qt.ItemDataRole.UserRole) == a:
                panel.todo_list.setCurrentItem(item)
                break
        panel._complete_selected_todo()
        remaining_ids = [r[0] for r in self.db.list_todos()]
        self.assertEqual(remaining_ids, [b])

    def test_delete_selected_todo_removes_only_that_row(self):
        a = self.db.add_todo("x")
        b = self.db.add_todo("y")
        panel = self.mod.NotesTodosPanel(self.main, self.theme)
        panel._reload_todos()
        for i in range(panel.todo_list.count()):
            item = panel.todo_list.item(i)
            if item.data(self.mod.Qt.ItemDataRole.UserRole) == a:
                panel.todo_list.setCurrentItem(item)
                break
        panel._delete_selected_todo()
        remaining_ids = [r[0] for r in self.db.list_todos()]
        self.assertEqual(remaining_ids, [b])

    def test_add_and_delete_quick_note(self):
        panel = self.mod.NotesTodosPanel(self.main, self.theme)
        panel.note_input.setText("remember this")
        panel._add_note()
        self.assertEqual(len(self.db.list_quick_notes()), 1)
        for i in range(panel.notes_list.count()):
            item = panel.notes_list.item(i)
            if item.data(self.mod.Qt.ItemDataRole.UserRole) is not None:
                panel.notes_list.setCurrentItem(item)
                break
        panel._delete_selected_note()
        self.assertEqual(self.db.list_quick_notes(), [])


class CalendarPanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.calendar_panel")

    def test_add_event_via_natural_language_saves_and_lists(self):
        """CalendarPanel._add_event calls CalendarManager.create_event()
        directly (see calendar_panel.py's own module docstring) - its
        dateparser-based `when` parsing is stood in for here exactly the
        way test_phase6_calendar.py's ParsingFreeTests do (mocking
        automation.calendar_manager._parse_point), since dateparser isn't
        installed in every environment this suite runs in."""
        import datetime
        from unittest import mock
        import automation.calendar_manager as cal_mod
        start = datetime.datetime.now() + datetime.timedelta(days=1)
        panel = self.mod.CalendarPanel(self.main, self.theme)
        panel.title_input.setText("Team standup")
        panel.when_input.setText("tomorrow at 9am")
        with mock.patch.object(cal_mod, "_parse_point", return_value=start):
            panel._add_event()
        now = datetime.datetime.now()
        rows = self.db.get_calendar_events_between(now.isoformat(),
                                                     (now + datetime.timedelta(days=5)).isoformat())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "Team standup")

    def test_cancel_selected_removes_the_right_event(self):
        a = self.db.save_calendar_event("Alpha", self._future_iso())
        b = self.db.save_calendar_event("Beta", self._future_iso())
        panel = self.mod.CalendarPanel(self.main, self.theme)
        panel._reload_list()
        for i in range(panel.list_widget.count()):
            item = panel.list_widget.item(i)
            if item.data(self.mod.Qt.ItemDataRole.UserRole) == a:
                panel.list_widget.setCurrentItem(item)
                break
        panel._cancel_selected()
        import datetime
        now = datetime.datetime.now()
        remaining_ids = [r[0] for r in self.db.get_calendar_events_between(
            now.isoformat(), (now + datetime.timedelta(days=65)).isoformat())]
        self.assertEqual(remaining_ids, [b])

    @staticmethod
    def _future_iso():
        import datetime
        return (datetime.datetime.now() + datetime.timedelta(days=1)).isoformat()


class SkillsPanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.skills_panel")

    def test_catalog_lists_registered_skills(self):
        panel = self.mod.SkillsPanel(self.main, self.theme)
        # The real skills_registry has real built-in skills registered at
        # import time (brain/handlers/*.py) - just assert it's non-trivially
        # populated, not an exact count (which would be an unrelated-change
        # tripwire every time a skill is added elsewhere in the project).
        self.assertGreater(panel.catalog_list.count(), 10)

    def test_search_filters_catalog(self):
        panel = self.mod.SkillsPanel(self.main, self.theme)
        panel.search_input.setText("zzzznonexistentskillzzzz")
        panel._reload_catalog()
        self.assertEqual(panel.catalog_list.count(), 1)  # the "no match" placeholder row
        self.assertIn("No skills match", panel.catalog_list.item(0).text())

    def test_status_label_reports_user_skills_directory_state(self):
        panel = self.mod.SkillsPanel(self.main, self.theme)
        self.assertIn("Directory:", panel.status_lbl.text())

    # ---------- Tier 21: "user skills, enable/disable" ----------
    def _loader_on_tmpdir(self):
        """A real UserSkillLoader bound to a throw-away folder and registry
        (never the shared global one) - mocked in as get_loader() so the
        panel's buttons exercise the real brain/skill_loader.py logic
        without touching anything outside this test's own tmpdir."""
        import brain.skill_loader as skill_loader_mod
        from brain.skills_registry import SkillRegistry
        skills_dir = os.path.join(self.tmp, "user_skills")
        loader = skill_loader_mod.UserSkillLoader(skills_dir, registry=SkillRegistry(), poll_interval=0.1)
        self.addCleanup(loader.stop)
        return loader

    def _write_skill(self, loader, filename, body):
        os.makedirs(loader.directory, exist_ok=True)
        with open(os.path.join(loader.directory, filename), "w", encoding="utf-8") as fh:
            fh.write(body)

    def test_files_list_shows_each_known_drop_in(self):
        from unittest import mock
        loader = self._loader_on_tmpdir()
        self._write_skill(loader, "hi.py",
                           '@skill("say_hi", "Says hi.", {}, [])\ndef say_hi(brain, args):\n    return "hi"\n')
        loader.load_now()
        with mock.patch.object(self.mod, "get_loader", return_value=loader):
            panel = self.mod.SkillsPanel(self.main, self.theme)
        rows = [panel.files_list.item(i).text() for i in range(panel.files_list.count())]
        self.assertEqual(rows, ["✓\thi.py"])

    def test_disable_button_turns_off_a_loaded_skill(self):
        from unittest import mock
        loader = self._loader_on_tmpdir()
        self._write_skill(loader, "hi.py",
                           '@skill("say_hi", "Says hi.", {}, [])\ndef say_hi(brain, args):\n    return "hi"\n')
        loader.load_now()
        with mock.patch.object(self.mod, "get_loader", return_value=loader):
            panel = self.mod.SkillsPanel(self.main, self.theme)
            panel.files_list.setCurrentRow(0)
            self.assertEqual(panel.toggle_btn.text(), "Disable")
            panel._toggle_selected_file()
        self.assertTrue(loader.is_disabled("hi.py"))
        self.assertNotIn("say_hi", loader.registry)
        rows = [panel.files_list.item(i).text() for i in range(panel.files_list.count())]
        self.assertEqual(rows, ["⏸\thi.py"])
        self.assertTrue(any("disabled hi.py" in msg.lower() for msg, _ in self.main.toast.shown))

    def test_enable_button_turns_a_disabled_skill_back_on(self):
        from unittest import mock
        loader = self._loader_on_tmpdir()
        self._write_skill(loader, "hi.py",
                           '@skill("say_hi", "Says hi.", {}, [])\ndef say_hi(brain, args):\n    return "hi"\n')
        loader.load_now()
        loader.set_disabled("hi.py", True)
        with mock.patch.object(self.mod, "get_loader", return_value=loader):
            panel = self.mod.SkillsPanel(self.main, self.theme)
            panel.files_list.setCurrentRow(0)
            self.assertEqual(panel.toggle_btn.text(), "Enable")
            panel._toggle_selected_file()
        self.assertFalse(loader.is_disabled("hi.py"))
        self.assertEqual(loader.registry.get_handler("say_hi")(None, {}), "hi")

    def test_rescan_keeps_a_disabled_file_disabled(self):
        from unittest import mock
        loader = self._loader_on_tmpdir()
        self._write_skill(loader, "hi.py",
                           '@skill("say_hi", "Says hi.", {}, [])\ndef say_hi(brain, args):\n    return "hi"\n')
        loader.load_now()
        loader.set_disabled("hi.py", True)
        with mock.patch.object(self.mod, "get_loader", return_value=loader):
            panel = self.mod.SkillsPanel(self.main, self.theme)
            panel._rescan()
        self.assertNotIn("say_hi", loader.registry)
        rows = [panel.files_list.item(i).text() for i in range(panel.files_list.count())]
        self.assertEqual(rows, ["⏸\thi.py"])


class PerformancePanelTests(ManagementPagesTestsBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mod = T.import_with_fakes("ui.performance_panel")

    def test_empty_log_shows_guidance_not_a_crash(self):
        from unittest import mock
        with mock.patch.object(self.mod, "load_rows", return_value=[]):
            panel = self.mod.PerformancePanel(self.main, self.theme)
        self.assertEqual(panel.path_table.rowCount(), 0)
        self.assertIn("No voice turns logged yet", panel.empty_lbl.text())

    def test_populated_log_fills_path_table(self):
        from unittest import mock
        fake_rows = [{"source": "voice", "path": "fast_path", "ttfa_ms": 500, "heard": "volume up"}]
        fake_summary = [{"path": "fast path", "n": 1, "measured": 1, "p50_ms": 500, "p90_ms": 500,
                          "p50_first_token_ms": None, "p50_stt_ms": None, "languages": ["en"],
                          "target_ms": 800.0}]
        with mock.patch.object(self.mod, "load_rows", return_value=fake_rows), \
             mock.patch.object(self.mod, "summarize", return_value=fake_summary), \
             mock.patch.object(self.mod, "by_language", return_value=[]):
            panel = self.mod.PerformancePanel(self.main, self.theme)
        self.assertEqual(panel.path_table.rowCount(), 1)
        self.assertEqual(panel.path_table.item(0, 0).text(), "fast path")

    # ---------- Tier 21: "Local-only usage stats, opt-in" section ----------
    def test_usage_stats_off_by_default_shows_how_to_turn_it_on(self):
        from unittest import mock
        with mock.patch.object(self.mod, "load_rows", return_value=[]), \
             mock.patch.object(self.mod.usage_stats, "summarize", return_value={"enabled": False}):
            panel = self.mod.PerformancePanel(self.main, self.theme)
        self.assertIn("Settings > Privacy", panel.usage_lbl.text())
        self.assertIn("never sent anywhere", panel.usage_lbl.text())

    def test_usage_stats_enabled_with_no_activity_says_so(self):
        from unittest import mock
        stats = {"enabled": True, "days": 7, "total_turns": 0, "by_source": {},
                  "success_rate": None, "top_skills": []}
        with mock.patch.object(self.mod, "load_rows", return_value=[]), \
             mock.patch.object(self.mod.usage_stats, "summarize", return_value=stats):
            panel = self.mod.PerformancePanel(self.main, self.theme)
        self.assertIn("No activity logged", panel.usage_lbl.text())

    def test_usage_stats_enabled_with_data_shows_the_summary(self):
        from unittest import mock
        stats = {"enabled": True, "days": 7, "total_turns": 12,
                  "by_source": {"voice": 9, "text": 3}, "success_rate": 0.917,
                  "top_skills": [("open_app", 5), ("set_reminder", 3)]}
        with mock.patch.object(self.mod, "load_rows", return_value=[]), \
             mock.patch.object(self.mod.usage_stats, "summarize", return_value=stats):
            panel = self.mod.PerformancePanel(self.main, self.theme)
        text = panel.usage_lbl.text()
        self.assertIn("12 turns", text)
        self.assertIn("voice: 9", text)
        self.assertIn("open_app (5)", text)
        self.assertIn("92%", text)

    def test_usage_stats_read_failure_does_not_crash_the_page(self):
        from unittest import mock
        with mock.patch.object(self.mod, "load_rows", return_value=[]), \
             mock.patch.object(self.mod.usage_stats, "summarize", side_effect=RuntimeError("boom")):
            panel = self.mod.PerformancePanel(self.main, self.theme)   # must not raise
        self.assertIn("Couldn't read usage stats", panel.usage_lbl.text())


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MainWindowWiringTests(unittest.TestCase):
    """Verifies NAV_ITEMS/index constants/command-palette entries agree with
    each other, without constructing a full MainWindow (far too much else
    it wires up - workers, audio, etc - for a pure wiring-consistency check)."""
    @classmethod
    def setUpClass(cls):
        _install_fakes()
        cls.mod = T.import_with_fakes("ui.main_window")

    def test_nav_items_indices_are_unique_and_sequential(self):
        indices = [idx for _, _, idx in self.mod.NAV_ITEMS]
        self.assertEqual(indices, list(range(len(self.mod.NAV_ITEMS))))

    def test_new_page_index_constants_match_nav_items(self):
        by_label = {label: idx for label, _, idx in self.mod.NAV_ITEMS}
        self.assertEqual(by_label["Memory"], self.mod.MEMORY_PAGE_INDEX)
        self.assertEqual(by_label["Reminders"], self.mod.REMINDERS_PAGE_INDEX)
        self.assertEqual(by_label["Notes & To-dos"], self.mod.NOTES_TODOS_PAGE_INDEX)
        self.assertEqual(by_label["Calendar"], self.mod.CALENDAR_PAGE_INDEX)
        self.assertEqual(by_label["Skills"], self.mod.SKILLS_PAGE_INDEX)
        self.assertEqual(by_label["Performance"], self.mod.PERFORMANCE_PAGE_INDEX)
        self.assertEqual(by_label["Settings"], self.mod.SETTINGS_PAGE_INDEX)

    def test_pre_existing_page_indices_unchanged(self):
        """The whole point of inserting the new pages between Workflows and
        Settings: every index that existed before Tier 21 must still point
        at the same page."""
        by_label = {label: idx for label, _, idx in self.mod.NAV_ITEMS}
        self.assertEqual(by_label["Home"], 0)
        self.assertEqual(by_label["Chat"], 1)
        self.assertEqual(by_label["Activity"], self.mod.ACTIVITY_PAGE_INDEX)
        self.assertEqual(self.mod.ACTIVITY_PAGE_INDEX, 8)
        self.assertEqual(by_label["Workflows"], self.mod.WORKFLOWS_PAGE_INDEX)
        self.assertEqual(self.mod.WORKFLOWS_PAGE_INDEX, 9)


if __name__ == "__main__":
    unittest.main()
