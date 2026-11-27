"""tests/test_tier54_green_on_windows.py - Tier 54: the app-side fixes behind
a fully green test suite on the owner's Windows PC.

Most of the 21 failures + 7 errors the suite showed there were tests reading
the real machine (its .env, registry, installed packages, open windows) or a
patch landing on the wrong copy of a module - fixed in the tests themselves.
Three were real bugs, pinned here so they stay fixed on every platform:
- a voice/AI "volume up" left TWO activity rows on a PC whose volume can be
  read ("Volume up" and "Set volume 60"), so "my recent actions" never
  grouped them (brightness and the max/min commands the same);
- "delete my latest screenshot" could delete the OLDER of two shots that
  share a creation time (one tick of Windows' file clock);
- "watch ~/Desktop for a download" reported C:\\Users\\me/Desktop.
"""
import os
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

from automation import audit as audit_mod  # noqa: E402
from automation import backends  # noqa: E402
from automation.app_control import AppController  # noqa: E402
from automation.audit import Audit, MemoryStore  # noqa: E402
from automation.backends.base import PlatformBackend  # noqa: E402
from settings.config import config  # noqa: E402


class _Levels(PlatformBackend):
    """A PC whose volume and brightness can be read and set."""
    name, display_name = "fakeos", "FakeOS"

    def __init__(self, volume=50, brightness=50):
        self.volume, self.brightness = volume, brightness

    def get_volume(self):
        return self.volume

    def set_volume(self, percent):
        self.volume = max(0, min(100, int(percent)))
        return f"Volume set to {self.volume}%."

    def get_brightness(self):
        return self.brightness

    def set_brightness(self, percent):
        self.brightness = max(0, min(100, int(percent)))
        return f"Brightness set to {self.brightness}%."


class OneRowPerCommandTests(unittest.TestCase):
    def setUp(self):
        self.audit = Audit(store=MemoryStore())
        saved = audit_mod.audit
        audit_mod.audit = self.audit
        self.levels = _Levels()
        backends.set_backend(self.levels)

        def restore():
            audit_mod.audit = saved
            backends.set_backend(None)
        self.addCleanup(restore)

    def _dispatch(self, tool, call):
        """What brain/groq_core.py's dispatcher does around every tool call."""
        with self.audit.action(tool, {}, source="ai", adoptable=True) as act:
            return act.done(call())

    def test_a_dispatched_volume_up_is_one_row_that_can_be_undone(self):
        self.assertEqual(self._dispatch("volume_up", AppController.volume_up), "Volume set to 60%.")
        rows = self.audit.recent()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.tool, row.source, row.status, row.args), ("volume_up", "ai", "ok", {}))
        self.assertEqual((row.undo_kind, row.undo_payload), ("restore_volume", {"percent": 50}))
        self.assertEqual(row.summary(), "Volume up")
        self.assertEqual(row.result, "Volume set to 60%.")

    def test_a_run_of_them_groups_into_one_line(self):
        from brain.handlers.automation import _group_consecutive
        for _ in range(3):
            self._dispatch("volume_up", AppController.volume_up)
        self.assertEqual(self.levels.volume, 80)
        groups = _group_consecutive(self.audit.recent())
        self.assertEqual([(r.summary(), n) for r, n in groups], [("Volume up", 3)])

    def test_max_min_and_brightness_follow_the_same_rule(self):
        for tool, call, label, undo in (
                ("volume_max", AppController.volume_max, "Volume to max", "restore_volume"),
                ("volume_down", AppController.volume_down, "Volume down", "restore_volume"),
                ("brightness_up", AppController.brightness_up, "Brightness up", "restore_brightness"),
                ("brightness_min", AppController.brightness_min, "Brightness to min", "restore_brightness")):
            with self.subTest(tool=tool):
                before = len(self.audit.recent())
                self._dispatch(tool, call)
                rows = self.audit.recent()
                self.assertEqual(len(rows), before + 1)
                self.assertEqual((rows[0].tool, rows[0].summary(), rows[0].undo_kind), (tool, label, undo))

    def test_a_button_press_is_still_audited_as_the_delegate(self):
        AppController.volume_up()                      # no dispatcher around it
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.args), ("set_volume", {"percent": 60}))

    def test_any_other_tool_still_nests_as_its_own_row(self):
        self._dispatch("open_app", lambda: AppController.set_volume(30))
        self.assertEqual([r.tool for r in self.audit.recent()], ["open_app", "set_volume"])


class LatestScreenshotTests(unittest.TestCase):
    """Two shots in one tick of the file clock share a creation time; the
    name (which carries the timestamp) decides which is newer."""

    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.shots = os.path.join(self.tmp, "screenshots")
        os.makedirs(self.shots)
        for name in ("Wave_20260101_000001.png", "Wave_20260101_000002.png", "Wave_20260101_000003.png",
                     "wave_internal_x.png"):
            with open(os.path.join(self.shots, name), "wb") as f:
                f.write(b"png")
        saved_dir, config.DATA_DIR = config.DATA_DIR, self.tmp
        self.addCleanup(setattr, config, "DATA_DIR", saved_dir)
        saved_audit = audit_mod.audit
        audit_mod.audit = Audit(store=MemoryStore())
        self.addCleanup(setattr, audit_mod, "audit", saved_audit)

    def test_delete_latest_takes_the_later_name_on_a_tie(self):
        import automation.undo as undo_mod
        with mock.patch("os.path.getctime", return_value=1000.0), \
                mock.patch.object(undo_mod, "trash_enabled", return_value=False):
            AppController.delete_latest_screenshot()
        self.assertEqual(sorted(os.listdir(self.shots)),
                         ["Wave_20260101_000001.png", "Wave_20260101_000002.png", "wave_internal_x.png"])

    def test_retention_keeps_the_later_names_on_a_tie(self):
        from vision.screen_capture import _enforce_retention
        with mock.patch("os.path.getctime", return_value=1000.0):
            _enforce_retention(self.shots, "Wave_", 2)
        self.assertEqual(sorted(os.listdir(self.shots)),
                         ["Wave_20260101_000002.png", "Wave_20260101_000003.png", "wave_internal_x.png"])


class WatchFolderTests(unittest.TestCase):
    def test_a_home_relative_folder_comes_back_in_the_platforms_own_form(self):
        for given in ("~/Desktop", "Desktop"):
            with self.subTest(given=given):
                got = AppController._resolve_watch_folder(given)
                self.assertEqual(got, os.path.normpath(os.path.join(os.path.expanduser("~"), "Desktop")))
                self.assertEqual(got, os.path.normpath(got))       # no C:\Users\me/Desktop mix

    def test_the_users_own_absolute_path_is_left_as_given(self):
        self.assertEqual(AppController._resolve_watch_folder("/tmp/some/folder"), "/tmp/some/folder")
        self.assertIsNone(AppController._resolve_watch_folder(""))


if __name__ == "__main__":
    unittest.main()
