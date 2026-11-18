"""
tests/test_tier26_web_ui_parity.py — the web UI rebuilt to match the PyQt6 UI.

The page (webui/static) now mirrors ui/main_window.py page for page, and
webui/bridge_parity.py's ParityMixin gives it the backend calls the desktop
widgets make. These tests cover that mixin (and the bridge changes that came
with it) the same way test_tier24_web_ui.py covers the rest of the bridge:
a fake brain, a fake window capturing pushed events, and a throw-away DB.

Nothing here opens a real window, microphone, file dialog or speaker, and
nothing calls the power/shutdown command (only its rejection path).

    python -m unittest discover -s tests -p "test_tier26_web_ui_parity.py"
"""
import json
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db                      # noqa: E402
from automation import security_log                      # noqa: E402
from settings.config import config                       # noqa: E402
import webui.bridge as bridge_module                       # noqa: E402
import webui.bridge_parity as parity                       # noqa: E402
from webui.bridge import WaveWebBridge                    # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "webui", "static")


def _live_audit():
    """The action log the bridge itself reads. bridge_parity looks
    automation.audit.audit up on every call, and other test files in the
    same run swap that module attribute, so a reference taken when this file
    was imported can point at a log the bridge never reads."""
    return parity.audit_mod.audit


class _FakeBrain:
    def __init__(self, reply="hi there"):
        self.reply = reply
        self.pending_action = None
        self.dispatched = []
        self.reminders = []
        self.confirm_pending = False

    def think_stream(self, text, attachment=None, cancel_event=None):
        for piece in self.reply.split(" "):
            yield piece + " "

    def check_provider(self):
        return (True, "fake-model", 12)

    def provider_info(self):
        return {"provider": "fake", "model": "m1", "endpoint": "local", "key": "…1234",
                "configured": True, "problem": ""}

    def _dispatch_tool(self, name, args, source="ai"):
        self.dispatched.append((name, args, source))
        return f"ran {name} {args.get('name')}"

    def _set_reminder(self, message, when_text, recurrence="none"):
        self.reminders.append((message, when_text, recurrence))
        return f"Okay, I'll remind you to {message}."

    def cancel_pending_confirmation(self):
        was = self.confirm_pending
        self.confirm_pending = False
        return was


class _FakeWindow:
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)

    def events_named(self, name):
        return [c for c in self.calls if f'"{name}"' in c]


class _Base(unittest.TestCase):
    def setUp(self):
        self.old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", self.old_path)
        audit = _live_audit()
        audit.clear()
        security_log.clear()
        self.addCleanup(audit.clear)
        self.addCleanup(security_log.clear)
        self.settings_file = os.path.join(tmp, "wave_settings.json")
        old_settings = bridge_module.SETTINGS_FILE
        bridge_module.SETTINGS_FILE = self.settings_file
        self.addCleanup(setattr, bridge_module, "SETTINGS_FILE", old_settings)
        self.brain = _FakeBrain()
        self.bridge = WaveWebBridge(brain=self.brain)
        self.addCleanup(self._stop_reminders)
        self.window = _FakeWindow()
        self.bridge.set_window(self.window)
        self.tmp = tmp

    def _stop_reminders(self):
        self.bridge._reminder_worker.stop()
        self.bridge._reminder_worker.join(timeout=2)


