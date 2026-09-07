"""Shared helpers for the test suites.

Deliberately plain Python: no pytest, no fixtures framework, no new dependency.
Each suite is a script that can be run on its own, and run_all.py runs them all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"

# A real, project-local ESLint install for the eslint-adapter suites (docs/14
# -first-multi-language-analyzer.md) - not a qa_agent runtime dependency, so
# it lives here rather than in requirements.txt. `npm install` once inside
# tests/fixtures/eslint before running tests that need a live eslint binary.
ESLINT_BIN = FIXTURES / "eslint" / "node_modules" / ".bin"


def eslint_available():
    """Whether the real eslint fixture has been `npm install`ed.

    Tests needing a live eslint skip cleanly and visibly when it hasn't been -
    never silently, matching this project's own "nothing disappears" rule.
    """
    return (ESLINT_BIN / "eslint.cmd").exists() or (ESLINT_BIN / "eslint").exists()


def env_with_eslint():
    """A copy of the environment with the fixture's eslint prepended to PATH."""
    env = os.environ.copy()
    env["PATH"] = str(ESLINT_BIN) + os.pathsep + env.get("PATH", "")
    return env


# A real ShellCheck binary for the shellcheck-adapter suites (Phase C Part
# 5) - not pip/npm-installable, so fetched directly from its GitHub release
# rather than through a package manager. See fetch.ps1 in this directory.
SHELLCHECK_BIN = FIXTURES / "shellcheck"


def shellcheck_available():
    """Whether the real shellcheck fixture has been fetched (fetch.ps1).

    Tests needing a live shellcheck skip cleanly and visibly when it hasn't
    been - never silently, matching this project's own "nothing disappears"
    rule.
    """
    return (SHELLCHECK_BIN / "shellcheck.exe").exists()


def env_with_shellcheck():
    """A copy of the environment with the fixture's shellcheck on PATH."""
    env = os.environ.copy()
    env["PATH"] = str(SHELLCHECK_BIN) + os.pathsep + env.get("PATH", "")
    return env


def env_with_tools(eslint=False, shellcheck=False):
    """A copy of the environment with any combination of the optional
    tool fixtures prepended to PATH - for tests that mix more than one.
    """
    env = os.environ.copy()
    prefix = []
    if eslint:
        prefix.append(str(ESLINT_BIN))
    if shellcheck:
        prefix.append(str(SHELLCHECK_BIN))
    if prefix:
        env["PATH"] = os.pathsep.join(prefix) + os.pathsep + env.get("PATH", "")
    return env

# Ensure the package under test is importable no matter where this is run from.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class Suite:
    """Collects pass/fail results so a suite can report and exit meaningfully."""

    def __init__(self, name):
        self.name = name
        self.passed = 0
        self.failed = []

    def check(self, description, ok, detail=""):
        if ok:
            self.passed += 1
            print("  PASS  {}{}".format(description, detail))
        else:
            self.failed.append(description)
            print("  FAIL  {}{}".format(description, detail))
        return ok

    def run(self, tests):
        print("== {} ==".format(self.name))
        for test in tests:
            try:
                test(self)
            except Exception as error:  # a crashing test is a failing test
                self.failed.append("{} raised {!r}".format(test.__name__, error))
                print("  FAIL  {} raised {!r}".format(test.__name__, error))
        total = self.passed + len(self.failed)
        print("  {}/{} checks passed\n".format(self.passed, total))
        return 0 if not self.failed else 1


class TempProject:
    """A throwaway directory, cleaned up even when a test fails."""

    def __init__(self, name="qa-agent-test"):
        self.path = Path(tempfile.mkdtemp(prefix=name + "-"))

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False

    def write(self, relative, text):
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline="" - write exactly the bytes the caller's string has, no
        # platform text-mode translation. Found the hard way (Phase C Part
        # 5): Windows' default \n -> \r\n translation on write_text() left
        # every .sh fixture with CRLF, which ShellCheck correctly flags
        # (SC1017) - real, useful signal for a real file, but noise for a
        # fixture meant to be clean. Harmless for every other adapter, which
        # never cared about line-ending style.
        target.write_text(text, encoding="utf-8", newline="")
        return target


def run_agent(args, cwd=None, env=None):
    """Run the CLI exactly as a user would, with this interpreter.

    `-m qa_agent` resolves the package relative to the subprocess's cwd -
    fine when cwd defaults to REPO_ROOT, but a caller pointing cwd at a
    TempProject (e.g. so config discovery, which starts from cwd, finds a
    fixture's own .qa-agent.json) would otherwise get "No module named
    qa_agent". PYTHONPATH keeps the package importable regardless of cwd.

    This interpreter's own Scripts/bin directory (where `pip install -r
    requirements.txt` puts ruff and pyright, pinned versions and all) is
    prepended to PATH too - found by accident, not by design: a stray
    machine-wide ruff install happened to already be on PATH throughout this
    project's whole history, so this was never actually forced until Part 5
    added pyright, which has no such accidental fallback anywhere.
    """
    full_env = dict(os.environ if env is None else env)
    existing = full_env.get("PYTHONPATH")
    full_env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    scripts_dir = str(Path(sys.executable).parent)
    full_env["PATH"] = scripts_dir + os.pathsep + full_env.get("PATH", "")
    return subprocess.run(
        [sys.executable, "-m", "qa_agent", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        env=full_env,
    )


def normalise(text, root):
    """Make output comparable across machines and runs.

    Absolute paths and timestamps differ every run and on every machine, so they
    are replaced rather than baked into a golden file that could never match.
    """
    out = text.replace(str(root), "<PROJECT>").replace(str(root).lower(), "<PROJECT>")
    lines = []
    for line in out.splitlines():
        stripped = line.strip()
        # Two report formats carry a timestamp: the terminal report ("Run at:")
        # and the Markdown one ("- **Run at:** ..."). Both must be neutralised,
        # or the golden could never match twice.
        if stripped.startswith("Run at:"):
            line = "  Run at:  <TIMESTAMP>"
        elif stripped.startswith("- **Run at:**"):
            line = "- **Run at:** <TIMESTAMP>"
        lines.append(line.rstrip())
    return "\n".join(lines).strip() + "\n"


def compare_golden(suite, name, actual, update=False):
    """Compare against a stored golden file, or create it the first time."""
    GOLDEN.mkdir(parents=True, exist_ok=True)
    path = GOLDEN / name
    if update or not path.exists():
        path.write_text(actual, encoding="utf-8")
        return suite.check("golden '{}' recorded".format(name), True)
    expected = path.read_text(encoding="utf-8")
    ok = suite.check("output matches golden '{}'".format(name), actual == expected)
    if not ok:
        print("    --- expected ---")
        print("    " + expected.replace("\n", "\n    "))
        print("    --- actual ---")
        print("    " + actual.replace("\n", "\n    "))
    return ok
