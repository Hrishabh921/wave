"""Phase 4 - UI logic smoke tests (ideas 1, 3 and 5), WITHOUT a display or PyQt6.

PyQt6 isn't importable in the environment these tests were written in, so a tiny stand-in for
PyQt6 is installed (only if the real one is absent) that records what the code does: which labels
and buttons the Activity page builds, which slots get connected, what the toasts say. This
verifies the Python logic of the new UI code (names, wiring, undo flow, timer gating, resize
debounce). It does NOT verify how anything looks or paints - that still needs a real run of the
app on your machine.

If real PyQt6 is installed these tests are skipped (they would try to drive real widgets).
"""
import os
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


# ------------------------------------------------------------------ the stand-in
class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _Any()
        type.__setattr__(cls, name, value)          # cached: Qt.X.Y is the SAME object every time
        return value


class _Any(metaclass=_Meta):
    """Absorbs calls / attribute access / operators."""

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = _Any()
        object.__setattr__(self, name, value)
        return value

    def __iter__(self):
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    __or__ = __ror__ = __and__ = __rand__ = __add__ = __radd__ = lambda self, o: _Any()


class _BoundSignal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for fn in list(self.slots):
            fn(*a)


class _Signal:
    """pyqtSignal(...) stand-in: one _BoundSignal per instance."""

    def __set_name__(self, owner, name):
        self.name = "_sig_" + name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        if self.name not in obj.__dict__:
            obj.__dict__[self.name] = _BoundSignal()
        return obj.__dict__[self.name]


class QWidget(_Any):
    def __init__(self, *a, **k):
        self._deleted = False
        self._props = {}
        self._enabled = True

    def deleteLater(self):
        self._deleted = True

    def setProperty(self, k, v):
        self._props[k] = v

    def setEnabled(self, flag):
        self._enabled = flag

    def isEnabled(self):
        return self._enabled

    def setToolTip(self, t):
        self._tip = t

    def toolTip(self):
        return getattr(self, "_tip", "")


class QLabel(QWidget):
    def __init__(self, text="", *a):
        super().__init__()
        self._text = text

    def setText(self, t):
        self._text = t

    def text(self):
        return self._text


class QPushButton(QLabel):
    clicked = _Signal()


class _Layout(_Any):
    def __init__(self, parent=None, *a, **k):
        self.items = []
        if isinstance(parent, QWidget):
            parent._layout = self            # QHBoxLayout(widget) installs itself on the widget

    def addWidget(self, w, *a):
        self.items.append(w)

    def addLayout(self, l, *a):
        self.items.append(l)

    def insertWidget(self, i, w, *a):
        self.items.insert(i, w)

    def count(self):
        return len(self.items)

    def takeAt(self, i):
        item = self.items.pop(i)
        return types.SimpleNamespace(widget=lambda: item if isinstance(item, QWidget) else None)


class _QtModule(types.ModuleType):
    OVERRIDES = {}

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name in ("pyqtSignal",):
            return lambda *a, **k: _Signal()
        if name == "pyqtSlot":
            return lambda *a, **k: (lambda f: f)
        cls = self.OVERRIDES.get(name) or _Meta(name, (_Any,), {})
        setattr(self, name, cls)
        return cls


def install_stub():
    widgets = _QtModule("PyQt6.QtWidgets")
    widgets.OVERRIDES = {"QWidget": QWidget, "QFrame": type("QFrame", (QWidget,), {}), "QLabel": QLabel,
                         "QPushButton": QPushButton, "QVBoxLayout": type("QVBoxLayout", (_Layout,), {}),
                         "QHBoxLayout": type("QHBoxLayout", (_Layout,), {}),
                         "QMainWindow": type("QMainWindow", (QWidget,), {}),
                         "QObject": type("QObject", (_Any,), {})}
    core, gui = _QtModule("PyQt6.QtCore"), _QtModule("PyQt6.QtGui")
    core.OVERRIDES = {"QObject": widgets.OVERRIDES["QObject"],
                      "QThread": type("QThread", (_Any,), {}), "QTimer": type("QTimer", (_Any,), {})}
    pkg = types.ModuleType("PyQt6")
    pkg.QtWidgets, pkg.QtCore, pkg.QtGui = widgets, core, gui
    sys.modules.update({"PyQt6": pkg, "PyQt6.QtWidgets": widgets, "PyQt6.QtCore": core, "PyQt6.QtGui": gui})
    return widgets


# ------------------------------------------------------------------ helpers
class FakeToast:
    def __init__(self):
        self.shown = []

    def show(self, message, icon="info", duration=3000):
        self.shown.append((message, icon))


class SyncPool:
    """Runs the job immediately and replays its result through Worker-like signals."""

    def submit(self, fn, *args, **kwargs):
        sig = types.SimpleNamespace(result=_BoundSignal(), error=_BoundSignal())
        worker = types.SimpleNamespace(signals=sig)

        class Later:
            def __init__(self):
                self.done = False

        def connect_result(slot):
            sig.result.slots.append(slot)
            if not getattr(worker, "_ran", False):
                worker._ran = True
                try:
                    out = fn(*args, **kwargs)
                except Exception as e:
                    for s in sig.error.slots:
                        s(str(e))
                else:
                    for s in sig.result.slots:
                        s(out)
        worker.signals = types.SimpleNamespace(result=types.SimpleNamespace(connect=connect_result),
                                               error=sig.error)
        return worker


