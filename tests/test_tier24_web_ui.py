"""
tests/test_tier24_web_ui.py — Tier 24: the pywebview + HTML/CSS/JS UI.

Covers webui/bridge.py (WaveWebBridge - the one seam between the page and
the real backend) and webui/voice_worker.py (the threading.Thread ports of
ui/workers.py's ContinuousMicWorker/AIStreamWorker). No PyQt6 import
anywhere in this file or in webui/ itself - these tests run exactly where
the rest of this suite already does.

Same DB-isolation pattern test_tier16_notes_todos.py's
_DbIsolatedTestBase already uses: redirect db.db_path to a fresh temp
file per test rather than touching the real wave_memory.db.

Run: python -m unittest discover -s tests -p "test_tier24_web_ui.py"
"""
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from database.db_manager import db                      # noqa: E402
from automation.audit import audit as audit_singleton    # noqa: E402
from automation import security_log                      # noqa: E402
import webui.bridge as bridge_module                       # noqa: E402
from webui.bridge import WaveWebBridge                    # noqa: E402
from webui.voice_worker import WebContinuousMicWorker, WebReminderWorker  # noqa: E402
from voice import commands as voice_commands               # noqa: E402


class _FakeBrain:
    """Enough of WaveBrain's surface for the bridge methods that touch
    it - think_stream (chat), check_provider (settings), and
    process_actions/pending_action (workflows) - without needing a real
    LLM provider or API key."""
    def __init__(self, reply="hi there", raise_on=None):
        self.reply = reply
        self.raise_on = raise_on
        self.pending_action = None
        self.actions_seen = []

    def think_stream(self, text, attachment=None, cancel_event=None):
        if self.raise_on and text == self.raise_on:
            raise RuntimeError("boom")
        for piece in self.reply.split(" "):
            if cancel_event is not None and cancel_event.is_set():
                return
            yield piece + " "

    def check_provider(self):
        return (True, "fake-model", 12)

    def process_actions(self, text):
        self.actions_seen.append(text)
        return f"did: {text}"


class _FakeWindow:
    """Captures every evaluate_js call a bridge pushes, instead of a real
    pywebview window - the same role _SlowFakeBrain/fake transports play
    elsewhere in this suite."""
    def __init__(self):
        self.calls = []

    def evaluate_js(self, script):
        self.calls.append(script)


def _stop_and_join_reminder_worker(worker, timeout=2):
    """Every WaveWebBridge() now starts a WebReminderWorker immediately
    (mirroring desktop's _start_reminder_worker()), so each test that
    constructs a bridge must stop it in addCleanup() - a bare .stop() only
    flips a flag; it doesn't wait for an ALREADY-IN-PROGRESS poll tick to
    finish. Without the join() below, a poll that's mid-flight when one
    test ends can still be running when the NEXT test's _DbIsolatedTestBase
    swaps db.db_path to a fresh temp file, and hit a table that doesn't
    exist yet in it - a real race, not a hypothetical one (this is exactly
    what joining here closes off)."""
    worker.stop()
    worker.join(timeout=timeout)


