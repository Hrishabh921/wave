"""Phase 4 - ideas 2, 3 and 5 working TOGETHER through the real WaveBrain.

A fake OpenAI-compatible server stands in for the LLM; the file system, SQLite and
the skill loader are real (temp folders). pyautogui/pywhatkit are stubbed because
they can't import on a headless Linux box - nothing here presses a key.
"""
import json
import os
import time
import unittest
from unittest import mock

import _phase4_support as S
from _fake_openai import FakeOpenAI, Reply, completion, error_body, tool_call

S.install_fake_module("pyautogui", FAILSAFE=True, PAUSE=0.1, press=lambda *a, **k: None,
                      hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None, write=lambda *a, **k: None,
                      screenshot=lambda *a, **k: None, size=lambda: (1920, 1080))
S.install_fake_module("pywhatkit")

from settings.config import config                             # noqa: E402
from brain.skills_registry import skills                      # noqa: E402
from brain.skill_loader import UserSkillLoader                 # noqa: E402
from automation import audit as audit_mod                      # noqa: E402
from automation import undo as undo_mod                        # noqa: E402
from automation.audit import Audit, DbStore                    # noqa: E402
from automation.undo import TrashStore, UndoManager            # noqa: E402
from database.db_manager import db                             # noqa: E402


class BrainBase(unittest.TestCase):
    def setUp(self):
        self.tmp = S.make_tmpdir(self)
        self.srv = FakeOpenAI().start()
        self.addCleanup(self.srv.stop)

        # isolated SQLite, audit, trash, HOME (so "the Desktop" is a temp folder)
        old_path = db.db_path
        db.db_path = os.path.join(self.tmp, "t.db")
        db.init_db()
        self.desktop = os.path.join(self.tmp, "Desktop")
        os.makedirs(self.desktop)
        self.audit = Audit(store=DbStore(db), max_entries=100)
        self.trash = TrashStore(os.path.join(self.tmp, "trash"), 7, 10 * 1024 * 1024)
        self.um = UndoManager(self.audit, self.trash)
        saved = (audit_mod.audit, undo_mod.trash, undo_mod.undo_manager)
        audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = self.audit, self.trash, self.um
        import brain.groq_core as gc
        self.gc = gc
        gc_saved = (gc.audit, gc.undo_manager)
        gc.audit, gc.undo_manager = self.audit, self.um
        # Tier 4: list_recent_actions/undo_last_action moved to
        # brain/handlers/automation.py, which imports audit/undo_manager
        # independently of groq_core.py - patching gc.audit/gc.undo_manager
        # alone no longer reaches the code that actually runs for those two
        # tools, so this same swap needs to happen there too.
        import brain.handlers.automation as gc_automation
        gc_automation_saved = (gc_automation.audit, gc_automation.undo_manager)
        gc_automation.audit, gc_automation.undo_manager = self.audit, self.um

        # Tier 2: this file drives think() with phrasing like "undo that"
        # specifically to test the LLM-driven path against a SCRIPTED
        # server reply - brain/intent_router.py's fast path would now
        # catch that same phrasing first (correctly, in real use - that's
        # its whole job) without ever consulting self.srv.script.
        # Disabled here so BrainBase's tests keep exercising exactly the
        # Groq/tool-calling behavior they're named for; the router has its
        # own tests in test_phase8_intent_router.py.
        router_saved = config.INTENT_ROUTER_ENABLED
        config.INTENT_ROUTER_ENABLED = False

        env = {"LLM_PROVIDER": "custom", "LLM_BASE_URL": self.srv.url, "LLM_MODEL": "test-model",
               "LLM_API_KEY": "sk-test-1234567890", "HOME": self.tmp, "USERPROFILE": self.tmp}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()

        def cleanup():
            patcher.stop()
            db.db_path = old_path
            audit_mod.audit, undo_mod.trash, undo_mod.undo_manager = saved
            gc.audit, gc.undo_manager = gc_saved
            gc_automation.audit, gc_automation.undo_manager = gc_automation_saved
            config.INTENT_ROUTER_ENABLED = router_saved
        self.addCleanup(cleanup)
        self.brain = gc.WaveBrain()

    def tool_names_in(self, req):
        return [t["function"]["name"] for t in (req.json.get("tools") or [])]


