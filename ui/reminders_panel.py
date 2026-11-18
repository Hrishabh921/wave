"""
ui/reminders_panel.py — Tier 21 item: Reminders & Timers management page.

Reminders/alarms/timers (database/db_manager.py's reminders table,
save_reminder/get_upcoming_reminders/cancel_reminder_by_id) were chat-only:
"remind me to..." / "set an alarm for..." worked by voice, with no page to
see every pending one at a glance, no way to add one without talking to
Wave, and no quick single-click cancel.

RemindersPanel is a real page for that: every untriggered reminder/alarm/
timer in one list (they're all just rows in the same `reminders` table -
there's no separate "timer" table, see brain/handlers/timers.py), each
with a one-click Cancel using cancel_reminder_by_id (the Tier 5 precise-id
method - a text-substring cancel would risk catching the wrong row if two
reminders share wording), and a simple add form that reuses
WaveBrain._set_reminder() directly - the exact same dateparser-based
natural-language time parsing voice commands already use ("in 10 minutes",
"tomorrow at 5pm", ...), so this page needs no second time-parsing
implementation of its own.
"""
from ui.common import *  # noqa: F401,F403 - brings in `datetime` (the class - ui.common does `from datetime import datetime`)
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from database.db_manager import db


class RemindersPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._setup_ui()
        self._reload_list()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        root.addWidget(make_section_heading("REMINDERS & TIMERS", self._theme))

        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet(f"""
            QListWidget {{ background: rgba(0,0,0,0.15); border: 1px solid {self._theme.border_soft}; border-radius: 8px; padding: 4px; }}
            QListWidget::item {{ padding: 10px; color: {self._theme.text_main}; border-radius: 6px; margin: 2px; background: rgba(255,255,255,0.03); }}
        """)
        root.addWidget(self.list_widget, 1)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch()
        cancel_btn = QPushButton("Cancel Selected")
        cancel_btn.setProperty("class", "DangerPill")
        cancel_btn.clicked.connect(self._cancel_selected)
        cancel_row.addWidget(cancel_btn)
        root.addLayout(cancel_row)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 18, 20, 18)
        pv.setSpacing(10)
        pv.addWidget(make_section_heading("NEW REMINDER", self._theme))

        form_row = QHBoxLayout()
        self.message_input = QLineEdit()
        self.message_input.setPlaceholderText("What should I remind you about? e.g. 'check the oven'")
        form_row.addWidget(self.message_input, 1)
        pv.addLayout(form_row)

        when_row = QHBoxLayout()
        self.when_input = QLineEdit()
        self.when_input.setPlaceholderText("When? e.g. 'in 10 minutes', 'tomorrow at 5pm', '7am'")
        self.when_input.returnPressed.connect(self._add_reminder)
        when_row.addWidget(self.when_input, 1)
        self.daily_check = QCheckBox("Repeat daily")
        when_row.addWidget(self.daily_check)
        add_btn = QPushButton("Add")
        add_btn.setProperty("class", "ActionBtn")
        add_btn.clicked.connect(self._add_reminder)
        when_row.addWidget(add_btn)
        pv.addLayout(when_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        pv.addWidget(self.status_lbl)

        root.addWidget(panel)

    # ------------------------------------------------------------------
    def _reload_list(self):
        self._rows = db.get_upcoming_reminders()  # (id, message, due_at, recurrence)
        self.list_widget.clear()
        for reminder_id, message, due_at, recurrence in self._rows:
            try:
                when = datetime.fromisoformat(due_at).strftime("%I:%M %p on %b %d")
            except Exception:
                when = due_at
            suffix = " (repeats daily)" if recurrence == "daily" else ""
            item = QListWidgetItem(f"{message} — {when}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, reminder_id)
            self.list_widget.addItem(item)
        if not self._rows:
            self.list_widget.addItem("No upcoming reminders or timers.")

    def _cancel_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        reminder_id = item.data(Qt.ItemDataRole.UserRole)
        if reminder_id is None:
            return
        if db.cancel_reminder_by_id(reminder_id):
            self._main.toast.show("Reminder cancelled.", "success")
            self._reload_list()
        else:
            QMessageBox.warning(self, "Couldn't cancel", "That reminder may have already fired.")

    def _add_reminder(self):
        message = self.message_input.text().strip() or "Reminder"
        when_text = self.when_input.text().strip()
        if not when_text:
            self.status_lbl.setText("Enter a time first.")
            return
        recurrence = "daily" if self.daily_check.isChecked() else "none"
        # Reuses the exact same natural-language parsing voice commands use
        # (brain/groq_core.py's _set_reminder) rather than a second
        # implementation here - see this file's module docstring.
        result = self._main.brain._set_reminder(message, when_text, recurrence)
        self.status_lbl.setText(result)
        if "couldn't" not in result.lower() and "need" not in result.lower() and "already passed" not in result.lower():
            self.message_input.clear()
            self.when_input.clear()
            self.daily_check.setChecked(False)
            self._main.toast.show(result, "success")
            self._reload_list()
