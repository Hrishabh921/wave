"""
tests/test_tier14_volume_brightness.py — Tier 14: volume (set_volume/
volume_max/volume_min/volume_up/volume_down/mute_volume/unmute_volume) and
brightness (set_brightness/brightness_max/brightness_min/brightness_up/
brightness_down/get_brightness_status).

Same split as test_tier14_power_session.py: AppController-level logic
(captured-prior-state undo for mute/unmute and for a plain set_volume/
set_brightness, the "mostly delegates to set_volume/set_brightness, with a
direction-named exception only on the no-real-backend fallback path" tool-
naming fix, and plain NotSupported handling) is tested against a minimal
fake backend. WindowsVolumeBrightnessTests/LinuxVolumeBrightnessTests/
MacVolumeBrightnessTests cover a representative sample of the real per-OS
command construction, including a dedicated regression test for this
session's HAS_PYCAW NameError fix in automation/backends/windows.py.

Run: python -m unittest discover -s tests -p "test_tier14_volume_brightness.py"
(pytest will also collect it.)
"""
import sys
import types
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
from automation.backends.linux import LinuxBackend                 # noqa: E402
from automation.backends.macos import MacBackend                   # noqa: E402
from automation.backends.windows import WindowsBackend             # noqa: E402


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


class _FakeVolBackend(PlatformBackend):
    """Minimal stand-in exercising ONLY AppController's own volume/
    brightness logic - real per-OS command construction is covered by the
    Windows/Linux/Mac test classes further down instead. `raise_on` names
    which primitives should raise NotSupported when called (no capabilities()
    gating is involved anywhere in AppController's volume/brightness code -
    every method there just try/excepts the backend call directly - so,
    unlike Tier 14's power/session section, a plain instance flag is
    faithful here; no need for the "genuinely not overridden" subclass
    trick test_tier14_power_session.py's capabilities-check test needed."""
    name = "fakeos"
    display_name = "FakeOS"

    def __init__(self, volume=50, muted=False, brightness=50, raise_on=()):
        self.calls = []
        self._volume = volume
        self._muted = muted
        self._brightness = brightness
        self._raise_on = set(raise_on)

    def _check(self, feature, what):
        if feature in self._raise_on:
            raise NotSupported(f"{what} isn't supported on FakeOS yet.")

    def get_volume(self):
        self._check("get_volume", "Reading the system volume")
        return self._volume

    def set_volume(self, percent):
        self._check("set_volume", "Setting an exact system volume")
        self._volume = max(0, min(100, int(percent)))
        self.calls.append(("set_volume", self._volume))
        return f"Volume set to {self._volume}%."

    def is_muted(self):
        self._check("is_muted", "Reading whether audio is muted")
        return self._muted

    def mute_volume(self):
        self._check("mute_volume", "Muting system audio")
        self._muted = True
        self.calls.append(("mute_volume",))
        return "Muted."

    def unmute_volume(self):
        self._check("unmute_volume", "Unmuting system audio")
        self._muted = False
        self.calls.append(("unmute_volume",))
        return "Unmuted."

    def get_brightness(self):
        self._check("get_brightness", "Reading screen brightness")
        return self._brightness

    def set_brightness(self, percent):
        self._check("set_brightness", "Setting screen brightness")
        self._brightness = max(0, min(100, int(percent)))
        self.calls.append(("set_brightness", self._brightness))
        return f"Brightness set to {self._brightness}%."


class VolBrightTestBase(unittest.TestCase):
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


class SetVolumeTests(VolBrightTestBase):
    def test_sets_the_exact_percent_and_captures_the_previous_one_for_undo(self):
        backend = self._use(_FakeVolBackend(volume=30))
        result = AppController.set_volume(70)
        self.assertEqual(result, "Volume set to 70%.")
        self.assertEqual(backend._volume, 70)
        row = self._last()
        self.assertEqual((row.tool, row.status, row.args), ("set_volume", "ok", {"percent": 70}))
        self.assertEqual(row.undo_kind, "restore_volume")
        self.assertEqual(row.undo_payload, {"percent": 30})
        self.assertEqual(row.undo_label, "Set the volume back to 30%")

    def test_volume_max_and_min_go_through_set_volume(self):
        backend = self._use(_FakeVolBackend(volume=40))
        AppController.volume_max()
        self.assertEqual(backend._volume, 100)
        AppController.volume_min()
        self.assertEqual(backend._volume, 0)
        self.assertEqual(self._last().undo_payload, {"percent": 100})  # captured from just before volume_min

    def test_not_supported_fails_cleanly_with_no_undo_registered(self):
        self._use(_FakeVolBackend(raise_on=("set_volume",)))
        result = AppController.set_volume(70)
        self.assertIn("isn't supported on FakeOS", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)


