"""
ui/performance_panel.py — Tier 21 item: Performance page.

Time-to-first-audio and related latencies (telemetry/ttfa.py logs every
voice turn to <data dir>/database/ttfa.jsonl; telemetry/ttfa_report.py
summarizes it into per-path p50/p90 against Tier 11's own targets) were
command-line-only: `python -m telemetry.ttfa_report` from a terminal, with
no in-app page to glance at.

PerformancePanel reuses telemetry/ttfa_report.py's own load_rows/summarize/
by_language functions directly - no second parser or aggregator for the
same log file - and renders the same numbers that script already prints,
as a simple table plus a per-language breakdown, with a Refresh button
(this log only grows while voice turns happen, so there's no live-tailing
need - a manual refresh is the honest affordance, not a polling timer
running forever in the background for a page most people check rarely).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading
from telemetry.ttfa_report import load_rows, summarize, by_language, _fmt
from telemetry import usage_stats


class PerformancePanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._setup_ui()
        self._reload()

    # ------------------------------------------------------------------
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.setSpacing(16)

        header_row = QHBoxLayout()
        header_row.addWidget(make_section_heading("PERFORMANCE — TIME TO FIRST AUDIO", self._theme))
        header_row.addStretch()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setProperty("class", "ActionBtn")
        refresh_btn.clicked.connect(self._reload)
        header_row.addWidget(refresh_btn)
        root.addLayout(header_row)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(20, 18, 20, 18)
        pv.setSpacing(10)
        pv.addWidget(make_section_heading("BY PATH (fast path / planner / LLM / ...)", self._theme))

        self.path_table = QTableWidget(0, 6)
        self.path_table.setHorizontalHeaderLabels(["Path", "Turns", "p50", "p90", "p50 1st text", "Target met?"])
        self.path_table.horizontalHeader().setStretchLastSection(True)
        self.path_table.verticalHeader().setVisible(False)
        self.path_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        pv.addWidget(self.path_table, 1)

        root.addWidget(panel, 2)

        lang_panel = QFrame()
        lang_panel.setProperty("class", "Panel")
        add_shadow(lang_panel)
        lv = QVBoxLayout(lang_panel)
        lv.setContentsMargins(20, 18, 20, 18)
        lv.setSpacing(10)
        lv.addWidget(make_section_heading("BY LANGUAGE", self._theme))
        self.lang_table = QTableWidget(0, 3)
        self.lang_table.setHorizontalHeaderLabels(["Language", "Turns", "p50"])
        self.lang_table.horizontalHeader().setStretchLastSection(True)
        self.lang_table.verticalHeader().setVisible(False)
        self.lang_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        lv.addWidget(self.lang_table, 1)
        root.addWidget(lang_panel, 1)

        self.empty_lbl = QLabel("")
        self.empty_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
        self.empty_lbl.setWordWrap(True)
        root.addWidget(self.empty_lbl)

        # ── Tier 21: "Local-only usage stats, opt-in" ──────────────────
        # Off by default (Settings > Privacy > "Show my local usage
        # stats"); summarize() returns {"enabled": False} without even
        # touching the DB when it's off, so this section just shows how
        # to turn it on rather than an empty table.
        usage_panel = QFrame()
        usage_panel.setProperty("class", "Panel")
        add_shadow(usage_panel)
        uv = QVBoxLayout(usage_panel)
        uv.setContentsMargins(20, 18, 20, 18)
        uv.setSpacing(10)
        uv.addWidget(make_section_heading("USAGE STATS (opt-in, never leaves this device)", self._theme))
        self.usage_lbl = QLabel("")
        self.usage_lbl.setWordWrap(True)
        self.usage_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
        uv.addWidget(self.usage_lbl)
        root.addWidget(usage_panel)

    # ------------------------------------------------------------------
    def _reload(self):
        try:
            rows = load_rows()
        except Exception as e:
            self.empty_lbl.setText(f"Couldn't read the TTFA log: {e}")
            rows = []

        summary = summarize(rows) if rows else []
        self.path_table.setRowCount(len(summary))
        for i, s in enumerate(summary):
            met = ""
            if s["target_ms"] is not None and s["p50_ms"] is not None:
                met = "MET" if s["p50_ms"] <= s["target_ms"] else "NOT MET"
            values = [s["path"], str(s["n"]), _fmt(s["p50_ms"]), _fmt(s["p90_ms"]),
                      _fmt(s["p50_first_token_ms"]), met]
            for col, value in enumerate(values):
                self.path_table.setItem(i, col, QTableWidgetItem(value))

        langs = by_language(rows) if rows else []
        self.lang_table.setRowCount(len(langs))
        for i, l in enumerate(langs):
            for col, value in enumerate([l["language"], str(l["n"]), _fmt(l["p50_ms"])]):
                self.lang_table.setItem(i, col, QTableWidgetItem(value))

        if not rows:
            self.empty_lbl.setText(
                "No voice turns logged yet. Talk to Wave with the mic on (TTFA_ENABLED=true in settings), "
                "then come back and Refresh.")
        else:
            self.empty_lbl.setText(
                "TTFA = time from the end of your speech to Wave's first audio. "
                "Fast-path target: p50 ≤ 0.8s. LLM answers: p50 ≤ 2.0s.")

        self._reload_usage_stats()

    def _reload_usage_stats(self):
        try:
            stats = usage_stats.summarize()
        except Exception as e:
            self.usage_lbl.setText(f"Couldn't read usage stats: {e}")
            return
        if not stats.get("enabled"):
            self.usage_lbl.setText(
                "Off by default. Turn on \"Show my local usage stats\" in Settings > Privacy "
                "to see how you've been using Wave - voice vs. text, your most-used skills, "
                "success rate. It's computed from data already on this device and is never sent anywhere.")
            return
        if not stats.get("total_turns"):
            self.usage_lbl.setText(f"No activity logged in the last {stats.get('days', 7)} day(s) yet.")
            return
        by_source = ", ".join(f"{k}: {v}" for k, v in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]))
        top_skills = ", ".join(f"{name} ({n})" for name, n in stats["top_skills"][:5]) or "none yet"
        rate = stats["success_rate"]
        rate_text = f"{rate * 100:.0f}%" if rate is not None else "n/a"
        self.usage_lbl.setText(
            f"Last {stats['days']} day(s): {stats['total_turns']} turns ({by_source}). "
            f"Success rate: {rate_text}. Top skills: {top_skills}.")
