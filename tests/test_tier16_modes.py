"""
tests/test_tier16_modes.py — Tier 16: AppController.enable_mode/
disable_mode/get_active_mode (the study/gaming/focus/dnd/sleep settings
bundles), plus the brain/handlers/productivity.py skill wrappers around
them.

Reuses test_tier14_volume_brightness.py's _FakeVolBackend/VolBrightTestBase
fixture directly (same cross-file reuse test_tier15_taint_guard.py already
does with test_phase7_confirm_gate.py's ConfirmGateBase) - modes are built
entirely on the same volume/mute/brightness primitives that fixture
already exercises, so there's nothing mode-specific to add to it.

Undo is checked the same way test_tier14_volume_brightness.py/
test_tier14_window_mgmt.py already check volume/brightness/window undo:
shallowly, against the recorded undo_kind/undo_payload on the audit row,
plus calling the registered handler function directly as a plain
function. No existing test in this codebase round-trips a real
undo_manager.undo() call for any of AppController's own registered undo
kinds (restore_volume, restore_brightness, ..., and now
restore_mode_settings) - see automation/undo.py's module docstring and
automation/app_control.py's own undo_mod.undo_manager.register(...) calls,
which only ever run once, at that module's IMPORT time, against whatever
singleton object existed then. Replacing undo_mod.undo_manager afterward
(what a naive test might try) does not carry those registrations onto a
fresh instance, so this file follows the same established, shallower
precedent rather than inventing a new singleton-patching test pattern.

Run: python -m unittest discover -s tests -p "test_tier16_modes.py"
(pytest will also collect it.)
"""
import unittest

import _phase4_support as S  # noqa: E402  (adds the repo root to sys.path)

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from automation import app_control                                  # noqa: E402
from automation.app_control import AppController, _MODE_BUNDLES, _active_mode  # noqa: E402
from automation.backends.base import NotSupported                   # noqa: E402
from brain.skills_registry import skills                            # noqa: E402
import brain.handlers  # noqa: E402,F401  (side effect: registers every built-in skill)

from test_tier14_volume_brightness import _FakeVolBackend, VolBrightTestBase  # noqa: E402


class _ModesTestBase(VolBrightTestBase):
    def setUp(self):
        super().setUp()
        # _active_mode is module-level state in automation/app_control.py,
        # not per-backend - reset it so one test's enabled mode can't leak
        # into the next.
        _active_mode["name"], _active_mode["snapshot"] = None, None
        self.addCleanup(lambda: _active_mode.update(name=None, snapshot=None))


class ModeBundlesTests(_ModesTestBase):
    """The bundles dict itself - what each mode actually changes."""
    def test_every_mode_bundle_is_a_known_subset_of_volume_mute_brightness(self):
        for name, bundle in _MODE_BUNDLES.items():
            self.assertTrue(set(bundle) <= {"volume", "mute", "brightness"}, msg=name)

    def test_sleep_is_an_alias_for_dnd_not_a_different_combination(self):
        # Roadmap names this bundle "sleep/DND" - one bundle, two names.
        self.assertEqual(_MODE_BUNDLES["sleep"], _MODE_BUNDLES["dnd"])

    def test_focus_matches_the_pre_existing_gui_focus_toggle_mute_only(self):
        self.assertEqual(_MODE_BUNDLES["focus"], {"mute": True})


class EnableModeTests(_ModesTestBase):
    def test_enabling_a_known_mode_applies_every_setting_in_its_bundle(self):
        backend = self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        result = AppController.enable_mode("study")
        self.assertIn("Study mode on", result)
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (20, False, 75))

    def test_unknown_mode_name_lists_the_real_ones_and_changes_nothing(self):
        backend = self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        result = AppController.enable_mode("party")
        self.assertIn("don't have a 'party' mode", result)
        for name in ("dnd", "focus", "gaming", "sleep", "study"):
            self.assertIn(name, result)
        self.assertEqual(backend.calls, [])

    def test_enabling_records_restore_mode_settings_as_the_undo_kind(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        AppController.enable_mode("gaming")
        row = self._last()
        self.assertEqual(row.undo_kind, "restore_mode_settings")
        self.assertEqual(row.undo_payload,
                         {"volume_percent": 50, "was_muted": False, "brightness_percent": 50})

    def test_enabling_sets_get_active_mode_and_the_label_is_title_cased(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        AppController.enable_mode("dnd")
        self.assertEqual(AppController.get_active_mode(), "Dnd mode is on.")

    def test_enabling_the_sleep_alias_reports_its_own_name_not_dnd(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        result = AppController.enable_mode("sleep")
        self.assertIn("Sleep mode on", result)
        self.assertEqual(AppController.get_active_mode(), "Sleep mode is on.")

    def test_name_is_case_and_whitespace_insensitive(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        result = AppController.enable_mode("  STUDY  ")
        self.assertIn("Study mode on", result)


class EnableModePartialFailureRollbackTests(_ModesTestBase):
    """The real bug this tier's own smoke-testing caught before shipping:
    enable_mode applies several backend settings sequentially inside ONE
    audited action - a NotSupported raised partway through (after an
    earlier setting already succeeded) must roll back what already
    applied, since act.fail() never calls act.set_undo() and would
    otherwise leave the backend silently half-changed with no recorded
    way back."""
    def test_brightness_failing_after_volume_and_mute_succeeded_rolls_both_back(self):
        # study = {"mute": False, "volume": 20, "brightness": 75} - volume
        # and mute apply fine, brightness is the one that raises.
        backend = self._use(_FakeVolBackend(volume=40, muted=False, brightness=60,
                                            raise_on=("set_brightness",)))
        result = AppController.enable_mode("study")
        self.assertIn("isn't supported on FakeOS", result)
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (40, False, 60))
        # Check the audit row before calling get_active_mode() below, which
        # is itself an audited (read-only) action and would otherwise
        # become the new "last" row.
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)
        self.assertEqual(AppController.get_active_mode(), "No mode is currently on.")

    def test_mute_failing_as_the_very_first_touched_setting_touches_nothing(self):
        # dnd = {"mute": True, "brightness": 15} - no "volume" key, so mute
        # is the first (and here, failing) setting attempted.
        backend = self._use(_FakeVolBackend(volume=10, muted=False, brightness=90,
                                            raise_on=("mute_volume",)))
        result = AppController.enable_mode("dnd")
        self.assertIn("isn't supported on FakeOS", result)
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (10, False, 90))
        self.assertEqual(backend.calls, [])  # nothing had succeeded yet, so nothing to roll back

    def test_rollback_itself_tolerates_a_backend_that_cant_undo_either(self):
        # Pathological but shouldn't raise out of enable_mode: if even the
        # rollback call fails, the rollback's own try/except swallows it
        # (we're already reporting failure either way). dnd = {"mute":
        # True, "brightness": 15} - mute succeeds, brightness raises AND
        # can't be rolled back either (set_brightness itself is blocked).
        backend = self._use(_FakeVolBackend(volume=40, muted=False, brightness=60,
                                            raise_on=("set_brightness",)))
        result = AppController.enable_mode("dnd")
        self.assertIn("isn't supported on FakeOS", result)
        # mute DID succeed and WAS rolled back (unmute); brightness is
        # always applied last, so the setting that raises never has
        # anything of its own to roll back - its failing call never
        # mutated the backend in the first place.
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (40, False, 60))


