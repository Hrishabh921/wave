"""
ui/skills_panel.py — Tier 21 item: Skills management page.

Wave's tool-calling "skills" (brain/skills_registry.py's SkillRegistry -
built-in handlers in brain/handlers/*.py, plus drop-in files in
settings.config.USER_SKILLS_DIR hot-loaded by brain/skill_loader.py) had no
page of their own: "what can you do" / "list my skills" worked by voice,
and a user_skills/ drop-in's load/reload/error events only ever reached a
toast (ui/main_window.py's _on_skill_event) - nothing stuck around to look
at afterward.

SkillsPanel is a read-only catalog (skills_registry.describe() - name,
source, description - the exact same data `list my skills` reads from) and,
below it, the live status of user_skills/'s hot-reload loader
(brain/skill_loader.py's status(): which files loaded successfully, which
failed and why), with a manual "Rescan Now" button for anyone who'd rather
not wait for the poll interval. It does not let you write or edit a skill
file from here - these are plain Python files with "the same rights as
Wave itself" (skill_loader.py's own trust-model docstring), and a page
that writes arbitrary Python to disk is a much bigger trust surface than a
read-only catalog + status view; the existing drop-in-a-.py-file workflow
stays the way to add one.

Tier 21 roadmap line for this page also asks for "user skills,
enable/disable, hot-reload status" - the catalog+status view above covered
hot-reload status from the start, but enable/disable was missing until now.
It's deliberately scoped to user_skills/ drop-ins only (the roadmap's own
wording), not the built-in catalog above: a built-in skill is part of the
app itself, nothing here disables Wave's own features, and "disabling" a
drop-in just means "don't load this file's tools right now" - the file
itself is left untouched on disk (brain/skill_loader.py's set_disabled()
remembers the choice in a small sidecar file next to the skills).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from brain.skills_registry import skills as skill_registry
from brain.skill_loader import get_loader


class SkillsPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._setup_ui()
        self._reload_all()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        header_row = QHBoxLayout()
        header_row.addWidget(make_section_heading("SKILLS", self._theme))
        header_row.addStretch()
        rescan_btn = QPushButton("Rescan user_skills/")
        rescan_btn.setProperty("class", "ActionBtn")
        rescan_btn.clicked.connect(self._rescan)
        header_row.addWidget(rescan_btn)
        root.addLayout(header_row)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter by name or description...")
        self.search_input.textChanged.connect(self._reload_catalog)
        root.addWidget(self.search_input)

        self.catalog_list = QListWidget()
        self.catalog_list.setStyleSheet(f"""
            QListWidget {{ background: rgba(0,0,0,0.15); border: 1px solid {self._theme.border_soft}; border-radius: 8px; padding: 4px; }}
            QListWidget::item {{ padding: 8px 10px; color: {self._theme.text_main}; border-radius: 6px; margin: 1px; background: rgba(255,255,255,0.03); }}
        """)
        root.addWidget(self.catalog_list, 2)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 18, 20, 18)
        pv.setSpacing(8)
        pv.addWidget(make_section_heading("USER_SKILLS/ HOT-RELOAD STATUS", self._theme))
        self.status_lbl = QLabel("")
        self.status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
        self.status_lbl.setWordWrap(True)
        pv.addWidget(self.status_lbl)

        pv.addWidget(make_section_heading("DROP-IN FILES — ENABLE / DISABLE", self._theme))
        self.files_list = QListWidget()
        self.files_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; }}
            QListWidget::item {{ padding: 6px 8px; color: {self._theme.text_muted}; border-radius: 6px; }}
            QListWidget::item:hover {{ background: rgba(255,255,255,0.05); color: {self._theme.text_main}; }}
            QListWidget::item:selected {{ background: {self._theme.accent_1}30; color: {self._theme.accent_1}; }}
        """)
        self.files_list.currentTextChanged.connect(self._on_file_selected)
        pv.addWidget(self.files_list)

        toggle_row = QHBoxLayout()
        toggle_row.addStretch()
        self.toggle_btn = QPushButton("Disable")
        self.toggle_btn.setProperty("class", "DangerPill")
        self.toggle_btn.setEnabled(False)
        self.toggle_btn.clicked.connect(self._toggle_selected_file)
        toggle_row.addWidget(self.toggle_btn)
        pv.addLayout(toggle_row)

        root.addWidget(panel, 1)

    # ------------------------------------------------------------------
    def _reload_all(self):
        self._reload_catalog()
        self._reload_status()
        self._reload_files()

    def _reload_catalog(self, filter_text: str = None):
        filter_text = (filter_text if filter_text is not None else self.search_input.text()).strip().lower()
        self.catalog_list.clear()
        entries = sorted(skill_registry.describe(), key=lambda e: e["name"])
        shown = 0
        for entry in entries:
            if filter_text and filter_text not in entry["name"].lower() \
                    and filter_text not in entry["description"].lower():
                continue
            self.catalog_list.addItem(f"{entry['name']}  ·  {entry['source']}  —  {entry['description']}")
            shown += 1
        if shown == 0:
            self.catalog_list.addItem("No skills match that filter." if filter_text else "No skills registered.")

    def _reload_status(self):
        try:
            status = get_loader().status()
        except Exception as e:
            self.status_lbl.setText(f"Couldn't read user_skills/ status: {e}")
            return
        lines = [f"Directory: {status.get('directory', '?')}",
                 f"Watching: {'yes' if status.get('watching') else 'no'}"]
        loaded = status.get("loaded") or {}
        errors = status.get("errors") or {}
        disabled = status.get("disabled") or []
        if loaded:
            for filename, names in loaded.items():
                lines.append(f"✓ {filename} → {', '.join(names) if names else '(no skills)'}")
        if errors:
            for filename, detail in errors.items():
                lines.append(f"✗ {filename}: {detail}")
        if disabled:
            for filename in disabled:
                lines.append(f"⏸ {filename}: disabled")
        if not loaded and not errors and not disabled:
            lines.append("No drop-in skill files found yet.")
        self.status_lbl.setText("\n".join(lines))

    def _file_row_label(self, filename: str, status: dict) -> str:
        if filename in (status.get("disabled") or []):
            icon = "⏸"
        elif filename in (status.get("errors") or {}):
            icon = "✗"
        else:
            icon = "✓"
        # Tab, not space, as the separator - same reasoning as
        # ui/memory_panel.py's _row_label: a filename containing a space
        # (unusual, but not impossible) must never be mis-split.
        return f"{icon}\t{filename}"

    @staticmethod
    def _filename_from_row(label: str) -> str:
        return label.split("\t", 1)[1] if "\t" in label else label

    def _reload_files(self):
        """Every drop-in file the loader currently knows about (loaded,
        errored, or disabled), so a disabled file never just vanishes from
        the list - it stays here, clearly marked, ready to re-enable."""
        try:
            status = get_loader().status()
        except Exception:
            self.files_list.clear()
            return
        names = set(status.get("loaded") or {}) | set(status.get("errors") or {}) | set(status.get("disabled") or [])
        self.files_list.blockSignals(True)
        self.files_list.clear()
        for name in sorted(names):
            self.files_list.addItem(self._file_row_label(name, status))
        self.files_list.blockSignals(False)
        self._on_file_selected(self.files_list.currentItem().text() if self.files_list.currentItem() else "")

    def _on_file_selected(self, label: str):
        filename = self._filename_from_row(label) if label else ""
        if not filename:
            self.toggle_btn.setEnabled(False)
            self.toggle_btn.setText("Disable")
            return
        self.toggle_btn.setEnabled(True)
        if get_loader().is_disabled(filename):
            self.toggle_btn.setText("Enable")
            self.toggle_btn.setProperty("class", "ActionBtn")
        else:
            self.toggle_btn.setText("Disable")
            self.toggle_btn.setProperty("class", "DangerPill")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

    def _toggle_selected_file(self):
        item = self.files_list.currentItem()
        if item is None:
            return
        filename = self._filename_from_row(item.text())
        if not filename:
            return
        currently_disabled = get_loader().is_disabled(filename)
        try:
            get_loader().set_disabled(filename, not currently_disabled)
        except Exception as e:
            QMessageBox.warning(self, "Couldn't update skill", str(e))
            return
        self._reload_all()
        verb = "Enabled" if currently_disabled else "Disabled"
        self._main.toast.show(f"{verb} {filename}.", "success")

    def _rescan(self):
        try:
            events = get_loader().load_now()
            self._reload_all()
            self._main.toast.show(
                f"Rescanned user_skills/ — {len(events)} change(s)." if events else "Rescanned user_skills/ — no changes.",
                "success")
        except Exception as e:
            QMessageBox.warning(self, "Rescan failed", str(e))
