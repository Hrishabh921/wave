"""Phase 4 / idea 4 - platform-abstraction layer (automation/backends + the AppController facade).

1. Windows behaviour is pinned call-for-call against golden logs captured from the original,
   pre-Phase-4 AppController (tests/golden_windows_calls.json).
2. The Linux/macOS reference backends are checked with a fake Shell: the exact commands they would
   run. They have NOT been executed on a real Linux/macOS desktop.
"""
import contextlib
import json
import os
import unittest
from unittest import mock

import _phase4_support as S
import _ac_scenarios as sc

from automation import audit as audit_mod
from automation import backends
from automation.audit import Audit, MemoryStore
from automation.backends.base import NotSupported, PlatformBackend, Shell, UnsupportedBackend
from automation.backends.linux import LinuxBackend
from automation.backends.macos import MacBackend
from automation.backends.windows import WindowsBackend
from settings.config import config, resolve_dirs

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_windows_calls.json")


class FakeShell(Shell):
    def __init__(self, installed=(), results=None):
        self.installed, self.results, self.calls = set(installed), results or {}, []

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.installed else None

    def run(self, argv, timeout=10):
        self.calls.append(("run", list(argv)))
        return self.results.get(tuple(argv), (0, ""))

    def spawn(self, argv, **kwargs):
        self.calls.append(("spawn", list(argv)))


class WindowsGoldenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.audit = Audit(store=MemoryStore())
        # Never touch the real data folder or the real trash while testing.
        import automation.undo as undo_mod
        from automation.undo import TrashStore, UndoManager
        trash = TrashStore(os.path.join(self.tmp, "trash"))
        saved_audit, saved_dir = audit_mod.audit, config.DATA_DIR
        saved_undo = (undo_mod.trash, undo_mod.undo_manager)
        audit_mod.audit, config.DATA_DIR = self.audit, self.tmp
        undo_mod.trash, undo_mod.undo_manager = trash, UndoManager(self.audit, trash)
        backends.set_backend(WindowsBackend())

        def restore():
            audit_mod.audit, config.DATA_DIR = saved_audit, saved_dir
            undo_mod.trash, undo_mod.undo_manager = saved_undo
            backends.set_backend(None)
        self.addCleanup(restore)

    def test_every_scenario_matches_the_original_call_for_call(self):
        from automation.app_control import AppController

        @contextlib.contextmanager
        def focus(seq):
            it = iter(seq)
            with mock.patch.object(WindowsBackend, "_focus_whatsapp_window", lambda self: next(it, False)):
                yield
        got = sc.normalise(sc.run_all(AppController, focus, self.tmp), [self.tmp])
        with open(GOLDEN, encoding="utf-8") as fh:
            golden = json.load(fh)
        self.assertEqual(sorted(got), sorted(golden))
        for name in golden:
            with self.subTest(scenario=name):
                self.assertEqual(got[name], golden[name])

    def test_actions_are_audited_and_screenshots_are_undoable(self):
        from automation.app_control import AppController
        from automation.undo import TrashStore, UndoManager
        import automation.undo as undo_mod
        trash = TrashStore(os.path.join(self.tmp, "trash"))
        um = UndoManager(self.audit, trash)
        saved = (undo_mod.trash, undo_mod.undo_manager)
        undo_mod.trash, undo_mod.undo_manager = trash, um
        self.addCleanup(lambda: setattr(undo_mod, "trash", saved[0]) or setattr(undo_mod, "undo_manager", saved[1]))
        shots = os.path.join(self.tmp, "screenshots")
        os.makedirs(shots)
        rec = sc.Recorder()
        with sc.instrumented(rec):
            import pyautogui
            real_shot = pyautogui.screenshot
            def fake_shot(path, *a, **k):                                                 # make a real file
                with open(path, "wb") as fh:
                    fh.write(b"PNG")
            pyautogui.screenshot = fake_shot
            try:
                AppController.take_screenshot()
            finally:
                pyautogui.screenshot = real_shot
        made = os.listdir(shots)
        self.assertEqual(len(made), 1)
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.undo_kind), ("take_screenshot", "remove_created_file"))
        self.assertTrue(um.undo(row.id).ok)
        self.assertEqual(os.listdir(shots), [])
        with sc.instrumented(sc.Recorder()):
            AppController.open_app("youtube")
            AppController.close_app("ghost")
        self.assertEqual([(r.tool, r.status) for r in self.audit.recent(2)],
                         [("close_app", "failed"), ("open_app", "ok")])

    def test_deleted_screenshot_comes_back(self):
        from automation.app_control import AppController
        from automation.undo import TrashStore, UndoManager
        import automation.undo as undo_mod
        trash = TrashStore(os.path.join(self.tmp, "trash"))
        um = UndoManager(self.audit, trash)
        saved = (undo_mod.trash, undo_mod.undo_manager)
        undo_mod.trash, undo_mod.undo_manager = trash, um
        self.addCleanup(lambda: setattr(undo_mod, "trash", saved[0]) or setattr(undo_mod, "undo_manager", saved[1]))
        shots = os.path.join(self.tmp, "screenshots")
        os.makedirs(shots)
        with open(os.path.join(shots, "Wave_20260101_000001.png"), "wb") as fh:
            fh.write(b"PNG")
        msg = AppController.delete_latest_screenshot()
        self.assertIn("undo", msg)
        self.assertEqual(os.listdir(shots), [])
        self.assertTrue(um.undo_last().ok)
        self.assertEqual(os.listdir(shots), ["Wave_20260101_000001.png"])

    def test_whatsapp_message_text_is_not_stored_in_the_audit_log(self):
        from automation.app_control import AppController
        with sc.instrumented(sc.Recorder()), mock.patch.object(WindowsBackend, "_focus_whatsapp_window", lambda s: False):
            AppController.send_whatsapp_message("Riya", "my secret plan")
        row = self.audit.recent(1)[0]
        self.assertEqual(row.args["contact_name"], "Riya")
        self.assertNotIn("secret", json.dumps(row.args))

    def test_windows_extras_added_for_the_files_page(self):
        b = WindowsBackend()
        rec = sc.Recorder()
        with sc.instrumented(rec, which_map={"wt": "C:/wt.exe", "code": "C:/code.cmd"}):
            self.assertIn("terminal", b.open_terminal_at("C:/proj"))
            self.assertIn("VS Code", b.open_in_editor("C:/proj"))
            b.reveal_path("C:/proj/a.txt")
        calls = [c for c in rec.log if c[0] == "Popen"]
        self.assertEqual(calls[0][1][0], ["wt", "-d", "C:/proj"])
        self.assertEqual(calls[1][1][0], ["C:/code.cmd", "C:/proj"])
        self.assertEqual(calls[2][1][0][:2], ["explorer", "/select,"])
        with sc.instrumented(sc.Recorder()):                      # no `code` on PATH -> a helpful sentence, not a crash
            self.assertIn("wasn't found on PATH", b.open_in_editor("C:/proj"))


