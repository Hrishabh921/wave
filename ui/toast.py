"""
ui/toast.py
=============================================================================
ToastManager: transient on-screen notification toasts.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import Anim


class ToastManager(QObject):
    def __init__(self, parent, theme: ThemeColors):
        super().__init__(parent)
        self._parent = parent
        self._theme = theme
        self._toasts = []
        # Tier 21: gated by the Settings/onboarding "Desktop notifications"
        # checkbox (see ui/settings_panel.py's cb_notifications). WAVE has
        # no OS-level desktop notifications anywhere in the codebase, so
        # this toggle controls the one real notification surface that
        # exists - these in-app toasts - rather than a permission that
        # would otherwise gate nothing.
        self.enabled = True

    def show(self, message: str, icon: str = "info", duration: int = 3000):
        if not self.enabled:
            return None
        toast = QFrame()
        toast.setProperty("class", "Panel")
        toast.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.ToolTip | Qt.WindowType.WindowStaysOnTopHint)
        toast.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        layout = QHBoxLayout(toast)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        
        icons = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
        colors = {"info": self._theme.accent_1, "success": self._theme.success, 
                  "warning": self._theme.warning, "error": self._theme.danger}
        
        icon_lbl = QLabel(icons.get(icon, "ℹ️"))
        icon_lbl.setStyleSheet(f"font-size: 16px; color: {colors.get(icon, self._theme.accent_1)};")
        layout.addWidget(icon_lbl)
        
        msg_lbl = QLabel(message)
        msg_lbl.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
        msg_lbl.setWordWrap(True)
        layout.addWidget(msg_lbl, 1)
        
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet("background: transparent; border: none; color: #888; font-size: 12px;")
        close_btn.clicked.connect(toast.close)
        layout.addWidget(close_btn)
        
        # Position at top-right of parent
        toast.adjustSize()
        parent_rect = self._parent.geometry()
        x = parent_rect.x() + parent_rect.width() - toast.width() - 24
        y = parent_rect.y() + 24 + len(self._toasts) * (toast.height() + 8)
        toast.move(x, y)
        
        self._toasts.append(toast)
        toast.show()
        Anim.fade_in(toast, 200)
        
        def remove_toast():
            if toast in self._toasts:
                self._toasts.remove(toast)
            toast.deleteLater()
            self._reposition()
        
        QTimer.singleShot(duration, remove_toast)
        return toast
    
    def _reposition(self):
        parent_rect = self._parent.geometry()
        for i, toast in enumerate(self._toasts):
            y = parent_rect.y() + 24 + i * (toast.height() + 8)
            x = parent_rect.x() + parent_rect.width() - toast.width() - 24
            toast.move(x, y)


# =============================================================================
# ⚙️ WORKER POOL (cleaner threading)
# =============================================================================
