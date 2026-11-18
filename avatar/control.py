"""
avatar/control.py — Tier 25: the one Qt-dependent seam between WaveBrain's
skill dispatch (brain/handlers/avatar.py's show_ui/hide_ui, called
synchronously from whichever thread is running the turn - ui/workers.py's
AIStreamWorker is a QThread, NOT the GUI thread that owns MainWindow/
AvatarWindow) and the two real windows: MainWindow (ui/main_window.py,
the full dashboard) and AvatarWindow (avatar/window.py, the floating
companion). Both are QWidgets, so they may only be touched from the GUI
thread that created them - this module is what makes that safe without
brain/ needing to know or care which thread it's running on.

AvatarController is constructed exactly once, on the GUI thread, by
MainWindow's own startup (see ui/main_window.py's _init_avatar()) right
after both windows exist, and registered via set_active_controller() -
which, along with get_active_controller(), actually LIVES in
avatar/registry.py (re-exported below purely for convenience) and not
here, specifically so brain/handlers/avatar.py can import that one
Qt-free module instead of this one - this module wraps QObject/
QMetaObject, so importing IT would make PyQt6 a transitive hard
dependency of brain/, which must stay importable with no display or
PyQt6 at all (see avatar/registry.py's own docstring for the full story
of why this split exists). brain/handlers/avatar.py only ever touches
get_active_controller()/the two methods below - never MainWindow or
AvatarWindow directly.

show_ui()/hide_ui() are the two methods a skill call is allowed to use.
Both are safe to call from ANY thread: already on the GUI thread (the
avatar window's own right-click "Show Wave UI" menu action, which fires
synchronously on the GUI thread that delivered the click), they run
immediately; from anywhere else (a skill dispatched on an AIStreamWorker/
WebAIStreamWorker thread) they're marshalled onto the GUI thread via
QMetaObject.invokeMethod(..., BlockingQueuedConnection), so the string the
skill hands back to the user ("Dashboard khol diya.") is only returned
once the window has actually been shown/hidden - never a fire-and-forget
guess about what's about to happen.

Not unit-tested here, the same documented reason avatar/window.py isn't:
this sandbox has no PyQt6 installed at all. What IS worth testing without
Qt - brain/handlers/avatar.py's two skills - is tested against a tiny fake
object standing in for an AvatarController (duck-typed: anything with
show_ui()/hide_ui() methods works, this class included), in
tests/test_tier25_avatar_skills.py.
"""
import logging

from PyQt6.QtCore import QMetaObject, QObject, Qt, QThread, pyqtSlot
from PyQt6.QtWidgets import QApplication

from avatar.registry import set_active_controller, get_active_controller  # noqa: F401  (re-exported)

logger = logging.getLogger("AvatarController")


class AvatarController(QObject):
    """Owns the show/hide policy for Wave's two surfaces once an avatar
    rig exists: the avatar window (default-visible) and the full
    MainWindow dashboard (default-hidden, see ui/main_window.py's
    should_start_hidden()). main_window/avatar_window are the
    already-constructed, already-on-the-GUI-thread widgets; this class
    never constructs either one itself - MainWindow._init_avatar() does,
    then hands both to this constructor."""

    def __init__(self, main_window, avatar_window):
        super().__init__()
        self.main_window = main_window
        self.avatar_window = avatar_window
        self._last_result = ""
        avatar_window.set_on_show_ui_requested(self.show_ui)

    def _on_gui_thread(self) -> bool:
        app = QApplication.instance()
        return app is None or QThread.currentThread() is app.thread()

    def show_ui(self) -> str:
        """Shows the full dashboard - the avatar window is left exactly
        as it is (still running, still visible), this only concerns the
        separate MainWindow."""
        if self._on_gui_thread():
            return self._show_ui_impl()
        QMetaObject.invokeMethod(self, "_show_ui_slot", Qt.ConnectionType.BlockingQueuedConnection)
        return self._last_result

    @pyqtSlot()
    def _show_ui_slot(self):
        self._last_result = self._show_ui_impl()

    def _show_ui_impl(self) -> str:
        self.main_window.show()
        self.main_window.showNormal()
        self.main_window.raise_()
        self.main_window.activateWindow()
        logger.info("Dashboard UI shown (avatar left running).")
        return "Dashboard khol diya."

    def hide_ui(self) -> str:
        """Hides the full dashboard, leaving only the avatar window
        visible - NOT the same as closing/quitting Wave (the avatar's own
        right-click menu keeps a separate "Quit Wave" action for that)."""
        if self._on_gui_thread():
            return self._hide_ui_impl()
        QMetaObject.invokeMethod(self, "_hide_ui_slot", Qt.ConnectionType.BlockingQueuedConnection)
        return self._last_result

    @pyqtSlot()
    def _hide_ui_slot(self):
        self._last_result = self._hide_ui_impl()

    def _hide_ui_impl(self) -> str:
        self.main_window.hide()
        logger.info("Dashboard UI hidden (avatar still running).")
        return "Dashboard chhupa diya."