class BackendSelectionTests(unittest.TestCase):
    def tearDown(self):
        backends.set_backend(None)

    def test_detects_by_platform_and_can_be_overridden(self):
        for system, cls in (("Windows", WindowsBackend), ("Darwin", MacBackend), ("Linux", LinuxBackend)):
            backends.set_backend(None)
            with mock.patch("platform.system", return_value=system):
                self.assertIsInstance(backends.get_backend(), cls)
        backends.set_backend(None)
        with mock.patch("platform.system", return_value="Plan9"):
            b = backends.get_backend()
        self.assertIsInstance(b, UnsupportedBackend)
        self.assertEqual(b.name, "plan9")

    def test_unsupported_features_become_friendly_sentences_not_crashes(self):
        from automation.app_control import AppController
        backends.set_backend(UnsupportedBackend("plan9"))
        with mock.patch.object(audit_mod, "audit", Audit(store=MemoryStore())):
            self.assertEqual(AppController.lock_pc(), "Locking the screen isn't supported on plan9 yet.")
            self.assertIn("isn't supported on plan9", AppController.open_app("notepad"))
            self.assertIn("isn't supported on plan9", AppController.toggle_hardware("wifi"))
            self.assertIn("Couldn't open a terminal", AppController.open_terminal_at("/x"))
            self.assertIsNotNone(AppController.open_path("/x"))

    def test_capabilities_are_honest(self):
        win, lin, mac = WindowsBackend().capabilities(), LinuxBackend().capabilities(), MacBackend().capabilities()
        self.assertTrue(all(win.values()), [k for k, v in win.items() if not v])
        self.assertFalse(lin["type_in_text_editor"])
        self.assertFalse(mac["toggle_radio"])
        self.assertTrue(mac["open_image_viewer"])              # reuses open_path
        self.assertFalse(any(UnsupportedBackend("x").capabilities()[f] for f in ("launch_app", "lock_screen", "open_path")))

    def test_every_backend_implements_the_whole_interface_surface(self):
        for cls in (WindowsBackend, LinuxBackend, MacBackend):
            for feature in PlatformBackend.FEATURES:
                self.assertTrue(callable(getattr(cls, feature)), f"{cls.__name__}.{feature}")


