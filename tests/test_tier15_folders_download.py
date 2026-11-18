"""
tests/test_tier15_folders_download.py — Tier 15: system control surface II,
part 2 - folder shortcuts (open_downloads/open_documents/open_pictures/
open_music/open_videos/open_this_pc/open_recycle_bin/open_task_manager/
open_control_panel/empty_recycle_bin) and the download watcher
(watch_for_download/cancel_download_watch, plus the real mechanism behind
them in automation/proactive.py).

FolderShortcutTests/SpecialLocationTests/EmptyRecycleBinTests cover
AppController-level logic against a minimal fake backend, same split as
test_tier15_keyboard_browser.py. DownloadWatchEngineTests exercises the
REAL ProactiveEngine (a real temp folder, no mocking) - that's where the
actual "new file appeared" / "temp file vanished, final name appeared" /
timeout logic lives, so it's the one place in this file worth testing
against reality rather than a fake. AppControllerDownloadWatchTests then
checks only the thin delegation+audit wrapper on top of it.

Run: python -m unittest discover -s tests -p "test_tier15_folders_download.py"
(pytest will also collect it.)
"""
import os
import shutil
import tempfile
import time
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
from automation.proactive import ProactiveEngine, ProactiveNudge    # noqa: E402
from settings.config import config                                   # noqa: E402


class _FakeFolderBackend(PlatformBackend):
    """open_path raises whatever `open_path_raises` is (None = succeeds
    silently, matching the real contract - see app_control.py's own
    _open_home_folder). open_special_location/empty_recycle_bin return
    canned strings (success OR a 'Couldn't ...' failure string - both are
    real possible returns, never exceptions, on Windows/macOS) or raise
    NotSupported (Linux's own "no app found" case) depending on what the
    test sets."""
    name = "linux"
    display_name = "FakeOS"

    def __init__(self, open_path_raises=None, special_results=None, recycle_result="Emptied the trash."):
        self.open_path_raises = open_path_raises
        self.special_results = special_results or {}
        self.recycle_result = recycle_result
        self.opened_paths = []

    def open_path(self, path: str) -> None:
        self.opened_paths.append(path)
        if self.open_path_raises is not None:
            raise self.open_path_raises

    def open_special_location(self, name: str) -> str:
        result = self.special_results.get(name, f"Opened {name}.")
        if isinstance(result, Exception):
            raise result
        return result

    def empty_recycle_bin(self) -> str:
        if isinstance(self.recycle_result, Exception):
            raise self.recycle_result
        return self.recycle_result


class FolderDownloadTestBase(unittest.TestCase):
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


class FolderShortcutTests(FolderDownloadTestBase):
    def test_each_home_folder_opens_the_expanded_path_under_home(self):
        backend = self._use(_FakeFolderBackend())
        cases = {
            AppController.open_downloads: "Downloads", AppController.open_documents: "Documents",
            AppController.open_pictures: "Pictures", AppController.open_music: "Music",
            AppController.open_videos: "Videos",
        }
        for method, folder_name in cases.items():
            with self.subTest(method=method.__name__):
                result = method()
                expected_path = os.path.join(os.path.expanduser("~"), folder_name)
                self.assertIn(expected_path, backend.opened_paths)
                self.assertEqual(result, f"Opened {folder_name}.")

    def test_each_home_folder_is_audited_under_its_own_name_with_the_path(self):
        self._use(_FakeFolderBackend())
        AppController.open_downloads()
        row = self._last()
        self.assertEqual(row.tool, "open_downloads")
        self.assertEqual(row.args, {"path": os.path.join(os.path.expanduser("~"), "Downloads")})
        self.assertEqual(row.status, "ok")

    def test_not_supported_is_reported_and_audited_as_failed(self):
        self._use(_FakeFolderBackend(open_path_raises=NotSupported("no file manager found")))
        result = AppController.open_pictures()
        self.assertIn("couldn't open pictures", result.lower())
        self.assertEqual(self._last().status, "failed")

    def test_oserror_is_reported_and_audited_as_failed(self):
        self._use(_FakeFolderBackend(open_path_raises=OSError("disk error")))
        result = AppController.open_music()
        self.assertIn("couldn't open music", result.lower())
        self.assertEqual(self._last().status, "failed")


