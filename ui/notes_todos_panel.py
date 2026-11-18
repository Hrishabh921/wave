"""
ui/notes_todos_panel.py — Tier 21 item: Notes & To-dos management page.

Quick notes (database/db_manager.py's quick_notes table) and to-dos (its
todos table) were both chat-only, with no page to see everything at a
glance, check off or delete one precisely, or add one outside a voice/chat
turn. This single page covers both (they're small, closely related
scratch-lists, not worth two separate nav entries), using the Tier 21
id-precise methods (delete_todo_by_id/complete_todo_by_id/
delete_quick_note_by_id) added to database/db_manager.py specifically for
pages like this one - a click on one row must affect exactly that row, not
every row whose text happens to share a substring.
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from database.db_manager import db


class NotesTodosPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._setup_ui()
        self._reload_all()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(24)

        # ---- To-dos column ----
        todo_col = QFrame()
        todo_col.setProperty("class", "Panel")
        add_shadow(todo_col)
        tv = QVBoxLayout(todo_col)
        tv.setContentsMargins(20, 18, 20, 18)
        tv.setSpacing(10)
        tv.addWidget(make_section_heading("TO-DOS", self._theme))

        self.todo_list = QListWidget()
        self.todo_list.itemDoubleClicked.connect(self._complete_todo)
        tv.addWidget(self.todo_list, 1)

        todo_row = QHBoxLayout()
        self.todo_input = QLineEdit()
        self.todo_input.setPlaceholderText("New to-do... (double-click an item to complete it)")
        self.todo_input.returnPressed.connect(self._add_todo)
        todo_row.addWidget(self.todo_input, 1)
        add_todo_btn = QPushButton("Add")
        add_todo_btn.setProperty("class", "ActionBtn")
        add_todo_btn.clicked.connect(self._add_todo)
        todo_row.addWidget(add_todo_btn)
        tv.addLayout(todo_row)

        todo_btn_row = QHBoxLayout()
        complete_btn = QPushButton("Complete Selected")
        complete_btn.setProperty("class", "ActionBtn")
        complete_btn.clicked.connect(self._complete_selected_todo)
        todo_btn_row.addWidget(complete_btn)
        delete_todo_btn = QPushButton("Delete Selected")
        delete_todo_btn.setProperty("class", "DangerPill")
        delete_todo_btn.clicked.connect(self._delete_selected_todo)
        todo_btn_row.addWidget(delete_todo_btn)
        tv.addLayout(todo_btn_row)

        root.addWidget(todo_col, 1)

        # ---- Quick notes column ----
        notes_col = QFrame()
        notes_col.setProperty("class", "Panel")
        add_shadow(notes_col)
        nv = QVBoxLayout(notes_col)
        nv.setContentsMargins(20, 18, 20, 18)
        nv.setSpacing(10)
        nv.addWidget(make_section_heading("QUICK NOTES", self._theme))

        self.notes_list = QListWidget()
        nv.addWidget(self.notes_list, 1)

        notes_row = QHBoxLayout()
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("New quick note...")
        self.note_input.returnPressed.connect(self._add_note)
        notes_row.addWidget(self.note_input, 1)
        add_note_btn = QPushButton("Add")
        add_note_btn.setProperty("class", "ActionBtn")
        add_note_btn.clicked.connect(self._add_note)
        notes_row.addWidget(add_note_btn)
        nv.addLayout(notes_row)

        delete_note_btn = QPushButton("Delete Selected")
        delete_note_btn.setProperty("class", "DangerPill")
        delete_note_btn.clicked.connect(self._delete_selected_note)
        nv.addWidget(delete_note_btn)

        root.addWidget(notes_col, 1)

    # ------------------------------------------------------------------
    def _reload_all(self):
        self._reload_todos()
        self._reload_notes()

    def _reload_todos(self):
        self._todo_rows = db.list_todos(include_completed=False)
        self.todo_list.clear()
        for todo_id, text, due_text, completed, created_at in self._todo_rows:
            label = text + (f"  (due {due_text})" if due_text else "")
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, todo_id)
            self.todo_list.addItem(item)
        if not self._todo_rows:
            self.todo_list.addItem("Nothing pending - nice work.")

    def _reload_notes(self):
        self._note_rows = db.list_quick_notes()
        self.notes_list.clear()
        for note_id, content, created_at in self._note_rows:
            item = QListWidgetItem(content)
            item.setData(Qt.ItemDataRole.UserRole, note_id)
            self.notes_list.addItem(item)
        if not self._note_rows:
            self.notes_list.addItem("No quick notes yet.")

    # ---- To-dos ----
    def _add_todo(self):
        text = self.todo_input.text().strip()
        if not text:
            return
        if db.add_todo(text) is not None:
            self.todo_input.clear()
            self._reload_todos()
        else:
            QMessageBox.warning(self, "Couldn't add", "Something went wrong adding that to-do.")

    def _selected_todo_id(self):
        item = self.todo_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _complete_todo(self, item: QListWidgetItem):
        todo_id = item.data(Qt.ItemDataRole.UserRole)
        if todo_id is None:
            return
        if db.complete_todo_by_id(todo_id):
            self._main.toast.show("To-do completed.", "success")
            self._reload_todos()

    def _complete_selected_todo(self):
        todo_id = self._selected_todo_id()
        if todo_id is None:
            return
        if db.complete_todo_by_id(todo_id):
            self._main.toast.show("To-do completed.", "success")
            self._reload_todos()

    def _delete_selected_todo(self):
        todo_id = self._selected_todo_id()
        if todo_id is None:
            return
        if db.delete_todo_by_id(todo_id):
            self._main.toast.show("To-do deleted.", "success")
            self._reload_todos()

    # ---- Quick notes ----
    def _add_note(self):
        text = self.note_input.text().strip()
        if not text:
            return
        if db.add_quick_note(text):
            self.note_input.clear()
            self._reload_notes()
        else:
            QMessageBox.warning(self, "Couldn't add", "Something went wrong adding that note.")

    def _delete_selected_note(self):
        item = self.notes_list.currentItem()
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        if note_id is None:
            return
        if db.delete_quick_note_by_id(note_id):
            self._main.toast.show("Note deleted.", "success")
            self._reload_notes()
