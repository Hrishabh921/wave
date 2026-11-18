"""
ui/onboarding.py
=============================================================================
OnboardingWizard: first-run setup dialog (mic test, wake word, theme,
and - since Tier 1 - actually validating GROQ_API_KEY / the LLM provider).
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.icons import Icon
from ui.effects import Anim, add_shadow, set_active
import voice.audio_devices as audio_devices
from voice.tts import HAS_PIPER


class OnboardingWizard(QDialog):
    def __init__(self, parent, theme: ThemeColors, on_complete: Callable):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self._theme = theme
        self._on_complete = on_complete
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedSize(520, 460)

        # Tier 21: the four new steps (Wake-Word Calibration, Language,
        # Local Voice, Calendar) are appended AFTER Permissions and before
        # Done rather than interleaved with the original six - that keeps
        # every pre-existing step at its original index, so nothing that
        # already looked up a step by position (tests included) breaks.
        self._steps = [
            ("Welcome", "Hi! I'm WAVE — your desktop AI assistant.\nLet's get you set up in 30 seconds.", "👋"),
            ("AI Brain", "Before anything else, let's confirm your AI\nprovider is configured and actually reachable.", "🧠"),
            ("Microphone", "We need microphone access for voice commands.\nPick your devices and test them.", "🎙"),
            ("Wake Word", "Choose your wake phrase:", "🔊"),
            ("Theme", "Pick your accent color:", "🎨"),
            ("Permissions", "Allow these for the best experience:", "🛡"),
            ("Wake-Word Calibration", "Let's measure your room's background\nnoise so wake-word detection is accurate.", "📏"),
            ("Language", "What language will you mostly speak to me in?", "🌐"),
            ("Local Voice", "Optional: let WAVE speak offline\nusing a local voice (Piper).", "💾"),
            ("Calendar", "Optional: connect an ICS calendar feed\n(you can always add this later in Settings).", "📅"),
            ("Done", "You're all set! Press <b>Ctrl+Shift+Space</b> anytime to wake me.", "✨"),
        ]
        self._current_step = 0
        
        self._setup_ui()
        self._show_step(0)
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        container = QFrame()
        container.setProperty("class", "Panel")
        add_shadow(container, blur=40, y_offset=20)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(32, 28, 32, 28)
        cl.setSpacing(20)
        
        # Progress dots
        self.progress_row = QHBoxLayout()
        self.progress_row.setSpacing(8)
        self.progress_row.addStretch()
        self.progress_dots = []
        for i in range(len(self._steps)):
            dot = QLabel("●")
            dot.setFixedSize(12, 12)
            dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            dot.setStyleSheet(f"color: {self._theme.text_faint}; font-size: 10px;")
            self.progress_dots.append(dot)
            self.progress_row.addWidget(dot)
        self.progress_row.addStretch()
        cl.addLayout(self.progress_row)
        
        # Icon
        self.icon_lbl = QLabel("👋")
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setStyleSheet("font-size: 48px; background: transparent;")
        cl.addWidget(self.icon_lbl)
        
        # Title
        self.title_lbl = QLabel("Welcome")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_lbl.setStyleSheet(f"color: {self._theme.text_main}; font-size: 22px; font-weight: 800; background: transparent;")
        cl.addWidget(self.title_lbl)
        
        # Description
        self.desc_lbl = QLabel()
        self.desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.desc_lbl.setWordWrap(True)
        self.desc_lbl.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 13px; background: transparent; line-height: 1.5;")
        cl.addWidget(self.desc_lbl)
        
        # Dynamic content area
        self.content_area = QWidget()
        self.content_layout = QVBoxLayout(self.content_area)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)
        cl.addWidget(self.content_area, 1)
        
        # Buttons
        self.btn_row = QHBoxLayout()
        self.btn_row.setSpacing(12)
        self.btn_back = QPushButton("Back")
        self.btn_back.setProperty("class", "ActionBtn")
        self.btn_back.clicked.connect(self._go_back)
        self.btn_next = QPushButton("Next")
        self.btn_next.setObjectName("SaveBtn")
        self.btn_next.clicked.connect(self._go_next)
        self.btn_row.addStretch()
        self.btn_row.addWidget(self.btn_back)
        self.btn_row.addWidget(self.btn_next)
        cl.addLayout(self.btn_row)
        
        layout.addWidget(container)
    
    def _clear_content(self):
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
    
    def _show_step(self, index: int):
        self._current_step = index
        title, desc, icon = self._steps[index]
        self.icon_lbl.setText(icon)
        self.title_lbl.setText(title)
        self.desc_lbl.setText(desc)
        
        # Update progress dots
        for i, dot in enumerate(self.progress_dots):
            if i <= index:
                dot.setStyleSheet(f"color: {self._theme.accent_1}; font-size: 10px;")
            else:
                dot.setStyleSheet(f"color: {self._theme.text_faint}; font-size: 10px;")
        
        self._clear_content()
        
        if index == 1:  # AI Brain — real connectivity check, not cosmetic
            info = self.parent().brain.provider_info()
            self.ai_info_lbl = QLabel(f"{info['provider']} / {info['model']}  ·  {info['endpoint']}  ·  key {info['key']}")
            self.ai_info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.ai_info_lbl.setWordWrap(True)
            self.ai_info_lbl.setStyleSheet(f"color: {self._theme.text_faint}; font-size: 11px;")
            self.ai_test_btn = QPushButton("🧠  Test Connection")
            self.ai_test_btn.setProperty("class", "ActionBtn")
            self.ai_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.ai_test_btn.clicked.connect(self._test_ai_provider)
            self.ai_status = QLabel(
                f"⚠ Not usable yet: {info['problem']}" if not info["configured"] else "Click to verify it's actually reachable..."
            )
            self.ai_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.ai_status.setWordWrap(True)
            self.ai_status.setStyleSheet(
                f"color: {self._theme.warning if not info['configured'] else self._theme.text_muted}; font-size: 12px;"
            )
            self.content_layout.addWidget(self.ai_info_lbl)
            self.content_layout.addWidget(self.ai_test_btn)
            self.content_layout.addWidget(self.ai_status)
        
        elif index == 2:  # Microphone + speaker picker with a real level test
            # Tier 21: real device picker - these combos list whatever
            # speech_recognition/PyAudio actually see (voice/audio_devices.py),
            # never a fake/hardcoded device list. An empty combo (no audio
            # subsystem in a sandbox) just means the Test buttons measure/
            # play nothing and say so - never a fabricated reading.
            self.content_layout.addWidget(QLabel("Microphone:"))
            self.input_device_combo = QComboBox()
            self._input_devices = audio_devices.list_input_devices()
            if self._input_devices:
                self.input_device_combo.addItems([name for _, name in self._input_devices])
            else:
                self.input_device_combo.addItem("No microphone found")
                self.input_device_combo.setEnabled(False)
            self.content_layout.addWidget(self.input_device_combo)

            self.mic_test_btn = QPushButton("🎙  Test Microphone")
            self.mic_test_btn.setProperty("class", "ActionBtn")
            self.mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mic_test_btn.clicked.connect(self._test_mic)
            self.mic_status = QLabel("Click to test...")
            self.mic_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.mic_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
            self.content_layout.addWidget(self.mic_test_btn)
            self.content_layout.addWidget(self.mic_status)

            self.content_layout.addWidget(QLabel("Speaker:"))
            self.output_device_combo = QComboBox()
            self._output_devices = audio_devices.list_output_devices()
            if self._output_devices:
                self.output_device_combo.addItems([name for _, name in self._output_devices])
            else:
                self.output_device_combo.addItem("No speaker found")
                self.output_device_combo.setEnabled(False)
            self.content_layout.addWidget(self.output_device_combo)

            self.speaker_test_btn = QPushButton("🔊  Play Test Sound")
            self.speaker_test_btn.setProperty("class", "ActionBtn")
            self.speaker_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.speaker_test_btn.clicked.connect(self._test_speaker)
            self.speaker_status = QLabel("")
            self.speaker_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.speaker_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
            self.content_layout.addWidget(self.speaker_test_btn)
            self.content_layout.addWidget(self.speaker_status)

        elif index == 3:  # Wake word
            self.wake_combo = QComboBox()
            self.wake_combo.addItems(["Hey Wave", "Hello Wave", "Okay Wave", "Custom..."])
            self.wake_combo.setStyleSheet(f"padding: 10px; font-size: 13px;")
            self.content_layout.addWidget(self.wake_combo)
        
        elif index == 4:  # Theme
            self.theme_btns = []
            for theme_name in ["Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark"]:
                btn = QPushButton(theme_name)
                btn.setProperty("class", "Pill")
                btn.setCheckable(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.clicked.connect(partial(self._select_theme, theme_name, btn))
                self.theme_btns.append(btn)
                self.content_layout.addWidget(btn)
            # Select current
            for btn in self.theme_btns:
                if btn.text() == self.parent().theme_manager.get_current().name:
                    btn.setChecked(True)
                    set_active(btn, True)
        
        elif index == 5:  # Permissions
            self.perm_clipboard = QCheckBox("Clipboard history (read copied text)")
            self.perm_clipboard.setChecked(True)
            self.perm_screen = QCheckBox("Screen capture (for OCR / vision)")
            self.perm_screen.setChecked(True)
            self.perm_notifications = QCheckBox("Desktop notifications")
            self.perm_notifications.setChecked(True)
            for cb in [self.perm_clipboard, self.perm_screen, self.perm_notifications]:
                cb.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
                self.content_layout.addWidget(cb)

        elif index == 6:  # Wake-Word Calibration
            self.calibrate_btn = QPushButton("📏  Calibrate Now")
            self.calibrate_btn.setProperty("class", "ActionBtn")
            self.calibrate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.calibrate_btn.clicked.connect(self._run_calibration)
            self.calibrate_status = QLabel("Stay quiet for a second, then click Calibrate.")
            self.calibrate_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.calibrate_status.setWordWrap(True)
            self.calibrate_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
            self.content_layout.addWidget(self.calibrate_btn)
            self.content_layout.addWidget(self.calibrate_status)

        elif index == 7:  # Language
            self.lang_combo = QComboBox()
            self.lang_combo.addItems(["en-US", "en-IN", "hi-IN", "es-ES", "fr-FR", "de-DE"])
            self.lang_combo.setCurrentText(getattr(stt_engine, "language", "en-US"))
            self.content_layout.addWidget(self.lang_combo)

        elif index == 8:  # Local Voice (Piper)
            self.local_voice_cb = QCheckBox("Download a local voice for offline speech (via Piper)")
            self.local_voice_cb.setChecked(bool(getattr(config, "TTS_LOCAL_AUTO_DOWNLOAD", False)))
            self.local_voice_cb.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
            self.content_layout.addWidget(self.local_voice_cb)
            status = QLabel(
                "You can turn this on now, or later in Settings > Voice." if HAS_PIPER else
                "piper-tts isn't installed - this stays off until it is (`pip install piper-tts`)."
            )
            status.setWordWrap(True)
            status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 11.5px;")
            self.content_layout.addWidget(status)

        elif index == 9:  # Calendar connect
            self.cal_name_edit = QLineEdit()
            self.cal_name_edit.setPlaceholderText("Feed name (e.g. 'work')")
            self.cal_url_edit = QLineEdit()
            self.cal_url_edit.setPlaceholderText("ICS feed URL (leave blank to skip)")
            self.content_layout.addWidget(self.cal_name_edit)
            self.content_layout.addWidget(self.cal_url_edit)

        # Update buttons
        self.btn_back.setVisible(index > 0)
        if index == len(self._steps) - 1:
            self.btn_next.setText("Finish")
        else:
            self.btn_next.setText("Next")
    
    def _selected_device_index(self, combo: QComboBox, devices: list):
        """Maps a combo's current row back to the (index, name) list it was
        built from - None (system default) if nothing real is selected."""
        row = combo.currentIndex()
        if 0 <= row < len(devices):
            return devices[row][0]
        return None

    def _test_mic(self):
        """Tier 21: a REAL level test (voice/audio_devices.measure_input_level)
        on a background worker, replacing the old 2-second QTimer that just
        pretended to listen regardless of whether a mic even worked."""
        self.mic_status.setText("Listening... speak now")
        self.mic_status.setStyleSheet(f"color: {self._theme.accent_1}; font-size: 12px;")
        device_index = self._selected_device_index(self.input_device_combo, self._input_devices)
        worker = self.parent().worker_pool.submit(audio_devices.measure_input_level, device_index, 1.5)
        worker.signals.result.connect(self._on_mic_level_measured)
        worker.signals.error.connect(lambda e: self._on_mic_level_measured(0.0))

    def _on_mic_level_measured(self, level: float):
        try:
            if level <= 0.5:
                self.mic_status.setText("⚠ Heard nothing - check the mic is selected and unmuted.")
                self.mic_status.setStyleSheet(f"color: {self._theme.warning}; font-size: 12px;")
            else:
                self.mic_status.setText(f"✓ Microphone working! (peak level {level:.0f}%)")
                self.mic_status.setStyleSheet(f"color: {self._theme.success}; font-size: 12px;")
        except RuntimeError:
            pass  # user already moved past this step

    def _test_speaker(self):
        device_index = self._selected_device_index(self.output_device_combo, self._output_devices)
        self.speaker_status.setText("Playing...")
        self.speaker_status.setStyleSheet(f"color: {self._theme.accent_1}; font-size: 12px;")
        worker = self.parent().worker_pool.submit(audio_devices.play_test_tone, device_index)
        worker.signals.result.connect(self._on_speaker_test_done)
        worker.signals.error.connect(lambda e: self._on_speaker_test_done(False))

    def _on_speaker_test_done(self, played: bool):
        try:
            if played:
                self.speaker_status.setText("✓ Did you hear that?")
                self.speaker_status.setStyleSheet(f"color: {self._theme.success}; font-size: 12px;")
            else:
                self.speaker_status.setText("⚠ Couldn't play - check the speaker is selected.")
                self.speaker_status.setStyleSheet(f"color: {self._theme.warning}; font-size: 12px;")
        except RuntimeError:
            pass

    def _run_calibration(self):
        """Tier 21: backs the Wake-Word Calibration step with the real
        voice/stt.py SpeechReceiver.recalibrate() measurement rather than a
        second, parallel calibration implementation."""
        self.calibrate_btn.setEnabled(False)
        self.calibrate_status.setText("Calibrating...")
        self.calibrate_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
        worker = self.parent().worker_pool.submit(stt_engine.recalibrate)
        worker.signals.result.connect(self._on_calibration_done)
        worker.signals.error.connect(lambda e: self._on_calibration_done(None))

    def _on_calibration_done(self, result):
        try:
            self.calibrate_btn.setEnabled(True)
            if not result:
                self.calibrate_status.setText("⚠ Calibration failed - you can retry later in Settings.")
                self.calibrate_status.setStyleSheet(f"color: {self._theme.warning}; font-size: 12px;")
                return
            self.calibrate_status.setText(
                f"✓ Calibrated. Ambient level {result['baseline_energy']:.0f} -> "
                f"idle threshold {result['idle_threshold']:.0f}."
            )
            self.calibrate_status.setStyleSheet(f"color: {self._theme.success}; font-size: 12px;")
        except RuntimeError:
            pass

    def _test_ai_provider(self):
        """Actually calls the configured LLM provider (same check_provider() the
        Settings page uses) instead of just trusting that a key is present."""
        self.ai_test_btn.setEnabled(False)
        self.ai_status.setText("Testing...")
        self.ai_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
        worker = self.parent().worker_pool.submit(self.parent().brain.check_provider)
        worker.signals.result.connect(self._on_ai_provider_checked)
        worker.signals.error.connect(lambda e: self._on_ai_provider_checked((False, str(e), 0)))
    
    def _on_ai_provider_checked(self, result):
        ok, message, ms = result
        try:
            self.ai_status.setText(f"{'✓' if ok else '✗'} {message}" + (f" ({ms} ms)" if ok else ""))
            self.ai_status.setStyleSheet(f"color: {self._theme.success if ok else self._theme.danger}; font-size: 12px;")
            self.ai_test_btn.setEnabled(True)
        except RuntimeError:
            pass  # user already moved past this step before the check returned
    
    def _select_theme(self, name: str, btn: QPushButton):
        for b in self.theme_btns:
            if b != btn:
                b.setChecked(False)
                set_active(b, False)
        set_active(btn, True)
        self.parent().theme_manager.set_theme(name)
        self.parent().apply_theme(name)
    
    def _go_back(self):
        if self._current_step > 0:
            self._show_step(self._current_step - 1)
    
    def _go_next(self):
        if self._current_step < len(self._steps) - 1:
            self._show_step(self._current_step + 1)
        else:
            self._save_preferences()
            self.accept()
            if self._on_complete:
                self._on_complete()

    def _save_preferences(self):
        """🔧 FIX: this used to be a bare "# Save preferences" comment with no
        code after it - the wizard's own self.wake_combo / self.perm_* widgets
        were never read, so a first-run wake-phrase or permission choice
        silently vanished the moment the dialog closed. This wizard has its
        own separate widget instances from the Settings page's (sp.wake_combo,
        sp.cb_clipboard, ...), so rather than build a second persistence path,
        push the wizard's choices onto the Settings page's equivalent widgets
        - that fires the same change handlers the Settings page itself relies
        on (applying each choice immediately) - then reuse save_settings()'s
        existing JSON-file write instead of duplicating it.
        """
        win = self.parent()
        sp = getattr(win, "settings_panel", None)

        if sp is not None:
            if hasattr(self, "wake_combo") and hasattr(sp, "wake_combo"):
                label = self.wake_combo.currentText()
                if label == "Custom...":
                    # The wizard's own combo never actually captures free text
                    # for "Custom..." (no input field) - ask here, the same
                    # question sp's _on_wake_word_change would ask.
                    text, ok = QInputDialog.getText(
                        self, "Custom Wake Word", "What phrase should wake WAVE up?"
                    )
                    phrases = [text.strip().lower()] if ok and text.strip() else ["hey wave"]
                    phrases = phrases + ["wake up"]
                    win.wake_phrases = phrases
                    if getattr(win, "mic_worker", None):
                        win.mic_worker.set_wake_phrases(phrases)
                    if hasattr(sp, "_refresh_wake_status"):
                        sp._refresh_wake_status()
                elif label in getattr(sp, "WAKE_WORD_PRESETS", {}):
                    sp.wake_combo.setCurrentText(label)  # fires _on_wake_word_change

            if hasattr(self, "perm_clipboard") and hasattr(sp, "cb_clipboard"):
                sp.cb_clipboard.setChecked(self.perm_clipboard.isChecked())
            if hasattr(self, "perm_screen") and hasattr(sp, "cb_screen"):
                sp.cb_screen.setChecked(self.perm_screen.isChecked())
            if hasattr(self, "perm_notifications") and hasattr(sp, "cb_notifications"):
                sp.cb_notifications.setChecked(self.perm_notifications.isChecked())

            # Tier 21: Language step - fires _on_stt_lang_change, same as
            # picking a language on the Settings page would.
            if hasattr(self, "lang_combo") and hasattr(sp, "stt_lang"):
                sp.stt_lang.setCurrentText(self.lang_combo.currentText())

            # Tier 21: Local Voice step - fires _on_local_voice_toggle.
            if hasattr(self, "local_voice_cb") and hasattr(sp, "cb_local_voice"):
                sp.cb_local_voice.setChecked(self.local_voice_cb.isChecked())

            if hasattr(win, "save_settings"):
                win.save_settings()

        # Tier 21: Calendar step - independent of the Settings page; both
        # fields are optional, so a blank name or URL just skips it. Errors
        # (bad name, missing URL) are swallowed here rather than blocking
        # Finish - the user can always retry from the Calendar page.
        if hasattr(self, "cal_name_edit") and hasattr(self, "cal_url_edit"):
            name = self.cal_name_edit.text().strip()
            url = self.cal_url_edit.text().strip()
            if name and url:
                try:
                    from automation.calendar_sync import CalendarSync
                    CalendarSync.add_ics_feed(name, url)
                except Exception:
                    pass
    
    def showEvent(self, event):
        super().showEvent(event)
        if self.parent():
            parent_rect = self.parent().geometry()
            x = parent_rect.x() + (parent_rect.width() - self.width()) // 2
            y = parent_rect.y() + (parent_rect.height() - self.height()) // 2
            self.move(x, y)
        Anim.fade_in(self.findChild(QFrame), 200)


# =============================================================================
# ⚙️ SETTINGS PANEL (with live preview)
# =============================================================================