# ---------------------------------------------------------------------------
class BootstrapTests(_Base):
    def test_first_run_is_reported_when_there_is_no_settings_file(self):
        boot = self.bridge.get_bootstrap()
        self.assertTrue(boot["ok"])
        self.assertTrue(boot["first_run"])
        self.assertEqual(boot["last_theme"], "Black Glass")
        for key in ("winsdk", "pycaw", "sbc", "pyperclip", "pypdf", "qrcode", "piper", "keyboard"):
            self.assertIn(key, boot["capabilities"])

    def test_saved_theme_and_custom_themes_come_back(self):
        self.bridge.save_settings({"last_theme": "Midnight Blue"})
        themes_dir = os.path.join(self.tmp, "themes")
        os.makedirs(themes_dir)
        with open(os.path.join(themes_dir, "Mine.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "Mine", "accent_1": "#123456"}, f)
        with mock.patch.object(config, "DATA_DIR", self.tmp):
            boot = self.bridge.get_bootstrap()
        self.assertFalse(boot["first_run"])
        self.assertEqual(boot["last_theme"], "Midnight Blue")
        self.assertEqual([t["name"] for t in boot["custom_themes"]], ["Mine"])

    def test_settings_info_reports_whether_the_file_exists(self):
        self.assertFalse(self.bridge.get_settings_info()["exists"])
        self.bridge.save_settings({"notes": "x"})
        info = self.bridge.get_settings_info()
        self.assertTrue(info["exists"])
        self.assertEqual(info["settings"]["notes"], "x")


# ---------------------------------------------------------------------------
class ChatTests(_Base):
    def test_markdown_is_rendered_and_raw_html_is_escaped(self):
        html = self.bridge.render_markdown("## Hi\n\n- **bold** item\n\n<script>alert(1)</script>")["html"]
        self.assertIn("<h2>Hi</h2>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertNotIn("<script>", html)

    def test_speech_text_drops_code_blocks(self):
        text = self.bridge.speech_text("Here you go:\n```python\nprint('x')\n```\nDone.")["text"]
        self.assertNotIn("print", text)
        self.assertIn("Done", text)

    def test_clear_chat_no_longer_wipes_the_database(self):
        db.save_memory("favorite color", "blue", source="explicit")
        self.assertTrue(self.bridge.clear_chat()["ok"])
        self.assertEqual(dict(db.get_all_memory()).get("favorite color"), "blue")

    def test_a_reply_reports_when_its_speech_finished(self):
        with mock.patch.object(parity.tts_engine, "speak_stream", return_value=True):
            self.bridge.send_message("hi")
            self.bridge._reply_worker.join(timeout=5)
        self.assertEqual(len(self.window.events_named("chat_done")), 1)
        finished = self.window.events_named("reply_speech_finished")
        self.assertEqual(len(finished), 1)
        self.assertIn("true", finished[0])

    def test_open_url_only_allows_http_links(self):
        self.assertFalse(self.bridge.open_url("javascript:alert(1)")["ok"])
        self.assertFalse(self.bridge.open_url("file:///C:/Windows")["ok"])
        with mock.patch.object(parity.webbrowser, "open") as opened:
            self.assertTrue(self.bridge.open_url("https://example.com")["ok"])
        opened.assert_called_once_with("https://example.com")

    def test_file_dialogs_without_a_window_just_return_nothing(self):
        self.bridge._window = None
        self.assertIsNone(self.bridge.pick_attachment()["attachment"])
        self.assertFalse(self.bridge.export_chat("x")["saved"])
        self.assertIsNone(self.bridge.pick_file("photo")["path"])


# ---------------------------------------------------------------------------
class VoiceTests(_Base):
    class _Mic:
        def __init__(self):
            self.mode = "SLEEPING"

        def is_alive(self):
            return True

        def set_mode(self, mode, announce=True):
            self.mode = mode

    def test_toggle_listening_flips_awake_and_sleeping_like_the_desktop(self):
        self.bridge._mic_worker = self._Mic()
        self.assertEqual(self.bridge.toggle_listening()["mode"], "AWAKE")
        self.assertEqual(self.bridge.toggle_listening()["mode"], "SLEEPING")

    def test_voice_stop_cancels_a_pending_confirmation_and_says_so(self):
        self.brain.confirm_pending = True
        with mock.patch.object(parity.tts_engine, "stop"):
            self.bridge._on_voice_stop()
        events = self.window.events_named("voice_stop")
        self.assertEqual(len(events), 1)
        self.assertIn('"cancelled_confirmation": true', events[0])

    def test_emergency_stop_puts_the_mic_back_to_sleep(self):
        mic = self._Mic()
        mic.mode = "AWAKE"
        self.bridge._mic_worker = mic
        with mock.patch.object(parity.tts_engine, "stop"):
            self.assertTrue(self.bridge.emergency_stop()["ok"])
        self.assertEqual(mic.mode, "SLEEPING")


# ---------------------------------------------------------------------------
class ActionGuardTests(_Base):
    def test_only_whitelisted_actions_run(self):
        self.assertFalse(self.bridge.run_action("os_system", ["calc"])["ok"])
        with mock.patch.dict(parity.RUN_ACTIONS, {"open_app": lambda name: f"Opened {name}."}):
            r = self.bridge.run_action("open_app", ["notepad"])
        self.assertEqual(r["result"], "Opened notepad.")

    def test_only_media_keys_can_be_pressed(self):
        self.assertFalse(self.bridge.press_key("alt+f4")["ok"])
        self.assertTrue(self.bridge.press_key("playpause")["ok"])

    def test_unknown_power_commands_are_refused(self):
        with mock.patch.object(parity.os, "system") as system:
            self.assertFalse(self.bridge.power_command("format c:")["ok"])
        system.assert_not_called()


# ---------------------------------------------------------------------------
class PagesTests(_Base):
    def test_list_dir_puts_folders_first_with_desktop_style_columns(self):
        root = os.path.join(self.tmp, "browse")
        os.makedirs(os.path.join(root, "zeta folder"))
        with open(os.path.join(root, "alpha.txt"), "w") as f:
            f.write("x" * 2048)
        r = self.bridge.list_dir(root)
        self.assertEqual([e["name"] for e in r["entries"]], ["zeta folder", "alpha.txt"])
        folder, txt = r["entries"]
        self.assertEqual(folder["type"], "File Folder")
        self.assertEqual(txt["type"], "TXT File")
        self.assertEqual(txt["size_text"], "2.0 KB")

    def test_a_missing_folder_shows_the_drives_like_qfilesystemmodel(self):
        missing = os.path.join(self.tmp, "no-such-desktop")
        r = self.bridge.list_dir(missing)
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], missing)
        self.assertTrue(r["entries"])
        self.assertTrue(all(e["type"] == "Drive" for e in r["entries"]))

    def test_workflows_save_rename_and_run_through_dispatch(self):
        self.assertTrue(self.bridge.save_workflow("Morning", ["open chrome", "what's the weather"])["saved"])
        self.assertTrue(self.bridge.rename_workflow("morning", "Daily")["renamed"])
        names = [w["name"] for w in self.bridge.get_workflows()["workflows"]]
        self.assertEqual(names, ["daily"])
        r = self.bridge.run_workflow_ui("daily")
        self.assertEqual(self.brain.dispatched, [("run_workflow", {"name": "daily"}, "ui")])
        self.assertIn("run_workflow", r["result"])

    def test_memory_saved_from_the_page_is_explicit(self):
        self.assertTrue(self.bridge.save_memory("city", "Jaipur")["saved"])
        item = self.bridge.get_memory()["items"][0]
        self.assertEqual((item["key"], item["value"], item["source"]), ("city", "Jaipur", "explicit"))

    def test_todos_and_notes_by_id(self):
        self.assertTrue(self.bridge.add_todo_raw("buy milk")["added"])
        todo_id = self.bridge.get_todos()["todos"][0]["id"]
        self.assertTrue(self.bridge.complete_todo_id(todo_id)["done"])
        self.assertEqual(self.bridge.get_todos()["todos"], [])
        self.assertTrue(self.bridge.add_note_raw("call mom")["added"])
        note_id = self.bridge.get_notes()["notes"][0]["id"]
        self.assertTrue(self.bridge.delete_note_id(note_id)["deleted"])
        self.assertEqual(self.bridge.get_notes()["notes"], [])

    def test_reminder_rows_are_labelled_like_the_desktop_list(self):
        from datetime import datetime, timedelta
        due = (datetime.now() + timedelta(hours=2)).replace(second=0, microsecond=0)
        db.save_reminder("stretch", due.isoformat(), recurrence="daily")
        row = self.bridge.get_reminder_rows()["rows"][0]
        self.assertEqual(row["label"], f"stretch — {due.strftime('%I:%M %p on %b %d')} (repeats daily)")
        self.assertTrue(self.bridge.cancel_reminder_id(row["id"])["cancelled"])
        self.assertEqual(self.bridge.get_reminder_rows()["rows"], [])

    def test_add_reminder_goes_through_the_brains_parser(self):
        r = self.bridge.add_reminder("check the oven", "in 10 minutes", "none")
        self.assertEqual(self.brain.reminders, [("check the oven", "in 10 minutes", "none")])
        self.assertIn("check the oven", r["result"])

    def test_activity_includes_the_trash_note(self):
        with _live_audit().action("create_folder", {"folder_name": "tier26-x"}, source="ui") as act:
            act.ok("Created tier26-x")
        r = self.bridge.get_activity()
        mine = [a for a in r["actions"] if a["result"] == "Created tier26-x"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["source"], "ui")
        self.assertIn("trash", r["trash_note"].lower())

    def test_skills_catalog_lists_registered_tools(self):
        names = [e["name"] for e in self.bridge.get_skills_catalog()["entries"]]
        self.assertIn("open_app", names)
        self.assertEqual(names, sorted(names))

    def test_performance_report_with_no_voice_turns(self):
        r = self.bridge.get_performance_report()
        self.assertTrue(r["ok"])
        self.assertIsInstance(r["paths"], list)
        self.assertIn("usage", r["usage_text"].lower())


# ---------------------------------------------------------------------------
class SettingsTests(_Base):
    def test_unknown_settings_are_rejected(self):
        self.assertFalse(self.bridge.apply_setting("rm_rf", True)["ok"])

    def test_privacy_toggles_take_effect_immediately(self):
        # The class the bridge module itself imported - another test file in
        # the same run may have swapped vision.screen_capture in sys.modules.
        vision_cls = parity.VisionCapture
        old = vision_cls.capture_enabled
        self.addCleanup(setattr, vision_cls, "capture_enabled", old)
        r = self.bridge.apply_setting("screen_capture_allowed", False)
        self.assertTrue(r["ok"], r)
        self.assertFalse(vision_cls.capture_enabled)
        r = self.bridge.apply_setting("clipboard_monitoring", False)
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.bridge._clip_enabled)

    def test_provider_info_text_matches_the_settings_page_line(self):
        r = self.bridge.provider_info()
        self.assertEqual(r["text"], "fake / m1  ·  local  ·  key …1234")

    def test_clear_all_data_removes_the_settings_file(self):
        self.bridge.save_settings({"notes": "x"})
        self.assertTrue(self.bridge.clear_all_data()["cleared"])
        self.assertFalse(os.path.exists(self.settings_file))


