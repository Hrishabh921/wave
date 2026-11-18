"""
tests/test_tier14_power_session.py — Tier 14: power/session (shutdown_pc/
restart_pc/sleep_pc/hibernate_pc/log_off/cancel_shutdown).

Split the same way test_tier8_windows.py splits from test_phase4_platform.py:
PowerSessionControllerTests below exercises ONLY AppController's own logic -
delay resolution, the capabilities()-check-first fix (schedule nothing when
a feature is honestly unsupported - see automation/app_control.py's own
comment on this), audit wiring, and the Wave-owned cancellable timer for
sleep/hibernate/log-off - against a minimal fake backend, isolated from any
real OS mechanism. WindowsPowerSessionTests/LinuxPowerSessionTests/
MacPowerSessionTests cover a representative sample of the real per-OS
command construction, the same FakeShell-based style
test_phase4_platform.py already uses for the rest of each backend.

Run: python -m unittest discover -s tests -p "test_tier14_power_session.py"
(pytest will also collect it.)
"""
import time
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

# automation.app_control imports pyautogui unconditionally at module level -
# same bootstrap test_tier8_windows.py/test_tier8_services.py already need
# in a sandbox without a real pyautogui.
S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import audit as audit_mod                                            # noqa: E402
from automation import app_control as ac_mod                                         # noqa: E402
from automation import backends                                                      # noqa: E402
from automation.audit import Audit, MemoryStore                                      # noqa: E402
from automation.app_control import AppController                                     # noqa: E402
from automation.backends.base import NATIVE_SHUTDOWN_CANCELLED_MSG, NotSupported, PlatformBackend  # noqa: E402
from automation.backends.linux import LinuxBackend                                   # noqa: E402
from automation.backends.macos import MacBackend                                     # noqa: E402
from automation.backends.windows import WindowsBackend                               # noqa: E402
from settings.config import config                                                   # noqa: E402


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


class _FakePowerBackend(PlatformBackend):
    """Minimal stand-in so these tests exercise ONLY AppController's own
    power/session logic, never a real subprocess/ctypes/osascript call -
    test_phase4_platform.py-style real-backend coverage lives further down
    this file instead. `unsupported` names which of the six primitives
    should behave as an honestly-unsupported feature (never overridden at
    all, so capabilities() reports it False and calling it raises the
    base class's own NotSupported) - every other primitive is a working
    fake. `cancel_result` lets a test script exactly what
    cancel_native_shutdown() hands back without raising."""
    name = "fakeos"
    display_name = "FakeOS"

    def __init__(self, unsupported=(), cancel_result=NATIVE_SHUTDOWN_CANCELLED_MSG):
        self.calls = []
        # 🔧 Named _unsupported_features, NOT _unsupported: PlatformBackend's
        # OWN `_unsupported(self, feature)` is a METHOD every base-class
        # default stub calls (`raise self._unsupported(...)`) - an instance
        # attribute literally named `self._unsupported` would shadow it and
        # break every `super().X_now()` call below with a "'set' object is
        # not callable" TypeError instead of the intended NotSupported.
        self._unsupported_features = set(unsupported)
        self.cancel_result = cancel_result

    def shutdown_now(self, delay_s):
        if "shutdown_now" in self._unsupported_features:
            return super().shutdown_now(delay_s)
        self.calls.append(("shutdown_now", delay_s))
        return f"Shutting down in {delay_s} seconds."

    def restart_now(self, delay_s):
        if "restart_now" in self._unsupported_features:
            return super().restart_now(delay_s)
        self.calls.append(("restart_now", delay_s))
        return f"Restarting in {delay_s} seconds."

    def cancel_native_shutdown(self):
        if "cancel_native_shutdown" in self._unsupported_features:
            return super().cancel_native_shutdown()
        self.calls.append(("cancel_native_shutdown",))
        return self.cancel_result

    def sleep_now(self):
        if "sleep_now" in self._unsupported_features:
            return super().sleep_now()
        self.calls.append(("sleep_now",))
        return "Putting the PC to sleep now."

    def hibernate_now(self):
        if "hibernate_now" in self._unsupported_features:
            return super().hibernate_now()
        self.calls.append(("hibernate_now",))
        return "Hibernating the PC now."

    def log_off_now(self):
        if "log_off_now" in self._unsupported_features:
            return super().log_off_now()
        self.calls.append(("log_off_now",))
        return "Logging off now."


