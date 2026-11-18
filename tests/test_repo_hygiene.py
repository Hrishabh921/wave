"""Tier 10 - repo hygiene guards.

The Tier 1-9 merge left the repo red for a reason no single feature test
could catch: a new package was unzipped on top of an older checkout, so
files the new version had dropped were never deleted. Five leftover test
files kept being collected (66 failures + 12 errors) and five modules sat
in the tree with nothing importing them. These tests make that state loud:

  * test_no_obsolete_files_left_behind - fails naming any file listed in
    scripts/remove_obsolete_files.py's OBSOLETE_PATHS that still exists.
  * test_every_app_module_is_imported_somewhere - fails naming any module
    in Wave's own packages that no other file in the repo imports (by a
    static scan of import statements - nothing is executed). A dead module
    is either a leftover like the five above, or something a future change
    forgot to wire in; both are worth knowing about.

    python -m unittest tests.test_repo_hygiene
"""
import ast
import importlib.util
import os
import unittest

import _phase4_support as S

ROOT = S.ROOT

# Wave's own importable packages (top level). Everything under these is
# expected to be imported by something.
APP_PACKAGES = (
    "automation", "brain", "database", "monitoring", "planning", "reminders",
    "settings", "skills", "telemetry", "ui", "vision", "voice", "web",
)

# Modules that are entry points rather than imports, so nothing importing
# them is expected. Keep this list short and explain every entry.
ENTRY_POINT_MODULES = {
    "telemetry.dump",          # run as `python -m telemetry.dump` (see its docstring)
    "telemetry.ttfa_report",   # Tier 11: run as `python -m telemetry.ttfa_report`
}

_SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "wave_env", "venv", ".venv", "build", "dist"}


def _load_cleanup_script():
    path = os.path.join(ROOT, "scripts", "remove_obsolete_files.py")
    spec = importlib.util.spec_from_file_location("wave_remove_obsolete_files", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith((".py", ".pyw")):
                yield os.path.join(dirpath, fn)


def _module_name(path: str) -> str:
    rel = os.path.relpath(path, ROOT)
    rel = rel[:-4] if rel.endswith(".pyw") else rel[:-3]
    parts = rel.split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imported_names(path: str) -> set:
    """Every dotted module name `path` imports, plus their parents, plus
    string arguments to importlib.import_module()/__import__()."""
    with open(path, encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read(), filename=path)
        except SyntaxError:
            return set()
    this = _module_name(path)
    is_pkg = os.path.basename(path) == "__init__.py"
    package = this if is_pkg else this.rpartition(".")[0]
    names = set()

    def add(dotted: str):
        while dotted:
            names.add(dotted)
            dotted = dotted.rpartition(".")[0]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package.split(".") if package else []
                base_parts = base_parts[: len(base_parts) - (node.level - 1)] if node.level > 1 else base_parts
                base = ".".join(base_parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            if base:
                add(base)
            for alias in node.names:
                if alias.name != "*" and base:
                    add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Call):
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if fname in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    add(arg.value)
    return names


class RepoHygieneTests(unittest.TestCase):
    def test_no_obsolete_files_left_behind(self):
        script = _load_cleanup_script()
        left = script.present(ROOT)
        self.assertEqual(
            left, [],
            "These files belong to an older version of Wave and were left behind (usually by "
            "unzipping a new package on top of an old folder). Delete them with "
            "`python scripts/remove_obsolete_files.py --delete`: " + ", ".join(left),
        )

    def test_every_app_module_is_imported_somewhere(self):
        modules = {}
        imported = set()
        for path in _iter_py_files():
            name = _module_name(path)
            top = name.split(".")[0]
            if top in APP_PACKAGES and not path.endswith("__init__.py"):
                modules[name] = path
            imported |= {n for n in _imported_names(path) if n != name}
        orphans = sorted(m for m in modules if m not in imported and m not in ENTRY_POINT_MODULES)
        self.assertEqual(
            orphans, [],
            "Nothing imports these modules - either leftovers that should be deleted (and added to "
            "scripts/remove_obsolete_files.py) or new code that was never wired in: " + ", ".join(orphans),
        )

    def test_the_scan_really_sees_imports(self):
        """Guards the guard: if _imported_names() ever silently returned
        nothing, the orphan test above would flag everything - or, with an
        over-broad allowlist, nothing. A few known edges must be found."""
        imported = set()
        for path in _iter_py_files():
            imported |= _imported_names(path)
        for expected in ("brain.groq_core", "voice.tts", "automation.backends.windows", "brain.handlers.fun"):
            self.assertIn(expected, imported)


if __name__ == "__main__":
    unittest.main()
