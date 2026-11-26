"""Tier 51 - the typed core (mypy.ini) stays type-clean.

CI's "Type check (typed core, blocking)" job runs the same check; this test
makes a local full run catch a new type error too. Skipped when mypy isn't
installed (pip install mypy).
"""
import importlib.util
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _listed_files() -> list:
    files, collecting = [], False
    with open(os.path.join(ROOT, "mypy.ini"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("files ="):
                collecting = True
                continue
            if collecting:
                if not line.startswith((" ", "\t")):
                    break
                files.append(line.strip().rstrip(","))
    return [f for f in files if f]


class TypedCoreListTests(unittest.TestCase):
    def test_every_listed_file_exists(self):
        files = _listed_files()
        self.assertGreaterEqual(len(files), 70)
        for f in files:
            self.assertTrue(os.path.exists(os.path.join(ROOT, f)), f)

    def test_the_newer_modules_are_in_it(self):
        files = set(_listed_files())
        for f in ["brain/semantic_intents.py", "brain/people_graph.py", "brain/memory_rank.py",
                  "brain/episodic_recall.py", "brain/grounding.py", "brain/context_line.py",
                  "voice/echo_guard.py", "voice/system_reference.py", "voice/text_prep.py",
                  "brain/intent_router.py", "bench/heldout_bench.py", "bench/memory_bench.py"]:
            self.assertIn(f, files)


@unittest.skipUnless(importlib.util.find_spec("mypy"), "mypy isn't installed")
class TypedCoreMypyTests(unittest.TestCase):
    def test_no_type_errors(self):
        run = subprocess.run([sys.executable, "-m", "mypy"], cwd=ROOT, capture_output=True, text=True,
                             timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-1000:])


if __name__ == "__main__":
    unittest.main()
