"""Tier 23 — the remote mobile dashboard (remote/ package) and its two
small hooks into ui/settings_panel.py and ui/main_window.py.

Closes §2.2 of claude/JARVIS_GAP_ANALYSIS.md: Jarvis ships a FastAPI+
uvicorn web control panel paired to a phone by QR code; Wave had nothing
reachable off the desktop at all.

Part A tests remote/ directly - plain Python plus FastAPI's TestClient,
no Qt involved at all. Part B tests the UI-layer hooks using this
project's standard fake-Qt stand-in (test_phase4_ui_logic.py's _QtModule,
reused via test_tier5_ui_logic.py's import_with_fakes), same convention
every other ui/*.py logic test in this suite follows, because there's no
display or PyQt6 in this sandbox.
"""
import threading
import time
import types
import unittest
from unittest import mock

import _phase4_support as S

try:
    import PyQt6  # noqa: F401
    REAL_QT = True
except ImportError:
    REAL_QT = False

import test_tier5_ui_logic as T
import test_phase4_ui_logic as P4

from remote import netinfo, qr, security
from remote.bridge import RemoteBridge
from remote.server import DashboardState, create_app
from remote import runner as runner_mod


# =============================================================================
# Part A — remote/ itself
# =============================================================================
class SecurityTests(unittest.TestCase):
    def test_pairing_token_and_code_are_distinct_each_call(self):
        self.assertNotEqual(security.generate_pairing_token(), security.generate_pairing_token())
        self.assertNotEqual(security.generate_pairing_code(), security.generate_pairing_code())

    def test_pairing_code_uses_only_the_unambiguous_alphabet(self):
        code = security.generate_pairing_code()
        self.assertEqual(len(code), security._CODE_LEN)
        self.assertTrue(set(code) <= set(security._CODE_ALPHABET))
        for bad in "0O1I":
            self.assertNotIn(bad, code if bad not in security._CODE_ALPHABET else "")

    def test_constant_time_eq_basic_behavior(self):
        self.assertTrue(security.constant_time_eq("abc", "abc"))
        self.assertFalse(security.constant_time_eq("abc", "abd"))
        self.assertFalse(security.constant_time_eq("abc", "abcd"))

    def test_session_signer_round_trip(self):
        signer = security.SessionSigner("s3cret")
        token = signer.issue()
        self.assertTrue(signer.verify(token))

    def test_session_signer_rejects_tampered_token(self):
        signer = security.SessionSigner("s3cret")
        token = signer.issue()
        self.assertFalse(signer.verify(token + "x"))
        self.assertFalse(signer.verify(token[:-1]))

    def test_session_signer_rejects_wrong_secret(self):
        token = security.SessionSigner("s3cret").issue()
        self.assertFalse(security.SessionSigner("other").verify(token))

    def test_session_signer_rejects_expired_token(self):
        signer = security.SessionSigner("s3cret")
        token = signer.issue(now=1000.0)
        self.assertTrue(signer.verify(token, now=1000.0 + security.SESSION_TTL_SECONDS - 1))
        self.assertFalse(signer.verify(token, now=1000.0 + security.SESSION_TTL_SECONDS + 1))

    def test_session_signer_rejects_malformed_tokens_without_raising(self):
        signer = security.SessionSigner("s3cret")
        for bad in (None, "", "no-dot-here", "abc.def", "notanumber.deadbeef"):
            self.assertFalse(signer.verify(bad))

    def test_session_signer_refresh_issues_a_verifiable_new_token(self):
        signer = security.SessionSigner("s3cret")
        fresh = signer.refresh(now=5000.0)
        self.assertTrue(signer.verify(fresh, now=5000.0))

    def test_rate_limiter_locks_out_after_max_attempts_from_one_ip(self):
        rl = security.PairingRateLimiter(max_attempts=3, window_seconds=60)
        for _ in range(2):
            rl.record_failure("1.2.3.4")
        self.assertFalse(rl.is_locked_out("1.2.3.4"))
        rl.record_failure("1.2.3.4")
        self.assertTrue(rl.is_locked_out("1.2.3.4"))

    def test_rate_limiter_tracks_ips_independently(self):
        rl = security.PairingRateLimiter(max_attempts=1, window_seconds=60)
        rl.record_failure("1.1.1.1")
        self.assertTrue(rl.is_locked_out("1.1.1.1"))
        self.assertFalse(rl.is_locked_out("2.2.2.2"))

    def test_rate_limiter_window_expires_old_failures(self):
        rl = security.PairingRateLimiter(max_attempts=1, window_seconds=60)
        rl.record_failure("1.1.1.1", now=1000.0)
        self.assertFalse(rl.is_locked_out("1.1.1.1", now=1000.0 + 61))

    def test_rate_limiter_success_clears_the_strike_count(self):
        rl = security.PairingRateLimiter(max_attempts=1, window_seconds=60)
        rl.record_failure("1.1.1.1")
        rl.record_success("1.1.1.1")
        self.assertFalse(rl.is_locked_out("1.1.1.1"))

    def test_new_pairing_material_returns_three_distinct_strings(self):
        token, code, secret = security.new_pairing_material()
        self.assertTrue(token and code and secret)
        self.assertEqual(len({token, code, secret}), 3)


