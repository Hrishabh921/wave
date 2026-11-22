"""
ui/main_window.py
=============================================================================
MainWindow: the application's top-level QMainWindow - navigation, pages,
wiring of WaveBrain/workers/voice/vision/automation into the UI, and the
NAV_ITEMS / QUICK_APPS / ACTIVITY_PAGE_INDEX constants + get_system_volume()
/ set_system_volume() helpers it uses.

This file used to hold ALL 22 UI classes (5,245 lines). It now holds just
MainWindow (the thing that actually needs all the others wired together);
the rest were split into focused files below ui/ - see ui/common.py for
the shared-imports rationale. Everything that was importable from
`ui.main_window` before (ThemeColors, Icon, AuroraBackdrop, ChatBubble,
SettingsPanel, etc.) is still importable from here via the re-exporting
`from ui.X import *` lines below, so external code / tests do not need to
change their import paths.
"""
from ui.common import *
from ui.theme import *
from ui.icons import *
from ui.effects import *
from ui.widgets_basic import *
from ui.chat_widgets import *
from ui.dashboard_tile import *
from ui.command_palette import *
from ui.onboarding import *
from ui.settings_panel import *
from ui.workflows_panel import WorkflowsPanel
from ui.memory_panel import MemoryPanel
from ui.reminders_panel import RemindersPanel
from ui.notes_todos_panel import NotesTodosPanel
from ui.calendar_panel import CalendarPanel
from ui.skills_panel import SkillsPanel
from ui.performance_panel import PerformancePanel
from ui.toast import *
from ui.worker_pool import *
from voice.tts import CLONE_VOICE_ID


def get_system_volume():
    if not HAS_PYCAW:
        return None
    try:
        comtypes.CoInitialize()
        devices = AudioUtilities.GetSpeakers()
        volume = devices.EndpointVolume
        return round(volume.GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        logger.error(f"get_system_volume error: {e}")
        return None


def set_system_volume(percent):
    if not HAS_PYCAW:
        return False
    try:
        comtypes.CoInitialize()
        devices = AudioUtilities.GetSpeakers()
        volume = devices.EndpointVolume
        volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, percent / 100.0)), None)
        return True
    except Exception as e:
        logger.error(f"set_system_volume error: {e}")
        return False


# =============================================================================
# 🌊 MAIN WINDOW
# =============================================================================
QUICK_APPS = [
    ("Terminal", "cmd", "terminal"),
    ("File Explorer", "file explorer", "folder"),
    ("Chrome", "chrome", "chrome"),
    ("VS Code", "visual studio code", "code"),
    ("Notepad", "notepad", "file-text"),
    ("Task Manager", "task manager", "activity"),
]

NAV_ITEMS = [
    ("Home", "house", 0),
    ("Chat", "message", 1),
    ("System Monitor", "chart-line", 2),
    ("Apps & Power", "bolt", 3),
    ("Music", "music", 4),
    ("Files", "folder", 5),
    ("Vision", "eye", 6),
    ("Clipboard", "clipboard", 7),
    ("Activity", "clock-rotate-left", 8),
    ("Workflows", "diagram-project", 9),  # Tier 3 item 9
    # Tier 21: six new DB-backed management pages, inserted between
    # Workflows and Settings so every existing index above stays unchanged.
    ("Memory", "brain", 10),
    ("Reminders", "bell", 11),
    ("Notes & To-dos", "list-check", 12),
    ("Calendar", "calendar", 13),
    ("Skills", "wand-magic-sparkles", 14),
    ("Performance", "gauge", 15),
    ("Settings", "gear", 16),
]
ACTIVITY_PAGE_INDEX = 8
WORKFLOWS_PAGE_INDEX = 9
MEMORY_PAGE_INDEX = 10
REMINDERS_PAGE_INDEX = 11
NOTES_TODOS_PAGE_INDEX = 12
CALENDAR_PAGE_INDEX = 13
SKILLS_PAGE_INDEX = 14
PERFORMANCE_PAGE_INDEX = 15
SETTINGS_PAGE_INDEX = 16


class _ReplyView:
    """Tier 11: the on-screen side of one streaming reply - its bubble, the
    text shown so far, and whether a throttled re-render is queued."""
    __slots__ = ("bubble", "text", "render_pending", "done")

    def __init__(self, bubble):
        self.bubble = bubble
        self.text = ""
        self.render_pending = False
        self.done = False


