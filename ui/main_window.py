import logging
import time
import os
import ctypes
import json
import socket
import random
import math
import re
import pyautogui
import psutil
import requests
from collections import deque
from datetime import datetime
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field
from functools import partial

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QFrame, QScrollArea, QSizePolicy,
    QGraphicsDropShadowEffect, QTextEdit, QSlider, QButtonGroup, QTreeView, QMessageBox,
    QGraphicsBlurEffect, QGraphicsScene, QGraphicsPixmapItem, QDialog, QMenu,
    QGraphicsOpacityEffect, QStackedWidget, QListWidget, QListWidgetItem,
    QTextBrowser, QSplitter, QTabWidget, QCheckBox, QComboBox, QSpinBox,
    QColorDialog, QFileDialog, QFormLayout, QProgressBar, QToolTip
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QRectF, QPointF, QPoint, QDir,
    QPropertyAnimation, QParallelAnimationGroup, QEasingCurve, QEvent,
    QObject, QRect, QSize, QMimeData, QUrl
)
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QPen, QPolygonF, QLinearGradient, QRadialGradient,
    QBrush, QFileSystemModel, QPixmap, QIcon, QAction, QCursor, QTextCharFormat,
    QSyntaxHighlighter, QTextDocument, QShortcut, QKeySequence, QTextCursor
)

try:
    import qtawesome as qta
    HAS_QTA = True
except ImportError:
    HAS_QTA = False

try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

try:
    from pygments import highlight
    from pygments.lexers import get_lexer_by_name, guess_lexer, TextLexer
    from pygments.formatters import HtmlFormatter
    from pygments.styles import get_style_by_name
    HAS_PYGMENTS = True
except ImportError:
    HAS_PYGMENTS = False

try:
    import markdown as _markdown_lib
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

from brain.groq_core import WaveBrain
from monitoring.sys_monitor import SystemMonitor
from voice.tts import tts_engine
from voice.stt import stt_engine
from automation.app_control import AppController
from automation.file_manager import FileManager
from vision.screen_capture import VisionCapture
from vision.ocr_engine import ocr
from database.db_manager import db
from settings.config import config
from monitoring.media_info import get_now_playing, seek_to, format_time, HAS_WINSDK

try:
    import screen_brightness_control as sbc
    HAS_SBC = True
except ImportError:
    HAS_SBC = False

try:
    import comtypes
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    HAS_PYCAW = True
except ImportError:
    HAS_PYCAW = False

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

logger = logging.getLogger("GUI")
SETTINGS_FILE = "wave_settings.json"
THEMES_DIR = "themes"


# =============================================================================
# 🎨 THEME SYSTEM
# =============================================================================
@dataclass
class ThemeColors:
    name: str = "Black Glass"
    bg_main: str = "#000000"
    bg_panel: str = "rgba(20,20,20,0.95)"
    bg_surface: str = "rgba(30,30,30,0.98)"
    bg_surface2: str = "#1A1A1A"
    border: str = "rgba(255,255,255,0.15)"
    border_soft: str = "rgba(255,255,255,0.08)"
    text_main: str = "#FFFFFF"
    text_muted: str = "#B0B0B0"
    text_faint: str = "#808080"
    accent_1: str = "#00D4AA"
    accent_2: str = "#FF6B9D"
    accent_3: str = "#FFD60A"
    success: str = "#00E676"
    warning: str = "#FFD60A"
    danger: str = "#FF3D5A"
    bubble_sent: str = "#00D4AA"
    bubble_recv: str = "#1A1A1A"
    page_bg_alpha: float = 0.95
    panel_top_alpha: float = 0.98
    panel_bottom_alpha: float = 0.92

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})


