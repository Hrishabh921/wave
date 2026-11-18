"""
ui/theme.py
=============================================================================
ThemeColors (dataclass of all theme tokens) and ThemeManager (loads/saves
/ applies themes, built-in presets + custom themes in settings.THEMES_DIR).
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *




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

    # Tier 21: design tokens - spacing, radius, type scale, and motion
    # duration/easing as named, themeable fields instead of literal numbers
    # sprinkled through build_stylesheet()'s QSS and ui/effects.py's Anim
    # defaults. Deliberately NOT a full rewrite of every literal in
    # build_stylesheet() below: this project has no way to visually verify
    # a stylesheet change in this sandbox (no display, no screenshot of a
    # real running app), so each default here was set to match an existing
    # literal EXACTLY where build_stylesheet() now references the token -
    # converting those call sites is zero-risk (the rendered pixel value
    # doesn't change), while sites whose fine-tuned literal doesn't line up
    # with a clean scale value were deliberately left as literals rather
    # than silently nudged to the nearest token and risking an unverifiable
    # visual regression. See build_stylesheet()'s own comment for exactly
    # which rules were converted.
    spacing_xs: int = 4
    spacing_sm: int = 8
    spacing_md: int = 16
    spacing_lg: int = 24
    spacing_xl: int = 32
    radius_sm: int = 10
    radius_md: int = 14
    radius_lg: int = 18
    radius_xl: int = 22
    font_size_xs: int = 10
    font_size_sm: int = 12
    font_size_md: int = 13
    font_size_lg: int = 16
    font_size_xl: int = 21
    motion_duration_fast_ms: int = 150
    motion_duration_normal_ms: int = 250
    motion_duration_slow_ms: int = 400
    motion_stagger_delay_ms: int = 50   # per-item delay in Anim.stagger_show()
    motion_easing: str = "OutCubic"   # a QEasingCurve.Type name - see ui/effects.py's _easing_curve()

    # Tier 21: a handful of hover/pressed/header/dialog surfaces that used
    # to be bare "rgba(40,40,40,1)"-style literals directly in
    # build_stylesheet() below - fine for a dark theme, but completely
    # unreadable (dark-on-dark) on a light one. Pulled out into their own
    # fields, still defaulting to the exact same literal every existing
    # dark theme already rendered with (so no dark theme's look changes),
    # so a light theme (see _load_builtin_themes() below) can override just
    # these to their light equivalents without having to fork the entire
    # 200+ line stylesheet.
    surface_hover: str = "rgba(40,40,40,1)"
    surface_pressed: str = "rgba(10,10,10,1)"
    surface_header: str = "rgba(30,30,30,1)"
    dialog_bg: str = "#0A0A0A"
    page_bg_rgb: str = "20,20,20"   # combined with page_bg_alpha above as rgba({page_bg_rgb},{page_bg_alpha})

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

        # Tier 21: expanding from 5 (all dark) to 10+ themes, including
        # genuinely light ones, to exercise the new surface_hover/pressed/
        # header/dialog_bg/page_bg_rgb tokens added above specifically for
        # this. Two more dark palettes first (same pattern as the five
        # above, no new tokens needed), then the light themes.
        self._themes["Slate Dark"] = ThemeColors(
            name="Slate Dark",
            bg_main="#0A0C10",
            bg_panel="rgba(22,26,34,0.95)",
            bg_surface="rgba(28,33,42,0.98)",
            bg_surface2="#181C24",
            border="rgba(150,170,200,0.2)",
            border_soft="rgba(150,170,200,0.1)",
            accent_1="#7B9FE0",
            accent_2="#A0B8F0",
            accent_3="#FFD60A",
            bubble_sent="#7B9FE0",
            bubble_recv="#181C24",
        )
        self._themes["Violet Dark"] = ThemeColors(
            name="Violet Dark",
            bg_main="#120818",
            bg_panel="rgba(35,15,45,0.95)",
            bg_surface="rgba(45,20,58,0.98)",
            bg_surface2="#2A1038",
            border="rgba(190,120,255,0.2)",
            border_soft="rgba(190,120,255,0.1)",
            accent_1="#B478FF",
            accent_2="#D9A8FF",
            accent_3="#FFD60A",
            bubble_sent="#B478FF",
            bubble_recv="#2A1038",
        )

        # Light themes: the ThemeColors dataclass defaults (text_main white,
        # surface_hover/pressed/header/dialog_bg dark) all assume a dark
        # background, so every light theme below overrides those tokens too,
        # not just bg_*/border - otherwise text and hover states would
        # render dark-on-light-background-but-still-dark-chrome, unreadable.
        # Every value here is a fresh, deliberate light-appropriate choice
        # (there's no "existing literal" to preserve, since no theme has
        # ever been light before), checked only for internal contrast
        # (dark text on light surfaces, light text never used), not against
        # a real rendered screenshot (no display in this sandbox).
        self._themes["Daylight"] = ThemeColors(
            name="Daylight",
            bg_main="#F4F5F7",
            bg_panel="rgba(255,255,255,0.95)",
            bg_surface="rgba(248,249,251,0.98)",
            bg_surface2="#FFFFFF",
            border="rgba(0,0,0,0.12)",
            border_soft="rgba(0,0,0,0.06)",
            text_main="#1A1C1E",
            text_muted="#5B5F66",
            text_faint="#8A8E96",
            accent_1="#0A84FF",
            accent_2="#FF6B9D",
            accent_3="#E0A500",
            success="#1EA672",
            warning="#B8860B",
            danger="#D92D4C",
            bubble_sent="#0A84FF",
            bubble_recv="#EAEBEF",
            surface_hover="rgba(225,227,232,1)",
            surface_pressed="rgba(205,208,215,1)",
            surface_header="rgba(240,241,244,1)",
            dialog_bg="#FFFFFF",
            page_bg_rgb="244,245,247",
        )
        self._themes["Paper Light"] = ThemeColors(
            name="Paper Light",
            bg_main="#F7F2E9",
            bg_panel="rgba(255,252,245,0.95)",
            bg_surface="rgba(252,248,239,0.98)",
            bg_surface2="#FFFDF7",
            border="rgba(90,70,40,0.15)",
            border_soft="rgba(90,70,40,0.08)",
            text_main="#2A2316",
            text_muted="#6B5E45",
            text_faint="#9A8C6E",
            accent_1="#B4651E",
            accent_2="#C94E4E",
            accent_3="#2F7A4F",
            success="#2F7A4F",
            warning="#B4651E",
            danger="#C94E4E",
            bubble_sent="#B4651E",
            bubble_recv="#EEE4D0",
            surface_hover="rgba(232,224,205,1)",
            surface_pressed="rgba(214,204,180,1)",
            surface_header="rgba(242,235,220,1)",
            dialog_bg="#FFFCF5",
            page_bg_rgb="247,242,233",
        )
        self._themes["Ocean Light"] = ThemeColors(
            name="Ocean Light",
            bg_main="#EEF5F8",
            bg_panel="rgba(255,255,255,0.95)",
            bg_surface="rgba(246,251,253,0.98)",
            bg_surface2="#FFFFFF",
            border="rgba(0,80,120,0.12)",
            border_soft="rgba(0,80,120,0.06)",
            text_main="#0D2430",
            text_muted="#4A6A78",
            text_faint="#7D98A3",
            accent_1="#0088B3",
            accent_2="#00B4D8",
            accent_3="#E0A500",
            success="#188F5A",
            warning="#B8860B",
            danger="#D92D4C",
            bubble_sent="#0088B3",
            bubble_recv="#DCEDF3",
            surface_hover="rgba(216,233,238,1)",
            surface_pressed="rgba(192,216,224,1)",
            surface_header="rgba(230,242,246,1)",
            dialog_bg="#FFFFFF",
            page_bg_rgb="238,245,248",
        )
        self._themes["Mint Light"] = ThemeColors(
            name="Mint Light",
            bg_main="#EFF8F3",
            bg_panel="rgba(255,255,255,0.95)",
            bg_surface="rgba(247,253,250,0.98)",
            bg_surface2="#FFFFFF",
            border="rgba(0,100,70,0.12)",
            border_soft="rgba(0,100,70,0.06)",
            text_main="#0C2A1D",
            text_muted="#4A6B5A",
            text_faint="#7E9A8C",
            accent_1="#00A878",
            accent_2="#00D4AA",
            accent_3="#E0A500",
            success="#00A878",
            warning="#B8860B",
            danger="#D92D4C",
            bubble_sent="#00A878",
            bubble_recv="#DCF0E6",
            surface_hover="rgba(214,237,226,1)",
            surface_pressed="rgba(190,220,206,1)",
            surface_header="rgba(228,245,237,1)",
            dialog_bg="#FFFFFF",
            page_bg_rgb="239,248,243",
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
        # Tier 21: the rules below reference t.radius_*/t.font_size_* instead
        # of a literal wherever the token's default exactly matches what was
        # already here: radius_xl=22 (Panel, QLineEdit), radius_md=14 (base
        # QPushButton, NavItem, ActionBtn, StickyNote, SaveBtn), radius_lg=18
        # (IconBtn), radius_sm=10 (QComboBox, QSpinBox, the QMessageBox
        # button, QListWidget::item); font_size_xl=21 (PageTitle),
        # font_size_md=13 (the base QLabel), font_size_sm=12 (ActionBtn,
        # StickyNote), font_size_xs=10 (Tagline). See the token fields' own
        # comment in ThemeColors for why every OTHER radius/font-size
        # literal below (RoundSm's 19px, MicInline's 15px, the many
        # fine-tuned 10.5px/11.5px/12.5px label sizes, ...) was deliberately
        # left alone rather than nudged to the nearest token value - this is
        # a zero-visual-risk pass, not a full rewrite.
        return f"""
        QMainWindow {{ background-color: {t.bg_main}; }}
        QWidget {{ font-family: 'Segoe UI'; }}
        QToolTip {{ background-color: {t.bg_surface2}; color: {t.text_main}; border: 1px solid {t.border}; padding: 4px 8px; border-radius: 6px; }}

        QWidget#Sidebar {{ background-color: {t.bg_panel}; border-right: 1px solid {t.border_soft}; }}
        QWidget#PageBg {{ background-color: rgba({t.page_bg_rgb},{t.page_bg_alpha}); }}
        QWidget#ChatHeader {{ background-color: {t.bg_panel}; border-bottom: 1px solid {t.border_soft}; }}
        QWidget#ChatInputBar {{ background-color: {t.bg_panel}; border-top: 1px solid {t.border_soft}; }}

        QFrame.Panel {{
            background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 rgba(30,30,30,{t.panel_top_alpha}), stop:1 rgba(20,20,20,{t.panel_bottom_alpha}));
            border-radius: {t.radius_xl}px;
            border: 1px solid {t.border};
            border-top: 1px solid rgba(255,255,255,0.18);
        }}
        QFrame.Divider {{
            background-color: {t.border};
            max-height: 1px; min-height: 1px; border: none;
        }}

        QLabel {{ color: {t.text_muted}; font-size: {t.font_size_md}px; background: transparent; }}
        QLabel.Wordmark {{ color: {t.text_main}; font-size: 18px; font-weight: 800; letter-spacing: 1.5px; }}
        QLabel.Tagline {{ color: {t.text_faint}; font-size: {t.font_size_xs}px; }}
        QLabel.Heading {{ color: {t.text_faint}; font-size: 11px; font-weight: 700; letter-spacing: 2px; }}
        QLabel.PageTitle {{ color: {t.text_main}; font-size: {t.font_size_xl}px; font-weight: 800; }}
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
            border-radius: {t.radius_xl}px;
            padding: 12px 20px;
            font-size: 14px;
        }}
        QLineEdit:focus {{ border: 1px solid {t.accent_1}; background-color: {t.surface_hover}; }}
        QLineEdit::placeholder {{ color: {t.text_faint}; }}

        QTextEdit#ChatInputBox {{
            background-color: {t.bg_surface};
            color: {t.text_main};
            border: 1px solid {t.border};
            border-radius: 20px;
            padding: 10px 16px;
            font-size: 14px;
        }}
        QTextEdit#ChatInputBox:focus {{ border: 1px solid {t.accent_1}; background-color: {t.surface_hover}; }}

        QTextEdit#StickyNote {{
            background-color: {t.bg_surface};
            color: {t.text_main};
            border: 1px solid {t.border};
            border-radius: {t.radius_md}px;
            padding: 12px;
            font-size: {t.font_size_sm}px;
        }}
        QTextEdit#StickyNote:focus {{ border: 1px solid {t.accent_1}; }}

        QPushButton {{
            background-color: {t.bg_surface};
            color: {t.text_muted};
            border-radius: {t.radius_md}px;
            padding: 10px 15px;
            font-weight: 600;
            font-size: 12px;
            border: 1px solid {t.border};
        }}
        QPushButton:hover {{ background-color: rgba(50,50,50,1); border: 1px solid {t.accent_1}; color: {t.text_main}; }}
        QPushButton:pressed {{ background-color: {t.surface_pressed}; }}

        QPushButton.NavItem {{
            text-align: left; padding: 11px 14px; border-radius: {t.radius_md}px;
            background: transparent; color: {t.text_muted}; border: none;
            font-size: 12.5px; font-weight: 700;
        }}
        QPushButton.NavItem:hover {{ background-color: {t.surface_hover}; color: {t.text_main}; }}
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
            background: transparent; border: none; border-radius: {t.radius_lg}px;
            font-size: 15px; color: {t.text_muted}; padding: 0px;
        }}
        QPushButton.IconBtn:hover {{ background-color: {t.surface_hover}; color: {t.text_main}; }}

        QPushButton.ActionBtn {{
            padding: 10px 16px; font-size: {t.font_size_sm}px; border-radius: {t.radius_md}px;
            background-color: {t.bg_surface}; color: {t.text_muted}; border: 1px solid {t.border};
        }}
        QPushButton.ActionBtn:hover {{ color: {t.accent_2}; border: 1px solid {t.accent_2}; }}

        QPushButton.RoundSm {{
            border-radius: 19px; font-size: 14px; background-color: {t.bg_surface};
            color: {t.text_main}; border: 1px solid {t.border}; padding: 0px;
        }}

        QPushButton#SaveBtn {{
            border-radius: {t.radius_md}px;
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
        QTreeView::item:hover {{ background-color: {t.surface_hover}; }}
        QHeaderView::section {{
            background-color: {t.surface_header}; color: {t.text_muted}; border: none;
            padding: 8px; font-size: 10.5px; font-weight: 700;
        }}

        QScrollArea {{ border: none; background-color: transparent; }}
        QScrollBar:vertical {{ border: none; background: transparent; width: 6px; margin: 0px; }}
        QScrollBar::handle:vertical {{ background: {t.border}; min-height: 20px; border-radius: 3px; }}
        QScrollBar::handle:vertical:hover {{ background: {t.accent_1}; }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}

        QMessageBox {{ background-color: {t.dialog_bg}; border: 1px solid {t.border}; }}
        QMessageBox QLabel {{ color: {t.text_main}; font-size: 13px; background: transparent; padding: 6px; }}
        QMessageBox QPushButton {{
            background-color: {t.bg_surface2}; color: {t.text_main}; border: 1px solid {t.border};
            border-radius: {t.radius_sm}px; padding: 8px 18px; font-weight: 700; min-width: 90px;
        }}
        QMessageBox QPushButton:hover {{ border: 1px solid {t.accent_1}; color: {t.accent_1}; }}

        QDialog {{ background-color: {t.bg_main}; }}
        QListWidget {{ background: transparent; border: none; outline: none; }}
        QListWidget::item {{ padding: 10px 14px; border-radius: {t.radius_sm}px; color: {t.text_main}; }}
        QListWidget::item:hover {{ background: {t.surface_hover}; }}
        QListWidget::item:selected {{ background: {t.accent_1}; color: #000000; }}

        QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 12px; background: {t.bg_panel}; }}
        QTabBar::tab {{ background: transparent; color: {t.text_muted}; padding: 10px 20px; font-weight: 600; }}
        QTabBar::tab:selected {{ color: {t.accent_1}; }}
        QTabBar::tab:hover {{ color: {t.text_main}; }}

        QComboBox {{ background: {t.bg_surface}; color: {t.text_main}; border: 1px solid {t.border}; border-radius: {t.radius_sm}px; padding: 8px 12px; }}
        QComboBox::drop-down {{ border: none; width: 24px; }}
        QComboBox QAbstractItemView {{ background: {t.bg_panel}; color: {t.text_main}; selection-background-color: {t.accent_1}; selection-color: #000000; border: 1px solid {t.border}; }}

        QSpinBox {{ background: {t.bg_surface}; color: {t.text_main}; border: 1px solid {t.border}; border-radius: {t.radius_sm}px; padding: 8px 12px; }}
        QCheckBox {{ color: {t.text_main}; spacing: 8px; }}
        QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 4px; border: 1px solid {t.border}; background: {t.bg_surface}; }}
        QCheckBox::indicator:checked {{ background: {t.accent_1}; border: 1px solid {t.accent_1}; image: url(:/qta/check); }}

        QProgressBar {{ border: 1px solid {t.border}; border-radius: 6px; background: {t.bg_surface2}; text-align: center; color: {t.text_main}; }}
        QProgressBar::chunk {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {t.accent_1}, stop:1 {t.accent_2}); border-radius: 5px; }}
        """


# =============================================================================
# 🎯 ICON HELPER (qtawesome fallback to emoji)
# =============================================================================
