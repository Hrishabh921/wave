"""
ui/effects.py
=============================================================================
AuroraBackdrop (animated background widget) and Anim (small animation
helpers), plus the add_shadow() / make_section_heading() helper functions
that live alongside them.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors


class AuroraBackdrop(QWidget):
    def __init__(self, theme: ThemeColors, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._blur_radius = 25.0
        self._cache = None
        self._cache_key = None
        self._resizing = False
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._finish_resize)

    def set_theme(self, theme: ThemeColors):
        self._theme = theme
        self._cache = None
        self.update()

    def set_blur_radius(self, radius: float):
        self._blur_radius = max(0.0, radius)
        self._cache = None
        self.update()

    def resizeEvent(self, event):
        # Phase 4: keep painting the cached image (stretched) while the window is being
        # dragged, and re-render the blurred backdrop once, shortly after the resize stops.
        if self._cache is not None:
            self._resizing = True
            self._settle.start(120)
        super().resizeEvent(event)

    def _finish_resize(self):
        self._resizing = False
        self.update()

    def _render_backdrop(self, w, h):
        scale = 0.5
        rw, rh = max(1, int(w * scale)), max(1, int(h * scale))

        raw = QPixmap(rw, rh)
        raw.fill(QColor(self._theme.bg_main))
        painter = QPainter(raw)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        # Theme-aware blobs
        c1 = QColor(self._theme.accent_1)
        c2 = QColor(self._theme.accent_2)
        c3 = QColor(self._theme.accent_3)
        blobs = [
            (0.20, 0.15, 0.90, c1),
            (0.80, 0.25, 0.85, c2),
            (0.50, 0.85, 0.90, c3),
        ]
        for fx, fy, fr, color in blobs:
            cx, cy = rw * fx, rh * fy
            radius = max(rw, rh) * fr
            grad = QRadialGradient(cx, cy, radius)
            c0 = QColor(color); c0.setAlpha(60)
            c1 = QColor(color); c1.setAlpha(20)
            c2 = QColor(color); c2.setAlpha(0)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(0.55, c1)
            grad.setColorAt(1.0, c2)
            painter.setBrush(QBrush(grad))
            painter.drawRect(raw.rect())
        painter.end()

        if self._blur_radius > 0.5:
            scene = QGraphicsScene()
            item = QGraphicsPixmapItem(raw)
            effect = QGraphicsBlurEffect()
            effect.setBlurRadius(self._blur_radius)
            item.setGraphicsEffect(effect)
            scene.addItem(item)
            blurred = QPixmap(rw, rh)
            blurred.fill(Qt.GlobalColor.transparent)
            p2 = QPainter(blurred)
            scene.render(p2, QRectF(0, 0, rw, rh), QRectF(0, 0, rw, rh))
            p2.end()
            result = blurred
        else:
            result = raw

        p3 = QPainter(result)
        p3.fillRect(result.rect(), QColor(0, 0, 0, 40))
        p3.end()

        return result.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)

    def paintEvent(self, event):
        w, h = max(1, self.width()), max(1, self.height())
        key = (w, h, round(self._blur_radius, 1))
        if self._cache is None or (self._cache_key != key and not self._resizing):
            self._cache = self._render_backdrop(w, h)
            self._cache_key = key
        painter = QPainter(self)
        if self._cache.width() != w or self._cache.height() != h:      # mid-resize: scale the old render
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(self.rect(), self._cache)
        else:
            painter.drawPixmap(0, 0, self._cache)


# =============================================================================
# 🎬 ANIMATION HELPERS
# =============================================================================
# Tier 21: motion duration/easing are now design tokens on ThemeColors
# (motion_duration_fast_ms/normal_ms/slow_ms, motion_easing) rather than
# bare literals handed to QPropertyAnimation at each call site. Anim's own
# methods are plain @staticmethods called from many places throughout
# ui/main_window.py/ui/*_panel.py with no theme reference threaded through
# (a signature change there is a much bigger, harder-to-verify-without-a-
# display refactor than this tier can safely take on) - so instead, the
# *default* duration values below are sourced from a default ThemeColors()
# instance at import time, and _easing_curve() resolves ThemeColors.
# motion_easing (a plain string, so it survives a theme JSON round-trip -
# see ThemeColors.to_dict/from_dict) to the real QEasingCurve.Type. Any
# caller that still wants the OLD exact behavior can pass a literal
# duration/curve through as before; nothing here changes what already
# worked, it just gives the previous literals real, documented names.
_DEFAULT_THEME_FOR_MOTION = ThemeColors()


def _easing_curve(name: str = None) -> "QEasingCurve.Type":
    """Resolves a ThemeColors.motion_easing string (e.g. "OutCubic") to the
    matching QEasingCurve.Type, falling back to OutCubic for an unknown or
    missing name - never raises, same fail-open posture as every other
    small lookup helper in this project."""
    resolved = name or _DEFAULT_THEME_FOR_MOTION.motion_easing
    return getattr(QEasingCurve.Type, resolved, QEasingCurve.Type.OutCubic)


class Anim:
    @staticmethod
    def fade_in(widget: QWidget, duration: int = None, start: float = 0.0, end: float = 1.0):
        if duration is None:
            duration = _DEFAULT_THEME_FOR_MOTION.motion_duration_normal_ms
        eff = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(eff)
        # `widget` is passed as the animation's parent so Qt (C++ side) keeps it alive.
        # Previously `anim` had no parent, so Python garbage-collected it the instant
        # this function returned (nothing else held a reference) - which killed the
        # animation before it ever ran, leaving widgets stuck at opacity 0 (invisible)
        # forever, even though they stayed fully clickable underneath. That's exactly
        # why the sidebar nav list was invisible but still worked on click.
        anim = QPropertyAnimation(eff, b"opacity", widget)
        anim.setDuration(duration)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(_easing_curve())
        if end >= 1.0:
            # Fully faded in -> drop the effect so the widget renders normally
            # afterward instead of staying wrapped in an offscreen compositing layer.
            anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start()
        return anim

    @staticmethod
    def slide_in(widget: QWidget, direction: str = "left", duration: int = 300):
        """direction: left, right, up, down.

        Tier 21: duration is deliberately left as the original literal
        (300ms), not switched to a motion_duration_* token - 300 doesn't
        sit cleanly on the fast/normal/slow (150/250/400) scale, and
        nudging it to the nearest token would be a real, unverifiable
        timing change in this no-display sandbox (same "don't tokenize a
        value that doesn't already match" reasoning as ui/theme.py's
        build_stylesheet() comment explains for its own literals)."""
        start_pos = widget.pos()
        w, h = widget.width(), widget.height()
        if direction == "left":
            end_pos = QPoint(0, 0)
            start_pos = QPoint(-w, 0)
        elif direction == "right":
            end_pos = QPoint(0, 0)
            start_pos = QPoint(w, 0)
        elif direction == "up":
            end_pos = QPoint(0, 0)
            start_pos = QPoint(0, -h)
        else:  # down
            end_pos = QPoint(0, 0)
            start_pos = QPoint(0, h)

        widget.move(start_pos)
        anim = QPropertyAnimation(widget, b"pos")
        anim.setDuration(duration)
        anim.setStartValue(start_pos)
        anim.setEndValue(end_pos)
        anim.setEasingCurve(_easing_curve())
        anim.start()
        return anim

    @staticmethod
    def stagger_show(widgets: List[QWidget], delay: int = None, duration: int = None):
        if delay is None:
            delay = _DEFAULT_THEME_FOR_MOTION.motion_stagger_delay_ms
        if duration is None:
            duration = _DEFAULT_THEME_FOR_MOTION.motion_duration_normal_ms
        for i, w in enumerate(widgets):
            QTimer.singleShot(i * delay, lambda ww=w: Anim.fade_in(ww, duration))

    @staticmethod
    def pulse(widget: QWidget, color: QColor, duration: int = 1000):
        """Pulse border glow"""
        original_style = widget.styleSheet()
        # Would need custom paintEvent for true pulse - simplified here


def add_shadow(widget, color=None, blur=36, x_offset=0, y_offset=10):
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(blur)
    shadow.setXOffset(x_offset)
    shadow.setYOffset(y_offset)
    shadow.setColor(color if color is not None else QColor(0, 0, 0, 180))
    widget.setGraphicsEffect(shadow)


# =============================================================================
# 🧱 UI BUILDING BLOCKS
# =============================================================================
def make_section_heading(text: str, theme: ThemeColors) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("class", "Heading")
    return lbl


def make_divider(theme: ThemeColors) -> QFrame:
    line = QFrame()
    line.setProperty("class", "Divider")
    return line


def set_active(btn: QPushButton, active: bool):
    btn.setProperty("active", "true" if active else "false")
    btn.style().unpolish(btn)
    btn.style().polish(btn)


def wrap_scroll(content_widget: QWidget, theme: ThemeColors) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    scroll.viewport().setStyleSheet("background: transparent;")
    scroll.setWidget(content_widget)
    return scroll


# =============================================================================
# 🧩 SLIDING STACK (with swipe + wheel)
# =============================================================================