class ProviderWiringTests(BrainBase):
    def test_brain_uses_the_configured_provider_not_groq(self):
        self.assertEqual((self.brain.provider.name, self.brain.model), ("custom", "test-model"))
        self.srv.script = [Reply(200, completion("Hello from the fake LLM."))]
        # Tier 28 sends only a turn's likely tools; send them all here so this
        # still checks that every built-in can reach the provider.
        with mock.patch.object(config, "LLM_MAX_TOOLS", 0):
            reply = self.brain.think("hi there")
        self.assertEqual(reply, "Hello from the fake LLM.")
        req = self.srv.posts()[0]
        self.assertEqual(req.path, "/v1/chat/completions")
        self.assertEqual(req.json["model"], "test-model")
        self.assertEqual(req.headers["authorization"], "Bearer sk-test-1234567890")
        names = self.tool_names_in(req)
        self.assertIn("open_app", names)                    # built-ins reach the provider
        self.assertIn("undo_last_action", names)
        self.assertIn("list_recent_actions", names)

    def test_provider_info_and_connection_check(self):
        info = self.brain.provider_info()
        self.assertEqual((info["provider"], info["model"], info["configured"]), ("custom", "test-model", True))
        self.assertNotIn("sk-test", json.dumps(info))
        self.srv.get_routes["/v1/models"] = Reply(200, {"data": []})
        ok, msg, _ = self.brain.check_provider()
        self.assertTrue(ok, msg)

    def test_unconfigured_provider_does_not_crash_the_brain(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "openai", "LLM_API_KEY": "", "OPENAI_API_KEY": ""}):
            brain = self.gc.WaveBrain()
        self.assertIsNone(brain.client)
        self.assertIn("OPENAI_API_KEY", brain.provider_info()["problem"])
        reply = brain.think("hello")                        # falls through to the offline paths
        self.assertIsInstance(reply, str)
        self.assertEqual(len(self.srv.requests), 0)         # and never tried the network

    def test_provider_failure_falls_back_and_offline_undo_still_works(self):
        self.gc.FileManager.create_file("note.txt", "hi", self.desktop)
        self.assertTrue(os.path.exists(os.path.join(self.desktop, "note.txt")))
        self.srv.default = Reply(401, error_body("bad key"))
        reply = self.brain.think("undo that")               # LLM is rejecting us; keyword path handles it
        self.assertIn("Undid", reply)
        self.assertFalse(os.path.exists(os.path.join(self.desktop, "note.txt")))


