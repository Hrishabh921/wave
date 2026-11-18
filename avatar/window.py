"""
avatar/window.py — Tier 25: the floating, always-on-top avatar widget.

A frameless, draggable, translucent QWidget - the thing actually visible
when Wave starts (the full dashboard, ui/main_window.py's MainWindow,
stays hidden until asked for - see avatar/control.py's show_ui/hide_ui).
Renders the current frame from avatar/rig.py's output on a QTimer,
picked by avatar/state.py's AvatarStateMachine:

  IDLE      - "neutral", with an occasional "blink" and a slow
              left/right sway once settled in for a few seconds.
  LISTENING - "neutral" held steady - no sway, so it reads as attentive
              rather than bored, but no blink suppression either (a
              listening face still blinks).
  THINKING  - "neutral" with a faster, smaller sway - a "pondering" tell.
  SPEAKING  - alternates "neutral"/"mouth_open" on a fixed cadence: the
              "light" tier's mouth-flap, not amplitude-driven lip sync -
              see avatar/state.py's own docstring for why that tradeoff
              was made (and ui/workers.py's started_speaking signal /
              webui/voice_worker.py's on_speech_started callback for
              where this window's state machine actually gets fed from).

Qt-dependent, and NOT unit-tested the way state.py/rig.py are - this
project's own established pattern for UI-layer code (ui/main_window.py
itself has no direct unit tests either) is to verify real Qt widgets by
hand on a real display, which this sandbox doesn't have (same constraint
Tier 23/24 already documented). What CAN be unit-tested without a
QApplication at all is kept in frame_for_state() below, a plain function
with no Qt/widget dependency - every actual "which frame right now"
decision lives there, specifically so it's covered even though
AvatarWindow itself isn't.
"""
import logging
import os
import random
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QPixmap
from PyQt6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget

from avatar import rig
from avatar.animation import (
    BLINK_DURATION_S, BLINK_MIN_INTERVAL_S, BLINK_MAX_INTERVAL_S, FRAME_INTERVAL_MS, frame_for_state,
)

logger = logging.getLogger("AvatarWindow")


class AvatarWindow(QWidget):
    """One instance, created once at startup (see avatar/control.py) and
    kept alive for the whole run - hiding it (the "Hide avatar" menu
    action, or nothing to show yet - see has_rig()) just hides the
    widget, it doesn't tear the timer/state machine down."""

    def __init__(self, rig_dir: str, state_machine, parent=None):
        super().__init__(parent)
        self.state_machine = state_machine
        self._rig_dir = rig_dir
        self._pixmaps = {}
        self._next_blink_at = 0.0
        self._drag_offset = None
        self._on_show_ui_requested = lambda: None
        self._load_rig()
        self._build_ui()
        self._schedule_next_blink()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(FRAME_INTERVAL_MS)

    def has_rig(self) -> bool:
        """False if avatar/rig.py's build_rig() hasn't been run yet (no
        photo set up) - callers (avatar/control.py) check this before
        showing the window at all, so a fresh install without a photo
        doesn't pop an empty/broken-looking widget on startup."""
        return bool(self._pixmaps)

    def _load_rig(self):
        manifest = rig.load_manifest(self._rig_dir)
        if not manifest:
            logger.info(f"No avatar rig built yet under '{self._rig_dir}'.")
            return
        for name, filename in manifest.get("frames", {}).items():
            pm = QPixmap(os.path.join(self._rig_dir, filename))
            if not pm.isNull():
                self._pixmaps[name] = pm
            else:
                logger.warning(f"Avatar rig frame '{filename}' failed to load.")

    def _build_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._label = QLabel(self)
        self._label.setScaledContents(True)
        layout.addWidget(self._label)
        self._apply_frame("neutral")
        screen = QGuiApplication.primaryScreen()
        if screen is not None and not self.size().isEmpty():
            geo = screen.availableGeometry()
            self.move(geo.right() - self.width() - 24, geo.bottom() - self.height() - 24)

    def set_on_show_ui_requested(self, callback):
        """Wired by avatar/control.py to MainWindow.show()/raise_() -
        kept as an injected callback rather than an import, so this
        module has no dependency on ui.main_window at all."""
        self._on_show_ui_requested = callback or (lambda: None)

    def _apply_frame(self, name: str):
        pm = self._pixmaps.get(name) or self._pixmaps.get("neutral")
        if pm is None:
            return
        self._label.setPixmap(pm)
        self.resize(pm.size())

    def _schedule_next_blink(self):
        self._next_blink_at = time.monotonic() + random.uniform(BLINK_MIN_INTERVAL_S, BLINK_MAX_INTERVAL_S)

    def _tick(self):
        now = time.monotonic()
        sm = self.state_machine
        name = frame_for_state(sm.state, sm.seconds_in_state(), now, self._next_blink_at)
        if name != "blink" and now >= self._next_blink_at + BLINK_DURATION_S:
            self._schedule_next_blink()
        self._apply_frame(name)

    # ---- window chrome: draggable by left-click, right-click menu -----
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Show Wave UI", self._on_show_ui_requested)
        menu.addAction("Hide avatar", self.hide)
        menu.addSeparator()
        app = QGuiApplication.instance()
        menu.addAction("Quit Wave", app.quit if app else (lambda: None))
        menu.exec(event.globalPos())