# ---------------------------------------------------------------------------
class StaticPageTests(unittest.TestCase):
    """The page itself: everything the desktop has is wired in."""

    def _read(self, *parts):
        with open(os.path.join(STATIC, *parts), encoding="utf-8") as f:
            return f.read()

    def test_index_loads_every_script_in_order(self):
        html = self._read("index.html")
        order = ["js/theme.js", "js/bridge.js", "js/widgets.js", "js/avatar.js", "js/app.js", "js/pages2.js"]
        positions = [html.index(f'src="{s}"') for s in order]
        self.assertEqual(positions, sorted(positions))

    def test_all_seventeen_desktop_pages_are_in_the_nav(self):
        app = self._read("js", "app.js")
        for label in ["Home", "Chat", "System Monitor", "Apps & Power", "Music", "Files", "Vision", "Clipboard",
                      "Activity", "Workflows", "Memory", "Reminders", "Notes & To-dos", "Calendar", "Skills",
                      "Performance", "Settings"]:
            self.assertIn(f'["{label}", ', app)

    def test_dodo_sits_just_above_chat_and_holds_the_character(self):
        # The user moved Wave's character off the Chat page onto its own
        # sidebar page, "Dodo", right before Chat (web UI only).
        app = self._read("js", "app.js")
        self.assertIn('["Home", "house"], ["Dodo", "user"], ["Chat", "message"]', app)
        self.assertIn("WaveAvatar.build(dodo.mount)", app)
        self.assertNotIn("avatarMount", app)
        self.assertNotIn("avatar-mount", self._read("css", "main.css"))

    def test_page_indices_match_the_nav_order(self):
        # IDX, the nav list and the order pages are built in must agree, or
        # a nav button / palette entry opens the wrong page.
        import re
        app = self._read("js", "app.js")
        nav_block = app[app.index("const NAV = ["):app.index("];", app.index("const NAV = ["))]
        nav = re.findall(r'\["([^"]+)", "[^"]+"\]', nav_block)
        idx_block = app[app.index("const IDX = {"):app.index("};", app.index("const IDX = {"))]
        idx = {k: int(v) for k, v in re.findall(r"(\w+): (\d+)", idx_block)}
        order_block = app[app.index("const order = ["):app.index("];", app.index("const order = ["))]
        order = re.findall(r'"(\w+)"', order_block)
        labels = {"HOME": "Home", "DODO": "Dodo", "CHAT": "Chat", "SYSTEM": "System Monitor", "APPS": "Apps & Power",
                  "MUSIC": "Music", "FILES": "Files", "VISION": "Vision", "CLIPBOARD": "Clipboard", "ACTIVITY": "Activity",
                  "WORKFLOWS": "Workflows", "MEMORY": "Memory", "REMINDERS": "Reminders", "NOTES": "Notes & To-dos",
                  "CALENDAR": "Calendar", "SKILLS": "Skills", "PERFORMANCE": "Performance", "SETTINGS": "Settings"}
        self.assertEqual(len(nav), 18)
        self.assertEqual(sorted(idx.values()), list(range(len(nav))))
        self.assertEqual(len(order), len(nav))
        for key, i in idx.items():
            self.assertEqual(nav[i], labels[key], key)
            self.assertEqual(order[i], key.lower(), key)
        self.assertIsNone(re.search(r"slideTo\(\d+\)", app), "page switches must go through IDX")

    def test_all_eleven_desktop_themes_exist(self):
        theme_js = self._read("js", "theme.js")
        for name in ["Black Glass", "Midnight Blue", "Emerald Dark", "Crimson Dark", "Amber Dark", "Slate Dark",
                     "Violet Dark", "Daylight", "Paper Light", "Ocean Light", "Mint Light"]:
            self.assertIn(name, theme_js)

    def test_the_icon_font_ships_with_the_page(self):
        self.assertTrue(os.path.getsize(os.path.join(STATIC, "fonts", "fa6-solid-900.ttf")) > 100_000)

    def test_every_bridge_call_the_page_makes_exists(self):
        import re
        source = self._read("js", "app.js") + self._read("js", "pages2.js") + self._read("js", "avatar.js")
        called = set(re.findall(r'call\("([a-z_]+)"', source))
        missing = sorted(name for name in called if not callable(getattr(WaveWebBridge, name, None)))
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