class LinuxBackendTests(unittest.TestCase):
    def test_launch_lock_open_and_terminal(self):
        sh = FakeShell(installed={"firefox", "gtk-launch", "loginctl", "xdg-open", "gnome-terminal", "code"})
        b = LinuxBackend(sh)
        self.assertEqual(b.launch_app("Firefox".lower()), "Successfully opened firefox.")
        self.assertEqual(sh.calls[-1], ("spawn", ["/usr/bin/firefox"]))
        self.assertIn("couldn't find", LinuxBackend(FakeShell()).launch_app("nonexistent thing"))
        self.assertEqual(b.lock_screen(), "Locking your screen now.")
        self.assertEqual(sh.calls[-1], ("run", ["loginctl", "lock-session"]))
        b.open_path("/home/u/a.pdf")
        self.assertEqual(sh.calls[-1], ("spawn", ["xdg-open", "/home/u/a.pdf"]))
        self.assertIn("Opened a terminal", b.open_terminal_at("/home/u/proj"))
        self.assertEqual(sh.calls[-1][1], ["/usr/bin/gnome-terminal", "--working-directory=/home/u/proj"])
        self.assertIn("VS Code", b.open_in_editor("/home/u/proj"))

    def test_lock_without_any_tool_is_not_supported(self):
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).lock_screen()

    # ---- close_window / close_tab (Tier 5's DISPLAY-KeyError fix) ----
    # Previously untested at all - added in Tier 9 after confirming, against
    # a real (virtual) X server, that these behave correctly; see
    # docs/MANUAL_TEST_CHECKLIST.md for how that was verified.
    def test_close_window_sends_alt_f4_when_a_display_is_present(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch("pyautogui.hotkey") as hotkey:
            self.assertIsNone(LinuxBackend(FakeShell()).close_window())
        hotkey.assert_called_once_with("alt", "f4")

    def test_close_tab_sends_ctrl_w_when_a_display_is_present(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch("pyautogui.hotkey") as hotkey:
            self.assertIsNone(LinuxBackend(FakeShell()).close_tab())
        hotkey.assert_called_once_with("ctrl", "w")

    def test_close_window_and_close_tab_without_a_display_are_not_supported(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ""}):
            with self.assertRaises(NotSupported):
                LinuxBackend(FakeShell()).close_window()
            with self.assertRaises(NotSupported):
                LinuxBackend(FakeShell()).close_tab()

    def test_close_window_wraps_an_ordinary_pyautogui_failure(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch("pyautogui.hotkey", side_effect=RuntimeError("no accessible X display")):
            with self.assertRaises(NotSupported):
                LinuxBackend(FakeShell()).close_window()

    def test_close_window_and_close_tab_survive_pyautoguis_own_sys_exit(self):
        """Real, confirmed finding (Tier 9): `import pyautogui` itself calls
        sys.exit(...) - not a normal exception - when its optional mouseinfo
        dependency can't find tkinter, which a real Linux box has no
        guarantee of having installed. SystemExit deliberately isn't an
        Exception subclass, so it used to blow straight past the `except
        Exception` below and take the whole app down with it, instead of
        the same friendly NotSupported every other missing-tool case gets.
        hotkey() is mocked as the seam (same as elsewhere in this file)
        since the import itself only raises once per process and can't be
        re-triggered on demand once pyautogui is already cached."""
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch("pyautogui.hotkey", side_effect=SystemExit("no tkinter")):
            with self.assertRaises(NotSupported):
                LinuxBackend(FakeShell()).close_window()
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
             mock.patch("pyautogui.hotkey", side_effect=SystemExit("no tkinter")):
            with self.assertRaises(NotSupported):
                LinuxBackend(FakeShell()).close_tab()

    def test_wifi_toggle_reads_back_the_real_state(self):
        sh = FakeShell(installed={"nmcli"}, results={("nmcli", "radio", "wifi"): (0, "disabled")})
        out = LinuxBackend(sh).toggle_radio("turn wifi off")
        self.assertEqual(out, "Wi-Fi is now off.")
        self.assertIn(("run", ["nmcli", "radio", "wifi", "off"]), sh.calls)
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).toggle_radio("wifi on")
        with self.assertRaises(NotSupported):                       # no blind toggle for Bluetooth
            LinuxBackend(FakeShell(installed={"rfkill"})).toggle_radio("bluetooth")

    def test_browser_lookup_and_desktop_dir(self):
        b = LinuxBackend(FakeShell(installed={"brave-browser", "xdg-user-dir"},
                                   results={("xdg-user-dir", "DESKTOP"): (0, "/home/u/Schreibtisch")}))
        self.assertEqual(b.find_browser("Brave"), "/usr/bin/brave-browser")
        self.assertIsNone(b.find_browser("edge"))
        self.assertEqual(b.desktop_dir(), "/home/u/Schreibtisch")

    def test_whatsapp_falls_back_to_web_and_says_so(self):
        with mock.patch("webbrowser.open") as opened:
            msg = LinuxBackend(FakeShell()).send_whatsapp_message("Riya", "hi")
        opened.assert_called_once_with("https://web.whatsapp.com")
        self.assertIn("send 'hi' to Riya yourself", msg)

    def test_settings_pages_tier5_expansion(self):
        """Every key _GNOME_PANELS actually has, verified straight from the
        module's own dict (not a hand-copied duplicate list here) so this
        keeps covering the real set if it's ever extended again."""
        from automation.backends.linux import _GNOME_PANELS
        for key, panel in _GNOME_PANELS.items():
            with self.subTest(key=key):
                sh = FakeShell(installed={"gnome-control-center"})
                msg = LinuxBackend(sh).open_system_setting(key)
                self.assertIn(key.capitalize(), msg)
                expected = ["gnome-control-center"] + ([panel] if panel else [])
                self.assertEqual(sh.calls[-1], ("spawn", expected))

    def test_service_start_stop(self):
        """Tier 8: systemctl start/stop, gated on systemctl actually being
        on PATH (NotSupported otherwise) - same shape as toggle_radio's own
        nmcli/rfkill checks above."""
        sh = FakeShell(installed={"systemctl"},
                       results={("systemctl", "start", "nginx"): (0, ""),
                                ("systemctl", "stop", "nginx"): (0, "")})
        b = LinuxBackend(sh)
        self.assertEqual(b.start_service("nginx"), "Successfully started the 'nginx' service.")
        self.assertEqual(sh.calls[-1], ("run", ["systemctl", "start", "nginx"]))
        self.assertEqual(b.stop_service("nginx"), "Successfully stopped the 'nginx' service.")
        self.assertEqual(sh.calls[-1], ("run", ["systemctl", "stop", "nginx"]))

    def test_service_failure_is_a_plain_reply_not_a_crash(self):
        sh = FakeShell(installed={"systemctl"}, results={("systemctl", "start", "ghost"): (1, "")})
        self.assertIn("Couldn't start", LinuxBackend(sh).start_service("ghost"))

    def test_service_control_without_systemd_is_not_supported(self):
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).start_service("nginx")
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).stop_service("nginx")

    def test_window_list_focus_minimize(self):
        sh = FakeShell(installed={"wmctrl"},
                       results={("wmctrl", "-l"): (0, "0x01 0 host Notepad - untitled\n0x02 0 host Firefox"),
                                ("wmctrl", "-a", "Firefox"): (0, ""),
                                ("wmctrl", "-r", "Firefox", "-b", "add,hidden"): (0, "")})
        b = LinuxBackend(sh)
        self.assertEqual(b.list_open_windows(), ["Notepad - untitled", "Firefox"])
        self.assertEqual(b.focus_window("Firefox"), "Switched to the window matching 'Firefox'.")
        self.assertEqual(b.minimize_window("Firefox"), "Minimized the window matching 'Firefox'.")

    def test_window_management_without_wmctrl_is_not_supported(self):
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).list_open_windows()
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).focus_window("x")
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).minimize_window("x")

    def test_window_not_found_is_a_plain_reply_not_an_error(self):
        sh = FakeShell(installed={"wmctrl"}, results={("wmctrl", "-a", "ghost"): (1, "")})
        self.assertIn("couldn't find", LinuxBackend(sh).focus_window("ghost"))

    def test_snap_window_left_and_right_halves(self):
        sh = FakeShell(installed={"xdotool"},
                       results={("xdotool", "search", "--name", "Firefox"): (0, "12345"),
                                ("xdotool", "getdisplaygeometry"): (0, "1920 1080")})
        b = LinuxBackend(sh)
        self.assertEqual(b.snap_window("Firefox", "left"), "Snapped the window matching 'Firefox' to left.")
        self.assertIn(("run", ["xdotool", "windowsize", "12345", "960", "1080"]), sh.calls)
        self.assertIn(("run", ["xdotool", "windowmove", "12345", "0", "0"]), sh.calls)
        sh.calls.clear()
        self.assertEqual(b.snap_window("Firefox", "right"), "Snapped the window matching 'Firefox' to right.")
        self.assertIn(("run", ["xdotool", "windowmove", "12345", "960", "0"]), sh.calls)

    def test_snap_window_rejects_bad_position_before_any_lookup(self):
        sh = FakeShell(installed={"xdotool"})
        self.assertIn("isn't a position", LinuxBackend(sh).snap_window("Firefox", "sideways"))
        self.assertEqual(sh.calls, [])

    def test_snap_window_without_xdotool_is_not_supported(self):
        with self.assertRaises(NotSupported):
            LinuxBackend(FakeShell()).snap_window("Firefox", "left")

    def test_snap_window_no_match_is_a_plain_reply(self):
        sh = FakeShell(installed={"xdotool"}, results={("xdotool", "search", "--name", "ghost"): (0, "")})
        self.assertIn("couldn't find", LinuxBackend(sh).snap_window("ghost", "left"))


