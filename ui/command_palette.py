"""
ui/command_palette.py
=============================================================================
CommandPalette: the Ctrl+K style quick-command dialog.
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors
from ui.effects import Anim, add_shadow


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
