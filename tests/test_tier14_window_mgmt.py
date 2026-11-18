"""
tests/test_tier14_window_mgmt.py — Tier 14: the rest of per-window
management (move_window/resize_window/maximize_window/restore_window/
set_always_on_top/set_fullscreen/show_desktop/minimize_all_windows/
switch_window) plus restart_app's close-then-reopen composition.

Same split as test_tier8_windows.py (which covers Tier 8's own list_open_
windows/focus_window/minimize_window/snap_window): AppController-level
logic is tested here against a minimal fake backend - the captured-prior-
state undo for move/resize/always-on-top, and most importantly this
session's two fixes (act.ok() -> act.done() so a not-found window is
audited as "failed" not "ok"; set_always_on_top no longer registering a
spurious undo when the window wasn't found at all). WindowsWindowMgmtTests/
LinuxWindowMgmtTests/MacWindowMgmtTests cover a representative sample of
the real per-OS command construction.

Run: python -m unittest discover -s tests -p "test_tier14_window_mgmt.py"
(pytest will also collect it.)
"""
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                        # noqa: E402
from automation import backends                                   # noqa: E402
from automation.audit import Audit, MemoryStore                   # noqa: E402
from automation.app_control import AppController                  # noqa: E402
from automation.backends.base import NotSupported, PlatformBackend  # noqa: E402
from automation.backends.linux import LinuxBackend                 # noqa: E402
from automation.backends.macos import MacBackend                   # noqa: E402
from automation.backends.windows import WindowsBackend              # noqa: E402


class _FakeShell:
    def __init__(self, installed=(), results=None):
        self.installed, self.results, self.calls = set(installed), results or {}, []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.installed else None

    def run(self, argv, timeout=10):
        self.calls.append(("run", list(argv)))
        return self.results.get(tuple(argv), (0, ""))

    def spawn(self, argv, **kwargs):
        self.calls.append(("spawn", list(argv)))