class _DbIsolatedTestBase(unittest.TestCase):
    def setUp(self):
        self.old_path = db.db_path
        tmp = S.make_tmpdir(self)
        db.db_path = os.path.join(tmp, "t.db")
        db.init_db()
        self.addCleanup(setattr, db, "db_path", self.old_path)
        # audit_singleton/security_log are process-wide singletons, not
        # redirected by the db.db_path swap above - clear them going IN
        # (so an earlier test file's leftover rows in THIS process don't
        # inflate an assertion here) as well as coming out (so this test
        # doesn't leave rows behind for whatever runs after it).
        audit_singleton.clear()
        security_log.clear()
        self.addCleanup(audit_singleton.clear)
        self.addCleanup(security_log.clear)


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
class SettingsTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        tmp = S.make_tmpdir(self)
        self.settings_file = os.path.join(tmp, "wave_settings.json")
        import webui.bridge as bridge_mod
        self.old_settings_file = bridge_mod.SETTINGS_FILE
        bridge_mod.SETTINGS_FILE = self.settings_file
        self.addCleanup(setattr, bridge_mod, "SETTINGS_FILE", self.old_settings_file)
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_settings_on_a_fresh_install_is_an_empty_dict(self):
        self.assertEqual(self.bridge.get_settings(), {"ok": True, "settings": {}})

    def test_save_settings_merges_rather_than_replaces(self):
        self.bridge.save_settings({"a": 1})
        result = self.bridge.save_settings({"b": 2})
        self.assertEqual(result["settings"], {"a": 1, "b": 2})

    def test_save_settings_persists_across_bridge_instances(self):
        self.bridge.save_settings({"web_theme": "light"})
        other = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, other._reminder_worker)
        self.assertEqual(other.get_settings()["settings"]["web_theme"], "light")

    def test_check_provider_reports_the_fake_brains_result(self):
        result = self.bridge.check_provider()
        self.assertTrue(result["provider_ok"])
        self.assertEqual(result["message"], "fake-model")


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------
class ChatTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.brain = _FakeBrain(reply="hello world")
        self.bridge = WaveWebBridge(brain=self.brain)
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)
        self.window = _FakeWindow()
        self.bridge.set_window(self.window)

    def test_send_message_streams_chunks_then_a_done_event(self):
        result = self.bridge.send_message("hi")
        self.assertTrue(result["started"])
        self.bridge._reply_worker.join(timeout=2)
        chunk_events = [c for c in self.window.calls if '"chat_chunk"' in c]
        done_events = [c for c in self.window.calls if '"chat_done"' in c]
        self.assertTrue(chunk_events, "expected at least one chat_chunk push")
        self.assertEqual(len(done_events), 1)
        self.assertIn("hello world", done_events[0])

    def test_a_new_message_cancels_the_one_in_flight(self):
        self.brain.reply = "a b c d e f g h i j k l m n o p"
        self.bridge.send_message("first")
        first_worker = self.bridge._reply_worker
        self.bridge.send_message("second")
        self.assertTrue(first_worker.cancelled)
        self.bridge._reply_worker.join(timeout=2)

    def test_cancel_reply_with_nothing_in_flight_is_a_harmless_no_op(self):
        result = self.bridge.cancel_reply()
        self.assertFalse(result["cancelled"])

    def test_a_failing_brain_does_not_crash_the_bridge(self):
        self.brain.raise_on = "trigger"
        result = self.bridge.send_message("trigger")
        self.assertTrue(result["started"])
        self.bridge._reply_worker.join(timeout=2)
        done_events = [c for c in self.window.calls if '"chat_done"' in c]
        self.assertEqual(len(done_events), 1)  # still finishes cleanly, just with no text

    def test_send_message_speaks_the_reply_like_desktop_always_does(self):
        # 🔧 regression test: send_message() used to hardcode
        # speak_reply=False, so every single web-UI chat reply - typed or
        # voice-triggered - was silent end to end. Desktop's own
        # AIStreamWorker call always passes speak_reply=True regardless of
        # how the turn started; the web UI now matches that exactly.
        self.bridge.send_message("hi")
        self.assertTrue(self.bridge._reply_worker.speak_reply)
        self.bridge._reply_worker.join(timeout=2)


# ---------------------------------------------------------------------------
# memory / notes / todos / reminders
# ---------------------------------------------------------------------------
class MemoryTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_memory_reflects_saved_facts_with_provenance(self):
        db.save_memory("favorite_color", "blue", source="explicit")
        items = self.bridge.get_memory()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["key"], "favorite_color")
        self.assertEqual(items[0]["source"], "explicit")

    def test_delete_memory_removes_the_key(self):
        db.save_memory("x", "y")
        self.bridge.delete_memory("x")
        self.assertEqual(self.bridge.get_memory()["items"], [])


class NotesTodosTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_add_and_list_notes_round_trip(self):
        self.bridge.add_note("buy milk")
        notes = self.bridge.get_notes()["notes"]
        self.assertEqual([n["content"] for n in notes], ["buy milk"])

    def test_clear_notes(self):
        self.bridge.add_note("one")
        self.bridge.clear_notes()
        self.assertEqual(self.bridge.get_notes()["notes"], [])

    def test_add_complete_and_delete_todo(self):
        self.bridge.add_todo("ship tier 24")
        todos = self.bridge.get_todos()["todos"]
        self.assertEqual(len(todos), 1)
        self.assertFalse(todos[0]["completed"])
        self.bridge.complete_todo("ship tier 24")
        todos = self.bridge.get_todos(include_completed=True)["todos"]
        self.assertTrue(todos[0]["completed"])
        self.bridge.delete_todo("ship tier 24")
        self.assertEqual(self.bridge.get_todos(include_completed=True)["todos"], [])


class RemindersTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_reminders_reflects_saved_rows(self):
        db.save_reminder("drink water", "2099-01-01T10:00:00")
        reminders = self.bridge.get_reminders()["reminders"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["message"], "drink water")

    def test_cancel_reminder(self):
        db.save_reminder("drink water", "2099-01-01T10:00:00")
        result = self.bridge.cancel_reminder("drink water")
        self.assertEqual(result["cancelled"], 1)
        self.assertEqual(self.bridge.get_reminders()["reminders"], [])


# ---------------------------------------------------------------------------
# WebReminderWorker - before this class existed, nothing in the web UI ever
# polled for a reminder coming due (unlike desktop's
# MainWindow._start_reminder_worker()), so a reminder set via chat was
# written to the DB correctly but would just silently never fire - this is
# the gap the user actually hit ("reminder set" in the reply, nothing ever
# happened). These tests call _check_due()/_check_upcoming() directly
# rather than through run()'s polling loop, same reasoning
# test_tier3_reminders.py uses for the desktop version: the logic under
# test is the SQL + composer wiring, not the sleep loop around it.
# ---------------------------------------------------------------------------
class ReminderWorkerTests(_DbIsolatedTestBase):
    def test_check_due_fires_the_callback_and_marks_the_reminder_triggered(self):
        db.save_reminder("drink water", "2000-01-01T00:00:00")  # long past due
        fired = []
        worker = WebReminderWorker(on_due=lambda text, reminder_id: fired.append((text, reminder_id)))
        worker._check_due()
        self.assertEqual(len(fired), 1)
        text, reminder_id = fired[0]
        self.assertTrue(text)  # composer always returns non-empty text for real input
        self.assertIsInstance(reminder_id, int)
        # marked triggered -> a second poll must not fire it again
        fired.clear()
        worker._check_due()
        self.assertEqual(fired, [])

    def test_check_upcoming_fires_once_with_the_right_minutes_until(self):
        due_at = (datetime.now() + timedelta(minutes=5)).isoformat()
        db.save_reminder("team meeting", due_at)
        fired = []
        worker = WebReminderWorker(
            on_upcoming=lambda text, reminder_id, minutes_until: fired.append((text, reminder_id, minutes_until)))
        worker._check_upcoming()
        self.assertEqual(len(fired), 1)
        text, reminder_id, minutes_until = fired[0]
        self.assertTrue(text)
        self.assertIsInstance(reminder_id, int)
        self.assertTrue(0 <= minutes_until <= 5)
        # notified_upcoming is now set -> a second poll must not repeat it
        fired.clear()
        worker._check_upcoming()
        self.assertEqual(fired, [])

    def test_a_reminder_not_yet_due_fires_neither_callback(self):
        db.save_reminder("someday", (datetime.now() + timedelta(days=1)).isoformat())
        due_fired, upcoming_fired = [], []
        worker = WebReminderWorker(on_due=lambda *a: due_fired.append(a),
                                    on_upcoming=lambda *a: upcoming_fired.append(a))
        worker._check_due()
        worker._check_upcoming()
        self.assertEqual(due_fired, [])
        self.assertEqual(upcoming_fired, [])

    def test_bridge_starts_a_reminder_worker_automatically_like_desktop_does(self):
        # MainWindow starts ReminderWorker right in __init__, not lazily on
        # first mic toggle, because a reminder can be due at any time
        # whether or not the user has ever touched voice - the web bridge
        # now mirrors that exactly.
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)
        self.assertIsInstance(bridge._reminder_worker, WebReminderWorker)
        self.assertTrue(bridge._reminder_worker.is_alive())

    def test_bridge_on_reminder_due_pushes_a_toast_event_and_speaks_it(self):
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)
        window = _FakeWindow()
        bridge.set_window(window)
        with mock.patch.object(bridge_module.tts_engine, "speak") as mock_speak:
            bridge._on_reminder_due("It's time to drink water.", 7)
        due_events = [c for c in window.calls if '"reminder_due"' in c]
        self.assertEqual(len(due_events), 1)
        self.assertIn("It's time to drink water.", due_events[0])
        mock_speak.assert_called_once_with("It's time to drink water.")

    def test_bridge_on_reminder_upcoming_pushes_and_speaks_without_raising_if_tts_fails(self):
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)
        window = _FakeWindow()
        bridge.set_window(window)
        with mock.patch.object(bridge_module.tts_engine, "speak", side_effect=RuntimeError("no audio device")):
            bridge._on_reminder_upcoming("Team meeting in 5 minutes.", 3, 5)  # must not raise
        upcoming_events = [c for c in window.calls if '"reminder_upcoming"' in c]
        self.assertEqual(len(upcoming_events), 1)
        self.assertIn("Team meeting in 5 minutes.", upcoming_events[0])


