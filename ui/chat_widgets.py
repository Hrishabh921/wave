"""
ui/chat_widgets.py
=============================================================================
ChatBubble (rendered chat message, markdown/code-highlighting) and
ChatInputBox (the multi-line chat entry widget).
Split out of the original ui/main_window.py (see ui/common.py for why).
"""
from ui.common import *
from ui.theme import ThemeColors


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
        # Tier 11: the CURRENT text - a streamed reply's bubble is updated
        # in place via set_text(), so replay/copy must read this, not the
        # text the bubble happened to be created with.
        self._text = text or ""
        self._max_width = max_width

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
                replay_btn.clicked.connect(lambda: on_replay(self._text))
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
        self.copy_btn.clicked.connect(lambda: pyperclip.copy(self._text) if HAS_PYPERCLIP else None)
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

    def text(self) -> str:
        return self._text

    def set_text(self, text: str, max_width: int = None):
        """Tier 11: replaces the bubble's text in place - a streamed reply
        grows through this (MainWindow throttles the calls). max_width lets
        the caller widen the bubble once a code block shows up mid-stream."""
        self._text = text or ""
        if max_width:
            self._max_width = max_width
            self.setMaximumWidth(max_width)
        self.text_browser.setVisible(True)
        self.text_browser.setMaximumHeight(16777215)   # QWIDGETSIZE_MAX - undo the empty-bubble collapse
        self.text_browser.setHtml(self._format_message(self._text) if self._text.strip() else "")
        if self._text.strip():
            self._size_text_browser(self._max_width)

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
