"""Tier 9 - logic tests for ui/chat_widgets.py (ChatBubble, ChatInputBox),
WITHOUT a display or PyQt6.

Nothing in test_phase4_ui_logic.py or test_tier5_ui_logic.py touches this
file. The highest-value, never-tested logic here is ChatBubble's
_format_message(): a three-tier fallback (full Markdown+Pygments -> a
hand-rolled fenced-code-block parser using Pygments alone -> plain HTML
escaping) that silently degrades depending on which optional packages are
installed - exactly the kind of branch that's easy to break without
noticing if your own dev machine happens to have `markdown` installed.
Also covered: the attachment chip's KB/MB size formatting and pdf/doc icon
choice, reaction-count styling, and ChatInputBox's Enter-sends /
Shift+Enter-inserts-a-newline key handling plus its auto-grow height clamp.

Reuses test_phase4_ui_logic.py's stand-in. ChatBubble is only ever
constructed via object.__new__() here, never through its real __init__:
that constructor calls _size_text_browser(), which does `int(doc.idealWidth())`
against the stand-in's absorbed-everything QTextBrowser - a real QTextDocument
returns a real float there, but the stand-in's generic absorber object has no
__int__, so a real construction raises TypeError. Testing the handler/helper
methods directly (as test_phase4_ui_logic.py already does for SettingsPanel,
AudioVisualizer, etc.) sidesteps that construction path entirely, which is the
point: this is a logic test, not a construction test. ChatInputBox has no such
landmine, so it's constructed normally.
"""
import sys
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_phase4_ui_logic as P4


def import_with_fakes(module_name):
    """Same retry loop as test_tier9_theme.py / test_tier5_ui_logic.py."""
    for name in [m for m in sys.modules if m == "ui" or m.startswith("ui.")]:
        del sys.modules[name]
    for _ in range(30):
        try:
            return __import__(module_name, fromlist=["_"])
        except ModuleNotFoundError as e:
            missing = (e.name or "").split(".")[0]
            if not missing or missing in sys.modules:
                raise
            sys.modules[missing] = P4._QtModule(missing)