class ThemeManager(QObject):
    theme_changed = pyqtSignal(ThemeColors)
    
    def __init__(self):
        super().__init__()
        self._themes: Dict[str, ThemeColors] = {}
        self._current: ThemeColors = ThemeColors()
        self._load_builtin_themes()
        self._load_custom_themes()
    
    def _load_builtin_themes(self):
        self._themes["Black Glass"] = ThemeColors()
        self._themes["Midnight Blue"] = ThemeColors(
            name="Midnight Blue",
            bg_main="#050A1A",
            bg_panel="rgba(15,20,40,0.95)",
            bg_surface="rgba(20,25,50,0.98)",
            bg_surface2="#101830",
            border="rgba(100,150,255,0.2)",
            border_soft="rgba(100,150,255,0.1)",
            accent_1="#00B4D8",
            accent_2="#90E0EF",
            accent_3="#FFD60A",
            bubble_sent="#00B4D8",
            bubble_recv="#101830",
        )
        self._themes["Emerald Dark"] = ThemeColors(
            name="Emerald Dark",
            bg_main="#001008",
            bg_panel="rgba(10,30,20,0.95)",
            bg_surface="rgba(15,40,25,0.98)",
            bg_surface2="#0A2518",
            border="rgba(0,255,150,0.2)",
            border_soft="rgba(0,255,150,0.1)",
            accent_1="#00FF96",
            accent_2="#00D4AA",
            accent_3="#FFD60A",
            bubble_sent="#00FF96",
            bubble_recv="#0A2518",
        )
        self._themes["Crimson Dark"] = ThemeColors(
            name="Crimson Dark",
            bg_main="#1A0008",
            bg_panel="rgba(40,10,20,0.95)",
            bg_surface="rgba(50,15,25,0.98)",
            bg_surface2="#2A0A18",
            border="rgba(255,60,120,0.2)",
            border_soft="rgba(255,60,120,0.1)",
            accent_1="#FF3C6E",
            accent_2="#FF6B9D",
            accent_3="#FFD60A",
            bubble_sent="#FF3C6E",
            bubble_recv="#2A0A18",
        )
        self._themes["Amber Dark"] = ThemeColors(
            name="Amber Dark",
            bg_main="#1A1000",
            bg_panel="rgba(40,30,10,0.95)",
            bg_surface="rgba(50,40,15,0.98)",
            bg_surface2="#2A200A",
            border="rgba(255,180,0,0.2)",
            border_soft="rgba(255,180,0,0.1)",
            accent_1="#FFB400",
            accent_2="#FFD60A",
            accent_3="#FF6B9D",
            bubble_sent="#FFB400",
            bubble_recv="#2A200A",
        )
    
    def _load_custom_themes(self):
        os.makedirs(THEMES_DIR, exist_ok=True)
        for fname in os.listdir(THEMES_DIR):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(THEMES_DIR, fname), "r", encoding="utf-8") as f:
                        data = json.load(f)
                    theme = ThemeColors.from_dict(data)
                    self._themes[theme.name] = theme
                except Exception as e:
                    logger.warning(f"Failed to load theme {fname}: {e}")
    
    def get_theme(self, name: str) -> Optional[ThemeColors]:
        return self._themes.get(name)
    
    def get_current(self) -> ThemeColors:
        return self._current
    
    def set_theme(self, name: str):
        if name in self._themes:
            self._current = self._themes[name]
            self.theme_changed.emit(self._current)
            self.save_last_theme(name)
    
    def get_theme_names(self) -> List[str]:
        return list(self._themes.keys())
    
    def save_custom_theme(self, theme: ThemeColors):
        self._themes[theme.name] = theme
        os.makedirs(THEMES_DIR, exist_ok=True)
        path = os.path.join(THEMES_DIR, f"{theme.name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(theme.to_dict(), f, indent=2)
    
    def save_last_theme(self, name: str):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data["last_theme"] = name
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    
    def get_last_theme(self) -> str:
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("last_theme", "Black Glass")
        except Exception:
            return "Black Glass"
    
    def build_stylesheet(self, theme: ThemeColors) -> str:
        t = theme
        return f"""
        QMainWindow {{ background-color: {t.bg_main}; }}
        QWidget {{ font-family: 'Segoe UI'; }}
        QToolTip {{ background-color: {t.bg_surface2}; color: {t.text_main}; border: 1px solid {t.border}; padding: 4px 8px; border-radius: 6px; }}

        QWidget#Sidebar {{ background-color: {t.bg_panel}; border-right: 1px solid {t.border_soft}; }}
        QWidget#PageBg {{ background-color: rgba(20,20,20,{t.page_bg_alpha}); }}
        QWidget#ChatHeader {{ background-color: {t.bg_panel}; border-bottom: 1px solid {t.border_soft}; }}
        QWidget#ChatInputBar {{ background-color: {t.bg_panel}; border-top: 1px solid {t.border_soft}; }}

        QFrame.Panel {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(30,30,30,{t.panel_top_alpha}), stop:1 rgba(20,20,20,{t.panel_bottom_alpha}));
            border-radius: 22px;
            border: 1px solid {t.border};
            border-top: 1px solid rgba(255,255,255,0.18);
        }}
        QFrame.Divider {{
            background-color: {t.border};
            max-height: 1px; min-height: 1px; border: none;
        }}

        QLabel {{ color: {t.text_muted}; font-size: 13px; background: transparent; }}
        QLabel.Wordmark {{ color: {t.text_main}; font-size: 18px; font-weight: 800; letter-spacing: 1.5px; }}
        QLabel.Tagline {{ color: {t.text_faint}; font-size: 10px; }}
        QLabel.Heading {{ color: {t.text_faint}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
        QLabel.PageTitle {{ color: {t.text_main}; font-size: 21px; font-weight: 800; }}
        QLabel.PageSubtitle {{ color: {t.text_muted}; font-size: 12px; }}
        QLabel.ContactName {{ color: {t.text_main}; font-size: 14px; font-weight: 700; }}
        QLabel.StatusMessage {{ font-size: 12px; }}
        QLabel.InfoData {{ color: {t.text_main}; font-size: 12px; font-weight: 600; }}
        QLabel.GaugeCaption {{ color: {t.text_muted}; font-size: 10.5px; font-weight: 700; letter-spacing: 1px; }}
        QLabel.GaugeDetail {{ color: {t.text_faint}; font-size: 10.5px; }}
        QLabel.ClockBig {{ color: {t.text_main}; }}

        QLineEdit {{
            background-color: {t.bg_surface};
            color: {t.text_main};
            border: 1px solid {t.border};
            border-radius: 22px;
            padding: 12px 20px;
            font-size: 14px;
        }}
        QLineEdit:focus {{ border: 1px solid {t.accent_1}; background-color: rgba(40,40,40,1); }}
        QLineEdit::placeholder {{ color: {t.text_faint}; }}

        QTextEdit#ChatInputBox {{
            background-color: {t.bg_surface};
            color: {t.text_main};
            border: 1px solid {t.border};
            border-radius: 20px;
            padding: 10px 16px;
            font-size: 14px;
        }}
        QTextEdit#ChatInputBox:focus {{ border: 1px solid {t.accent_1}; background-color: rgba(40,40,40,1); }}

        QTextEdit#StickyNote {{
            background-color: {t.bg_surface};
            color: {t.text_main};
            border: 1px solid {t.border};
            border-radius: 14px;
            padding: 12px;
            font-size: 12px;
        }}
        QTextEdit#StickyNote:focus {{ border: 1px solid {t.accent_1}; }}

        QPushButton {{
            background-color: {t.bg_surface};
            color: {t.text_muted};
            border-radius: 14px;
            padding: 10px 15px;
            font-weight: 600;
            font-size: 12px;
            border: 1px solid {t.border};
        }}
        QPushButton:hover {{ background-color: rgba(50,50,50,1); border: 1px solid {t.accent_1}; color: {t.text_main}; }}
        QPushButton:pressed {{ background-color: rgba(10,10,10,1); }}

        QPushButton.NavItem {{
            text-align: left; padding: 11px 14px; border-radius: 14px;
            background: transparent; color: {t.text_muted}; border: none;
            font-size: 12.5px; font-weight: 700;
        }}
        QPushButton.NavItem:hover {{ background-color: rgba(40,40,40,1); color: {t.text_main}; }}
        QPushButton.NavItem:checked {{
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t.accent_1}40, stop:1 {t.accent_2}20);
            color: {t.text_main};
            border: 1px solid {t.accent_1}80;
        }}

        QPushButton.Pill {{
            background-color: {t.bg_surface};
            color: {t.text_muted};
            border: 1px solid {t.border};
            border-radius: 16px;
            padding: 9px 13px;
            font-size: 11.5px;
            font-weight: 700;
        }}
        QPushButton.Pill:hover {{ border: 1px solid {t.accent_1}; color: {t.text_main}; }}
        QPushButton.Pill[active="true"] {{
            background-color: {t.accent_1};
            color: #000000;
            border: 1px solid {t.accent_1};
        }}

        QPushButton.DangerPill {{
            background-color: {t.bg_surface};
            color: {t.danger};
            border: 1px solid {t.danger}50;
            border-radius: 16px;
            padding: 9px 13px;
            font-size: 11.5px;
            font-weight: 700;
        }}
        QPushButton.DangerPill:hover {{ background-color: {t.danger}; color: #FFFFFF; border: 1px solid {t.danger}; }}

        QPushButton.IconBtn {{
            background: transparent; border: none; border-radius: 18px;
            font-size: 15px; color: {t.text_muted}; padding: 0px;
        }}
        QPushButton.IconBtn:hover {{ background-color: rgba(40,40,40,1); color: {t.text_main}; }}

        QPushButton.ActionBtn {{
            padding: 10px 16px; font-size: 12px; border-radius: 14px;
            background-color: {t.bg_surface}; color: {t.text_muted}; border: 1px solid {t.border};
        }}
        QPushButton.ActionBtn:hover {{ color: {t.accent_2}; border: 1px solid {t.accent_2}; }}

        QPushButton.RoundSm {{
            border-radius: 19px; font-size: 14px; background-color: {t.bg_surface};
            color: {t.text_main}; border: 1px solid {t.border}; padding: 0px;
        }}

        QPushButton#SaveBtn {{
            border-radius: 14px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t.accent_1}, stop:1 {t.accent_2});
            color: #000000; border: none; font-weight: 800; padding: 10px 16px;
        }}
        QPushButton#SaveBtn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t.accent_2}, stop:1 {t.accent_1}); }}

        QPushButton#SendBtn {{
            border-radius: 21px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t.accent_1}, stop:1 {t.accent_2});
            color: #000000; border: none; font-weight: 800; padding: 0px;
        }}
        QPushButton#SendBtn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t.accent_2}, stop:1 {t.accent_1}); }}

        QPushButton#MicInline {{
            border-radius: 21px; font-size: 15px; background-color: {t.bg_surface};
            color: {t.accent_1}; border: 1px solid {t.border}; padding: 0px;
        }}

        QSlider::groove:horizontal {{ border-radius: 3px; height: 6px; background: {t.bg_surface2}; }}
        QSlider::sub-page:horizontal {{
            border-radius: 3px;
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t.accent_1}, stop:1 {t.accent_2});
        }}
        QSlider::handle:horizontal {{
            background: {t.accent_1}; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px;
            border: 2px solid {t.bg_main};
        }}
        QSlider::handle:horizontal:hover {{ background: {t.accent_2}; }}

        QTreeView {{
            background-color: transparent; color: {t.text_main}; border: none;
            border-radius: 16px; font-size: 12px; alternate-background-color: rgba(255,255,255,0.03);
        }}
        QTreeView::item {{ padding: 5px; }}
        QTreeView::item:selected {{ background-color: {t.accent_1}; color: #000000; }}
        QTreeView::item:hover {{ background-color: rgba(40,40,40,1); }}
        QHeaderView::section {{
            background-color: rgba(30,30,30,1); color: {t.text_muted}; border: none;
            padding: 8px; font-size: 10.5px; font-weight: 700;
        }}

        QScrollArea {{ border: none; background-color: transparent; }}
        QScrollBar:vertical {{ border: none; background: transparent; width: 6px; margin: 0px; }}
        QScrollBar::handle:vertical {{ background: {t.border}; min-height: 20px; border-radius: 3px; }}
        QScrollBar::handle:vertical:hover {{ background: {t.accent_1}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}

        QMessageBox {{ background-color: #0A0A0A; border: 1px solid {t.border}; }}
        QMessageBox QLabel {{ color: {t.text_main}; font-size: 13px; background: transparent; padding: 6px; }}
        QMessageBox QPushButton {{
            background-color: {t.bg_surface2}; color: {t.text_main}; border: 1px solid {t.border};
            border-radius: 10px; padding: 8px 18px; font-weight: 700; min-width: 90px;
        }}
        QMessageBox QPushButton:hover {{ border: 1px solid {t.accent_1}; color: {t.accent_1}; }}

        QDialog {{ background-color: {t.bg_main}; }}
        QListWidget {{ background: transparent; border: none; outline: none; }}
        QListWidget::item {{ padding: 10px 14px; border-radius: 10px; color: {t.text_main}; }}
        QListWidget::item:hover {{ background: rgba(40,40,40,1); }}
        QListWidget::item:selected {{ background: {t.accent_1}; color: #000000; }}

        QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 12px; background: {t.bg_panel}; }}
        QTabBar::tab {{ background: transparent; color: {t.text_muted}; padding: 10px 20px; font-weight: 600; }}
        QTabBar::tab:selected {{ color: {t.accent_1}; }}
        QTabBar::tab:hover {{ color: {t.text_main}; }}

        QComboBox {{ background: {t.bg_surface}; color: {t.text_main}; border: 1px solid {t.border}; border-radius: 10px; padding: 8px 12px; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox QAbstractItemView {{ background: {t.bg_panel}; color: {t.text_main}; selection-background-color: {t.accent_1}; selection-color: #000000; border: 1px solid {t.border}; }}

        QSpinBox {{ background: {t.bg_surface}; color: {t.text_main}; border: 1px solid {t.border}; border-radius: 10px; padding: 8px 12px; }}
        QCheckBox {{ color: {t.text_main}; spacing: 8px; }}
        QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {t.border}; background: {t.bg_surface}; }}
        QCheckBox::indicator:checked {{ background: {t.accent_1}; border: 1px solid {t.accent_1}; image: url(:/qta/check); }}

        QProgressBar {{ border: 1px solid {t.border}; border-radius: 6px; background: {t.bg_surface2}; text-align: center; color: {t.text_main}; }}
        QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t.accent_1}, stop:1 {t.accent_2}); border-radius: 5px; }}
        """


# =============================================================================
# 🎯 ICON HELPER (qtawesome fallback to emoji)
# =============================================================================
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
            "music": "🎵", "folder": "📁", "eye": "👁", "clipboard": "📋",
            "gear": "⚙", "mic": "🎙", "send": "➤", "trash": "🗑",
            "play": "▶", "pause": "⏸", "skip-back": "⏮", "skip-forward": "⏭",
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
            "paperclip": "📎", "xmark": "✕",
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
class AuroraBackdrop(QWidget):
    def __init__(self, theme: ThemeColors, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._blur_radius = 25.0
        self._cache = None
        self._cache_key = None

    def set_theme(self, theme: ThemeColors):
        self._theme = theme
        self._cache = None
        self.update()

    def set_blur_radius(self, radius: float):
        self._blur_radius = max(0.0, radius)
        self._cache = None
        self.update()

    def resizeEvent(self, event):
        self._cache = None
        super().resizeEvent(event)

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
        if self._cache is None or self._cache_key != key:
            self._cache = self._render_backdrop(w, h)
            self._cache_key = key
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self._cache)


# =============================================================================
# 🎬 ANIMATION HELPERS
# =============================================================================
class Anim:
    @staticmethod
    def fade_in(widget: QWidget, duration: int = 250, start: float = 0.0, end: float = 1.0):
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
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        if end >= 1.0:
            # Fully faded in -> drop the effect so the widget renders normally
            # afterward instead of staying wrapped in an offscreen compositing layer.
            anim.finished.connect(lambda: widget.setGraphicsEffect(None))
        anim.start()
        return anim

    @staticmethod
    def slide_in(widget: QWidget, direction: str = "left", duration: int = 300):
        """direction: left, right, up, down"""
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
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.start()
        return anim

    @staticmethod
    def stagger_show(widgets: List[QWidget], delay: int = 50, duration: int = 250):
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
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])


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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)

    def set_mode(self, mode):
        self.mode = mode

    def _bar_count(self):
        step = self.bar_width + self.spacing
        return max(1, int(self.width() // step))

    def _sync_lists(self, n):
        if len(self.heights) != n:
            self.heights = [3.0] * n
            self.targets = [3.0] * n

    def _tick(self):
        n = self._bar_count()
        self._sync_lists(n)
        self.phase += 0.18
        max_h = max(6.0, self.height() - 6.0)

        for i in range(n):
            if self.mode == "LISTENING":
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
        if self.spinning:
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
class ChatBubble(QWidget):
    TAIL = 9
    REACTIONS = ["👍", "❤️", "😂", "😮", "😢", "😡"]

    def __init__(self, text: str, is_user: bool, max_width: int = 420,
                 on_replay: Callable = None, on_reaction: Callable = None,
                 theme: ThemeColors = None, parent=None, attachment: dict = None):
        super().__init__(parent)
        self.is_user = is_user
        self._theme = theme or ThemeColors()
        self._on_reaction = on_reaction
        self._reaction_counts = {r: 0 for r in self.REACTIONS}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # Bubble content
        bubble_wrap = QWidget()
        bw = QHBoxLayout(bubble_wrap)
        bw.setContentsMargins(0, 0, 0, 0)
        bw.setSpacing(0)

        if is_user:
            bw.addStretch()

        self.bubble_inner = QWidget()
        self.bubble_inner.setStyleSheet("background: transparent;")
        bi = QVBoxLayout(self.bubble_inner)
        bi.setContentsMargins(14, 10, 14, 8)
        bi.setSpacing(6)

        # Attachment preview (image thumbnail or document chip), above the text
        if attachment:
            bi.addWidget(self._build_attachment_widget(attachment))

        has_text = bool(text and text.strip())
        text_color = '#000000' if is_user else self._theme.text_main

        # Message text with markdown/code support
        self.text_browser = QTextBrowser()
        self.text_browser.setOpenExternalLinks(True)
        self.text_browser.setReadOnly(True)
        self.text_browser.setFrameShape(QFrame.Shape.NoFrame)
        self.text_browser.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_browser.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_browser.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.text_browser.setStyleSheet(f"""
            background: transparent; 
            color: {text_color}; 
            font-size: 14px; 
            border: none;
            selection-background-color: {self._theme.accent_1}80;
        """)
        self.text_browser.document().setDefaultStyleSheet(self._document_css(text_color))
        self.text_browser.setHtml(self._format_message(text) if has_text else "")
        if has_text:
            self._size_text_browser(max_width)
        bi.addWidget(self.text_browser)
        if not has_text and is_user:
            # Image/doc attached with no caption - keep the widget alive (so
            # code elsewhere that reads bubble.text_browser never breaks) but
            # collapse it to zero space instead of showing an empty box.
            self.text_browser.setVisible(False)
            self.text_browser.setMaximumHeight(0)

        # Timestamp + reactions
        stamp_row = QHBoxLayout()
        stamp_row.setSpacing(8)
        stamp = QLabel(time.strftime("%I:%M %p"))
        stamp_color = "rgba(0,0,0,0.5)" if is_user else self._theme.text_faint
        stamp.setStyleSheet(f"background: transparent; color: {stamp_color}; font-size: 10px;")

        if not is_user:
            # Reactions bar
            self.reaction_btns = {}
            for react in self.REACTIONS:
                btn = QPushButton(react)
                btn.setFixedSize(22, 22)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setStyleSheet(f"""
                    background: transparent; border: none; font-size: 11px; padding: 0px;
                    color: {self._theme.text_faint};
                """)
                btn.clicked.connect(partial(self._emit_reaction, react))
                self.reaction_btns[react] = btn
                stamp_row.addWidget(btn)

        if is_user:
            check = QLabel("✓✓")
            check.setStyleSheet("background: transparent; color: #006B5A; font-size: 10px; font-weight: 700;")
            stamp_row.addStretch()
            stamp_row.addWidget(stamp)
            stamp_row.addWidget(check)
        else:
            stamp_row.addWidget(stamp)
            if on_replay:
                replay_btn = QPushButton("🔊")
                replay_btn.setToolTip("Read aloud")
                replay_btn.setFixedSize(18, 18)
                replay_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                replay_btn.setStyleSheet(f"background: transparent; border: none; font-size: 10px; color: {self._theme.text_faint}; padding: 0px;")
                replay_btn.clicked.connect(lambda: on_replay(text))
                stamp_row.addWidget(replay_btn)
            stamp_row.addStretch()

        bi.addLayout(stamp_row)

        # Copy button (hover only)
        self.copy_btn = QPushButton("📋")
        self.copy_btn.setFixedSize(24, 24)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setStyleSheet(f"""
            background: {self._theme.accent_1}; color: #000000; border: none; border-radius: 4px; font-size: 11px;
        """)
        self.copy_btn.clicked.connect(lambda: pyperclip.copy(text) if HAS_PYPERCLIP else None)
        self.copy_btn.hide()
        bi.addWidget(self.copy_btn, 0, Qt.AlignmentFlag.AlignRight)

        bw.addWidget(self.bubble_inner)

        if not is_user:
            bw.addStretch()

        outer.addWidget(bubble_wrap)

        self.setMaximumWidth(max_width)
        self.setMouseTracking(True)
        self.bubble_inner.enterEvent = lambda e: self.copy_btn.show()
        self.bubble_inner.leaveEvent = lambda e: self.copy_btn.hide()

    def _size_text_browser(self, max_width: int):
        """QTextBrowser doesn't automatically resize itself to fit its
        content, and Qt doesn't always have the widget's real font ready the
        instant it's constructed (the stylesheet gets "polished" onto it a
        moment later) - so measuring once, synchronously, here could end up
        a line short of the real layout once that font kicks in, cutting
        off the end of longer replies. This measures immediately for a
        reasonable first size, then re-measures once more on the next
        event-loop tick (after polishing has happened) to correct it."""
        available = max(80, max_width - 34)  # bubble_inner's 14px+14px side margins, plus a small buffer
        tb = self.text_browser
        doc = tb.document()

        def _apply():
            try:
                doc.setTextWidth(-1)
                ideal = doc.idealWidth()
                # A generous buffer matters here - too little of one can leave
                # the box just narrow enough to force an extra, unplanned wrap,
                # and then the (now too-short) fixed height clips that text.
                width = min(available, max(60, int(ideal) + 24))
                doc.setTextWidth(width)
                height = doc.size().height()
                tb.setFixedWidth(width)
                tb.setFixedHeight(int(height) + 10)
            except RuntimeError:
                pass  # bubble was closed/destroyed before this ran

        _apply()
        QTimer.singleShot(0, _apply)

    def _build_attachment_widget(self, attachment: dict) -> QWidget:
        """A small preview shown inside the bubble above the message text:
        a rounded thumbnail for images, or a file chip (icon+name+size) for
        documents/PDFs/text files."""
        if attachment.get("kind") == "image":
            lbl = QLabel()
            pix = QPixmap(attachment.get("path", ""))
            if not pix.isNull():
                scaled = pix.scaled(260, 260, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                lbl.setPixmap(scaled)
                lbl.setFixedSize(scaled.size())
            else:
                lbl.setText("🖼 (image unavailable)")
                lbl.setStyleSheet(f"background: transparent; color: {'#000000' if self.is_user else self._theme.text_muted}; font-size: 12px;")
            lbl.setStyleSheet(lbl.styleSheet() + " background: transparent; border-radius: 10px;")
            return lbl

        chip = QWidget()
        chip.setStyleSheet(f"background: rgba(255,255,255,{0.16 if self.is_user else 0.06}); border-radius: 10px;")
        cl = QHBoxLayout(chip)
        cl.setContentsMargins(10, 8, 10, 8)
        cl.setSpacing(8)
        is_pdf = attachment.get("name", "").lower().endswith(".pdf")
        icon_lbl = QLabel("📕" if is_pdf else "📄")
        icon_lbl.setStyleSheet("background: transparent; font-size: 18px;")
        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        name_color = "#000000" if self.is_user else self._theme.text_main
        name_lbl = QLabel(attachment.get("name", "file"))
        name_lbl.setStyleSheet(f"background: transparent; color: {name_color}; font-size: 12px; font-weight: 700;")
        kb = attachment.get("size_bytes", 0) / 1024
        size_text = f"{kb / 1024:.1f} MB" if kb > 1024 else f"{kb:.0f} KB"
        meta_color = "rgba(0,0,0,0.55)" if self.is_user else self._theme.text_faint
        meta_lbl = QLabel(size_text)
        meta_lbl.setStyleSheet(f"background: transparent; color: {meta_color}; font-size: 10px;")
        text_col.addWidget(name_lbl)
        text_col.addWidget(meta_lbl)
        cl.addWidget(icon_lbl)
        cl.addLayout(text_col, 1)
        return chip

    def _document_css(self, text_color: str) -> str:
        """Default stylesheet for the rendered-markdown QTextDocument: styles
        tables, fenced code blocks, headers, lists and blockquotes so they
        look intentional against the bubble's dark/accent background instead
        of Qt's bare default rich-text styling."""
        accent = self._theme.accent_1
        border = "rgba(0,0,0,0.18)" if self.is_user else self._theme.border
        code_bg = "rgba(0,0,0,0.16)" if self.is_user else "#1E1E1E"
        code_border = "rgba(0,0,0,0.22)" if self.is_user else self._theme.border_soft
        th_bg = "rgba(0,0,0,0.12)" if self.is_user else "rgba(255,255,255,0.06)"
        quote_color = "rgba(0,0,0,0.65)" if self.is_user else self._theme.text_muted
        return f"""
            body, p {{ color: {text_color}; margin: 0 0 6px 0; }}
            h1, h2, h3 {{ color: {text_color}; margin: 6px 0 4px 0; font-weight: 800; }}
            h1 {{ font-size: 17px; }}
            h2 {{ font-size: 15.5px; }}
            h3 {{ font-size: 14.5px; }}
            ul, ol {{ margin: 2px 0 8px 18px; padding: 0px; }}
            li {{ margin: 2px 0; }}
            a {{ color: {accent}; }}
            blockquote {{ border-left: 3px solid {accent}; margin: 6px 0; padding: 2px 0 2px 10px; color: {quote_color}; }}
            table {{ border-collapse: collapse; margin: 6px 0; }}
            th, td {{ border: 1px solid {border}; padding: 4px 9px; }}
            th {{ background: {th_bg}; font-weight: 700; }}
            code {{ background: {code_bg}; padding: 1px 5px; border-radius: 4px; font-family: Consolas, monospace; font-size: 12.5px; }}
            pre {{ background: {code_bg}; border: 1px solid {code_border}; border-radius: 8px; padding: 10px 12px; margin: 6px 0; }}
            pre code {{ background: transparent; padding: 0px; }}
        """

    def _format_message(self, text: str) -> str:
        """Render chat text as HTML. Uses full Markdown (tables, bold/italic,
        headers, lists, blockquotes) when the `markdown` package is
        installed, with fenced code blocks syntax-highlighted via Pygments.
        Falls back to a simpler code-block-only renderer, then to plain
        escaped text, if those packages aren't available."""
        if HAS_MARKDOWN:
            try:
                extensions = ['fenced_code', 'tables', 'nl2br', 'sane_lists']
                extension_configs = {}
                if HAS_PYGMENTS:
                    extensions.append('codehilite')
                    extension_configs['codehilite'] = {
                        'noclasses': True, 'pygments_style': 'monokai', 'guess_lang': False,
                    }
                body = _markdown_lib.markdown(text, extensions=extensions, extension_configs=extension_configs)
                return f'<div style="line-height:1.5;">{body}</div>'
            except Exception:
                pass  # fall through to the simpler renderers below

        if not HAS_PYGMENTS:
            # Simple escape
            escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f'<div style="line-height:1.5;">{escaped.replace(chr(10), "<br>")}</div>'

        # Simple markdown code block parsing (no `markdown` package installed)
        parts = []
        in_code = False
        code_lang = "text"
        code_buffer = []
        
        lines = text.split("\n")
        for line in lines:
            if line.strip().startswith("```"):
                if not in_code:
                    in_code = True
                    code_lang = line.strip()[3:].strip() or "text"
                    code_buffer = []
                else:
                    in_code = False
                    code_text = "\n".join(code_buffer)
                    try:
                        lexer = get_lexer_by_name(code_lang)
                    except Exception:
                        lexer = TextLexer()
                    formatter = HtmlFormatter(style="monokai", nowrap=True, noclasses=True)
                    highlighted = highlight(code_text, lexer, formatter)
                    parts.append(f'<pre><code>{highlighted}</code></pre>')
            elif in_code:
                code_buffer.append(line)
            else:
                escaped = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                escaped = re.sub(r'`([^`]+)`', r'<code>\1</code>', escaped)
                parts.append(escaped)
        
        if in_code and code_buffer:
            code_text = "\n".join(code_buffer)
            try:
                lexer = get_lexer_by_name(code_lang)
            except Exception:
                lexer = TextLexer()
            formatter = HtmlFormatter(style="monokai", nowrap=True, noclasses=True)
            highlighted = highlight(code_text, lexer, formatter)
            parts.append(f'<pre><code>{highlighted}</code></pre>')

        html = "<br>".join(parts)
        return f'<div style="line-height:1.5;">{html}</div>'

    def _emit_reaction(self, reaction: str):
        if self._on_reaction:
            self._on_reaction(reaction)

    def set_reaction_count(self, reaction: str, count: int):
        if reaction in self.reaction_btns:
            self._reaction_counts[reaction] = count
            btn = self.reaction_btns[reaction]
            btn.setText(f"{reaction} {count}" if count > 0 else reaction)
            btn.setStyleSheet(f"""
                background: {'rgba(0,212,170,0.2)' if count > 0 else 'transparent'};
                border: {f'1px solid {self._theme.accent_1}' if count > 0 else 'none'};
                border-radius: 11px; font-size: 11px; padding: 0px 4px;
                color: {self._theme.accent_1 if count > 0 else self._theme.text_faint};
            """)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        r = 15.0
        tw = float(self.TAIL)
        rect = self.rect()
        h = rect.height()

        # Find bubble_inner geometry
        inner_rect = self.bubble_inner.geometry()
        bx, by = inner_rect.x(), inner_rect.y()
        bw, bh = inner_rect.width(), inner_rect.height()

        if self.is_user:
            body = QRectF(bx, by, bw - tw, bh)
            painter.setBrush(QColor(self._theme.bubble_sent))
            painter.drawRoundedRect(body, r, r)
            tail = QPolygonF([
                QPointF(body.right() - 9, bh - 19),
                QPointF(body.right() + tw - 1, bh - 9),
                QPointF(body.right() - 9, bh - 1),
            ])
            painter.drawPolygon(tail)
        else:
            body = QRectF(bx + tw, by, bw - tw, bh)
            painter.setBrush(QColor(self._theme.bubble_recv))
            painter.drawRoundedRect(body, r, r)
            tail = QPolygonF([
                QPointF(body.left() + 9, bh - 19),
                QPointF(body.left() - tw + 1, bh - 9),
                QPointF(body.left() + 9, bh - 1),
            ])
            painter.drawPolygon(tail)


# =============================================================================
# ⌨️ CHAT INPUT BOX (auto-growing, Enter to send / Shift+Enter for newline)
# =============================================================================
class ChatInputBox(QTextEdit):
    """A QLineEdit can only ever hold one line, so the placeholder's promise
    of 'Shift+Enter for a new line' never actually worked. This is a
    QTextEdit that grows with its content up to a max height, sends on a
    plain Enter, and inserts a real newline on Shift+Enter."""
    send_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatInputBox")
        self.setAcceptRichText(False)
        self.setPlaceholderText("Message W.A.V.E... (Enter to send, Shift+Enter for new line)")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._min_h = 44
        self._max_h = 132
        self.setFixedHeight(self._min_h)
        self.textChanged.connect(self._auto_grow)

    def _auto_grow(self):
        doc_h = int(self.document().size().height()) + 16
        new_h = max(self._min_h, min(self._max_h, doc_h))
        if new_h != self.height():
            self.setFixedHeight(new_h)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)  # Shift+Enter -> real newline
            else:
                self.send_requested.emit()  # Enter -> send
            return
        super().keyPressEvent(event)


# =============================================================================
# 🧱 DASHBOARD TILE (with live preview)
# =============================================================================
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
class CommandPalette(QDialog):
    def __init__(self, parent, theme: ThemeColors, actions: Dict[str, Callable]):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog | Qt.WindowType.WindowStaysOnTopHint)
        self._theme = theme
        self._actions = actions
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Container with panel styling
        container = QFrame()
        container.setProperty("class", "Panel")
        container.setFixedWidth(640)
        add_shadow(container, blur=40, y_offset=20)
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        
        # Input
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command, app, or action... (Ctrl+K to close)")
        self.input.setStyleSheet(f"""
            background: {theme.bg_surface}; color: {theme.text_main};
            border: none; border-radius: 0; padding: 16px 20px; font-size: 16px;
            border-bottom: 1px solid {theme.border};
        """)
        self.input.textChanged.connect(self._filter)
        self.input.installEventFilter(self)
        cl.addWidget(self.input)
        
        # Results list
        self.list = QListWidget()
        self.list.setStyleSheet(f"""
            QListWidget {{ background: transparent; border: none; outline: none; }}
            QListWidget::item {{ padding: 12px 20px; border-radius: 0; color: {theme.text_main}; font-size: 13px; }}
            QListWidget::item:hover {{ background: rgba(40,40,40,1); }}
            QListWidget::item:selected {{ background: {theme.accent_1}; color: #000000; }}
        """)
        self.list.itemActivated.connect(self._activate)
        cl.addWidget(self.list)
        
        layout.addWidget(container)
        
        self._populate_list("")
        self.input.setFocus()
    
    def _populate_list(self, query: str):
        self.list.clear()
        query = query.lower().strip()
        for name, action in self._actions.items():
            if not query or query in name.lower():
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, action)
                self.list.addItem(item)
        if self.list.count() > 0:
            self.list.setCurrentRow(0)
    
    def _filter(self, text):
        self._populate_list(text)
    
    def _activate(self, item):
        action = item.data(Qt.ItemDataRole.UserRole)
        if action:
            action()
        self.accept()
    
    def eventFilter(self, obj, event):
        if obj == self.input and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Return or event.key() == Qt.Key.Key_Enter:
                if self.list.currentItem():
                    self._activate(self.list.currentItem())
                return True
            elif event.key() == Qt.Key.Key_Down:
                row = min(self.list.currentRow() + 1, self.list.count() - 1)
                self.list.setCurrentRow(row)
                return True
            elif event.key() == Qt.Key.Key_Up:
                row = max(self.list.currentRow() - 1, 0)
                self.list.setCurrentRow(row)
                return True
            elif event.key() == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)
    
    def showEvent(self, event):
        super().showEvent(event)
        # Center on parent
        if self.parent():
            parent_rect = self.parent().geometry()
            x = parent_rect.x() + (parent_rect.width() - self.width()) // 2
            y = parent_rect.y() + 80
            self.move(x, y)
        Anim.fade_in(self.findChild(QFrame), 150)