class DisableModeTests(_ModesTestBase):
    def test_disabling_restores_the_exact_pre_enable_snapshot(self):
        backend = self._use(_FakeVolBackend(volume=33, muted=True, brightness=77))
        AppController.enable_mode("gaming")
        self.assertNotEqual((backend._volume, backend._muted, backend._brightness), (33, True, 77))
        result = AppController.disable_mode()
        self.assertEqual(result, "Gaming mode off.")
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (33, True, 77))
        self.assertEqual(AppController.get_active_mode(), "No mode is currently on.")

    def test_disabling_with_nothing_on_says_so_and_touches_nothing(self):
        backend = self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        result = AppController.disable_mode()
        self.assertEqual(result, "No mode is currently on.")
        self.assertEqual(backend.calls, [])

    def test_disabling_is_correct_even_if_something_else_changed_settings_in_between(self):
        # The reason _active_mode is its own explicit snapshot rather than
        # "replay the audit log's last reversible action": that lookup
        # would point at the WRONG action once something else reversible
        # has happened since enabling the mode.
        backend = self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        AppController.enable_mode("focus")  # snapshot: volume=50, muted=False, brightness=50
        AppController.set_volume(99)        # unrelated change after the mode was enabled
        result = AppController.disable_mode()
        self.assertEqual(result, "Focus mode off.")
        self.assertEqual(backend._muted, False)  # restored from the mode's own snapshot
        self.assertEqual(backend._volume, 50)    # not left at 99


class GetActiveModeTests(_ModesTestBase):
    def test_reports_none_on_a_clean_start(self):
        self._use(_FakeVolBackend())
        self.assertEqual(AppController.get_active_mode(), "No mode is currently on.")


class RestoreModeSettingsUndoHandlerTests(_ModesTestBase):
    """The registered undo-kind handler, called directly as a plain
    function - see this file's own module docstring for why this is
    checked this way rather than through a full undo_manager.undo() round
    trip."""
    def test_handler_is_registered_under_the_exact_kind_enable_mode_records(self):
        self.assertIn("restore_mode_settings", app_control.undo_mod.undo_manager._handlers)

    def test_handler_restores_every_captured_setting(self):
        backend = self._use(_FakeVolBackend(volume=10, muted=True, brightness=10))
        AppController.enable_mode("gaming")  # volume=100, muted=False, brightness=100
        payload = self._last().undo_payload
        ok, message = app_control._undo_restore_mode_settings(payload)
        self.assertTrue(ok)
        self.assertIn("Restored", message)
        self.assertEqual((backend._volume, backend._muted, backend._brightness), (10, True, 10))

    def test_handler_skips_any_setting_that_was_none_in_the_snapshot(self):
        backend = self._use(_FakeVolBackend(volume=10, muted=True, brightness=10,
                                            raise_on=("get_brightness",)))
        AppController.enable_mode("focus")  # mute-only bundle; brightness unreadable -> None in snapshot
        payload = self._last().undo_payload
        self.assertIsNone(payload["brightness_percent"])
        backend._brightness = 42  # prove the handler really skips it, not "coincidentally restores 42"
        ok, _ = app_control._undo_restore_mode_settings(payload)
        self.assertTrue(ok)
        self.assertEqual(backend._brightness, 42)


class ModeSkillWrapperTests(_ModesTestBase):
    def _call(self, _tool, **kwargs):
        return skills.get_handler(_tool)(None, kwargs)

    def test_enable_mode_skill(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        self.assertIn("Study mode on", self._call("enable_mode", name="study"))

    def test_disable_mode_skill(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        self._call("enable_mode", name="study")
        self.assertEqual(self._call("disable_mode"), "Study mode off.")

    def test_get_active_mode_skill(self):
        self._use(_FakeVolBackend(volume=50, muted=False, brightness=50))
        self.assertEqual(self._call("get_active_mode"), "No mode is currently on.")


if __name__ == "__main__":
    unittest.main()