class _ScriptedShell(Shell):
    """For MacBackend's per-window management methods below: each real
    osascript call carries a long, exact AppleScript string, so exact-argv
    keying (what FakeShell does for the other, short commands elsewhere in
    this file) would be brittle - a harmless comment or whitespace tweak
    in the script would silently stop matching and fall through to
    FakeShell's default (0, ""), which could make a test pass for the
    wrong reason instead of erroring. This instead returns canned
    (returncode, stdout) responses IN CALL ORDER, while still recording
    the full argv (including the script text) so a test can assert on
    specific CONTENT inside it (e.g. that a window name was escaped)
    without being coupled to the whole script verbatim."""

    def __init__(self, canned=()):
        self.canned = list(canned)
        self.calls = []

    def run(self, argv, timeout=10):
        self.calls.append(list(argv))
        return self.canned.pop(0) if self.canned else (0, "")

    def which(self, name):
        return f"/usr/bin/{name}"

    def spawn(self, argv, **kwargs):
        self.calls.append(list(argv))


class MacWindowManagementTests(unittest.TestCase):
    def test_list_open_windows(self):
        sh = _ScriptedShell(canned=[(0, "Notepad\nFinder\n")])
        b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
        self.assertEqual(b.list_open_windows(), ["Notepad", "Finder"])
        self.assertEqual(sh.calls[-1][:2], ["osascript", "-e"])

    def test_list_open_windows_needs_accessibility(self):
        sh = _ScriptedShell(canned=[(1, "")])
        with self.assertRaises(NotSupported):
            MacBackend(sh, applications_dir=S.make_tmpdir(self)).list_open_windows()

    def test_focus_window_found_and_not_found(self):
        sh = _ScriptedShell(canned=[(0, "ok\n"), (0, "not found\n")])
        b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
        self.assertIn("Switched to", b.focus_window("Finder"))
        self.assertIn("couldn't find", b.focus_window("ghost"))

    def test_focus_window_escapes_an_embedded_quote_in_the_name(self):
        sh = _ScriptedShell(canned=[(0, "ok\n")])
        MacBackend(sh, applications_dir=S.make_tmpdir(self)).focus_window('Weird "Name"')
        script = sh.calls[-1][2]
        self.assertIn('Weird \\"Name\\"', script)   # safely escaped, not a raw embedded quote

    def test_minimize_window(self):
        sh = _ScriptedShell(canned=[(0, "ok\n")])
        self.assertIn("Minimized", MacBackend(sh, applications_dir=S.make_tmpdir(self)).minimize_window("Finder"))

    def test_focus_and_minimize_need_accessibility(self):
        for method, arg in (("focus_window", "Finder"), ("minimize_window", "Finder")):
            with self.subTest(method=method):
                sh = _ScriptedShell(canned=[(1, "")])
                b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
                with self.assertRaises(NotSupported):
                    getattr(b, method)(arg)

    def test_snap_window_rejects_bad_position_before_any_osascript_call(self):
        sh = _ScriptedShell()
        self.assertIn("isn't a position", MacBackend(sh, applications_dir=S.make_tmpdir(self)).snap_window(
            "Finder", "sideways"))
        self.assertEqual(sh.calls, [])

    def test_snap_window_left_right_maximize(self):
        for position in ("left", "right", "maximize"):
            with self.subTest(position=position):
                sh = _ScriptedShell(canned=[(0, "ok\n")])
                b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
                msg = b.snap_window("Finder", position)
                self.assertIn(f"to {position}", msg)
                self.assertIn("screenBounds", sh.calls[-1][2])

    def test_snap_window_needs_accessibility(self):
        sh = _ScriptedShell(canned=[(1, "")])
        with self.assertRaises(NotSupported):
            MacBackend(sh, applications_dir=S.make_tmpdir(self)).snap_window("Finder", "left")