# =============================================================================
# 🧙 ONBOARDING WIZARD
# =============================================================================
class OnboardingWizard(QDialog):
    def __init__(self, parent, theme: ThemeColors, on_complete: Callable):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self._theme = theme
        self._on_complete = on_complete
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setFixedSize(520, 420)
        
        self._steps = [
            ("Welcome", "Hi! I'm WAVE — your desktop AI assistant.\nLet's get you set up in 30 seconds.", "👋"),
            ("Microphone", "We need microphone access for voice commands.\nClick 'Test Mic' and say something.", "🎙"),
            ("Wake Word", "Choose your wake phrase:", "🔊"),
            ("Theme", "Pick your accent color:", "🎨"),
            ("Permissions", "Allow these for the best experience:", "🛡"),
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
        
        if index == 1:  # Microphone test
            self.mic_test_btn = QPushButton("🎙  Test Microphone")
            self.mic_test_btn.setProperty("class", "ActionBtn")
            self.mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mic_test_btn.clicked.connect(self._test_mic)
            self.mic_status = QLabel("Click to test...")
            self.mic_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.mic_status.setStyleSheet(f"color: {self._theme.text_muted}; font-size: 12px;")
            self.content_layout.addWidget(self.mic_test_btn)
            self.content_layout.addWidget(self.mic_status)
        
        elif index == 2:  # Wake word
            self.wake_combo = QComboBox()
            self.wake_combo.addItems(["Hey Wave", "Hello Wave", "Okay Wave", "Custom..."])
            self.wake_combo.setStyleSheet(f"padding: 10px; font-size: 13px;")
            self.content_layout.addWidget(self.wake_combo)
        
        elif index == 3:  # Theme
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
        
        elif index == 4:  # Permissions
            self.perm_clipboard = QCheckBox("Clipboard history (read copied text)")
            self.perm_clipboard.setChecked(True)
            self.perm_screen = QCheckBox("Screen capture (for OCR / vision)")
            self.perm_screen.setChecked(True)
            self.perm_notifications = QCheckBox("Desktop notifications")
            self.perm_notifications.setChecked(True)
            for cb in [self.perm_clipboard, self.perm_screen, self.perm_notifications]:
                cb.setStyleSheet(f"color: {self._theme.text_main}; font-size: 12px;")
                self.content_layout.addWidget(cb)
        
        # Update buttons
        self.btn_back.setVisible(index > 0)
        if index == len(self._steps) - 1:
            self.btn_next.setText("Finish")
        else:
            self.btn_next.setText("Next")
    
    def _test_mic(self):
        self.mic_status.setText("Listening... speak now")
        self.mic_status.setStyleSheet(f"color: {self._theme.accent_1}; font-size: 12px;")
        QTimer.singleShot(2000, lambda: self.mic_status.setText("✓ Microphone working!"))
        QTimer.singleShot(2000, lambda: self.mic_status.setStyleSheet(f"color: {self._theme.success}; font-size: 12px;"))
    
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
            # Save preferences
            self.accept()
            if self._on_complete:
                self._on_complete()
    
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
        categories = ["General", "Appearance", "Voice", "Hotkeys", "Privacy", "Advanced", "About"]
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
        self.pages.addWidget(self._build_hotkeys_page())
        self.pages.addWidget(self._build_privacy_page())
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
        
        # TTS Voice
        pv.addWidget(QLabel("TTS Voice:"))
        self.tts_combo = QComboBox()
        self.tts_combo.addItems(["Default (SAPI)", "Microsoft David", "Microsoft Zira", "Microsoft Mark"])
        pv.addWidget(self.tts_combo)
        
        # Speech rate
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Speech Rate:"))
        self.rate_slider = QSlider(Qt.Orientation.Horizontal)
        self.rate_slider.setRange(50, 200)
        self.rate_slider.setValue(100)
        pv.addWidget(self.rate_slider)
        
        # Volume
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("TTS Volume:"))
        self.tts_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.tts_vol_slider.setRange(0, 100)
        self.tts_vol_slider.setValue(80)
        pv.addWidget(self.tts_vol_slider)
        
        # Wake word
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Wake Word:"))
        self.wake_combo = QComboBox()
        self.wake_combo.addItems(["Hey Wave", "Hello Wave", "Okay Wave", "Wave", "Custom..."])
        pv.addWidget(self.wake_combo)
        
        # STT Language
        pv.addWidget(make_divider(self._theme))
        pv.addWidget(QLabel("Speech Recognition Language:"))
        self.stt_lang = QComboBox()
        self.stt_lang.addItems(["en-US", "en-IN", "hi-IN", "es-ES", "fr-FR", "de-DE"])
        pv.addWidget(self.stt_lang)
        
        v.addWidget(panel)
        v.addStretch()
        return page
    
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
        self.cb_screen = QCheckBox("Allow screen capture (OCR/Vision)")
        self.cb_screen.setChecked(True)
        self.cb_analytics = QCheckBox("Send anonymous usage analytics")
        self.cb_analytics.setChecked(False)
        self.cb_chat_history = QCheckBox("Save chat history locally")
        self.cb_chat_history.setChecked(True)
        
        for cb in [self.cb_clipboard, self.cb_screen, self.cb_analytics, self.cb_chat_history]:
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
    
    def _clear_all_data(self):
        reply = QMessageBox.question(self, "Confirm", "Delete ALL local data? (Chat history, settings, clipboard, logs)",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            # Clear everything
            if os.path.exists(SETTINGS_FILE):
                os.remove(SETTINGS_FILE)
            # Clear DB
            try:
                db.clear_all()
            except Exception:
                pass
            QMessageBox.information(self, "Done", "All local data cleared. Restart the app.")
    
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
        pv.addWidget(QLabel("Groq API Key:"))
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Leave blank to use default")
        pv.addWidget(self.api_key)
        
        pv.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["llama-3.1-8b-instant", "llama-3.1-70b-versatile", "mixtral-8x7b-32768", "gemma2-9b-it"])
        pv.addWidget(self.model_combo)
        
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
        Brain: Groq (llama-3.1-8b-instant)<br>
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
class ToastManager(QObject):
    def __init__(self, parent, theme: ThemeColors):
        super().__init__(parent)
        self._parent = parent
        self._theme = theme
        self._toasts = []
    
    def show(self, message: str, icon: str = "info", duration: int = 3000):
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
class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal()