class _ScriptedShell:
    """Returns pre-programmed (returncode, stdout) tuples in call order,
    ignoring the actual argv - used for macOS's AppleScript methods, whose
    generated script text is too brittle to match exactly (see
    test_phase4_platform.py's own precedent for this)."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def which(self, name):
        return f"/usr/bin/{name}"

    def run(self, argv, timeout=10):
        self.calls.append(list(argv))
        return self._results.pop(0) if self._results else (0, "")

    def spawn(self, argv, **kwargs):
        self.calls.append(list(argv))


class _FakeBackend(PlatformBackend):
    """Minimal stand-in exercising ONLY AppController's own Tier 14 window-
    management logic - real per-OS command construction is covered by the
    Windows/Linux/Mac test classes further down instead. A tiny registry of
    windows, each [x, y, w, h, on_top] - a name NOT in the registry is "not
    found", reported the same plain-string way every real backend here
    does (NOT a raise - see automation/backends/base.py's own
    get_window_rect docstring on this split, and test_tier14_power_
    session.py's capabilities()-identity-check note: NONE of AppController's
    window-management methods consult capabilities() at all, so a plain
    instance flag is faithful here, unlike that file's one special case)."""
    name = "fake"
    display_name = "FakeOS"

    def __init__(self, windows=None, raise_on=()):
        self.windows = {k: list(v) for k, v in (windows or {}).items()}
        self.calls = []
        self._raise_on = set(raise_on)

    def _maybe_raise(self, feature, what):
        if feature in self._raise_on:
            raise NotSupported(f"{what} isn't supported on FakeOS yet.")

    @staticmethod
    def _not_found(window_name):
        return f"I couldn't find an open window matching '{window_name}'."

    def get_window_rect(self, window_name):
        self._maybe_raise("get_window_rect", "Reading a window's position and size")
        w = self.windows.get(window_name)
        return tuple(w[:4]) if w else None

    def move_window(self, window_name, x, y):
        self._maybe_raise("move_window", "Moving a window")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.windows[window_name][0], self.windows[window_name][1] = x, y
        self.calls.append(("move_window", window_name, x, y))
        return f"Moved the window matching '{window_name}'."

    def resize_window(self, window_name, width, height):
        self._maybe_raise("resize_window", "Resizing a window")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.windows[window_name][2], self.windows[window_name][3] = width, height
        self.calls.append(("resize_window", window_name, width, height))
        return f"Resized the window matching '{window_name}'."

    def maximize_window(self, window_name):
        self._maybe_raise("maximize_window", "Maximizing a window")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.calls.append(("maximize_window", window_name))
        return f"Maximized the window matching '{window_name}'."

    def restore_window(self, window_name):
        self._maybe_raise("restore_window", "Restoring a window to its normal size")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.calls.append(("restore_window", window_name))
        return f"Restored the window matching '{window_name}'."

    def is_always_on_top(self, window_name):
        self._maybe_raise("is_always_on_top", "Reading a window's always-on-top state")
        w = self.windows.get(window_name)
        return bool(w[4]) if w else False  # False for BOTH "not found" and "found but not on top"

    def set_always_on_top(self, window_name, enabled):
        self._maybe_raise("set_always_on_top", "Setting a window always-on-top")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.windows[window_name][4] = bool(enabled)
        self.calls.append(("set_always_on_top", window_name, bool(enabled)))
        return (f"The window matching '{window_name}' will now stay on top." if enabled else
                f"The window matching '{window_name}' will no longer stay on top.")

    def set_fullscreen(self, window_name):
        self._maybe_raise("set_fullscreen", "Toggling fullscreen")
        if window_name not in self.windows:
            return self._not_found(window_name)
        self.calls.append(("set_fullscreen", window_name))
        return f"Toggled fullscreen for the window matching '{window_name}'."

    def show_desktop(self):
        self._maybe_raise("show_desktop", "Showing the desktop")
        self.calls.append(("show_desktop",))
        return "Showing the desktop."

    def switch_window(self):
        self._maybe_raise("switch_window", "Switching to the next window")
        self.calls.append(("switch_window",))
        return "Switched to the next window."

    def launch_app(self, app_name):
        self.calls.append(("launch_app", app_name))
        return f"Successfully opened {app_name}."


class WindowControlTestBase(unittest.TestCase):
    def setUp(self):
        self.audit = Audit(store=MemoryStore())
        self.saved_audit = audit_mod.audit
        audit_mod.audit = self.audit
        self.addCleanup(self._restore)

    def _restore(self):
        audit_mod.audit = self.saved_audit
        backends.set_backend(None)

    def _use(self, backend):
        backends.set_backend(backend)
        return backend

    def _last(self):
        return self.audit.recent(1)[0]


class MoveResizeTests(WindowControlTestBase):
    def test_move_window_succeeds_and_captures_prior_position_for_undo(self):
        backend = self._use(_FakeBackend(windows={"notepad": [10, 20, 300, 400, False]}))
        result = AppController.move_window("notepad", 100, 200)
        self.assertEqual(result, "Moved the window matching 'notepad'.")
        self.assertEqual(backend.windows["notepad"][:2], [100, 200])
        row = self._last()
        self.assertEqual((row.tool, row.status, row.args),
                         ("move_window", "ok", {"window_name": "notepad", "x": 100, "y": 200}))
        self.assertEqual(row.undo_kind, "restore_window_position")
        self.assertEqual(row.undo_payload, {"window_name": "notepad", "x": 10, "y": 20})
        self.assertEqual(row.undo_label, "Move 'notepad' back")

    def test_move_window_not_found_is_a_failed_status_not_ok(self):
        """🏆 THE regression test for this session's act.ok() -> act.done()
        fix: the backend returns a plain "I couldn't find..." STRING here
        (never a raise), and act.ok() used to force that into an "ok" audit
        row regardless of what the text actually said."""
        backend = self._use(_FakeBackend())  # nothing registered
        result = AppController.move_window("ghost", 1, 1)
        self.assertIn("couldn't find", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind, "no undo should be registered for a window that was never found")
        self.assertEqual(backend.calls, [])

    def test_resize_window_succeeds_and_captures_prior_size_for_undo(self):
        backend = self._use(_FakeBackend(windows={"notepad": [10, 20, 300, 400, False]}))
        result = AppController.resize_window("notepad", 500, 600)
        self.assertEqual(result, "Resized the window matching 'notepad'.")
        self.assertEqual(backend.windows["notepad"][2:4], [500, 600])
        row = self._last()
        self.assertEqual(row.undo_kind, "restore_window_size")
        self.assertEqual(row.undo_payload, {"window_name": "notepad", "width": 300, "height": 400})
        self.assertEqual(row.undo_label, "Resize 'notepad' back")

    def test_resize_window_not_found_is_failed_with_no_undo(self):
        self._use(_FakeBackend())
        result = AppController.resize_window("ghost", 1, 1)
        self.assertIn("couldn't find", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)

    def test_blank_window_name_is_rejected_before_touching_the_backend(self):
        backend = self._use(_FakeBackend())
        self.assertIn("need to know which window to move", AppController.move_window("", 1, 1))
        self.assertIn("need to know which window to resize", AppController.resize_window("  ", 1, 1))
        self.assertEqual(backend.calls, [])

    def test_not_supported_fails_with_no_undo(self):
        self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, False]}, raise_on=("move_window",)))
        result = AppController.move_window("notepad", 1, 1)
        self.assertIn("isn't supported on FakeOS", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)