THEME = types.SimpleNamespace(text_faint="#888", text_main="#eee", bg_surface="#222", border="#333",
                              success="#0f0", danger="#f00", warning="#fa0", accent_1="#00f")


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class UiLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                              hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                              typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None, size=lambda: (1, 1))
        S.install_fake_module("pywhatkit")
        cls.widgets = install_stub()
        sys.modules.pop("ui.main_window", None)
        for _ in range(20):                    # voice/audio/etc. packages that aren't installed here
            try:
                import ui.main_window as mw
                break
            except ModuleNotFoundError as e:
                missing = (e.name or "").split(".")[0]
                if not missing or missing in sys.modules:
                    raise
                sys.modules[missing] = _QtModule(missing)
        cls.mw = mw

    def setUp(self):
        from automation.audit import Audit, MemoryStore
        from automation.undo import TrashStore, UndoManager
        import automation.audit as audit_mod
        import automation.undo as undo_mod
        self.tmp = S.make_tmpdir(self)
        self.audit = Audit(store=MemoryStore())
        self.trash = TrashStore(os.path.join(self.tmp, "trash"))
        self.um = UndoManager(self.audit, self.trash)
        saved = (audit_mod.audit, undo_mod.trash, undo_mod.undo_manager)
        audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = self.audit, self.trash, self.um
        self.addCleanup(lambda: (setattr(audit_mod, "audit", saved[0]), setattr(undo_mod, "trash", saved[1]),
                                 setattr(undo_mod, "undo_manager", saved[2])))
        win = object.__new__(self.mw.MainWindow)
        win.theme, win.toast, win.worker_pool = THEME, FakeToast(), SyncPool()
        win._make_back_row = lambda *a, **k: QWidget()
        self.win = win
        self.mw.make_section_heading = lambda *a, **k: QWidget()
        self.mw.add_shadow = lambda *a, **k: None

    # ---------- import level ----------
    def test_module_imports_and_navigation_is_consistent(self):
        names = [n for n, _, _ in self.mw.NAV_ITEMS]
        self.assertEqual(names[self.mw.ACTIVITY_PAGE_INDEX], "Activity")
        self.assertEqual([i for _, _, i in self.mw.NAV_ITEMS], list(range(len(names))))
        self.assertEqual(names[-1], "Settings")

    # ---------- Activity page: the undo flow ----------
    def test_activity_page_builds_rows_and_undo_button_really_undoes(self):
        from automation.file_manager import FileManager
        page = self.win._build_activity_page()
        self.assertIsInstance(page, QWidget)
        lay = self.win.activity_list_layout
        self.assertEqual(lay.count(), 1)                                       # the "Nothing yet" hint
        self.assertFalse(self.win.btn_undo_last.isEnabled())

        FileManager.create_file("plan.txt", "x", self.tmp)
        self.win._refresh_activity_page()
        self.assertTrue(self.win.btn_undo_last.isEnabled())
        row = lay.items[0]._layout                                             # the row's QHBoxLayout
        undo_btn = next(w for w in row.items if isinstance(w, QPushButton) and w.text() == "Undo")
        self.assertEqual(undo_btn.toolTip(), "Remove the file 'plan.txt'")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "plan.txt")))

        undo_btn.clicked.emit()                                                # <- the user clicks Undo
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "plan.txt")))
        message, icon = self.win.toast.shown[-1]
        self.assertEqual(icon, "success")
        self.assertIn("Removed 'plan.txt'", message)
        self.assertFalse(self.win.btn_undo_last.isEnabled())                   # nothing left to undo
        self.assertEqual(self.audit.recent()[1].undone_at is not None, True)

    def test_undo_last_button_with_nothing_to_undo_just_says_so(self):
        self.win._build_activity_page()
        self.win._undo_last_clicked()
        self.assertEqual(self.win.toast.shown[-1], ("There's nothing to undo right now.", "info"))

    def test_audit_events_only_rebuild_the_page_when_it_is_visible(self):
        self.win._build_activity_page()
        page = QWidget()
        self.win._activity_page = page
        calls = []
        self.win._refresh_activity_page = lambda: calls.append(1)
        page.isVisible = lambda: False
        self.win._on_audit_event()
        self.assertEqual((calls, self.win._activity_dirty), ([], True))
        page.isVisible = lambda: True
        self.win._on_audit_event()
        self.assertEqual(calls, [1])

    def test_empty_trash_asks_first(self):
        self.win._build_activity_page()
        with mock.patch.object(self.mw.QMessageBox, "question", return_value=self.mw.QMessageBox.StandardButton.No):
            self.win._empty_trash_clicked()
        self.assertEqual(self.win.toast.shown, [])

    # ---------- Files page delete ----------
    def test_files_page_delete_goes_through_the_trash(self):
        p = os.path.join(self.tmp, "keep_me.txt")
        with open(p, "w") as fh:
            fh.write("precious")
        with mock.patch.object(self.mw.QMessageBox, "question", return_value=self.mw.QMessageBox.StandardButton.Yes):
            self.win._delete_file(p)
        self.assertFalse(os.path.exists(p))
        self.assertIn("undo", self.win.toast.shown[-1][0])
        self.assertTrue(self.um.undo_last().ok)
        self.assertTrue(os.path.exists(p))

    # ---------- skills ----------
    def test_skill_events_become_toasts(self):
        self.win._on_skill_event("loaded", "Skill loaded: x")
        self.win._on_skill_event("error", "Skill file y.py not loaded - SyntaxError")
        self.assertEqual([i for _, i in self.win.toast.shown], ["success", "warning"])

    # ---------- smoothness gating ----------
    def test_timers_do_nothing_while_minimised_and_net_speed_uses_real_elapsed_time(self):
        win = self.win
        win.isMinimized, win.isVisible = (lambda: True), (lambda: True)
        with mock.patch.object(self.mw.SystemMonitor, "get_stats", side_effect=AssertionError("must not run")):
            win.update_sys_stats()                      # gated: never reaches psutil
        win._update_clock()
        # active again, after a 100-second gap: 50 MB since the last reading must NOT show as 50 MB/s
        win.isMinimized = lambda: False
        win.dl_lbl, win.ul_lbl = QLabel(), QLabel()
        io = types.SimpleNamespace(bytes_recv=60 * 1024 * 1024, bytes_sent=10 * 1024 * 1024)
        win.last_net_io = types.SimpleNamespace(bytes_recv=10 * 1024 * 1024, bytes_sent=10 * 1024 * 1024)
        win._last_net_ts = self.mw.time.monotonic() - 100
        with mock.patch.object(self.mw.SystemMonitor, "get_stats", return_value={}), \
                mock.patch.object(self.mw.psutil, "net_io_counters", return_value=io), \
                mock.patch.object(self.mw.psutil, "disk_usage", side_effect=OSError):
            win.update_sys_stats()
        self.assertIn("0.50 MB/s", win.dl_lbl.text())

    def test_visualizer_and_disc_skip_ticks_when_hidden(self):
        vis = object.__new__(self.mw.AudioVisualizer)
        vis.isVisible, vis.window = (lambda: True), (lambda: types.SimpleNamespace(isMinimized=lambda: True))
        vis._bar_count = lambda: (_ for _ in ()).throw(AssertionError("animated while minimised"))
        vis._tick()
        disc = object.__new__(self.mw.AlbumArtDisc)
        disc.spinning, disc.angle = True, 0
        disc.isVisible, disc.window = (lambda: False), (lambda: types.SimpleNamespace(isMinimized=lambda: False))
        disc._tick()
        self.assertEqual(disc.angle, 0)

    def test_backdrop_does_not_rerender_during_a_drag_resize(self):
        bd = object.__new__(self.mw.AuroraBackdrop)
        bd._blur_radius, bd._resizing = 25.0, False
        bd._cache, bd._cache_key = types.SimpleNamespace(width=lambda: 800, height=lambda: 600), (800, 600, 25.0)
        started = []
        bd._settle = types.SimpleNamespace(start=lambda ms: started.append(ms))
        bd.update = lambda: started.append("update")
        with mock.patch.object(self.mw.QWidget, "resizeEvent", create=True):
            bd.resizeEvent(None)
        self.assertEqual((bd._resizing, started), (True, [120]))
        bd.width, bd.height = (lambda: 900), (lambda: 650)
        bd._render_backdrop = lambda w, h: (_ for _ in ()).throw(AssertionError("re-rendered mid-resize"))
        bd.rect = lambda: None
        with mock.patch.object(self.mw, "QPainter"):
            bd.paintEvent(None)                         # stretches the old render instead
        bd._finish_resize()
        self.assertEqual((bd._resizing, started[-1]), (False, "update"))

    # ---------- settings ----------
    def test_provider_status_text_and_connection_check_result(self):
        panel = object.__new__(self.mw.SettingsPanel)
        panel._main = types.SimpleNamespace(brain=types.SimpleNamespace(provider_info=lambda: {
            "provider": "openrouter", "model": "m", "endpoint": "openrouter.ai", "key": "…1234",
            "configured": False, "problem": "OPENROUTER_API_KEY is missing."}))
        self.assertIn("Not usable yet: OPENROUTER_API_KEY is missing.", panel._provider_text())
        self.assertEqual(panel._provider_brief(), "openrouter (m)")
        panel.btn_test_provider, panel.provider_status = QPushButton("Test"), QLabel()
        panel.btn_test_provider.setEnabled(False)
        panel._on_provider_checked((True, "Connected to openrouter.ai.", 42))
        self.assertEqual(panel.provider_status.text(), "OK: Connected to openrouter.ai. (42 ms)")
        self.assertTrue(panel.btn_test_provider.isEnabled())


if __name__ == "__main__":
    unittest.main()