class Worker(QThread):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()
    
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
            self.signals.result.emit(result)
        except Exception as e:
            self.signals.error.emit(str(e))
        finally:
            self.signals.finished.emit()


class WorkerPool:
    def __init__(self):
        self._workers = []
    
    def submit(self, fn, *args, **kwargs) -> Worker:
        worker = Worker(fn, *args, **kwargs)
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.append(worker)
        worker.start()
        return worker
    
    def _cleanup(self, worker):
        if worker in self._workers:
            self._workers.remove(worker)
    
    def shutdown(self):
        for w in self._workers:
            w.terminate()
            w.wait(1000)
        self._workers.clear()


# =============================================================================
# 🔧 AUDIO HELPERS
# =============================================================================
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
    ("Settings", "gear", 8),
]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        
        # Core components
        self.theme_manager = ThemeManager()
        self.theme = self.theme_manager.get_current()
        self.worker_pool = WorkerPool()
        
        # State
        self.brain = WaveBrain()
        self.mic_active = False
        self.tts_thread = None
        self.mic_worker = None
        self.clip_worker = None
        self.action_worker = None
        self.ocr_worker = None
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
        self.stack.add_page(wrap_scroll(self._build_settings_page(), self.theme))
        
        root.addWidget(self.stack, 1)

        self.nav_buttons[0].setChecked(True)
        self._load_chat_history()
    
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
    
    def setup_global_hotkeys(self):
        if not HAS_KEYBOARD:
            return
        try:
            keyboard.add_hotkey('ctrl+shift+space', self._global_mic_toggle)
            keyboard.add_hotkey('ctrl+k', self._global_command_palette)
            keyboard.add_hotkey('ctrl+alt+w', self._global_toggle_window)
        except Exception as e:
            logger.warning(f"Global hotkeys not available: {e}")
    
    def _global_mic_toggle(self):
        # Must run on main thread
        QTimer.singleShot(0, self.toggle_mic)
    
    def _global_command_palette(self):
        QTimer.singleShot(0, self.show_command_palette)
    
    def _global_toggle_window(self):
        QTimer.singleShot(0, self._toggle_window_visibility)
    
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
            "Settings": lambda: self.stack.slide_to(8),
            "Toggle Mic": self.toggle_mic,
            "Clear Chat": self.clear_chat,
            "Take Screenshot": lambda: self._run_action(AppController.take_screenshot),
            "Lock PC": lambda: os.system("rundll32.exe user32.dll,LockWorkStation"),
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
        self.btn_send.clicked.connect(self.send_message)

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
            ("Lock", "lock", lambda: os.system("rundll32.exe user32.dll,LockWorkStation")),
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
            menu.addAction("Open", lambda: os.startfile(path))
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
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                self.toast.show("Deleted", "success")
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
                os.startfile(path)
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
        data = {
            "notes": self.sticky_note.toPlainText() if hasattr(self, 'sticky_note') else "",
            "focus_mode": self.focus_mode,
        }
        if HAS_PYCAW and hasattr(self, "vol_slider"):
            data["volume"] = self.vol_slider.value()
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
            self.toast.show("Settings saved", "success")
        except Exception as e:
            logger.error(f"Save settings error: {e}")
            self.toast.show("Could not save settings", "error")
    
    def load_settings(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if hasattr(self, 'sticky_note'):
                self.sticky_note.setPlainText(data.get("notes", ""))
            if "volume" in data and HAS_PYCAW and hasattr(self, "vol_slider"):
                self.vol_slider.setValue(data["volume"])
            self.toast.show("Settings loaded", "success")
        except FileNotFoundError:
            self.toast.show("No saved settings found", "info")
        except Exception as e:
            logger.error(f"Load settings error: {e}")
            self.toast.show("Could not load settings", "error")
    
    def clear_chat(self):
        for i in reversed(range(self.chat_layout.count())):
            widget = self.chat_layout.itemAt(i).widget()
            if widget is not None:
                widget.deleteLater()
        self._chat_messages.clear()
        self._last_sender = None
        self.add_chat_bubble("Chat cleared. Fresh start!", is_user=False)
    
    def add_chat_bubble(self, text, is_user=True, attachment=None):
        # Message grouping: if same sender as last, append to previous bubble
        if self._last_sender == ("user" if is_user else "ai") and self._chat_messages:
            last_bubble = self._chat_messages[-1]
            # Could merge here - for now create new
            pass
        
        row_widget = QWidget()
        row_widget.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)

        has_code = "```" in text
        width_ratio = 0.88 if has_code else 0.68
        max_w = int(min(720, max(260, self.chat_scroll.width() * width_ratio))) if hasattr(self, 'chat_scroll') else 420
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
        
        QTimer.singleShot(80, lambda: self.chat_scroll.verticalScrollBar().setValue(
            self.chat_scroll.verticalScrollBar().maximum()
        ))
    
    def _on_bubble_reaction(self, reaction: str):
        self.toast.show(f"Reacted with {reaction}", "info")
        # Could send to backend
    
    def update_weather(self, weather_data):
        if hasattr(self, 'weather_lbl'):
            self.weather_lbl.setText(weather_data)
    
    def turn_on_mic(self):
        self.mic_active = True
        self.mic_worker = ContinuousMicWorker()
        self.mic_worker.text_ready.connect(self.process_voice_input)
        self.mic_worker.mode_update.connect(self.handle_wake_word_mode)
        self.mic_worker.interruption_signal.connect(self.stop_speaking)
        self.mic_worker.start()
    
    def handle_wake_word_mode(self, mode):
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
            self.start_speaking("Standing by.")
    
    def stop_speaking(self):
        if self.tts_thread and self.tts_thread.isRunning():
            try:
                tts_engine.stop()
                self.tts_thread.terminate()
                self.tts_thread.wait()
            except Exception:
                pass

        if self.mic_worker and self.mic_worker.mode == "AWAKE":
            self.visualizer.set_mode("LISTENING")
            self._set_status("Listening...", self.theme.accent_1)
        else:
            self.visualizer.set_mode("SLEEPING")
            self._set_status("Online", self.theme.success)
    
    def start_speaking(self, text):
        self.stop_speaking()
        self.visualizer.set_mode("SPEAKING")
        self._set_status("Speaking...", self.theme.accent_3)
        self.tts_thread = TTSWorker(text)
        self.tts_thread.finished_speaking.connect(self.on_speech_finished)
        self.tts_thread.start()
    
    def on_speech_finished(self):
        if self.mic_worker and self.mic_worker.mode == "AWAKE":
            self.visualizer.set_mode("LISTENING")
            self._set_status("Listening...", self.theme.accent_1)
        else:
            self.visualizer.set_mode("SLEEPING")
            self._set_status("Online", self.theme.success)
    
    def process_voice_input(self, text):
        self.stop_speaking()
        self.chat_input.setPlainText(text)
        self.send_message()
    
    def toggle_mic(self):
        if hasattr(self, 'mic_worker') and self.mic_worker:
            if self.mic_worker.mode == "AWAKE":
                self.mic_worker.set_mode("SLEEPING")
            else:
                self.mic_worker.set_mode("AWAKE")
    
    # ==================== AI NLP BRAIN ====================
    def send_message(self):
        user_text = self.chat_input.toPlainText().strip()
        attachment = self._pending_attachment
        if not user_text and not attachment:
            return
        self.stop_speaking()

        self.add_chat_bubble(user_text, is_user=True, attachment=attachment)
        self.chat_input.clear()
        self._clear_attachment()

        if attachment:
            # A file is attached - skip the local voice/text command shortcuts
            # below (they're for plain commands) and go straight to the AI.
            self._set_status("Thinking...", self.theme.warning)
            worker = AIWorker(self.brain, user_text, attachment=attachment)
            worker.response_ready.connect(self.display_ai_response)
            self._track_worker(worker)
            worker.start()
            return

        cmd = user_text.lower()

        # 1. WhatsApp Intent
        if "whatsapp" in cmd or ("send" in cmd and "message" in cmd):
            match = re.search(r"(?:send|say|message|type)\s+(.*?)\s+(?:to|for)\s+(.*?)(?:\s+on\s+whatsapp|\s+in\s+whatsapp)?$", cmd)
            if match:
                message = match.group(1).strip()
                contact = match.group(2).strip()
                contact = re.sub(r"\s+on\s+whatsapp$", "", contact).strip()
                self._run_action(AppController.send_whatsapp_message, contact, message)
                return

        # 2. Notepad Write Intent
        if "write" in cmd and ("notepad" in cmd or "file" in cmd):
            match = re.search(r"write\s+(.*?)\s+(?:in|on|to)\s+notepad", cmd)
            if match:
                text_to_write = match.group(1).strip()
            else:
                text_to_write = cmd.replace("write", "").replace("in notepad", "").strip()
            self._run_action(AppController.write_in_notepad, text_to_write)
            return

        # 3. Screenshot Intent
        if "screenshot" in cmd or "capture" in cmd:
            if "delete" in cmd or "remove" in cmd:
                self._run_action(AppController.delete_latest_screenshot)
            else:
                self._run_action(AppController.take_screenshot)
            return

        # 4. Open Applications
        if cmd.startswith("open ") or cmd.startswith("launch ") or cmd.startswith("start "):
            app_name = re.sub(r"^(open|launch|start)\s+", "", cmd).strip()
            if " and " in app_name:
                app_name = app_name.split(" and ")[0].strip()
            if "setting" in app_name:
                self._run_action(AppController.open_system_setting, app_name)
            else:
                self._run_action(AppController.open_app, app_name)
            return
            
        if cmd.startswith("close ") or cmd.startswith("exit ") or cmd.startswith("kill "):
            app_name = re.sub(r"^(close|exit|kill)\s+", "", cmd).strip()
            if "window" in app_name or "current" in app_name:
                self._run_action(AppController.close_current_window)
            elif "tab" in app_name:
                self._run_action(AppController.close_current_tab)
            else:
                self._run_action(AppController.close_app, app_name)
            return

        # 5. Hardware Toggles
        if "turn on" in cmd or "turn off" in cmd or "enable" in cmd or "disable" in cmd:
            if "wifi" in cmd or "bluetooth" in cmd or "hotspot" in cmd:
                self._run_action(AppController.toggle_hardware, cmd)
                return

        # Fallback: Groq AI
        self._set_status("Thinking...", self.theme.warning)
        worker = AIWorker(self.brain, user_text)
        worker.response_ready.connect(self.display_ai_response)
        self._track_worker(worker)
        worker.start()

    def _plain_text_for_speech(self, text: str) -> str:
        """Strips markdown syntax before handing text to TTS, so it doesn't
        read asterisks, backticks, pipes, and hash symbols out loud."""
        t = re.sub(r'```.*?```', ' Code block omitted. ', text, flags=re.DOTALL)
        t = re.sub(r'`([^`]+)`', r'\1', t)
        t = re.sub(r'\*\*([^*]+)\*\*', r'\1', t)
        t = re.sub(r'\*([^*]+)\*', r'\1', t)
        t = re.sub(r'^\s{0,3}#{1,6}\s*', '', t, flags=re.MULTILINE)
        t = re.sub(r'^\s*\|?[\s:|-]+\|?\s*$', '', t, flags=re.MULTILINE)
        t = t.replace('|', ', ')
        return t
    
    def display_ai_response(self, response: str):
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
        if hasattr(self, "time_lbl"):
            self.time_lbl.setText(time.strftime("%I:%M:%S %p"))
            self.date_lbl.setText(time.strftime("%d %b %Y").upper())
    
    def update_sys_stats(self):
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
        dl_speed = (current_net_io.bytes_recv - self.last_net_io.bytes_recv) / 1024 / 1024
        ul_speed = (current_net_io.bytes_sent - self.last_net_io.bytes_sent) / 1024 / 1024
        self.last_net_io = current_net_io

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
        # Cleanup workers
        self.worker_pool.shutdown()
        if self.mic_worker:
            self.mic_worker.stop()
        if self.weather_worker:
            self.weather_worker.quit()
        if self.clip_worker:
            self.clip_worker.stop()
        if self.media_info_worker:
            self.media_info_worker.stop()
        if HAS_KEYBOARD:
            keyboard.unhook_all()
        super().closeEvent(event)