class MaximizeRestoreTests(WindowControlTestBase):
    def test_maximize_and_restore_happy_path(self):
        backend = self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, False]}))
        self.assertEqual(AppController.maximize_window("notepad"), "Maximized the window matching 'notepad'.")
        self.assertEqual(AppController.restore_window("notepad"), "Restored the window matching 'notepad'.")
        self.assertEqual([c[0] for c in backend.calls], ["maximize_window", "restore_window"])

    def test_not_found_is_failed_not_ok(self):
        self._use(_FakeBackend())
        for call, label in ((lambda: AppController.maximize_window("ghost"), "maximize_window"),
                             (lambda: AppController.restore_window("ghost"), "restore_window")):
            result = call()
            self.assertIn("couldn't find", result)
            row = self._last()
            self.assertEqual((row.tool, row.status), (label, "failed"))

    def test_blank_name_rejected(self):
        self.assertIn("need to know which window to maximize", AppController.maximize_window(""))
        self.assertIn("need to know which window to restore", AppController.restore_window(""))


class AlwaysOnTopTests(WindowControlTestBase):
    """🏆 The captured-prior-state regression, same shape as volume's mute/
    unmute: undo must restore the REAL prior on-top state, and - this
    session's second fix on this method - must NEVER be registered at all
    when the window wasn't found, since is_always_on_top()'s plain False
    return can't tell "not on top" apart from "doesn't exist"."""

    def test_turning_on_when_not_on_top_really_changes_it_and_undo_says_stop_keeping(self):
        backend = self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, False]}))
        result = AppController.set_always_on_top("notepad", True)
        self.assertIn("will now stay on top", result)
        self.assertTrue(backend.windows["notepad"][4])
        row = self._last()
        self.assertEqual((row.tool, row.status), ("set_always_on_top", "ok"))
        self.assertEqual(row.undo_kind, "restore_always_on_top")
        self.assertEqual(row.undo_payload, {"window_name": "notepad", "was_on_top": False})
        self.assertEqual(row.undo_label, "Stop keeping 'notepad' on top")

    def test_turning_on_an_already_on_top_window_is_a_no_op_but_undo_still_says_keep(self):
        """The key case: calling with enabled=True on an ALREADY-topmost
        window must not produce the blind-opposite "stop keeping" label -
        the prior state really was on-top, so restoring it means keeping it."""
        backend = self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, True]}))
        AppController.set_always_on_top("notepad", True)
        row = self._last()
        self.assertEqual(row.undo_payload, {"window_name": "notepad", "was_on_top": True})
        self.assertEqual(row.undo_label, "Keep 'notepad' on top")

    def test_turning_off_when_on_top_really_changes_it_and_undo_says_keep(self):
        backend = self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, True]}))
        result = AppController.set_always_on_top("notepad", False)
        self.assertIn("will no longer stay on top", result)
        self.assertFalse(backend.windows["notepad"][4])
        row = self._last()
        self.assertEqual(row.undo_payload, {"window_name": "notepad", "was_on_top": True})
        self.assertEqual(row.undo_label, "Keep 'notepad' on top")

    def test_window_not_found_registers_no_undo_and_is_a_failed_status(self):
        """🏆 THE spurious-undo regression test: before this session's fix,
        this path ALWAYS registered an undo entry, even for a window that
        was never found at all, because is_always_on_top()'s False return
        is ambiguous between "found but not on top" and "not found"."""
        backend = self._use(_FakeBackend())  # "ghost" is not registered
        result = AppController.set_always_on_top("ghost", True)
        self.assertIn("couldn't find", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind, "a not-found window must never register an undo entry")
        self.assertEqual(backend.calls, [])  # set_always_on_top itself was never actually called

    def test_blank_name_rejected(self):
        self.assertIn("need to know which window to set", AppController.set_always_on_top("", True))

    def test_not_supported_fails_with_no_undo(self):
        self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, False]}, raise_on=("is_always_on_top",)))
        result = AppController.set_always_on_top("notepad", True)
        self.assertIn("isn't supported on FakeOS", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)