class HotSkillThroughTheBrainTests(BrainBase):
    def setUp(self):
        super().setUp()
        self.skills_dir = os.path.join(self.tmp, "user_skills")
        self.loader = UserSkillLoader(self.skills_dir, registry=skills, poll_interval=0.1)

        def cleanup():
            self.loader.stop()
            for src in {skills.source_of(n) for n in skills.names()}:
                if src and src.startswith("user:"):
                    skills.unregister_source(src)
        self.addCleanup(cleanup)
        self.loader.load_now()

    def drop(self, name, body):
        path = os.path.join(self.skills_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        t = time.time_ns() + 5_000_000_000
        os.utime(path, ns=(t, t))

    ECHO = '''
@skill("p4_echo", "Echoes text back, loudly.", {"text": {"type": "string"}}, ["text"])
def p4_echo(brain, args):
    return "ECHO: " + str(args.get("text", "")).upper()
'''

    def test_a_skill_dropped_in_while_running_is_offered_to_the_llm_and_executed(self):
        self.srv.script = [Reply(200, completion("before"))]
        self.brain.think("first message")
        self.assertNotIn("p4_echo", self.tool_names_in(self.srv.posts()[0]))

        self.drop("echo.py", self.ECHO)                     # <- no restart
        self.loader.scan_once(settle=False)

        self.srv.requests.clear()
        self.srv.script = [Reply(200, completion(None, [tool_call("p4_echo", {"text": "wave"})])),
                           Reply(200, completion("It says ECHO: WAVE."))]
        reply = self.brain.think("echo wave please")
        self.assertEqual(reply, "It says ECHO: WAVE.")
        first, second = self.srv.posts()
        self.assertIn("p4_echo", self.tool_names_in(first))
        tool_msgs = [m for m in second.json["messages"] if m.get("role") == "tool"]
        self.assertEqual([m["content"] for m in tool_msgs], ["ECHO: WAVE"])      # the skill really ran
        row = self.audit.recent(1)[0]
        self.assertEqual((row.tool, row.source, row.status), ("p4_echo", "ai", "ok"))

        os.remove(os.path.join(self.skills_dir, "echo.py"))                       # and removal is live too
        self.loader.scan_once(settle=False)
        self.srv.requests.clear()
        self.srv.script = [Reply(200, completion("gone"))]
        self.brain.think("anything")
        self.assertNotIn("p4_echo", self.tool_names_in(self.srv.posts()[0]))

    def test_a_broken_skill_never_reaches_the_llm_tool_list(self):
        self.drop("bad.py", '@skill("bad name!", "x", {}, [])\ndef f(b, a): return "x"\n')
        events = self.loader.scan_once(settle=False)
        self.assertEqual([e.kind for e in events], ["error"])
        self.srv.script = [Reply(200, completion("fine"))]
        self.brain.think("hello")
        self.assertFalse([n for n in self.tool_names_in(self.srv.posts()[0]) if "bad" in n])

    def test_list_my_skills_tool(self):
        from brain.skill_loader import get_loader
        loader = get_loader()
        with mock.patch.object(loader, "status", return_value={"directory": "/x/user_skills", "watching": True,
                                                              "loaded": {"a.py": ["alpha"]}, "errors": {"b.py": "SyntaxError"}}):
            out = self.brain._dispatch_tool("list_my_skills", {})
        self.assertIn("a.py (alpha)", out)
        self.assertIn("b.py: SyntaxError", out)


class AuditAndUndoThroughTheBrainTests(BrainBase):
    def test_llm_creates_a_file_then_the_user_says_undo(self):
        # Phase 7: create_file is gated now - round 1 still decides to call
        # it, but _dispatch_tool blocks it pending confirmation instead of
        # running it, so round 2's "reply" is really just the model relaying
        # that ask (whatever words it uses - the script controls that, same
        # as any other tool result it composes from).
        self.srv.script = [
            Reply(200, completion(None, [tool_call("create_file", {"file_name": "ideas.txt", "content": "v1"})])),
            Reply(200, completion("Sure - want me to go ahead and create ideas.txt?")),
            Reply(200, completion(None, [tool_call("undo_last_action", {})], model="m")),
            Reply(200, completion("Done - I removed it.")),
        ]
        reply = self.brain.think("make a file called ideas.txt")
        self.assertEqual(reply, "Sure - want me to go ahead and create ideas.txt?")
        path = os.path.join(self.desktop, "ideas.txt")
        self.assertFalse(os.path.exists(path))                                    # not created yet
        self.assertEqual(self.brain.pending_action["tool"], "create_file")
        self.assertEqual(self.audit.recent(1)[0].status, "blocked")

        # Saying yes replays it directly - no LLM round-trip needed for that,
        # so the script above is never touched for this step.
        self.assertEqual(self.brain.think("yes"), "File 'ideas.txt' created successfully.")
        self.assertTrue(os.path.exists(path))
        self.assertIsNone(self.brain.pending_action)
        rows = self.audit.recent()
        self.assertEqual(len(rows), 2)                                            # the block, then the confirmed run
        self.assertEqual((rows[0].tool, rows[0].source, rows[0].status, rows[0].undo_kind),
                         ("create_file", "ai", "ok", "remove_created_file"))       # adoption still collapses to one row
        self.assertEqual((rows[1].tool, rows[1].status), ("create_file", "blocked"))

        self.assertEqual(self.brain.think("undo that"), "Done - I removed it.")
        self.assertFalse(os.path.exists(path))
        tool_result = [m["content"] for m in self.srv.posts()[3].json["messages"] if m.get("role") == "tool"][0]
        self.assertIn("Undid 'Created file ideas.txt'", tool_result)
        self.assertEqual([r.tool for r in self.audit.recent()],
                         ["undo_last_action", "undo", "create_file", "create_file"])

    def test_list_recent_actions_reports_what_can_be_undone(self):
        self.gc.FileManager.create_file("a.txt", "", self.desktop)
        self.gc.FileManager.create_file("b.txt", "", self.desktop)
        self.um.undo_last()
        out = self.brain._dispatch_tool("list_recent_actions", {"count": 5})
        self.assertIn("Created file b.txt (undone)", out)
        self.assertIn("Created file a.txt (can be undone)", out)
        self.assertEqual(len(self.audit.recent()), 3)                             # listing itself is quiet

    # Tier 5: recent_actions polish - grouping consecutive repeats and the
    # today/failed filters. volume_up is used below as the "ordinary
    # successful action" (rather than close_app, which the Errors section
    # of this tier's own notes flags as a trap: close_app is T2/gated, so
    # dispatching it directly only ever produces a "blocked" pending-
    # confirm row, never a genuine "ok" one) - volume_up is T1/ungated (see
    # brain/tiers.py) and brain/handlers/media.py's _skill_volume_up always
    # returns "Turned the volume up." regardless of whether real audio
    # hardware is present, so it's a reliable, always-"ok" action to repeat.
    def test_list_recent_actions_groups_consecutive_repeats(self):
        for _ in range(3):
            self.brain._dispatch_tool("volume_up", {})
        out = self.brain._dispatch_tool("list_recent_actions", {"count": 5})
        self.assertIn("Volume up (x3)", out)
        self.assertEqual(out.count("Volume up"), 1)          # one grouped line, not three

    def test_list_recent_actions_does_not_group_across_a_different_action(self):
        self.brain._dispatch_tool("volume_up", {})
        self.brain._dispatch_tool("volume_down", {})
        self.brain._dispatch_tool("volume_up", {})
        out = self.brain._dispatch_tool("list_recent_actions", {"count": 5})
        self.assertNotIn("(x2)", out)
        self.assertNotIn("(x3)", out)
        self.assertEqual(out.count("Volume up"), 2)
        self.assertEqual(out.count("Volume down"), 1)

    def test_list_recent_actions_today_filter_includes_todays_actions(self):
        self.brain._dispatch_tool("volume_up", {})
        out = self.brain._dispatch_tool("list_recent_actions", {"filter": "today"})
        self.assertIn("Volume up", out)

    def test_list_recent_actions_failed_filter(self):
        # An all-ok history gets a distinct "nothing's failed" message for
        # filter=failed, not the generic "I haven't done anything" one -
        # there IS history, it just doesn't match the filter.
        self.brain._dispatch_tool("volume_up", {})
        out = self.brain._dispatch_tool("list_recent_actions", {"filter": "failed"})
        self.assertIn("Nothing's failed recently", out)

        # delete_file is gated (Phase 7) - the first call only arms a
        # pending confirmation ("blocked"), so "yes" has to actually replay
        # it to get a genuine failure (ghost.txt really doesn't exist).
        asked = self.brain._dispatch_tool("delete_file", {"file_name": "ghost.txt"})
        self.assertIn("confirm", asked.lower())
        self.brain.think("yes")
        self.assertEqual(self.audit.recent(1)[0].status, "failed")

        out = self.brain._dispatch_tool("list_recent_actions", {"filter": "failed"})
        self.assertIn("Deleted file ghost.txt", out)
        self.assertIn("didn't work", out)
        self.assertNotIn("Volume up", out)                   # the earlier ok action is filtered out

    def test_failed_and_unknown_tools_are_logged_honestly(self):
        self.assertIn("don't have", self.brain._dispatch_tool("no_such_tool", {}))
        self.assertEqual(self.audit.recent(), [])                                 # nothing ran, nothing to record

        # Phase 7: delete_file is gated - the first attempt only asks for
        # confirmation (logged as "blocked"), without even touching the
        # filesystem yet.
        asked = self.brain._dispatch_tool("delete_file", {"file_name": "ghost.txt"})
        self.assertIn("confirm", asked.lower())
        self.assertEqual(self.brain.pending_action["tool"], "delete_file")
        self.assertEqual(self.audit.recent(1)[0].status, "blocked")

        # Saying yes actually attempts it - and ghost.txt genuinely isn't
        # there, so the real failure surfaces exactly as it always did once
        # a gated tool actually runs. think("yes") needs no LLM call at all
        # (pure local replay), so no server script is needed for this.
        out = self.brain.think("yes")
        self.assertIn("couldn't find", out)
        self.assertEqual(self.audit.recent(1)[0].status, "failed")

    def test_keyword_path_creates_one_row_per_command_and_nothing_for_non_commands(self):
        self.assertIsNone(self.brain._execute_single_action("what is the capital of france"))
        self.assertEqual(self.audit.recent(), [])


if __name__ == "__main__":
    unittest.main()