class SpecialLocationTests(FolderDownloadTestBase):
    def test_success_string_is_audited_ok_via_infer_status(self):
        """open_special_location()'s return value can itself describe
        success OR failure (same shape as launch_app() - see app_control.py's
        own comment) - this is act.done()'s job, not a blind act.ok()."""
        self._use(_FakeFolderBackend(special_results={"this_pc": "Opened Computer in Finder."}))
        result = AppController.open_this_pc()
        self.assertEqual(result, "Opened Computer in Finder.")
        self.assertEqual(self._last().status, "ok")

    def test_an_honest_caveated_success_is_still_audited_ok(self):
        msg = "Linux has no single 'This PC' view - opened your home folder instead."
        self._use(_FakeFolderBackend(special_results={"this_pc": msg}))
        result = AppController.open_this_pc()
        self.assertEqual(result, msg)
        self.assertEqual(self._last().status, "ok")

    def test_a_failure_string_starting_with_couldnt_is_audited_as_failed(self):
        """Windows' own open_special_location() never raises - a failure
        comes back as a plain "Couldn't open that: ..." STRING, which
        act.done()'s infer_status() must still catch."""
        self._use(_FakeFolderBackend(special_results={"recycle_bin": "Couldn't open that: no such app"}))
        result = AppController.open_recycle_bin()
        self.assertEqual(result, "Couldn't open that: no such app")
        self.assertEqual(self._last().status, "failed")

    def test_not_supported_raised_is_caught_and_audited_as_failed(self):
        """Linux's own open_special_location() DOES raise for task_manager/
        control_panel when no matching app is installed - the one case
        that's a real exception rather than a returned string."""
        self._use(_FakeFolderBackend(
            special_results={"task_manager": NotSupported("No system monitor app was found.")}))
        result = AppController.open_task_manager()
        self.assertEqual(result, "No system monitor app was found.")
        self.assertEqual(self._last().status, "failed")

    def test_open_control_panel_delegates_to_the_same_mechanism(self):
        self._use(_FakeFolderBackend(special_results={"control_panel": "Opened System Settings."}))
        result = AppController.open_control_panel()
        self.assertEqual(result, "Opened System Settings.")
        self.assertEqual(self._last().tool, "open_control_panel")


class EmptyRecycleBinTests(FolderDownloadTestBase):
    def test_success_is_audited_ok(self):
        self._use(_FakeFolderBackend(recycle_result="Emptied the trash."))
        result = AppController.empty_recycle_bin()
        self.assertEqual(result, "Emptied the trash.")
        self.assertEqual(self._last().status, "ok")

    def test_nothing_to_empty_is_still_a_success(self):
        msg = "The trash folder doesn't exist yet - nothing to empty."
        self._use(_FakeFolderBackend(recycle_result=msg))
        result = AppController.empty_recycle_bin()
        self.assertEqual(result, msg)
        self.assertEqual(self._last().status, "ok")

    def test_a_couldnt_failure_string_is_audited_as_failed(self):
        self._use(_FakeFolderBackend(recycle_result="Couldn't empty the trash."))
        result = AppController.empty_recycle_bin()
        self.assertEqual(self._last().status, "failed")

    def test_not_supported_is_caught_and_reported(self):
        self._use(_FakeFolderBackend(recycle_result=NotSupported("Emptying the Recycle Bin isn't supported")))
        result = AppController.empty_recycle_bin()
        self.assertIn("isn't supported", result)
        self.assertEqual(self._last().status, "failed")