class FullscreenShowDesktopSwitchTests(WindowControlTestBase):
    """set_fullscreen/show_desktop/minimize_all_windows/switch_window are
    deliberately NOT in the undo set (see automation/app_control.py's own
    comment) - none of these tests expect an undo_kind."""

    def test_set_fullscreen_happy_and_not_found(self):
        backend = self._use(_FakeBackend(windows={"notepad": [0, 0, 100, 100, False]}))
        self.assertIn("Toggled fullscreen", AppController.set_fullscreen("notepad"))
        self.assertIsNone(self._last().undo_kind)
        result = AppController.set_fullscreen("ghost")
        self.assertIn("couldn't find", result)
        self.assertEqual(self._last().status, "failed")

    def test_show_desktop(self):
        backend = self._use(_FakeBackend())
        self.assertEqual(AppController.show_desktop(), "Showing the desktop.")
        self.assertEqual(backend.calls, [("show_desktop",)])
        self.assertEqual(self._last().tool, "show_desktop")

    def test_minimize_all_windows_reuses_show_desktop_under_its_own_tool_name(self):
        """Same OS feature as show_desktop (Win+D toggles both) - two tool
        names/phrasings for the one backend call, not two mechanisms."""
        backend = self._use(_FakeBackend())
        AppController.minimize_all_windows()
        self.assertEqual(backend.calls, [("show_desktop",)])
        self.assertEqual(self._last().tool, "minimize_all_windows")

    def test_switch_window(self):
        backend = self._use(_FakeBackend())
        self.assertEqual(AppController.switch_window(), "Switched to the next window.")
        self.assertEqual(self._last().tool, "switch_window")

    def test_not_supported_on_all_three(self):
        self._use(_FakeBackend(raise_on=("set_fullscreen", "show_desktop", "switch_window")))
        self.assertIn("isn't supported on FakeOS", AppController.set_fullscreen("notepad"))
        self.assertIn("isn't supported on FakeOS", AppController.show_desktop())
        self.assertIn("isn't supported on FakeOS", AppController.switch_window())