THEME = types.SimpleNamespace(
    accent_1="#00D4AA", text_main="#FFFFFF", text_muted="#B0B0B0", text_faint="#808080",
    border="#333333", border_soft="#222222",
)


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class FormatMessageTests(unittest.TestCase):
    """_format_message() takes no `self` state at all - it's a pure function
    of `text` plus the two module-level HAS_* flags - so these are tested
    straight off a bare object.__new__() instance."""

    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.cw = import_with_fakes("ui.chat_widgets")

    def setUp(self):
        self.bubble = object.__new__(self.cw.ChatBubble)

    # ---------- tier 1: full markdown + pygments ----------
    def test_full_markdown_path_passes_codehilite_extension_when_pygments_available(self):
        fake_md = mock.Mock()
        fake_md.markdown.return_value = "<p>rendered</p>"
        with mock.patch.object(self.cw, "HAS_MARKDOWN", True), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", True), \
             mock.patch.object(self.cw, "_markdown_lib", fake_md, create=True):
            out = self.bubble._format_message("**hi**")
        self.assertEqual(out, '<div style="line-height:1.5;"><p>rendered</p></div>')
        call = fake_md.markdown.call_args
        self.assertEqual(call.args[0], "**hi**")
        self.assertIn("codehilite", call.kwargs["extensions"])
        self.assertIn("fenced_code", call.kwargs["extensions"])

    def test_full_markdown_path_omits_codehilite_when_pygments_unavailable(self):
        fake_md = mock.Mock()
        fake_md.markdown.return_value = "<p>rendered</p>"
        with mock.patch.object(self.cw, "HAS_MARKDOWN", True), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", False), \
             mock.patch.object(self.cw, "_markdown_lib", fake_md, create=True):
            self.bubble._format_message("plain")
        self.assertNotIn("codehilite", fake_md.markdown.call_args.kwargs["extensions"])

    def test_markdown_lib_raising_falls_through_to_the_next_tier(self):
        fake_md = mock.Mock()
        fake_md.markdown.side_effect = RuntimeError("boom")
        with mock.patch.object(self.cw, "HAS_MARKDOWN", True), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", False), \
             mock.patch.object(self.cw, "_markdown_lib", fake_md, create=True):
            out = self.bubble._format_message("hello & <world>")
        # Fell all the way through to the plain-escape tier.
        self.assertEqual(out, '<div style="line-height:1.5;">hello &amp; &lt;world&gt;</div>')

    # ---------- tier 2: pygments-only fenced-code parsing (no `markdown` package) ----------
    def test_pygments_only_tier_highlights_a_fenced_code_block_and_inline_code(self):
        fake_lexer = object()
        with mock.patch.object(self.cw, "HAS_MARKDOWN", False), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", True), \
             mock.patch.object(self.cw, "get_lexer_by_name", return_value=fake_lexer, create=True), \
             mock.patch.object(self.cw, "highlight", return_value="H!GHLIGHTED", create=True), \
             mock.patch.object(self.cw, "HtmlFormatter", lambda **k: "fmt", create=True):
            text = "before `inline` text\n```python\nprint(1)\n```\nafter"
            out = self.bubble._format_message(text)
        self.assertIn("before <code>inline</code> text", out)
        self.assertIn("<pre><code>H!GHLIGHTED</code></pre>", out)
        self.assertIn("after", out)

    def test_pygments_only_tier_falls_back_to_textlexer_on_an_unknown_language(self):
        calls = []
        with mock.patch.object(self.cw, "HAS_MARKDOWN", False), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", True), \
             mock.patch.object(self.cw, "get_lexer_by_name", side_effect=Exception("no such lexer"), create=True), \
             mock.patch.object(self.cw, "TextLexer", lambda: calls.append("text_lexer_used") or "TL", create=True), \
             mock.patch.object(self.cw, "highlight", return_value="X", create=True), \
             mock.patch.object(self.cw, "HtmlFormatter", lambda **k: "fmt", create=True):
            out = self.bubble._format_message("```made-up-lang\ncode\n```")
        self.assertEqual(calls, ["text_lexer_used"])
        self.assertIn("<pre><code>X</code></pre>", out)

    def test_pygments_only_tier_still_flushes_an_unclosed_trailing_code_fence(self):
        """Text that ends mid-fence (no closing ``` ) - the tail-handling
        branch at the end of _format_message, easy to forget when editing
        the main loop since the main loop's own closing-fence branch never
        fires for this input."""
        with mock.patch.object(self.cw, "HAS_MARKDOWN", False), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", True), \
             mock.patch.object(self.cw, "get_lexer_by_name", return_value=object(), create=True), \
             mock.patch.object(self.cw, "highlight", return_value="TAIL", create=True), \
             mock.patch.object(self.cw, "HtmlFormatter", lambda **k: "fmt", create=True):
            out = self.bubble._format_message("```python\nprint('never closed')")
        self.assertIn("<pre><code>TAIL</code></pre>", out)

    # ---------- tier 3: plain escape (neither package installed) ----------
    def test_plain_escape_tier_escapes_html_and_converts_newlines_to_br(self):
        with mock.patch.object(self.cw, "HAS_MARKDOWN", False), \
             mock.patch.object(self.cw, "HAS_PYGMENTS", False):
            out = self.bubble._format_message("a < b & c > d\nsecond line")
        self.assertEqual(out, '<div style="line-height:1.5;">a &lt; b &amp; c &gt; d<br>second line</div>')


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class AttachmentAndReactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.cw = import_with_fakes("ui.chat_widgets")

    def _bubble(self, is_user=False):
        b = object.__new__(self.cw.ChatBubble)
        b.is_user = is_user
        b._theme = THEME
        return b

    # ---------- attachment chip: icon + size formatting ----------
    def test_pdf_attachment_gets_the_pdf_icon_and_kb_size(self):
        bubble = self._bubble()
        chip = bubble._build_attachment_widget({"kind": "document", "name": "report.PDF", "size_bytes": 2048})
        # Walk the chip's child layout directly via the stand-in's tracked items.
        inner_layout = chip._layout
        icon_lbl = inner_layout.items[0]
        text_col = inner_layout.items[1]
        name_lbl, meta_lbl = text_col.items[0], text_col.items[1]
        self.assertEqual(icon_lbl.text(), "📕")
        self.assertEqual(name_lbl.text(), "report.PDF")
        self.assertEqual(meta_lbl.text(), "2 KB")

    def test_non_pdf_attachment_gets_the_generic_doc_icon(self):
        bubble = self._bubble()
        chip = bubble._build_attachment_widget({"kind": "document", "name": "notes.txt", "size_bytes": 500})
        icon_lbl = chip._layout.items[0]
        self.assertEqual(icon_lbl.text(), "📄")

    def test_attachment_size_over_1mb_is_formatted_in_mb_not_kb(self):
        bubble = self._bubble()
        two_mb = 2 * 1024 * 1024
        chip = bubble._build_attachment_widget({"kind": "document", "name": "video.mp4", "size_bytes": two_mb})
        text_col = chip._layout.items[1]
        meta_lbl = text_col.items[1]
        self.assertEqual(meta_lbl.text(), "2.0 MB")

    def test_attachment_size_just_under_1mb_stays_in_kb(self):
        bubble = self._bubble()
        chip = bubble._build_attachment_widget({"kind": "document", "name": "small.txt", "size_bytes": 1024 * 1000})
        text_col = chip._layout.items[1]
        meta_lbl = text_col.items[1]
        self.assertEqual(meta_lbl.text(), "1000 KB")

    def test_image_attachment_that_fails_to_load_shows_the_unavailable_fallback(self):
        class _AlwaysNullPixmap:
            def __init__(self, path):
                pass
            def isNull(self):
                return True
        bubble = self._bubble(is_user=True)
        with mock.patch.object(self.cw, "QPixmap", _AlwaysNullPixmap):
            lbl = bubble._build_attachment_widget({"kind": "image", "path": "/no/such/file.png"})
        self.assertEqual(lbl.text(), "🖼 (image unavailable)")

    def test_image_attachment_that_loads_does_not_show_the_fallback_text(self):
        class _AlwaysValidPixmap:
            def __init__(self, path):
                pass
            def isNull(self):
                return False
            def scaled(self, *a, **k):
                return self
            def size(self):
                return types.SimpleNamespace()
        bubble = self._bubble(is_user=True)
        with mock.patch.object(self.cw, "QPixmap", _AlwaysValidPixmap):
            lbl = bubble._build_attachment_widget({"kind": "image", "path": "/real/photo.png"})
        self.assertEqual(lbl.text(), "")   # never set to the fallback string

    # ---------- reactions ----------
    def test_set_reaction_count_updates_label_text_with_the_count_suffix(self):
        bubble = self._bubble()
        btn = P4.QPushButton("👍")
        bubble.reaction_btns = {"👍": btn}
        bubble._reaction_counts = {"👍": 0}
        bubble.set_reaction_count("👍", 3)
        self.assertEqual(btn.text(), "👍 3")
        self.assertEqual(bubble._reaction_counts["👍"], 3)

    def test_set_reaction_count_back_to_zero_removes_the_count_suffix(self):
        bubble = self._bubble()
        btn = P4.QPushButton("👍")
        bubble.reaction_btns = {"👍": btn}
        bubble._reaction_counts = {"👍": 3}
        bubble.set_reaction_count("👍", 0)
        self.assertEqual(btn.text(), "👍")

    def test_set_reaction_count_for_an_unknown_reaction_is_a_no_op(self):
        bubble = self._bubble()
        bubble.reaction_btns = {}
        bubble._reaction_counts = {}
        bubble.set_reaction_count("🤷", 5)   # must not raise / must not insert

    def test_emit_reaction_calls_the_callback_when_present(self):
        bubble = self._bubble()
        seen = []
        bubble._on_reaction = lambda r: seen.append(r)
        bubble._emit_reaction("❤️")
        self.assertEqual(seen, ["❤️"])

    def test_emit_reaction_without_a_callback_does_not_crash(self):
        bubble = self._bubble()
        bubble._on_reaction = None
        bubble._emit_reaction("❤️")   # must not raise


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class ChatInputBoxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = P4.install_stub()
        cls.cw = import_with_fakes("ui.chat_widgets")

    def setUp(self):
        # Real construction is safe here (unlike ChatBubble): __init__ only
        # ever calls absorbed no-op setters, nothing does numeric coercion
        # of a stand-in object.
        self.box = self.cw.ChatInputBox()

    # ---------- auto-grow clamping ----------
    def _set_doc_height(self, px):
        self.box.document = lambda: types.SimpleNamespace(size=lambda: types.SimpleNamespace(height=lambda: px))

    def test_auto_grow_shrinks_back_to_minimum_for_a_single_short_line(self):
        self._set_doc_height(10)   # 10 + 16 = 26, clamped up to _min_h (44)
        heights = []
        self.box.setFixedHeight = lambda h: heights.append(h)
        self.box.height = lambda: 100   # as if several lines were just deleted back down to one
        self.box._auto_grow()
        self.assertEqual(heights, [44])

    def test_auto_grow_grows_with_content_up_to_the_maximum(self):
        self._set_doc_height(500)  # way past _max_h (132) - must clamp, not grow unbounded
        heights = []
        self.box.setFixedHeight = lambda h: heights.append(h)
        self.box.height = lambda: 44
        self.box._auto_grow()
        self.assertEqual(heights, [132])

    def test_auto_grow_does_not_resize_when_the_height_is_already_correct(self):
        self._set_doc_height(60)  # 60 + 16 = 76, within [44, 132]
        heights = []
        self.box.setFixedHeight = lambda h: heights.append(h)
        self.box.height = lambda: 76   # already the target height
        self.box._auto_grow()
        self.assertEqual(heights, [])   # no redundant resize call

    # ---------- Enter sends, Shift+Enter inserts a newline ----------
    def test_plain_enter_emits_send_requested_and_does_not_insert_a_newline(self):
        Qt = self.cw.Qt
        ev = types.SimpleNamespace(key=lambda: Qt.Key.Key_Return, modifiers=lambda: _Mods(False))
        sent = []
        self.box.send_requested.connect(lambda: sent.append(True))
        QTextEditCls = type(self.box).__mro__[1]   # the stand-in's generic QTextEdit base
        with mock.patch.object(QTextEditCls, "keyPressEvent", create=True) as base_kpe:
            self.box.keyPressEvent(ev)
        self.assertEqual(sent, [True])
        base_kpe.assert_not_called()               # must NOT fall through to insert a newline

    def test_shift_enter_inserts_a_newline_and_does_not_send(self):
        Qt = self.cw.Qt
        ev = types.SimpleNamespace(key=lambda: Qt.Key.Key_Return, modifiers=lambda: _Mods(True))
        sent = []
        self.box.send_requested.connect(lambda: sent.append(True))
        QTextEditCls = type(self.box).__mro__[1]
        with mock.patch.object(QTextEditCls, "keyPressEvent", create=True) as base_kpe:
            self.box.keyPressEvent(ev)
        self.assertEqual(sent, [])                  # did NOT send
        base_kpe.assert_called_once_with(ev)         # mock.patch.object on the class doesn't auto-bind self

    def test_an_unrelated_key_is_passed_through_untouched(self):
        Qt = self.cw.Qt
        ev = types.SimpleNamespace(key=lambda: Qt.Key.Key_A, modifiers=lambda: _Mods(False))
        sent = []
        self.box.send_requested.connect(lambda: sent.append(True))
        QTextEditCls = type(self.box).__mro__[1]
        with mock.patch.object(QTextEditCls, "keyPressEvent", create=True) as base_kpe:
            self.box.keyPressEvent(ev)
        self.assertEqual(sent, [])
        base_kpe.assert_called_once_with(ev)


class _Mods:
    """Fakes Qt.KeyboardModifier flags just enough for `mods & Qt.KeyboardModifier.ShiftModifier`
    to evaluate the way the test wants: the stand-in's generic _Any.__and__ always returns a
    (truthy) _Any(), which can't express "shift was NOT held", so this stands in for the real
    bitmask value returned by event.modifiers()."""
    def __init__(self, shift_held):
        self._shift_held = shift_held

    def __and__(self, other):
        return self._shift_held


if __name__ == "__main__":
    unittest.main()
