"""Tier 8 - AppController.start_service()/stop_service() (automation/app_control.py),
the AppController-level layer above the per-platform backend methods behind
them (automation/backends/*.py).

Split the same way test_tier8_fun.py splits from the handler-structure
tests: FakeShell-level command-construction coverage for the three real
backends (sc.exe / systemctl / launchctl) lives alongside the rest of that
mechanism in test_phase4_platform.py (LinuxBackendTests.test_service_start_stop,
MacBackendTests.test_service_start_stop, and the new WindowsServiceTests
class there, since WindowsBackend has no FakeShell of its own). THIS file
covers the layer above those backends, against a minimal fake backend so
each behaviour is isolated from any real OS mechanism:

NormalizeServiceNameTests  - _normalize_service_name()'s own matching rules
                             in isolation (lowercase, strip a trailing
                             ".service", blank-safe).
StartServiceTests          - delegates to the backend, blank-name rejected
                             up front, NotSupported -> a plain reply not a
                             crash, audited.
StopServiceTests           - same shape as StartServiceTests, plus that a
                             refusal is ALSO audited (as "failed", not
                             silently dropped).
CriticalServiceGuardTests  - the _CRITICAL_SERVICE_NAMES hard refusal:
                             every entry refused for stop_service, NONE of
                             them refused for start_service (the same
                             asymmetry _CRITICAL_PROCESS_NAMES already has
                             with close_app/open_app - see that frozenset's
                             own comment in app_control.py), matching is
                             case/".service"-suffix insensitive, and a name
                             that merely CONTAINS a critical one (e.g.
                             "sshd-extra") is correctly left alone.

Run: python -m unittest discover -s tests -p "test_tier8_*.py"
(pytest will also collect it.)
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

# automation.app_control imports pyautogui unconditionally at module level -
# same bootstrap test_tier8_fun.py/test_handlers_structure.py/
# test_phase4_platform.py (via _ac_scenarios.py) all need before anything
# under automation/brain is imported in a sandbox without a real pyautogui.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                                        # noqa: E402
from automation import backends                                                   # noqa: E402
from automation.audit import Audit, MemoryStore                                   # noqa: E402
from automation.app_control import AppController, _CRITICAL_SERVICE_NAMES, _normalize_service_name  # noqa: E402
from automation.backends.base import NotSupported, PlatformBackend                # noqa: E402


class _FakeBackend(PlatformBackend):
    """A minimal stand-in so these tests exercise ONLY AppController's own
    logic (the critical-name guard, blank-name handling, audit wiring) -
    never a real sc.exe/systemctl/launchctl call, which the FakeShell-based
    tests in test_phase4_platform.py already cover for each real backend."""
    name = "fake"
    display_name = "FakeOS"

    def __init__(self, raise_not_supported: bool = False):
        self.calls = []
        self.raise_not_supported = raise_not_supported

    def start_service(self, service_name: str) -> str:
        if self.raise_not_supported:
            raise NotSupported("Starting a system service isn't supported on FakeOS yet.")
        self.calls.append(("start", service_name))
        return f"Successfully started the '{service_name}' service."

    def stop_service(self, service_name: str) -> str:
        if self.raise_not_supported:
            raise NotSupported("Stopping a system service isn't supported on FakeOS yet.")
        self.calls.append(("stop", service_name))
        return f"Successfully stopped the '{service_name}' service."


class ServiceControlTestBase(unittest.TestCase):
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


class NormalizeServiceNameTests(unittest.TestCase):
    def test_lowercases_and_strips_the_systemd_suffix(self):
        self.assertEqual(_normalize_service_name("SSHD"), "sshd")
        self.assertEqual(_normalize_service_name("sshd.service"), "sshd")
        self.assertEqual(_normalize_service_name("  Sshd.Service  "), "sshd")

    def test_a_name_with_no_dot_is_left_alone(self):
        self.assertEqual(_normalize_service_name("myservice"), "myservice")

    def test_blank_or_none_normalizes_to_empty_string(self):
        self.assertEqual(_normalize_service_name(""), "")
        self.assertEqual(_normalize_service_name(None), "")
        self.assertEqual(_normalize_service_name("   "), "")


class StartServiceTests(ServiceControlTestBase):
    def test_delegates_to_the_backend_and_reports_success(self):
        backend = self._use(_FakeBackend())
        result = AppController.start_service("myapp-worker")
        self.assertIn("Successfully started", result)
        self.assertEqual(backend.calls, [("start", "myapp-worker")])

    def test_blank_name_is_rejected_before_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.start_service("   ")
        self.assertIn("need to know", result.lower())
        self.assertEqual(backend.calls, [])

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        result = AppController.start_service("anything")
        self.assertIn("isn't supported on FakeOS", result)

    def test_none_of_the_critical_service_names_are_blocked_for_start(self):
        """The hard refusal only guards stop_service - see
        _CRITICAL_SERVICE_NAMES's own comment in app_control.py for why
        (mirrors _CRITICAL_PROCESS_NAMES only ever gating close_app, never
        open_app)."""
        backend = self._use(_FakeBackend())
        for name in _CRITICAL_SERVICE_NAMES:
            with self.subTest(service=name):
                self.assertIn("Successfully started", AppController.start_service(name))
        self.assertEqual(len(backend.calls), len(_CRITICAL_SERVICE_NAMES))

    def test_is_audited(self):
        self._use(_FakeBackend())
        AppController.start_service("myapp-worker")
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status, row.args["service_name"]),
                         ("start_service", "ok", "myapp-worker"))


class StopServiceTests(ServiceControlTestBase):
    def test_delegates_to_the_backend_and_reports_success(self):
        backend = self._use(_FakeBackend())
        result = AppController.stop_service("myapp-worker")
        self.assertIn("Successfully stopped", result)
        self.assertEqual(backend.calls, [("stop", "myapp-worker")])

    def test_blank_name_is_rejected_before_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        result = AppController.stop_service("")
        self.assertIn("need to know", result.lower())
        self.assertEqual(backend.calls, [])

    def test_not_supported_becomes_a_plain_reply_not_a_crash(self):
        self._use(_FakeBackend(raise_not_supported=True))
        result = AppController.stop_service("anything")
        self.assertIn("isn't supported on FakeOS", result)

    def test_success_and_refusal_are_both_audited(self):
        self._use(_FakeBackend())
        AppController.stop_service("myapp-worker")
        self.assertEqual(self.audit.recent(1)[0].status, "ok")
        AppController.stop_service("sshd")                      # critical -> refused
        refused_row = self.audit.recent(1)[0]
        self.assertEqual(refused_row.status, "failed")
        self.assertEqual(refused_row.args["service_name"], "sshd")


class CriticalServiceGuardTests(ServiceControlTestBase):
    """stop_service() must refuse every platform's critical names WITHOUT
    ever reaching the backend - same hard-refusal shape as close_app's
    _CRITICAL_PROCESS_NAMES check in the same file. Iterates the real,
    live frozenset (not a hand-copied duplicate list here) so this keeps
    covering the actual set if it's ever extended, the same philosophy
    test_phase4_platform.py's own settings-page expansion tests use."""

    def test_every_critical_service_name_is_refused_without_reaching_the_backend(self):
        backend = self._use(_FakeBackend())
        self.assertGreater(len(_CRITICAL_SERVICE_NAMES), 10)   # sanity: the real list, not an empty stub
        for name in _CRITICAL_SERVICE_NAMES:
            with self.subTest(service=name):
                result = AppController.stop_service(name)
                self.assertIn("critical system service", result)
        self.assertEqual(backend.calls, [])

    def test_matching_is_case_insensitive_and_strips_the_service_suffix(self):
        backend = self._use(_FakeBackend())
        for variant in ("SSHD", "sshd.service", "Sshd.Service", "  sshd  "):
            with self.subTest(variant=variant):
                self.assertIn("critical system service", AppController.stop_service(variant))
        self.assertEqual(backend.calls, [])

    def test_a_name_that_merely_contains_a_critical_one_is_not_refused(self):
        """'sshd-extra' isn't 'sshd' - substring matching would be both
        wrong and a much blunter refusal than intended; this must reach
        the (fake) backend like any other ordinary name."""
        backend = self._use(_FakeBackend())
        result = AppController.stop_service("sshd-extra")
        self.assertIn("Successfully stopped", result)
        self.assertEqual(backend.calls, [("stop", "sshd-extra")])

    def test_an_ordinary_non_critical_service_is_not_refused(self):
        backend = self._use(_FakeBackend())
        result = AppController.stop_service("myapp-worker")
        self.assertIn("Successfully stopped", result)
        self.assertEqual(backend.calls, [("stop", "myapp-worker")])


if __name__ == "__main__":
    unittest.main()