class NudgeVolumeTests(VolBrightTestBase):
    """volume_up/volume_down MOSTLY delegate to set_volume (which audits
    under ITS OWN name, "set_volume" - see AppController's own comment on
    this) - they only keep their own tool identity on the no-real-backend
    fallback path, where there's no resulting percent to report."""

    def test_happy_path_delegates_to_set_volume_and_is_audited_as_such(self):
        backend = self._use(_FakeVolBackend(volume=50))
        AppController.volume_up()
        self.assertEqual(backend._volume, 60)
        row = self._last()
        self.assertEqual((row.tool, row.args), ("set_volume", {"percent": 60}))
        self.assertEqual(row.undo_payload, {"percent": 50})

    def test_volume_down_delegates_too(self):
        backend = self._use(_FakeVolBackend(volume=50))
        AppController.volume_down()
        self.assertEqual(backend._volume, 40)
        self.assertEqual(self._last().tool, "set_volume")

    def test_clamps_at_100_and_0(self):
        backend = self._use(_FakeVolBackend(volume=95))
        AppController.volume_up()
        self.assertEqual(backend._volume, 100)
        backend2 = self._use(_FakeVolBackend(volume=5))
        AppController.volume_down()
        self.assertEqual(backend2._volume, 0)

    def test_fallback_when_backend_cannot_read_current_volume_is_audited_under_its_own_name(self):
        """🔧 FIX anchor: this used to always audit as "set_volume" with no
        distinguishing detail even on the fallback path, so a run of
        volume_up vs volume_down fallback nudges collapsed into an
        identical, indistinguishable list_recent_actions summary."""
        self._use(_FakeVolBackend(raise_on=("get_volume",)))
        with mock.patch("automation.app_control.pyautogui.press") as press:
            result = AppController.volume_up()
        self.assertIn("up", result.lower())
        press.assert_called_once_with("volumeup")
        row = self._last()
        self.assertEqual((row.tool, row.args, row.status), ("volume_up", {"via": "media_key"}, "ok"))

    def test_fallback_volume_down_presses_the_right_key_and_keeps_its_own_name(self):
        self._use(_FakeVolBackend(raise_on=("get_volume",)))
        with mock.patch("automation.app_control.pyautogui.press") as press:
            AppController.volume_down()
        press.assert_called_once_with("volumedown")
        self.assertEqual(self._last().tool, "volume_down")


class MuteTests(VolBrightTestBase):
    """🏆 The captured-prior-state regression: undo must restore the REAL
    prior mute state, never a blind "call the opposite" flip - which would
    be wrong the moment the call was a no-op (e.g. muting an already-muted
    device)."""

    def test_muting_when_not_muted_really_changes_it_and_undo_will_unmute(self):
        backend = self._use(_FakeVolBackend(muted=False))
        result = AppController.mute_volume()
        self.assertEqual(result, "Muted.")
        self.assertTrue(backend._muted)
        row = self._last()
        self.assertEqual((row.tool, row.undo_kind), ("mute_volume", "restore_mute_state"))
        self.assertEqual(row.undo_payload, {"was_muted": False})
        self.assertEqual(row.undo_label, "Unmute it again")

    def test_muting_when_already_muted_is_a_no_op_but_undo_still_says_mute_again(self):
        """The key case: calling mute_volume() on an already-muted device
        must NOT produce the blind-opposite "Unmute it again" label - the
        prior state really was muted, so restoring it means muting again."""
        backend = self._use(_FakeVolBackend(muted=True))
        AppController.mute_volume()
        self.assertTrue(backend._muted)
        row = self._last()
        self.assertEqual(row.undo_payload, {"was_muted": True})
        self.assertEqual(row.undo_label, "Mute it again")

    def test_unmuting_when_muted_really_changes_it_and_undo_will_mute(self):
        backend = self._use(_FakeVolBackend(muted=True))
        result = AppController.unmute_volume()
        self.assertEqual(result, "Unmuted.")
        self.assertFalse(backend._muted)
        row = self._last()
        self.assertEqual((row.tool, row.undo_payload, row.undo_label),
                         ("unmute_volume", {"was_muted": True}, "Mute it again"))

    def test_unmuting_when_already_unmuted_is_a_no_op_but_undo_still_says_unmute_again(self):
        backend = self._use(_FakeVolBackend(muted=False))
        AppController.unmute_volume()
        self.assertFalse(backend._muted)
        self.assertEqual(self._last().undo_label, "Unmute it again")

    def test_not_supported_fails_with_no_undo(self):
        self._use(_FakeVolBackend(raise_on=("is_muted",)))
        result = AppController.mute_volume()
        self.assertIn("isn't supported on FakeOS", result)
        row = self._last()
        self.assertEqual(row.status, "failed")
        self.assertIsNone(row.undo_kind)