class PowerSessionTestBase(unittest.TestCase):
    def setUp(self):
        self.audit = Audit(store=MemoryStore())
        self.saved_audit = audit_mod.audit
        audit_mod.audit = self.audit
        # Tier 14's pending-power-timer state is module-level/global (see
        # app_control.py's own comment on why) - cleared before AND after
        # every test so one test's scheduled timer can never leak into,
        # or get silently cancelled by, the next.
        ac_mod._cancel_pending_power_timer()
        self.addCleanup(ac_mod._cancel_pending_power_timer)
        self.addCleanup(self._restore)

    def _restore(self):
        audit_mod.audit = self.saved_audit
        backends.set_backend(None)

    def _use(self, backend):
        backends.set_backend(backend)
        return backend


class ShutdownRestartTests(PowerSessionTestBase):
    """shutdown_pc/restart_pc hand delay_s straight to the backend's OWN
    delayed command - no Wave-owned timer involved, unlike the other three
    below (see automation/app_control.py's own comment on this split)."""

    def test_shutdown_pc_uses_the_explicit_delay_when_given(self):
        backend = self._use(_FakePowerBackend())
        result = AppController.shutdown_pc(5)
        self.assertIn("5 seconds", result)
        self.assertEqual(backend.calls, [("shutdown_now", 5)])

    def test_shutdown_pc_falls_back_to_the_configured_default_delay(self):
        backend = self._use(_FakePowerBackend())
        AppController.shutdown_pc()
        self.assertEqual(backend.calls, [("shutdown_now", config.POWER_ACTION_DELAY_S)])

    def test_restart_pc_uses_the_explicit_delay_when_given(self):
        backend = self._use(_FakePowerBackend())
        result = AppController.restart_pc(7)
        self.assertIn("7 seconds", result)
        self.assertEqual(backend.calls, [("restart_now", 7)])

    def test_negative_delay_is_clamped_to_zero(self):
        backend = self._use(_FakePowerBackend())
        AppController.shutdown_pc(-5)
        self.assertEqual(backend.calls, [("shutdown_now", 0)])

    def test_not_supported_is_a_plain_reply_not_a_crash(self):
        self._use(_FakePowerBackend(unsupported=("shutdown_now",)))
        self.assertIn("isn't supported on FakeOS", AppController.shutdown_pc(5))
        self._use(_FakePowerBackend(unsupported=("restart_now",)))
        self.assertIn("isn't supported on FakeOS", AppController.restart_pc(5))

    def test_shutdown_pc_is_audited_with_the_resolved_delay(self):
        self._use(_FakePowerBackend())
        AppController.shutdown_pc(5)
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status, row.args["delay_s"]), ("shutdown_pc", "ok", 5))

    def test_restart_pc_failure_is_audited_as_failed(self):
        self._use(_FakePowerBackend(unsupported=("restart_now",)))
        AppController.restart_pc(5)
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status), ("restart_pc", "failed"))