class MacBackendTests(unittest.TestCase):
    def test_commands(self):
        sh = FakeShell(results={("open", "-a", "Nope"): (1, "")})
        b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
        self.assertEqual(b.launch_app("Spotify"), "Successfully opened Spotify.")
        self.assertEqual(sh.calls[-1], ("run", ["open", "-a", "Spotify"]))
        self.assertIn("couldn't find", b.launch_app("Nope"))
        b.reveal_path("/Users/u/a.txt")
        self.assertEqual(sh.calls[-1], ("spawn", ["open", "-R", "/Users/u/a.txt"]))
        b.open_terminal_at("/Users/u/p")
        self.assertEqual(sh.calls[-1], ("spawn", ["open", "-a", "Terminal", "/Users/u/p"]))
        b.lock_screen()
        self.assertEqual(sh.calls[-1], ("run", ["pmset", "displaysleepnow"]))
        with self.assertRaises(NotSupported):
            b.toggle_radio("wifi")

    def test_browser_is_only_offered_when_the_app_is_installed(self):
        apps = S.make_tmpdir(self)
        b = MacBackend(FakeShell(), applications_dir=apps)
        self.assertIsNone(b.find_browser("brave"))
        os.makedirs(os.path.join(apps, "Brave Browser.app"))
        self.assertEqual(b.find_browser("brave"), "Brave Browser")
        b.launch_in_browser("Brave Browser", "https://x.org")
        self.assertEqual(b.sh.calls[-1], ("spawn", ["open", "-a", "Brave Browser", "https://x.org"]))

    def test_settings_pages_tier5_expansion(self):
        """Every key _PANES actually has, verified straight from the
        module's own dict so this keeps covering the real set if it's ever
        extended again."""
        from automation.backends.macos import _PANES
        for key, pane in _PANES.items():
            with self.subTest(key=key):
                sh = FakeShell()
                msg = MacBackend(sh, applications_dir=S.make_tmpdir(self)).open_system_setting(key)
                self.assertIn(key.capitalize(), msg)
                self.assertEqual(sh.calls[-1], ("spawn", ["open", f"x-apple.systempreferences:{pane}"]))

    def test_service_start_stop(self):
        """Tier 8: the legacy launchctl start/stop subcommands, addressed
        by launchd label (see macos.py's own comment on why there's no
        casual-name lookup table the way _PANES provides for settings)."""
        sh = FakeShell(results={("launchctl", "start", "com.acme.worker"): (0, ""),
                                ("launchctl", "stop", "com.acme.worker"): (0, "")})
        b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
        self.assertEqual(b.start_service("com.acme.worker"), "Successfully started the 'com.acme.worker' service.")
        self.assertEqual(sh.calls[-1], ("run", ["launchctl", "start", "com.acme.worker"]))
        self.assertEqual(b.stop_service("com.acme.worker"), "Successfully stopped the 'com.acme.worker' service.")
        self.assertEqual(sh.calls[-1], ("run", ["launchctl", "stop", "com.acme.worker"]))

    def test_service_failure_mentions_the_launchd_label_convention(self):
        sh = FakeShell(results={("launchctl", "start", "bluetooth"): (1, "")})
        b = MacBackend(sh, applications_dir=S.make_tmpdir(self))
        msg = b.start_service("bluetooth")
        self.assertIn("Couldn't start", msg)
        self.assertIn("launchd label", msg)


