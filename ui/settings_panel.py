"""
ui/settings_panel.py
=============================================================================
SettingsPanel: the full Settings page (theme picker, voice, hotkeys,
integrations, etc).
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow, make_section_heading, make_divider
from voice.tts import CLONE_VOICE_ID
from voice.wakeword import get_wake_gate, reset_wake_gate


class SettingsPanel(QWidget):
    def __init__(self, main_window, theme: ThemeColors):
        super().__init__()
        self._main = main_window
        self._theme = theme
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Sidebar categories
        self.cat_list = QListWidget()
        self.cat_list.setFixedWidth(200)
        self.cat_list.setStyleSheet(f"""
            QListWidget {{ background: {self._theme.bg_panel}; border: none; border-right: 1px solid {self._theme.border_soft}; }}
            QListWidget::item {{ padding: 14px 16px; color: {self._theme.text_muted}; font-weight: 600; border-radius: 0; }}
            QListWidget::item:hover {{ background: rgba(40,40,40,1); color: {self._theme.text_main}; }}
            QListWidget::item:selected {{ background: {self._theme.accent_1}30; color: {self._theme.accent_1}; border-left: 3px solid {self._theme.accent_1}; }}
        """)
        categories = ["General", "Appearance", "Voice", "Avatar", "Hotkeys", "Privacy", "Security", "Remote", "Advanced", "About"]
        for cat in categories:
            self.cat_list.addItem(cat)
        self.cat_list.setCurrentRow(0)
        self.cat_list.currentRowChanged.connect(self._switch_page)
        layout.addWidget(self.cat_list)
        
        # Pages stack
        self.pages = QStackedWidget()
        self.pages.setStyleSheet("background: transparent;")
        
        self.pages.addWidget(self._build_general_page())
        self.pages.addWidget(self._build_appearance_page())
        self.pages.addWidget(self._build_voice_page())
        self.pages.addWidget(self._build_avatar_page())
        self.pages.addWidget(self._build_hotkeys_page())
        self.pages.addWidget(self._build_privacy_page())
        self.pages.addWidget(self._build_security_page())
        self.pages.addWidget(self._build_remote_page())
        self.pages.addWidget(self._build_advanced_page())
        self.pages.addWidget(self._build_about_page())
        
        layout.addWidget(self.pages, 1)
    
    def _switch_page(self, index: int):
        self.pages.setCurrentIndex(index)
    
    def _build_general_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("GENERAL", self._theme))
        
        # Startup
        startup_group = QFrame()
        startup_group.setProperty("class", "Panel")
        add_shadow(startup_group)
        sg = QVBoxLayout(startup_group)
        sg.setContentsMargins(24, 20, 24, 20)
        sg.setSpacing(12)
        
        self.cb_autostart = QCheckBox("Start WAVE on system boot")
        self.cb_minimize_tray = QCheckBox("Minimize to tray on close")
        self.cb_start_minimized = QCheckBox("Start minimized")
        for cb in [self.cb_autostart, self.cb_minimize_tray, self.cb_start_minimized]:
            cb.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
            sg.addWidget(cb)
        
        v.addWidget(startup_group)
        
        # Quick Notes (moved from Home)
        notes_panel = QFrame()
        notes_panel.setProperty("class", "Panel")
        add_shadow(notes_panel)
        np = QVBoxLayout(notes_panel)
        np.setContentsMargins(24, 20, 24, 20)
        np.addWidget(make_section_heading("QUICK NOTES", self._theme))
        np.addSpacing(8)
        self.sticky_note = QTextEdit()
        self.sticky_note.setObjectName("StickyNote")
        self.sticky_note.setPlaceholderText("Write your daily goals here...")
        self.sticky_note.setMinimumHeight(120)
        np.addWidget(self.sticky_note)
        v.addWidget(notes_panel)
        
        # Environment
        env_panel = QFrame()
        env_panel.setProperty("class", "Panel")
        add_shadow(env_panel)
        ep = QVBoxLayout(env_panel)
        ep.setContentsMargins(24, 20, 24, 20)
        ep.addWidget(make_section_heading("ENVIRONMENT", self._theme))
        ep.addSpacing(8)
        self.weather_lbl = QLabel("Fetching...")
        self.weather_lbl.setProperty("class", "InfoData")
        ep.addWidget(self.weather_lbl)
        v.addWidget(env_panel)
        
        v.addStretch()
        return page
    
    def _build_appearance_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("APPEARANCE", self._theme))
        
        # Theme selector
        theme_panel = QFrame()
        theme_panel.setProperty("class", "Panel")
        add_shadow(theme_panel)
        tp = QVBoxLayout(theme_panel)
        tp.setContentsMargins(24, 20, 24, 20)
        tp.setSpacing(14)
        
        tp.addWidget(QLabel("Theme:"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(self._main.theme_manager.get_theme_names())
        self.theme_combo.setCurrentText(self._theme.name)
        self.theme_combo.currentTextChanged.connect(self._on_theme_change)
        tp.addWidget(self.theme_combo)
        
        # Accent color picker
        tp.addWidget(make_divider(self._theme))
        tp.addWidget(QLabel("Custom Accent Colors:"))
        
        accent_row = QHBoxLayout()
        accent_row.setSpacing(12)
        self.accent1_btn = QPushButton("Primary")
        self.accent1_btn.setFixedHeight(40)
        self.accent1_btn.setStyleSheet(f"background: {self._theme.accent_1}; color: #000; border-radius: 8px; font-weight: 700;")
        self.accent1_btn.clicked.connect(lambda: self._pick_accent(1))
        
        self.accent2_btn = QPushButton("Secondary")
        self.accent2_btn.setFixedHeight(40)
        self.accent2_btn.setStyleSheet(f"background: {self._theme.accent_2}; color: #000; border-radius: 8px; font-weight: 700;")
        self.accent2_btn.clicked.connect(lambda: self._pick_accent(2))
        
        self.accent3_btn = QPushButton("Tertiary")
        self.accent3_btn.setFixedHeight(40)
        self.accent3_btn.setStyleSheet(f"background: {self._theme.accent_3}; color: #000; border-radius: 8px; font-weight: 700;")
        self.accent3_btn.clicked.connect(lambda: self._pick_accent(3))
        
        accent_row.addWidget(self.accent1_btn)
        accent_row.addWidget(self.accent2_btn)
        accent_row.addWidget(self.accent3_btn)
        tp.addLayout(accent_row)
        
        # Blur intensity
        tp.addWidget(make_divider(self._theme))
        tp.addWidget(QLabel("Backdrop Blur:"))
        self.blur_slider = QSlider(Qt.Orientation.Horizontal)
        self.blur_slider.setRange(0, 50)
        self.blur_slider.setValue(int(self._main.backdrop._blur_radius))
        self.blur_slider.valueChanged.connect(self._on_blur_change)
        tp.addWidget(self.blur_slider)
        
        # Panel opacity
        tp.addWidget(QLabel("Panel Opacity:"))
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(50, 100)
        self.opacity_slider.setValue(int(self._theme.page_bg_alpha * 100))
        self.opacity_slider.valueChanged.connect(self._on_opacity_change)
        tp.addWidget(self.opacity_slider)
        
        # Save as custom theme
        save_btn = QPushButton("Save as Custom Theme")
        save_btn.setProperty("class", "ActionBtn")
        save_btn.clicked.connect(self._save_custom_theme)
        tp.addWidget(save_btn)
        
        v.addWidget(theme_panel)
        v.addStretch()
        return page
    
    def _on_theme_change(self, name: str):
        self._main.theme_manager.set_theme(name)
        self._main.apply_theme(name)
        # Update this panel's theme reference
        self._theme = self._main.theme_manager.get_current()
        self._refresh_appearance_controls()
    
    def _refresh_appearance_controls(self):
        t = self._theme
        self.accent1_btn.setStyleSheet(f"background: {t.accent_1}; color: #000; border-radius: 8px; font-weight: 700;")
        self.accent2_btn.setStyleSheet(f"background: {t.accent_2}; color: #000; border-radius: 8px; font-weight: 700;")
        self.accent3_btn.setStyleSheet(f"background: {t.accent_3}; color: #000; border-radius: 8px; font-weight: 700;")
        self.blur_slider.setValue(int(self._main.backdrop._blur_radius))
        self.opacity_slider.setValue(int(t.page_bg_alpha * 100))
    
    def _pick_accent(self, which: int):
        color = QColorDialog.getColor(QColor(getattr(self._theme, f"accent_{which}")), self, "Pick Accent Color")
        if color.isValid():
            hex_color = color.name()
            setattr(self._theme, f"accent_{which}", hex_color)
            self._refresh_appearance_controls()
            self._main.apply_theme(self._theme.name)  # Re-apply with modified theme
    
    def _on_blur_change(self, value: int):
        self._main.backdrop.set_blur_radius(value)
    
    def _on_opacity_change(self, value: int):
        self._theme.page_bg_alpha = value / 100.0
        self._main.apply_theme(self._theme.name)
    
    def _save_custom_theme(self):
        name, ok = QFileDialog.getSaveFileName(self, "Save Theme", "", "JSON Files (*.json)")
        if ok:
            custom = ThemeColors(
                name=os.path.basename(name).replace(".json", ""),
                bg_main=self._theme.bg_main,
                bg_panel=self._theme.bg_panel,
                bg_surface=self._theme.bg_surface,
                bg_surface2=self._theme.bg_surface2,
                border=self._theme.border,
                border_soft=self._theme.border_soft,
                text_main=self._theme.text_main,
                text_muted=self._theme.text_muted,
                text_faint=self._theme.text_faint,
                accent_1=self._theme.accent_1,
                accent_2=self._theme.accent_2,
                accent_3=self._theme.accent_3,
                success=self._theme.success,
                warning=self._theme.warning,
                danger=self._theme.danger,
                bubble_sent=self._theme.bubble_sent,
                bubble_recv=self._theme.bubble_recv,
                page_bg_alpha=self._theme.page_bg_alpha,
                panel_top_alpha=self._theme.panel_top_alpha,
                panel_bottom_alpha=self._theme.panel_bottom_alpha,
            )
            self._main.theme_manager.save_custom_theme(custom)
            self.theme_combo.addItem(custom.name)
            self.theme_combo.setCurrentText(custom.name)
    
    TTS_VOICES = {
        "Neerja — English India (Female)": "en-IN-NeerjaNeural",
        "Prabhat — English India (Male)":  "en-IN-PrabhatNeural",
        "Swara — Hindi (Female)":          "hi-IN-SwaraNeural",
        "Madhur — Hindi (Male)":           "hi-IN-MadhurNeural",
        "Aria — English US (Female)":      "en-US-AriaNeural",
        "Guy — English US (Male)":         "en-US-GuyNeural",
    }

    WAKE_WORD_PRESETS = {
        "Hey Wave":   ["hey wave"],
        "Hello Wave": ["hello wave"],
        "Okay Wave":  ["okay wave", "ok wave"],
        "Wave":       ["wave"],
    }

    def _build_voice_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("VOICE SETTINGS", self._theme))
        
        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)
        
        # ── TTS Voice ────────────────────────────────
        # Tier 33: the cloned voice (named after its voice sample) comes
        # first - it's Wave's default whenever a sample is set.
        pv.addWidget(QLabel("TTS Voice:"))
        self.tts_combo = QComboBox()
        self._clone_label = tts_engine.clone_voice_label()
        self.tts_combo.addItems([self._clone_label] + list(self.TTS_VOICES.keys()))
        self.tts_combo.setCurrentText(self._voice_label(tts_engine.voice_choice))
        self.tts_combo.currentTextChanged.connect(self._on_tts_voice_change)
        pv.addWidget(self.tts_combo)
        self.tts_clone_status_lbl = QLabel(tts_engine.clone_status())
        self.tts_clone_status_lbl.setWordWrap(True)
        self.tts_clone_status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11.5px; background: transparent;")
        pv.addWidget(self.tts_clone_status_lbl)

        # ── Speech rate ───────────────────────────────
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Speech Rate:"))
        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(50, 200)
        try:
            self.rate_slider.setValue(100 + int(tts_engine.rate.replace("%", "")))
        except ValueError:
            self.rate_slider.setValue(108)
        self.rate_slider.valueChanged.connect(self._on_rate_change)
        pv.addWidget(self.rate_slider)

        # ── TTS Volume ────────────────────────────────
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("TTS Volume:"))
        self.tts_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.tts_vol_slider.setRange(0, 100)
        self.tts_vol_slider.setValue(int(tts_engine.volume_level * 100))
        self.tts_vol_slider.valueChanged.connect(self._on_tts_volume_change)
        pv.addWidget(self.tts_vol_slider)

        # ── Wake word ─────────────────────────────────
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Wake Word:"))
        self.wake_combo = QComboBox()
        self.wake_combo.addItems(list(self.WAKE_WORD_PRESETS.keys()) + ["Custom..."])
        self.wake_combo.currentTextChanged.connect(self._on_wake_word_change)
        pv.addWidget(self.wake_combo)

        # ── Tier 11: how the wake word is detected ────
        pv.addWidget(QLabel("Wake Word Detection:"))
        self.wake_engine_combo = QComboBox()
        self.wake_engine_combo.addItems(list(self.WAKE_ENGINES.keys()))
        current_engine = (getattr(config, "WAKE_WORD_ENGINE", "auto") or "auto").lower()
        self.wake_engine_combo.setCurrentText(next(
            (lbl for lbl, eng in self.WAKE_ENGINES.items() if eng == current_engine), "Automatic"))
        self.wake_engine_combo.currentTextChanged.connect(self._on_wake_engine_change)
        pv.addWidget(self.wake_engine_combo)
        pv.addWidget(QLabel("Wake Sensitivity:"))
        self.wake_sens_slider = QSlider(Qt.Orientation.Horizontal)
        self.wake_sens_slider.setRange(10, 90)          # = (1 - threshold) * 100
        try:
            self.wake_sens_slider.setValue(int(round((1.0 - get_wake_gate().threshold) * 100)))
        except Exception:
            self.wake_sens_slider.setValue(50)
        self.wake_sens_slider.valueChanged.connect(self._on_wake_sensitivity_change)
        pv.addWidget(self.wake_sens_slider)
        self.wake_status_lbl = QLabel("")
        self.wake_status_lbl.setWordWrap(True)
        self.wake_status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11.5px; background: transparent;")
        pv.addWidget(self.wake_status_lbl)
        self._refresh_wake_status()

        # ── STT Language ──────────────────────────────
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Speech Recognition Language:"))
        self.stt_lang = QComboBox()
        self.stt_lang.addItems(["en-US", "en-IN", "hi-IN", "es-ES", "fr-FR", "de-DE"])
        self.stt_lang.setCurrentText(stt_engine.language)
        self.stt_lang.currentTextChanged.connect(self._on_stt_lang_change)
        pv.addWidget(self.stt_lang)

        # ── Tier 21: local-voice (Piper) auto-download ────────────────
        # TTS_LOCAL_AUTO_DOWNLOAD already exists in settings/config.py as
        # "this project's primary consent mechanism" for downloading a
        # local Piper voice (see voice/tts.py's _confirm_piper_download
        # docstring) - it just never had a Settings UI control. Piper
        # itself stays optional/unbundled either way; this only toggles
        # whether WAVE is ALLOWED to fetch a voice model for it.
        pv.addWidget(make_divider(self._theme))
        self.cb_local_voice = QCheckBox("Download a local voice for offline speech (via Piper)")
        self.cb_local_voice.setChecked(bool(getattr(config, "TTS_LOCAL_AUTO_DOWNLOAD", False)))
        self.cb_local_voice.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
        self.cb_local_voice.toggled.connect(self._on_local_voice_toggle)
        pv.addWidget(self.cb_local_voice)
        self.local_voice_status_lbl = QLabel(self._local_voice_status_text())
        self.local_voice_status_lbl.setWordWrap(True)
        self.local_voice_status_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11.5px; background: transparent;")
        pv.addWidget(self.local_voice_status_lbl)

        v.addWidget(panel)
        v.addStretch()
        return page

    def _local_voice_status_text(self) -> str:
        from voice.tts import HAS_PIPER
        if not HAS_PIPER:
            return "piper-tts isn't installed - this stays off until it is (`pip install piper-tts`)."
        if getattr(tts_engine, "_piper_voice", None) is not None:
            return "A local Piper voice is already loaded."
        model_path = (getattr(config, "TTS_LOCAL_MODEL_PATH", "") or "").strip()
        if model_path:
            return f"Using the voice configured at {model_path}."
        return "No local voice downloaded yet - WAVE will fetch one the first time it's needed."

    # ── Voice Settings handlers ───────────────────────────────────────────
    def _on_tts_voice_change(self, label: str):
        voice_id = self._voice_id(label)
        if voice_id:
            tts_engine.set_voice(voice_id)
            self.refresh_clone_status()

    # Tier 33: the cloned voice in the picker
    def _voice_id(self, label: str):
        """A picker label -> its voice id (CLONE_VOICE_ID for the cloned
        voice), or None."""
        if label and label in (getattr(self, "_clone_label", None), tts_engine.clone_voice_label()):
            return CLONE_VOICE_ID
        return self.TTS_VOICES.get(label)

    def _voice_label(self, voice_id: str) -> str:
        if voice_id == CLONE_VOICE_ID:
            return getattr(self, "_clone_label", None) or tts_engine.clone_voice_label()
        return next((lbl for lbl, vid in self.TTS_VOICES.items() if vid == voice_id), next(iter(self.TTS_VOICES)))

    def current_tts_voice_id(self) -> str:
        """What "Save Settings" stores as "tts_voice"."""
        return self._voice_id(self.tts_combo.currentText()) or tts_engine.voice_choice

    def select_tts_voice(self, saved: str) -> None:
        """A saved "tts_voice" setting, applied. Since Tier 33 that's a voice
        id; before, it was the label of an edge-tts voice, saved when the
        picker had no cloned voice - it only ever chose the voice that fills
        in for the clone, and keeps exactly that meaning."""
        saved = str(saved or "")
        if saved in self.TTS_VOICES:
            tts_engine.set_fallback_voice(self.TTS_VOICES[saved])
            self.refresh_clone_voice()
        elif saved == CLONE_VOICE_ID or saved in self.TTS_VOICES.values():
            self.tts_combo.setCurrentText(self._voice_label(saved))

    def refresh_clone_voice(self) -> None:
        """The cloned voice's entry, the picked voice and the status line
        again - after the voice sample changed (start-up restore, "Build
        avatar")."""
        if not hasattr(self, "tts_combo"):
            return
        self._clone_label = tts_engine.clone_voice_label()
        self.tts_combo.blockSignals(True)
        try:
            self.tts_combo.setItemText(0, self._clone_label)
            self.tts_combo.setCurrentText(self._voice_label(tts_engine.voice_choice))
        finally:
            self.tts_combo.blockSignals(False)
        self.refresh_clone_status()

    def refresh_clone_status(self) -> None:
        """The status line under the picker; while the cloned voice is
        loading it checks again every few seconds, until it isn't."""
        label = getattr(self, "tts_clone_status_lbl", None)
        if label is None:
            return
        label.setText(tts_engine.clone_status())
        if tts_engine.clone_state() == "loading" and not getattr(self, "_clone_status_pending", False):
            self._clone_status_pending = True

            def again():
                self._clone_status_pending = False
                self.refresh_clone_status()
            QTimer.singleShot(3000, again)

    def _on_rate_change(self, value: int):
        tts_engine.set_rate(f"{value - 100:+d}%")

    def _on_tts_volume_change(self, value: int):
        tts_engine.set_volume(value / 100.0)

    def _on_wake_word_change(self, label: str):
        if label == "Custom...":
            text, ok = QInputDialog.getText(
                self, "Custom Wake Word",
                "What phrase should wake WAVE up?"
            )
            phrases = [text.strip().lower()] if ok and text.strip() else ["hey wave"]
        else:
            phrases = self.WAKE_WORD_PRESETS.get(label, [label.lower()])
        # "wake up" always works as a universal fallback on top of the preset.
        phrases = phrases + ["wake up"]
        self._main.wake_phrases = phrases
        if self._main.mic_worker:
            self._main.mic_worker.set_wake_phrases(phrases)
        self._refresh_wake_status()

    # Tier 11: label -> WAKE_WORD_ENGINE value (see voice/wakeword.py)
    WAKE_ENGINES = {
        "Automatic": "auto",
        "On-device model (openWakeWord)": "openwakeword",
        "Speech recognition only": "transcript",
    }

    def _apply_wake_gate(self):
        reset_wake_gate()
        gate = get_wake_gate()
        mic = getattr(self._main, "mic_worker", None)
        if mic is not None:
            mic.wake_gate = gate
        return gate

    def _on_wake_engine_change(self, label: str):
        config.WAKE_WORD_ENGINE = self.WAKE_ENGINES.get(label, "auto")
        threshold = None
        if hasattr(self, "wake_sens_slider"):
            threshold = 1.0 - self.wake_sens_slider.value() / 100.0
        gate = self._apply_wake_gate()
        if threshold is not None and getattr(self, "_wake_threshold_touched", False):
            gate.threshold = threshold
        self._refresh_wake_status()

    def _on_wake_sensitivity_change(self, value: int):
        self._wake_threshold_touched = True
        gate = get_wake_gate()
        gate.threshold = round(1.0 - value / 100.0, 2)
        self._refresh_wake_status()

    def wake_status_text(self) -> str:
        """One line for the Settings page: what is listening for the wake
        word right now, and why."""
        try:
            gate = get_wake_gate()
            phrases = list(getattr(self._main, "wake_phrases", []) or [])
            engine = getattr(gate, "engine", "auto")
            if engine == "transcript":
                return "Speech recognition listens for your wake phrase (no on-device model)."
            if not gate.wanted():
                return ("No on-device wake model found - speech recognition is used. Install "
                        "requirements-voice-local.txt to use Wave's built-in 'Hey Wave' model.")
            from voice.commands import normalize_utterance
            if engine == "auto" and normalize_utterance(gate.phrase) not in {normalize_utterance(p) for p in phrases}:
                return (f"The on-device model listens for '{gate.phrase.title()}' - pick that wake word "
                        f"to use it; '{(phrases or ['?'])[0]}' uses speech recognition.")
            # Never load (or download) the model from here - this runs on the
            # GUI thread; the mic loop loads it the first time Wave sleeps.
            if getattr(gate, "_load_failed", False):
                return ("The on-device model couldn't load (is openwakeword installed, and were its "
                        "feature files downloaded?) - speech recognition is used instead.")
            state = "active" if getattr(gate, "_model", None) is not None else "ready (starts when Wave sleeps)"
            check = (" Every detection is double-checked against what was said."
                     if float(getattr(gate, "confirm_below", 0.0) or 0.0) >= 1.0 else "")
            return (f"On-device '{gate.phrase.title()}' model {state}, threshold {gate.threshold:.2f}. "
                    "While asleep nothing is transcribed until it hears the wake word." + check)
        except Exception as e:
            return f"Wake word status unavailable: {e}"

    def _refresh_wake_status(self):
        if hasattr(self, "wake_status_lbl"):
            self.wake_status_lbl.setText(self.wake_status_text())

    def _on_stt_lang_change(self, lang_code: str):
        stt_engine.set_language(lang_code)

    def _on_local_voice_toggle(self, checked: bool):
        config.TTS_LOCAL_AUTO_DOWNLOAD = checked

    def _build_avatar_page(self):
        """Tier 25: the "girl ki tarah" avatar companion - closes
        JARVIS_GAP_ANALYSIS.md §2.1. Everything here is read/written by
        MainWindow.save_settings/load_settings (avatar_show_on_startup/
        avatar_show_dashboard_on_startup) plus the two path fields, which
        are only used as one-shot arguments to build_avatar_from_files()
        - they aren't needed again afterwards, so these two QLineEdits
        specifically are not restored from SETTINGS_FILE on load (nothing
        bad happens if they're left blank; the "Build avatar" button just
        stays a no-op until a photo is picked again). This is narrower
        than it sounds: the voice-clone reference path that a successful
        build actually hands to the TTS engine IS persisted and restored
        across restarts - just under its own key
        (avatar_voice_reference_path, in MainWindow._init_avatar()/
        build_avatar_from_files()), not through this page's widgets."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)

        v.addWidget(make_section_heading("AVATAR COMPANION", self._theme))

        startup_panel = QFrame()
        startup_panel.setProperty("class", "Panel")
        add_shadow(startup_panel)
        spv = QVBoxLayout(startup_panel)
        spv.setContentsMargins(24, 20, 24, 20)
        spv.setSpacing(14)

        self.cb_avatar_show = QCheckBox("Show the avatar when Wave starts")
        self.cb_avatar_show.setChecked(True)
        self.cb_avatar_show.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
        spv.addWidget(self.cb_avatar_show)

        self.cb_avatar_show_dashboard = QCheckBox("Also show the full dashboard window on startup")
        self.cb_avatar_show_dashboard.setChecked(False)
        self.cb_avatar_show_dashboard.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
        spv.addWidget(self.cb_avatar_show_dashboard)

        hint = QLabel(
            "With both left as above, only the avatar is visible - say 'wave ka ui dikhao' "
            "(or 'show the UI') any time to open the dashboard, and 'ui chhupao' to hide it again."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        spv.addWidget(hint)

        v.addWidget(startup_panel)
        v.addWidget(make_divider(self._theme))

        build_panel = QFrame()
        build_panel.setProperty("class", "Panel")
        add_shadow(build_panel)
        bpv = QVBoxLayout(build_panel)
        bpv.setContentsMargins(24, 20, 24, 20)
        bpv.setSpacing(12)

        bpv.addWidget(make_section_heading("BUILD YOUR AVATAR", self._theme))

        photo_row = QHBoxLayout()
        self.avatar_photo_edit = QLineEdit()
        self.avatar_photo_edit.setPlaceholderText("A clear, front-facing, well-lit photo of you...")
        self.avatar_photo_edit.setStyleSheet(
            f"color: {self._theme.text_main}; background: {self._theme.bg_surface}; "
            f"border: 1px solid {self._theme.border}; border-radius: 8px; padding: 8px 10px;")
        photo_row.addWidget(self.avatar_photo_edit, 1)
        btn_photo = QPushButton("Browse…")
        btn_photo.setProperty("class", "ActionBtn")
        btn_photo.clicked.connect(self._on_browse_avatar_photo)
        photo_row.addWidget(btn_photo)
        bpv.addLayout(photo_row)

        voice_row = QHBoxLayout()
        self.avatar_voice_edit = QLineEdit()
        self.avatar_voice_edit.setPlaceholderText("(Optional, for Tier 25's voice cloning) a clean ~20-30s voice sample...")
        self.avatar_voice_edit.setStyleSheet(
            f"color: {self._theme.text_main}; background: {self._theme.bg_surface}; "
            f"border: 1px solid {self._theme.border}; border-radius: 8px; padding: 8px 10px;")
        voice_row.addWidget(self.avatar_voice_edit, 1)
        btn_voice = QPushButton("Browse…")
        btn_voice.setProperty("class", "ActionBtn")
        btn_voice.clicked.connect(self._on_browse_avatar_voice)
        voice_row.addWidget(btn_voice)
        bpv.addLayout(voice_row)

        self.btn_build_avatar = QPushButton("Build avatar")
        self.btn_build_avatar.setProperty("class", "ActionBtn")
        self.btn_build_avatar.clicked.connect(self._on_build_avatar_clicked)
        bpv.addWidget(self.btn_build_avatar)

        self.avatar_build_status = QLabel("No avatar built yet - pick a photo above to get started.")
        self.avatar_build_status.setWordWrap(True)
        self.avatar_build_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        bpv.addWidget(self.avatar_build_status)

        v.addWidget(build_panel)
        v.addStretch()
        self._refresh_avatar_build_status()
        return page

    def _on_browse_avatar_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose your photo", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.avatar_photo_edit.setText(path)

    def _on_browse_avatar_voice(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a voice sample", "", "Audio (*.wav *.mp3 *.m4a *.flac *.ogg)")
        if path:
            self.avatar_voice_edit.setText(path)

    def _on_build_avatar_clicked(self):
        photo_path = self.avatar_photo_edit.text().strip()
        if not photo_path:
            self.avatar_build_status.setText("Pick a photo first.")
            return
        self.btn_build_avatar.setEnabled(False)
        result = self._main.build_avatar_from_files(photo_path, self.avatar_voice_edit.text().strip())
        self.btn_build_avatar.setEnabled(True)
        self.avatar_build_status.setText(result)
        self._main.toast.show(result, "success" if "ban gaya" in result else "error")

    def _refresh_avatar_build_status(self):
        avatar_window = getattr(self._main, "avatar_window", None)
        if avatar_window is not None and avatar_window.has_rig():
            self.avatar_build_status.setText("Avatar already built and ready.")

    def _build_hotkeys_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("GLOBAL HOTKEYS", self._theme))
        
        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)
        
        hotkeys = [
            ("Toggle Mic", "ctrl+shift+space"),
            ("Command Palette", "ctrl+k"),
            ("Show/Hide Window", "ctrl+alt+w"),
            ("Next Tab", "ctrl+tab"),
            ("Previous Tab", "ctrl+shift+tab"),
            ("Clear Chat", "ctrl+l"),
        ]
        
        for name, default in hotkeys:
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl = QLabel(name)
            lbl.setFixedWidth(160)
            lbl.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
            edit = QLineEdit(default)
            edit.setReadOnly(True)
            edit.setFixedWidth(200)
            edit.setStyleSheet(f"background: {self._theme.bg_surface}; color: {self._theme.text_muted}; border: 1px solid {self._theme.border}; border-radius: 8px; padding: 8px 12px;")
            btn_change = QPushButton("Change")
            btn_change.setProperty("class", "ActionBtn")
            btn_change.setFixedWidth(80)
            btn_change.clicked.connect(partial(self._capture_hotkey, edit, name))
            row.addWidget(lbl)
            row.addWidget(edit)
            row.addWidget(btn_change)
            row.addStretch()
            pv.addLayout(row)
        
        v.addWidget(panel)
        v.addStretch()
        return page
    
    def _capture_hotkey(self, edit: QLineEdit, name: str):
        edit.setText("Press keys...")
        edit.setStyleSheet(f"background: {self._theme.accent_1}30; color: {self._theme.accent_1}; border: 1px solid {self._theme.accent_1}; border-radius: 8px; padding: 8px 12px;")
        # Would need a key capture dialog - simplified
    
    def _build_privacy_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("PRIVACY & DATA", self._theme))
        
        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)
        
        self.cb_clipboard = QCheckBox("Enable clipboard monitoring")
        self.cb_clipboard.setChecked(True)
        self.cb_clipboard.toggled.connect(self._on_clipboard_toggle)
        self.cb_screen = QCheckBox("Allow screen capture (OCR/Vision)")
        self.cb_screen.setChecked(True)
        self.cb_screen.toggled.connect(self._on_screen_capture_toggle)
        self.cb_chat_history = QCheckBox("Save chat history locally")
        self.cb_chat_history.setChecked(True)
        self.cb_chat_history.toggled.connect(self._on_chat_history_toggle)
        # Tier 21: gates ToastManager (the one real notification surface
        # WAVE has - there's no OS-level desktop notification code here).
        self.cb_notifications = QCheckBox("Desktop notifications")
        self.cb_notifications.setChecked(True)
        self.cb_notifications.toggled.connect(self._on_notifications_toggle)
        # Tier 21: "Local-only usage stats, opt-in" - see
        # telemetry/usage_stats.py for exactly what this does and doesn't do.
        self.cb_usage_stats = QCheckBox("Show my local usage stats (never leaves this device)")
        self.cb_usage_stats.setChecked(bool(getattr(config, "USAGE_STATS_ENABLED", False)))
        self.cb_usage_stats.toggled.connect(self._on_usage_stats_toggle)
        # 🔧 FIX: "Send anonymous usage analytics" was removed - there is no
        # analytics/telemetry code anywhere in this app, so that checkbox
        # had nothing real to control either way. A checkbox that looks like
        # a real data-sharing control but silently does nothing is worse
        # than not having it.

        for cb in [self.cb_clipboard, self.cb_screen, self.cb_chat_history, self.cb_notifications, self.cb_usage_stats]:
            cb.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
            pv.addWidget(cb)
        
        pv.addWidget(make_divider(self._theme))
        
        btn_clear_data = QPushButton("Clear All Local Data")
        btn_clear_data.setProperty("class", "DangerPill")
        btn_clear_data.clicked.connect(self._clear_all_data)
        pv.addWidget(btn_clear_data)
        
        v.addWidget(panel)
        v.addStretch()
        return page

    # 🔧 FIX: these three checkboxes used to just sit there - toggling them
    # changed nothing anywhere else in the app. Each one now actually gates
    # its feature, and takes effect immediately (no restart needed).
    def _on_clipboard_toggle(self, checked: bool):
        if self._main.clip_worker:
            self._main.clip_worker.monitoring_enabled = checked

    def _on_screen_capture_toggle(self, checked: bool):
        VisionCapture.capture_enabled = checked

    def _on_chat_history_toggle(self, checked: bool):
        db.history_enabled = checked

    def _on_notifications_toggle(self, checked: bool):
        if getattr(self._main, "toast", None) is not None:
            self._main.toast.enabled = checked

    def _on_usage_stats_toggle(self, checked: bool):
        config.USAGE_STATS_ENABLED = checked

    def _clear_all_data(self):
        reply = QMessageBox.question(self, "Confirm", "Delete ALL local data? (Chat history, remembered preferences, saved settings, the activity log, and Wave's trash)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            if os.path.exists(SETTINGS_FILE):
                os.remove(SETTINGS_FILE)
            # 🔧 FIX: db.clear_all() didn't exist before - this call always
            # raised AttributeError, silently swallowed by the try/except
            # that used to be here, so "Done" was shown every time even
            # though nothing was ever actually deleted. clear_all() now
            # exists and reports back whether it actually succeeded.
            try:
                undo_mod.trash.empty()
                audit_mod.audit.clear()
                security_log.clear()
            except Exception as e:
                logging.getLogger("MainWindow").error(f"Couldn't clear trash/activity: {e}")
            if db.clear_all():
                QMessageBox.information(self, "Done", "All local data cleared. Restart the app.")
            else:
                QMessageBox.warning(self, "Partial failure",
                                     "Settings were cleared, but the local database couldn't be wiped - "
                                     "check wave_system.log for details.")
    
    def _build_security_page(self):
        """Tier 20: mode selector + a read-only recent-events list, backed
        by automation/security_log.py. No new persistence of its own - the
        mode is config.SECURITY_MODE (saved/loaded the same way every other
        settings_panel toggle already is, see MainWindow.save_settings/
        load_settings), and the events list is RAM-only, same as the
        Activity page's own audit log."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)

        v.addWidget(make_section_heading("SECURITY", self._theme))

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)

        mode_label = QLabel("Security mode:")
        mode_label.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
        pv.addWidget(mode_label)

        self.security_mode_combo = QComboBox()
        self.security_mode_combo.addItems(["off", "standard", "strict"])
        self.security_mode_combo.setCurrentText(security_log.effective_mode())
        self.security_mode_combo.currentTextChanged.connect(self._on_security_mode_changed)
        pv.addWidget(self.security_mode_combo)

        mode_help = QLabel(
            "off: no extra guard beyond Wave's normal per-tool rules.\n"
            "standard: the default tier x taint policy (recommended).\n"
            "strict: tighter - more things need a spoken \"yes\" before running."
        )
        mode_help.setWordWrap(True)
        mode_help.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        pv.addWidget(mode_help)

        pv.addWidget(make_divider(self._theme))

        self.cb_redact_secrets = QCheckBox("Redact secrets (API keys, card numbers, etc.) from logs and diagnostics")
        self.cb_redact_secrets.setChecked(bool(getattr(config, "REDACT_SECRETS_ENABLED", True)))
        self.cb_redact_secrets.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
        self.cb_redact_secrets.toggled.connect(self._on_redact_toggle)
        pv.addWidget(self.cb_redact_secrets)

        v.addWidget(panel)

        events_panel = QFrame()
        events_panel.setProperty("class", "Panel")
        add_shadow(events_panel)
        ev = QVBoxLayout(events_panel)
        ev.setContentsMargins(24, 20, 24, 20)
        ev.setSpacing(10)

        header_row = QHBoxLayout()
        header_row.addWidget(make_section_heading("RECENT SECURITY EVENTS", self._theme))
        header_row.addStretch()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.setProperty("class", "ActionBtn")
        btn_refresh.clicked.connect(self._refresh_security_events)
        header_row.addWidget(btn_refresh)
        ev.addLayout(header_row)

        self.security_events_list = QListWidget()
        self.security_events_list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; color: {self._theme.text_main}; font-size: 11px; }}
            QListWidget::item {{ padding: 6px 4px; border-bottom: 1px solid {self._theme.border_soft}; }}
        """)
        self.security_events_list.setMinimumHeight(220)
        ev.addWidget(self.security_events_list)

        v.addWidget(events_panel)
        v.addStretch()

        self._refresh_security_events()
        return page

    def _on_security_mode_changed(self, text: str):
        actual = security_log.set_mode(text)
        if actual != text:
            self.security_mode_combo.blockSignals(True)
            self.security_mode_combo.setCurrentText(actual)
            self.security_mode_combo.blockSignals(False)
        self._refresh_security_events()

    def _on_redact_toggle(self, checked: bool):
        config.REDACT_SECRETS_ENABLED = checked

    def _refresh_security_events(self):
        self.security_events_list.clear()
        events = security_log.recent(50)
        if not events:
            self.security_events_list.addItem("No security events yet.")
            return
        for e in events:
            label = f"[{e.time_text}] {e.kind}"
            if e.tool:
                label += f" · {e.tool}"
            label += f" — {e.reason}"
            self.security_events_list.addItem(label)

    def _build_remote_page(self):
        """Tier 23: control Wave from a phone on the same Wi-Fi/LAN -
        closes §2.2 of claude/JARVIS_GAP_ANALYSIS.md. No persistence of
        its own beyond the two fields MainWindow.save_settings/
        load_settings already handle (remote_dashboard_enabled,
        remote_dashboard_port) - everything else here (the pairing
        token/code, the QR image) is regenerated fresh every time the
        dashboard (re)starts, by design (see remote/runner.py)."""
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)

        v.addWidget(make_section_heading("REMOTE DASHBOARD", self._theme))

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)

        self.cb_remote_dashboard = QCheckBox("Enable remote dashboard (control Wave from your phone)")
        self.cb_remote_dashboard.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px; spacing: 10px;")
        self.cb_remote_dashboard.toggled.connect(self._on_remote_dashboard_toggled)
        pv.addWidget(self.cb_remote_dashboard)

        warn = QLabel(
            "While this is on, Wave is reachable from any device on your Wi-Fi/LAN - "
            "not just your phone. Never forward this port through your router to the internet."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {self._theme.warning}; font-size: 11px;")
        pv.addWidget(warn)

        pv.addWidget(make_divider(self._theme))

        port_row = QHBoxLayout()
        port_label = QLabel("Port:")
        port_label.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
        port_row.addWidget(port_label)
        self.remote_port_spin = QSpinBox()
        self.remote_port_spin.setRange(1024, 65535)
        self.remote_port_spin.setValue(8787)
        port_row.addWidget(self.remote_port_spin)
        port_row.addStretch()
        pv.addLayout(port_row)

        self.remote_status_label = QLabel("Off.")
        self.remote_status_label.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        self.remote_status_label.setWordWrap(True)
        pv.addWidget(self.remote_status_label)

        v.addWidget(panel)

        pairing_panel = QFrame()
        pairing_panel.setProperty("class", "Panel")
        add_shadow(pairing_panel)
        ppv = QVBoxLayout(pairing_panel)
        ppv.setContentsMargins(24, 20, 24, 20)
        ppv.setSpacing(10)

        ppv.addWidget(make_section_heading("PAIR A PHONE", self._theme))

        self.remote_qr_label = QLabel("Start the dashboard to show a pairing QR code here.")
        self.remote_qr_label.setWordWrap(True)
        self.remote_qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remote_qr_label.setMinimumHeight(180)
        self.remote_qr_label.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        ppv.addWidget(self.remote_qr_label)

        self.remote_code_label = QLabel("")
        self.remote_code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remote_code_label.setStyleSheet(f"color: {self._theme.text_main}; font-size: 18px; font-weight: 700; letter-spacing: 2px;")
        ppv.addWidget(self.remote_code_label)

        self.remote_url_label = QLabel("")
        self.remote_url_label.setWordWrap(True)
        self.remote_url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.remote_url_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.remote_url_label.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11px;")
        ppv.addWidget(self.remote_url_label)

        self.btn_regenerate_pairing = QPushButton("Regenerate pairing code (signs out every paired phone)")
        self.btn_regenerate_pairing.setProperty("class", "ActionBtn")
        self.btn_regenerate_pairing.clicked.connect(self._on_regenerate_pairing_clicked)
        self.btn_regenerate_pairing.setEnabled(False)
        ppv.addWidget(self.btn_regenerate_pairing)

        v.addWidget(pairing_panel)
        v.addStretch()
        return page

    def _on_remote_dashboard_toggled(self, checked: bool):
        if checked:
            self._main.start_remote_dashboard(self.remote_port_spin.value())
        else:
            self._main.stop_remote_dashboard()
            self.remote_status_label.setText("Off.")
            self.remote_qr_label.setText("Start the dashboard to show a pairing QR code here.")
            self.remote_qr_label.setPixmap(QPixmap())
            self.remote_code_label.setText("")
            self.remote_url_label.setText("")
            self.btn_regenerate_pairing.setEnabled(False)

    def _on_regenerate_pairing_clicked(self):
        self._main.regenerate_remote_pairing()

    def show_remote_dashboard_result(self, ok: bool, message: str):
        """Called by MainWindow after start_remote_dashboard()/
        stop_remote_dashboard() finish on the worker thread."""
        self.remote_status_label.setText(message)
        if not ok:
            self.cb_remote_dashboard.blockSignals(True)
            self.cb_remote_dashboard.setChecked(False)
            self.cb_remote_dashboard.blockSignals(False)
            self.btn_regenerate_pairing.setEnabled(False)
            return
        self.btn_regenerate_pairing.setEnabled(True)
        self.refresh_remote_pairing_display()

    def refresh_remote_pairing_display(self):
        """Pulls the current pairing QR/code/URL from MainWindow.remote_
        dashboard and shows them - called right after a successful start
        and after every "Regenerate pairing code" click."""
        info = self._main.remote_pairing_info()
        if info is None:
            return
        self.remote_code_label.setText(info.code)
        self.remote_url_label.setText(info.url)
        try:
            from remote.qr import HAS_QRCODE, build_pairing_qr_png
            if HAS_QRCODE:
                png = build_pairing_qr_png(info.url)
                pix = QPixmap()
                pix.loadFromData(png, "PNG")
                self.remote_qr_label.setPixmap(pix)
                self.remote_qr_label.setText("")
                return
        except Exception:
            pass
        self.remote_qr_label.setText(
            "Install 'qrcode[pil]' to show a scannable code - for now, "
            "open the URL above on your phone's browser instead."
        )

    def _provider_text(self):
        info = self._main.brain.provider_info()
        line = f"{info['provider']} / {info['model']}  ·  {info['endpoint']}  ·  key {info['key']}"
        return line if info["configured"] else f"{line}\nNot usable yet: {info['problem']}"

    def _provider_brief(self):
        info = self._main.brain.provider_info()
        return f"{info['provider']} ({info['model']})"

    def _test_provider_clicked(self):
        self.btn_test_provider.setEnabled(False)
        self.provider_status.setText("Testing...")
        worker = self._main.worker_pool.submit(self._main.brain.check_provider)
        worker.signals.result.connect(self._on_provider_checked)
        worker.signals.error.connect(lambda e: self._on_provider_checked((False, str(e), 0)))

    def _on_provider_checked(self, result):
        ok, message, ms = result
        self.provider_status.setText(f"{'OK' if ok else 'Problem'}: {message}" + (f" ({ms} ms)" if ok else ""))
        self.btn_test_provider.setEnabled(True)

    def _build_advanced_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("ADVANCED", self._theme))
        
        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)
        
        # Log level
        pv.addWidget(QLabel("Log Level:"))
        self.log_combo = QComboBox()
        self.log_combo.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.log_combo.setCurrentText("INFO")
        pv.addWidget(self.log_combo)
        
        # API settings
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("AI provider (switch it with LLM_PROVIDER in .env):"))
        self.provider_lbl = QLabel(self._provider_text())
        self.provider_lbl.setWordWrap(True)
        pv.addWidget(self.provider_lbl)
        
        self.btn_test_provider = QPushButton("Test connection")
        self.btn_test_provider.setProperty("class", "ActionBtn")
        self.btn_test_provider.clicked.connect(self._test_provider_clicked)
        pv.addWidget(self.btn_test_provider)
        self.provider_status = QLabel("")
        self.provider_status.setWordWrap(True)
        pv.addWidget(self.provider_status)
        
        # Reset buttons
        pv.addWidget(make_divider(self._theme))
        btn_reset_ui = QPushButton("Reset UI Layout")
        btn_reset_ui.setProperty("class", "ActionBtn")
        pv.addWidget(btn_reset_ui)
        
        v.addWidget(panel)
        v.addStretch()
        return page
    
    def _build_about_page(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        
        v.addWidget(make_section_heading("ABOUT WAVE", self._theme))
        
        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(24, 20, 24, 20)
        pv.setSpacing(14)
        
        about_text = f"""
        <div style="text-align:center; line-height:1.8;">
        <h2 style="color:{self._theme.text_main}; margin:0;">{config.APP_NAME}</h2>
        <p style="color:{self._theme.text_muted}; margin:8px 0;">Version {config.VERSION}</p>
        <hr style="border-color:{self._theme.border}; margin:16px 0;">
        <p style="color:{self._theme.text_main};">Your intelligent desktop companion — voice-controlled, always ready to help.</p>
        <p style="color:{self._theme.text_muted}; font-size:12px;">
        Voice: Windows SAPI (TTS) + Google Speech Recognition (STT)<br>
        Brain: {self._provider_brief()}<br>
        Vision: Tesseract OCR<br>
        UI: PyQt6 + qtawesome
        </p>
        <hr style="border-color:{self._theme.border}; margin:16px 0;">
        <p style="color:{self._theme.text_faint}; font-size:11px;">
        Built with ❤️ using Python & Qt
        </p>
        </div>
        """
        about_lbl = QLabel(about_text)
        about_lbl.setWordWrap(True)
        about_lbl.setStyleSheet("background: transparent;")
        pv.addWidget(about_lbl)
        
        # Links
        links_row = QHBoxLayout()
        links_row.setSpacing(12)
        for text, url in [("GitHub", "https://github.com"), ("Issues", "https://github.com/issues"), ("Docs", "https://docs.example.com")]:
            btn = QPushButton(text)
            btn.setProperty("class", "ActionBtn")
            btn.clicked.connect(lambda _, u=url: os.system(f"start {u}"))
            links_row.addWidget(btn)
        links_row.addStretch()
        pv.addLayout(links_row)
        
        v.addWidget(panel)
        v.addStretch()
        return page


# =============================================================================
# 🔔 TOAST NOTIFICATIONS
# =============================================================================