class DelayedTimerActionTests(PowerSessionTestBase):
    """sleep_pc/hibernate_pc/log_off: no native OS delay to lean on, so
    AppController schedules its OWN cancellable threading.Timer instead
    and returns immediately - the timer only calls the backend once it
    actually fires."""

    def test_sleep_pc_returns_immediately_without_calling_the_backend_yet(self):
        backend = self._use(_FakePowerBackend())
        result = AppController.sleep_pc(2)
        self.assertIn("sleeping in 2 seconds", result.lower())
        self.assertEqual(backend.calls, [])  # not fired yet - only just scheduled
        self.assertIsNotNone(ac_mod._pending_power_timer)
        self.assertEqual(ac_mod._pending_power_label, "sleeping")

    def test_sleep_pc_eventually_fires_the_real_backend_call(self):
        backend = self._use(_FakePowerBackend())
        AppController.sleep_pc(1)
        deadline = time.monotonic() + 3.0
        while not backend.calls and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(backend.calls, [("sleep_now",)])
        # The timer clears its own pending-state once it fires, so a
        # cancel_shutdown() afterwards correctly has nothing left to cancel.
        self.assertIsNone(ac_mod._pending_power_timer)

    def test_cancelling_before_it_fires_genuinely_prevents_the_action(self):
        backend = self._use(_FakePowerBackend())
        AppController.sleep_pc(1)
        msg = AppController.cancel_shutdown()
        self.assertIn("scheduled sleeping", msg)
        time.sleep(1.3)  # past the original delay
        # cancel_shutdown() always ALSO calls cancel_native_shutdown() (see
        # its own branching above), so backend.calls legitimately gets a
        # ("cancel_native_shutdown",) entry from the cancel itself - assert
        # precisely that sleep_now specifically never fired, rather than
        # the whole list being empty.
        self.assertNotIn(("sleep_now",), backend.calls, "cancelling must stop the timer from ever firing")

    def test_hibernate_pc_and_log_off_schedule_with_their_own_label(self):
        backend = self._use(_FakePowerBackend())
        AppController.hibernate_pc(5)
        self.assertEqual(ac_mod._pending_power_label, "hibernating")
        AppController.log_off(5)  # scheduling a second one replaces the first
        self.assertEqual(ac_mod._pending_power_label, "logging off")
        self.assertEqual(backend.calls, [])

    def test_scheduling_a_new_one_cancels_whatever_was_already_pending(self):
        """Only ONE Wave-owned power timer can be pending at a time - see
        automation/app_control.py's own comment on why this is a deliberate
        simplification, not an oversight."""
        backend = self._use(_FakePowerBackend())
        AppController.sleep_pc(1)
        first_timer = ac_mod._pending_power_timer
        AppController.log_off(5)
        # Timer.cancel() just does self.finished.set() synchronously in the
        # calling thread - checking that Event is the non-racy way to
        # confirm the cancel was ISSUED. is_alive() is NOT safe to assert
        # on here: the cancelled thread may not have woken from its wait()
        # and actually unwound yet, even though it's guaranteed never to
        # call its function now.
        self.assertTrue(first_timer.finished.is_set(), "the first timer should have been cancelled")
        time.sleep(1.3)  # past sleep's original 1s delay
        self.assertEqual(backend.calls, [], "the replaced sleep action must never fire")

    def test_capabilities_check_first_fails_immediately_with_no_timer_scheduled(self):
        """🏆 The actual fix this test anchors: without checking
        capabilities() BEFORE scheduling, an honestly-unsupported feature
        (hibernate on macOS, say) would schedule anyway and only fail
        silently, in a background thread, 15+ seconds later, with no
        caller left to tell. This confirms the failure is immediate AND
        that nothing gets scheduled at all.

        capabilities() itself (automation/backends/base.py) decides
        "supported" with a CLASS-level identity check - `getattr(type(self),
        f) is not getattr(PlatformBackend, f)` - so a _FakePowerBackend
        instance flag can't fake "unsupported" for this one; hibernate_now
        has to be genuinely missing from the subclass, the same shape
        MacBackend not overriding cancel_native_shutdown already uses."""
        class _NoHibernate(_FakePowerBackend):
            hibernate_now = PlatformBackend.hibernate_now
        backend = self._use(_NoHibernate())
        result = AppController.hibernate_pc(5)
        self.assertIn("isn't supported on FakeOS", result)
        self.assertIsNone(ac_mod._pending_power_timer)
        self.assertEqual(backend.calls, [])

    def test_sleep_pc_is_audited_immediately_even_though_the_action_is_delayed(self):
        self._use(_FakePowerBackend())
        AppController.sleep_pc(5)
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status, row.args["delay_s"]), ("sleep_pc", "ok", 5))