class SetBrightnessTests(VolBrightTestBase):
    def test_sets_the_exact_percent_and_captures_the_previous_one_for_undo(self):
        backend = self._use(_FakeVolBackend(brightness=20))
        result = AppController.set_brightness(80)
        self.assertEqual(result, "Brightness set to 80%.")
        self.assertEqual(backend._brightness, 80)
        row = self._last()
        self.assertEqual((row.tool, row.status, row.args), ("set_brightness", "ok", {"percent": 80}))
        self.assertEqual(row.undo_kind, "restore_brightness")
        self.assertEqual(row.undo_payload, {"percent": 20})
        self.assertEqual(row.undo_label, "Set brightness back to 20%")

    def test_brightness_max_and_min_go_through_set_brightness(self):
        backend = self._use(_FakeVolBackend(brightness=40))
        AppController.brightness_max()
        self.assertEqual(backend._brightness, 100)
        AppController.brightness_min()
        self.assertEqual(backend._brightness, 0)

    def test_not_supported_fails_cleanly(self):
        self._use(_FakeVolBackend(raise_on=("set_brightness",)))
        result = AppController.set_brightness(80)
        self.assertIn("isn't supported on FakeOS", result)
        self.assertEqual(self._last().status, "failed")


class NudgeBrightnessTests(VolBrightTestBase):
    def test_happy_path_delegates_to_set_brightness(self):
        backend = self._use(_FakeVolBackend(brightness=50))
        AppController.brightness_up()
        self.assertEqual(backend._brightness, 60)
        self.assertEqual(self._last().tool, "set_brightness")

    def test_brightness_down_delegates_too(self):
        backend = self._use(_FakeVolBackend(brightness=50))
        AppController.brightness_down()
        self.assertEqual(backend._brightness, 40)

    def test_clamps_at_100_and_0(self):
        backend = self._use(_FakeVolBackend(brightness=95))
        AppController.brightness_up()
        self.assertEqual(backend._brightness, 100)

    def test_no_media_key_fallback_exists_so_an_unsupported_read_just_fails(self):
        """Unlike volume, brightness has no universal hardware key to fall
        back on - get_brightness() failing is a hard, immediate failure,
        audited under brightness_up/brightness_down's own name."""
        self._use(_FakeVolBackend(raise_on=("get_brightness",)))
        result = AppController.brightness_up()
        self.assertIn("isn't supported on FakeOS", result)
        row = self._last()
        self.assertEqual((row.tool, row.status), ("brightness_up", "failed"))


class GetBrightnessStatusTests(VolBrightTestBase):
    def test_reports_the_current_percent_and_is_a_quiet_read(self):
        """get_brightness_status is in QUIET_TOOLS (automation/audit.py) -
        a successful, non-reversible quiet-tool call is deliberately NOT
        recorded at all, so it doesn't clutter list_recent_actions with
        plain reads the same way get_pc_info doesn't. recent() must stay
        genuinely empty, not just unchecked."""
        self._use(_FakeVolBackend(brightness=65))
        self.assertEqual(AppController.get_brightness_status(), "Screen brightness is at 65%.")
        self.assertEqual(self.audit.recent(5), [])

    def test_not_supported_fails_cleanly(self):
        self._use(_FakeVolBackend(raise_on=("get_brightness",)))
        self.assertIn("isn't supported on FakeOS", AppController.get_brightness_status())


