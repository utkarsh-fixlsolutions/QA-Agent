"""Phase A must keep behaving exactly as it did before Phase B existed.

Output is compared against stored golden files with absolute paths and
timestamps normalised, so the comparison is meaningful on any machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

HAS_ISSUES = "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n    y = 2\n    return x\n"
CLEAN = "def add(a, b):\n    return a + b\n"


def _project(temp):
    temp.write("has_issues.py", HAS_ISSUES)
    temp.write("clean.py", CLEAN)
    temp.write("notes.txt", "not python\n")
    return temp.path


def test_file_with_findings(suite):
    with TempProject() as _:
        pass
    temp = TempProject()
    try:
        root = _project(temp)
        proc = run_agent([str(root / "has_issues.py")])
        suite.check("exit code 1 when findings exist", proc.returncode == 1,
                    "  [got {}]".format(proc.returncode))
        compare_golden(suite, "cli_findings.txt", normalise(proc.stdout, root))
    finally:
        temp.__exit__()


def test_clean_file(suite):
    temp = TempProject()
    try:
        root = _project(temp)
        proc = run_agent([str(root / "clean.py")])
        suite.check("exit code 0 when clean", proc.returncode == 0)
        compare_golden(suite, "cli_clean.txt", normalise(proc.stdout, root))
    finally:
        temp.__exit__()


def test_directory_with_unsupported_file(suite):
    temp = TempProject()
    try:
        root = _project(temp)
        proc = run_agent([str(root)])
        suite.check("directory scan exits 1", proc.returncode == 1)
        suite.check("unsupported file is skipped, not analysed",
                    "no tool configured" in proc.stdout)
        compare_golden(suite, "cli_directory.txt", normalise(proc.stdout, root))
    finally:
        temp.__exit__()


def test_missing_path(suite):
    temp = TempProject()
    try:
        root = _project(temp)
        proc = run_agent([str(root / "nope.py")])
        suite.check("missing path exits 2", proc.returncode == 2)
        suite.check("missing path is reported, not invented",
                    "Not found" in proc.stdout)
    finally:
        temp.__exit__()


def test_markdown_output(suite):
    temp = TempProject()
    try:
        root = _project(temp)
        report = root / "report.md"
        proc = run_agent([str(root / "has_issues.py"), "--output", str(report)])
        suite.check("--output still exits 1 with findings", proc.returncode == 1)
        suite.check("report file written", report.exists())
        compare_golden(suite, "cli_markdown.md",
                       normalise(report.read_text(encoding="utf-8"), root))
    finally:
        temp.__exit__()


def test_unwritable_output(suite):
    temp = TempProject()
    try:
        root = _project(temp)
        proc = run_agent([str(root / "clean.py"), "--output", str(root / "no" / "dir" / "r.md")])
        suite.check("unwritable output exits 2", proc.returncode == 2)
        suite.check("results still printed despite the write failure",
                    "QA Agent report" in proc.stdout)
        suite.check("write failure explained on stderr", "could not write" in proc.stderr)
    finally:
        temp.__exit__()


def test_argument_errors(suite):
    suite.check("no arguments exits 2", run_agent([]).returncode == 2)
    suite.check("--git-diff plus paths is refused",
                run_agent(["--git-diff", "some/path", "extra"]).returncode == 2)
    suite.check("watch without a path exits 2", run_agent(["watch"]).returncode == 2)
    suite.check("watch on a missing directory exits 2",
                run_agent(["watch", "D:/definitely/not/here"]).returncode == 2)


if __name__ == "__main__":
    suite = Suite("Phase A CLI regression")
    sys.exit(suite.run([
        test_file_with_findings,
        test_clean_file,
        test_directory_with_unsupported_file,
        test_missing_path,
        test_markdown_output,
        test_unwritable_output,
        test_argument_errors,
    ]))