# ---------------------------------------------------------------------------
# voice-mode announcements - mirrors MainWindow.handle_wake_word_mode: the
# sidebar's STANDBY/LISTENING label is driven by the real mode (not
# optimistically set the instant the mic toggles on), and Wave speaks the
# wake/sleep transition itself, same as desktop does.
# ---------------------------------------------------------------------------
class VoiceModeAnnounceTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)
        self.window = _FakeWindow()
        self.bridge.set_window(self.window)

    def test_waking_up_pushes_awake_and_speaks_systems_active(self):
        with mock.patch.object(bridge_module.tts_engine, "speak") as mock_speak:
            self.bridge._on_voice_mode_changed("AWAKE", True)
        mode_events = [c for c in self.window.calls if '"voice_mode"' in c]
        self.assertEqual(len(mode_events), 1)
        self.assertIn("AWAKE", mode_events[0])
        mock_speak.assert_called_once_with("Systems active.")

    def test_going_to_sleep_pushes_sleeping_and_speaks_standing_by(self):
        with mock.patch.object(bridge_module.tts_engine, "speak") as mock_speak:
            self.bridge._on_voice_mode_changed("SLEEPING", True)
        mode_events = [c for c in self.window.calls if '"voice_mode"' in c]
        self.assertEqual(len(mode_events), 1)
        self.assertIn("SLEEPING", mode_events[0])
        mock_speak.assert_called_once_with("Standing by.")

    def test_the_initial_mode_push_on_mic_start_is_silent(self):
        # WebContinuousMicWorker.run() announces its starting mode with
        # announce=False - this is the thread starting, not a real
        # wake/sleep transition, so it must not speak.
        with mock.patch.object(bridge_module.tts_engine, "speak") as mock_speak:
            self.bridge._on_voice_mode_changed("SLEEPING", False)
        mode_events = [c for c in self.window.calls if '"voice_mode"' in c]
        self.assertEqual(len(mode_events), 1)
        mock_speak.assert_not_called()


