"""
ui/memory_panel.py — Tier 21 item: Memory management page.

Memory (database/db_manager.py's memory table, save_memory/get_memory/
get_all_memory/get_memory_provenance/delete_memory) was chat-only: "remember
X" / "what do you remember about Y" / "forget Z" worked by voice, but there
was no way to SEE every fact Wave has stored, fix a wrong one, or clean out
stale ones without knowing the exact key to say out loud.

MemoryPanel is a real page for that: a list of remembered keys on the left
(each key already IS the natural precise identifier - no substring-match
ambiguity the way to-dos/notes/calendar events have, since save_memory's
own "ON CONFLICT(key)" semantics mean a key is unique by definition), and
on the right a simple editor for the selected key's value plus its Tier 20
provenance (source: explicit vs. auto_consolidated, and when it was last
updated) - the same data brain/handlers/system.py's why_remembered skill
reads, just visible instead of requiring a voice question.
"""
import json

from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from database.db_manager import db

# Tier 21 roadmap line for this page: "review, edit and delete, with an
# auto-vs-explicit badge and export." The badge is a short prefix on each
# list row (see _badge_for below); "explicit" also covers facts with no
# recorded provenance at all (saved before Tier 20 added the column) -
# defaulting an unknown origin to "you said this" rather than inventing a
# third badge for what is really just missing data.
_BADGE = {"explicit": "🔒", "auto_consolidated": "✨"}


class MemoryPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._current_key = None
        self._setup_ui()
        self._reload_list()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Left: remembered keys ----
        left = QFrame()
        left.setFixedWidth(280)
        left.setStyleSheet(f"background: {self._theme.bg_panel}; border-right: 1px solid {self._theme.border_soft};")
        lv = QVBoxLayout(left)
        lv.setContentsMargins(16, 20, 16, 20)
        lv.setSpacing(10)
        lv.addWidget(make_section_heading("REMEMBERED FACTS", self._theme))

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter...")
        self.search_input.textChanged.connect(self._reload_list)
        lv.addWidget(self.search_input)

        self.key_list = QListWidget()
        self.key_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; }}
            QListWidget::item {{ padding: 10px 12px; color: {self._theme.text_muted}; border-radius: 6px; margin-bottom: 2px; }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.05); color: {self._theme.text_main}; }}
            QListWidget::item:selected {{ background: {self._theme.accent_1}30; color: {self._theme.accent_1}; }}
        """)
        self.key_list.currentTextChanged.connect(self._on_select_key)
        lv.addWidget(self.key_list, 1)

        btn_row = QHBoxLayout()
        new_btn = QPushButton("+ New Fact")
        new_btn.setProperty("class", "ActionBtn")
        new_btn.clicked.connect(self._new_fact)
        btn_row.addWidget(new_btn)
        export_btn = QPushButton("Export")
        export_btn.setProperty("class", "ActionBtn")
        export_btn.clicked.connect(self._export_facts)
        btn_row.addWidget(export_btn)
        lv.addLayout(btn_row)

        legend = QLabel(f"{_BADGE['explicit']} you told me  ·  {_BADGE['auto_consolidated']} I picked up on my own")
        legend.setWordWrap(True)
        legend.setStyleSheet(f"color: {self._theme.text_faint}; font-size: 10.5px;")
        lv.addWidget(legend)

        root.addWidget(left)

        # ---- Right: editor for the selected (or new) fact ----
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(32, 28, 32, 28)
        rv.setSpacing(16)

        self.title_lbl = QLabel("New Fact")
        self.title_lbl.setStyleSheet(f"color: {self._theme.text_main}; font-size: 18px; font-weight: 700;")
        rv.addWidget(self.title_lbl)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 18, 20, 18)
        pv.setSpacing(10)

        pv.addWidget(make_section_heading("KEY", self._theme))
        self.key_input = QLineEdit()
        self.key_input.setPlaceholderText("e.g. favorite color")
        pv.addWidget(self.key_input)

        pv.addWidget(make_section_heading("VALUE", self._theme))
        self.value_input = QLineEdit()
        self.value_input.setPlaceholderText("e.g. blue")
        pv.addWidget(self.value_input)

        self.provenance_lbl = QLabel("")
        self.provenance_lbl.setStyleSheet(f"color: {self._theme.text_faint}; font-size: 11px;")
        self.provenance_lbl.setWordWrap(True)
        pv.addWidget(self.provenance_lbl)

        pv.addStretch()
        rv.addWidget(panel, 1)

        footer_row = QHBoxLayout()
        footer_row.addStretch()
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setProperty("class", "DangerPill")
        self.delete_btn.clicked.connect(self._delete_fact)
        footer_row.addWidget(self.delete_btn)
        save_btn = QPushButton("Save Fact")
        save_btn.setProperty("class", "ActionBtn")
        save_btn.clicked.connect(self._save_fact)
        footer_row.addWidget(save_btn)
        rv.addLayout(footer_row)

        root.addWidget(right, 1)
        self._new_fact()

    # ------------------------------------------------------------------
    def _badge_for(self, key: str) -> str:
        prov = db.get_memory_provenance(key) or {}
        return _BADGE.get(prov.get("source"), _BADGE["explicit"])

    def _row_label(self, key: str) -> str:
        return f"{self._badge_for(key)}\t{key}"

    def _reload_list(self, filter_text: str = None):
        """Repopulates the left-hand list from the DB, applying the search
        box's (case-insensitive, key-or-value) filter if any text is in it.
        Each row is prefixed with its provenance badge (🔒 explicit vs.
        ✨ auto-consolidated - see _BADGE above)."""
        filter_text = (filter_text if filter_text is not None else self.search_input.text()).strip().lower()
        self._facts = {key: value for key, value in db.get_all_memory()}
        self.key_list.blockSignals(True)
        self.key_list.clear()
        for key in sorted(self._facts):
            if not filter_text or filter_text in key.lower() or filter_text in self._facts[key].lower():
                self.key_list.addItem(self._row_label(key))
        self.key_list.blockSignals(False)

    def _on_select_key(self, label: str):
        # Strip the "<badge>\t" prefix _row_label() adds before looking the
        # key up - the list widget's currentTextChanged gives us the full
        # display string, not the bare key. A tab is the separator (not a
        # space) specifically so a key that happens to contain spaces can
        # never be mis-split.
        key = label.split("\t", 1)[1] if "\t" in label else label
        if not key or key not in self._facts:
            return
        self._current_key = key
        self.title_lbl.setText(key)
        self.key_input.setText(key)
        self.key_input.setEnabled(False)  # renaming a fact = delete + new; keeps save_memory's key semantics simple
        self.value_input.setText(self._facts[key])
        self.delete_btn.setEnabled(True)
        prov = db.get_memory_provenance(key)
        self.provenance_lbl.setText(self._format_provenance(prov))

    def _format_provenance(self, prov) -> str:
        if not prov:
            return ""
        parts = []
        if prov.get("source"):
            label = {"explicit": "you told me this directly",
                     "auto_consolidated": "I picked this up from conversation"}.get(prov["source"], prov["source"])
            parts.append(label)
        if prov.get("updated_at"):
            parts.append(f"last updated {prov['updated_at']}")
        return " · ".join(parts) if parts else "No provenance recorded for this fact (saved before Tier 20)."

    def _new_fact(self):
        self._current_key = None
        self.title_lbl.setText("New Fact")
        self.key_input.clear()
        self.key_input.setEnabled(True)
        self.value_input.clear()
        self.provenance_lbl.setText("")
        self.key_list.clearSelection()
        self.delete_btn.setEnabled(False)

    # ------------------------------------------------------------------
    def _save_fact(self):
        key = self.key_input.text().strip()
        value = self.value_input.text().strip()
        if not key or not value:
            QMessageBox.warning(self, "Missing info", "Both a key and a value are needed.")
            return
        if db.save_memory(key, value, source="explicit"):
            self._reload_list()
            for i in range(self.key_list.count()):
                row_key = self.key_list.item(i).text().split("\t", 1)[-1].strip().lower()
                if row_key == key.strip().lower():
                    self.key_list.setCurrentRow(i)
                    break
            self._main.toast.show(f"Remembered '{key}'.", "success")
        else:
            QMessageBox.warning(self, "Couldn't save", "Something went wrong saving that fact.")

    def _export_facts(self):
        """Tier 21 roadmap line for this page explicitly asks for export -
        writes every remembered fact plus its Tier 20 provenance to a JSON
        file the user picks, so they can back it up or inspect it outside
        the app. Never includes anything beyond what's already visible in
        this page (no secrets scanning needed here - that already happens
        before anything is logged elsewhere; this is a plain data export
        the user themselves asked for, of their own data)."""
        if not self._facts:
            QMessageBox.information(self, "Nothing to export", "There are no remembered facts yet.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export memory", "wave_memory_export.json", "JSON (*.json)")
        if not path:
            return
        rows = []
        for key, value in sorted(self._facts.items()):
            prov = db.get_memory_provenance(key) or {}
            rows.append({"key": key, "value": value,
                         "source": prov.get("source"), "updated_at": prov.get("updated_at")})
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2, ensure_ascii=False)
            self._main.toast.show(f"Exported {len(rows)} fact(s) to {path}", "success")
        except OSError as e:
            QMessageBox.warning(self, "Couldn't export", f"Something went wrong writing that file: {e}")

    def _delete_fact(self):
        if not self._current_key:
            return
        reply = QMessageBox.question(
            self, "Forget this fact", f"Forget '{self._current_key}'? This can't be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        if db.delete_memory(self._current_key):
            self._main.toast.show(f"Forgot '{self._current_key}'.", "success")
            self._reload_list()
            self._new_fact()
        else:
            QMessageBox.warning(self, "Couldn't delete", "Something went wrong forgetting that fact.")