class RestartAppTests(WindowControlTestBase):
    """restart_app composes the EXISTING close_app/open_app rather than
    duplicating their process-matching/critical-process-guard logic, with
    collapse_if_children=True so their own rows don't ALSO appear
    separately (see automation/app_control.py's own comment)."""

    def test_requires_an_app_name(self):
        self.assertIn("need to know which app to restart", AppController.restart_app(""))

    def test_closes_then_reopens_with_a_pause_between(self):
        with mock.patch.object(AppController, "close_app", return_value="Closed notepad.exe.") as close_mock, \
             mock.patch.object(AppController, "open_app", return_value="Successfully opened notepad.") as open_mock, \
             mock.patch("automation.app_control.time.sleep") as sleep_mock:
            result = AppController.restart_app("notepad")
        close_mock.assert_called_once_with("notepad")
        sleep_mock.assert_called_once_with(1.0)
        open_mock.assert_called_once_with("notepad")
        self.assertEqual(result, "Closed notepad.exe. Successfully opened notepad.")
        row = self._last()
        self.assertEqual((row.tool, row.status), ("restart_app", "ok"))

    def test_stops_and_never_reopens_if_close_fails(self):
        with mock.patch.object(AppController, "close_app",
                                return_value="'ghost' doesn't appear to be running.") as close_mock, \
             mock.patch.object(AppController, "open_app") as open_mock, \
             mock.patch("automation.app_control.time.sleep") as sleep_mock:
            result = AppController.restart_app("ghost")
        open_mock.assert_not_called()
        sleep_mock.assert_not_called()
        self.assertEqual(result, "'ghost' doesn't appear to be running.")
        self.assertEqual(self._last().status, "failed")

    def test_collapsing_drops_restart_apps_own_row_keeping_the_childrens(self):
        """The real close_app/open_app run for real here (only psutil and
        the window backend's launch_app are faked), to see what
        collapse_if_children ACTUALLY does. Per automation/audit.py's own
        _commit(): collapse_if_children is checked on the COMMITTING
        scope itself, using ITS OWN .children counter - so a scope that
        had children (close_app's and open_app's calls each bump their
        PARENT's counter when THEY commit) is the one whose row gets
        dropped. That means restart_app's OWN row disappears here, and
        close_app's + open_app's rows (which commit normally - neither of
        THEM has collapse_if_children set) are what's left: the comment's
        "close_app/open_app each log their own row already" is describing
        exactly this, not an extra third row alongside them."""
        backend = self._use(_FakeBackend())  # supplies launch_app for open_app's real code path
        fake_proc = mock.Mock()
        fake_proc.info = {"pid": 4321, "name": "notepad.exe"}
        with mock.patch("psutil.process_iter", return_value=[fake_proc]), \
             mock.patch("psutil.Process") as fake_process_cls, \
             mock.patch("automation.app_control.time.sleep"):
            result = AppController.restart_app("notepad")
        fake_process_cls.return_value.terminate.assert_called_once()
        self.assertIn("notepad", result.lower())
        self.assertEqual(backend.calls, [("launch_app", "notepad")])
        rows = self.audit.recent(10)
        tools = sorted(r.tool for r in rows)
        self.assertEqual(tools, ["close_app", "open_app"], "restart_app's own row should collapse away")

    def test_a_refused_critical_process_close_stops_restart_too(self):
        """🏆 THE regression test for this session's fix: close_app's
        critical-process refusal ("'{name}' is a critical system process -
        I won't close that.") ALSO leads with the quoted name, so it was
        ALSO silently missed by the infer_status()-only check - restart_app
        would refuse to close explorer.exe (correctly) but then still try
        to reopen it anyway (the bug), rather than stopping there."""
        fake_proc = mock.Mock()
        fake_proc.info = {"pid": 999, "name": "explorer.exe"}
        with mock.patch("psutil.process_iter", return_value=[fake_proc]), \
             mock.patch("psutil.Process") as fake_process_cls, \
             mock.patch.object(AppController, "open_app") as open_mock, \
             mock.patch("automation.app_control.time.sleep") as sleep_mock:
            result = AppController.restart_app("explorer")
        fake_process_cls.return_value.terminate.assert_not_called()
        open_mock.assert_not_called()
        sleep_mock.assert_not_called()
        self.assertIn("critical system process", result)
        rows = self.audit.recent(10)
        # close_app refused on its own (no further children of ITS OWN), so
        # ITS row commits normally; restart_app had exactly one child
        # (close_app) and is collapse_if_children, so its own row still
        # drops, same as the happy-path test above.
        self.assertEqual([r.tool for r in rows], ["close_app"])
        self.assertEqual(rows[0].status, "failed")


