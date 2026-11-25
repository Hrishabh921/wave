"""
tests/test_tier43_app_index.py — Tier 43: open any installed app at once.

"open X" drove the Start menu (press Win, type, Enter - a second of waits,
and it could type into the wrong window). SARA keeps an index of installed
apps; automation/app_index.py does that for Wave from Windows' own list
(Get-StartApps) and launches through shell:AppsFolder. Windows is never
asked here - a stand-in answers instead.

    python -m unittest discover -s tests -p "test_tier43_app_index.py"
"""
import json
import os
import sys
import unittest
from unittest import mock

import _phase4_support as S  # noqa: E402

from automation import app_index as ai  # noqa: E402
from settings.config import config  # noqa: E402

_APPS = [
    {"Name": "Google Chrome", "AppID": "Chrome"},
    {"Name": "Notepad", "AppID": "Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"},
    {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
    {"Name": "Visual Studio Code", "AppID": "Microsoft.VisualStudioCode"},
    {"Name": "Uninstall Visual Studio Code", "AppID": "C:\\VSCode\\unins000.exe"},
    {"Name": "Word", "AppID": "Microsoft.Office.WINWORD.EXE.15"},
    {"Name": "WordPad", "AppID": "Microsoft.Windows.WordPad"},
    {"Name": "WhatsApp", "AppID": "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"},
    {"Name": "File Explorer", "AppID": "Microsoft.Windows.Explorer"},
]


class _Result:
    def __init__(self, stdout):
        self.stdout = stdout


class AppIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        p = mock.patch.object(config, "APP_INDEX", True)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch.object(ai.sys, "platform", "win32")
        p2.start()
        self.addCleanup(p2.stop)
        self.calls = []
        self.now = [1_000_000.0]
        self.idx = self._index()
        self.idx.build_now()

    def _index(self):
        def runner(cmd, **kw):
            self.calls.append(cmd)
            return _Result(json.dumps(_APPS))
        return ai.AppIndex(path=os.path.join(self.tmp, "app_index.json"), runner=runner, clock=lambda: self.now[0])

    def _name(self, query):
        entry = self.idx.resolve(query)
        return entry["name"] if entry else None

    def test_names_nicknames_words_and_typos(self):
        cases = {"chrome": "Google Chrome", "Google Chrome": "Google Chrome", "notepad": "Notepad",
                 "calc": "Calculator", "vs code": "Visual Studio Code", "word": "Word", "wordpad": "WordPad",
                 "whatsapp app": "WhatsApp", "the file explorer": "File Explorer", "notepd": "Notepad"}
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self._name(query), expected)

    def test_vague_or_unknown_names_go_the_old_way(self):
        for query in ("spotify", "xyz", "", "a"):
            with self.subTest(query=query):
                self.assertIsNone(self.idx.resolve(query))

    def test_uninstallers_are_never_in_the_index(self):
        self.assertNotIn("Uninstall Visual Studio Code", [e["name"] for e in self.idx._entries])

    def test_windows_is_asked_once_and_the_list_kept_for_the_next_start(self):
        self.assertEqual(len(self.calls), 1)
        self.assertIn("Get-StartApps", " ".join(self.calls[0]))
        again = self._index()
        self.assertEqual(again.resolve("calc")["name"], "Calculator")
        self.assertEqual(len(self.calls), 1)                 # the saved list, no new question to Windows

    def test_an_old_list_is_refreshed_in_the_background(self):
        self.now[0] += ai.REFRESH_S + 1
        with mock.patch.object(ai.threading, "Thread") as thread:
            self.idx.warm_up()
        thread.assert_called_once()

    def test_launching_uses_the_app_id_from_windows_list_only(self):
        with mock.patch.object(ai.subprocess, "Popen") as popen:
            ai.AppIndex.launch(self.idx.resolve("calc"))
        self.assertEqual(popen.call_args[0][0],
                         ["explorer.exe", "shell:AppsFolder\\Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"])
        with self.assertRaises(ValueError):
            ai.AppIndex.launch({"name": "x", "appid": 'bad" & del *'})

    def test_off_means_the_old_way(self):
        with mock.patch.object(config, "APP_INDEX", False):
            self.assertIsNone(self.idx.resolve("calc"))


@unittest.skipUnless(sys.platform == "win32", "the Windows backend")
class WindowsBackendTests(unittest.TestCase):
    def test_open_app_launches_from_the_index_and_falls_back_to_the_start_menu(self):
        from automation.backends import windows  # noqa: PLC0415
        backend = windows.WindowsBackend() if hasattr(windows, "WindowsBackend") else None
        if backend is None:
            self.skipTest("no WindowsBackend class")
        entry = {"name": "Calculator", "appid": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"}
        with mock.patch.object(ai.app_index, "resolve", return_value=entry), \
                mock.patch.object(ai.app_index, "launch") as launch, \
                mock.patch.object(windows.pyautogui, "press") as press:
            self.assertEqual(backend.launch_app("calc"), "Successfully opened Calculator.")
        launch.assert_called_once_with(entry)
        press.assert_not_called()
        with mock.patch.object(ai.app_index, "resolve", return_value=None), \
                mock.patch.object(windows.pyautogui, "press") as press, \
                mock.patch.object(windows.pyautogui, "write"), mock.patch.object(windows.time, "sleep"):
            backend.launch_app("spotify")
        press.assert_called()


if __name__ == "__main__":
    unittest.main()