class WindowsSettingsPagesTests(unittest.TestCase):
    """Tier 5: WindowsBackend.SETTINGS_MAP grew from 5 to 16 entries - this
    pins every one of them against the module's own dict (rather than a
    hand-copied list) so it keeps covering the real set if it's extended
    again, plus one targeted check that battery/power - easy to confuse,
    since both are plausible answers to "power settings" - actually open
    two DIFFERENT pages rather than one silently aliasing the other."""

    def test_tier5_expansion(self):
        b = WindowsBackend()
        for key, command in WindowsBackend.SETTINGS_MAP.items():
            with self.subTest(key=key):
                with mock.patch("os.system") as mocked:
                    msg = b.open_system_setting(key)
                mocked.assert_called_once_with(f"start {command}")
                self.assertIn(key.capitalize(), msg)

    def test_battery_and_power_are_genuinely_different_pages(self):
        b = WindowsBackend()
        with mock.patch("os.system") as mocked:
            b.open_system_setting("battery")
        mocked.assert_called_once_with("start ms-settings:batterysaver")
        with mock.patch("os.system") as mocked:
            b.open_system_setting("power")
        mocked.assert_called_once_with("start ms-settings:powersleep")


class WindowsServiceTests(unittest.TestCase):
    """Tier 8: sc.exe start/stop. windows.py has no Shell abstraction (see
    its own module docstring) so, same as WindowsSettingsPagesTests above
    mocking os.system directly, these mock subprocess.run directly rather
    than a FakeShell."""

    def test_start_and_stop_call_sc_exe_with_the_exact_verb(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(b.start_service("wuauserv"), "Successfully started the 'wuauserv' service.")
            mocked.assert_called_once_with(["sc", "start", "wuauserv"], capture_output=True, text=True, timeout=15)
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            self.assertEqual(b.stop_service("wuauserv"), "Successfully stopped the 'wuauserv' service.")
            mocked.assert_called_once_with(["sc", "stop", "wuauserv"], capture_output=True, text=True, timeout=15)

    def test_sc_exes_own_failure_text_is_surfaced_verbatim(self):
        b = WindowsBackend()
        with mock.patch("subprocess.run") as mocked:
            mocked.return_value = mock.Mock(
                returncode=1060,
                stdout="FAILED 1060: The specified service does not exist as an installed service.",
                stderr="")
            msg = b.start_service("ghost")
        self.assertIn("Couldn't start", msg)
        self.assertIn("FAILED 1060", msg)

    def test_a_timeout_is_a_plain_reply_not_a_crash(self):
        import subprocess
        b = WindowsBackend()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="sc", timeout=15)):
            msg = b.stop_service("stuckservice")
        self.assertIn("timed out", msg)


