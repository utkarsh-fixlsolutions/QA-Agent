"""Shared helpers for the test suites.

Deliberately plain Python: no pytest, no fixtures framework, no new dependency.
Each suite is a script that can be run on its own, and run_all.py runs them all.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"

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
        target.write_text(text, encoding="utf-8")
        return target


def run_agent(args, cwd=None):
    """Run the CLI exactly as a user would, with this interpreter."""
    return subprocess.run(
        [sys.executable, "-m", "qa_agent", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
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