class NetInfoTests(unittest.TestCase):
    def test_get_lan_ip_never_raises_and_returns_a_string(self):
        ip = netinfo.get_lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(ip)

    def test_get_lan_ip_falls_back_to_loopback_when_sockets_fail(self):
        with mock.patch.object(netinfo.socket, "socket", side_effect=OSError("no network")), \
             mock.patch.object(netinfo.socket, "gethostbyname", side_effect=OSError("no dns")):
            self.assertEqual(netinfo.get_lan_ip(), "127.0.0.1")

    def test_pairing_url_format(self):
        url = netinfo.pairing_url("192.168.1.5", 8787, "tok123")
        self.assertEqual(url, "http://192.168.1.5:8787/login?token=tok123")


class QrTests(unittest.TestCase):
    def test_build_pairing_qr_png_produces_a_real_png_when_qrcode_is_installed(self):
        if not qr.HAS_QRCODE:
            self.skipTest("qrcode not installed in this environment")
        png = qr.build_pairing_qr_png("http://example.invalid/login?token=x")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_build_pairing_qr_png_raises_cleanly_without_qrcode(self):
        with mock.patch.object(qr, "HAS_QRCODE", False):
            with self.assertRaises(RuntimeError):
                qr.build_pairing_qr_png("http://example.invalid")