# =============================================================================
# Real backend command construction - a representative sample.
# =============================================================================
class WindowsVolumeBrightnessTests(unittest.TestCase):
    def test_without_pycaw_every_volume_method_fails_honestly_not_with_a_nameerror(self):
        """🏆 THE regression test for this session's fix: _volume_endpoint()
        referenced HAS_PYCAW as a bare name that was never defined anywhere
        in windows.py, so every volume call on a real Windows machine raised
        a NameError (silently re-wrapped by each caller's own broad `except
        Exception` into a wrong, confusing message) regardless of whether
        pycaw was actually installed. Explicitly forcing HAS_PYCAW False
        here (rather than relying on this sandbox's own lack of pycaw)
        keeps this test meaningful even in a future sandbox that has it."""
        b = WindowsBackend()
        with mock.patch("automation.backends.windows.HAS_PYCAW", False):
            with self.assertRaises(NotSupported) as ctx:
                b.get_volume()
        msg = str(ctx.exception)
        self.assertIn("pycaw", msg)
        self.assertNotIn("HAS_PYCAW", msg)  # the old, broken message literally said this

    def test_get_and_set_volume_call_the_real_pycaw_endpoint(self):
        """A fake pycaw.pycaw module stands in (pycaw genuinely isn't
        installed in this sandbox) so the actual GetMasterVolumeLevelScalar/
        SetMasterVolumeLevelScalar/SetMute plumbing can be checked for real,
        with HAS_PYCAW forced True to reach past the check above."""
        pycaw_mod = S.install_fake_module("pycaw")
        pycaw_pycaw_mod = S.install_fake_module("pycaw.pycaw")

        class _FakeEndpoint:
            def __init__(self):
                self.calls = []
                self.vol = 0.42
                self.muted = 0

            def GetMasterVolumeLevelScalar(self):
                return self.vol

            def SetMasterVolumeLevelScalar(self, value, _guid):
                self.calls.append(("SetMasterVolumeLevelScalar", value, _guid))

            def GetMute(self):
                return self.muted

            def SetMute(self, value, _guid):
                self.calls.append(("SetMute", value, _guid))

        endpoint = _FakeEndpoint()

        class _FakeAudioUtilities:
            @staticmethod
            def GetSpeakers():
                return types.SimpleNamespace(EndpointVolume=endpoint)

        pycaw_pycaw_mod.AudioUtilities = _FakeAudioUtilities
        b = WindowsBackend()
        with mock.patch("automation.backends.windows.HAS_PYCAW", True):
            self.assertEqual(b.get_volume(), 42)
            self.assertEqual(b.set_volume(77), "Volume set to 77%.")
            self.assertEqual(endpoint.calls[-1], ("SetMasterVolumeLevelScalar", 0.77, None))
            b.mute_volume()
            self.assertEqual(endpoint.calls[-1], ("SetMute", 1, None))
            b.unmute_volume()
            self.assertEqual(endpoint.calls[-1], ("SetMute", 0, None))

    def test_without_screen_brightness_control_brightness_fails_honestly(self):
        """No HAS_* guard for this one (unlike pycaw) - a plain local
        `import screen_brightness_control` wrapped in `except Exception`,
        so a missing package is already a graceful NotSupported, confirmed
        against this sandbox's own genuine absence of the package. Hides
        the package for the test's duration and restores the sys.modules
        entry after - another test in this file installs a FAKE module
        under this same name, and sys.modules is process-global, so this
        test must not depend on running before that one."""
        # A None entry makes the import itself fail. Just popping the entry
        # let a machine that HAS the package (the owner's PC) import the real
        # one - and read the real screen's brightness - instead.
        with mock.patch.dict(sys.modules, {"screen_brightness_control": None}):
            b = WindowsBackend()
            with self.assertRaises(NotSupported) as ctx:
                b.get_brightness()
            self.assertIn("brightness", str(ctx.exception).lower())

    def test_set_brightness_calls_the_real_helper_when_available(self):
        # install_fake_module() only APPLIES its kwargs the first time a
        # given name is inserted - a second call with this same name
        # (another test here, or a module already cached from a prior
        # test run in this process) just returns the existing module
        # as-is. Setting the attributes directly on the returned
        # reference, rather than trusting the constructor kwargs, keeps
        # this test correct regardless of what ran before it.
        fake_sbc = S.install_fake_module("screen_brightness_control")
        fake_sbc.get_brightness = lambda display=0: [55]
        fake_sbc.set_brightness = mock.Mock()
        b = WindowsBackend()
        self.assertEqual(b.get_brightness(), 55)
        self.assertEqual(b.set_brightness(90), "Brightness set to 90%.")
        fake_sbc.set_brightness.assert_called_once_with(90, display=0)


