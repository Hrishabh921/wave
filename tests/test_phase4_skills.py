"""Phase 4 / idea 3 - hot-reload skills.

Covers the transactional registry (brain/skills_registry.py) and the folder
watcher (brain/skill_loader.py). No Qt, no network, no real Groq - everything
runs against throw-away registries and temp folders.
"""
import os
import sys
import threading
import time
import unittest

import _phase4_support as S  # noqa: F401  (puts the project root on sys.path)

from brain.skills_registry import SkillRegistry, SkillValidationError, BUILTIN_SOURCE
from brain.skill_loader import UserSkillLoader


def write(path, text, bump_mtime=True):
    """Write a file and make sure its (mtime, size) signature really changes -
    some filesystems only have 1-2 s mtime resolution."""
    before = os.stat(path).st_mtime_ns if os.path.exists(path) else 0
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    if bump_mtime:
        now = max(time.time_ns(), before + 5_000_000_000)
        os.utime(path, ns=(now, now))


SKILL_V1 = '''
@skill("say_hi", "Says hi.", {"who": {"type": "string"}}, [])
def say_hi(brain, args):
    return "hi v1"
'''
SKILL_V2 = SKILL_V1.replace("hi v1", "hi v2")  # SAME length on purpose: size can't give the change away


class RegistryStagingTests(unittest.TestCase):
    def setUp(self):
        self.reg = SkillRegistry()

        @self.reg.register("builtin_one", "A built-in.", {}, [])
        def _b(brain, args):
            return "builtin"

    def test_commit_makes_skills_live_and_bumps_revision(self):
        rev = self.reg.revision
        with self.reg.staging("user:a.py"):
            @self.reg.register("alpha", "Alpha tool.", {"x": {"type": "string"}}, ["x"])
            def alpha(brain, args):
                return "a"
        self.assertIn("alpha", self.reg)
        self.assertEqual(self.reg.source_of("alpha"), "user:a.py")
        self.assertGreater(self.reg.revision, rev)
        names = [t["function"]["name"] for t in self.reg.tool_specs()]
        self.assertEqual(names, ["builtin_one", "alpha"])

    def test_exception_inside_stage_rolls_everything_back(self):
        with self.reg.staging("user:a.py"):
            @self.reg.register("alpha", "v1", {}, [])
            def alpha(brain, args):
                return "v1"
        with self.assertRaises(RuntimeError):
            with self.reg.staging("user:a.py"):
                @self.reg.register("alpha", "v2 (half written)", {}, [])
                def alpha2(brain, args):
                    return "v2"
                raise RuntimeError("boom half way through the file")
        self.assertEqual(self.reg.get_handler("alpha")(None, {}), "v1")  # old version still live
        self.assertEqual(self.reg.tool_specs()[1]["function"]["description"], "v1")

    def test_replacing_a_source_removes_skills_it_no_longer_defines(self):
        with self.reg.staging("user:a.py"):
            @self.reg.register("one", "1", {}, [])
            def one(b, a): return 1
            @self.reg.register("two", "2", {}, [])
            def two(b, a): return 2
        with self.reg.staging("user:a.py"):
            @self.reg.register("two", "2b", {}, [])
            def two_b(b, a): return 22
        self.assertNotIn("one", self.reg)
        self.assertEqual(self.reg.get_handler("two")(None, {}), 22)

    def test_user_skill_cannot_shadow_builtin_or_another_file(self):
        with self.assertRaises(SkillValidationError):
            with self.reg.staging("user:evil.py"):
                @self.reg.register("builtin_one", "shadow!", {}, [])
                def evil(b, a): return "pwned"
        self.assertEqual(self.reg.get_handler("builtin_one")(None, {}), "builtin")

        with self.reg.staging("user:a.py"):
            @self.reg.register("shared", "x", {}, [])
            def s(b, a): return "a"
        with self.assertRaises(SkillValidationError):
            with self.reg.staging("user:b.py"):
                @self.reg.register("shared", "x", {}, [])
                def s2(b, a): return "b"
        self.assertEqual(self.reg.source_of("shared"), "user:a.py")

    def test_invalid_specs_are_rejected_before_they_can_poison_the_tool_list(self):
        bad = [
            ("has space", "d", {}, []),
            ("", "d", {}, []),
            ("x" * 65, "d", {}, []),
            ("ok", "   ", {}, []),                                   # empty description
            ("ok", "d", {"a": "not-a-dict"}, []),
            ("ok", "d", {"a": {"type": "string"}}, ["missing"]),     # required not in params
            ("ok", "d", {"a": {"type": object()}}, []),              # not JSON-serialisable
        ]
        for name, desc, params, req in bad:
            with self.subTest(name=name[:12], desc=desc[:6]):
                with self.assertRaises(SkillValidationError):
                    with self.reg.staging("user:bad.py"):
                        self.reg.register(name, desc, params, req)(lambda b, a: "x")
        self.assertEqual(self.reg.names(), ["builtin_one"])

    def test_builtin_duplicate_still_fails_loudly_at_import_time(self):
        with self.assertRaises(ValueError):
            self.reg.register("builtin_one", "again", {}, [])(lambda b, a: "x")

    def test_staging_cannot_be_nested(self):
        with self.assertRaises(RuntimeError):
            with self.reg.staging("user:a.py"):
                with self.reg.staging("user:b.py"):
                    pass

    def test_concurrent_reads_while_a_writer_swaps_skills(self):
        stop, errors = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    specs = self.reg.tool_specs()
                    assert specs[0]["function"]["name"] == "builtin_one"
                    self.reg.get_handler("builtin_one")
                except Exception as e:  # pragma: no cover - failure path
                    errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        [t.start() for t in threads]
        for i in range(200):
            with self.reg.staging("user:churn.py"):
                self.reg.register(f"churn_{i % 3}", "c", {}, [])(lambda b, a: "x")
        stop.set()
        [t.join() for t in threads]
        self.assertEqual(errors, [])


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = S.make_tmpdir(self)
        self.reg = SkillRegistry()
        self.reg.register("builtin_one", "A built-in.", {}, [])(lambda b, a: "builtin")
        self.loader = UserSkillLoader(self.dir, registry=self.reg, poll_interval=0.1)
        self.events = []
        self.loader.add_listener(self.events.append)
        self.addCleanup(self.loader.stop)

    def path(self, name):
        return os.path.join(self.dir, name)

    def kinds(self):
        return [e.kind for e in self.events]

    def test_new_file_is_loaded_and_callable(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds(), ["loaded"])
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v1")
        self.assertEqual(self.reg.source_of("say_hi"), "user:hi.py")

    def test_same_size_edit_is_picked_up_not_served_from_a_stale_pyc(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        write(self.path("hi.py"), SKILL_V2)
        self.assertEqual(len(SKILL_V1), len(SKILL_V2))
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds(), ["loaded", "reloaded"])
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v2")

    def test_syntax_error_keeps_previous_working_version(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        write(self.path("hi.py"), SKILL_V1 + "\ndef broken(:\n")
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds(), ["loaded", "error"])
        self.assertIn("SyntaxError", self.events[-1].detail)
        self.assertIn("previous working version", self.events[-1].detail)
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v1")
        # the same broken file is not re-reported on every poll
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds().count("error"), 1)
        # ...and fixing it recovers
        write(self.path("hi.py"), SKILL_V2)
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds()[-1], "reloaded")
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v2")

    def test_runtime_error_at_import_is_reported_and_leaves_registry_clean(self):
        write(self.path("bad.py"), SKILL_V1.replace("say_hi", "half_done") + "\nraise ValueError('nope')\n")
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds(), ["error"])
        self.assertIn("ValueError", self.events[0].detail)
        self.assertNotIn("half_done", self.reg)  # decorated before the raise, but never committed

    def test_deleting_the_file_removes_the_skill(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        os.remove(self.path("hi.py"))
        self.loader.scan_once(settle=False)
        self.assertEqual(self.kinds(), ["loaded", "unloaded"])
        self.assertNotIn("say_hi", self.reg)
        self.assertEqual(self.events[-1].skills, ["say_hi"])

    def test_ignored_files(self):
        write(self.path("_example.py"), SKILL_V1)
        write(self.path(".hidden.py"), SKILL_V1)
        write(self.path("notes.txt"), SKILL_V1)
        os.makedirs(self.path("sub.py"))  # a directory that merely looks like a skill
        self.loader.scan_once(settle=False)
        self.assertEqual(self.events, [])
        self.assertEqual(self.reg.names(), ["builtin_one"])

    def _global_loader(self):
        """A loader bound to the REAL shared registry - the production path, and the
        only one where an explicit `from brain.skills_registry import skill` resolves
        to the registry being tested. Cleans up after itself."""
        from brain.skills_registry import skills as shared
        loader = UserSkillLoader(self.dir, registry=shared, poll_interval=0.1)
        events = []
        loader.add_listener(events.append)

        def cleanup():
            loader.stop()
            for src in [s for s in {shared.source_of(n) for n in shared.names()} if s and s.startswith("user:")]:
                shared.unregister_source(src)
            shared.unregister("p4_test_builtin")
        self.addCleanup(cleanup)
        return shared, loader, events

    def test_cannot_shadow_builtin_and_file_without_skill_is_explained(self):
        shared, loader, events = self._global_loader()
        shared.register("p4_test_builtin", "A built-in.", {}, [])(lambda b, a: "builtin")
        write(self.path("evil.py"), 'from brain.skills_registry import skill  # noqa\n'
                                   '@skill("p4_test_builtin", "shadow", {}, [])\ndef f(b, a): return "pwned"\n')
        write(self.path("empty.py"), "x = 1\n")
        loader.scan_once(settle=False)
        details = {e.file: e.detail for e in events}
        self.assertIn("already taken", details["evil.py"])
        self.assertIn("doesn't define any skill", details["empty.py"])
        self.assertEqual(shared.get_handler("p4_test_builtin")(None, {}), "builtin")

    def test_two_files_with_the_same_skill_name_second_one_loses(self):
        write(self.path("a.py"), SKILL_V1)
        write(self.path("b.py"), SKILL_V2)
        self.loader.scan_once(settle=False)
        self.assertEqual(self.reg.source_of("say_hi"), "user:a.py")
        self.assertEqual(sorted(self.kinds()), ["error", "loaded"])

    def test_a_file_saved_a_moment_ago_waits_one_poll_before_loading(self):
        write(self.path("hi.py"), SKILL_V1, bump_mtime=False)   # mtime == now
        with open(self.path("hi.py"), "a", encoding="utf-8") as fh:
            fh.write("\n")
        self.loader.scan_once(settle=True)
        self.assertEqual(self.events, [])            # looked too fresh -> deferred
        time.sleep(0.5)
        self.loader.scan_once(settle=True)
        self.assertEqual(self.kinds(), ["loaded"])

    def test_unreadable_file_is_retried_not_remembered_as_failed(self):
        write(self.path("hi.py"), SKILL_V1)
        real_open = open

        def flaky_open(p, *a, **k):
            if str(p).endswith("hi.py") and not getattr(flaky_open, "ok", False):
                raise PermissionError("locked by editor")
            return real_open(p, *a, **k)

        import builtins
        builtins.open, orig = flaky_open, builtins.open
        try:
            self.loader.scan_once(settle=False)
            self.assertEqual(self.events, [])
            flaky_open.ok = True
            self.loader.scan_once(settle=False)   # same file signature, now readable
        finally:
            builtins.open = orig
        self.assertEqual(self.kinds(), ["loaded"])

    def test_skill_is_available_without_an_import_line(self):
        write(self.path("hi.py"), SKILL_V1)           # no `from brain... import skill`
        self.loader.scan_once(settle=False)
        self.assertIn("say_hi", self.reg)

    def test_background_watcher_end_to_end(self):
        self.loader.start(watch=True)
        self.assertTrue(self.loader.watching)
        write(self.path("hi.py"), SKILL_V1)
        deadline = time.time() + 5
        while "say_hi" not in self.reg and time.time() < deadline:
            time.sleep(0.05)
        self.assertIn("say_hi", self.reg)
        write(self.path("hi.py"), SKILL_V2)
        deadline = time.time() + 5
        while self.reg.get_handler("say_hi")(None, {}) != "hi v2" and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v2")
        os.remove(self.path("hi.py"))
        deadline = time.time() + 5
        while "say_hi" in self.reg and time.time() < deadline:
            time.sleep(0.05)
        self.assertNotIn("say_hi", self.reg)
        self.loader.stop()
        self.assertFalse(self.loader.watching)

    def test_start_loads_existing_files_immediately_and_creates_folder(self):
        sub = os.path.join(self.dir, "fresh_folder")
        ldr = UserSkillLoader(sub, registry=self.reg, poll_interval=0.1)
        self.addCleanup(ldr.stop)
        ldr.start(watch=False)
        self.assertTrue(os.path.isdir(sub))
        write(os.path.join(sub, "hi.py"), SKILL_V1)
        UserSkillLoader(sub, registry=self.reg).load_now()
        self.assertIn("say_hi", self.reg)

    def test_listener_exceptions_do_not_break_scanning(self):
        def bad(ev):
            raise RuntimeError("listener bug")
        self.loader.add_listener(bad)
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.assertIn("say_hi", self.reg)

    def test_status_reports_loaded_and_errors(self):
        write(self.path("hi.py"), SKILL_V1)
        write(self.path("bad.py"), "def (:\n")
        self.loader.scan_once(settle=False)
        st = self.loader.status()
        self.assertEqual(st["loaded"], {"hi.py": ["say_hi"]})
        self.assertIn("bad.py", st["errors"])

    def test_the_shipped_example_skill_is_valid_and_works(self):
        import shutil
        shared, loader, events = self._global_loader()
        src = os.path.join(S.ROOT, "user_skills", "_example_skill.py")
        shutil.copy(src, self.path("dice.py"))
        loader.scan_once(settle=False)
        self.assertEqual([e.kind for e in events], ["loaded"], events)
        out = shared.get_handler("roll_dice")(None, {"sides": 6, "count": 3})
        self.assertIn("total", out)
        self.assertIn("d6 x3", out)
        self.assertRegex(shared.get_handler("roll_dice")(None, {}), r"You rolled a [1-6] \(d6\)\.")


class EnableDisableTests(unittest.TestCase):
    """Tier 21 roadmap line for ui/skills_panel.py: 'user skills,
    enable/disable, hot-reload status'. set_disabled() is the mechanism the
    Skills page's toggle button calls - these tests cover it directly,
    without any Qt involved, the same way LoaderTests above covers the
    rest of this file's load/reload/unload behavior."""

    def setUp(self):
        self.dir = S.make_tmpdir(self)
        self.reg = SkillRegistry()
        self.loader = UserSkillLoader(self.dir, registry=self.reg, poll_interval=0.1)
        self.events = []
        self.loader.add_listener(self.events.append)
        self.addCleanup(self.loader.stop)

    def path(self, name):
        return os.path.join(self.dir, name)

    def kinds(self):
        return [e.kind for e in self.events]

    def test_disabling_a_loaded_file_unregisters_its_skill(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.assertIn("say_hi", self.reg)

        ev = self.loader.set_disabled("hi.py", True)
        self.assertEqual(ev.kind, "disabled")
        self.assertNotIn("say_hi", self.reg)
        self.assertTrue(self.loader.is_disabled("hi.py"))
        self.assertIn("hi.py", self.loader.disabled_skills())

    def test_enabling_it_again_reloads_the_skill(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.loader.set_disabled("hi.py", True)

        ev = self.loader.set_disabled("hi.py", False)
        self.assertEqual(ev.kind, "enabled")
        self.assertEqual(self.reg.get_handler("say_hi")(None, {}), "hi v1")
        self.assertFalse(self.loader.is_disabled("hi.py"))

    def test_disabled_choice_survives_a_scan(self):
        """The file is left on disk untouched - scan_once must keep treating
        a disabled file as absent instead of silently reloading it on the
        very next poll."""
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.loader.set_disabled("hi.py", True)
        self.events.clear()

        self.loader.scan_once(settle=False)
        self.assertEqual(self.events, [])
        self.assertNotIn("say_hi", self.reg)

    def test_disabled_choice_persists_across_a_fresh_loader_instance(self):
        """Sidecar file survives a restart - a new UserSkillLoader pointed at
        the same folder must come up already knowing hi.py is disabled."""
        write(self.path("hi.py"), SKILL_V1)
        self.loader.set_disabled("hi.py", True)

        reloaded = UserSkillLoader(self.dir, registry=SkillRegistry(), poll_interval=0.1)
        self.addCleanup(reloaded.stop)
        self.assertTrue(reloaded.is_disabled("hi.py"))
        reloaded.scan_once(settle=False)
        self.assertNotIn("say_hi", reloaded.registry)

    def test_toggling_twice_is_a_no_op_the_second_time(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.loader.set_disabled("hi.py", True)
        self.assertIsNone(self.loader.set_disabled("hi.py", True))  # already disabled

    def test_disabling_a_file_that_errored_clears_the_error(self):
        write(self.path("bad.py"), "def (:\n")
        self.loader.scan_once(settle=False)
        self.assertIn("bad.py", self.loader.status()["errors"])

        self.loader.set_disabled("bad.py", True)
        self.assertNotIn("bad.py", self.loader.status()["errors"])
        self.assertIn("bad.py", self.loader.status()["disabled"])

    def test_status_lists_disabled_files(self):
        write(self.path("hi.py"), SKILL_V1)
        self.loader.scan_once(settle=False)
        self.loader.set_disabled("hi.py", True)
        self.assertEqual(self.loader.status()["disabled"], ["hi.py"])
        self.assertEqual(self.loader.status()["loaded"], {})


if __name__ == "__main__":
    unittest.main()