class CancelShutdownTests(PowerSessionTestBase):
    def test_cancels_a_pending_wave_timer_only(self):
        backend = self._use(_FakePowerBackend(cancel_result="There wasn't a shutdown or restart scheduled to cancel."))
        AppController.log_off(5)
        msg = AppController.cancel_shutdown()
        self.assertEqual(msg, "Cancelled the scheduled logging off.")
        self.assertIsNone(ac_mod._pending_power_timer)

    def test_cancels_a_native_shutdown_only(self):
        backend = self._use(_FakePowerBackend())  # cancel_result defaults to the real "cancelled" sentinel
        msg = AppController.cancel_shutdown()
        self.assertEqual(msg, "Cancelled the scheduled shutdown/restart.")
        self.assertEqual(backend.calls, [("cancel_native_shutdown",)])

    def test_cancels_both_at_once_with_a_combined_message(self):
        backend = self._use(_FakePowerBackend())
        AppController.sleep_pc(5)
        msg = AppController.cancel_shutdown()
        self.assertEqual(msg, "Cancelled the scheduled sleeping and the scheduled shutdown/restart.")

    def test_nothing_pending_says_so_plainly(self):
        self._use(_FakePowerBackend(cancel_result="There wasn't a shutdown or restart scheduled to cancel."))
        self.assertEqual(AppController.cancel_shutdown(), "There wasn't anything scheduled to cancel.")

    def test_native_cancel_not_supported_surfaces_that_specific_message(self):
        """macOS never overrides cancel_native_shutdown at all (see
        automation/backends/macos.py's own comment) - this is the shape
        that exercises, with nothing Wave-owned pending either."""
        self._use(_FakePowerBackend(unsupported=("cancel_native_shutdown",)))
        self.assertIn("isn't supported on FakeOS", AppController.cancel_shutdown())

    def test_is_always_a_successful_audit_row_even_when_nothing_was_pending(self):
        """Safe to call speculatively ("cancel shutdown" just in case) -
        never itself a failure, whatever it finds."""
        self._use(_FakePowerBackend(cancel_result="There wasn't a shutdown or restart scheduled to cancel."))
        AppController.cancel_shutdown()
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.status), ("cancel_shutdown", "ok"))