# =============================================================================
# The real ProactiveEngine mechanism - no mocking, a real temp folder.
# =============================================================================
class DownloadWatchEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wave_dlwatch_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = ProactiveEngine()
        self.saved_enabled = config.PROACTIVE_DOWNLOAD_WATCH_ENABLED
        config.PROACTIVE_DOWNLOAD_WATCH_ENABLED = True
        self.addCleanup(setattr, config, "PROACTIVE_DOWNLOAD_WATCH_ENABLED", self.saved_enabled)

    def test_check_downloads_is_a_no_op_when_nothing_is_armed(self):
        self.assertEqual(self.engine._check_downloads(), [])

    def test_arm_snapshots_the_folder_and_confirms_in_plain_language(self):
        result = self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        self.assertIn(self.tmp, result)
        self.assertIn("5", result)
        self.assertIsNotNone(self.engine._download_watch)

    def test_arming_a_missing_folder_fails_cleanly_without_arming_anything(self):
        missing = os.path.join(self.tmp, "does_not_exist")
        result = self.engine.arm_download_watch(missing, timeout_minutes=5)
        self.assertIn("couldn't watch", result.lower())
        self.assertIsNone(self.engine._download_watch)

    def test_a_brand_new_non_temp_file_is_detected_as_a_finished_download(self):
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        with open(os.path.join(self.tmp, "report.pdf"), "w") as f:
            f.write("done")
        nudges = self.engine._check_downloads()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "download")
        self.assertIn("report.pdf", nudges[0].message)
        self.assertIsNone(self.engine._download_watch)   # self-disarms once it fires

    def test_a_new_temp_file_alone_does_not_count_as_finished(self):
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        with open(os.path.join(self.tmp, "report.pdf.crdownload"), "w") as f:
            f.write("still going")
        self.assertEqual(self.engine._check_downloads(), [])
        self.assertIsNotNone(self.engine._download_watch)   # still armed - nothing finished yet

    def test_a_temp_file_vanishing_with_its_final_name_appearing_counts_as_finished(self):
        """Catches a download finishing under a filename that ALREADY
        existed before the watch started, which signal #1 (brand new
        file) alone would never see - see automation/proactive.py's own
        comment on why there are two signals."""
        final_path = os.path.join(self.tmp, "report.pdf")
        with open(final_path, "w") as f:
            f.write("an old version from before this watch started")
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)

        temp_path = final_path + ".crdownload"
        with open(temp_path, "w") as f:
            f.write("downloading the new version")
        self.assertEqual(self.engine._check_downloads(), [])   # temp file present, not finished yet

        # The browser finishes: the .crdownload disappears, the real file
        # (already present in the baseline) is overwritten/stays.
        os.remove(temp_path)
        nudges = self.engine._check_downloads()
        self.assertEqual(len(nudges), 1)
        self.assertIn("report.pdf", nudges[0].message)

    def test_timing_out_disarms_and_says_so_honestly(self):
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        self.engine._download_watch["timeout_at"] = time.monotonic() - 1   # force it into the past
        nudges = self.engine._check_downloads()
        self.assertEqual(len(nudges), 1)
        self.assertIn("didn't see", nudges[0].message.lower())
        self.assertIsNone(self.engine._download_watch)

    def test_cancel_disarms_and_confirms(self):
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        result = self.engine.cancel_download_watch()
        self.assertIn(self.tmp, result)
        self.assertIsNone(self.engine._download_watch)
        self.assertEqual(self.engine._check_downloads(), [])

    def test_cancel_when_nothing_is_armed_says_so_without_erroring(self):
        result = self.engine.cancel_download_watch()
        self.assertIn("wasn't watching", result.lower())

    def test_disabled_via_config_never_fires_even_when_armed(self):
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        config.PROACTIVE_DOWNLOAD_WATCH_ENABLED = False
        with open(os.path.join(self.tmp, "report.pdf"), "w") as f:
            f.write("done")
        self.assertEqual(self.engine._check_downloads(), [])

    def test_check_once_includes_the_download_trigger_alongside_battery_and_calendar(self):
        """The actual Tier 15 design decision: a third trigger folded into
        the SAME engine/check_once(), not a second thread."""
        self.engine.arm_download_watch(self.tmp, timeout_minutes=5)
        with open(os.path.join(self.tmp, "report.pdf"), "w") as f:
            f.write("done")
        # Tier 18 added 6 more checks to this same check_once() loop (see
        # automation/proactive.py) - neutralized here the same way
        # battery/calendar already were, so this test (predating all of
        # them) stays about what it's actually testing: download sharing
        # check_once() with the others, not any one specific trigger count.
        with mock.patch.object(self.engine, "_check_battery", return_value=[]), \
             mock.patch.object(self.engine, "_check_calendar", return_value=[]), \
             mock.patch.object(self.engine, "_check_scheduled_workflows", return_value=[]), \
             mock.patch.object(self.engine, "_check_reminder_followups", return_value=[]), \
             mock.patch.object(self.engine, "_check_morning_briefing", return_value=[]), \
             mock.patch.object(self.engine, "_check_idle_break", return_value=[]), \
             mock.patch.object(self.engine, "_check_streak_milestones", return_value=[]), \
             mock.patch.object(self.engine, "_check_habit_detection", return_value=[]), \
             mock.patch.object(self.engine, "_check_low_disk", return_value=[]):
            nudges = self.engine.check_once()
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0].kind, "download")


