"""Tier 8 - AppController.list_open_windows()/focus_window()/
minimize_window()/snap_window() (automation/app_control.py), the
AppController-level layer above the per-platform backend methods behind
them (automation/backends/*.py).

Split the same way test_tier8_services.py splits from
test_phase4_platform.py: FakeShell/win32gui-level command-construction
coverage for the three real backends lives in test_phase4_platform.py
(LinuxBackendTests' window tests, MacWindowManagementTests,
WindowsWindowManagementTests). THIS file covers the layer above those
backends, against a minimal fake backend so each behaviour is isolated
from any real OS mechanism:

ListOpenWindowsTests  - the one tool here that formats its own reply from
                        a plain list (no windows / one window / several,
                        with correct singular/plural wording), rather than
                        just relaying what the backend said - the backend
                        only hands back titles (see base.py's own comment
                        on why, mirroring db.get_upcoming_reminders()/
                        _skill_list_reminders()'s split for reminders).
FocusWindowTests      - delegates, blank name rejected up front,
                        NotSupported -> a plain reply not a crash, audited.
MinimizeWindowTests   - same shape as FocusWindowTests.
SnapWindowTests       - same shape, plus both window_name AND position
                        are passed through and audited.

None of these four need a critical-name guard the way stop_service does -
unlike stopping a service or closing an app, focusing/minimizing/snapping
a window never destroys anything or halts a running system function, so
there's nothing here parallel to _CRITICAL_SERVICE_NAMES/_CRITICAL_PROCESS_NAMES.

Run: python -m unittest discover -s tests -p "test_tier8_*.py"
(pytest will also collect it.)
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

# automation.app_control imports pyautogui unconditionally at module level -
# same bootstrap test_tier8_services.py/test_tier8_fun.py/
# test_handlers_structure.py all need in a sandbox without a real pyautogui.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                        # noqa: E402
from automation import backends                                   # noqa: E402
from automation.audit import Audit, MemoryStore                   # noqa: E402
from automation.app_control import AppController                  # noqa: E402
from automation.backends.base import NotSupported, PlatformBackend  # noqa: E402


class _FakeBackend(PlatformBackend):
    """Minimal stand-in so these tests exercise ONLY AppController's own
    logic (blank-name handling, the list-formatting in
    AppController.list_open_windows, audit wiring) - never a real
    win32gui/wmctrl/osascript call, which test_phase4_platform.py already
    covers for each real backend."""
    name = "fake"
    display_name = "FakeOS"

    def __init__(self, titles=(), raise_not_supported: bool = False):
        self.titles = list(titles)
        self.calls = []
        self.raise_not_supported = raise_not_supported

    def _maybe_raise(self, what: str):
        if self.raise_not_supported:
            raise NotSupported(f"{what} isn't supported on FakeOS yet.")

    def list_open_windows(self) -> list:
        self._maybe_raise("Listing windows")
        return self.titles

    def focus_window(self, window_name: str) -> str:
        self._maybe_raise("Focusing a specific window")
        self.calls.append(("focus", window_name))
        return f"Switched to the window matching '{window_name}'."

    def minimize_window(self, window_name: str) -> str:
        self._maybe_raise("Minimizing a specific window")
        self.calls.append(("minimize", window_name))
        return f"Minimized the window matching '{window_name}'."

    def snap_window(self, window_name: str, position: str) -> str:
        self._maybe_raise("Snapping a window")
        self.calls.append(("snap", window_name, position))
        return f"Snapped the window matching '{window_name}' to {position}."


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


class ListOpenWindowsTests(WindowControlTestBase):
    def test_no_windows_open(self):
        self._use(_FakeBackend(titles=[]))
        self.assertEqual(AppController.list_open_windows(), "No windows are currently open.")

    def test_exactly_one_window_uses_singular_wording(self):
        self._use(_FakeBackend(titles=["Notepad"]))
        result = AppController.list_open_windows()
        self.assertIn("1 window open", result)
        self.assertNotIn("windows open", result)
        self.assertIn("Notepad", result)

    def test_several_windows_are_all_named_and_pluralized(self):
        self._use(_FakeBackend(titles=["Notepad", "Firefox", "Spotify"]))
        result = AppController.list_open_windows()
        self.assertIn("3 windows open", result)
        for title in ("Notepad", "Firefox", "Spotify"):
            self.assertIn(title, result)

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        self.assertIn("isn't supported on FakeOS", AppController.list_open_windows())

    def test_is_audited(self):
        self._use(_FakeBackend(titles=["Notepad"]))
        AppController.list_open_windows()
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status), ("list_open_windows", "ok"))


class FocusWindowTests(WindowControlTestBase):
    def test_delegates_to_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.focus_window("notepad")
        self.assertIn("Switched to", result)
        self.assertEqual(backend.calls, [("focus", "notepad")])

    def test_blank_name_is_rejected_before_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.focus_window("   ")
        self.assertIn("need to know", result.lower())
        self.assertEqual(backend.calls, [])

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        self.assertIn("isn't supported on FakeOS", AppController.focus_window("notepad"))

    def test_is_audited(self):
        self._use(_FakeBackend())
        AppController.focus_window("notepad")
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status, row.args["window_name"]), ("focus_window", "ok", "notepad"))


class MinimizeWindowTests(WindowControlTestBase):
    def test_delegates_to_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.minimize_window("notepad")
        self.assertIn("Minimized", result)
        self.assertEqual(backend.calls, [("minimize", "notepad")])

    def test_blank_name_is_rejected_before_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.minimize_window("")
        self.assertIn("need to know", result.lower())
        self.assertEqual(backend.calls, [])

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        self.assertIn("isn't supported on FakeOS", AppController.minimize_window("notepad"))

    def test_is_audited(self):
        self._use(_FakeBackend())
        AppController.minimize_window("notepad")
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status, row.args["window_name"]), ("minimize_window", "ok", "notepad"))


class SnapWindowTests(WindowControlTestBase):
    def test_delegates_to_the_backend_with_both_arguments(self):
        backend = self._use(_FakeBackend())
        result = AppController.snap_window("notepad", "left")
        self.assertIn("Snapped", result)
        self.assertEqual(backend.calls, [("snap", "notepad", "left")])

    def test_blank_name_is_rejected_before_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.snap_window("  ", "left")
        self.assertIn("need to know", result.lower())
        self.assertEqual(backend.calls, [])

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        self.assertIn("isn't supported on FakeOS", AppController.snap_window("notepad", "left"))

    def test_is_audited_with_both_arguments(self):
        self._use(_FakeBackend())
        AppController.snap_window("notepad", "left")
        row = self.audit.recent(1)[0]
        self.assertEqual(row.tool, "snap_window")
        self.assertEqual(row.status, "ok")
        self.assertEqual((row.args["window_name"], row.args["position"]), ("notepad", "left"))


if __name__ == "__main__":
    unittest.main()
