"""Phase C Part 5: Pyright and ShellCheck through the real CLI, config, and
unified reporting - proving Parts 1-4's architecture scales to a third and
fourth real tool without any engine change.

Pyright is pip-installed into this project's own venv (requirements.txt) and
resolvable via harness.run_agent()'s own PATH fix - no extra setup needed.
ShellCheck is fetched separately (tests/fixtures/shellcheck/fetch.ps1);
tests needing it check first and skip cleanly, never silently, if it hasn't
been done.

Every fixture is written with newline="" - found the hard way while writing
this file: Windows' default text-mode \n -> \r\n translation left every .sh
fixture with a stray carriage return, which ShellCheck correctly (if
surprisingly) flags as SC1017. Real, useful signal for a real file; noise
for a fixture meant to be clean.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import (  # noqa: E402
    Suite, TempProject, env_with_tools, eslint_available, run_agent, shellcheck_available,
)

ESLINT_CONFIG = 'module.exports = [{ rules: { "no-unused-vars": "error" } }];\n'
PY_TYPE_ERROR = 'def add(a: int, b: int) -> int:\n    return a + "not a number"\n'
PY_RUFF_ISSUE_ONLY = "import os\n\ndef f(x):\n    return x\n"
SH_WITH_ISSUE = "#!/bin/bash\nVAR=$1\necho $VAR\n"
SH_CLEAN = '#!/bin/bash\necho "hello"\n'
JS_WITH_ISSUE = "var unusedThing = 42;\nfunction greet(name) {\n  console.log(name);\n}\n"


def _write(root, relative, text):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="")
    return target


def _skip(suite, name, why):
    return suite.check("(skipped - {} {})".format(name, why), True)


def test_pyright_real_type_error(suite):
    with TempProject() as root:
        target = _write(root, "bad.py", PY_TYPE_ERROR)
        proc = run_agent([str(target)])
        suite.check("exit 1, a genuine real type error", proc.returncode == 1)
        suite.check("pyright's own finding text is present", "reportOperatorIssue" in proc.stdout)
        suite.check("attributed to pyright, not invented", "(pyright)" in proc.stdout)


def test_pyright_and_ruff_both_run_on_the_same_file(suite):
    """The real, not-fake-adapter proof of Part 1's dispatch generality:
    one .py file, two adapters, both attempted, both merged into one result.
    By Part 7, mypy claims the same file too (a third), for free - the
    exact scaling this test was written to prove is even possible.
    """
    with TempProject() as root:
        # A ruff-flagged style issue that is not a type error, and separately
        # verified (docs/step-log.md, Phase C Parts 5 and 7) that pyright and
        # mypy both stay silent on it - so any finding from either here would
        # be real and new, not noise from the same issue ruff already covers.
        target = _write(root, "mixed.py", PY_RUFF_ISSUE_ONLY)
        proc = run_agent([str(target)])
        suite.check("all three .py tools attempted",
                     "Tools:   mypy, pyright, ruff" in proc.stdout)
        suite.check("ruff's real finding present", "F401" in proc.stdout)
        suite.check("pyright found nothing new on this file (verified separately)",
                     "reportOperatorIssue" not in proc.stdout and "reportMissingImports"
                     not in proc.stdout)
        suite.check("mypy found nothing new on this file either (verified separately)",
                     "(mypy)" not in proc.stdout)


def test_config_disables_pyright_keeps_ruff(suite):
    with TempProject() as root:
        _write(root, ".qa-agent.json", '{"analyzers": ["ruff"]}')
        _write(root, "bad.py", PY_TYPE_ERROR)
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 0 - ruff has nothing to say about this file's only issue "
                     "(a type error, ruff's domain is lint/style)", proc.returncode == 0)
        suite.check("pyright's finding is absent - it never ran",
                     "reportOperatorIssue" not in proc.stdout)


def test_shellcheck_real_finding(suite):
    if not shellcheck_available():
        return _skip(suite, "test_shellcheck_real_finding", "needs tests/fixtures/shellcheck/fetch.ps1")
    with TempProject() as root:
        target = _write(root, "bad.sh", SH_WITH_ISSUE)
        proc = run_agent([str(target)], env=env_with_tools(shellcheck=True))
        suite.check("exit 1, a genuine real shellcheck finding", proc.returncode == 1)
        suite.check("shellcheck's own SC-prefixed finding present", "SC2086" in proc.stdout)
        suite.check("attributed to shellcheck, not invented", "(shellcheck)" in proc.stdout)


def test_shellcheck_clean_file(suite):
    if not shellcheck_available():
        return _skip(suite, "test_shellcheck_clean_file", "needs tests/fixtures/shellcheck/fetch.ps1")
    with TempProject() as root:
        target = _write(root, "clean.sh", SH_CLEAN)
        proc = run_agent([str(target)], env=env_with_tools(shellcheck=True))
        suite.check("exit 0, genuinely clean", proc.returncode == 0,
                     "" if proc.returncode == 0 else "  [{}]".format(proc.stdout[-300:]))
        suite.check("no issues found", "no issues found" in proc.stdout)


def test_config_disables_shellcheck(suite):
    if not shellcheck_available():
        return _skip(suite, "test_config_disables_shellcheck", "needs tests/fixtures/shellcheck/fetch.ps1")
    with TempProject() as root:
        _write(root, ".qa-agent.json", '{"analyzers": ["ruff"]}')
        _write(root, "bad.sh", SH_WITH_ISSUE)
        proc = run_agent([str(root)], cwd=root, env=env_with_tools(shellcheck=True))
        suite.check("exit 0 - shellcheck never ran", proc.returncode == 0)
        suite.check("the .sh file is honestly skipped as disabled",
                     "disabled by config" in proc.stdout)
        suite.check("no shellcheck finding leaked through", "SC2086" not in proc.stdout)


def test_four_language_mixed_project(suite):
    """One project, four real tools, one unified, deterministically-ordered
    report - the concrete proof this phase exists to build.
    """
    if not eslint_available():
        return _skip(suite, "test_four_language_mixed_project", "needs eslint fixture")
    if not shellcheck_available():
        return _skip(suite, "test_four_language_mixed_project", "needs shellcheck fixture")
    with TempProject() as root:
        _write(root, "eslint.config.js", ESLINT_CONFIG)
        _write(root, "app.js", JS_WITH_ISSUE)
        _write(root, "mid_bad.py", PY_TYPE_ERROR)
        _write(root, "z_bad.sh", SH_WITH_ISSUE)
        proc = run_agent([str(root)], cwd=root, env=env_with_tools(eslint=True, shellcheck=True))
        suite.check("exit 1, real findings from multiple tools", proc.returncode == 1)
        # mypy also claims .py (Phase C Part 7) - free, unrequested proof
        # that a fifth tool joining an already-mixed project needed no
        # change here beyond this line's own expected string.
        suite.check("all five tools attempted",
                     "Tools:   eslint, mypy, pyright, ruff, shellcheck" in proc.stdout)
        suite.check("eslint's real finding present", "no-unused-vars" in proc.stdout)
        suite.check("pyright's real finding present", "reportOperatorIssue" in proc.stdout)
        suite.check("mypy's real finding present, a second independent type checker "
                     "agreeing with pyright", "(mypy)" in proc.stdout)
        suite.check("shellcheck's real finding present", "SC2086" in proc.stdout)
        # Deterministic order (Part 3): sorted by file path, so - with names
        # deliberately chosen to sort app.js < mid_bad.py < z_bad.sh - each
        # tool's finding line appears in that same order, regardless of
        # which tool produced it.
        findings_block = proc.stdout.split("Findings (")[1]
        app_pos = findings_block.find("app.js")
        py_pos = findings_block.find("mid_bad.py")
        sh_pos = findings_block.find("z_bad.sh")
        suite.check("findings ordered by file name, regardless of which tool produced them",
                     0 <= app_pos < py_pos < sh_pos,
                     "  [app={} py={} sh={}]".format(app_pos, py_pos, sh_pos))


# --- Phase C Part 7: Mypy, a second independent .py type checker -----------

PY_SYNTAX_ERROR = "def broken(:\n    pass\n"


def test_mypy_real_type_error(suite):
    with TempProject() as root:
        target = _write(root, "bad.py", PY_TYPE_ERROR)
        proc = run_agent([str(target)])
        suite.check("exit 1, a genuine real type error", proc.returncode == 1)
        suite.check("mypy's own finding text is present",
                     "Unsupported operand types for +" in proc.stdout)
        suite.check("attributed to mypy, not invented", "(mypy)" in proc.stdout)


def test_mypy_pyright_and_ruff_all_run_on_the_same_file(suite):
    """The real, not-fake-adapter proof this part exists to build: a THIRD
    adapter sharing .py, not just a second (Part 5's own proof).
    """
    with TempProject() as root:
        target = _write(root, "triple.py", PY_TYPE_ERROR)
        proc = run_agent([str(target)])
        suite.check("all three .py tools attempted",
                     "Tools:   mypy, pyright, ruff" in proc.stdout)
        suite.check("mypy's real finding present", "(mypy)" in proc.stdout)
        suite.check("pyright's real finding present, agreeing independently",
                     "reportOperatorIssue" in proc.stdout)
        # Verified separately (ruff --output-format=json on this exact
        # content returns []): this is a type error, not ruff's domain.
        suite.check("ruff attempted but has nothing to say (verified separately)",
                     "(ruff)" not in proc.stdout)


def test_config_enables_only_mypy(suite):
    with TempProject() as root:
        _write(root, ".qa-agent.json", '{"analyzers": ["mypy"]}')
        _write(root, "bad.py", PY_TYPE_ERROR)
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 1 - mypy alone still finds the real error", proc.returncode == 1)
        suite.check("only mypy attempted", "Tools:   mypy" in proc.stdout)
        suite.check("pyright never ran", "reportOperatorIssue" not in proc.stdout)


def test_mypy_paths_are_absolute_and_consistent_with_other_tools(suite):
    """The real, end-to-end proof of the --show-absolute-path fix
    (docs/step-log.md, Phase C Part 7): two files, both with a real mypy
    finding, must sort correctly interleaved by file (Part 3) with matching
    absolute paths - not clustered apart because mypy's own path happened
    to be relative while pyright's and ruff's were absolute.
    """
    with TempProject() as root:
        _write(root, "a_first.py", PY_TYPE_ERROR)
        _write(root, "z_second.py", PY_TYPE_ERROR)
        proc = run_agent([str(root)], cwd=root)
        suite.check("mypy's own finding uses the exact same absolute path pyright's does",
                     str((root / "a_first.py").resolve()) + ":2" in proc.stdout)
        findings_block = proc.stdout.split("Findings (")[1]
        first_pos = findings_block.find("a_first.py")
        second_pos = findings_block.find("z_second.py")
        suite.check("both files' findings are grouped together in file order - "
                     "not split apart by an inconsistent path format",
                     0 <= first_pos < second_pos)


def test_mypy_syntax_error_becomes_a_tool_error_ruff_still_reports_it(suite):
    """The real, non-obvious behaviour found while building this adapter
    (docs/step-log.md, Phase C Part 7): a syntax error makes mypy exit 2 -
    the same code it uses for a genuine crash - so it is conservatively
    treated as a tool error rather than guessed at. Ruff parses independently
    and catches the same syntax error on its own, so the merged report still
    carries real signal for this file - the concrete, organic proof that one
    tool's failure never hides another's real finding (docs/15), for a
    failure this project did not have to construct.
    """
    with TempProject() as root:
        target = _write(root, "broken.py", PY_SYNTAX_ERROR)
        proc = run_agent([str(target)])
        suite.check("exit 2 - mypy's own failure wins the exit code", proc.returncode == 2)
        suite.check("mypy's failure is reported, not silently swallowed",
                     "mypy: 'mypy' exited with code 2" in proc.stdout)
        suite.check("ruff's own real syntax-error finding still appears",
                     "(ruff)" in proc.stdout and "invalid-syntax" in proc.stdout)


def test_one_tool_missing_does_not_stop_the_others(suite):
    """Real tool isolation (Part 1/3), with a genuinely missing tool rather
    than a simulated one: shellcheck absent from PATH entirely, ruff and
    pyright still run and their real findings still appear.
    """
    with TempProject() as root:
        _write(root, "bad.py", PY_TYPE_ERROR)
        _write(root, "bad.sh", SH_WITH_ISSUE)
        # Deliberately no env_with_tools(shellcheck=True) - shellcheck is not
        # on PATH here, ruff/pyright are (via run_agent()'s own PATH fix).
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 2 - a tool genuinely failed", proc.returncode == 2)
        suite.check("pyright's real finding survives shellcheck's failure",
                     "reportOperatorIssue" in proc.stdout)
        suite.check("shellcheck's failure is reported, not swallowed",
                     "shellcheck: 'shellcheck' is not installed or not on PATH" in proc.stdout)


if __name__ == "__main__":
    suite = Suite("Additional analyzers: Pyright + ShellCheck (Phase C Part 5)")
    sys.exit(suite.run([
        test_pyright_real_type_error,
        test_pyright_and_ruff_both_run_on_the_same_file,
        test_config_disables_pyright_keeps_ruff,
        test_shellcheck_real_finding,
        test_shellcheck_clean_file,
        test_config_disables_shellcheck,
        test_four_language_mixed_project,
        test_mypy_real_type_error,
        test_mypy_pyright_and_ruff_all_run_on_the_same_file,
        test_config_enables_only_mypy,
        test_mypy_paths_are_absolute_and_consistent_with_other_tools,
        test_mypy_syntax_error_becomes_a_tool_error_ruff_still_reports_it,
        test_one_tool_missing_does_not_stop_the_others,
    ]))
