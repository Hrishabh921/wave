"""
ui/calendar_panel.py — Tier 21 item: Calendar management page.

Calendar events (database/db_manager.py's calendar_events table, reached
through automation/calendar_manager.py's CalendarManager) were chat-only:
"create an event for..." / "what's on my calendar" / "cancel..." all
worked by voice, with no page to see the next couple of months at a
glance or remove one precisely.

CalendarPanel lists every event (including expanded recurring occurrences
- see get_calendar_events_between's own docstring) in a rolling window
from today, with a one-click Cancel using the Tier 21 id-precise
cancel_calendar_event_by_id, and an add form that reuses
CalendarManager.create_event() directly - the exact same dateparser-based
time parsing and recurrence handling voice commands already use, so this
page adds no second implementation of that logic.
"""
from ui.common import *  # noqa: F401,F403 - brings in `datetime` (the class, not the module - see below)
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from database.db_manager import db
from automation.calendar_manager import CalendarManager
# ui.common does `from datetime import datetime`, so after the wildcard
# import above, the name `datetime` is the *class*, not the module - this
# file needs timedelta too, so it's imported separately rather than
# re-importing the whole datetime module (which would shadow ui.common's
# `datetime` class binding with the module instead).
from datetime import timedelta

WINDOW_DAYS = 60


class CalendarPanel(QWidget):
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

        root.addWidget(make_section_heading(f"CALENDAR — NEXT {WINDOW_DAYS} DAYS", self._theme))

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
        pv.addWidget(make_section_heading("NEW EVENT", self._theme))

        title_row = QHBoxLayout()
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Title, e.g. 'Team standup'")
        title_row.addWidget(self.title_input, 1)
        self.location_input = QLineEdit()
        self.location_input.setPlaceholderText("Location (optional)")
        title_row.addWidget(self.location_input, 1)
        pv.addLayout(title_row)

        when_row = QHBoxLayout()
        self.when_input = QLineEdit()
        self.when_input.setPlaceholderText("When? e.g. 'tomorrow at 3pm', 'next Monday 10am'")
        self.when_input.returnPressed.connect(self._add_event)
        when_row.addWidget(self.when_input, 1)
        self.recurrence_combo = QComboBox()
        self.recurrence_combo.addItems(["none", "daily", "weekly"])
        when_row.addWidget(self.recurrence_combo)
        add_btn = QPushButton("Add")
        add_btn.setProperty("class", "ActionBtn")
        add_btn.clicked.connect(self._add_event)
        when_row.addWidget(add_btn)
        pv.addLayout(when_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        pv.addWidget(self.status_lbl)

        root.addWidget(panel)

    # ------------------------------------------------------------------
    def _reload_list(self):
        now = datetime.now()
        until = now + timedelta(days=WINDOW_DAYS)
        self._rows = db.get_calendar_events_between(now.isoformat(), until.isoformat())
        self.list_widget.clear()
        for event_id, title, start_at, end_at, location, notes, recurrence in self._rows:
            try:
                when = datetime.fromisoformat(start_at).strftime("%a %b %d, %I:%M %p")
            except Exception:
                when = start_at
            suffix = f" ({recurrence})" if recurrence else ""
            loc = f" @ {location}" if location else ""
            item = QListWidgetItem(f"{title} — {when}{loc}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, event_id)
            self.list_widget.addItem(item)
        if not self._rows:
            self.list_widget.addItem(f"Nothing on the calendar for the next {WINDOW_DAYS} days.")

    def _cancel_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        event_id = item.data(Qt.ItemDataRole.UserRole)
        if event_id is None:
            return
        if db.cancel_calendar_event_by_id(event_id):
            self._main.toast.show("Event cancelled.", "success")
            self._reload_list()
        else:
            QMessageBox.warning(self, "Couldn't cancel", "That event may already be gone.")

    def _add_event(self):
        title = self.title_input.text().strip()
        when_text = self.when_input.text().strip()
        if not title or not when_text:
            self.status_lbl.setText("Both a title and a time are needed.")
            return
        recurrence = self.recurrence_combo.currentText()
        # Reuses CalendarManager.create_event() directly - the exact same
        # dateparser-based parsing voice commands already use, so this page
        # has no second time-parsing implementation of its own.
        result = CalendarManager.create_event(
            title, when_text, None, self.location_input.text().strip(), recurrence)
        self.status_lbl.setText(result)
        if "couldn't" not in result.lower() and "need" not in result.lower():
            self.title_input.clear()
            self.location_input.clear()
            self.when_input.clear()
            self.recurrence_combo.setCurrentIndex(0)
            self._main.toast.show(result, "success")
            self._reload_list()