class _FakeWin32:
    """Stands in for BOTH win32gui and win32con (pywin32 - Windows-only,
    not installed in this sandbox, so HAS_WIN32GUI is False here without
    this): a tiny registry of (hwnd, title, visible, iconic) windows plus
    a call log, the same role FakeShell plays for the Linux/macOS
    backends. Reused as the win32con stand-in too since the only thing
    these methods need from win32con are the two SW_* constants, which
    are plain class attributes here - see WindowsWindowManagementTests._patched."""
    SW_RESTORE = 9
    SW_MINIMIZE = 6

    def __init__(self, windows=()):
        self._windows = {w[0]: {"title": w[1], "visible": w[2], "iconic": w[3]} for w in windows}
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

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("SetForegroundWindow", hwnd))


class WindowsWindowManagementTests(unittest.TestCase):
    """Tier 8. Every test here patches win32gui/win32con/HAS_WIN32GUI
    directly on the module (rather than via sys.modules + a reload),
    scoped to the one test, so nothing leaks into any other test in this
    file - the existing WhatsApp-focused tests never exercise this code
    path at all (they mock _focus_whatsapp_window itself, a level above),
    so HAS_WIN32GUI being False for them, as it genuinely is in this
    sandbox, was never load-bearing for those."""

    def _patched(self, fake):
        from automation.backends import windows as windows_mod
        # create=True: win32gui/win32con were never bound as module
        # attributes at all (the real `import win32gui` failed, so the
        # names don't exist here to patch over) - this adds them for the
        # duration of the test and removes them again afterward, rather
        # than requiring they pre-exist the way mock.patch normally does.
        return mock.patch.multiple(windows_mod, win32gui=fake, win32con=_FakeWin32, HAS_WIN32GUI=True, create=True)

    def test_list_open_windows_only_includes_visible_titled_windows(self):
        fake = _FakeWin32(windows=[(1, "Notepad", True, False), (2, "", True, False), (3, "Hidden", False, False)])
        with self._patched(fake):
            self.assertEqual(WindowsBackend().list_open_windows(), ["Notepad"])

    def test_list_open_windows_without_pywin32_is_not_supported(self):
        with self.assertRaises(NotSupported):
            WindowsBackend().list_open_windows()

    def test_focus_window_restores_if_minimized_then_raises_it(self):
        fake = _FakeWin32(windows=[(5, "Spotify - now playing", True, True)])
        with self._patched(fake):
            msg = WindowsBackend().focus_window("spotify")
        self.assertIn("Switched to", msg)
        self.assertEqual(fake.calls, [("ShowWindow", 5, _FakeWin32.SW_RESTORE), ("SetForegroundWindow", 5)])

    def test_focus_window_skips_restore_when_not_minimized(self):
        fake = _FakeWin32(windows=[(5, "Spotify", True, False)])
        with self._patched(fake):
            WindowsBackend().focus_window("spotify")
        self.assertEqual(fake.calls, [("SetForegroundWindow", 5)])

    def test_focus_window_no_match_is_a_plain_reply(self):
        fake = _FakeWin32(windows=[(5, "Spotify", True, False)])
        with self._patched(fake):
            msg = WindowsBackend().focus_window("ghost")
        self.assertIn("couldn't find", msg)
        self.assertEqual(fake.calls, [])

    def test_minimize_window(self):
        fake = _FakeWin32(windows=[(7, "Notepad - untitled", True, False)])
        with self._patched(fake):
            msg = WindowsBackend().minimize_window("notepad")
        self.assertIn("Minimized", msg)
        self.assertEqual(fake.calls, [("ShowWindow", 7, _FakeWin32.SW_MINIMIZE)])

    def test_snap_window_rejects_an_unrecognized_position_before_any_lookup(self):
        fake = _FakeWin32(windows=[(5, "Spotify", True, False)])
        with self._patched(fake):
            msg = WindowsBackend().snap_window("spotify", "sideways")
        self.assertIn("isn't a position I recognize", msg)
        self.assertEqual(fake.calls, [])

    def test_snap_window_sends_the_matching_win_arrow_combo(self):
        for position, key in (("left", "left"), ("right", "right"), ("maximize", "up")):
            with self.subTest(position=position):
                fake = _FakeWin32(windows=[(5, "Spotify", True, False)])
                with self._patched(fake), mock.patch("time.sleep"), mock.patch("pyautogui.hotkey") as hotkey:
                    msg = WindowsBackend().snap_window("spotify", position)
                self.assertIn(f"to {position}", msg)
                hotkey.assert_called_once_with("win", key)
                self.assertEqual(fake.calls, [("SetForegroundWindow", 5)])