# =============================================================================
# Real backend command construction - a representative sample.
# =============================================================================
class _FakeWin32:
    """Stands in for BOTH win32gui and win32con - same role as the Tier
    8-era _FakeWin32 in test_phase4_platform.py (duplicated rather than
    imported across test files, matching how test_tier8_windows.py's own
    _FakeBackend is separate from that file's backend-level fakes too),
    extended with the handful of extra attributes/methods Tier 14's
    get_window_rect/move_window/resize_window/maximize_window/
    restore_window/is_always_on_top/set_always_on_top need: each window
    now also carries a (left, top, right, bottom) rect and a topmost flag."""
    SW_RESTORE = 9
    SW_MINIMIZE = 6
    SW_MAXIMIZE = 3
    GWL_EXSTYLE = -20
    WS_EX_TOPMOST = 0x00000008
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001

    def __init__(self, windows=()):
        self._windows = {}
        for w in windows:
            hwnd, title, visible, iconic = w[0], w[1], w[2], w[3]
            rect = list(w[4]) if len(w) > 4 else [0, 0, 100, 100]
            topmost = w[5] if len(w) > 5 else False
            self._windows[hwnd] = {"title": title, "visible": visible, "iconic": iconic,
                                    "rect": rect, "topmost": topmost}
        self.calls = []

    def EnumWindows(self, callback, extra):
        for hwnd in list(self._windows):
            callback(hwnd, extra)

    def IsWindowVisible(self, hwnd):
        return self._windows[hwnd]["visible"]

    def GetWindowText(self, hwnd):
        return self._windows[hwnd]["title"]

    def IsIconic(self, hwnd):
        return self._windows[hwnd]["iconic"]

    def ShowWindow(self, hwnd, flag):
        self.calls.append(("ShowWindow", hwnd, flag))
        if flag == self.SW_RESTORE:
            self._windows[hwnd]["iconic"] = False
        elif flag == self.SW_MINIMIZE:
            self._windows[hwnd]["iconic"] = True
        elif flag == self.SW_MAXIMIZE:
            self._windows[hwnd]["iconic"] = False

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))

    def GetWindowRect(self, hwnd):
        return tuple(self._windows[hwnd]["rect"])

    def MoveWindow(self, hwnd, x, y, w, h, repaint):
        self.calls.append(("MoveWindow", hwnd, x, y, w, h))
        self._windows[hwnd]["rect"] = [x, y, x + w, y + h]

    def GetWindowLong(self, hwnd, index):
        return self.WS_EX_TOPMOST if self._windows[hwnd]["topmost"] else 0

    def SetWindowPos(self, hwnd, insert_after, x, y, w, h, flags):
        self.calls.append(("SetWindowPos", hwnd, insert_after))
        self._windows[hwnd]["topmost"] = (insert_after == self.HWND_TOPMOST)


class WindowsWindowMgmtTests(unittest.TestCase):
    def _patched(self, fake):
        from automation.backends import windows as windows_mod
        # create=True: win32gui/win32con were never bound as module
        # attributes at all (the real `import win32gui` failed, so the
        # names don't exist here to patch over) - this adds them for the
        # duration of the test and removes them again afterward.
        return mock.patch.multiple(windows_mod, win32gui=fake, win32con=_FakeWin32, HAS_WIN32GUI=True, create=True)

    def test_get_window_rect_returns_xywh_not_the_raw_ltrb(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False, (10, 20, 110, 220))])
        with self._patched(fake):
            self.assertEqual(WindowsBackend().get_window_rect("notepad"), (10, 20, 100, 200))

    def test_get_window_rect_not_found_returns_none_not_a_string(self):
        """get_window_rect's OWN not-found signal is None, unlike every
        sibling method here (which return a plain string) - this is what
        move_window/resize_window in AppController read to decide whether
        to register an undo at all."""
        fake = _FakeWin32(windows=[(5, "Notepad", True, False)])
        with self._patched(fake):
            self.assertIsNone(WindowsBackend().get_window_rect("ghost"))

    def test_move_window_keeps_the_existing_size(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False, (10, 20, 110, 220))])
        with self._patched(fake):
            msg = WindowsBackend().move_window("notepad", 300, 400)
        self.assertEqual(fake.calls[-1], ("MoveWindow", 5, 300, 400, 100, 200))
        self.assertIn("Moved", msg)

    def test_move_window_not_found_is_a_plain_reply(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False)])
        with self._patched(fake):
            msg = WindowsBackend().move_window("ghost", 1, 1)
        self.assertIn("couldn't find", msg)

    def test_resize_window_keeps_the_existing_position(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False, (10, 20, 110, 220))])
        with self._patched(fake):
            WindowsBackend().resize_window("notepad", 500, 600)
        self.assertEqual(fake.calls[-1], ("MoveWindow", 5, 10, 20, 500, 600))

    def test_maximize_and_restore(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False)])
        with self._patched(fake):
            b = WindowsBackend()
            self.assertIn("Maximized", b.maximize_window("notepad"))
            self.assertEqual(fake.calls[-1], ("ShowWindow", 5, _FakeWin32.SW_MAXIMIZE))
            self.assertIn("Restored", b.restore_window("notepad"))
            self.assertEqual(fake.calls[-1], ("ShowWindow", 5, _FakeWin32.SW_RESTORE))

    def test_always_on_top_round_trips_through_the_real_window_style_bit(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False, (0, 0, 100, 100), False)])
        with self._patched(fake):
            b = WindowsBackend()
            self.assertFalse(b.is_always_on_top("notepad"))
            msg = b.set_always_on_top("notepad", True)
            self.assertIn("now stay on top", msg)
            self.assertEqual(fake.calls[-1], ("SetWindowPos", 5, _FakeWin32.HWND_TOPMOST))
            self.assertTrue(b.is_always_on_top("notepad"))
            msg = b.set_always_on_top("notepad", False)
            self.assertIn("no longer stay on top", msg)
            self.assertEqual(fake.calls[-1], ("SetWindowPos", 5, _FakeWin32.HWND_NOTOPMOST))

    def test_is_always_on_top_not_found_returns_false_not_a_raise(self):
        """The exact ambiguity AppController.set_always_on_top's own fix
        has to work around: False here means EITHER "found but not on top"
        OR "not found at all" - there's no third value."""
        fake = _FakeWin32(windows=[(5, "Notepad", True, False)])
        with self._patched(fake):
            self.assertFalse(WindowsBackend().is_always_on_top("ghost"))

    def test_set_fullscreen_sends_f11(self):
        fake = _FakeWin32(windows=[(5, "Notepad", True, False)])
        with self._patched(fake), mock.patch("time.sleep"), mock.patch("pyautogui.press") as press:
            msg = WindowsBackend().set_fullscreen("notepad")
        press.assert_called_once_with("f11")
        self.assertIn("Toggled fullscreen", msg)

    def test_show_desktop_and_switch_window_send_the_expected_hotkeys(self):
        with mock.patch("pyautogui.hotkey") as hotkey:
            self.assertIn("desktop", WindowsBackend().show_desktop().lower())
            hotkey.assert_called_with("win", "d")
            self.assertIn("next window", WindowsBackend().switch_window().lower())
            hotkey.assert_called_with("alt", "tab")


