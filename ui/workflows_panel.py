"""
ui/workflows_panel.py — Tier 3 item 9.

Workflows (brain/groq_core.py's create_workflow/run_workflow/list_workflows,
database/db_manager.py's workflows table) used to be chat-only: you could
create one by telling Wave a name and a list of steps in one sentence, run
it by name, and list names - but there was no way to SEE a workflow's steps,
reorder them, fix a typo in one step, delete one you no longer want, or
build one without going through the voice/chat flow at all.

WorkflowsPanel is a real page for that: a list of saved workflows on the
left, and a simple visual step-builder on the right (add/remove/reorder/
edit steps, rename, save, run, delete). "Visual" here means drag-to-reorder
and click-to-edit, not a node graph - workflow steps are just an ordered
list of voice-command strings (see save_workflow's docstring), so a plain
ordered list IS the natural, honest representation of what a workflow
actually is under the hood; a node-graph editor would be decoration around
the same data; it's not a Scratch-style branching automation runner.
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from database.db_manager import db


class WorkflowsPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._current_name = None   # name of the workflow being edited, or None = unsaved/new
        self._dirty = False
        self._setup_ui()
        self._reload_list()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Left: saved workflows ----
        left = QFrame()
        left.setFixedWidth(260)
        left.setStyleSheet(f"background: {self._theme.bg_panel}; border-right: 1px solid {self._theme.border_soft};")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 20, 16, 20)
        lv.setSpacing(10)
        lv.addWidget(make_section_heading("WORKFLOWS", self._theme))

        self.name_list = QListWidget()
        self.name_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; }}
            QListWidget::item {{ padding: 10px 12px; color: {self._theme.text_muted}; border-radius: 6px; margin-bottom: 2px; }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.05); color: {self._theme.text_main}; }}
            QListWidget::item:selected {{ background: {self._theme.accent_1}30; color: {self._theme.accent_1}; }}
        """)
        self.name_list.currentTextChanged.connect(self._on_select_workflow)
        lv.addWidget(self.name_list, 1)

        new_btn = QPushButton("+ New Workflow")
        new_btn.setProperty("class", "ActionBtn")
        new_btn.clicked.connect(self._new_workflow)
        lv.addWidget(new_btn)

        root.addWidget(left)

        # ---- Right: visual step-builder for the selected/new workflow ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(32, 28, 32, 28)
        rv.setSpacing(16)

        header_row = QHBoxLayout()
        self.title_lbl = QLabel("New Workflow")
        self.title_lbl.setStyleSheet(f"color: {self._theme.text_main}; font-size: 18px; font-weight: 700;")
        header_row.addWidget(self.title_lbl)
        header_row.addStretch()
        rename_btn = QPushButton("Rename")
        rename_btn.setProperty("class", "ActionBtn")
        rename_btn.clicked.connect(self._rename_workflow)
        header_row.addWidget(rename_btn)
        rv.addLayout(header_row)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 18, 20, 18)
        pv.setSpacing(10)
        pv.addWidget(make_section_heading("STEPS (runs top to bottom)", self._theme))

        # Drag-to-reorder IS the "visual builder" part - each row is a
        # plain voice-command string (exactly what run_workflow replays
        # through WaveBrain.process_actions), so reordering rows here is
        # literally reordering execution order, live.
        self.steps_list = QListWidget()
        self.steps_list.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.steps_list.setStyleSheet(f"""
            QListWidget {{ background: rgba(0,0,0,0.15); border: 1px solid {self._theme.border_soft}; border-radius: 8px; padding: 4px; }}
            QListWidget::item {{ padding: 10px; color: {self._theme.text_main}; border-radius: 6px; margin: 2px; background: rgba(255,255,255,0.03); }}
            QListWidget::item:selected {{ background: {self._theme.accent_1}25; }}
        """)
        self.steps_list.itemDoubleClicked.connect(self._edit_step)
        self.steps_list.model().rowsMoved.connect(lambda *a: self._mark_dirty())
        pv.addWidget(self.steps_list, 1)

        step_row = QHBoxLayout()
        self.step_input = QLineEdit()
        self.step_input.setPlaceholderText("e.g. open chrome, or 'what's the weather'...")
        self.step_input.returnPressed.connect(self._add_step)
        step_row.addWidget(self.step_input, 1)
        add_step_btn = QPushButton("Add Step")
        add_step_btn.setProperty("class", "ActionBtn")
        add_step_btn.clicked.connect(self._add_step)
        step_row.addWidget(add_step_btn)
        pv.addLayout(step_row)

        step_btn_row = QHBoxLayout()
        up_btn = QPushButton("↑ Move Up")
        up_btn.clicked.connect(lambda: self._move_step(-1))
        down_btn = QPushButton("↓ Move Down")
        down_btn.clicked.connect(lambda: self._move_step(1))
        remove_btn = QPushButton("Remove Step")
        remove_btn.setProperty("class", "DangerPill")
        remove_btn.clicked.connect(self._remove_step)
        for b in (up_btn, down_btn, remove_btn):
            b.setProperty("class", "ActionBtn" if b is not remove_btn else "DangerPill")
            step_btn_row.addWidget(b)
        step_btn_row.addStretch()
        pv.addLayout(step_btn_row)

        rv.addWidget(panel, 1)

        footer_row = QHBoxLayout()
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        footer_row.addWidget(self.status_lbl)
        footer_row.addStretch()

        run_btn = QPushButton("▶ Run Now")
        run_btn.setProperty("class", "ActionBtn")
        run_btn.clicked.connect(self._run_workflow)
        footer_row.addWidget(run_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setProperty("class", "DangerPill")
        self.delete_btn.clicked.connect(self._delete_workflow)
        footer_row.addWidget(self.delete_btn)

        save_btn = QPushButton("Save Workflow")
        save_btn.setProperty("class", "ActionBtn")
        save_btn.clicked.connect(self._save_workflow)
        footer_row.addWidget(save_btn)

        rv.addLayout(footer_row)
        root.addWidget(right, 1)

        self._new_workflow()

    # ------------------------------------------------------------------
    def _reload_list(self):
        """Repopulates the left-hand list from the DB. list_workflows_full()
        (database/db_manager.py, added alongside this panel) returns steps
        too, so switching workflows needs no second round-trip."""
        self._workflows = {name: actions for name, actions in db.list_workflows_full()}
        self.name_list.blockSignals(True)
        self.name_list.clear()
        for name in self._workflows:
            self.name_list.addItem(name)
        self.name_list.blockSignals(False)

    def _on_select_workflow(self, name: str):
        if not name or name not in self._workflows:
            return
        self._current_name = name
        self._dirty = False
        self.title_lbl.setText(name.title())
        self.steps_list.clear()
        for step in self._workflows[name]:
            self.steps_list.addItem(step)
        self.delete_btn.setEnabled(True)
        self.status_lbl.setText(f"{len(self._workflows[name])} step(s) · saved")

    def _new_workflow(self):
        self._current_name = None
        self._dirty = False
        self.title_lbl.setText("New Workflow")
        self.steps_list.clear()
        self.name_list.clearSelection()
        self.delete_btn.setEnabled(False)
        self.status_lbl.setText("Not saved yet")

    def _mark_dirty(self):
        self._dirty = True
        suffix = " · unsaved changes" if self._current_name else " · not saved yet"
        self.status_lbl.setText(f"{self.steps_list.count()} step(s){suffix}")

    # ------------------------------------------------------------------
    def _add_step(self):
        text = self.step_input.text().strip()
        if not text:
            return
        self.steps_list.addItem(text)
        self.step_input.clear()
        self._mark_dirty()

    def _edit_step(self, item: QListWidgetItem):
        new_text, ok = QInputDialog.getText(self, "Edit Step", "Command:", text=item.text())
        if ok and new_text.strip():
            item.setText(new_text.strip())
            self._mark_dirty()

    def _remove_step(self):
        row = self.steps_list.currentRow()
        if row >= 0:
            self.steps_list.takeItem(row)
            self._mark_dirty()

    def _move_step(self, delta: int):
        row = self.steps_list.currentRow()
        new_row = row + delta
        if row < 0 or not (0 <= new_row < self.steps_list.count()):
            return
        item = self.steps_list.takeItem(row)
        self.steps_list.insertItem(new_row, item)
        self.steps_list.setCurrentRow(new_row)
        self._mark_dirty()

    # ------------------------------------------------------------------
    def _current_steps(self) -> list:
        return [self.steps_list.item(i).text() for i in range(self.steps_list.count())]

    def _save_workflow(self):
        steps = self._current_steps()
        if not steps:
            QMessageBox.warning(self, "No steps", "Add at least one step before saving.")
            return
        name = self._current_name
        if not name:
            name, ok = QInputDialog.getText(self, "Save Workflow", "Workflow name:")
            if not ok or not name.strip():
                return
            name = name.strip()
        if db.save_workflow(name, steps):
            self._current_name = name.strip().lower()
            self._dirty = False
            self.status_lbl.setText(f"{len(steps)} step(s) · saved")
            self._reload_list()
            # Re-select it in the list now that it exists there.
            for i in range(self.name_list.count()):
                if self.name_list.item(i).text() == self._current_name:
                    self.name_list.setCurrentRow(i)
                    break
            self._main.toast.show(f"Workflow '{self._current_name}' saved.", "success")
        else:
            QMessageBox.warning(self, "Couldn't save", "Something went wrong saving that workflow.")

    def _rename_workflow(self):
        if not self._current_name:
            QMessageBox.information(self, "Save first", "Save this workflow before renaming it.")
            return
        new_name, ok = QInputDialog.getText(self, "Rename Workflow", "New name:", text=self._current_name)
        if not ok or not new_name.strip():
            return
        if db.rename_workflow(self._current_name, new_name.strip()):
            self._current_name = new_name.strip().lower()
            self.title_lbl.setText(self._current_name.title())
            self._reload_list()
            self._main.toast.show("Workflow renamed.", "success")
        else:
            QMessageBox.warning(self, "Couldn't rename", "That name may already be taken.")

    def _delete_workflow(self):
        if not self._current_name:
            return
        reply = QMessageBox.question(
            self, "Delete workflow", f"Delete '{self._current_name}'? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes and db.delete_workflow(self._current_name):
            self._main.toast.show("Workflow deleted.", "info")
            self._reload_list()
            self._new_workflow()

    def _run_workflow(self):
        if self._dirty:
            QMessageBox.information(self, "Unsaved changes", "Save the workflow before running it.")
            return
        if not self._current_name:
            QMessageBox.information(self, "Nothing to run", "Save this workflow first.")
            return
        # Goes through _dispatch_tool (not the raw _skill_run_workflow
        # handler) so a run started from this page gets the same audit-log
        # row and confirm-gate treatment as one started by voice/chat -
        # source="ui" just keeps the audit trail honest about where it came
        # from. Submitted to the worker pool so the GUI thread never blocks
        # on it.
        self.status_lbl.setText("Running...")
        worker = self._main.worker_pool.submit(
            self._main.brain._dispatch_tool, "run_workflow", {"name": self._current_name}, "ui")
        worker.signals.result.connect(lambda result: self._on_run_finished(result))
        worker.signals.error.connect(lambda e: self._on_run_finished(f"Run failed: {e}"))

    def _on_run_finished(self, result):
        self.status_lbl.setText(f"{self.steps_list.count()} step(s) · saved")
        self._main.toast.show(str(result), "info", duration=5000)
        self._main.add_chat_bubble(str(result), is_user=False)