# ---------------------------------------------------------------------------
# workflows - uses the fake brain's process_actions, same call
# brain/handlers/automation.py's run_workflow skill makes
# ---------------------------------------------------------------------------
class WorkflowsTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.brain = _FakeBrain()
        self.bridge = WaveWebBridge(brain=self.brain)
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_workflows_lists_saved_ones_with_their_steps(self):
        db.save_workflow("morning", ["open spotify", "what's the weather"])
        workflows = self.bridge.get_workflows()["workflows"]
        self.assertEqual(workflows, [{"name": "morning", "actions": ["open spotify", "what's the weather"]}])

    def test_run_workflow_calls_process_actions_for_each_step_in_order(self):
        db.save_workflow("morning", ["step one", "step two"])
        result = self.bridge.run_workflow("morning")
        self.assertEqual(self.brain.actions_seen, ["step one", "step two"])
        self.assertEqual(result["results"], ["did: step one", "did: step two"])

    def test_run_workflow_stops_at_a_pending_confirmation(self):
        db.save_workflow("risky", ["step one", "step two"])

        def process_actions(text):
            self.brain.actions_seen.append(text)
            if text == "step one":
                self.brain.pending_action = {"tool": "something"}
            return f"did: {text}"
        self.brain.process_actions = process_actions

        self.bridge.run_workflow("risky")
        self.assertEqual(self.brain.actions_seen, ["step one"])  # never reached step two

    def test_run_workflow_on_an_unknown_name_is_an_honest_error(self):
        result = self.bridge.run_workflow("does-not-exist")
        self.assertFalse(result["ok"])

    def test_delete_workflow(self):
        db.save_workflow("temp", ["x"])
        self.bridge.delete_workflow("temp")
        self.assertEqual(self.bridge.get_workflows()["workflows"], [])


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------
class SkillsTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.skills_dir = S.make_tmpdir(self)
        with open(os.path.join(self.skills_dir, "example.py"), "w") as f:
            f.write("# empty\n")
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)
        self.bridge._skills.directory = self.skills_dir
        # redirect the disabled-sidecar path to live alongside the fake directory too
        self.bridge._skills._disabled_path = os.path.join(self.skills_dir, ".disabled_skills.json")

    def test_get_skills_lists_python_files_as_enabled_by_default(self):
        skills = self.bridge.get_skills()["skills"]
        self.assertEqual(skills, [{"filename": "example.py", "disabled": False}])

    def test_set_skill_disabled_is_reflected_on_the_next_listing(self):
        self.bridge.set_skill_disabled("example.py", True)
        skills = self.bridge.get_skills()["skills"]
        self.assertTrue(skills[0]["disabled"])


# ---------------------------------------------------------------------------
# activity / undo, security log
# ---------------------------------------------------------------------------
class ActivityTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_recent_activity_reflects_audited_actions(self):
        with audit_singleton.action("test_tool", {"a": 1}, source="test") as scope:
            scope.ok("did a thing")
        actions = self.bridge.get_recent_activity()["actions"]
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["tool"], "test_tool")

    def test_undo_last_with_nothing_reversible_reports_failure_not_a_crash(self):
        result = self.bridge.undo_last()
        self.assertFalse(result["success"])


class SystemMusicVisionTests(_DbIsolatedTestBase):
    """The System Monitor / Music / Vision pages added so the web UI covers
    every page the PyQt6 UI has (ui/main_window.py's NAV_ITEMS), not just
    the subset Tier 24 originally shipped with."""

    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)

    def test_get_system_stats_returns_real_psutil_numbers(self):
        result = self.bridge.get_system_stats()
        self.assertTrue(result["ok"])
        stats = result["stats"]
        self.assertIn("cpu_percent", stats)
        self.assertIn("ram_percent", stats)
        self.assertIn("disk_percent", stats)

    def test_media_controls_go_through_without_raising(self):
        for method in (self.bridge.media_play_pause, self.bridge.media_next_track,
                       self.bridge.media_previous_track):
            result = method()
            self.assertTrue(result["ok"])

    def test_vision_read_screen_reports_a_capture_failure_honestly(self):
        # No real display in this sandbox, so take_screenshot() returning
        # falsy is the expected path - proves the bridge surfaces that as
        # an honest error instead of crashing on a None path.
        result = self.bridge.vision_read_screen()
        self.assertIn("ok", result)


class SecurityTests(_DbIsolatedTestBase):
    def setUp(self):
        super().setUp()
        self.bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, self.bridge._reminder_worker)
        # security_log.set_mode() changes settings.config.SECURITY_MODE in
        # place (a real, shared, module-level global - see its own
        # docstring) - leaving it on "strict" after this test would change
        # confirm-gate behavior for every OTHER test file that runs after
        # this one in the same process, same class of bug the DB-isolation
        # base class above exists to prevent for the database.
        from settings.config import config as _config
        self._old_security_mode = getattr(_config, "SECURITY_MODE", "standard")
        self.addCleanup(setattr, _config, "SECURITY_MODE", self._old_security_mode)

    def test_set_and_get_security_mode_round_trips(self):
        self.bridge.set_security_mode("strict")
        self.assertEqual(self.bridge.get_security_mode()["mode"], "strict")

    def test_get_security_log_reflects_recorded_events(self):
        security_log.record("guard_deny", "a test denial", tool="t", source="test")
        events = self.bridge.get_security_log()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "guard_deny")