class FrozenPathTests(unittest.TestCase):
    ENV = {}

    def test_running_from_source_changes_nothing(self):
        self.assertEqual(resolve_dirs(False, None, "/usr/bin/python3", {}, "/proj"), ("/proj", "/proj"))

    def test_onefile_build_never_writes_into_the_temporary_bundle(self):
        app, data = resolve_dirs(True, "/tmp/_MEI123", "C:/Apps/Wave/Wave.exe", {}, "/proj", is_writable=lambda p: True)
        self.assertEqual(app, "/tmp/_MEI123")
        self.assertEqual(data, os.path.dirname(os.path.abspath("C:/Apps/Wave/Wave.exe")))

    def test_readonly_install_dir_falls_back_to_localappdata(self):
        app, data = resolve_dirs(True, None, "C:/Program Files/Wave/Wave.exe", {"LOCALAPPDATA": "C:/Users/u/AppData/Local"},
                                 "/proj", is_writable=lambda p: False)
        self.assertEqual(data, os.path.join("C:/Users/u/AppData/Local", "WaveAI"))

    def test_env_override_wins_in_both_modes(self):
        env = {"WAVE_DATA_DIR": "/data/wave"}
        self.assertEqual(resolve_dirs(True, "/m", "/x/Wave.exe", env, "/proj")[1], os.path.abspath("/data/wave"))
        self.assertEqual(resolve_dirs(False, None, "py", env, "/proj")[1], os.path.abspath("/data/wave"))

    def test_config_exposes_the_split_and_derives_everything_writable_from_data_dir(self):
        self.assertEqual(config.BASE_DIR, config.APP_DIR)
        for p in (config.DB_DIR, config.TRASH_DIR, config.USER_SKILLS_DIR):
            self.assertTrue(p.startswith(config.DATA_DIR) or os.getenv("WAVE_USER_SKILLS_DIR"), p)


if __name__ == "__main__":
    unittest.main()