class AppControllerDownloadWatchTests(unittest.TestCase):
    """The thin AppController wrappers - delegation + audit only; the real
    logic is DownloadWatchEngineTests above."""

    def setUp(self):
        self.audit = Audit(store=MemoryStore())
        self.saved_audit = audit_mod.audit
        audit_mod.audit = self.audit
        self.addCleanup(lambda: setattr(audit_mod, "audit", self.saved_audit))

    def _last(self):
        return self.audit.recent(1)[0]

    def test_watch_for_download_delegates_with_no_folder_argument(self):
        with mock.patch("automation.app_control.proactive_engine.arm_download_watch",
                         return_value="Watching ~/Downloads for a new download...") as arm:
            result = AppController.watch_for_download()
        arm.assert_called_once_with(None, None)
        self.assertEqual(result, "Watching ~/Downloads for a new download...")
        self.assertEqual(self._last().tool, "watch_for_download")

    def test_watch_for_download_resolves_a_bare_folder_name_relative_to_home(self):
        with mock.patch("automation.app_control.proactive_engine.arm_download_watch",
                         return_value="ok") as arm:
            AppController.watch_for_download("Desktop", 15)
        arm.assert_called_once_with(os.path.join(os.path.expanduser("~"), "Desktop"), 15)

    def test_watch_for_download_leaves_an_absolute_path_untouched(self):
        with mock.patch("automation.app_control.proactive_engine.arm_download_watch",
                         return_value="ok") as arm:
            AppController.watch_for_download("/tmp/some/folder", None)
        arm.assert_called_once_with("/tmp/some/folder", None)

    def test_watch_for_download_expands_a_tilde_path(self):
        with mock.patch("automation.app_control.proactive_engine.arm_download_watch",
                         return_value="ok") as arm:
            AppController.watch_for_download("~/Desktop", None)
        arm.assert_called_once_with(os.path.join(os.path.expanduser("~"), "Desktop"), None)

    def test_a_failure_message_from_the_engine_is_audited_as_failed(self):
        with mock.patch("automation.app_control.proactive_engine.arm_download_watch",
                         return_value="Couldn't watch /no/such/folder: not found"):
            AppController.watch_for_download("/no/such/folder")
        self.assertEqual(self._last().status, "failed")

    def test_cancel_download_watch_delegates_and_audits(self):
        with mock.patch("automation.app_control.proactive_engine.cancel_download_watch",
                         return_value="Stopped watching /x for a download.") as cancel:
            result = AppController.cancel_download_watch()
        cancel.assert_called_once_with()
        self.assertEqual(result, "Stopped watching /x for a download.")
        self.assertEqual(self._last().tool, "cancel_download_watch")


if __name__ == "__main__":
    unittest.main()