# ---------------------------------------------------------------------------
# the _push mechanism itself (every streaming event goes through this)
# ---------------------------------------------------------------------------
class PushTests(_DbIsolatedTestBase):
    def test_push_with_no_window_set_is_a_silent_no_op(self):
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)
        bridge._push("some_event", {"x": 1})  # must not raise

    def test_push_calls_evaluate_js_with_the_event_and_payload(self):
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)
        window = _FakeWindow()
        bridge.set_window(window)
        bridge._push("voice_level", {"level": 0.5})
        self.assertEqual(len(window.calls), 1)
        self.assertIn("voice_level", window.calls[0])
        self.assertIn("0.5", window.calls[0])

    def test_push_never_raises_even_if_evaluate_js_itself_throws(self):
        bridge = WaveWebBridge(brain=_FakeBrain())
        self.addCleanup(_stop_and_join_reminder_worker, bridge._reminder_worker)

        class BrokenWindow:
            def evaluate_js(self, script):
                raise RuntimeError("window is closing")
        bridge.set_window(BrokenWindow())
        bridge._push("x", {})  # must not raise


# ---------------------------------------------------------------------------
# webui/voice_worker.py - a structural mirror of ui/workers.py's
# ContinuousMicWorker, so only its policy surface (handle_text's routing)
# needs its own check here; the underlying decide()/voice_commands logic
# is already covered by the existing voice-loop test files.
# ---------------------------------------------------------------------------
class _FakeText(str):
    """A str subclass so handle_text's `text == ""` check still works,
    with the extra attributes ContinuousMicWorkerTests pins (wake_detected,
    snr_db, etc.) available via getattr() the same way a real
    voice.stt.Transcript object's would be."""
    wake_detected = False
    snr_db = None
    speech_end = None
    captured_at = None
    recognized_at = None
    engine = "test"


class WebContinuousMicWorkerTests(unittest.TestCase):
    def _make_worker(self):
        events = {"text_ready": [], "mode_changed": [], "stop_requested": []}
        worker = WebContinuousMicWorker(
            wake_phrases=["hey wave"],
            on_text_ready=lambda t: events["text_ready"].append(t),
            on_mode_changed=lambda m, a: events["mode_changed"].append((m, a)),
            on_stop_requested=lambda: events["stop_requested"].append(True),
        )
        return worker, events

    def test_wake_word_while_sleeping_wakes_without_dispatching_text(self):
        worker, events = self._make_worker()
        text = _FakeText("hey wave")
        text.wake_detected = True
        worker.handle_text(text)
        self.assertEqual(worker.mode, "AWAKE")
        self.assertEqual(events["text_ready"], [])

    def test_wake_and_command_in_one_breath_dispatches_the_command(self):
        worker, events = self._make_worker()
        text = _FakeText("hey wave open chrome")
        text.wake_detected = True
        worker.handle_text(text)
        self.assertEqual(worker.mode, "AWAKE")
        self.assertEqual(events["text_ready"], ["open chrome"])

    def test_plain_command_while_awake_dispatches_it(self):
        worker, events = self._make_worker()
        worker.mode = "AWAKE"
        worker.handle_text(_FakeText("what's the weather"))
        self.assertEqual(events["text_ready"], ["what's the weather"])

    def test_empty_text_while_awake_reports_recognition_failed_not_a_command(self):
        events = {"failed": []}
        worker = WebContinuousMicWorker(on_recognition_failed=lambda: events["failed"].append(True))
        worker.mode = "AWAKE"
        worker.handle_text(_FakeText(""))
        self.assertEqual(events["failed"], [True])

    def test_none_text_is_plain_silence_and_triggers_nothing(self):
        worker, events = self._make_worker()
        worker.handle_text(None)
        self.assertEqual(events["text_ready"], [])
        self.assertEqual(events["mode_changed"], [])

    def test_stop_phrase_triggers_stop_requested(self):
        worker, events = self._make_worker()
        worker.mode = "AWAKE"
        worker.handle_text(_FakeText("stop"))
        self.assertEqual(events["stop_requested"], [True])


if __name__ == "__main__":
    unittest.main()