class _SlowFakeBrain:
    """think_stream() sleeps briefly before yielding, so a test can prove
    RemoteBridge.ask() serializes two concurrent callers rather than
    letting them interleave."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.calls = []

    def think_stream(self, text):
        self.calls.append(("start", text, time.monotonic()))
        time.sleep(self.delay)
        self.calls.append(("end", text, time.monotonic()))
        yield f"reply to {text}"


class BridgeTests(unittest.TestCase):
    def test_ask_joins_streamed_chunks_into_one_string(self):
        class Brain:
            def think_stream(self, text):
                yield "Hello, "
                yield text

        bridge = RemoteBridge(Brain())
        with mock.patch("automation.audit.audit.record"):
            self.assertEqual(bridge.ask("world"), "Hello, world")

    def test_ask_records_an_audit_row_tagged_source_remote(self):
        class Brain:
            def think_stream(self, text):
                yield "ok"

        bridge = RemoteBridge(Brain())
        with mock.patch("automation.audit.audit.record") as rec:
            bridge.ask("do the thing")
        rec.assert_called_once()
        args, kwargs = rec.call_args
        self.assertEqual(args[0], "remote_message")
        self.assertEqual(kwargs.get("source"), "remote")

    def test_ask_returns_an_apology_instead_of_raising_when_brain_errors(self):
        class Brain:
            def think_stream(self, text):
                raise RuntimeError("boom")
                yield  # pragma: no cover - unreachable, makes this a generator

        bridge = RemoteBridge(Brain())
        reply = bridge.ask("anything")
        self.assertIn("wrong", reply.lower())

    def test_two_concurrent_asks_are_serialized_not_interleaved(self):
        brain = _SlowFakeBrain(delay=0.05)
        bridge = RemoteBridge(brain)
        with mock.patch("automation.audit.audit.record"):
            results = {}

            def worker(key, text):
                results[key] = bridge.ask(text)

            t1 = threading.Thread(target=worker, args=("a", "first"))
            t2 = threading.Thread(target=worker, args=("b", "second"))
            t1.start()
            time.sleep(0.01)  # make sure t1 actually acquires the lock first
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(results["a"], "reply to first")
        self.assertEqual(results["b"], "reply to second")
        # Serialized means the second call's "start" happens AFTER the
        # first call's "end" - never interleaved.
        starts_ends = brain.calls
        first_end = next(t for kind, text, t in starts_ends if kind == "end" and text == "first")
        second_start = next(t for kind, text, t in starts_ends if kind == "start" and text == "second")
        self.assertLessEqual(first_end, second_start)

    def test_ask_timeout_returns_a_busy_message_without_blocking_forever(self):
        bridge = RemoteBridge(_SlowFakeBrain(delay=0.2))
        with mock.patch("automation.audit.audit.record"):
            holder_thread = threading.Thread(target=bridge.ask, args=("hold the lock",))
            holder_thread.start()
            time.sleep(0.02)
            reply = bridge.ask("second caller", timeout=0.01)
            holder_thread.join()
        self.assertIn("finishing", reply.lower())


def _make_state(assistant_name="TestWave", bridge=None):
    bridge = bridge or RemoteBridge(types.SimpleNamespace(think_stream=lambda t: iter([f"echo: {t}"])))
    token, code, secret = security.new_pairing_material()
    return DashboardState(bridge, assistant_name, token, code, secret), token, code


class ServerTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        self.state, self.token, self.code = _make_state()
        self.app = create_app(self.state)
        self.client = TestClient(self.app)

    def _pair(self, value=None):
        return self.client.post(
            "/api/pair", json={"token": value or self.token},
            headers={"X-Wave-Dashboard": "1"},
        )

    def test_message_without_session_is_rejected(self):
        r = self.client.post("/api/message", json={"text": "hi"}, headers={"X-Wave-Dashboard": "1"})
        self.assertEqual(r.status_code, 401)

    def test_pair_without_csrf_header_is_rejected(self):
        r = self.client.post("/api/pair", json={"token": self.token})
        self.assertEqual(r.status_code, 400)

    def test_message_without_csrf_header_is_rejected_even_when_paired(self):
        self._pair()
        r = self.client.post("/api/message", json={"text": "hi"})
        self.assertEqual(r.status_code, 400)

    def test_wrong_token_is_rejected(self):
        r = self._pair("not-the-token")
        self.assertEqual(r.status_code, 401)

    def test_correct_token_pairs_and_sets_a_session_cookie(self):
        r = self._pair()
        self.assertEqual(r.status_code, 200)
        self.assertIn("wave_session", r.cookies)

    def test_short_human_code_also_pairs(self):
        r = self._pair(self.code.lower())  # typed by hand, case-insensitive
        self.assertEqual(r.status_code, 200)

    def test_pairing_is_rate_limited_after_repeated_failures(self):
        for _ in range(security.MAX_PAIR_ATTEMPTS):
            self._pair("wrong")
        r = self._pair("wrong")
        self.assertEqual(r.status_code, 429)

    def test_message_after_pairing_reaches_the_bridge_and_returns_its_reply(self):
        self._pair()
        r = self.client.post("/api/message", json={"text": "turn on wifi"}, headers={"X-Wave-Dashboard": "1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["reply"], "echo: turn on wifi")

    def test_empty_message_is_rejected(self):
        self._pair()
        r = self.client.post("/api/message", json={"text": "   "}, headers={"X-Wave-Dashboard": "1"})
        self.assertEqual(r.status_code, 400)

    def test_status_reflects_pairing_state(self):
        self.assertFalse(self.client.get("/api/status").json()["paired"])
        self._pair()
        self.assertTrue(self.client.get("/api/status").json()["paired"])

    def test_index_redirects_to_login_without_a_session(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertTrue(r.headers["location"].endswith("/login"))

    def test_index_serves_the_chat_page_once_paired(self):
        self._pair()
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)

    def test_login_page_is_always_reachable(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)

    def test_rotating_pairing_material_invalidates_the_old_session(self):
        self._pair()
        self.assertTrue(self.client.get("/api/status").json()["paired"])
        token2, code2, secret2 = security.new_pairing_material()
        self.state.rotate(token2, code2, secret2)
        # The OLD cookie the client is still holding no longer verifies -
        # the new secret's HMAC doesn't match it.
        self.assertFalse(self.client.get("/api/status").json()["paired"])


class CreateAppTests(unittest.TestCase):
    def test_create_app_raises_informatively_when_fastapi_is_absent(self):
        import remote.server as server_mod
        with mock.patch.object(server_mod, "HAS_FASTAPI", False):
            with self.assertRaises(RuntimeError):
                server_mod.create_app(_make_state()[0])


class RunnerTests(unittest.TestCase):
    def _fake_brain(self):
        return types.SimpleNamespace(think_stream=lambda t: iter([f"echo: {t}"]))

    def test_available_reflects_fastapi_uvicorn_presence(self):
        server = runner_mod.RemoteDashboardServer(self._fake_brain())
        with mock.patch.object(runner_mod, "HAS_FASTAPI", False):
            self.assertFalse(server.available)
        with mock.patch.object(runner_mod, "HAS_FASTAPI", True), \
             mock.patch.object(runner_mod, "uvicorn", mock.Mock()):
            self.assertTrue(server.available)

    def test_start_reports_failure_cleanly_when_unavailable(self):
        server = runner_mod.RemoteDashboardServer(self._fake_brain())
        with mock.patch.object(runner_mod, "HAS_FASTAPI", False):
            ok, msg = server.start(port=0)
        self.assertFalse(ok)
        self.assertIn("fastapi", msg.lower())

    def test_pairing_info_is_none_before_start(self):
        server = runner_mod.RemoteDashboardServer(self._fake_brain())
        self.assertIsNone(server.pairing_info())

    def test_regenerate_pairing_is_none_before_start(self):
        server = runner_mod.RemoteDashboardServer(self._fake_brain())
        self.assertIsNone(server.regenerate_pairing())

    def test_full_lifecycle_on_a_real_loopback_port(self):
        """One genuine end-to-end pass: start a real uvicorn server on
        loopback, pair over real HTTP, send a message, regenerate the
        pairing and confirm the old session stops working - everything
        else in this file exercises the pieces individually with fakes,
        but this is the one test that proves they actually fit together."""
        import httpx

        server = runner_mod.RemoteDashboardServer(self._fake_brain(), assistant_name="LoopbackWave")
        ok, msg = server.start(host="127.0.0.1", port=0 if False else _free_port())
        self.assertTrue(ok, msg)
        try:
            deadline = time.monotonic() + 3
            info = None
            while time.monotonic() < deadline:
                info = server.pairing_info()
                if info is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(info)

            base = f"http://127.0.0.1:{info.port}"
            with httpx.Client(base_url=base) as client:
                r = client.post("/api/pair", json={"token": info.code},
                                 headers={"X-Wave-Dashboard": "1"})
                self.assertEqual(r.status_code, 200)
                r = client.post("/api/message", json={"text": "status check"},
                                 headers={"X-Wave-Dashboard": "1"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["reply"], "echo: status check")

                new_info = server.regenerate_pairing()
                self.assertNotEqual(new_info.code, info.code)

                r = client.post("/api/message", json={"text": "should fail now"},
                                 headers={"X-Wave-Dashboard": "1"})
                self.assertEqual(r.status_code, 401)
        finally:
            server.stop()
        self.assertFalse(server.is_running)


def _free_port() -> int:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# =============================================================================
# Part B — the ui/ hooks (fake-Qt stand-in, no display/PyQt6 needed)
# =============================================================================
def _install_fakes():
    S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                           hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                           typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                           size=lambda: (1, 1))
    S.install_fake_module("pywhatkit")
    T.install_stub_with_list_widgets()


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class SettingsPanelRemoteHooksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fakes()
        cls.sp = T.import_with_fakes("ui.settings_panel")

    def _panel(self):
        return object.__new__(self.sp.SettingsPanel)

    def test_enabling_the_checkbox_starts_the_dashboard_on_the_chosen_port(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(start_remote_dashboard=mock.Mock())
        panel.remote_port_spin = types.SimpleNamespace(value=lambda: 9001)
        panel._on_remote_dashboard_toggled(True)
        panel._main.start_remote_dashboard.assert_called_once_with(9001)

    def test_disabling_the_checkbox_stops_the_dashboard_and_resets_the_display(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(stop_remote_dashboard=mock.Mock())
        panel.remote_status_label = mock.Mock()
        panel.remote_qr_label = mock.Mock()
        panel.remote_code_label = mock.Mock()
        panel.remote_url_label = mock.Mock()
        panel.btn_regenerate_pairing = mock.Mock()
        panel._on_remote_dashboard_toggled(False)
        panel._main.stop_remote_dashboard.assert_called_once()
        panel.remote_status_label.setText.assert_called_once_with("Off.")
        panel.btn_regenerate_pairing.setEnabled.assert_called_once_with(False)

    def test_show_result_failure_unchecks_the_box_and_disables_regenerate(self):
        panel = self._panel()
        panel.remote_status_label = mock.Mock()
        panel.cb_remote_dashboard = mock.Mock()
        panel.btn_regenerate_pairing = mock.Mock()
        panel.show_remote_dashboard_result(False, "fastapi not installed")
        panel.cb_remote_dashboard.setChecked.assert_called_once_with(False)
        panel.btn_regenerate_pairing.setEnabled.assert_called_once_with(False)

    def test_show_result_success_enables_regenerate_and_refreshes_pairing(self):
        panel = self._panel()
        panel.remote_status_label = mock.Mock()
        panel.btn_regenerate_pairing = mock.Mock()
        panel.refresh_remote_pairing_display = mock.Mock()
        panel.show_remote_dashboard_result(True, "Remote dashboard running on port 8787.")
        panel.btn_regenerate_pairing.setEnabled.assert_called_once_with(True)
        panel.refresh_remote_pairing_display.assert_called_once()

    def test_regenerate_click_delegates_to_main_window(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(regenerate_remote_pairing=mock.Mock())
        panel._on_regenerate_pairing_clicked()
        panel._main.regenerate_remote_pairing.assert_called_once()

    def test_refresh_pairing_display_does_nothing_when_dashboard_not_started(self):
        panel = self._panel()
        panel._main = types.SimpleNamespace(remote_pairing_info=lambda: None)
        panel.remote_code_label = mock.Mock()
        panel.refresh_remote_pairing_display()
        panel.remote_code_label.setText.assert_not_called()

    def test_refresh_pairing_display_sets_code_and_url_text(self):
        panel = self._panel()
        info = types.SimpleNamespace(url="http://1.2.3.4:8787/login?token=x", code="ABCD1234", lan_ip="1.2.3.4", port=8787)
        panel._main = types.SimpleNamespace(remote_pairing_info=lambda: info)
        panel.remote_code_label = mock.Mock()
        panel.remote_url_label = mock.Mock()
        panel.remote_qr_label = mock.Mock()
        with mock.patch("remote.qr.HAS_QRCODE", False):
            panel.refresh_remote_pairing_display()
        panel.remote_code_label.setText.assert_called_once_with("ABCD1234")
        panel.remote_url_label.setText.assert_called_once_with(info.url)
        panel.remote_qr_label.setText.assert_called_once()


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class MainWindowRemoteHooksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fakes()
        cls.mod = T.import_with_fakes("ui.main_window")

    def _bare_window(self):
        win = self.mod.MainWindow.__new__(self.mod.MainWindow)
        win.remote_dashboard = None
        win.worker_pool = mock.Mock()
        win.toast = mock.Mock()
        return win

    def test_get_remote_dashboard_imports_lazily_and_caches_the_instance(self):
        win = self._bare_window()
        fake_server = mock.Mock()
        fake_module = types.SimpleNamespace(RemoteDashboardServer=mock.Mock(return_value=fake_server))
        with mock.patch.dict("sys.modules", {"remote.runner": fake_module}):
            first = win._get_remote_dashboard()
            second = win._get_remote_dashboard()
        self.assertIs(first, fake_server)
        self.assertIs(second, fake_server)
        fake_module.RemoteDashboardServer.assert_called_once()  # not re-constructed on the second call

    def test_start_remote_dashboard_submits_to_the_worker_pool(self):
        win = self._bare_window()
        fake_server = mock.Mock()
        win._get_remote_dashboard = mock.Mock(return_value=fake_server)
        fake_worker = types.SimpleNamespace(signals=types.SimpleNamespace(result=mock.Mock(), error=mock.Mock()))
        win.worker_pool.submit = mock.Mock(return_value=fake_worker)
        win.start_remote_dashboard(9100)
        win.worker_pool.submit.assert_called_once_with(fake_server.start, host="0.0.0.0", port=9100)

    def test_start_result_success_notifies_settings_panel_and_toast(self):
        win = self._bare_window()
        panel = mock.Mock()
        win.settings_panel = panel
        win._on_remote_dashboard_start_result((True, "Remote dashboard running on port 8787."))
        panel.show_remote_dashboard_result.assert_called_once_with(True, "Remote dashboard running on port 8787.")
        win.toast.show.assert_called_once_with("Remote dashboard running on port 8787.", "success")

    def test_start_result_failure_uses_the_error_toast_style(self):
        win = self._bare_window()
        win.settings_panel = mock.Mock()
        win._on_remote_dashboard_start_result((False, "fastapi not installed"))
        win.toast.show.assert_called_once_with("fastapi not installed", "error")

    def test_stop_remote_dashboard_is_a_no_op_when_never_started(self):
        win = self._bare_window()
        win.stop_remote_dashboard()
        win.worker_pool.submit.assert_not_called()

    def test_stop_remote_dashboard_submits_stop_when_running(self):
        win = self._bare_window()
        fake_server = mock.Mock()
        win.remote_dashboard = fake_server
        win.stop_remote_dashboard()
        win.worker_pool.submit.assert_called_once_with(fake_server.stop)

    def test_regenerate_remote_pairing_no_op_when_never_started(self):
        win = self._bare_window()
        win.settings_panel = mock.Mock()
        win.regenerate_remote_pairing()
        win.settings_panel.refresh_remote_pairing_display.assert_not_called()

    def test_regenerate_remote_pairing_refreshes_display_and_toasts(self):
        win = self._bare_window()
        fake_server = mock.Mock()
        win.remote_dashboard = fake_server
        win.settings_panel = mock.Mock()
        win.regenerate_remote_pairing()
        fake_server.regenerate_pairing.assert_called_once()
        win.settings_panel.refresh_remote_pairing_display.assert_called_once()
        win.toast.show.assert_called_once()

    def test_remote_pairing_info_delegates_to_the_server(self):
        win = self._bare_window()
        fake_server = mock.Mock()
        fake_server.pairing_info.return_value = "info"
        win.remote_dashboard = fake_server
        self.assertEqual(win.remote_pairing_info(), "info")

    def test_remote_pairing_info_is_none_when_never_started(self):
        win = self._bare_window()
        self.assertIsNone(win.remote_pairing_info())


@unittest.skipIf(REAL_QT, "real PyQt6 present - this stub-based test is for headless boxes")
class SaveLoadSettingsRemoteFieldsTests(unittest.TestCase):
    """save_settings()/load_settings() round-trip for the two new fields,
    same mock.patch.object(module, "SETTINGS_FILE", ...) convention
    test_tier9_theme.py's own save/load tests use."""

    @classmethod
    def setUpClass(cls):
        _install_fakes()
        cls.mod = T.import_with_fakes("ui.main_window")

    def _bare_window(self):
        win = self.mod.MainWindow.__new__(self.mod.MainWindow)
        win.toast = mock.Mock()
        return win

    def test_save_settings_includes_remote_fields_when_widgets_present(self):
        import json
        import tempfile
        win = self._bare_window()
        win.focus_mode = False  # save_settings() always reads this; __init__ normally sets it
        panel = types.SimpleNamespace(
            cb_remote_dashboard=types.SimpleNamespace(isChecked=lambda: True),
            remote_port_spin=types.SimpleNamespace(value=lambda: 9090),
        )
        win.settings_panel = panel
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = f.name
        try:
            with mock.patch.object(self.mod, "SETTINGS_FILE", tmp_path):
                win.save_settings()
            with open(tmp_path) as f:
                data = json.load(f)
            self.assertTrue(data["remote_dashboard_enabled"])
            self.assertEqual(data["remote_dashboard_port"], 9090)
        finally:
            import os
            os.unlink(tmp_path)

    def test_load_settings_sets_port_before_enabling_so_the_right_port_is_used(self):
        import json
        import tempfile
        win = self._bare_window()
        call_order = []

        class _Spin:
            def setValue(self, v):
                call_order.append(("port", v))

        class _CheckBox:
            def setChecked(self, v):
                call_order.append(("enabled", v))

        # A plain object with ONLY the two attributes this test is about -
        # load_settings()'s hasattr(sp, ...) checks for everything else
        # (sticky_note, cb_clipboard, wake_combo, ...) correctly read as
        # absent, unlike they would on an auto-attribute mock.Mock().
        panel = types.SimpleNamespace(remote_port_spin=_Spin(), cb_remote_dashboard=_CheckBox())
        win.settings_panel = panel
        data = {"remote_dashboard_port": 9999, "remote_dashboard_enabled": True}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(data, f)
            tmp_path = f.name
        try:
            with mock.patch.object(self.mod, "SETTINGS_FILE", tmp_path):
                win.load_settings(silent=True)
        finally:
            import os
            os.unlink(tmp_path)
        self.assertEqual(call_order[0], ("port", 9999))
        self.assertIn(("enabled", True), call_order)


if __name__ == "__main__":
    unittest.main()
