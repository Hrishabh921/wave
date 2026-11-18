"""
ui/widgets_basic.py
=============================================================================
Small reusable display widgets: SlidingStack, CircularGauge, Sparkline,
AudioVisualizer, AlbumArtDisc, BrandIcon.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors


class SlidingStack(QWidget):
    def __init__(self):
        super().__init__()
        self.pages = []
        self.current_index = 0
        self.animating = False
        self._dragging = False
        self._drag_start_x = 0
        self._drag_dx = 0
        self._anim_group = None
        self.on_index_changed = None
        self._wheel_accum_x = 0.0
        self._wheel_cooldown = False

    def add_page(self, widget):
        widget.setParent(self)
        idx = len(self.pages)
        self.pages.append(widget)
        widget.show() if idx == self.current_index else widget.hide()
        self._reposition_all()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel:
            dx = event.angleDelta().x()
            if dx == 0:
                dx = event.pixelDelta().x()
            if dx != 0:
                self._handle_wheel_delta(dx, event)
                return True
        return super().eventFilter(obj, event)

    def _reposition_all(self):
        w, h = max(1, self.width()), max(1, self.height())
        for i, page in enumerate(self.pages):
            if i == self.current_index:
                page.setGeometry(0, 0, w, h)
            elif i < self.current_index:
                page.setGeometry(-w, 0, w, h)
            else:
                page.setGeometry(w, 0, w, h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.animating and not self._dragging:
            self._reposition_all()

    def slide_to(self, index):
        if self.animating or index == self.current_index or not (0 <= index < len(self.pages)):
            return
        self.animating = True
        direction = 1 if index > self.current_index else -1
        old_widget = self.pages[self.current_index]
        new_widget = self.pages[index]
        w, h = self.width(), self.height()

        new_widget.setGeometry(direction * w, 0, w, h)
        new_widget.show()
        new_widget.raise_()

        anim_old = QPropertyAnimation(old_widget, b"pos")
        anim_old.setDuration(320)
        anim_old.setStartValue(QPoint(0, 0))
        anim_old.setEndValue(QPoint(-direction * w, 0))
        anim_old.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_new = QPropertyAnimation(new_widget, b"pos")
        anim_new.setDuration(320)
        anim_new.setStartValue(QPoint(direction * w, 0))
        anim_new.setEndValue(QPoint(0, 0))
        anim_new.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(anim_old)
        self._anim_group.addAnimation(anim_new)

        def on_finished():
            old_widget.hide()
            old_widget.move(0, 0)
            self.current_index = index
            self.animating = False
            self._reposition_all()
            if self.on_index_changed:
                self.on_index_changed(index)

        self._anim_group.finished.connect(on_finished)
        self._anim_group.start()

    def mousePressEvent(self, event):
        if self.animating:
            return
        self._drag_start_x = event.position().x()
        self._dragging = True
        self._drag_dx = 0

    def mouseMoveEvent(self, event):
        if not self._dragging or self.animating:
            return
        dx = event.position().x() - self._drag_start_x
        self._drag_dx = dx
        w, h = self.width(), self.height()
        current = self.pages[self.current_index]
        current.move(int(dx), 0)

        if dx < 0 and self.current_index < len(self.pages) - 1:
            nxt = self.pages[self.current_index + 1]
            nxt.setGeometry(w + int(dx), 0, w, h)
            nxt.show()
        elif dx > 0 and self.current_index > 0:
            prv = self.pages[self.current_index - 1]
            prv.setGeometry(-w + int(dx), 0, w, h)
            prv.show()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        dx = self._drag_dx
        w = self.width()
        threshold = w * 0.2

        if dx <= -threshold and self.current_index < len(self.pages) - 1:
            self._drag_finish(self.current_index + 1, 1)
        elif dx >= threshold and self.current_index > 0:
            self._drag_finish(self.current_index - 1, -1)
        else:
            self._snap_back()

    def wheelEvent(self, event):
        dx = event.angleDelta().x()
        if dx == 0:
            dx = event.pixelDelta().x()
        if dx == 0:
            event.ignore()
            return
        self._handle_wheel_delta(dx, event)

    def _handle_wheel_delta(self, dx, event):
        if self.animating or self._dragging:
            event.accept()
            return
        if self._wheel_cooldown:
            event.accept()
            return
        self._wheel_accum_x += dx
        threshold = 90
        if self._wheel_accum_x >= threshold:
            self._wheel_trigger(go_next=True)
        elif self._wheel_accum_x <= -threshold:
            self._wheel_trigger(go_next=False)
        event.accept()

    def _wheel_trigger(self, go_next):
        self._wheel_accum_x = 0.0
        self._wheel_cooldown = True
        QTimer.singleShot(450, self._reset_wheel_cooldown)
        if go_next and self.current_index < len(self.pages) - 1:
            self.slide_to(self.current_index + 1)
        elif not go_next and self.current_index > 0:
            self.slide_to(self.current_index - 1)

    def _reset_wheel_cooldown(self):
        self._wheel_cooldown = False
        self._wheel_accum_x = 0.0

    def _drag_finish(self, new_index, direction):
        self.animating = True
        old_widget = self.pages[self.current_index]
        new_widget = self.pages[new_index]
        w = self.width()

        anim_old = QPropertyAnimation(old_widget, b"pos")
        anim_old.setDuration(220)
        anim_old.setStartValue(old_widget.pos())
        anim_old.setEndValue(QPoint(-direction * w, 0))
        anim_old.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_new = QPropertyAnimation(new_widget, b"pos")
        anim_new.setDuration(220)
        anim_new.setStartValue(new_widget.pos())
        anim_new.setEndValue(QPoint(0, 0))
        anim_new.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._anim_group = QParallelAnimationGroup()
        self._anim_group.addAnimation(anim_old)
        self._anim_group.addAnimation(anim_new)

        def on_finished():
            old_widget.hide()
            self.current_index = new_index
            self.animating = False
            self._reposition_all()
            if self.on_index_changed:
                self.on_index_changed(new_index)

        self._anim_group.finished.connect(on_finished)
        self._anim_group.start()

    def _snap_back(self):
        self.animating = True
        current = self.pages[self.current_index]
        anim = QPropertyAnimation(current, b"pos")
        anim.setDuration(200)
        anim.setStartValue(current.pos())
        anim.setEndValue(QPoint(0, 0))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        def on_finished():
            self.animating = False
            self._reposition_all()

        anim.finished.connect(on_finished)
        self._anim_group = anim
        anim.start()


# =============================================================================
# 📊 CIRCULAR GAUGE + SPARKLINE
# =============================================================================
class CircularGauge(QWidget):
    def __init__(self, color="#00D4AA", thickness=8, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self._color = QColor(color)
        self._track_color = QColor("#2A2A2A")
        self._thickness = thickness
        self._suffix = "%"
        self.setMinimumSize(64, 64)

    def setValue(self, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = 0.0
        value = max(0.0, min(100.0, value))
        if value != self._value:
            self._value = value
            self.update()

    def setColor(self, color):
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height())
        margin = self._thickness / 2 + 3
        rect = QRectF(0, 0, side - 2 * margin, side - 2 * margin)
        rect.moveCenter(QRectF(0, 0, self.width(), self.height()).center())

        pen_track = QPen(self._track_color)
        pen_track.setWidthF(self._thickness)
        pen_track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen_track)
        painter.drawArc(rect, 0, 360 * 16)

        if self._value > 0:
            pen_val = QPen(self._color)
            pen_val.setWidthF(self._thickness)
            pen_val.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen_val)
            span = int(-(self._value / 100.0) * 360 * 16)
            painter.drawArc(rect, 90 * 16, span)

        painter.setPen(QColor("#FFFFFF"))
        font = QFont("Segoe UI", max(8, side // 7), QFont.Weight.Bold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, f"{int(round(self._value))}{self._suffix}")


class Sparkline(QWidget):
    def __init__(self, color="#00D4AA", max_points=60, parent=None):
        super().__init__(parent)
        self.data = deque([0.0] * max_points, maxlen=max_points)
        self.color = QColor(color)
        self.setFixedHeight(36)
        self.setMinimumWidth(100)

    def add_value(self, value: float):
        self.data.append(max(0.0, min(100.0, value)))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        if len(self.data) < 2:
            return

        w, h = self.width(), self.height()
        margin = 4
        x_step = (w - 2 * margin) / (len(self.data) - 1)
        points = []

        for i, val in enumerate(self.data):
            x = margin + i * x_step
            y = h - margin - (val / 100.0) * (h - 2 * margin)
            points.append(QPointF(x, y))

        # Gradient fill under line
        grad = QLinearGradient(0, h, 0, 0)
        c_start = QColor(self.color); c_start.setAlpha(60)
        c_end = QColor(self.color); c_end.setAlpha(0)
        grad.setColorAt(0, c_start)
        grad.setColorAt(1, c_end)
        painter.setBrush(QBrush(grad))
        painter.setPen(Qt.PenStyle.NoPen)

        poly = QPolygonF(points)
        poly.append(QPointF(points[-1].x(), h - margin))
        poly.append(QPointF(points[0].x(), h - margin))
        painter.drawPolygon(poly)

        # Line
        pen = QPen(self.color)
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPolyline(QPolygonF(points))     # one call instead of one per segment


# =============================================================================
# 🎵 AUDIO VISUALIZER
# =============================================================================
class AudioVisualizer(QFrame):
    def __init__(self, theme: ThemeColors):
        super().__init__()
        self._theme = theme
        self.setFixedHeight(40)
        self.mode = "SLEEPING"
        self.bar_width = 3.0
        self.spacing = 3.0
        self.heights = []
        self.targets = []
        self.phase = 0.0
        # Tier 11: real microphone loudness (voice/stt.py's level tap via
        # ContinuousMicWorker.audio_level). While fresh readings arrive,
        # LISTENING bars follow the mic - the newest reading in the middle,
        # older ones rippling outwards; with none (mic idle, no callback)
        # the old animation is kept so the widget never looks dead.
        self._level = 0.0
        self._level_at = -1e9
        self._level_history = deque(maxlen=64)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)

    LEVEL_STALE_S = 0.6

    def set_mode(self, mode):
        self.mode = mode

    def set_level(self, level):
        """Live mic loudness, 0.0-1.0. Cheap - called ~15 times a second."""
        try:
            level = float(level)
        except (TypeError, ValueError):
            return
        self._level = max(0.0, min(1.0, level))
        self._level_at = time.monotonic()

    def _live_level(self):
        """The latest mic level, or None if no reading is fresh enough to
        trust (then the decorative animation is used instead)."""
        if time.monotonic() - self._level_at > self.LEVEL_STALE_S:
            return None
        return self._level

    def _level_targets(self, n, max_h, live):
        """Bar heights for LISTENING when real levels are flowing: mirrored
        around the centre, newest reading at the centre."""
        self._level_history.append(live)
        hist = list(self._level_history)
        centre = (n - 1) / 2.0
        out = []
        for i in range(n):
            age = int(abs(i - centre))
            lvl = hist[-1 - age] if age < len(hist) else 0.0
            out.append(3.0 + lvl * (max_h - 3.0))
        return out

    def _bar_count(self):
        step = self.bar_width + self.spacing
        return max(1, int(self.width() // step))

    def _sync_lists(self, n):
        if len(self.heights) != n:
            self.heights = [3.0] * n
            self.targets = [3.0] * n

    def _tick(self):
        if not self.isVisible() or self.window().isMinimized():
            return      # nothing to animate while hidden / minimised to the tray
        n = self._bar_count()
        self._sync_lists(n)
        self.phase += 0.18
        max_h = max(6.0, self.height() - 6.0)
        live = self._live_level() if self.mode == "LISTENING" else None
        if live is not None:
            self.targets = self._level_targets(n, max_h, live)

        for i in range(n):
            if self.mode == "LISTENING" and live is not None:
                target = self.targets[i]
            elif self.mode == "LISTENING":
                if random.random() < 0.16:
                    self.targets[i] = random.uniform(5, max_h * 0.55)
                target = self.targets[i]
            elif self.mode == "SPEAKING":
                if random.random() < 0.26:
                    self.targets[i] = random.uniform(8, max_h)
                target = self.targets[i]
            elif self.mode == "PLAYING":
                if random.random() < 0.22:
                    self.targets[i] = random.uniform(6, max_h * 0.85)
                target = self.targets[i]
            else:
                target = 3 + 3 * (0.5 + 0.5 * math.sin(self.phase + i * 0.4))

            self.heights[i] += (target - self.heights[i]) * 0.3

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self.heights)
        if n == 0:
            return

        step = self.bar_width + self.spacing
        total_w = n * step - self.spacing
        start_x = (self.width() - total_w) / 2.0
        mid_y = self.height() / 2.0

        if self.mode == "LISTENING":
            c1, c2 = QColor(self._theme.accent_2), QColor(self._theme.accent_1)
        elif self.mode == "SPEAKING":
            c1, c2 = QColor(self._theme.accent_1), QColor(self._theme.accent_3)
        elif self.mode == "PLAYING":
            c1, c2 = QColor(self._theme.success), QColor(self._theme.accent_2)
        else:
            c1, c2 = QColor("#1A1A1A"), QColor("#2A2A2A")

        painter.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            t = i / max(1, n - 1)
            color = self._lerp_color(c1, c2, t)
            hgt = self.heights[i]
            x = start_x + i * step
            y = mid_y - hgt / 2.0
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(x, y, self.bar_width, hgt), 1.5, 1.5)

    @staticmethod
    def _lerp_color(c1, c2, t):
        r = c1.red() + (c2.red() - c1.red()) * t
        g = c1.green() + (c2.green() - c1.green()) * t
        b = c1.blue() + (c2.blue() - c1.blue()) * t
        return QColor(int(r), int(g), int(b))


# =============================================================================
# 💿 ALBUM ART DISC
# =============================================================================
class AlbumArtDisc(QWidget):
    def __init__(self, theme: ThemeColors, size=76):
        super().__init__()
        self._theme = theme
        self.setFixedSize(size, size)
        self.angle = 0
        self.spinning = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    def set_spinning(self, spinning):
        self.spinning = spinning

    def _tick(self):
        if self.spinning and self.isVisible() and not self.window().isMinimized():
            self.angle = (self.angle + 3) % 360
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        cx, cy = self.width() / 2.0, self.height() / 2.0
        painter.translate(cx, cy)
        painter.rotate(self.angle)
        painter.translate(-cx, -cy)

        rect = QRectF(2, 2, self.width() - 4, self.height() - 4)
        grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
        grad.setColorAt(0, QColor(self._theme.accent_1))
        grad.setColorAt(1, QColor(self._theme.accent_2))
        painter.setBrush(QBrush(grad))
        painter.drawEllipse(rect)

        painter.setPen(QPen(QColor(255, 255, 255, 30), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for frac in (0.34, 0.25, 0.17):
            rr = rect.width() * frac
            painter.drawEllipse(QRectF(cx - rr, cy - rr, rr * 2, rr * 2))

        center_r = rect.width() * 0.14
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._theme.bg_main))
        painter.drawEllipse(QRectF(cx - center_r, cy - center_r, center_r * 2, center_r * 2))
        hole_r = center_r * 0.3
        painter.setBrush(QColor(self._theme.text_main))
        painter.drawEllipse(QRectF(cx - hole_r, cy - hole_r, hole_r * 2, hole_r * 2))


# =============================================================================
# 🏷️ BRAND ICON
# =============================================================================
class BrandIcon(QWidget):
    def __init__(self, theme: ThemeColors, size=52):
        super().__init__()
        self._theme = theme
        self.setFixedSize(size, int(size * 0.86))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        w, h = self.width(), self.height()
        tail_h = h * 0.22
        body = QRectF(0, 0, w, h - tail_h)
        grad = QLinearGradient(body.topLeft(), body.bottomRight())
        grad.setColorAt(0, QColor(self._theme.accent_1))
        grad.setColorAt(1, QColor(self._theme.accent_2))
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(body, w * 0.26, w * 0.26)

        tail = QPolygonF([
            QPointF(w * 0.20, body.bottom() - 3),
            QPointF(w * 0.40, body.bottom() - 3),
            QPointF(w * 0.20, h),
        ])
        painter.drawPolygon(tail)

        painter.setBrush(QColor(255, 255, 255, 235))
        dot_r = w * 0.045
        cy = body.center().y()
        for dx in (-0.16, 0.0, 0.16):
            cx = body.center().x() + w * dx
            painter.drawEllipse(QRectF(cx - dot_r, cy - dot_r, dot_r * 2, dot_r * 2))


# =============================================================================
# 💬 CHAT BUBBLE (with reactions, code highlight, copy)
# =============================================================================
