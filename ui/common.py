"""
ui/common.py
=============================================================================
Shared imports, optional-dependency feature flags (HAS_*), and module-level
constants used across the ui/ package.

This module exists so that ui/main_window.py's 22 classes (originally one
5,245-line file) could be split into focused files under ui/ without each
one having to hand-pick which of the ~40 third-party/project imports it
needs. Every ui/*.py split file does `from ui.common import *` and gets the
same surface the classes had when they all lived in one file together.

NOTE: this intentionally favors a single shared import surface over fully
minimal per-file imports, to make the split safe to do in one pass without
chasing down every class's exact dependency set. If a submodule is later
pulled out of ui/ entirely (e.g. into its own package), trim its imports
down from this wildcard set at that point.
"""

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
    QColorDialog, QFileDialog, QFormLayout, QProgressBar, QToolTip, QInputDialog,
    QTableWidget, QTableWidgetItem, QAbstractItemView,  # Tier 21: performance_panel.py's TTFA tables
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
from automation import audit as audit_mod
from automation import security_log
from automation import undo as undo_mod
from automation.proactive import proactive_engine
from brain.memory_consolidation import memory_consolidator
from brain.skill_loader import get_loader
from automation.thread_supervisor import thread_supervisor
from automation.health_check import run_health_check, Status
from brain.ollama_lifecycle import ollama_lifecycle
from settings.config import config
from monitoring.media_info import get_now_playing, seek_to, format_time, HAS_WINSDK
from ui.workers import (
    WeatherWorker, TTSWorker, ContinuousMicWorker, AIWorker, AIStreamWorker,
    ClipboardWorker, ActionWorker, OCRWorker, MediaInfoWorker, MediaSeekWorker,
    ReminderWorker,
)
from voice import speech_filter
from telemetry import ttfa, trace
# Tier 25: the avatar companion window + its state machine/rig pipeline +
# the Qt-thread-safety seam show_ui/hide_ui dispatch through - see
# avatar/control.py's own module docstring for why MainWindow is the one
# place that imports AvatarController directly (brain/handlers/avatar.py
# never does).
from avatar.window import AvatarWindow
from avatar.state import AvatarStateMachine
from avatar.control import AvatarController, set_active_controller
from avatar import rig as avatar_rig

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

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
SETTINGS_FILE = os.path.join(config.DATA_DIR, "wave_settings.json")   # DATA_DIR == project folder when run from source
THEMES_DIR = os.path.join(config.DATA_DIR, "themes")
AVATAR_RIG_DIR = os.path.join(config.DATA_DIR, "avatar_rig")  # Tier 25: avatar/rig.py's build_rig() output lives here