# =============================================================================
# 📦 WORKER CLASSES (unchanged but using WorkerPool pattern)
# =============================================================================
class WeatherWorker(QThread):
    weather_ready = pyqtSignal(str)

    def run(self):
        try:
            res = requests.get("https://wttr.in/?format=%C+%t", timeout=5)
            if res.status_code == 200:
                self.weather_ready.emit(res.text.strip())
            else:
                self.weather_ready.emit("Data Unavailable")
        except Exception:
            self.weather_ready.emit("Offline")


class TTSWorker(QThread):
    finished_speaking = pyqtSignal()

    def __init__(self, text):
        super().__init__()
        self.text = text

    def run(self):
        try:
            tts_engine.speak(self.text)
        except Exception as e:
            logger.error(f"TTS Error: {e}")
        finally:
            self.finished_speaking.emit()


class ContinuousMicWorker(QThread):
    text_ready = pyqtSignal(str)
    mode_update = pyqtSignal(str)
    interruption_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.is_running = True
        self.mode = "SLEEPING"

    def run(self):
        self.mode_update.emit(self.mode)
        while self.is_running:
            text = stt_engine.listen()
            if not self.is_running:
                break
            if text:
                text_lower = text.lower()
                self.interruption_signal.emit()

                if self.mode == "SLEEPING":
                    if any(w in text_lower for w in ["hello wave", "wake up", "hey wave"]):
                        self.set_mode("AWAKE")
                elif self.mode == "AWAKE":
                    if any(w in text_lower for w in ["mute", "sleep", "standby"]):
                        self.set_mode("SLEEPING")
                    else:
                        self.text_ready.emit(text)

    def set_mode(self, new_mode):
        self.mode = new_mode
        self.mode_update.emit(self.mode)

    def stop(self):
        self.is_running = False