class LinuxVolumeBrightnessTests(unittest.TestCase):
    def test_get_and_set_volume_prefer_pactl_over_amixer(self):
        sh = _FakeShell(installed={"pactl", "amixer"}, results={
            ("pactl", "get-sink-volume", "@DEFAULT_SINK@"): (0, "Volume: front-left: 32768 /  50% / ...")})
        b = LinuxBackend(sh)
        self.assertEqual(b.get_volume(), 50)
        b.set_volume(65)
        self.assertEqual(sh.calls[-1], ("run", ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "65%"]))

    def test_get_volume_falls_back_to_amixer_without_pactl(self):
        sh = _FakeShell(installed={"amixer"}, results={("amixer", "get", "Master"): (0, "  Mono: 70 [70%] [on]")})
        self.assertEqual(LinuxBackend(sh).get_volume(), 70)

    def test_mute_and_unmute_via_pactl(self):
        sh = _FakeShell(installed={"pactl"})
        b = LinuxBackend(sh)
        self.assertEqual(b.mute_volume(), "Muted.")
        self.assertEqual(sh.calls[-1], ("run", ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"]))
        self.assertEqual(b.unmute_volume(), "Unmuted.")
        self.assertEqual(sh.calls[-1], ("run", ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"]))

    def test_is_muted_reads_amixer_on_off_without_pactl(self):
        sh = _FakeShell(installed={"amixer"}, results={("amixer", "get", "Master"): (0, "Mono: 50 [50%] [off]")})
        self.assertTrue(LinuxBackend(sh).is_muted())

    def test_without_pactl_or_amixer_volume_is_not_supported(self):
        sh = _FakeShell()
        with self.assertRaises(NotSupported):
            LinuxBackend(sh).get_volume()
        with self.assertRaises(NotSupported):
            LinuxBackend(sh).mute_volume()

    def test_brightness_delegates_to_screen_brightness_control(self):
        # See WindowsVolumeBrightnessTests.test_set_brightness_calls_the_
        # real_helper_when_available's own comment on why the attributes
        # are set directly rather than via install_fake_module()'s kwargs.
        fake_sbc = S.install_fake_module("screen_brightness_control")
        fake_sbc.get_brightness = lambda display=0: [33]
        fake_sbc.set_brightness = mock.Mock()
        b = LinuxBackend(_FakeShell())
        self.assertEqual(b.get_brightness(), 33)
        b.set_brightness(10)
        fake_sbc.set_brightness.assert_called_once_with(10, display=0)


class MacVolumeBrightnessTests(unittest.TestCase):
    def test_get_and_set_volume_via_osascript(self):
        sh = _FakeShell(results={
            ("osascript", "-e", "output volume of (get volume settings)"): (0, "37")})
        b = MacBackend(sh)
        self.assertEqual(b.get_volume(), 37)
        self.assertEqual(b.set_volume(80), "Volume set to 80%.")
        self.assertEqual(sh.calls[-1], ("run", ["osascript", "-e", "set volume output volume 80"]))

    def test_mute_and_unmute_and_is_muted_via_osascript(self):
        sh = _FakeShell(results={
            ("osascript", "-e", "output muted of (get volume settings)"): (0, "true")})
        b = MacBackend(sh)
        self.assertTrue(b.is_muted())
        self.assertEqual(b.mute_volume(), "Muted.")
        self.assertEqual(sh.calls[-1], ("run", ["osascript", "-e", "set volume output muted true"]))
        self.assertEqual(b.unmute_volume(), "Unmuted.")
        self.assertEqual(sh.calls[-1], ("run", ["osascript", "-e", "set volume output muted false"]))

    def test_get_volume_not_a_number_is_not_supported(self):
        sh = _FakeShell(results={
            ("osascript", "-e", "output volume of (get volume settings)"): (1, "")})
        with self.assertRaises(NotSupported):
            MacBackend(sh).get_volume()


if __name__ == "__main__":
    unittest.main()
