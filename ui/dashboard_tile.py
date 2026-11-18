"""
ui/dashboard_tile.py
=============================================================================
DashboardTile: the clickable summary tile used on the Home page.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import add_shadow


class DashboardTile(QFrame):
    clicked = pyqtSignal()

    def __init__(self, icon_ch: str, label: str, theme: ThemeColors, parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setProperty("class", "Panel")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(150)
        add_shadow(self, blur=24, y_offset=6)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        title_bar = QWidget()
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(
            f"background-color: rgba(255,255,255,0.05); "
            "border-top-left-radius: 22px; border-top-right-radius: 22px;"
        )
        tb = QHBoxLayout(title_bar)
        tb.setContentsMargins(12, 0, 10, 0)
        tb.setSpacing(8)
        icon_lbl = QLabel(icon_ch)
        icon_lbl.setStyleSheet("font-size: 13px; background: transparent;")
        tb.addWidget(icon_lbl)
        text_lbl = QLabel(label)
        text_lbl.setStyleSheet(f"color: {theme.text_main}; font-size: 11.5px; font-weight: 700; background: transparent;")
        tb.addWidget(text_lbl)
        tb.addStretch()
        v.addWidget(title_bar)

        self.preview_lbl = QLabel()
        self.preview_lbl.setMinimumHeight(116)
        self.preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_lbl.setStyleSheet(
            f"background: rgba(0,0,0,0.3); "
            "border-bottom-left-radius: 22px; border-bottom-right-radius: 22px;"
        )
        v.addWidget(self.preview_lbl, 1)

    def set_preview(self, pixmap):
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(
                self.preview_lbl.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.preview_lbl.setPixmap(scaled)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self.setGraphicsEffect(None)
        glow = QColor(self._theme.accent_1)
        glow.setAlpha(100)
        add_shadow(self, color=glow, blur=30, y_offset=8)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setGraphicsEffect(None)
        add_shadow(self, blur=24, y_offset=6)
        super().leaveEvent(event)


# =============================================================================
# 🔍 COMMAND PALETTE (Ctrl+K)
# =============================================================================