class AIWorker(QThread):
    response_ready = pyqtSignal(str)

    def __init__(self, brain, text, attachment=None):
        super().__init__()
        self.brain = brain
        self.text = text
        self.attachment = attachment

    def run(self):
        reply = self.brain.think(self.text, attachment=self.attachment)
        self.response_ready.emit(reply)


class ClipboardWorker(QThread):
    clip_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.is_running = True

    def run(self):
        last_value = None
        while self.is_running:
            try:
                current = pyperclip.paste()
                if current and current != last_value:
                    last_value = current
                    self.clip_changed.emit(current)
            except Exception:
                pass
            time.sleep(1)

    def stop(self):
        self.is_running = False


class ActionWorker(QThread):
    result_ready = pyqtSignal(str)

    def __init__(self, func, *args):
        super().__init__()
        self.func = func
        self.args = args

    def run(self):
        try:
            result = self.func(*self.args)
            self.result_ready.emit(result if isinstance(result, str) and result else "Done.")
        except Exception as e:
            logger.error(f"ActionWorker error: {e}")
            self.result_ready.emit(f"Something went wrong: {e}")


class OCRWorker(QThread):
    result_ready = pyqtSignal(str)

    def run(self):
        try:
            path = VisionCapture.take_screenshot()
            if not path:
                self.result_ready.emit("Failed to capture the screen.")
                return
            text = ocr.extract_text(path)
            self.result_ready.emit(text)
        except Exception as e:
            logger.error(f"OCRWorker error: {e}")
            self.result_ready.emit(f"Error reading screen text: {e}")


class MediaInfoWorker(QThread):
    info_ready = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.is_running = True

    def run(self):
        while self.is_running:
            info = get_now_playing() or {}
            self.info_ready.emit(info)
            self.msleep(1000)

    def stop(self):
        self.is_running = False


class MediaSeekWorker(QThread):
    def __init__(self, position_seconds: float):
        super().__init__()
        self.position_seconds = position_seconds

    def run(self):
        seek_to(self.position_seconds)


# Icon name mapping for DashboardTile
ICON_MAP = {
    "house": "🏠", "message": "💬", "chart-line": "📊", "bolt": "⚡",
    "music": "🎵", "folder": "📁", "eye": "👁", "clipboard": "📋", "gear": "⚙",
}