class MainWindow(QMainWindow):
    # Phase 4: emitted from worker/watcher threads, handled on the GUI thread
    audit_changed = pyqtSignal()
    skill_event = pyqtSignal(str, str)
    proactive_nudge = pyqtSignal(str, str, str)  # Phase 6: kind, title, message - emitted from ProactiveEngine's
                                                   # own daemon thread (automation/proactive.py), same cross-thread
                                                   # pattern as audit_changed/skill_event above
    thread_health_changed = pyqtSignal()  # Tier 19: thread_supervisor's listener fires this (no payload - the
                                           # slot re-reads thread_supervisor.status() fresh, so it can never drift
                                           # from the real state); same cross-thread pattern as the three above.
    ollama_event = pyqtSignal(str, str)  # Tier 19: event, message - from ollama_lifecycle's own listener,
                                          # same cross-thread pattern (brain/ollama_lifecycle.py's add_listener).
    tool_activity = pyqtSignal(str)  # Tier 21: a short "Wave is doing: X" description, emitted when
                                      # automation/audit.py's ActionScope is entered (its new "started"
                                      # listener event) - same cross-thread pattern as the four above.

    def __init__(self):
        super().__init__()
        
        # Core components
        self.theme_manager = ThemeManager()
        self.theme = self.theme_manager.get_current()
        self.worker_pool = WorkerPool()
        
        # State
        self.brain = WaveBrain()
        # Tier 23: left None until the feature is actually touched (the
        # user ticks "Enable remote dashboard", or load_settings() finds
        # it was on last session) - importing remote.runner is what first
        # tries `import fastapi`/`import uvicorn`, and a default install
        # that never opens the Remote settings page should pay nothing for
        # it, the same promise web/web_ops.py's pywhatkit guard makes.
        self.remote_dashboard = None
        self.mic_active = False
        self.wake_phrases = ["hello wave", "wake up", "hey wave"]
        self.tts_thread = None
        self.mic_worker = None
        self.clip_worker = None
        self.action_worker = None
        self.ocr_worker = None
        self.reminder_worker = None
        # Tier 25: avatar companion - all three stay None until _init_avatar()
        # (called after load_settings(), below) actually builds them; a run
        # with no rig built yet (no photo set up) leaves them None forever,
        # which is exactly the pre-Tier-25 behaviour (dashboard is the only
        # surface, shown minimized - see should_start_hidden()).
        self.avatar_window = None
        self.avatar_state_machine = None
        self.avatar_controller = None
        # Tier 19: crash-recovery bookkeeping for the reminder worker - see
        # _on_reminder_worker_finished.
        self._reminder_worker_restart_count = 0
        self._reminder_worker_gave_up = False
        self._shutting_down = False
        self.last_net_io = psutil.net_io_counters()
        self.focus_mode = False
        self.media_playing = False
        self._media_position = 0.0
        self._media_duration = 0.0
        self._media_seeking = False
        self.app_pill_buttons = {}
        self._active_workers = []
        self._chat_messages = []  # For grouping
        self._last_sender = None
        self._pending_attachment = None
        # Tier 11: the reply currently streaming (an AIStreamWorker), and the
        # time-to-first-audio turn a voice command opened for the message
        # about to be sent (see process_voice_input / send_message).
        self._reply_worker = None
        self._voice_ttfa_turn = None
        # Tier 12: the live caption bubble (see _on_partial_text)
        self._caption_row = None
        self._caption_label = None
        self._caption_seq = 0
        self._caption_closed_at = -1e9
        # Time-to-first-audio: voice/tts.py reports the moment audio starts.
        tts_engine.on_playback_start = ttfa.tracker.on_audio_start
        tts_engine.warm_up()

        # UI refs
        self.backdrop = None
        self.stack = None
        self.nav_buttons = []
        self.toast = ToastManager(self, self.theme)
        
        self.init_ui()
        self.setup_timers()
        self.setup_global_hotkeys()
        
        # Load last theme
        last_theme = self.theme_manager.get_last_theme()
        self.apply_theme(last_theme)
        
        # Start background workers
        self._start_background_workers()

        # Restore saved settings (privacy toggles, notes, volume...) -
        # silent=True so this doesn't pop a toast on every single launch;
        # the manual "Load Settings" command palette action still confirms
        # out loud.
        self.load_settings(silent=True)

        # Tier 25: build the avatar companion (if a rig has been built -
        # see avatar/rig.py) and decide whether the dashboard should start
        # hidden behind it. Must run after load_settings() (reads the same
        # settings file for the avatar_* toggles) and before wave_start.pyw
        # calls should_start_hidden() right after this constructor returns.
        self._init_avatar()
        # Tier 32: the owner's voiceprint (their voice training) before the mic starts.
        from voice.speaker_id import speaker_verifier  # noqa: PLC0415
        speaker_verifier.warm_up()
        if hasattr(self, "settings_panel") and hasattr(self.settings_panel, "_refresh_avatar_build_status"):
            self.settings_panel._refresh_avatar_build_status()

        # Check if first run
        if not os.path.exists(SETTINGS_FILE):
            QTimer.singleShot(500, self._show_onboarding)
        else:
            QTimer.singleShot(1000, self.turn_on_mic)
            QTimer.singleShot(700, self._refresh_dashboard_previews)
    
    def _show_onboarding(self):
        wizard = OnboardingWizard(self, self.theme, self._on_onboarding_complete)
        wizard.exec()
    
    def _on_onboarding_complete(self):
        self.turn_on_mic()
        QTimer.singleShot(700, self._refresh_dashboard_previews)
        self.toast.show("Welcome to WAVE! Press Ctrl+Shift+Space to toggle mic.", "success")
    
    def apply_theme(self, name: str):
        theme = self.theme_manager.get_theme(name)
        if not theme:
            return
        self.theme = theme
        self.setStyleSheet(self.theme_manager.build_stylesheet(theme))
        # Update backdrop
        if self.backdrop:
            self.backdrop.set_theme(theme)
        # Update toast
        self.toast._theme = theme
        # Update all theme-aware widgets
        self._update_all_theme_aware()
    
    def _update_all_theme_aware(self):
        """Call update on all widgets that need theme refresh"""
        # This would be expanded to refresh all custom widgets
        pass
    
    def init_ui(self):
        self.setWindowTitle(f"{config.APP_NAME} — Desktop Assistant")
        self.setStyleSheet(self.theme_manager.build_stylesheet(self.theme))

        default_w, default_h = 1520, 940
        screen = QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(default_w, int(avail.width() * 0.94))
            h = min(default_h, int(avail.height() * 0.90))
            self.resize(w, h)
            frame = self.frameGeometry()
            frame.moveCenter(avail.center())
            self.move(frame.topLeft())
        else:
            self.resize(default_w, default_h)

        root_widget = AuroraBackdrop(self.theme)
        self.backdrop = root_widget
        self.backdrop.set_blur_radius(25.0)
        self.setCentralWidget(root_widget)
        root = QHBoxLayout(root_widget)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_sidebar())

        self.stack = SlidingStack()
        self.stack.on_index_changed = self._sync_nav_active
        QApplication.instance().installEventFilter(self.stack)
        
        # Add pages
        self.stack.add_page(self._build_home_page())
        self.stack.add_page(self._build_chat_page())
        self.stack.add_page(wrap_scroll(self._build_system_monitor_page(), self.theme))
        self.stack.add_page(wrap_scroll(self._build_apps_power_page(), self.theme))
        self.stack.add_page(wrap_scroll(self._build_music_page(), self.theme))
        self.stack.add_page(self._build_files_page())
        self.stack.add_page(wrap_scroll(self._build_vision_page(), self.theme))
        self.stack.add_page(wrap_scroll(self._build_clipboard_page(), self.theme))
        self._activity_page = self._build_activity_page()
        self.stack.add_page(wrap_scroll(self._activity_page, self.theme))
        self.workflows_panel = self._build_workflows_page()
        self.stack.add_page(self.workflows_panel)
        # Tier 21: six new DB-backed management pages (see NAV_ITEMS above
        # for their indices) - inserted here, between Workflows and
        # Settings, so Settings stays last.
        self.memory_panel = self._build_memory_page()
        self.stack.add_page(self.memory_panel)
        self.reminders_panel = self._build_reminders_page()
        self.stack.add_page(self.reminders_panel)
        self.notes_todos_panel = self._build_notes_todos_page()
        self.stack.add_page(self.notes_todos_panel)
        self.calendar_panel = self._build_calendar_page()
        self.stack.add_page(self.calendar_panel)
        self.skills_panel = self._build_skills_page()
        self.stack.add_page(self.skills_panel)
        self.performance_panel = self._build_performance_page()
        self.stack.add_page(self.performance_panel)
        self.settings_panel = self._build_settings_page()
        self.stack.add_page(wrap_scroll(self.settings_panel, self.theme))
        
        root.addWidget(self.stack, 1)

        self.nav_buttons[0].setChecked(True)
        self._load_chat_history()

        # Phase 4: live Activity page + hot-reload skills
        self.audit_changed.connect(self._on_audit_event)
        audit_mod.audit.add_listener(lambda event, rec: self.audit_changed.emit())
        self.skill_event.connect(self._on_skill_event)
        QTimer.singleShot(1500, self._start_skill_watcher)

        # Tier 21: live activity banner on the chat page - "Wave is doing:
        # X" for whatever tool call is currently running. A second listener
        # on the SAME audit feed as just above (add_listener appends, it
        # doesn't replace), filtered to only the new "started" event (see
        # automation/audit.py's ActionScope.__enter__ and add_listener's own
        # docstring for why there's no matching "finished" event to pair it
        # with - _on_tool_activity below auto-hides on a short timer instead).
        self.tool_activity.connect(self._on_tool_activity)
        audit_mod.audit.add_listener(self._on_audit_activity_event)

        # Phase 6: proactive engine - battery-low / upcoming-calendar-event
        # nudges Wave gives unprompted. Same cross-thread wiring as audit_changed
        # above: the engine's own daemon thread calls the lambda, which only
        # ever does a thread-safe .emit() - _on_proactive_nudge (connected to
        # the signal, so it runs on the GUI thread) does the actual UI work.
        self.proactive_nudge.connect(self._on_proactive_nudge)
        proactive_engine.add_listener(
            lambda nudge: self.proactive_nudge.emit(nudge.kind, nudge.title, nudge.message))

        # Tier 18: lets ProactiveEngine's own background thread trigger a
        # scheduled workflow run WITHOUT this Qt-free module ever touching
        # Qt or WaveBrain directly - see automation/proactive.py's
        # set_workflow_runner() docstring. self._run_scheduled_workflow
        # below hands off to self.worker_pool, which (like Qt's thread
        # pool machinery generally) is safe to submit to from any thread.
        proactive_engine.set_workflow_runner(self._run_scheduled_workflow)

        # Tier 19: thread_supervisor's health dot (sidebar) and
        # ollama_lifecycle's own toasts - same cross-thread wiring as
        # audit_changed/proactive_nudge above. thread_health_changed carries
        # no payload on purpose (see its own declaration): the slot just
        # re-reads thread_supervisor.status() fresh every time, so it can
        # never show something stale or drift from the real state.
        self.thread_health_changed.connect(self._refresh_health_dot)
        thread_supervisor.add_listener(lambda name, event: self.thread_health_changed.emit())
        self.ollama_event.connect(self._on_ollama_event)
        ollama_lifecycle.add_listener(lambda event, message: self.ollama_event.emit(event, message))

    def _start_background_workers(self):
        # Weather
        self.weather_worker = WeatherWorker()
        self.weather_worker.weather_ready.connect(self.update_weather)
        self.weather_worker.start()
        
        # Clipboard
        if HAS_PYPERCLIP:
            self.clip_worker = ClipboardWorker()
            self.clip_worker.clip_changed.connect(self.on_clip_changed)
            self.clip_worker.start()
        
        # Media info
        self.media_info_worker = MediaInfoWorker()
        self.media_info_worker.info_ready.connect(self.update_now_playing)
        self.media_info_worker.start()

        # Reminders / alarms - see _start_reminder_worker() (Tier 19 factored
        # the construction out so a crashed worker can be recreated the
        # same way - see _on_reminder_worker_finished).
        self._start_reminder_worker()

        # Phase 6: proactive engine - a plain daemon thread (not a QThread;
        # see automation/proactive.py), started here alongside the other
        # background workers and stopped in closeEvent the same way.
        proactive_engine.start()

        # Tier 2 item #6: memory consolidation - same plain-daemon-thread
        # shape as proactive_engine just above (see
        # brain/memory_consolidation.py), started/stopped alongside it.
        memory_consolidator.start()

        # Tier 19: watch all three raw-daemon-thread singletons above (plus
        # the skill watcher, once _start_skill_watcher has actually created
        # it - see there) so a thread that silently dies (a BaseException
        # escaping its tick loop - see automation/thread_supervisor.py's
        # own docstring) gets restarted instead of just staying dead for
        # the rest of the session.
        thread_supervisor.watch("proactive_engine", proactive_engine)
        thread_supervisor.watch("memory_consolidator", memory_consolidator)
        # Tier 19: the action log's background DB writer (DbStore only -
        # MemoryStore has nothing to watch, hence the None check) - see
        # automation/audit.py's Audit.store_for_supervision.
        db_writer_store = audit_mod.audit.store_for_supervision
        if db_writer_store is not None:
            thread_supervisor.watch("audit_db_writer", db_writer_store)
        thread_supervisor.start()

        # Tier 19: Ollama lifecycle (start `ollama serve` + pull its model,
        # only when Ollama is actually relevant - see
        # brain/ollama_lifecycle.py's _is_relevant()) runs on its own
        # daemon thread (ensure_ready()'s default wait=False) so a
        # multi-GB first-time model pull never holds up startup.
        ollama_lifecycle.ensure_ready()

        # Tier 19: startup health check (mic, speaker, voice models, LLM
        # provider, Tesseract) - via worker_pool, never the GUI thread,
        # since it opens real audio devices and makes a real network call.
        # "Reported loudly in the UI": every non-OK result becomes its own
        # toast in _on_health_check_done, not just a log line.
        health_worker = self.worker_pool.submit(run_health_check)
        health_worker.signals.result.connect(self._on_health_check_done)

    def _start_reminder_worker(self):
        """Builds, wires, and starts a fresh ReminderWorker. Tier 19 pulled
        this out of _start_background_workers() so _on_reminder_worker_finished
        (below) can call it again to replace a crashed worker - a QThread
        can't just be start()-ed again once its run() has returned (and
        even if it could, its signals would still need reconnecting), so
        the correct fix is the same one automation/thread_supervisor.py's
        three singletons already use internally for their own
        threading.Thread: build a brand new one."""
        self.reminder_worker = ReminderWorker()
        self.reminder_worker.reminder_due.connect(self.on_reminder_due)
        # 🔧 Tier 3 / item 8: two new signals alongside the original
        # reminder_due - see ReminderWorker's docstring in ui/workers.py.
        self.reminder_worker.reminder_upcoming.connect(self.on_reminder_upcoming)
        # 🔧 Tier 18: reminder_followup moved to automation/proactive.py's
        # _check_reminder_followups() - see ReminderWorker's own docstring.
        # Tier 19: QThread's own finished signal - emitted when run()
        # returns for ANY reason, deliberate stop() or an unhandled crash
        # - see _on_reminder_worker_finished for how it tells them apart.
        self.reminder_worker.finished.connect(self._on_reminder_worker_finished)
        self.reminder_worker.start()
        if self._reminder_worker_restart_count > 0:
            # Tier 19: same "stayed up long enough, stop counting this as
            # part of the earlier crash loop" idea as
            # automation/thread_supervisor.py's own healthy-reset, just
            # done with a Qt timer here instead of a polling thread - this
            # worker has no separate supervisor ticking it.
            worker_ref = self.reminder_worker
            QTimer.singleShot(int(config.THREAD_SUPERVISOR_HEALTHY_RESET_S * 1000),
                               lambda: self._reset_reminder_worker_restart_count(worker_ref))

    def _reset_reminder_worker_restart_count(self, worker_ref):
        if self.reminder_worker is worker_ref:  # still the same instance - stayed alive the whole time
            self._reminder_worker_restart_count = 0

    def _on_reminder_worker_finished(self):
        """Tier 19: see _start_reminder_worker's docstring. finished fires
        whenever run() returns, for ANY reason - distinguish a deliberate
        stop() (ReminderWorker.is_running was already flipped False before
        the thread actually exited) from an unhandled crash (is_running is
        still True - nothing told it to stop, it just died, the same
        Tier-5-precedent BaseException-escape risk
        automation/thread_supervisor.py's docstring explains for the other
        three background singletons) - only the second case restarts it."""
        if self._shutting_down or self._reminder_worker_gave_up:
            return
        worker = self.reminder_worker
        if worker is not None and not worker.is_running:
            return  # a deliberate stop() - not a crash
        if self._reminder_worker_restart_count >= config.THREAD_SUPERVISOR_MAX_RESTARTS:
            self._reminder_worker_gave_up = True
            logger.error("ReminderWorker crashed and hit its restart cap - "
                         "reminders won't fire again until Wave is restarted.")
            return
        backoff_ms = int(1000 * min(
            config.THREAD_SUPERVISOR_MAX_BACKOFF_S,
            config.THREAD_SUPERVISOR_BASE_BACKOFF_S * (2 ** self._reminder_worker_restart_count)))
        self._reminder_worker_restart_count += 1
        logger.warning(f"ReminderWorker is no longer running - restarting in {backoff_ms}ms "
                        f"(attempt {self._reminder_worker_restart_count}/{config.THREAD_SUPERVISOR_MAX_RESTARTS}).")
        QTimer.singleShot(backoff_ms, self._start_reminder_worker)

    def setup_global_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        try:
            keyboard.add_hotkey('ctrl+shift+space', self._global_mic_toggle)
            keyboard.add_hotkey('ctrl+k', self._global_command_palette)
            keyboard.add_hotkey('ctrl+alt+w', self._global_toggle_window)
            keyboard.add_hotkey('ctrl+alt+x', self._global_emergency_stop)
        except Exception as e:
            logger.warning(f"Global hotkeys not available: {e}")
    
    def _global_mic_toggle(self):
        # Must run on main thread
        QTimer.singleShot(0, self.toggle_mic)
    
    def _global_command_palette(self):
        QTimer.singleShot(0, self.show_command_palette)
    
    def _global_toggle_window(self):
        QTimer.singleShot(0, self._toggle_window_visibility)

    def _global_emergency_stop(self):
        # Must run on main thread - keyboard's hotkey callback fires on its
        # own listener thread, and every object touched below (TTS, workers,
        # the mic, the UI) belongs to the main/GUI thread.
        QTimer.singleShot(0, self.emergency_stop)

    # Tier 19: how long the reply worker's own cooperative cancel_event
    # gets to actually unwind it (close the HTTP stream, save the partial
    # reply as interrupted, record the trace row - see think_stream()'s
    # _TurnCancelled/GeneratorExit handling) before emergency_stop()'s
    # hard-terminate loop below potentially kills it mid-cleanup instead.
    # Short enough that a panic button still feels instant.
    EMERGENCY_STOP_GRACE_MS = 300

    def emergency_stop(self):
        """Panic button (Ctrl+Alt+X): immediately halts whatever Wave is
        currently doing. Stops speech, forcibly terminates any in-flight AI
        response/tool-dispatch and any running action worker, and drops the
        mic to SLEEPING so trailing audio from whatever was happening can't
        immediately trigger another command. Does NOT touch the always-on
        background pollers (weather/clipboard/media/reminders) - those
        aren't "doing" anything the user needs to interrupt.

        Worker termination here mirrors WorkerPool.shutdown() (used today on
        app close) rather than inventing a gentler mechanism: QThread.terminate()
        is already this codebase's accepted way to hard-kill a worker, so
        emergency stop reuses it instead of introducing a second, inconsistent
        shutdown path.

        Tier 19: that hard-terminate loop is kept exactly as it was - it's
        still the only backstop for a worker that never checks a
        cancel_event at all (a stuck direct action) or doesn't exit within
        its grace window. What's new is the loop just above it: the reply
        worker (if any) was already asked to stop cooperatively by
        _cancel_reply() two lines up, but until now terminate() always
        followed that ask on the very next statement - the same thread
        never got a realistic chance to notice and unwind itself before
        being killed outright. Giving it EMERGENCY_STOP_GRACE_MS first
        means the common case (an LLM round between checks, a tool that
        already returned) now actually exits cleanly instead of being
        cut off mid-cleanup; anything still alive after that falls straight
        through to the unchanged hard-terminate loop below, same as before
        this existed."""
        self._cancel_reply()
        self.stop_speaking()

        # Tier 19: give whatever was JUST cooperatively cancelled above a
        # short window to actually finish on its own. A worker that was
        # never asked to cancel (nothing in _active_workers but the reply
        # worker ever is today) has cancelled == False and is skipped here
        # at zero cost, falling straight to the hard-terminate loop below
        # exactly as before.
        for worker in list(self._active_workers):
            if getattr(worker, "cancelled", False):
                try:
                    worker.wait(self.EMERGENCY_STOP_GRACE_MS)
                except Exception as e:
                    logger.warning(f"Emergency stop: error waiting on a cancelled worker: {e}")

        # Any in-flight brain.think() call (AIWorker) - see send_message().
        for worker in list(self._active_workers):
            try:
                worker.terminate()
                worker.wait(1000)
            except Exception as e:
                logger.warning(f"Emergency stop: couldn't terminate an AI worker: {e}")
        self._active_workers.clear()

        # Any in-flight direct action (open app, OCR, screenshot, etc.) - see _run_action().
        self.worker_pool.shutdown()

        # Back to a safe idle state rather than mid-command.
        if self.mic_worker:
            self.mic_worker.set_mode("SLEEPING")

        logger.info("Emergency stop triggered (Ctrl+Alt+X) - all in-flight actions halted.")
        self.toast.show("Emergency stop - all actions halted.", "error")
        self.add_chat_bubble("🛑 Emergency stop — all actions halted.", is_user=False)
    
    def _toggle_window_visibility(self):
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.activateWindow()
            self.raise_()
    
    def show_command_palette(self):
        # Build action map
        actions = {
            "New Chat": lambda: self.stack.slide_to(1),
            "System Monitor": lambda: self.stack.slide_to(2),
            "Open App...": lambda: self.stack.slide_to(3),
            "Music Controls": lambda: self.stack.slide_to(4),
            "File Browser": lambda: self.stack.slide_to(5),
            "Screen OCR": lambda: self.run_ocr_capture(),
            "Clipboard History": lambda: self.stack.slide_to(7),
            "Activity & Undo": lambda: self.stack.slide_to(ACTIVITY_PAGE_INDEX),
            "Workflows": lambda: self.stack.slide_to(WORKFLOWS_PAGE_INDEX),
            "Memory": lambda: self.stack.slide_to(MEMORY_PAGE_INDEX),
            "Reminders": lambda: self.stack.slide_to(REMINDERS_PAGE_INDEX),
            "Notes & To-dos": lambda: self.stack.slide_to(NOTES_TODOS_PAGE_INDEX),
            "Calendar": lambda: self.stack.slide_to(CALENDAR_PAGE_INDEX),
            "Skills": lambda: self.stack.slide_to(SKILLS_PAGE_INDEX),
            "Performance": lambda: self.stack.slide_to(PERFORMANCE_PAGE_INDEX),
            "Settings": lambda: self.stack.slide_to(SETTINGS_PAGE_INDEX),
            "Toggle Mic": self.toggle_mic,
            "Clear Chat": self.clear_chat,
            "Take Screenshot": lambda: self._run_action(AppController.take_screenshot),
            "Lock PC": lambda: AppController.lock_pc(),
            "Focus Mode": self.toggle_focus,
            "Save Settings": self.save_settings,
            "Load Settings": self.load_settings,
        }
        # Add quick apps
        for name, target, _ in QUICK_APPS:
            actions[f"Open {name}"] = lambda t=target: self._run_action(AppController.open_app, t)
        
        palette = CommandPalette(self, self.theme, actions)
        palette.exec()
    
    def _load_chat_history(self):
        self.add_chat_bubble(
            f"{self._greeting_text()}! I'm W.A.V.E., your AI assistant. Systems are online and ready to help.",
            is_user=False
        )
    
    def _greeting_text(self):
        hour = time.localtime().tm_hour
        if hour < 12:
            return "Good Morning"
        elif hour < 18:
            return "Good Afternoon"
        return "Good Evening"
    
    @staticmethod
    def _get_local_ip():
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "Unavailable"
    
    def _track_worker(self, worker):
        self._active_workers.append(worker)
        worker.finished.connect(lambda: self._untrack_worker(worker))
        return worker
    
    def _untrack_worker(self, worker):
        if worker in self._active_workers:
            self._active_workers.remove(worker)
    
    def _run_action(self, func, *args, announce=True):
        worker = self.worker_pool.submit(func, *args)
        if announce:
            worker.signals.result.connect(self.display_ai_response)
        worker.signals.error.connect(lambda e: self.toast.show(f"Error: {e}", "error"))
        return worker
    
    # ==================== SIDEBAR ====================
    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(210)
        v = QVBoxLayout(sidebar)
        v.setContentsMargins(16, 22, 16, 18)
        v.setSpacing(4)

        icon = BrandIcon(self.theme, size=38)
        v.addWidget(icon)
        v.addSpacing(8)
        lbl_word = QLabel("W . A . V . E .")
        lbl_word.setProperty("class", "Wordmark")
        v.addWidget(lbl_word)
        lbl_tag = QLabel("DESKTOP ASSISTANT")
        lbl_tag.setProperty("class", "Tagline")
        v.addWidget(lbl_tag)
        v.addSpacing(20)

        self.nav_buttons = []
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for i, (label, icon_name, _) in enumerate(NAV_ITEMS):
            btn = QPushButton(f"  {label}")
            btn.setProperty("class", "NavItem")
            btn.setCheckable(True)
            btn.setFixedHeight(44)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(label)
            Icon.set_btn_icon(btn, icon_name, self.theme.text_muted, 20)
            btn.setIconSize(QSize(20, 20))
            btn.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
            self.nav_group.addButton(btn, i)
            v.addWidget(btn)
            self.nav_buttons.append(btn)
        self.nav_group.idClicked.connect(self.on_nav_changed)

        v.addStretch()
        v.addWidget(make_divider(self.theme))
        v.addSpacing(10)
        self.mic_mini_lbl = QLabel("●  STANDBY")
        self.mic_mini_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 10.5px; font-weight: 700; background: transparent;")
        v.addWidget(self.mic_mini_lbl)

        # Tier 19: thread_supervisor's own health dot - see
        # _refresh_health_dot() for what makes it change color. Starts
        # exactly like mic_mini_lbl above (neutral dot, "OK") since nothing
        # has been watched yet this early in __init__.
        self.health_dot_lbl = QLabel("●  SYSTEMS OK")
        self.health_dot_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 10.5px; font-weight: 700; background: transparent;")
        self.health_dot_lbl.setToolTip("All background workers running normally.")
        v.addWidget(self.health_dot_lbl)

        # Stagger animation on show
        QTimer.singleShot(100, lambda: Anim.stagger_show(self.nav_buttons, 40, 200))
        
        return sidebar

    def on_nav_changed(self, idx):
        self.stack.slide_to(idx)

    def _sync_nav_active(self, idx):
        if 0 <= idx < len(self.nav_buttons):
            self.nav_buttons[idx].setChecked(True)
        if idx == 0:
            QTimer.singleShot(80, self._refresh_dashboard_previews)
        if idx == ACTIVITY_PAGE_INDEX:
            QTimer.singleShot(0, self._refresh_activity_page)
    
    def _make_back_row(self, title, subtitle=""):
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(12)
        btn_back = QPushButton("←")
        btn_back.setProperty("class", "IconBtn")
        btn_back.setFixedSize(34, 34)
        btn_back.setToolTip("Back to Home (Esc)")
        btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_back.clicked.connect(lambda: self.stack.slide_to(0))
        Icon.set_btn_icon(btn_back, "chevron-left", self.theme.text_muted, 18)
        title_lbl = QLabel(title)
        title_lbl.setProperty("class", "PageTitle")
        row.addWidget(btn_back)
        row.addWidget(title_lbl)
        row.addStretch()
        v.addLayout(row)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setProperty("class", "PageSubtitle")
            v.addWidget(sub)
        return wrap
    
    # ==================== HOME PAGE ====================
    def _build_home_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        root = QVBoxLayout(page)
        root.setContentsMargins(20, 20, 20, 20)
        root.addWidget(wrap_scroll(self._build_home_dashboard(), self.theme))
        return page
    
    def _build_home_dashboard(self):
        wrap = QWidget()
        v = QVBoxLayout(wrap)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(16)
        
        v.addWidget(make_section_heading("QUICK ACCESS", self.theme))
        
        grid = QGridLayout()
        grid.setSpacing(14)
        cols = 4
        self._dashboard_tiles = []
        for i, (label, icon_name, idx) in enumerate(NAV_ITEMS[1:]):
            target_idx = idx
            tile = DashboardTile(ICON_MAP.get(icon_name, "⬜"), label, self.theme)
            tile.clicked.connect(lambda idx=target_idx: self.stack.slide_to(idx))
            row, col = divmod(i, cols)
            grid.addWidget(tile, row, col)
            self._dashboard_tiles.append((tile, target_idx))
        v.addLayout(grid)
        
        v.addStretch()
        return wrap
    
    def _make_stat_card(self, title, value, icon_name, color):
        card = QFrame()
        card.setProperty("class", "Panel")
        card.setFixedHeight(90)
        add_shadow(card, blur=16, y_offset=4)
        cl = QHBoxLayout(card)
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(12)
        
        icon_box = QFrame()
        icon_box.setFixedSize(44, 44)
        icon_box.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {color}40, stop:1 {color}10);
            border-radius: 12px; border: 1px solid {color}40;
        """)
        ibl = QHBoxLayout(icon_box)
        ibl.setContentsMargins(0, 0, 0, 0)
        ilbl = QLabel()
        Icon.set_btn_icon(ilbl, icon_name, color, 22)
        ibl.addWidget(ilbl, 0, Qt.AlignmentFlag.AlignCenter)
        cl.addWidget(icon_box)
        
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        val_lbl = QLabel(value)
        val_lbl.setStyleSheet(f"color: {self.theme.text_main}; font-size: 18px; font-weight: 700; background: transparent;")
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 11px; background: transparent;")
        text_col.addWidget(val_lbl)
        text_col.addWidget(title_lbl)
        cl.addLayout(text_col)
        cl.addStretch()
        
        return card
    
    def _refresh_dashboard_previews(self):
        if not hasattr(self, "_dashboard_tiles"):
            return
        for tile, idx in self._dashboard_tiles:
            if not (0 <= idx < len(self.stack.pages)):
                continue
            page_widget = self.stack.pages[idx]
            size = page_widget.size()
            if size.width() <= 1 or size.height() <= 1:
                continue
            pixmap = QPixmap(size)
            pixmap.fill(Qt.GlobalColor.transparent)
            page_widget.render(pixmap)
            tile.set_preview(pixmap)
    
    def _build_chat_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(14)
        v.addWidget(self._make_back_row("Chat", "Talk to W.A.V.E., your desktop assistant"))
        v.addWidget(self._build_chat_column(), 1)
        return page
    
    # ==================== CHAT COLUMN ====================
    def _build_chat_column(self):
        page = QFrame()
        page.setProperty("class", "Panel")
        add_shadow(page)
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("ChatHeader")
        header.setFixedHeight(68)
        h = QHBoxLayout(header)
        h.setContentsMargins(22, 10, 18, 10)
        h.setSpacing(12)

        avatar = QLabel("W")
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(f"""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {self.theme.accent_1}, stop:1 {self.theme.accent_2});
            color: #FFFFFF; border-radius: 20px; font-size: 16px; font-weight: 800;
        """)

        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        name_lbl = QLabel("W.A.V.E. Assistant")
        name_lbl.setProperty("class", "ContactName")
        self.status_lbl = QLabel()
        self.status_lbl.setProperty("class", "StatusMessage")
        name_col.addWidget(name_lbl)
        name_col.addWidget(self.status_lbl)
        self._set_status("Online", self.theme.success)

        btn_clear = QPushButton()
        btn_clear.setProperty("class", "IconBtn")
        btn_clear.setToolTip("Clear chat (Ctrl+L)")
        btn_clear.setFixedSize(34, 34)
        btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(btn_clear, "trash", self.theme.text_muted, 18)
        btn_clear.clicked.connect(self.clear_chat)

        h.addWidget(avatar)
        h.addLayout(name_col)
        h.addStretch()
        h.addWidget(btn_clear)
        v.addWidget(header)

        # Tier 21: live activity banner - "⚙ Doing: X" while a tool call is
        # running (see tool_activity signal / _on_tool_activity). Hidden by
        # default; this page already has a live transcript (captions) and a
        # real waveform (self.visualizer below) from earlier tiers - this is
        # the one piece that was still missing: visibility into WHAT Wave is
        # currently doing to the system, not just that it's listening/typing.
        self.activity_banner = QLabel("")
        self.activity_banner.setObjectName("LiveActivityBanner")
        self.activity_banner.setVisible(False)
        self.activity_banner.setStyleSheet(f"""
            background: {self.theme.accent_1}18; color: {self.theme.accent_1};
            padding: 6px 22px; font-size: 11px; font-weight: 700;
        """)
        v.addWidget(self.activity_banner)

        self.chat_scroll = QScrollArea()
        self.chat_scroll.setWidgetResizable(True)
        self.chat_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self.chat_content = QWidget()
        self.chat_content.setStyleSheet("background: transparent;")
        self.chat_scroll.viewport().setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_content)
        self.chat_layout.setContentsMargins(22, 16, 22, 16)
        self.chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.chat_layout.setSpacing(8)
        self.chat_scroll.setWidget(self.chat_content)
        v.addWidget(self.chat_scroll, 1)

        input_bar = QWidget()
        input_bar.setObjectName("ChatInputBar")
        ib = QVBoxLayout(input_bar)
        ib.setContentsMargins(22, 10, 22, 14)
        ib.setSpacing(8)

        self.visualizer = AudioVisualizer(self.theme)
        ib.addWidget(self.visualizer)

        # Pending-attachment preview chip - hidden until a file is picked
        self.attach_chip = QWidget()
        self.attach_chip.setVisible(False)
        self.attach_chip.setStyleSheet(f"""
            background-color: {self.theme.bg_surface};
            border: 1px solid {self.theme.border};
            border-radius: 12px;
        """)
        chip_row = QHBoxLayout(self.attach_chip)
        chip_row.setContentsMargins(10, 6, 10, 6)
        chip_row.setSpacing(10)
        self.attach_chip_thumb = QLabel()
        self.attach_chip_thumb.setFixedSize(36, 36)
        self.attach_chip_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.attach_chip_thumb.setStyleSheet("background: transparent; font-size: 20px;")
        name_meta_col = QVBoxLayout()
        name_meta_col.setSpacing(1)
        self.attach_chip_name = QLabel()
        self.attach_chip_name.setStyleSheet(f"background: transparent; color: {self.theme.text_main}; font-size: 12px; font-weight: 700;")
        self.attach_chip_meta = QLabel()
        self.attach_chip_meta.setStyleSheet(f"background: transparent; color: {self.theme.text_faint}; font-size: 10.5px;")
        name_meta_col.addWidget(self.attach_chip_name)
        name_meta_col.addWidget(self.attach_chip_meta)
        btn_remove_attach = QPushButton()
        btn_remove_attach.setProperty("class", "IconBtn")
        btn_remove_attach.setFixedSize(26, 26)
        btn_remove_attach.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_remove_attach.setToolTip("Remove attachment")
        Icon.set_btn_icon(btn_remove_attach, "xmark", self.theme.text_muted, 14)
        btn_remove_attach.clicked.connect(self._clear_attachment)
        chip_row.addWidget(self.attach_chip_thumb)
        chip_row.addLayout(name_meta_col, 1)
        chip_row.addWidget(btn_remove_attach)
        ib.addWidget(self.attach_chip)

        input_row = QHBoxLayout()
        input_row.setSpacing(10)

        self.btn_attach = QPushButton()
        self.btn_attach.setObjectName("MicInline")
        self.btn_attach.setToolTip("Attach an image or document")
        self.btn_attach.setFixedSize(42, 42)
        self.btn_attach.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(self.btn_attach, "paperclip", self.theme.accent_1, 18)
        self.btn_attach.clicked.connect(self._pick_attachment)

        self.btn_mic_inline = QPushButton()
        self.btn_mic_inline.setObjectName("MicInline")
        self.btn_mic_inline.setToolTip("Toggle voice listening (Ctrl+Shift+Space)")
        self.btn_mic_inline.setFixedSize(42, 42)
        self.btn_mic_inline.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(self.btn_mic_inline, "mic", self.theme.accent_1, 20)
        self.btn_mic_inline.clicked.connect(self.toggle_mic)

        self.chat_input = ChatInputBox()
        self.chat_input.send_requested.connect(self.send_message)

        self.btn_send = QPushButton()
        self.btn_send.setObjectName("SendBtn")
        self.btn_send.setToolTip("Send (Enter)")
        self.btn_send.setFixedSize(42, 42)
        self.btn_send.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(self.btn_send, "send", "#000000", 20)
        # Tier 19: doubles as a Stop button while a reply is streaming -
        # see _set_send_button_stop_mode()/_on_send_btn_clicked().
        self.btn_send.clicked.connect(self._on_send_btn_clicked)

        input_row.addWidget(self.btn_attach, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.btn_mic_inline, 0, Qt.AlignmentFlag.AlignBottom)
        input_row.addWidget(self.chat_input)
        input_row.addWidget(self.btn_send, 0, Qt.AlignmentFlag.AlignBottom)
        ib.addLayout(input_row)
        v.addWidget(input_bar)

        # Context menu for chat area
        self.chat_content.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.chat_content.customContextMenuRequested.connect(self._show_chat_context_menu)

        return page

    # ==================== ATTACHMENTS ====================
    def _pick_attachment(self):
        filters = (
            "Attach a file (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.pdf *.txt *.md *.csv "
            "*.json *.log *.py *.js *.ts *.jsx *.tsx *.html *.css *.xml *.yaml *.yml *.java *.c *.cpp);;"
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;"
            "Documents (*.pdf *.txt *.md *.csv *.json *.log *.py *.js *.ts *.jsx *.tsx *.html *.css *.xml *.yaml *.yml *.java *.c *.cpp);;"
            "All Files (*)"
        )
        path, _ = QFileDialog.getOpenFileName(self, "Attach a file", "", filters)
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        image_exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
        name = os.path.basename(path)
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            size_bytes = 0

        if ext in image_exts:
            pix = QPixmap(path)
            if pix.isNull():
                self.toast.show("Couldn't open that image", "error")
                return
            self._pending_attachment = {
                "kind": "image", "path": path, "name": name,
                "size_bytes": size_bytes, "text_content": None, "truncated": False,
            }
        elif ext == ".pdf":
            if not HAS_PYPDF:
                self.toast.show("PDF support needs: pip install pypdf", "warning")
                return
            try:
                reader = PdfReader(path)
                chunks = [(pg.extract_text() or "") for pg in reader.pages[:40]]
                text_content = "\n".join(chunks).strip()
            except Exception as e:
                self.toast.show(f"Couldn't read that PDF: {e}", "error")
                return
            if not text_content:
                self.toast.show("No readable text found in that PDF (it may be scanned).", "warning")
                return
            truncated = len(text_content) > 12000
            self._pending_attachment = {
                "kind": "document", "path": path, "name": name,
                "size_bytes": size_bytes,
                "text_content": text_content[:12000], "truncated": truncated,
            }
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text_content = f.read()
            except (UnicodeDecodeError, OSError):
                self.toast.show("That file type isn't supported yet - try an image, PDF, or text file.", "warning")
                return
            truncated = len(text_content) > 12000
            self._pending_attachment = {
                "kind": "document", "path": path, "name": name,
                "size_bytes": size_bytes,
                "text_content": text_content[:12000], "truncated": truncated,
            }

        self._show_attach_preview()

    def _show_attach_preview(self):
        att = self._pending_attachment
        if not att:
            self.attach_chip.setVisible(False)
            return

        if att["kind"] == "image":
            pix = QPixmap(att["path"])
            thumb = pix.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
            self.attach_chip_thumb.setPixmap(thumb)
            self.attach_chip_thumb.setText("")
        else:
            self.attach_chip_thumb.setPixmap(QPixmap())
            self.attach_chip_thumb.setText("📄")

        kb = att["size_bytes"] / 1024
        size_label = f"{kb / 1024:.1f} MB" if kb > 1024 else f"{kb:.0f} KB"
        self.attach_chip_name.setText(att["name"])
        note = " (truncated)" if att.get("truncated") else ""
        self.attach_chip_meta.setText(f"{size_label}{note} - will be sent with your message")
        self.attach_chip.setVisible(True)

    def _clear_attachment(self):
        self._pending_attachment = None
        self.attach_chip.setVisible(False)
    
    def _show_chat_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background: {self.theme.bg_panel}; border: 1px solid {self.theme.border}; border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 8px 24px; color: {self.theme.text_main}; border-radius: 4px; }}
            QMenu::item:selected {{ background: {self.theme.accent_1}; color: #000000; }}
        """)
        menu.addAction("Clear Chat", self.clear_chat)
        menu.addAction("Copy Last AI Response", self._copy_last_ai)
        menu.addAction("Export Chat", self._export_chat)
        menu.exec(self.chat_content.mapToGlobal(pos))
    
    def _copy_last_ai(self):
        if not HAS_PYPERCLIP:
            self.toast.show("pyperclip not installed", "warning")
            return
        for i in range(self.chat_layout.count() - 1, -1, -1):
            item = self.chat_layout.itemAt(i)
            if item and item.widget():
                bubble = item.widget().findChild(ChatBubble)
                if bubble and not bubble.is_user:
                    pyperclip.copy(bubble.text_browser.toPlainText())
                    self.toast.show("Copied last AI response", "success")
                    return
    
    def _export_chat(self):
        # Export chat to file
        path, _ = QFileDialog.getSaveFileName(self, "Export Chat", "wave_chat.txt", "Text Files (*.txt)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                for i in range(self.chat_layout.count()):
                    item = self.chat_layout.itemAt(i)
                    if item and item.widget():
                        bubble = item.widget().findChild(ChatBubble)
                        if bubble:
                            prefix = "You: " if bubble.is_user else "WAVE: "
                            f.write(prefix + bubble.text_browser.toPlainText() + "\n\n")
            self.toast.show("Chat exported", "success")
    
    def _set_status(self, text, dot_color):
        self.status_lbl.setText(
            f'<span style="color:{dot_color.name() if isinstance(dot_color, QColor) else dot_color};">●</span>&nbsp;&nbsp;'
            f'<span style="color:{self.theme.text_muted};">{text}</span>'
        )
    
    # ==================== SYSTEM MONITOR PAGE ====================
    def _build_system_monitor_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("System Monitor", "Live hardware usage, refreshed every second"))

        # Gauges with sparklines
        gauge_panel = QFrame()
        gauge_panel.setProperty("class", "Panel")
        add_shadow(gauge_panel)
        gp = QHBoxLayout(gauge_panel)
        gp.setContentsMargins(30, 28, 30, 28)
        gp.setSpacing(26)
        
        for title, color, gauge_attr, detail_attr, spark_attr in [
            ("CPU", self.theme.accent_2, "cpu_gauge_big", "cpu_detail_lbl_big", "cpu_spark"),
            ("MEMORY", self.theme.accent_1, "ram_gauge_big", "ram_detail_lbl_big", "ram_spark"),
            ("DISK", self.theme.accent_3, "disk_gauge_big", "disk_detail_lbl_big", "disk_spark"),
            ("BATTERY", self.theme.success, "bat_gauge_big", "bat_detail_lbl_big", "bat_spark"),
        ]:
            cell = QWidget()
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(8)
            
            gauge = CircularGauge(color=color)
            gauge.setFixedSize(110, 110)
            setattr(self, gauge_attr, gauge)
            
            spark = Sparkline(color=color, max_points=60)
            spark.setFixedHeight(36)
            setattr(self, spark_attr, spark)
            
            cap = QLabel(title)
            cap.setProperty("class", "GaugeCaption")
            cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            detail = QLabel("—")
            detail.setProperty("class", "GaugeDetail")
            detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
            setattr(self, detail_attr, detail)
            
            cl.addWidget(gauge, 0, Qt.AlignmentFlag.AlignHCenter)
            cl.addWidget(spark)
            cl.addWidget(cap)
            cl.addWidget(detail)
            gp.addWidget(cell)
        
        v.addWidget(gauge_panel)

        row = QHBoxLayout()
        row.setSpacing(20)

        net_panel = QFrame()
        net_panel.setProperty("class", "Panel")
        add_shadow(net_panel)
        npl = QVBoxLayout(net_panel)
        npl.setContentsMargins(26, 22, 26, 22)
        npl.addWidget(make_section_heading("NETWORK", self.theme))
        npl.addSpacing(10)
        self.dl_lbl_big = QLabel("⬇ DL: 0.00 MB/s")
        self.ul_lbl_big = QLabel("⬆ UL: 0.00 MB/s")
        self.dl_lbl_big.setProperty("class", "InfoData")
        self.ul_lbl_big.setProperty("class", "InfoData")
        self.ip_lbl = QLabel(f"Local IP: {self._get_local_ip()}")
        self.ip_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 11px;")
        npl.addWidget(self.dl_lbl_big)
        npl.addWidget(self.ul_lbl_big)
        npl.addSpacing(6)
        npl.addWidget(self.ip_lbl)
        
        # Network sparklines
        net_sparks = QHBoxLayout()
        self.net_dl_spark = Sparkline(color=self.theme.accent_1, max_points=60)
        self.net_ul_spark = Sparkline(color=self.theme.accent_2, max_points=60)
        net_sparks.addWidget(QLabel("DL"))
        net_sparks.addWidget(self.net_dl_spark, 1)
        net_sparks.addWidget(QLabel("UL"))
        net_sparks.addWidget(self.net_ul_spark, 1)
        npl.addLayout(net_sparks)
        npl.addStretch()
        row.addWidget(net_panel, 1)

        info_panel = QFrame()
        info_panel.setProperty("class", "Panel")
        add_shadow(info_panel)
        ipl = QVBoxLayout(info_panel)
        ipl.setContentsMargins(26, 22, 26, 22)
        ipl.addWidget(make_section_heading("HARDWARE INFO", self.theme))
        ipl.addSpacing(10)
        self.hw_info_lbl = QLabel("Tap below to fetch your PC's hardware details.")
        self.hw_info_lbl.setWordWrap(True)
        self.hw_info_lbl.setStyleSheet(f"color: {self.theme.text_main}; font-size: 12px; background: transparent;")
        ipl.addWidget(self.hw_info_lbl)
        ipl.addSpacing(10)
        btn_hw = QPushButton("Get Hardware Info")
        btn_hw.setProperty("class", "ActionBtn")
        btn_hw.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(btn_hw, "cpu", self.theme.accent_1, 16)
        btn_hw.clicked.connect(self.fetch_hardware_info)
        ipl.addWidget(btn_hw)
        ipl.addStretch()
        row.addWidget(info_panel, 1)

        v.addLayout(row)
        
        # Processes table
        proc_panel = QFrame()
        proc_panel.setProperty("class", "Panel")
        add_shadow(proc_panel)
        ppl = QVBoxLayout(proc_panel)
        ppl.setContentsMargins(26, 22, 26, 22)
        ppl.addWidget(make_section_heading("TOP PROCESSES", self.theme))
        ppl.addSpacing(10)
        
        self.proc_tree = QTreeView()
        self.proc_model = QFileSystemModel()  # Reuse for now
        self.proc_tree.setModel(self.proc_model)
        self.proc_tree.setRootIndex(self.proc_model.index(""))
        self.proc_tree.setColumnWidth(0, 200)
        self.proc_tree.setAlternatingRowColors(True)
        ppl.addWidget(self.proc_tree)
        
        v.addWidget(proc_panel, 1)
        v.addStretch()
        return page
    
    def fetch_hardware_info(self):
        self.hw_info_lbl.setText("Fetching...")
        worker = self.worker_pool.submit(SystemMonitor.get_hardware_info)
        worker.signals.result.connect(lambda msg: self.hw_info_lbl.setText(msg.replace("**", "")))
    
    # ==================== APPS & POWER PAGE ====================
    def _build_apps_power_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("Apps & Power", "Launch apps instantly and control your PC's power state"))

        row = QHBoxLayout()
        row.setSpacing(20)

        apps_panel = QFrame()
        apps_panel.setProperty("class", "Panel")
        add_shadow(apps_panel)
        apv = QVBoxLayout(apps_panel)
        apv.setContentsMargins(28, 24, 28, 24)
        apv.addWidget(make_section_heading("QUICK APPS", self.theme))
        apv.addSpacing(14)
        apps_grid = QGridLayout()
        apps_grid.setSpacing(10)
        r, c = 0, 0
        for name, target, icon_name in QUICK_APPS:
            btn = QPushButton(f"  {name}")
            btn.setProperty("class", "Pill")
            btn.setProperty("active", "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            Icon.set_btn_icon(btn, icon_name, self.theme.text_muted, 18)
            btn.clicked.connect(lambda checked, t=target, n=name: self.launch_app(t, n))
            apps_grid.addWidget(btn, r, c)
            self.app_pill_buttons.setdefault(name, []).append(btn)
            c += 1
            if c > 2:
                c, r = 0, r + 1
        apv.addLayout(apps_grid)
        apv.addSpacing(16)
        apv.addWidget(make_divider(self.theme))
        apv.addSpacing(14)
        apv.addWidget(make_section_heading("OR OPEN ANY OTHER APP", self.theme))
        apv.addSpacing(10)
        apv.addWidget(self._build_app_search_row())
        apv.addSpacing(6)
        hint = QLabel("Tip: you can also just type \"open spotify\" (or any app) straight into the chat.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self.theme.text_faint}; font-size: 11px;")
        apv.addWidget(hint)
        apv.addStretch()
        row.addWidget(apps_panel, 1)

        right_col = QVBoxLayout()
        right_col.setSpacing(20)

        power_panel = QFrame()
        power_panel.setProperty("class", "Panel")
        add_shadow(power_panel)
        pcv = QVBoxLayout(power_panel)
        pcv.setContentsMargins(28, 24, 28, 24)
        pcv.addWidget(make_section_heading("PC STATE", self.theme))
        pcv.addSpacing(14)
        pc_grid = QGridLayout()
        pc_grid.setSpacing(10)
        actions = [
            ("Lock", "lock", lambda: AppController.lock_pc()),
            ("Screenshot", "camera", lambda: self._run_action(AppController.take_screenshot)),
            ("Delete Last SS", "trash", lambda: self._run_action(AppController.delete_latest_screenshot)),
            ("Mute OS", "volume-mute", lambda: pyautogui.press('volumemute')),
        ]
        for i, (label, icon, action) in enumerate(actions):
            btn = QPushButton(f"  {label}")
            btn.setProperty("class", "Pill")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            Icon.set_btn_icon(btn, icon, self.theme.text_muted, 18)
            btn.clicked.connect(action)
            pc_grid.addWidget(btn, i // 2, i % 2)
        
        self.btn_focus = QPushButton("Focus Mode: OFF")
        self.btn_focus.setProperty("class", "Pill")
        self.btn_focus.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_focus.clicked.connect(self.toggle_focus)
        pc_grid.addWidget(self.btn_focus, 2, 0, 1, 2)
        pcv.addLayout(pc_grid)
        right_col.addWidget(power_panel)

        danger_panel = QFrame()
        danger_panel.setProperty("class", "Panel")
        add_shadow(danger_panel)
        dv = QVBoxLayout(danger_panel)
        dv.setContentsMargins(28, 24, 28, 24)
        dv.addWidget(make_section_heading("DANGER ZONE", self.theme))
        dv.addSpacing(12)
        d_row = QHBoxLayout()
        d_row.setSpacing(10)
        btn_restart = QPushButton("Restart PC")
        btn_shutdown = QPushButton("Shutdown PC")
        for b in (btn_restart, btn_shutdown):
            b.setProperty("class", "DangerPill")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_restart.clicked.connect(self.confirm_restart)
        btn_shutdown.clicked.connect(self.confirm_shutdown)
        d_row.addWidget(btn_restart)
        d_row.addWidget(btn_shutdown)
        dv.addLayout(d_row)
        right_col.addWidget(danger_panel)

        row.addLayout(right_col, 1)
        v.addLayout(row)
        v.addStretch()
        return page
    
    def _build_app_search_row(self):
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(10)
        self.app_search_input = QLineEdit()
        self.app_search_input.setPlaceholderText("Open any app by name...")
        self.app_search_input.returnPressed.connect(self._open_searched_app)
        btn_open = QPushButton("Open")
        btn_open.setProperty("class", "ActionBtn")
        btn_open.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open.clicked.connect(self._open_searched_app)
        h.addWidget(self.app_search_input, 1)
        h.addWidget(btn_open)
        return wrap

    def _open_searched_app(self):
        name = self.app_search_input.text().strip()
        if name:
            self._run_action(AppController.open_app, name)
            self.app_search_input.clear()
    
    def confirm_restart(self):
        self._confirm_and_run(
            "Restart PC",
            "Are you sure you want to restart your PC right now?\nAny unsaved work will be lost.",
            "shutdown /r /t 5"
        )

    def confirm_shutdown(self):
        self._confirm_and_run(
            "Shutdown PC",
            "Are you sure you want to shut down your PC right now?\nAny unsaved work will be lost.",
            "shutdown /s /t 5"
        )

    def _confirm_and_run(self, title, message, command):
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setText(message)
        box.setIcon(QMessageBox.Icon.Warning)
        yes_btn = box.addButton("Yes, continue", QMessageBox.ButtonRole.YesRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.NoRole)
        box.exec()
        if box.clickedButton() == yes_btn:
            os.system(command)
            self.add_chat_bubble(f"⚠️ {title} confirmed — executing in 5 seconds.", is_user=False)
            self.toast.show(f"{title} scheduled in 5s", "warning")
    
    # ==================== MUSIC PAGE ====================
    def _build_music_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("Music", "Control whatever's playing on your PC, plus display brightness"))

        now_panel = QFrame()
        now_panel.setProperty("class", "Panel")
        add_shadow(now_panel)
        npv = QVBoxLayout(now_panel)
        npv.setContentsMargins(34, 30, 34, 32)
        npv.addWidget(make_section_heading("NOW PLAYING", self.theme))
        npv.addSpacing(18)

        info_row = QHBoxLayout()
        info_row.setSpacing(18)
        self.album_disc_big = AlbumArtDisc(self.theme, size=76)
        info_col = QVBoxLayout()
        info_col.setSpacing(4)
        info_col.addStretch()
        self.media_title_lbl = QLabel("System Media Player")
        self.media_title_lbl.setStyleSheet(f"color: {self.theme.text_main}; font-size: 16px; font-weight: 700; background: transparent;")
        self.media_sub_lbl = QLabel("Works with Spotify, YouTube & more — press play to control")
        self.media_sub_lbl.setWordWrap(True)
        self.media_sub_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 12px; background: transparent;")
        info_col.addWidget(self.media_title_lbl)
        info_col.addWidget(self.media_sub_lbl)
        info_col.addStretch()
        info_row.addWidget(self.album_disc_big)
        info_row.addLayout(info_col, 1)
        npv.addLayout(info_row)
        npv.addSpacing(22)

        self.music_visualizer = AudioVisualizer(self.theme)
        self.music_visualizer.setFixedHeight(34)
        npv.addWidget(self.music_visualizer)
        npv.addSpacing(14)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        self.media_elapsed_lbl = QLabel("0:00")
        self.media_elapsed_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 11px; background: transparent;")
        self.media_progress = QSlider(Qt.Orientation.Horizontal)
        self.media_progress.setRange(0, 0)
        self.media_progress.setEnabled(HAS_WINSDK)
        self.media_progress.sliderPressed.connect(self._on_seek_pressed)
        self.media_progress.sliderReleased.connect(self._on_seek_released)
        self.media_duration_lbl = QLabel("0:00")
        self.media_duration_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 11px; background: transparent;")
        progress_row.addWidget(self.media_elapsed_lbl)
        progress_row.addWidget(self.media_progress, 1)
        progress_row.addWidget(self.media_duration_lbl)
        npv.addLayout(progress_row)
        npv.addSpacing(8)

        transport_row = QHBoxLayout()
        transport_row.setSpacing(14)
        transport_row.addStretch()
        
        btn_prev = QPushButton()
        btn_prev.setProperty("class", "RoundSm")
        btn_prev.setFixedSize(52, 52)
        btn_prev.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(btn_prev, "skip-back", self.theme.text_main, 22)
        btn_prev.clicked.connect(lambda: pyautogui.press('prevtrack'))

        self.btn_seek_back = QPushButton("-10s")
        self.btn_seek_back.setProperty("class", "RoundSm")
        self.btn_seek_back.setFixedSize(44, 44)
        self.btn_seek_back.setStyleSheet(f"font-size: 10.5px; font-weight: 800; color: {self.theme.text_main};")
        self.btn_seek_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_seek_back.clicked.connect(lambda: self._skip_seconds(-10))
        self.btn_seek_back.setVisible(HAS_WINSDK)

        self.btn_play_pause_big = QPushButton()
        self.btn_play_pause_big.setFixedSize(68, 68)
        self.btn_play_pause_big.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_play_pause_big.clicked.connect(self.toggle_play_pause)

        self.btn_seek_fwd = QPushButton("+10s")
        self.btn_seek_fwd.setProperty("class", "RoundSm")
        self.btn_seek_fwd.setFixedSize(44, 44)
        self.btn_seek_fwd.setStyleSheet(f"font-size: 10.5px; font-weight: 800; color: {self.theme.text_main};")
        self.btn_seek_fwd.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_seek_fwd.clicked.connect(lambda: self._skip_seconds(10))
        self.btn_seek_fwd.setVisible(HAS_WINSDK)

        btn_next = QPushButton()
        btn_next.setProperty("class", "RoundSm")
        btn_next.setFixedSize(52, 52)
        btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(btn_next, "skip-forward", self.theme.text_main, 22)
        btn_next.clicked.connect(lambda: pyautogui.press('nexttrack'))

        transport_row.addWidget(btn_prev)
        transport_row.addWidget(self.btn_seek_back)
        transport_row.addWidget(self.btn_play_pause_big)
        transport_row.addWidget(self.btn_seek_fwd)
        transport_row.addWidget(btn_next)
        transport_row.addStretch()
        npv.addLayout(transport_row)
        self._style_play_button()
        
        note = "" if HAS_WINSDK else "Install 'winsdk' package for real song title/progress — currently media-key controls only."
        if note:
            winsdk_note = QLabel(note)
            winsdk_note.setWordWrap(True)
            winsdk_note.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 11px; background: transparent;")
            npv.addWidget(winsdk_note)
            npv.addSpacing(6)

        v.addWidget(now_panel)

        row = QHBoxLayout()
        row.setSpacing(20)

        vol_panel = QFrame()
        vol_panel.setProperty("class", "Panel")
        add_shadow(vol_panel)
        vpl = QVBoxLayout(vol_panel)
        vpl.setContentsMargins(26, 22, 26, 22)
        vpl.addWidget(make_section_heading("VOLUME", self.theme))
        vpl.addSpacing(14)
        if HAS_PYCAW:
            current_vol = get_system_volume()
            self.vol_slider = QSlider(Qt.Orientation.Horizontal)
            self.vol_slider.setRange(0, 100)
            self.vol_slider.setValue(current_vol if current_vol is not None else 50)
            self.vol_lbl_val = QLabel(f"{self.vol_slider.value()}%")
            self.vol_lbl_val.setProperty("class", "InfoData")
            self.vol_slider.valueChanged.connect(self.change_volume)
            vr = QHBoxLayout()
            vr.addWidget(QLabel("🔊"))
            vr.addWidget(self.vol_slider)
            vr.addWidget(self.vol_lbl_val)
            vpl.addLayout(vr)
        else:
            vr = QHBoxLayout()
            vr.setSpacing(10)
            for label, key, icon in [("Down", "volumedown", "volume-down"), ("Mute", "volumemute", "volume-mute"), ("Up", "volumeup", "volume-up")]:
                btn = QPushButton()
                btn.setProperty("class", "RoundSm")
                btn.setFixedSize(40, 40)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                Icon.set_btn_icon(btn, icon, self.theme.text_main, 18)
                btn.clicked.connect(lambda _, k=key: pyautogui.press(k))
                vr.addWidget(btn)
            vr.addStretch()
            vpl.addLayout(vr)
        vpl.addStretch()
        row.addWidget(vol_panel, 1)

        bright_panel = QFrame()
        bright_panel.setProperty("class", "Panel")
        add_shadow(bright_panel)
        bpl = QVBoxLayout(bright_panel)
        bpl.setContentsMargins(26, 22, 26, 22)
        bpl.addWidget(make_section_heading("DISPLAY BRIGHTNESS", self.theme))
        bpl.addSpacing(14)
        bright_layout = QHBoxLayout()
        self.bright_slider = QSlider(Qt.Orientation.Horizontal)
        self.bright_slider.setRange(0, 100)
        self.bright_lbl_val = QLabel("50%")
        self.bright_lbl_val.setProperty("class", "InfoData")
        if HAS_SBC:
            try:
                curr_b = sbc.get_brightness(display=0)[0]
                self.bright_slider.setValue(curr_b)
                self.bright_lbl_val.setText(f"{curr_b}%")
            except Exception:
                pass
        self.bright_slider.valueChanged.connect(self.change_brightness)
        bright_layout.addWidget(QLabel("☀"))
        bright_layout.addWidget(self.bright_slider)
        bright_layout.addWidget(self.bright_lbl_val)
        bpl.addLayout(bright_layout)
        bpl.addStretch()
        row.addWidget(bright_panel, 1)

        v.addLayout(row)

        clock_panel = QFrame()
        clock_panel.setProperty("class", "Panel")
        add_shadow(clock_panel)
        cp = QHBoxLayout(clock_panel)
        cp.setContentsMargins(28, 18, 28, 18)
        cp_col = QVBoxLayout()
        cp_col.setSpacing(2)
        cp_col.addWidget(make_section_heading("LOCAL TIME", self.theme))
        self.time_lbl = QLabel("00:00:00")
        self.time_lbl.setProperty("class", "ClockBig")
        self.time_lbl.setFont(QFont("Consolas", 24, QFont.Weight.Bold))
        self.date_lbl = QLabel("YYYY-MM-DD")
        cp_col.addWidget(self.time_lbl)
        cp_col.addWidget(self.date_lbl)
        cp.addLayout(cp_col)
        cp.addStretch()
        v.addWidget(clock_panel)

        v.addStretch()
        return page
    
    def _style_play_button(self):
        icon = "pause" if self.media_playing else "play"
        style = (
            "border-radius: {r}px; font-size: {fs}px; font-weight: 800; padding: 0px; "
            f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {self.theme.accent_1}, stop:1 {self.theme.accent_2}); "
            "color: #000000; border: none;"
        )
        if hasattr(self, "btn_play_pause"):
            Icon.set_btn_icon(self.btn_play_pause, icon, "#000000", 20)
            self.btn_play_pause.setStyleSheet(style.format(r=20, fs=15))
        if hasattr(self, "btn_play_pause_big"):
            Icon.set_btn_icon(self.btn_play_pause_big, icon, "#000000", 28)
            self.btn_play_pause_big.setStyleSheet(style.format(r=34, fs=21))

    def _on_seek_pressed(self):
        self._media_seeking = True

    def _on_seek_released(self):
        target_seconds = self.media_progress.value() / 1000.0
        self._media_position = target_seconds
        self.media_elapsed_lbl.setText(format_time(target_seconds))
        worker = MediaSeekWorker(target_seconds)
        self._track_worker(worker)
        worker.start()
        self._media_seeking = False

    def _skip_seconds(self, delta: float):
        if not HAS_WINSDK:
            return
        target = max(0.0, min(self._media_duration, self._media_position + delta))
        self._media_position = target
        if hasattr(self, "media_progress"):
            self.media_progress.setValue(int(target * 1000))
            self.media_elapsed_lbl.setText(format_time(target))
        worker = MediaSeekWorker(target)
        self._track_worker(worker)
        worker.start()

    def toggle_play_pause(self):
        pyautogui.press('playpause')
        if not HAS_WINSDK:
            self.media_playing = not self.media_playing
            self._style_play_button()
            spinning = self.media_playing
            if hasattr(self, "album_disc"):
                self.album_disc.set_spinning(spinning)
            if hasattr(self, "album_disc_big"):
                self.album_disc_big.set_spinning(spinning)
            if hasattr(self, "music_visualizer"):
                self.music_visualizer.set_mode("PLAYING" if spinning else "SLEEPING")
            if hasattr(self, "media_sub_lbl"):
                self.media_sub_lbl.setText(
                    "Playing — controlling your active media player" if spinning
                    else "Paused — press play to resume"
                )

    def update_now_playing(self, info: dict):
        if not info:
            if HAS_WINSDK:
                if hasattr(self, "media_title_lbl"):
                    self.media_title_lbl.setText("Nothing playing")
                if hasattr(self, "media_sub_lbl"):
                    self.media_sub_lbl.setText("Play something on Spotify, YouTube, etc.")
                if hasattr(self, "media_progress"):
                    self.media_progress.setRange(0, 0)
                    self.media_progress.setValue(0)
                    self.media_elapsed_lbl.setText("0:00")
                    self.media_duration_lbl.setText("0:00")
            self.media_playing = False
            self._style_play_button()
            if hasattr(self, "album_disc"):
                self.album_disc.set_spinning(False)
            if hasattr(self, "album_disc_big"):
                self.album_disc_big.set_spinning(False)
            if hasattr(self, "music_visualizer"):
                self.music_visualizer.set_mode("SLEEPING")
            return

        self.media_playing = bool(info.get("is_playing"))
        title = info.get("title") or "Unknown Title"
        artist = info.get("artist") or ""
        position = info.get("position", 0)
        duration = info.get("duration", 0)
        self._media_duration = duration

        if hasattr(self, "media_title_lbl"):
            self.media_title_lbl.setText(title)
        if hasattr(self, "media_sub_lbl"):
            self.media_sub_lbl.setText(artist if artist else ("Playing" if self.media_playing else "Paused"))

        if hasattr(self, "media_progress") and not self._media_seeking:
            self._media_position = position
            total_ms = max(1, int(duration * 1000))
            self.media_progress.setRange(0, total_ms)
            self.media_progress.setValue(min(total_ms, int(position * 1000)))
            self.media_elapsed_lbl.setText(format_time(position))
            self.media_duration_lbl.setText(format_time(duration))

        self._style_play_button()
        if hasattr(self, "album_disc"):
            self.album_disc.set_spinning(self.media_playing)
        if hasattr(self, "album_disc_big"):
            self.album_disc_big.set_spinning(self.media_playing)
        if hasattr(self, "music_visualizer"):
            self.music_visualizer.set_mode("PLAYING" if self.media_playing else "SLEEPING")
    
    # ==================== FILES PAGE ====================
    def _build_files_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 20)
        v.setSpacing(14)
        v.addWidget(self._make_back_row("Files", "Browse your PC, and create new folders or files"))

        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        self.files_path_lbl = QLabel(FileManager.get_default_path())
        self.files_path_lbl.setProperty("class", "InfoData")
        
        btn_desktop = QPushButton("  Desktop")
        btn_new_folder = QPushButton("  New Folder")
        btn_new_file = QPushButton("  New File")
        for b, icon in [(btn_desktop, "house"), (btn_new_folder, "plus"), (btn_new_file, "file-text")]:
            b.setProperty("class", "ActionBtn")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            Icon.set_btn_icon(b, icon, self.theme.text_muted, 16)
        btn_desktop.clicked.connect(lambda: self.files_navigate_to(FileManager.get_default_path()))
        btn_new_folder.clicked.connect(self.create_new_folder)
        btn_new_file.clicked.connect(self.create_new_file)
        toolbar.addWidget(self.files_path_lbl, 1)
        toolbar.addWidget(btn_desktop)
        toolbar.addWidget(btn_new_folder)
        toolbar.addWidget(btn_new_file)
        v.addLayout(toolbar)

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(6, 6, 6, 6)

        self.fs_model = QFileSystemModel()
        self.fs_model.setRootPath("")
        self.files_tree = QTreeView()
        self.files_tree.setModel(self.fs_model)
        start_path = FileManager.get_default_path()
        self.files_tree.setRootIndex(self.fs_model.index(start_path))
        self.files_tree.setColumnWidth(0, 320)
        self.files_tree.setAnimated(True)
        self.files_tree.doubleClicked.connect(self.on_file_double_clicked)
        self.files_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.files_tree.customContextMenuRequested.connect(self._show_file_context_menu)
        pl.addWidget(self.files_tree)
        v.addWidget(panel, 1)

        return page
    
    def _show_file_context_menu(self, pos):
        index = self.files_tree.indexAt(pos)
        if not index.isValid():
            return
        path = self.fs_model.filePath(index)
        is_dir = self.fs_model.isDir(index)
        
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background: {self.theme.bg_panel}; border: 1px solid {self.theme.border}; border-radius: 8px; padding: 4px; }}
            QMenu::item {{ padding: 8px 24px; color: {self.theme.text_main}; border-radius: 4px; }}
            QMenu::item:selected {{ background: {self.theme.accent_1}; color: #000000; }}
        """)
        
        if is_dir:
            menu.addAction("Open in Terminal", lambda: self._run_action(AppController.open_terminal_at, path))
            menu.addAction("Open in VS Code", lambda: self._run_action(AppController.open_in_vscode, path))
        else:
            menu.addAction("Open", lambda: AppController.open_path(path))
            menu.addAction("Open With...", lambda: os.system(f'rundll32.exe shell32.dll,OpenAs_RunDLL "{path}"'))
            menu.addAction("Copy Path", lambda: pyperclip.copy(path) if HAS_PYPERCLIP else None)
            menu.addAction("Delete", lambda: self._delete_file(path))
        
        menu.addSeparator()
        menu.addAction("Refresh", lambda: self.files_navigate_to(self.files_path_lbl.text()))
        menu.exec(self.files_tree.mapToGlobal(pos))
    
    def _delete_file(self, path):
        reply = QMessageBox.question(self, "Delete", f"Delete {os.path.basename(path)}?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            try:
                if os.path.isdir(path):
                    import shutil
                    with audit_mod.audit.action("delete_folder", {"folder_name": os.path.basename(path),
                                                                  "path": os.path.dirname(path)}, source="ui") as act:
                        shutil.rmtree(path)
                        act.ok(f"Deleted folder '{os.path.basename(path)}' (permanently).")
                    self.toast.show("Folder deleted (folders can't be undone)", "success")
                else:
                    # Files go to Wave's recoverable trash - see the Activity page to undo.
                    msg = FileManager.delete_file(os.path.basename(path), os.path.dirname(path))
                    if msg.startswith("Deleted"):
                        self.toast.show("Deleted - you can undo it from Activity", "success")
                    else:
                        self.toast.show(msg, "error")
            except Exception as e:
                self.toast.show(f"Error: {e}", "error")
    
    def files_navigate_to(self, path):
        self.files_tree.setRootIndex(self.fs_model.index(path))
        self.files_path_lbl.setText(path)
    
    def on_file_double_clicked(self, index):
        path = self.fs_model.filePath(index)
        if self.fs_model.isDir(index):
            self.files_navigate_to(path)
        else:
            try:
                AppController.open_path(path)
            except Exception as e:
                logger.error(f"Open file error: {e}")
                self.toast.show(f"Error opening file: {e}", "error")
    
    def create_new_folder(self):
        path = self.files_path_lbl.text()
        self._run_action(FileManager.create_folder, "New Folder", path)
        QTimer.singleShot(300, lambda: self.files_navigate_to(path))
    
    def create_new_file(self):
        path = self.files_path_lbl.text()
        self._run_action(FileManager.create_file, "New File.txt", "", path)
        QTimer.singleShot(300, lambda: self.files_navigate_to(path))
    
    # ==================== VISION PAGE ====================
    def _build_vision_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("Vision", "Capture your screen and let W.A.V.E. read what's on it"))

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(30, 26, 30, 26)
        pv.addWidget(make_section_heading("SCREEN TEXT READER (OCR)", self.theme))
        pv.addSpacing(12)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_ocr = QPushButton("  Capture & Read Text")
        btn_photo = QPushButton("  Take Photo")
        for b, icon in [(btn_ocr, "image"), (btn_photo, "camera")]:
            b.setProperty("class", "ActionBtn")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            Icon.set_btn_icon(b, icon, self.theme.text_muted, 16)
        btn_ocr.clicked.connect(self.run_ocr_capture)
        btn_photo.clicked.connect(self.take_webcam_photo)
        btn_row.addWidget(btn_ocr)
        btn_row.addWidget(btn_photo)
        pv.addLayout(btn_row)
        pv.addSpacing(16)

        self.vision_result = QTextEdit()
        self.vision_result.setReadOnly(True)
        self.vision_result.setObjectName("StickyNote")
        self.vision_result.setPlaceholderText("Extracted text will appear here...")
        self.vision_result.setMinimumHeight(220)
        pv.addWidget(self.vision_result)
        pv.addSpacing(10)

        note = QLabel("Needs Tesseract-OCR installed on your PC for text reading to work.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.theme.text_faint}; font-size: 11px;")
        pv.addWidget(note)

        v.addWidget(panel)
        v.addStretch()
        return page
    
    def run_ocr_capture(self):
        self.vision_result.setPlainText("Capturing screen and reading text...")
        self.showMinimized()
        QTimer.singleShot(350, self._start_ocr_worker)
    
    def _start_ocr_worker(self):
        worker = self.worker_pool.submit(OCRWorker().run)
        worker.signals.result.connect(self._on_ocr_result)
        worker.signals.error.connect(lambda e: self.vision_result.setPlainText(f"Error: {e}"))
    
    def _on_ocr_result(self, text):
        self.vision_result.setPlainText(text)
        self.showNormal()
        self.activateWindow()
        self.raise_()
        self.toast.show("OCR complete", "success")
    
    def take_webcam_photo(self):
        self.vision_result.setPlainText("Opening webcam...")
        worker = self.worker_pool.submit(VisionCapture.take_photo)
        worker.signals.result.connect(self.vision_result.setPlainText)
        worker.signals.error.connect(lambda e: self.vision_result.setPlainText(f"Error: {e}"))
    
    # ==================== CLIPBOARD PAGE ====================
    # ==================== ACTIVITY PAGE (Phase 4: audit + undo) ====================
    def _build_activity_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("Activity", "Everything Wave did, newest first — file changes can be undone with one click"))

        panel = QFrame()
        panel.setProperty("class", "Panel")
        add_shadow(panel)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(26, 24, 26, 24)

        head = QHBoxLayout()
        head.addWidget(make_section_heading("RECENT ACTIONS", self.theme))
        head.addStretch()
        self.btn_undo_last = QPushButton("Undo last")
        self.btn_undo_last.setProperty("class", "ActionBtn")
        self.btn_undo_last.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_undo_last.clicked.connect(self._undo_last_clicked)
        head.addWidget(self.btn_undo_last)
        self.btn_empty_trash = QPushButton("Empty trash")
        self.btn_empty_trash.setProperty("class", "ActionBtn")
        self.btn_empty_trash.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_empty_trash.clicked.connect(self._empty_trash_clicked)
        head.addWidget(self.btn_empty_trash)
        pv.addLayout(head)

        self.activity_note = QLabel("")
        self.activity_note.setWordWrap(True)
        self.activity_note.setStyleSheet(f"color: {self.theme.text_faint}; font-size: 11px;")
        pv.addWidget(self.activity_note)
        pv.addSpacing(8)

        self.activity_list_layout = QVBoxLayout()
        self.activity_list_layout.setSpacing(8)
        pv.addLayout(self.activity_list_layout)
        pv.addStretch()
        v.addWidget(panel, 1)
        self._activity_dirty = False
        self._refresh_activity_page()
        return page

    def _make_activity_row(self, rec):
        t = self.theme
        entry = QFrame()
        entry.setStyleSheet(f"background-color: {t.bg_surface}; border-radius: 12px; border: 1px solid {t.border};")
        row = QHBoxLayout(entry)
        row.setContentsMargins(14, 10, 10, 10)
        row.setSpacing(12)
        mark, color = {"ok": ("✓", t.success), "failed": ("✗", t.danger),
                       "blocked": ("⛔", t.warning)}.get(rec.status, ("•", t.text_faint))
        dot = QLabel(mark)
        dot.setFixedWidth(18)
        dot.setStyleSheet(f"color: {color}; font-size: 14px; font-weight: 700; background: transparent; border: none;")
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel(rec.summary())
        title.setWordWrap(True)
        title.setStyleSheet(f"color: {t.text_main}; font-size: 12px; font-weight: 600; background: transparent; border: none;")
        detail = rec.result if len(rec.result) <= 110 else rec.result[:107] + "..."
        sub = QLabel(f"{rec.time_text}  ·  {rec.source}" + (f"  ·  {detail}" if detail else ""))
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {t.text_faint}; font-size: 11px; background: transparent; border: none;")
        text.addWidget(title)
        text.addWidget(sub)
        row.addWidget(dot)
        row.addLayout(text, 1)
        if rec.reversible:
            btn = QPushButton("Undo")
            btn.setProperty("class", "ActionBtn")
            btn.setFixedWidth(70)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(rec.undo_label or "Undo this action")
            btn.clicked.connect(lambda _=False, rid=rec.id: self._undo_clicked(rid))
            row.addWidget(btn)
        elif rec.undone_at:
            done = QLabel("Undone")
            done.setStyleSheet(f"color: {t.text_faint}; font-size: 11px; background: transparent; border: none;")
            row.addWidget(done)
        return entry

    def _refresh_activity_page(self):
        layout = getattr(self, "activity_list_layout", None)
        if layout is None:
            return
        self._activity_dirty = False
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        rows = audit_mod.audit.recent(60)
        if not rows:
            empty = QLabel("Nothing yet. Ask Wave to create a file or open an app and it will show up here.")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {self.theme.text_faint}; font-size: 12px; padding: 20px;")
            layout.addWidget(empty)
        for rec in rows:
            layout.addWidget(self._make_activity_row(rec))
        self.btn_undo_last.setEnabled(any(r.reversible for r in rows))
        self._update_trash_note()

    def _update_trash_note(self):
        if not undo_mod.trash_enabled():
            text = "Undo for deleted files is switched off (UNDO_TRASH_ENABLED=false in .env), so deletes are permanent."
        else:
            mb = undo_mod.trash.size_bytes() / (1024 * 1024)
            days = undo_mod.trash.retention_days
            text = (f"Deleted files wait in Wave's trash for {days} day{'s' if days != 1 else ''} "
                    f"before they're gone for good ({mb:.1f} MB there now).")
        self.activity_note.setText(text)

    def _on_audit_event(self):
        page = getattr(self, "_activity_page", None)
        if page is None:
            return
        if page.isVisible():
            self._refresh_activity_page()
        else:
            self._activity_dirty = True     # refreshed when the page is opened

    # Tier 21: live activity banner (see the tool_activity signal's own
    # comment and automation/audit.py's "started" event docstring).
    def _on_audit_activity_event(self, event, rec):
        """Runs on whatever thread the tool call is happening on - only
        ever does a thread-safe .emit(), same rule every other cross-thread
        audit/proactive/ollama listener in this file follows."""
        if event == "started" and rec is not None:
            try:
                description = audit_mod.summarize(rec.tool, rec.args)
            except Exception:
                description = rec.tool
            self.tool_activity.emit(description)

    def _on_tool_activity(self, description: str):
        banner = getattr(self, "activity_banner", None)
        if banner is None:
            return
        banner.setText(f"⚙  {description}…")
        banner.setVisible(True)
        # Auto-hide rather than waiting for a "finished" counterpart: most
        # tool calls finish in well under this window, and some (quiet
        # tools, joined child scopes - see add_listener's docstring) never
        # produce a second event at all. Each new "started" restarts this
        # timer, so a banner showing a slow action's description just keeps
        # refreshing instead of flickering off mid-action.
        self._activity_banner_timer_gen = getattr(self, "_activity_banner_timer_gen", 0) + 1
        my_gen = self._activity_banner_timer_gen
        QTimer.singleShot(4000, lambda: self._hide_activity_banner_if_current(my_gen))

    def _hide_activity_banner_if_current(self, gen: int):
        if gen == getattr(self, "_activity_banner_timer_gen", None):
            banner = getattr(self, "activity_banner", None)
            if banner is not None:
                banner.setVisible(False)

    def _undo_clicked(self, action_id):
        worker = self.worker_pool.submit(undo_mod.undo_manager.undo, action_id, "ui")
        worker.signals.result.connect(self._on_undo_result)
        worker.signals.error.connect(lambda e: self.toast.show(f"Error: {e}", "error"))

    def _undo_last_clicked(self):
        rec = audit_mod.audit.last_reversible()
        if rec is None:
            self.toast.show("There's nothing to undo right now.", "info")
            return
        self._undo_clicked(rec.id)

    def _on_undo_result(self, result):
        self.toast.show(result.message, "success" if result.ok else "warning", duration=5000)
        self._refresh_activity_page()

    def _empty_trash_clicked(self):
        reply = QMessageBox.question(self, "Empty trash",
                                     "Permanently delete everything in Wave's trash? Earlier deletions can't be undone afterwards.",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            count = undo_mod.trash.empty()
            self.toast.show(f"Trash emptied ({count} item{'s' if count != 1 else ''}).", "success")
            self._update_trash_note()

    # ==================== HOT-RELOAD SKILLS (Phase 4) ====================
    def _start_skill_watcher(self):
        try:
            loader = get_loader()
            loader.add_listener(lambda ev: self.skill_event.emit(ev.kind, ev.message()))
            loader.start(watch=config.SKILLS_HOT_RELOAD)
            if config.SKILLS_HOT_RELOAD:
                # Tier 19: only worth watching (and restarting) if it was
                # actually asked to watch in the first place - load_now()
                # above already ran its one-shot scan either way, but with
                # watch=False there's no background thread here at all for
                # is_alive() to report on.
                thread_supervisor.watch("skill_loader", loader)
        except Exception as e:
            logging.getLogger("MainWindow").error(f"Couldn't start the skills watcher: {e}")

    def _on_skill_event(self, kind, message):
        icon = {"loaded": "success", "reloaded": "success", "unloaded": "info", "error": "warning"}.get(kind, "info")
        self.toast.show(message, icon, duration=6000 if kind == "error" else 3500)

    def _build_clipboard_page(self):
        page = QWidget()
        page.setObjectName("PageBg")
        v = QVBoxLayout(page)
        v.setContentsMargins(32, 28, 32, 28)
        v.setSpacing(20)
        v.addWidget(self._make_back_row("Clipboard History", "Everything you copy shows up here — click Copy to grab it again"))

        if not HAS_PYPERCLIP:
            warn_panel = QFrame()
            warn_panel.setProperty("class", "Panel")
            add_shadow(warn_panel)
            wv = QVBoxLayout(warn_panel)
            wv.setContentsMargins(26, 22, 26, 22)
            note = QLabel("Clipboard monitoring needs the 'pyperclip' package.\nInstall it with: pip install pyperclip")
            note.setWordWrap(True)
            wv.addWidget(note)
            v.addWidget(warn_panel)

        list_panel = QFrame()
        list_panel.setProperty("class", "Panel")
        add_shadow(list_panel)
        lv = QVBoxLayout(list_panel)
        lv.setContentsMargins(26, 24, 26, 24)
        lv.addWidget(make_section_heading("RECENT ITEMS", self.theme))
        lv.addSpacing(12)

        self.clip_list_layout = QVBoxLayout()
        self.clip_list_layout.setSpacing(8)
        self.clip_list_layout.addWidget(self._clip_empty_label())
        lv.addLayout(self.clip_list_layout)
        lv.addStretch()
        v.addWidget(list_panel, 1)

        return page
    
    def _clip_empty_label(self):
        lbl = QLabel("Nothing copied yet this session. Copy some text anywhere and it'll show up here.")
        lbl.setObjectName("ClipEmptyLbl")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {self.theme.text_faint}; font-size: 12px; padding: 20px;")
        return lbl
    
    def on_clip_changed(self, text):
        empty_lbl = self.clip_list_layout.itemAt(0).widget() if self.clip_list_layout.count() else None
        if empty_lbl is not None and empty_lbl.objectName() == "ClipEmptyLbl":
            empty_lbl.deleteLater()

        entry = QFrame()
        entry.setStyleSheet(f"background-color: {self.theme.bg_surface}; border-radius: 12px; border: 1px solid {self.theme.border};")
        el = QHBoxLayout(entry)
        el.setContentsMargins(14, 10, 10, 10)
        preview = text if len(text) <= 90 else text[:87] + "..."
        lbl = QLabel(preview)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color: {self.theme.text_main}; font-size: 12px; background: transparent; border: none;")
        btn_copy = QPushButton("Copy")
        btn_copy.setProperty("class", "ActionBtn")
        btn_copy.setFixedWidth(70)
        btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        Icon.set_btn_icon(btn_copy, "copy", self.theme.accent_1, 14)
        btn_copy.clicked.connect(lambda: pyperclip.copy(text) if HAS_PYPERCLIP else None)
        el.addWidget(lbl, 1)
        el.addWidget(btn_copy)

        self.clip_list_layout.insertWidget(0, entry)
        while self.clip_list_layout.count() > 20:
            item = self.clip_list_layout.takeAt(self.clip_list_layout.count() - 1)
            if item.widget():
                item.widget().deleteLater()
    
    # ==================== WORKFLOWS PAGE (Tier 3 item 9) ====================
    def _build_workflows_page(self):
        return WorkflowsPanel(self, self.theme)

    # ==================== TIER 21 MANAGEMENT PAGES ====================
    def _build_memory_page(self):
        return MemoryPanel(self, self.theme)

    def _build_reminders_page(self):
        return RemindersPanel(self, self.theme)

    def _build_notes_todos_page(self):
        return NotesTodosPanel(self, self.theme)

    def _build_calendar_page(self):
        return CalendarPanel(self, self.theme)

    def _build_skills_page(self):
        return SkillsPanel(self, self.theme)

    def _build_performance_page(self):
        return PerformancePanel(self, self.theme)

    # ==================== SETTINGS PAGE ====================
    def _build_settings_page(self):
        return SettingsPanel(self, self.theme)
    
    # ==================== CHAT LOGIC ====================
    def launch_app(self, target, display_name):
        for name, buttons in self.app_pill_buttons.items():
            for b in buttons:
                set_active(b, name == display_name)
        self._run_action(AppController.open_app, target)
    
    def change_volume(self, value):
        self.vol_lbl_val.setText(f"{value}%")
        set_system_volume(value)
    
    def take_quick_screenshot(self):
        self._run_action(AppController.take_screenshot)
    
    def toggle_focus(self):
        self.focus_mode = not self.focus_mode
        pyautogui.press('volumemute')
        self.btn_focus.setText(f"Focus Mode: {'ON' if self.focus_mode else 'OFF'}")
        set_active(self.btn_focus, self.focus_mode)
        if self.focus_mode:
            self.add_chat_bubble("🧘 Focus Mode ON: System muted. Distractions minimized.", is_user=False)
        else:
            self.add_chat_bubble("🔔 Focus Mode OFF: System audio restored.", is_user=False)
    
    def change_brightness(self, value):
        self.bright_lbl_val.setText(f"{value}%")
        if HAS_SBC:
            try:
                sbc.set_brightness(value)
            except Exception as e:
                logger.error(f"Brightness error: {e}")
    
    def save_settings(self):
        sp = getattr(self, "settings_panel", None)
        data = {
            "notes": sp.sticky_note.toPlainText() if sp and hasattr(sp, 'sticky_note') else "",
            "focus_mode": self.focus_mode,
        }
        if HAS_PYCAW and hasattr(self, "vol_slider"):
            data["volume"] = self.vol_slider.value()
        if sp:
            if hasattr(sp, "cb_clipboard"):
                data["clipboard_monitoring"] = sp.cb_clipboard.isChecked()
            if hasattr(sp, "cb_screen"):
                data["screen_capture_allowed"] = sp.cb_screen.isChecked()
            if hasattr(sp, "cb_chat_history"):
                data["save_chat_history"] = sp.cb_chat_history.isChecked()
            if hasattr(sp, "cb_notifications"):
                data["notifications_enabled"] = sp.cb_notifications.isChecked()
            if hasattr(sp, "cb_usage_stats"):
                data["usage_stats_enabled"] = sp.cb_usage_stats.isChecked()
            # Tier 20: Security page
            if hasattr(sp, "security_mode_combo"):
                data["security_mode"] = sp.security_mode_combo.currentText()
            if hasattr(sp, "cb_redact_secrets"):
                data["redact_secrets"] = sp.cb_redact_secrets.isChecked()
            # ── Voice settings ──
            if hasattr(sp, "tts_combo"):
                data["tts_voice"] = sp.current_tts_voice_id()   # Tier 33: the id, not the label
            if hasattr(sp, "rate_slider"):
                data["tts_rate"] = sp.rate_slider.value()
            if hasattr(sp, "tts_vol_slider"):
                data["tts_volume"] = sp.tts_vol_slider.value()
            if hasattr(sp, "wake_combo"):
                data["wake_word_label"] = sp.wake_combo.currentText()
                data["wake_phrases"] = self.wake_phrases
            if hasattr(sp, "stt_lang"):
                data["stt_lang"] = sp.stt_lang.currentText()
            # Tier 11: wake word detection mode + sensitivity
            if hasattr(sp, "wake_engine_combo"):
                data["wake_engine"] = sp.wake_engine_combo.currentText()
            if getattr(sp, "_wake_threshold_touched", False) and hasattr(sp, "wake_sens_slider"):
                data["wake_sensitivity"] = sp.wake_sens_slider.value()
            if hasattr(sp, "cb_local_voice"):
                data["tts_local_auto_download"] = sp.cb_local_voice.isChecked()
            # Tier 23: remote dashboard
            if hasattr(sp, "cb_remote_dashboard"):
                data["remote_dashboard_enabled"] = sp.cb_remote_dashboard.isChecked()
            if hasattr(sp, "remote_port_spin"):
                data["remote_dashboard_port"] = sp.remote_port_spin.value()
            # Tier 25: avatar companion startup toggles - the photo/voice
            # path fields themselves are deliberately NOT saved here, see
            # SettingsPanel._build_avatar_page()'s own docstring for why.
            if hasattr(sp, "cb_avatar_show"):
                data["avatar_show_on_startup"] = sp.cb_avatar_show.isChecked()
            if hasattr(sp, "cb_avatar_show_dashboard"):
                data["avatar_show_dashboard_on_startup"] = sp.cb_avatar_show_dashboard.isChecked()
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            self.toast.show("Settings saved", "success")
        except Exception as e:
            logger.error(f"Save settings error: {e}")
            self.toast.show("Could not save settings", "error")

    def load_settings(self, silent=False):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            sp = getattr(self, "settings_panel", None)
            if sp and hasattr(sp, 'sticky_note'):
                sp.sticky_note.setPlainText(data.get("notes", ""))
            if "volume" in data and HAS_PYCAW and hasattr(self, "vol_slider"):
                self.vol_slider.setValue(data["volume"])
            if sp:
                # Privacy toggles
                if hasattr(sp, "cb_clipboard"):
                    sp.cb_clipboard.setChecked(data.get("clipboard_monitoring", True))
                if hasattr(sp, "cb_screen"):
                    sp.cb_screen.setChecked(data.get("screen_capture_allowed", True))
                if hasattr(sp, "cb_chat_history"):
                    sp.cb_chat_history.setChecked(data.get("save_chat_history", True))
                if hasattr(sp, "cb_notifications"):
                    sp.cb_notifications.setChecked(data.get("notifications_enabled", True))
                if hasattr(sp, "cb_usage_stats"):
                    sp.cb_usage_stats.setChecked(data.get("usage_stats_enabled", False))
                # Tier 20: Security page - setCurrentText fires
                # _on_security_mode_changed, which applies it immediately.
                if "security_mode" in data and hasattr(sp, "security_mode_combo"):
                    sp.security_mode_combo.setCurrentText(data["security_mode"])
                if "redact_secrets" in data and hasattr(sp, "cb_redact_secrets"):
                    sp.cb_redact_secrets.setChecked(data["redact_secrets"])
                # Voice settings — setCurrentText/setValue fires the
                # change handlers which apply the value to tts_engine/
                # stt_engine immediately. wake_combo uses blockSignals
                # so we don't pop the "type a custom phrase" dialog on
                # every single startup.
                if "tts_voice" in data and hasattr(sp, "tts_combo"):
                    sp.select_tts_voice(data["tts_voice"])
                if "tts_rate" in data and hasattr(sp, "rate_slider"):
                    sp.rate_slider.setValue(data["tts_rate"])
                if "tts_volume" in data and hasattr(sp, "tts_vol_slider"):
                    sp.tts_vol_slider.setValue(data["tts_volume"])
                if "stt_lang" in data and hasattr(sp, "stt_lang"):
                    sp.stt_lang.setCurrentText(data["stt_lang"])
                if hasattr(sp, "wake_combo"):
                    if "wake_phrases" in data:
                        self.wake_phrases = data["wake_phrases"]
                        if self.mic_worker:
                            self.mic_worker.set_wake_phrases(self.wake_phrases)
                    if "wake_word_label" in data:
                        sp.wake_combo.blockSignals(True)
                        sp.wake_combo.setCurrentText(data["wake_word_label"])
                        sp.wake_combo.blockSignals(False)
                if "wake_engine" in data and hasattr(sp, "wake_engine_combo"):
                    sp.wake_engine_combo.setCurrentText(data["wake_engine"])
                if "wake_sensitivity" in data and hasattr(sp, "wake_sens_slider"):
                    sp.wake_sens_slider.setValue(int(data["wake_sensitivity"]))
                if hasattr(sp, "_refresh_wake_status"):
                    sp._refresh_wake_status()
                if "tts_local_auto_download" in data and hasattr(sp, "cb_local_voice"):
                    sp.cb_local_voice.setChecked(data["tts_local_auto_download"])
                # Tier 23: remote dashboard - port MUST be set before the
                # checkbox, since checking it fires _on_remote_dashboard_
                # toggled -> start_remote_dashboard(self.remote_port_spin.
                # value()) immediately.
                if "remote_dashboard_port" in data and hasattr(sp, "remote_port_spin"):
                    sp.remote_port_spin.setValue(int(data["remote_dashboard_port"]))
                if data.get("remote_dashboard_enabled") and hasattr(sp, "cb_remote_dashboard"):
                    sp.cb_remote_dashboard.setChecked(True)
                # Tier 25: avatar companion startup toggles
                if hasattr(sp, "cb_avatar_show"):
                    sp.cb_avatar_show.setChecked(data.get("avatar_show_on_startup", True))
                if hasattr(sp, "cb_avatar_show_dashboard"):
                    sp.cb_avatar_show_dashboard.setChecked(data.get("avatar_show_dashboard_on_startup", False))
            if not silent:
                self.toast.show("Settings loaded", "success")
        except FileNotFoundError:
            if not silent:
                self.toast.show("No saved settings found", "info")
        except Exception as e:
            logger.error(f"Load settings error: {e}")
            if not silent:
                self.toast.show("Could not load settings", "error")
    
    # ==================== REMOTE DASHBOARD (Tier 23) ====================
    def _get_remote_dashboard(self):
        """Lazily imports remote.runner and constructs self.remote_
        dashboard on first use - see the comment on self.remote_dashboard
        in __init__ for why this isn't done eagerly."""
        if self.remote_dashboard is None:
            from remote.runner import RemoteDashboardServer
            self.remote_dashboard = RemoteDashboardServer(self.brain, assistant_name="Wave")
        return self.remote_dashboard

    def start_remote_dashboard(self, port: int):
        """Runs the actual start() off the GUI thread (same worker_pool
        every other settings-page "do a thing, then update a label" action
        already uses, e.g. _test_provider_clicked) and reports back to
        the Remote settings page once it's done."""
        server = self._get_remote_dashboard()
        worker = self.worker_pool.submit(server.start, host="0.0.0.0", port=port)
        worker.signals.result.connect(self._on_remote_dashboard_start_result)
        worker.signals.error.connect(lambda e: self._on_remote_dashboard_start_result((False, str(e))))

    def _on_remote_dashboard_start_result(self, result):
        ok, message = result
        sp = getattr(self, "settings_panel", None)
        if sp and hasattr(sp, "show_remote_dashboard_result"):
            sp.show_remote_dashboard_result(ok, message)
        if hasattr(self, "toast"):
            self.toast.show(message, "success" if ok else "error")

    # ==================== AVATAR COMPANION (Tier 25) ====================
    # avatar/window.py (the floating widget) + avatar/state.py (the state
    # machine) + avatar/control.py (the Qt-thread-safety seam show_ui/
    # hide_ui call into) - see docs/ROADMAP_BEAT_SARA.md's Tier 25 section
    # and avatar/control.py's own docstring for the full picture. Every
    # method below is a thin no-op when self.avatar_window is None (no rig
    # built yet), so none of this changes behaviour for a Wave install
    # that never sets a photo up - the dashboard still starts minimized
    # exactly as it always has.
    def _init_avatar(self):
        """Builds the avatar window + state machine + controller if
        avatar/rig.py has an actual rig to show (i.e. the user has set up
        a photo - see the Settings -> Avatar page's "Build avatar"
        button), reads the avatar_* toggles straight out of SETTINGS_FILE
        (not through settings_panel - this must work even before that
        panel's widgets exist), and shows the avatar window itself if the
        "show avatar on startup" toggle says to. Whether the DASHBOARD
        window should start hidden is read back afterwards via
        should_start_hidden() - wave_start.pyw decides between hide() and
        the pre-Tier-25 showMinimized() based on that, not on anything
        done directly here."""
        self.avatar_state_machine = AvatarStateMachine()
        self.avatar_window = AvatarWindow(AVATAR_RIG_DIR, self.avatar_state_machine)
        if not self.avatar_window.has_rig():
            # Nothing built yet - leave the window instance around (so a
            # later "Build avatar" in Settings can show() it without a
            # restart) but don't show it, and don't register a controller:
            # get_active_controller() returning None is show_ui/hide_ui's
            # own signal that avatar mode isn't live yet.
            logger.info("Tier 25: no avatar rig built yet - dashboard starts as before.")
            return
        self.avatar_controller = AvatarController(self, self.avatar_window)
        set_active_controller(self.avatar_controller)
        data = self._read_settings_file()
        if data.get("avatar_show_on_startup", True):
            self.avatar_window.show()
        # Restore a previously-set voice-clone reference, if any. This is
        # NOT the same thing _build_avatar_page()'s docstring means by "the
        # path fields aren't restored" - that's about the Settings page's
        # two file-picker QLineEdits, which genuinely don't need their old
        # text back. This is the reference clip build_avatar_from_files()
        # actually pointed the TTS engine at, and without restoring it here
        # too, a user who clones their voice once would silently lose that
        # clone on every single restart (falling back to edge-tts with no
        # indication why) until they re-ran "Build avatar" from scratch -
        # found while running this tier against a real photo/voice sample
        # for the first time, not caught by the mocked tests, which never
        # exercise a second process start.
        saved_ref = (data.get("avatar_voice_reference_path") or "").strip()
        ref_path = avatar_rig.find_voice_reference(saved_ref, AVATAR_RIG_DIR)
        if ref_path:
            config.TTS_CLONE_REFERENCE_PATH = ref_path
            if ref_path != saved_ref:
                # The project folder moved; remember where the clip is now.
                self._persist_settings_key("avatar_voice_reference_path", ref_path)
            logger.info("Tier 25: restored voice-clone reference from a previous session.")
            tts_engine.warm_up_clone()   # Tier 31: load the model before the first reply
            self._refresh_clone_voice_entry()
        elif saved_ref:
            logger.warning(
                f"Tier 25: previously-set voice-clone reference '{saved_ref}' is gone - "
                "falling back to edge-tts/Piper/Kokoro until 'Build avatar' is run again."
            )
        logger.info("Tier 25: avatar companion active.")

    def _read_settings_file(self) -> dict:
        """Tiny helper so _init_avatar() (which must run before
        settings_panel's widgets necessarily exist) and load_settings()
        don't each open SETTINGS_FILE their own slightly-different way."""
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def should_start_hidden(self) -> bool:
        """Called once by wave_start.pyw right after construction. True
        only when the avatar is actually live (a rig exists) AND the
        "show dashboard on startup" toggle is off (the default) - every
        other case (no rig yet, or the user explicitly wants the
        dashboard up too) keeps the original showMinimized() behaviour,
        so there is never a run where NEITHER the avatar NOR the
        dashboard is visible."""
        if not (self.avatar_window and self.avatar_window.has_rig()):
            return False
        return not self._read_settings_file().get("avatar_show_dashboard_on_startup", False)

    def build_avatar_from_files(self, photo_path: str, voice_sample_path: str = "") -> str:
        """Settings -> Avatar page's "Build avatar" button. Runs avatar/
        rig.py's build_rig() against the given photo (synchronously - it's
        a handful of OpenCV calls on one image, not worth a worker thread)
        and, if this is the first successful build this run, constructs
        the avatar window/state machine/controller immediately so the
        avatar appears without restarting Wave. If voice_sample_path is
        also given, points voice/tts.py's cloning engine (Tier 25's 4th
        TTS option) at it - see settings/config.py's TTS_CLONE_* docstring
        for why just setting this path IS the opt-in, no separate consent
        flag needed - and clears any previously cached load failure so a
        freshly-picked file gets its own fair attempt rather than reusing
        a verdict from a stale or missing one."""
        try:
            avatar_rig.build_rig(photo_path, AVATAR_RIG_DIR)
        except avatar_rig.RigBuildError as e:
            return f"Avatar nahi ban paya: {e}"
        except Exception as e:
            logger.error(f"build_avatar_from_files error: {e}")
            return f"Avatar nahi ban paya: {e}"
        if self.avatar_window is None or not self.avatar_window.has_rig():
            self._init_avatar()
        else:
            self.avatar_window._load_rig()  # re-pull the freshly-written frames
            self.avatar_window._apply_frame("neutral")
        if self.avatar_window and not self.avatar_window.isVisible():
            self.avatar_window.show()
        voice_sample_path = (voice_sample_path or "").strip()
        voice_note = ""
        if voice_sample_path:
            if os.path.exists(voice_sample_path):
                config.TTS_CLONE_REFERENCE_PATH = voice_sample_path
                tts_engine.reset_clone()   # Tier 31: stops the old sample's voice process
                # Persisted separately from the file-picker fields (see
                # _build_avatar_page()'s docstring) - this is the actual
                # reference clip the TTS engine now depends on, so it has
                # to survive a restart or voice cloning silently reverts
                # to edge-tts the next time Wave starts. Restored by
                # _init_avatar() above.
                self._persist_settings_key("avatar_voice_reference_path", voice_sample_path)
                # Tier 33: a sample picked here means "speak in this voice" -
                # it becomes the voice in Settings -> Voice (and loads now).
                tts_engine.set_voice(CLONE_VOICE_ID)
                self._refresh_clone_voice_entry()
                voice_note = " Aapki awaz clone hone ke liye set ho gayi hai."
            else:
                voice_note = f" (Voice sample '{voice_sample_path}' nahi mili - awaz clone nahi hui.)"
        return "Avatar ban gaya aur dikhayi de raha hai." + voice_note

    def _refresh_clone_voice_entry(self) -> None:
        """Tier 33: Settings -> Voice names the cloned voice after its
        sample - show the sample that's set now."""
        panel = getattr(self, "settings_panel", None)
        if panel is not None and hasattr(panel, "refresh_clone_voice"):
            panel.refresh_clone_voice()

    def _persist_settings_key(self, key: str, value) -> None:
        """Updates one key in SETTINGS_FILE without needing settings_panel's
        widgets (save_settings() is widget-driven and can't carry a value
        that has no corresponding control) - a small read-modify-write,
        same file save_settings()/load_settings()/_read_settings_file()
        already share."""
        data = self._read_settings_file()
        data[key] = value
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            logger.error(f"_persist_settings_key('{key}') error: {e}")

    def _avatar_on_reply_started(self):
        if self.avatar_state_machine is not None:
            self.avatar_state_machine.on_reply_started()

    def _avatar_on_reply_done_without_speech(self):
        if self.avatar_state_machine is not None:
            self.avatar_state_machine.on_reply_done_without_speech()

    def _avatar_on_speech_started(self):
        if self.avatar_state_machine is not None:
            self.avatar_state_machine.on_speech_started()

    def _avatar_on_speech_stopped(self):
        if self.avatar_state_machine is not None:
            self.avatar_state_machine.on_speech_stopped()

    def _avatar_on_mic_mode_changed(self, mode: str):
        if self.avatar_state_machine is not None:
            self.avatar_state_machine.on_mic_mode_changed(mode)

    def stop_remote_dashboard(self):
        if self.remote_dashboard is not None:
            self.worker_pool.submit(self.remote_dashboard.stop)

    def regenerate_remote_pairing(self):
        if self.remote_dashboard is None:
            return
        self.remote_dashboard.regenerate_pairing()
        sp = getattr(self, "settings_panel", None)
        if sp and hasattr(sp, "refresh_remote_pairing_display"):
            sp.refresh_remote_pairing_display()
        if hasattr(self, "toast"):
            self.toast.show("New pairing code - every previously paired phone was signed out", "info")

    def remote_pairing_info(self):
        if self.remote_dashboard is None:
            return None
        return self.remote_dashboard.pairing_info()

    def clear_chat(self):
        self._cancel_reply()
        for i in reversed(range(self.chat_layout.count())):
            widget = self.chat_layout.itemAt(i).widget()
            if widget is not None:
                widget.deleteLater()
        self._chat_messages.clear()
        self._last_sender = None
        self.add_chat_bubble("Chat cleared. Fresh start!", is_user=False)
    
    def _bubble_max_width(self, text):
        """Code needs room - a bubble with a fenced block gets most of the
        chat's width, a plain one about two thirds of it."""
        has_code = "```" in (text or "")
        width_ratio = 0.88 if has_code else 0.68
        if not hasattr(self, "chat_scroll"):
            return 420
        return int(min(720, max(260, self.chat_scroll.width() * width_ratio)))

    def _scroll_chat_to_bottom(self, delay_ms: int = 80):
        if not hasattr(self, "chat_scroll"):
            return
        QTimer.singleShot(delay_ms, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()
        ))

    def add_chat_bubble(self, text, is_user=True, attachment=None):
        """Adds a message bubble and returns it (Tier 11: a streamed reply
        keeps the handle and grows it with ChatBubble.set_text())."""
        text = text or ""
        row_widget = QWidget()
        row_widget.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)

        max_w = self._bubble_max_width(text)
        bubble = ChatBubble(
            text, is_user=is_user, max_width=max_w,
            on_replay=(None if is_user else self.start_speaking),
            on_reaction=(None if is_user else self._on_bubble_reaction),
            theme=self.theme, attachment=attachment,
        )

        if is_user:
            row_layout.addStretch()
            row_layout.addWidget(bubble)
        else:
            row_layout.addWidget(bubble)
            row_layout.addStretch()

        self.chat_layout.addWidget(row_widget)
        self._chat_messages.append(bubble)
        self._last_sender = "user" if is_user else "ai"
        self._scroll_chat_to_bottom()
        return bubble

    def _on_bubble_reaction(self, reaction: str):
        self.toast.show(f"Reacted with {reaction}", "info")
        # Could send to backend
    
    def update_weather(self, weather_data):
        if hasattr(self, 'weather_lbl'):
            self.weather_lbl.setText(weather_data)

    def on_reminder_due(self, message: str, reminder_id: int):
        """Fired by ReminderWorker (already marked triggered - or
        rescheduled a day forward, for a recurring reminder - in the DB by
        the time this runs) once per reminder that's come due. Beeps,
        speaks it, shows a toast, and drops it into the chat log so there's
        a persistent record even if the user misses the beep and the toast.

        🔧 Tier 3 / item 8: `message` is now the already-composed, speakable
        text from reminders/composer.py (an LLM reword, or
        reminders/context.py's deterministic template if the LLM was off,
        unavailable, or too slow) rather than the raw saved string - it
        already reads as a complete sentence (e.g. "It's due now - call
        mom."), so the old hardcoded "Reminder: " prefix was dropped here
        to avoid stacking a second one in front of it. Everything else
        about this method is unchanged."""
        if HAS_WINSOUND:
            try:
                winsound.Beep(1000, 300)
            except Exception as e:
                logger.debug(f"Reminder beep failed (non-fatal): {e}")
        self.toast.show(f"⏰ {message}", "warning", duration=6000)
        self.add_chat_bubble(f"⏰ {message}", is_user=False)
        self.start_speaking(message)

    def on_reminder_upcoming(self, message: str, reminder_id: int, minutes_until: int):
        """🔧 Tier 3 / item 8: fired by ReminderWorker a few minutes before a
        reminder is due (config.REMINDER_UPCOMING_WINDOW_MIN), once per
        occurrence. Deliberately lighter than on_reminder_due - no beep,
        since this is a heads-up, not the alarm itself - same "ambient,
        not urgent" treatment _on_proactive_nudge below already uses."""
        self.toast.show(f"⏳ {message}", "info", duration=5000)
        self.add_chat_bubble(f"⏳ {message}", is_user=False)
        self.start_speaking(message)

    # 🔧 Tier 18: on_reminder_followup removed - ReminderWorker no longer
    # emits reminder_followup at all (see that class's own docstring in
    # ui/workers.py). The same post-due check-in now arrives through
    # _on_proactive_nudge below with kind="reminder_followup", styled via
    # _PROACTIVE_NUDGE_ICONS (🔁, matching this method's old icon exactly).

    def _on_proactive_nudge(self, kind: str, title: str, message: str):
        """Fired by ProactiveEngine (automation/proactive.py) when it notices
        something unprompted (battery low, a calendar event starting soon).
        Same toast + chat bubble + speak treatment as on_reminder_due above,
        minus the beep - an ambient nudge Wave decided to mention on its own
        should feel lighter than an alarm the user explicitly set.

        🔧 FIX (Tier 18): this used to be `"🔋" if kind == "battery" else
        "📅"` - meaning every kind OTHER than battery, including the
        already-shipped "download" kind, silently fell through to the
        calendar icon. _PROACTIVE_NUDGE_ICONS (module-level, near ICON_MAP
        below) now covers every kind ProactiveEngine actually emits, with
        📅 kept as the fallback for any future kind not yet added there -
        preserving today's observable behavior for an unmapped kind."""
        icon = _PROACTIVE_NUDGE_ICONS.get(kind, "📅")
        self.toast.show(f"{icon} {title}: {message}", "info", duration=6000)
        self.add_chat_bubble(f"{icon} {message}", is_user=False)
        self.start_speaking(message)

    def _refresh_health_dot(self):
        """Tier 19: the sidebar's health_dot_lbl reflects ONLY thread_supervisor's
        own watched entries (reminder/proactive/consolidation/skill-watcher/
        DB-writer threads) - not the one-time startup health check or
        Ollama (both of those are toasts; see _on_health_check_done and
        _on_ollama_event). Re-derives the whole picture from
        thread_supervisor.status() fresh on every call rather than tracking
        state incrementally itself, so it can never drift from the truth -
        status() is already cheap (an is_alive() per watched entry)."""
        status = thread_supervisor.status()
        gave_up = [name for name, info in status.items() if info["gave_up"]]
        recovered = [name for name, info in status.items() if not info["gave_up"] and info["restart_count"] > 0]
        if gave_up:
            color, text = self.theme.danger, f"●  {len(gave_up)} WORKER{'S' if len(gave_up) != 1 else ''} DOWN"
            tip = "Stopped for good this session (hit the restart cap): " + ", ".join(gave_up)
            if recovered:
                tip += "\nRecovered after a restart: " + ", ".join(recovered)
        elif recovered:
            color, text = self.theme.warning, "●  RECOVERED FROM A CRASH"
            tip = "Restarted after crashing, running fine now: " + ", ".join(recovered)
        else:
            color, text = self.theme.text_muted, "●  SYSTEMS OK"
            tip = "All background workers running normally."
        self.health_dot_lbl.setText(text)
        self.health_dot_lbl.setStyleSheet(f"color: {color}; font-size: 10.5px; font-weight: 700; background: transparent;")
        self.health_dot_lbl.setToolTip(tip)

    def _on_ollama_event(self, event: str, message: str):
        """Tier 19: brain/ollama_lifecycle.py's own listener, hopped to the
        GUI thread via the ollama_event signal (see its declaration). Just
        a toast - this is one-shot startup activity, not something
        continuously monitored like thread_supervisor's watched entries
        (see _refresh_health_dot), so there's no persistent indicator to
        keep in sync here."""
        if event == "problem":
            self.toast.show(f"⚠️ Ollama: {message}", "warning", duration=6000)
        elif event in ("server_started", "model_ready"):
            self.toast.show(f"🦙 {message}", "success")
        # "already_running" / "model_pulling" are routine, expected states,
        # not something worth interrupting the user for - still logged by
        # ollama_lifecycle itself either way.

    def _on_health_check_done(self, results):
        """Tier 19: the roadmap's "each reported loudly in the UI" - one
        toast per non-OK result (never for an OK one - a clean bill of
        health for five checks every single startup would just be noise),
        so a broken mic or an unreachable LLM provider is something the
        user actually sees instead of a line in a log file they'd only
        open after already noticing voice doesn't work."""
        for r in results:
            if r.status is Status.PROBLEM:
                self.toast.show(f"⚠️ {r.name}: {r.message}", "error", duration=8000)
            elif r.status is Status.WARN:
                self.toast.show(f"{r.name}: {r.message}", "warning", duration=6000)

    def _run_scheduled_workflow(self, workflow_name: str):
        """Tier 18: the callback automation/proactive.py's
        ProactiveEngine.set_workflow_runner() was wired up with above - it
        calls this from ITS OWN background thread (not the GUI thread), so
        this must only ever do thread-safe hand-offs, never touch widgets
        directly. Mirrors ui/workflows_panel.py's _run_workflow/
        _on_run_finished exactly (same worker_pool.submit of
        brain._dispatch_tool, same chat-bubble-with-the-result finish) -
        source="proactive" instead of "ui" is the only real difference, so
        the audit trail honestly shows this run wasn't button-clicked."""
        worker = self.worker_pool.submit(
            self.brain._dispatch_tool, "run_workflow", {"name": workflow_name}, "proactive")
        worker.signals.result.connect(lambda result: self.add_chat_bubble(str(result), is_user=False))
        worker.signals.error.connect(
            lambda e: self.add_chat_bubble(f"Scheduled run of '{workflow_name}' failed: {e}", is_user=False))

    def turn_on_mic(self):
        self.mic_active = True
        self.mic_worker = ContinuousMicWorker(
            wake_phrases=getattr(self, "wake_phrases", None)
        )
        self.mic_worker.text_ready.connect(self.process_voice_input)
        # Tier 11: mode_changed also says whether to announce the change -
        # False when the wake word came with a command in the same breath
        # ("Hey Wave, open Chrome"), so Wave doesn't say "Systems active."
        # on top of its own answer. (mode_update is still emitted for any
        # other listener; it just doesn't drive this any more.)
        self.mic_worker.mode_changed.connect(self.handle_wake_word_mode)
        self.mic_worker.interruption_signal.connect(self._on_barge_in)
        self.mic_worker.stop_requested.connect(self._on_voice_stop)
        self.mic_worker.audio_level.connect(self._on_mic_level)
        self.mic_worker.recognition_failed.connect(self._on_recognition_failed)
        # Tier 12: live captions while you talk, cleared when listen() returns.
        self.mic_worker.partial_text.connect(self._on_partial_text)
        self.mic_worker.utterance_done.connect(self._clear_caption)
        self.mic_worker.start()

    # ── Tier 12: live captions ──────────────────────────────────────────
    # A rough transcript of what you're saying, shown WHILE you say it (local
    # Whisper + the continuous mic stream only - see voice/stt.py). One faint
    # bubble, replaced by the real message when you stop; cleared on its own
    # if nothing follows (an ignored or unrecognised utterance).
    CAPTION_CLEAR_MS = 4000
    CAPTION_STYLE = ("color: rgba(150, 160, 180, 0.95); font-style: italic; padding: 6px 12px;"
                     "border: 1px dashed rgba(140, 150, 170, 0.55); border-radius: 12px;"
                     "background: transparent;")

    def _on_partial_text(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if time.monotonic() - self._caption_closed_at < 0.5:
            return      # a straggler from the utterance that just ended
        label = self._caption_label
        if label is None:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            label = QLabel()
            label.setObjectName("liveCaption")
            label.setWordWrap(True)
            label.setStyleSheet(self.CAPTION_STYLE)
            lay.addStretch()
            lay.addWidget(label)
            self.chat_layout.addWidget(row)
            self._caption_row, self._caption_label = row, label
        try:
            label.setMaximumWidth(self._bubble_max_width(text))
        except Exception:
            pass
        label.setText("🎙 " + text + " …")
        self._caption_seq += 1
        seq = self._caption_seq
        QTimer.singleShot(self.CAPTION_CLEAR_MS, lambda: self._clear_caption(seq))
        self._scroll_chat_to_bottom()

    def _clear_caption(self, seq=None):
        """Removes the live caption. With `seq`, only if no newer caption
        text arrived since that timer was set."""
        if seq is not None and seq != self._caption_seq:
            return
        if seq is None:
            self._caption_closed_at = time.monotonic()
        row = self._caption_row
        self._caption_row = None
        self._caption_label = None
        if row is None:
            return
        try:
            self.chat_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        except Exception:
            pass

    def _on_recognition_failed(self):
        """Heard audio but couldn't recognize it (unclear speech, no
        internet, or the free speech-recognition service being unreachable) -
        this used to fail completely silently, leaving no way to tell 'the
        mic didn't hear anything' apart from 'recognition is broken'."""
        if hasattr(self, "toast"):
            self.toast.show("Didn't catch that - try again?", "warning")

    def _on_mic_level(self, level: float):
        """Tier 11: the real microphone level drives the waveform (it used
        to be random bars)."""
        vis = getattr(self, "visualizer", None)
        if vis is not None and hasattr(vis, "set_level"):
            vis.set_level(level)

    def _on_barge_in(self):
        """The mic heard speech addressed to Wave while Wave was talking (see
        voice/commands.py's barge-in gate): stop talking AND drop the reply
        in flight. What was said arrives right after as text_ready /
        stop_requested and is handled there."""
        self._cancel_reply()
        self.stop_speaking()

    def _on_voice_stop(self):
        """Tier 11: "stop" / "ruk jao" / "रुको" / "never mind" - ends the turn
        in flight (speech, the streaming reply, and any tool it was about to
        run) and drops a pending yes/no confirmation, so a later "yes" can't
        fire something the user just called off."""
        had_reply = self._cancel_reply()
        was_speaking = bool(getattr(tts_engine, "is_speaking", False))
        self.stop_speaking()
        ttfa.tracker.mark("audio_stopped")
        cancelled_confirmation = False
        cancel = getattr(getattr(self, "brain", None), "cancel_pending_confirmation", None)
        if callable(cancel):
            try:
                cancelled_confirmation = bool(cancel())
            except Exception as e:
                logger.warning(f"Couldn't cancel a pending confirmation: {e}")
        if ttfa.tracker.active_source() == "stop":
            ttfa.tracker.finish("ok", stopped_reply=had_reply or None, was_speaking=was_speaking or None)
        if not hasattr(self, "toast"):
            return
        if cancelled_confirmation:
            self.toast.show("Okay, cancelled - I won't do that.", "info")
        elif had_reply or was_speaking:
            self.toast.show("⏹ Stopped", "info")

    def handle_wake_word_mode(self, mode, announce: bool = True):
        self._avatar_on_mic_mode_changed(mode)
        if mode == "AWAKE":
            style = (
                f"border-radius: 21px; font-size: 15px; padding: 0px; "
                f"background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {self.theme.accent_2}, stop:1 {self.theme.accent_1}); "
                f"color: #FFFFFF; border: none;"
            )
            self.btn_mic_inline.setStyleSheet(style)
            glow_color = QColor(self.theme.accent_1)
            glow_color.setAlpha(150)
            add_shadow(self.btn_mic_inline, color=glow_color, blur=26, y_offset=0)
            self.mic_mini_lbl.setText("●  LISTENING")
            self.mic_mini_lbl.setStyleSheet(f"color: {self.theme.accent_1}; font-size: 10.5px; font-weight: 700; background: transparent;")
            self.visualizer.set_mode("LISTENING")
            self._set_status("Listening...", self.theme.accent_1)
            if announce:
                self.start_speaking("Systems active.")
        else:
            style = (
                f"border-radius: 21px; font-size: 15px; padding: 0px; "
                f"background-color: {self.theme.bg_surface}; color: {self.theme.accent_1}; border: 1px solid {self.theme.border};"
            )
            self.btn_mic_inline.setStyleSheet(style)
            self.btn_mic_inline.setGraphicsEffect(None)
            self.mic_mini_lbl.setText("●  STANDBY")
            self.mic_mini_lbl.setStyleSheet(f"color: {self.theme.text_muted}; font-size: 10.5px; font-weight: 700; background: transparent;")
            self.visualizer.set_mode("SLEEPING")
            self._set_status("Online", self.theme.success)
            if announce:
                self.start_speaking("Standing by.")

    def _show_idle_voice_state(self):
        if self.mic_worker and self.mic_worker.mode == "AWAKE":
            self.visualizer.set_mode("LISTENING")
            self._set_status("Listening...", self.theme.accent_1)
        else:
            self.visualizer.set_mode("SLEEPING")
            self._set_status("Online", self.theme.success)

    def stop_speaking(self):
        tts_engine.stop()
        self._show_idle_voice_state()

    def start_speaking(self, text):
        self.stop_speaking()
        self.visualizer.set_mode("SPEAKING")
        self._set_status("Speaking...", self.theme.accent_3)
        self._avatar_on_speech_started()
        # Tier 19: best-effort - a reminder/proactive nudge speaking outside
        # any voice/typed turn has no ttfa turn active, and that's fine;
        # trace.correlate(None) inside TTSWorker.run() is just a no-op then.
        self.tts_thread = TTSWorker(text, turn_id=ttfa.tracker.active_id())
        self.tts_thread.finished_speaking.connect(self.on_speech_finished)
        self.tts_thread.start()

    def on_speech_finished(self, success: bool = True):
        # 🔧 FIX: finished_speaking used to fire identically whether
        # playback actually worked or silently failed end-to-end (no
        # sounddevice, pygame mixer unable to play anything, offline
        # fallbacks also unavailable) - see ui/workers.py/voice/tts.py.
        if not success and hasattr(self, "toast"):
            self.toast.show("Couldn't play voice reply - check your audio output device", "warning")
        # Tier 11: a "Hey Wave" -> "Systems active." turn ends here.
        if ttfa.tracker.active_source() == "wake":
            ttfa.tracker.finish("ok" if success else "no_audio")
        self._show_idle_voice_state()
        self._avatar_on_speech_stopped()

    def process_voice_input(self, text):
        # The mic thread already opened this utterance's time-to-first-audio
        # turn (it alone has the capture timestamps); send_message() picks
        # it up instead of opening a "typed" one.
        self._voice_ttfa_turn = ttfa.tracker.active_id()
        self.chat_input.setPlainText(text)
        self.send_message()

    def toggle_mic(self):
        if hasattr(self, 'mic_worker') and self.mic_worker:
            if self.mic_worker.mode == "AWAKE":
                self.mic_worker.set_mode("SLEEPING")
            else:
                self.mic_worker.set_mode("AWAKE")

    # ==================== AI NLP BRAIN ====================
    # Shown in a reply bubble until its first text arrives, and appended to
    # one whose turn was stopped part-way.
    REPLY_PLACEHOLDER = "…"
    REPLY_STOPPED_MARK = "⏹ *Stopped*"
    # Live markdown re-render of a streaming bubble is throttled to this.
    STREAM_RENDER_MS = 60

    def _set_send_button_stop_mode(self, is_stop: bool):
        """Tier 19: the send button doubles as Stop while a reply is
        streaming - flipped on right as a worker starts (_start_reply) and
        back off the moment there's no reply in flight any more, whether
        that's because it finished normally (_on_reply_done) or was
        cancelled (_cancel_reply)."""
        btn = getattr(self, "btn_send", None)
        if btn is None:
            return
        if is_stop:
            btn.setToolTip("Stop")
            Icon.set_btn_icon(btn, "stop", "#000000", 16)
        else:
            btn.setToolTip("Send (Enter)")
            Icon.set_btn_icon(btn, "send", "#000000", 20)

    def _on_send_btn_clicked(self):
        """Dispatches the one physical button to whichever action its
        current icon promises - Send when idle, Stop while a reply (and
        nothing else - see _set_send_button_stop_mode) is in flight."""
        if getattr(self, "_reply_worker", None) is not None:
            self._on_stop_button_clicked()
        else:
            self.send_message()

    def _on_stop_button_clicked(self):
        """Clicking Stop has the same effect as the voice "stop" command's
        cancellation (_on_voice_stop) - minus that command's voice-latency
        bookkeeping (ttfa's "stop"-source audio_stopped mark), which has no
        equivalent for a mouse click that never started a voice turn."""
        had_reply = self._cancel_reply()
        was_speaking = bool(getattr(tts_engine, "is_speaking", False))
        self.stop_speaking()
        if (had_reply or was_speaking) and hasattr(self, "toast"):
            self.toast.show("⏹ Stopped", "info")

    def send_message(self):
        user_text = self.chat_input.toPlainText().strip()
        attachment = self._pending_attachment
        ttfa_turn, self._voice_ttfa_turn = self._voice_ttfa_turn, None
        if not user_text and not attachment:
            return
        # A new message supersedes whatever reply is still in flight.
        self._cancel_reply()
        self.stop_speaking()

        self.add_chat_bubble(user_text, is_user=True, attachment=attachment)
        self.chat_input.clear()
        self._clear_attachment()

        # Tier 11: the old keyword shortcuts that used to sit here (WhatsApp,
        # notepad, screenshot, open/close/kill <app>, wifi/bluetooth
        # toggles) are gone. They called AppController directly, so they
        # skipped the risk-tier confirm gate - "send hi to mom on whatsapp"
        # sent at once, "close chrome" closed without asking - and the audit
        # log and undo. Every message now goes to the brain, whose fast path
        # (brain/intent_router.py) answers the same commands without an LLM
        # round-trip, and whose no-provider fallback handles them offline.
        if ttfa_turn is None:
            ttfa_turn = ttfa.tracker.begin("typed", heard=user_text)
        ttfa.tracker.mark("dispatched", turn_id=ttfa_turn)
        # Tier 19: tags the GUI thread's own log lines for this dispatch -
        # AIStreamWorker adopts the same ttfa_turn id on its own thread
        # (see ui/workers.py's AIStreamWorker.run()), so the two threads'
        # log lines for one turn share an id despite never sharing a stack.
        with trace.correlate(ttfa_turn):
            self._start_reply(user_text, attachment=attachment, ttfa_turn=ttfa_turn)

    def _start_reply(self, user_text, attachment=None, ttfa_turn=None):
        """Streams the brain's answer into a new bubble and speaks it as it
        arrives (voice/tts.py's pipeline) - AIStreamWorker does the work."""
        self._set_status("Thinking...", self.theme.warning)
        self._avatar_on_reply_started()
        bubble = self.add_chat_bubble(self.REPLY_PLACEHOLDER, is_user=False)
        worker = AIStreamWorker(self.brain, user_text, attachment=attachment,
                                speak_reply=True, ttfa_turn=ttfa_turn)
        worker.view = _ReplyView(bubble)
        worker.text_chunk.connect(partial(self._on_reply_chunk, worker))
        worker.started_speaking.connect(self._avatar_on_speech_started)
        worker.response_ready.connect(partial(self._on_reply_done, worker))
        worker.finished_speaking.connect(partial(self._on_reply_speech_finished, worker))
        self._reply_worker = worker
        self._set_send_button_stop_mode(True)
        self._track_worker(worker)
        worker.start()
        return worker

    def _cancel_reply(self) -> bool:
        """Cancels the streaming reply in flight, if any (its bubble keeps
        what was shown, marked as stopped). Returns whether there was one."""
        worker, self._reply_worker = getattr(self, "_reply_worker", None), None
        if worker is None:
            return False
        self._set_send_button_stop_mode(False)
        try:
            worker.cancel()
        except Exception as e:
            logger.warning(f"Couldn't cancel the reply in flight: {e}")
        return True

    def _on_reply_chunk(self, worker, piece: str):
        view = getattr(worker, "view", None)
        if view is None or view.done or not piece:
            return
        first = not view.text
        view.text += piece
        if first and worker is self._reply_worker:
            if worker.speak_reply:
                self.visualizer.set_mode("SPEAKING")
                self._set_status("Speaking...", self.theme.accent_3)
            else:
                self._set_status("Replying...", self.theme.accent_3)
        if not view.render_pending:
            view.render_pending = True
            QTimer.singleShot(self.STREAM_RENDER_MS, partial(self._render_reply, worker))

    def _render_reply(self, worker):
        view = worker.view
        view.render_pending = False
        if not view.done:
            self._apply_reply_text(view, view.text or self.REPLY_PLACEHOLDER)

    def _apply_reply_text(self, view, text: str):
        # Follow the reply down only if the user hadn't scrolled up to read
        # something else.
        follow = True
        bar = getattr(getattr(self, "chat_scroll", None), "verticalScrollBar", None)
        if callable(bar):
            try:
                sb = bar()
                follow = sb.value() >= sb.maximum() - 48
            except Exception:
                follow = True
        try:
            view.bubble.set_text(text, max_width=self._bubble_max_width(text))
        except RuntimeError:
            return   # the bubble was deleted (chat cleared) while the reply streamed
        if follow:
            self._scroll_chat_to_bottom(delay_ms=30)

    def _on_reply_done(self, worker, full_text: str):
        view = getattr(worker, "view", None)
        if view is None:
            return
        view.done = True
        text = full_text or view.text
        if worker.cancelled:
            text = (text.rstrip() + "\n\n" if text.strip() else "") + self.REPLY_STOPPED_MARK
        elif not text.strip():
            text = "(no reply)"
        view.text = text
        self._apply_reply_text(view, text)
        if worker is self._reply_worker:
            self._reply_worker = None
            self._set_send_button_stop_mode(False)
            if not worker.speak_reply:
                self._show_idle_voice_state()
        # Tier 25: a no-op unless this turn's state is still THINKING - the
        # normal speak_reply=True path already settled it via
        # _avatar_on_speech_stopped() below, well before response_ready
        # fires (see ui/workers.py's AIStreamWorker._run_inner() for the
        # exact emission order). This only matters for speak_reply=False.
        self._avatar_on_reply_done_without_speech()

    def _on_reply_speech_finished(self, worker, success: bool):
        if worker.ttfa_turn is not None:
            ttfa.tracker.finish("interrupted" if worker.cancelled else ("ok" if success else "no_audio"),
                                turn_id=worker.ttfa_turn)
        if worker is self._reply_worker or self._reply_worker is None:
            if not success and hasattr(self, "toast"):
                self.toast.show("Couldn't play voice reply - check your audio output device", "warning")
            self._show_idle_voice_state()
        self._avatar_on_speech_stopped()

    def _plain_text_for_speech(self, text: str) -> str:
        """What of a finished reply gets read aloud - no code blocks, no table
        pipes (Tier 11: one rule set, shared with streamed replies - see
        voice/speech_filter.py; emphasis/heading markers are stripped by
        voice/tts.py's clean_for_speech)."""
        return speech_filter.for_speech(text)

    def display_ai_response(self, response: str):
        """For results that arrive as one finished string (the direct
        actions behind dashboard buttons, OCR) rather than a streamed turn."""
        self.add_chat_bubble(response, is_user=False)
        self.start_speaking(self._plain_text_for_speech(response))
    
    # ==================== TIMERS ====================
    def setup_timers(self):
        self.sys_timer = QTimer()
        self.sys_timer.timeout.connect(self.update_sys_stats)
        self.sys_timer.start(1000)
        
        # Clock timer
        self.clock_timer = QTimer()
        self.clock_timer.timeout.connect(self._update_clock)
        self.clock_timer.start(1000)
        self._update_clock()
    
    def _update_clock(self):
        if self.isMinimized() or not self.isVisible():
            return      # Wave lives minimised in the tray; nobody is looking at the clock
        if hasattr(self, "time_lbl"):
            self.time_lbl.setText(time.strftime("%I:%M:%S %p"))
            self.date_lbl.setText(time.strftime("%d %b %Y").upper())
    
    def update_sys_stats(self):
        if self.isMinimized() or not self.isVisible():
            return      # skip all the psutil work + repaints while nobody can see the gauges
        stats = SystemMonitor.get_stats()
        if stats:
            cpu = stats.get('cpu_percent', 0) or 0
            ram = stats.get('ram_percent', 0) or 0
            bat = stats.get('battery_percent', 0)

            # Update gauges + sparklines
            for gauge_attr, detail_attr, spark_attr, val, fmt in (
                ("cpu_gauge", "cpu_detail_lbl", "cpu_spark", cpu, f"{cpu:.0f}% load"),
                ("ram_gauge", "ram_detail_lbl", "ram_spark", ram, f"{stats.get('ram_used_gb', '—')}/{stats.get('ram_total_gb', '—')} GB"),
                ("disk_gauge", "disk_detail_lbl", "disk_spark", None, None),
            ):
                for suffix in ("", "_big"):
                    g = getattr(self, gauge_attr + suffix, None)
                    d = getattr(self, detail_attr + suffix, None)
                    s = getattr(self, spark_attr + suffix, None)
                    if g is not None:
                        g.setValue(val)
                    if d is not None:
                        d.setText(fmt)
                    if s is not None and val is not None:
                        s.add_value(val)

            # Battery
            if isinstance(bat, (int, float)):
                if bat <= 20:
                    bcolor = self.theme.danger
                elif bat <= 50:
                    bcolor = self.theme.warning
                else:
                    bcolor = self.theme.success
                bat_text = "Plugged In" if stats.get('is_plugged') else "On Battery"
            else:
                bat, bcolor, bat_text = 0, self.theme.text_faint, "No Battery"

            for suffix in ("", "_big"):
                g = getattr(self, "bat_gauge" + suffix, None)
                d = getattr(self, "bat_detail_lbl" + suffix, None)
                s = getattr(self, "bat_spark" + suffix, None)
                if g is not None:
                    g.setValue(bat if isinstance(bat, (int, float)) else 0)
                    g.setColor(bcolor)
                if d is not None:
                    d.setText(bat_text)
                if s is not None:
                    s.add_value(bat if isinstance(bat, (int, float)) else 0)

        try:
            disk = psutil.disk_usage(os.path.abspath(os.sep))
            for suffix in ("", "_big"):
                g = getattr(self, "disk_gauge" + suffix, None)
                d = getattr(self, "disk_detail_lbl" + suffix, None)
                s = getattr(self, "disk_spark" + suffix, None)
                if g is not None:
                    g.setValue(disk.percent)
                if d is not None:
                    d.setText(f"{disk.free / (1024**3):.1f} GB free")
                if s is not None:
                    s.add_value(disk.percent)
        except Exception:
            pass

        current_net_io = psutil.net_io_counters()
        now = time.monotonic()
        elapsed = max(0.25, now - getattr(self, "_last_net_ts", now - 1.0))   # real seconds since the last reading
        dl_speed = (current_net_io.bytes_recv - self.last_net_io.bytes_recv) / 1024 / 1024 / elapsed
        ul_speed = (current_net_io.bytes_sent - self.last_net_io.bytes_sent) / 1024 / 1024 / elapsed
        self.last_net_io = current_net_io
        self._last_net_ts = now

        for attr, txt in (("dl_lbl", f"⬇ DL: {dl_speed:.2f} MB/s"), ("ul_lbl", f"⬆ UL: {ul_speed:.2f} MB/s"),
                           ("dl_lbl_big", f"⬇ DL: {dl_speed:.2f} MB/s"), ("ul_lbl_big", f"⬆ UL: {ul_speed:.2f} MB/s")):
            w = getattr(self, attr, None)
            if w is not None:
                w.setText(txt)
        
        # Network sparklines
        if hasattr(self, "net_dl_spark"):
            self.net_dl_spark.add_value(min(100, dl_speed * 10))
        if hasattr(self, "net_ul_spark"):
            self.net_ul_spark.add_value(min(100, ul_speed * 10))

    def closeEvent(self, event):
        # Tier 19: tells _on_reminder_worker_finished this shutdown is
        # deliberate, so it doesn't try to recreate the worker this
        # closeEvent is about to stop. Set before anything is actually
        # stopped below.
        self._shutting_down = True
        # Stop the thread supervisor itself next - otherwise it could
        # "helpfully" restart one of the singletons below in the middle of
        # this deliberate shutdown, racing the very stop() calls that
        # follow (see automation/thread_supervisor.py's own docstring).
        thread_supervisor.stop()
        try:
            get_loader().stop()
        except Exception:
            pass
        # Cleanup workers
        self._cancel_reply()
        try:
            tts_engine.stop()
        except Exception:
            pass
        self.worker_pool.shutdown()
        if self.mic_worker:
            self.mic_worker.stop()
        try:
            stt_engine.close()       # Tier 12: release the microphone stream now
        except Exception:
            pass
        if self.weather_worker:
            self.weather_worker.quit()
        if self.clip_worker:
            self.clip_worker.stop()
        if self.media_info_worker:
            self.media_info_worker.stop()
        if self.reminder_worker:
            self.reminder_worker.stop()
        proactive_engine.stop()
        memory_consolidator.stop()
        # Tier 19: give the action log's background writer a bounded
        # chance to flush whatever accumulated during this whole shutdown
        # sequence before the process actually exits - see Audit.shutdown's
        # own docstring for why this is best-effort, not a guarantee.
        audit_mod.audit.shutdown()
        # Tier 19: only actually stops anything if THIS session started
        # `ollama serve` itself (see brain/ollama_lifecycle.py's shutdown()
        # docstring) - a no-op, same as the checks above, when there's
        # nothing of this app's own left to clean up.
        ollama_lifecycle.shutdown()
        if HAS_KEYBOARD:
            keyboard.unhook_all()
        # Tier 23: stop the dashboard's uvicorn thread if it was ever
        # started - a no-op (self.remote_dashboard is still None) on the
        # overwhelmingly common case where the feature was never touched.
        if self.remote_dashboard is not None:
            try:
                self.remote_dashboard.stop()
            except Exception:
                pass
        super().closeEvent(event)


# =============================================================================
# 📦 WORKER CLASSES moved to ui/workers.py (WeatherWorker, TTSWorker,
# ContinuousMicWorker, AIWorker, ClipboardWorker, ActionWorker, OCRWorker,
# MediaInfoWorker, MediaSeekWorker) - imported at the top of this file.
# Behavior is unchanged, only the location moved.
# ReminderWorker (Phase 2) was added directly to ui/workers.py, alongside them.
# =============================================================================

# Tier 18: icon per ProactiveEngine nudge `kind` - see _on_proactive_nudge's
# own 🔧 FIX comment above for why this exists. Covers every kind
# automation/proactive.py's ProactiveNudge.kind can actually be (see that
# module's own _PROACTIVE_KINDS tuple).
_PROACTIVE_NUDGE_ICONS = {
    "battery": "🔋", "calendar": "📅", "download": "📥", "workflow": "▶️",
    "reminder_followup": "🔁", "morning_briefing": "☀️", "idle_break": "🧘",
    "streak": "🔥", "habit": "💡", "low_disk": "💾",
}

# Icon name mapping for DashboardTile
ICON_MAP = {
    "house": "🏠", "message": "💬", "chart-line": "📊", "bolt": "⚡",
    "music": "🎵", "folder": "📁", "eye": "👁", "clipboard": "📋", "clock-rotate-left": "🕘", "gear": "⚙",
}