# =============================================================================
# Real backend command construction - a representative sample, same
# FakeShell-based style test_phase4_platform.py already uses for the rest
# of each backend.
# =============================================================================
class WindowsPowerSessionTests(unittest.TestCase):
    def test_shutdown_now_and_restart_now_call_shutdown_exe_with_seconds(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            msg = b.shutdown_now(15)
        mocked.assert_called_once_with(["shutdown", "/s", "/t", "15"], capture_output=True, text=True, timeout=10)
        self.assertIn("15 seconds", msg)
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            msg = b.restart_now(30)
        mocked.assert_called_once_with(["shutdown", "/r", "/t", "30"], capture_output=True, text=True, timeout=10)
        self.assertIn("30 seconds", msg)

    def test_cancel_native_shutdown_reports_the_shared_sentinel_on_success(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(b.cancel_native_shutdown(), NATIVE_SHUTDOWN_CANCELLED_MSG)
        mocked.assert_called_once_with(["shutdown", "/a"], capture_output=True, text=True, timeout=10)

    def test_cancel_native_shutdown_says_so_when_nothing_was_scheduled(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=1, stdout="", stderr="")
            msg = b.cancel_native_shutdown()
        self.assertNotEqual(msg, NATIVE_SHUTDOWN_CANCELLED_MSG)
        self.assertIn("wasn't a shutdown or restart scheduled", msg)

    def test_sleep_now_and_hibernate_now_call_setsuspendstate_with_the_right_flag(self):
        """SetSuspendState(bHibernate, bForce, bWakeupEventsDisabled) - the
        only difference between sleep and hibernate is bHibernate's value.
        ctypes.windll doesn't exist at all on this non-Windows sandbox
        (unlike win32gui/win32con, which at least import-fail cleanly), so
        it's patched into existence for the duration of each test."""
        b = WindowsBackend()
        fake_windll = mock.MagicMock()
        with mock.patch("ctypes.windll", fake_windll, create=True):
            self.assertIn("sleep", b.sleep_now().lower())
        fake_windll.powrprof.SetSuspendState.assert_called_once_with(False, True, False)

        fake_windll = mock.MagicMock()
        with mock.patch("ctypes.windll", fake_windll, create=True):
            self.assertIn("hibernat", b.hibernate_now().lower())
        fake_windll.powrprof.SetSuspendState.assert_called_once_with(True, True, False)

    def test_log_off_now_calls_shutdown_l(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            msg = b.log_off_now()
        mocked.assert_called_once_with(["shutdown", "/l"], capture_output=True, text=True, timeout=10)
        self.assertIn("Logging off", msg)

    def test_list_services_returns_sorted_names(self):
        b = WindowsBackend()
        fake_services = [mock.Mock(**{"name.return_value": n}) for n in ("Zeta", "Alpha", "Mu")]
        # win_service_iter is Windows-only - this sandbox's real psutil
        # module doesn't have the attribute AT ALL (not just non-functional),
        # so patching it here needs create=True.
        with mock.patch("psutil.win_service_iter", return_value=fake_services, create=True):
            self.assertEqual(b.list_services(), ["Alpha", "Mu", "Zeta"])


class LinuxPowerSessionTests(unittest.TestCase):
    def test_short_delay_rounds_to_now_and_says_so_honestly(self):
        """🔧 FIX anchor: a delay under 30s (which includes
        POWER_ACTION_DELAY_S's own 15s default) used to still claim 'about
        1 minute' to cancel, even though the real `shutdown -h now` command
        just sent has no real delay at all."""
        sh = _FakeShell(installed={"shutdown"})
        b = LinuxBackend(sh)
        msg = b.shutdown_now(15)
        self.assertEqual(sh.calls[-1], ("run", ["shutdown", "-h", "now"]))
        self.assertIn("now", msg.lower())
        # The honest message legitimately uses the word "minute" (as part of
        # "minute-granularity") - what it must NOT do is the old wrong thing,
        # promising "about N minute(s)" to cancel when none exists.
        self.assertNotIn("about", msg.lower())

    def test_longer_delay_is_rounded_to_whole_minutes_and_reported_that_way(self):
        sh = _FakeShell(installed={"shutdown"})
        b = LinuxBackend(sh)
        msg = b.restart_now(90)  # 90s -> round(1.5) -> 2 minutes (banker's rounding)
        self.assertEqual(sh.calls[-1], ("run", ["shutdown", "-r", "+2"]))
        self.assertIn("2 minute", msg)

    def test_cancel_native_shutdown(self):
        sh = _FakeShell(installed={"shutdown"}, results={("shutdown", "-c"): (0, "")})
        self.assertEqual(LinuxBackend(sh).cancel_native_shutdown(), NATIVE_SHUTDOWN_CANCELLED_MSG)

    def test_sleep_and_hibernate_call_systemctl(self):
        sh = _FakeShell(installed={"systemctl"})
        b = LinuxBackend(sh)
        b.sleep_now()
        self.assertEqual(sh.calls[-1], ("run", ["systemctl", "suspend"]))
        b.hibernate_now()
        self.assertEqual(sh.calls[-1], ("run", ["systemctl", "hibernate"]))

    def test_log_off_prefers_gnome_session_quit_over_loginctl(self):
        sh = _FakeShell(installed={"gnome-session-quit", "loginctl"})
        LinuxBackend(sh).log_off_now()
        self.assertEqual(sh.calls[-1], ("run", ["gnome-session-quit", "--logout", "--no-prompt"]))

    def test_log_off_falls_back_to_loginctl_terminate_user(self):
        sh = _FakeShell(installed={"loginctl"})
        with mock.patch("getpass.getuser", return_value="alex"):
            LinuxBackend(sh).log_off_now()
        self.assertEqual(sh.calls[-1], ("run", ["loginctl", "terminate-user", "alex"]))

    def test_without_shutdown_command_every_power_action_is_not_supported(self):
        sh = _FakeShell()
        for method, args in (("shutdown_now", (15,)), ("restart_now", (15,)), ("cancel_native_shutdown", ())):
            with self.assertRaises(NotSupported):
                getattr(LinuxBackend(sh), method)(*args)

    def test_without_systemd_sleep_and_hibernate_are_not_supported(self):
        sh = _FakeShell()
        with self.assertRaises(NotSupported):
            LinuxBackend(sh).sleep_now()
        with self.assertRaises(NotSupported):
            LinuxBackend(sh).hibernate_now()

    def test_list_services_parses_running_units(self):
        sh = _FakeShell(installed={"systemctl"}, results={
            ("systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--no-pager"):
                (0, "sshd.service loaded active running OpenSSH server\n"
                    "nginx.service loaded active running A high performance web server\n")})
        self.assertEqual(LinuxBackend(sh).list_services(), ["nginx.service", "sshd.service"])


class MacPowerSessionTests(unittest.TestCase):
    def test_shutdown_now_spawns_a_detached_delayed_applescript(self):
        """Spawned, not run - so it still fires even if Wave's own process
        exits during the countdown (see macos.py's own comment on why
        there's no cancel_native_shutdown override to match)."""
        sh = _FakeShell()
        msg = MacBackend(sh).shutdown_now(10)
        self.assertEqual(sh.calls[-1][0], "spawn")
        self.assertEqual(sh.calls[-1][1], ["osascript", "-e", "delay 10",
                                           "-e", 'tell application "System Events" to shut down'])
        self.assertIn("can't be", msg.lower())  # honestly flags it can't be cancelled

    def test_cancel_native_shutdown_is_honestly_not_supported(self):
        """Deliberately NOT overridden - see this backend's own module
        comment on why a spawned, detached AppleScript can't be cancelled
        once it's running."""
        with self.assertRaises(NotSupported):
            MacBackend(_FakeShell()).cancel_native_shutdown()

    def test_sleep_now_calls_pmset_sleepnow(self):
        sh = _FakeShell(results={("pmset", "sleepnow"): (0, "")})
        self.assertIn("sleep", MacBackend(sh).sleep_now().lower())
        self.assertEqual(sh.calls[-1], ("run", ["pmset", "sleepnow"]))

    def test_hibernate_now_is_always_not_supported(self):
        """macOS ties real hibernation to its own sleep settings with no
        on-demand trigger - faking it with plain sleep would be dishonest
        about what actually happened (see this backend's own comment)."""
        with self.assertRaises(NotSupported):
            MacBackend(_FakeShell()).hibernate_now()

    def test_hibernate_now_is_listed_in_always_unsupported_so_capabilities_stays_honest(self):
        """🏆 The actual fix this anchors: hibernate_now IS overridden on
        MacBackend (purely to give this specific message), which would
        otherwise make capabilities()'s plain "is it overridden" check
        wrongly call it supported."""
        self.assertIn("hibernate_now", MacBackend.ALWAYS_UNSUPPORTED)
        self.assertFalse(MacBackend(_FakeShell()).capabilities()["hibernate_now"])

    def test_log_off_now_calls_system_events_log_out(self):
        sh = _FakeShell(results={("osascript", "-e", 'tell application "System Events" to log out'): (0, "")})
        self.assertIn("off", MacBackend(sh).log_off_now().lower())

    def test_list_services_skips_the_header_line(self):
        sh = _FakeShell(results={("launchctl", "list"): (
            0, 'PID\tStatus\tLabel\n123\t0\tcom.apple.bluetoothd\n456\t0\tcom.acme.worker\n')})
        self.assertEqual(MacBackend(sh).list_services(), ["com.acme.worker", "com.apple.bluetoothd"])


if __name__ == "__main__":
    unittest.main()
