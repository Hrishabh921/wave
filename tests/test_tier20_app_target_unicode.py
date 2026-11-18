"""Tier 20 item #89: Unicode-aware app/process/window/service target
normalization in WaveBrain._validate_tool_args() (docs/ROADMAP_BEAT_SARA.md's
"Bring back the lost validate_app_target() (Unicode-aware)" bullet).

See brain/groq_core.py's own comment above _APP_TARGET_ARGS for the design:
not a second copy of SARA's validate_app_target() or of
automation/app_control.py's _CRITICAL_PROCESS_NAMES/_CRITICAL_SERVICE_NAMES
(planning/schema.py already explains why that was deliberately never
duplicated) - just NFKC compatibility normalization + the same invisible-
character stripping brain/content_trust.py already does, applied in place
to the one handful of args that name an app/process/window/service, so
every existing critical-name check downstream sees the plain form.

Uses the same ConfirmGateBase harness (real WaveBrain, fake OpenAI server,
isolated db/audit/undo) as tests/test_phase7_confirm_gate.py.
"""
import os
import sys

import _phase4_support as S

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                       hotkey=lambda *a, **k: None, write=lambda *a, **k: None,
                       typewrite=lambda *a, **k: None, screenshot=lambda *a, **k: None,
                       size=lambda: (1, 1))
S.install_fake_module("pywhatkit")

sys.path.insert(0, os.path.dirname(__file__))
from test_phase7_confirm_gate import ConfirmGateBase  # noqa: E402


class AppTargetUnicodeNormalizationTests(ConfirmGateBase):
    def test_fullwidth_app_name_is_normalized_to_plain_ascii(self):
        args = {"app_name": "ｅｘｐｌｏｒｅｒ"}  # fullwidth "explorer"
        self.brain._validate_tool_args("close_app", args)
        self.assertEqual(args["app_name"], "explorer")

    def test_zero_width_characters_are_stripped_from_app_name(self):
        args = {"app_name": "exp​lor‌er"}  # zero-width space/non-joiner inside "explorer"
        self.brain._validate_tool_args("close_app", args)
        self.assertEqual(args["app_name"], "explorer")

    def test_unicode_tag_characters_are_stripped_from_service_name(self):
        tagged = "sshd" + "\U000e0041\U000e0042"  # Unicode tag characters appended
        args = {"service_name": tagged}
        self.brain._validate_tool_args("stop_service", args)
        self.assertEqual(args["service_name"], "sshd")

    def test_window_name_is_normalized_too(self):
        args = {"window_name": "Ｎｏｔｅｐａｄ"}  # fullwidth "Notepad"
        self.brain._validate_tool_args("focus_window", args)
        self.assertEqual(args["window_name"], "Notepad")

    def test_untouched_tool_args_are_left_alone(self):
        """A tool with no app/process/window/service target (e.g.
        remember_this) must not have any of its arguments touched."""
        args = {"key": "fa​vorite", "value": "blue"}
        self.brain._validate_tool_args("remember_this", args)
        self.assertEqual(args["key"], "fa​vorite")

    def test_non_string_arg_value_is_left_alone_not_crashed_on(self):
        args = {"app_name": 12345}
        # Should not raise.
        self.brain._validate_tool_args("close_app", args)
        self.assertEqual(args["app_name"], 12345)

    def test_ordinary_plain_app_name_is_unaffected(self):
        args = {"app_name": "notepad"}
        self.brain._validate_tool_args("close_app", args)
        self.assertEqual(args["app_name"], "notepad")

    def test_normalize_helper_never_raises_on_garbage(self):
        self.assertEqual(self.brain._normalize_app_target(None), "")  # None -> "" like "" -> ""
        self.assertEqual(self.brain._normalize_app_target(""), "")

    def test_fullwidth_close_app_still_refuses_a_disguised_critical_process(self):
        """End-to-end: a close_app call whose app_name is a disguised
        (fullwidth) form of a critical process name must still be refused
        once normalized - exercised via the real close_app handler against
        a process list that genuinely contains 'explorer.exe'."""
        from unittest import mock
        import automation.app_control as app_control

        class FakeProc:
            def __init__(self, pid, name):
                self.info = {"pid": pid, "name": name}

        fake_processes = [FakeProc(111, "explorer.exe")]
        with mock.patch.object(app_control.psutil, "process_iter", return_value=fake_processes):
            args = {"app_name": "ｅｘｐｌｏｒｅｒ"}
            self.brain._validate_tool_args("close_app", args)
            result = app_control.AppController.close_app(args["app_name"])
        self.assertIn("critical system process", result)


if __name__ == "__main__":
    import unittest
    unittest.main()
