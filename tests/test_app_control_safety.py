"""
tests/test_app_control_safety.py — Tier 6: two narrow, previously-missing
safety checks in automation/app_control.py (the roadmap's "validate_url"/
"validate_app_target" ask, resolved HERE rather than by porting SARA's
validation functions into planning/schema.py - see that module's own
docstring, and planning/executor.py's module docstring, for why: the
planner already reuses WaveBrain's real dispatch path end to end via
dispatch_plan_step(), so a fix placed in this shared backend benefits
every call route - planner, AI tool-calling, intent_router - not just
planning).

1. open_website()'s raw-URL branch now rejects a malformed/structurally
   unsafe "URL" (_is_safe_http_url) before it ever reaches a backend that
   would launch a real browser against it. This is NOT web/web_ops.py's
   validate_url() - that guards Wave's own SERVER-SIDE fetches against
   SSRF to internal/metadata IPs; open_website() hands the URL to the
   PERSON'S OWN browser, where opening a private/local address is a
   completely ordinary, intended thing to ask for, so this only checks
   that the thing parses to exactly http/https with a real host.
2. close_app() now refuses outright - not just the usual T2 confirm-gate -
   when its one eligible candidate (see the existing 🔧 FIX comment there
   for why there's only ever one) is a critical system/shell process
   (_CRITICAL_PROCESS_NAMES).

Both are also exercised, with real processes/backends, by the existing
golden-call scenarios in tests/_ac_scenarios.py / test_phase4_platform.py -
none of those fixtures trip either new check ("https://example.org/x" has
a real host; none of Chrome.exe/code.exe/codecov.exe is a critical name),
confirmed by inspection before writing this file. So this file is purely
additive coverage for the two new checks, not a replacement for anything.
"""
import unittest
from unittest import mock

import _ac_scenarios as sc
from automation.app_control import AppController


class OpenWebsiteUrlSafetyTests(unittest.TestCase):
    def setUp(self):
        self.backend = mock.MagicMock()
        self.backend.find_browser.return_value = None
        self._patch = mock.patch.object(AppController, "_backend", staticmethod(lambda: self.backend))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_a_well_formed_http_url_still_opens_normally(self):
        result = AppController.open_website("https://example.org/x")
        self.backend.open_url_default.assert_called_once_with("https://example.org/x")
        self.assertIn("Opened", result)

    def test_http_scheme_not_just_https_is_also_accepted(self):
        result = AppController.open_website("http://example.org")
        self.backend.open_url_default.assert_called_once_with("http://example.org")
        self.assertIn("Opened", result)

    def test_a_local_or_private_address_is_accepted_not_blocked(self):
        """Different threat model from web_ops.validate_url() - this is
        the person's OWN browser, so their own router/dev server is a
        completely ordinary thing to open, not something to reject."""
        result = AppController.open_website("http://192.168.1.1")
        self.backend.open_url_default.assert_called_once_with("http://192.168.1.1")
        self.assertIn("Opened", result)

    def test_a_url_with_no_host_is_rejected_before_reaching_the_backend(self):
        result = AppController.open_website("https:///just-a-path")
        self.backend.open_url_default.assert_not_called()
        self.backend.launch_in_browser.assert_not_called()
        self.assertIn("valid", result.lower())

    def test_a_scheme_only_string_with_nothing_else_is_rejected(self):
        result = AppController.open_website("https://")
        self.backend.open_url_default.assert_not_called()
        self.assertIn("valid", result.lower())

    def test_rejection_is_audited_as_a_failure(self):
        from automation.audit import Audit, MemoryStore
        import automation.audit as audit_mod
        audit = Audit(store=MemoryStore())
        saved = audit_mod.audit
        audit_mod.audit = audit
        self.addCleanup(lambda: setattr(audit_mod, "audit", saved))
        AppController.open_website("https:///just-a-path")
        row = audit.recent(1)[0]
        self.assertEqual((row.tool, row.status), ("open_website", "failed"))


class CloseAppCriticalProcessTests(unittest.TestCase):
    def _run_close(self, app_name, procs):
        rec = sc.Recorder()
        with sc.instrumented(rec, procs=procs):
            result = AppController.close_app(app_name)
        terminated_pids = [entry[1] for entry in rec.log if entry[0] == "terminate"]
        return result, terminated_pids

    def test_an_exact_match_critical_process_is_refused_not_closed(self):
        procs = [sc.FakeProc(1, "explorer.exe")]
        result, terminated = self._run_close("explorer", procs)
        self.assertEqual(terminated, [])
        self.assertIn("critical", result.lower())

    def test_a_substring_match_critical_process_is_also_refused(self):
        procs = [sc.FakeProc(1, "explorer.exe")]
        result, terminated = self._run_close("explore", procs)
        self.assertEqual(terminated, [])
        self.assertIn("critical", result.lower())

    def test_macos_and_linux_critical_names_are_refused_too(self):
        for name in ("Finder", "systemd", "WindowServer"):
            with self.subTest(name=name):
                procs = [sc.FakeProc(99, name)]
                result, terminated = self._run_close(name.lower(), procs)
                self.assertEqual(terminated, [])
                self.assertIn("critical", result.lower())

    def test_an_ordinary_process_still_closes_normally(self):
        procs = [sc.FakeProc(7, "notepad.exe")]
        result, terminated = self._run_close("notepad", procs)
        self.assertEqual(terminated, [7])
        self.assertIn("Closed", result)

    def test_refusal_is_audited_as_a_failure(self):
        from automation.audit import Audit, MemoryStore
        import automation.audit as audit_mod
        audit = Audit(store=MemoryStore())
        saved = audit_mod.audit
        audit_mod.audit = audit
        self.addCleanup(lambda: setattr(audit_mod, "audit", saved))
        procs = [sc.FakeProc(1, "explorer.exe")]
        with sc.instrumented(sc.Recorder(), procs=procs):
            AppController.close_app("explorer")
        row = audit.recent(1)[0]
        self.assertEqual((row.tool, row.status), ("close_app", "failed"))


if __name__ == "__main__":
    unittest.main()