class LinuxWindowMgmtTests(unittest.TestCase):
    def test_get_window_rect_parses_xdotool_shell_output(self):
        sh = _FakeShell(installed={"xdotool"}, results={
            ("xdotool", "search", "--name", "notepad"): (0, "123"),
            ("xdotool", "getwindowgeometry", "--shell", "123"):
                (0, "WINDOW=123\nX=10\nY=20\nWIDTH=300\nHEIGHT=400\nSCREEN=0\n")})
        self.assertEqual(LinuxBackend(sh).get_window_rect("notepad"), (10, 20, 300, 400))

    def test_get_window_rect_no_match_returns_none(self):
        sh = _FakeShell(installed={"xdotool"}, results={("xdotool", "search", "--name", "ghost"): (0, "")})
        self.assertIsNone(LinuxBackend(sh).get_window_rect("ghost"))

    def test_get_window_rect_without_xdotool_raises(self):
        with self.assertRaises(NotSupported):
            LinuxBackend(_FakeShell()).get_window_rect("notepad")

    def test_move_and_resize_via_xdotool(self):
        sh = _FakeShell(installed={"xdotool"}, results={("xdotool", "search", "--name", "notepad"): (0, "123")})
        b = LinuxBackend(sh)
        b.move_window("notepad", 100, 200)
        self.assertEqual(sh.calls[-1], ("run", ["xdotool", "windowmove", "123", "100", "200"]))
        b.resize_window("notepad", 500, 600)
        self.assertEqual(sh.calls[-1], ("run", ["xdotool", "windowsize", "123", "500", "600"]))

    def test_maximize_and_restore_via_wmctrl_rc(self):
        sh = _FakeShell(installed={"wmctrl"}, results={
            ("wmctrl", "-r", "notepad", "-b", "add,maximized_vert,maximized_horz"): (0, ""),
            ("wmctrl", "-r", "ghost", "-b", "remove,maximized_vert,maximized_horz"): (1, "")})
        b = LinuxBackend(sh)
        self.assertIn("Maximized", b.maximize_window("notepad"))
        self.assertIn("couldn't find", b.restore_window("ghost"))

    def test_is_always_on_top_without_xdotool_raises_but_without_a_match_just_returns_false(self):
        """Two distinct failure modes: xdotool itself missing is a hard
        NotSupported (nothing can be checked at all); xdotool present but
        no window matches (or xprop is missing) is a plain False, the same
        ambiguous-with-"not on top" shape every backend's is_always_on_top
        uses."""
        with self.assertRaises(NotSupported):
            LinuxBackend(_FakeShell()).is_always_on_top("notepad")
        sh = _FakeShell(installed={"xdotool"}, results={("xdotool", "search", "--name", "ghost"): (0, "")})
        self.assertFalse(LinuxBackend(sh).is_always_on_top("ghost"))

    def test_set_always_on_top_via_wmctrl_above(self):
        sh = _FakeShell(installed={"wmctrl"}, results={
            ("wmctrl", "-r", "notepad", "-b", "add,above"): (0, "")})
        self.assertIn("now stay on top", LinuxBackend(sh).set_always_on_top("notepad", True))

    def test_show_desktop_and_switch_window(self):
        sh = _FakeShell(installed={"wmctrl", "xdotool"})
        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
            b = LinuxBackend(sh)
            self.assertEqual(b.show_desktop(), "Showing the desktop.")
            self.assertEqual(sh.calls[-1], ("run", ["wmctrl", "-k", "on"]))
            b.switch_window()
            self.assertEqual(sh.calls[-1], ("run", ["xdotool", "key", "alt+Tab"]))

    def test_switch_window_without_a_display_is_not_supported(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os as _os
            _os.environ.pop("DISPLAY", None)
            with self.assertRaises(NotSupported):
                LinuxBackend(_FakeShell(installed={"xdotool"})).switch_window()


class MacWindowMgmtTests(unittest.TestCase):
    def test_get_window_rect_parses_the_csv_reply(self):
        sh = _ScriptedShell([(0, "10,20,300,400")])
        self.assertEqual(MacBackend(sh).get_window_rect("notepad"), (10, 20, 300, 400))

    def test_get_window_rect_not_found_returns_none(self):
        sh = _ScriptedShell([(0, "not found")])
        self.assertIsNone(MacBackend(sh).get_window_rect("ghost"))

    def test_move_and_resize_report_not_found_as_a_plain_reply(self):
        sh = _ScriptedShell([(0, "not found")])
        self.assertIn("couldn't find", MacBackend(sh).move_window("ghost", 1, 1))

    def test_maximize_window_reuses_snap_windows_own_maximize_branch(self):
        """macOS has no separate OS-tracked maximize flag - maximize_window
        literally calls snap_window(name, "maximize") (see this backend's
        own comment), so the wording it returns is snap_window's own."""
        sh = _ScriptedShell([(0, "ok")])
        msg = MacBackend(sh).maximize_window("notepad")
        self.assertEqual(msg, "Snapped the window matching 'notepad' to maximize.")

    def test_always_on_top_is_honestly_not_supported(self):
        """Listed in MacBackend.ALWAYS_UNSUPPORTED (see
        test_tier14_power_session.py's hibernate_now test for the same
        pattern on this backend) - no built-in window flag to target at all."""
        self.assertIn("is_always_on_top", MacBackend.ALWAYS_UNSUPPORTED)
        self.assertIn("set_always_on_top", MacBackend.ALWAYS_UNSUPPORTED)
        with self.assertRaises(NotSupported):
            MacBackend(_ScriptedShell([])).is_always_on_top("notepad")
        with self.assertRaises(NotSupported):
            MacBackend(_ScriptedShell([])).set_always_on_top("notepad", True)

    def test_set_fullscreen_sends_the_mac_specific_hotkey(self):
        sh = _ScriptedShell([(0, "ok")])
        with mock.patch("pyautogui.hotkey") as hotkey:
            msg = MacBackend(sh).set_fullscreen("notepad")
        hotkey.assert_called_once_with("ctrl", "command", "f")
        self.assertIn("Toggled fullscreen", msg)

    def test_show_desktop_sends_f11_the_opposite_of_windows_show_desktop_key(self):
        with mock.patch("pyautogui.press") as press:
            msg = MacBackend(_ScriptedShell([])).show_desktop()
        press.assert_called_once_with("f11")
        self.assertIn("Showing the desktop", msg)


if __name__ == "__main__":
    unittest.main()
