"""
ui/icons.py
=============================================================================
Icon: tiny static helper that resolves a logical icon name to a QIcon,
preferring qtawesome (HAS_QTA) with an emoji/text fallback.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *


class Icon:
    @staticmethod
    def get(name: str, color: str = None, size: int = 18) -> QIcon:
        if HAS_QTA:
            try:
                kwargs = {"color": color} if color else {}
                return qta.icon(f"fa6s.{name}", **kwargs, options=[{"scale_factor": 1.2}])
            except Exception:
                pass
        # Fallback emoji mapping
        emoji_map = {
            "house": "🏠", "message": "💬", "chart-line": "📊", "bolt": "⚡",
            "music": "🎵", "folder": "📁", "eye": "👁", "clipboard": "📋", "clock-rotate-left": "🕘",
            "gear": "⚙", "mic": "🎙", "send": "➤", "trash": "🗑",
            "play": "▶", "pause": "⏸", "stop": "⏹", "skip-back": "⏮", "skip-forward": "⏭",
            "volume-up": "🔊", "volume-down": "🔉", "volume-mute": "🔇",
            "sun": "☀", "moon": "🌙", "lock": "🔒", "unlock": "🔓",
            "check": "✓", "x": "✕", "plus": "+", "minus": "-",
            "search": "🔍", "settings": "⚙", "terminal": "💻",
            "code": "💻", "copy": "📋", "speaker": "🔊", "camera": "📷",
            "chevron-left": "←", "chevron-right": "→", "chevron-down": "▼",
            "star": "⭐", "heart": "❤️", "thumbs-up": "👍", "laugh": "😂",
            "surprise": "😮", "sad": "😢", "angry": "😡", "sparkles": "✨",
            "palette": "🎨", "keyboard": "⌨", "shield": "🛡", "download": "⬇",
            "upload": "⬆", "wifi": "📶", "bluetooth": "📱", "monitor": "🖥",
            "database": "🗄", "server": "🖧", "cpu": "🧠", "memory": "💾",
            "hard-drive": "💿", "battery": "🔋", "plug": "🔌", "thermometer": "🌡",
            "zap": "⚡", "activity": "📈", "bar-chart": "📊", "pie-chart": "📊",
            "users": "👥", "user": "👤", "bot": "🤖", "brain": "🧠",
            "terminal": "💻", "code": "💻", "file-text": "📄", "image": "🖼",
            "video": "🎬", "music": "🎵", "headphones": "🎧", "mic": "🎙",
            "paperclip": "📎", "xmark": "✕", "diagram-project": "🧩",
            # Tier 21: nav icons for the new management pages, as emoji
            # fallbacks for when qtawesome isn't installed (HAS_QTA False) -
            # the fa6s.* names passed to Icon.get() above already cover the
            # qtawesome path.
            "bell": "🔔", "list-check": "✅", "calendar": "📅",
            "wand-magic-sparkles": "🪄", "gauge": "⏱",
        }
        emoji = emoji_map.get(name, "⬜")
        lbl = QLabel(emoji)
        lbl.setFixedSize(size, size)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if color:
            lbl.setStyleSheet(f"color: {color}; font-size: {size}px;")
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        lbl.render(pixmap)
        return QIcon(pixmap)

    @staticmethod
    def set_btn_icon(btn, name: str, color: str = None, size: int = 18):
        """🚨 FIX: this now works whether `btn` is a button-like widget
        (QPushButton/QToolButton — anything with setIcon) OR a plain QLabel
        (which only has setPixmap). Calling this on a QLabel used to crash
        with AttributeError: 'QLabel' object has no attribute 'setIcon' —
        that's exactly what was happening in _make_stat_card()."""
        icon = Icon.get(name, color, size)
        if hasattr(btn, "setIcon"):
            btn.setIcon(icon)
            if hasattr(btn, "setIconSize"):
                btn.setIconSize(QSize(size, size))
        elif hasattr(btn, "setPixmap"):
            btn.setPixmap(icon.pixmap(QSize(size, size)))



# =============================================================================
# 🌫️ AURORA BACKDROP (Theme-aware)
# =============================================================================
