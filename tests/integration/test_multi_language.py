"""Phase C Part 2 integration: ESLint alongside ruff, through the real CLI and
watch mode (docs/14-first-multi-language-analyzer.md).

Uses a real ESLint install (tests/fixtures/eslint, `npm install`ed once - see
its package.json). Tests that need it check first and skip cleanly and
visibly, never silently, if it hasn't been done.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import (  # noqa: E402
    REPO_ROOT, Suite, TempProject, env_with_eslint, eslint_available, run_agent,
)

ESLINT_CONFIG = (
    'module.exports = [{ rules: { "no-unused-vars": "error", "eqeqeq": "warn" } }];\n'
)
JS_WITH_ISSUE = "var unusedThing = 42;\nfunction greet(name) {\n  console.log(name);\n}\n"
JS_CLEAN = "function greet(name) {\n  console.log(name);\n}\nmodule.exports = greet;\n"
JS_SYNTAX_ERROR = "function broken( {\n  return 1\n"
PY_WITH_ISSUE = "import os\n\ndef add(a, b):\n    return a + b\n"


def _mixed_project(temp):
    temp.write("eslint.config.js", ESLINT_CONFIG)
    temp.write("app.js", JS_WITH_ISSUE)
    temp.write("util.py", PY_WITH_ISSUE)
    return temp.path


def _skip(suite, name):
    return suite.check(
        "(skipped - {} needs `npm install` in tests/fixtures/eslint)".format(name), True)


def test_mixed_project_both_tools_run(suite):
    if not eslint_available():
        return _skip(suite, "test_mixed_project_both_tools_run")
    temp = TempProject()
    try:
        root = _mixed_project(temp)
        proc = run_agent([str(root)], env=env_with_eslint())
        suite.check("exit 1, findings from both tools", proc.returncode == 1)
        # pyright and mypy also claim .py (Phase C Parts 5 and 7) and are on
        # PATH via this interpreter's own Scripts dir (harness.run_agent
        # always adds it) - both find nothing on this fixture (an unused
        # import and an untyped function are neither one a type error), so
        # both are listed as attempted but add no finding line.
        suite.check("all attempted tools listed",
                     "Tools:   eslint, mypy, pyright, ruff" in proc.stdout)
        suite.check("real eslint finding present", "no-unused-vars" in proc.stdout)
        suite.check("real ruff finding present", "F401" in proc.stdout)
    finally:
        temp.__exit__()


def test_cwd_anchoring_works_from_an_unrelated_directory(suite):
    """The exact scenario docs/14 identified: qa_agent launched from somewhere
    other than the analyzed project - watch mode's normal case. REPO_ROOT (this
    project) has no eslint.config.js anywhere in its own tree, so if cwd
    anchoring regressed, this would fail with eslint's "couldn't find" error.
    """
    if not eslint_available():
        return _skip(suite, "test_cwd_anchoring_works_from_an_unrelated_directory")
    temp = TempProject()
    try:
        root = _mixed_project(temp)
        proc = run_agent([str(root)], cwd=REPO_ROOT, env=env_with_eslint())
        suite.check("eslint's config was still found via upward search",
                     "couldn't find an eslint.config" not in proc.stdout)
        suite.check("real finding present despite the unrelated cwd",
                     "no-unused-vars" in proc.stdout)
    finally:
        temp.__exit__()


def test_fatal_syntax_error_is_a_finding(suite):
    if not eslint_available():
        return _skip(suite, "test_fatal_syntax_error_is_a_finding")
    temp = TempProject()
    try:
        target = temp.write("broken.js", JS_SYNTAX_ERROR)
        temp.write("eslint.config.js", ESLINT_CONFIG)
        proc = run_agent([str(target)], env=env_with_eslint())
        suite.check("a syntax error is reported as a finding, not a crash",
                     "Parsing error" in proc.stdout)
        suite.check("exit 1, like any other real finding", proc.returncode == 1)
    finally:
        temp.__exit__()


def test_missing_config_is_a_clear_tool_error(suite):
    """A tool error is now part of the one unified report on stdout
    (docs/15-unified-reporting.md), not a separate stderr-only crash message -
    that's the render_tool_error()/stderr path, reserved for a failure before
    any run() even starts (e.g. --git-diff outside a git repo).
    """
    if not eslint_available():
        return _skip(suite, "test_missing_config_is_a_clear_tool_error")
    temp = TempProject()
    try:
        target = temp.write("a.js", JS_CLEAN)  # deliberately no eslint.config.js
        proc = run_agent([str(target)], env=env_with_eslint())
        suite.check("exit 2, a genuine tool failure", proc.returncode == 2)
        suite.check("shown in the unified report, not a bare crash",
                     "Analyzer errors" in proc.stdout)
        suite.check("eslint's own explanation is surfaced verbatim",
                     "couldn't find an eslint.config" in proc.stdout)
    finally:
        temp.__exit__()


def test_eslint_genuinely_not_installed(suite):
    """A deliberately eslint-free PATH, not ambient absence - so this stays
    deterministic even on a machine that does have eslint installed globally.
    A .js-only target, so ruff's own presence/absence never matters here.
    """
    temp = TempProject()
    try:
        target = temp.write("a.js", JS_CLEAN)
        temp.write("eslint.config.js", ESLINT_CONFIG)
        env = os.environ.copy()
        env["PATH"] = ""
        proc = run_agent([str(target)], env=env)
        suite.check("exit 2, eslint missing", proc.returncode == 2)
        suite.check("clear message, not a traceback",
                     "'eslint' is not installed or not on PATH" in proc.stdout)
    finally:
        temp.__exit__()


def test_failing_analyzer_never_hides_another_tools_findings(suite):
    """Part 3 (docs/15-unified-reporting.md): what Part 2's docs/14 section 7
    deferred, now implemented. Was a KNOWN LIMITATION test asserting the
    opposite (ruff's finding did NOT survive eslint's failure) - flipped here
    now that result isolation is real, per that test's own instruction to
    update it and docs/14 section 7 together rather than treat this as a
    regression.
    """
    if not eslint_available():
        return _skip(suite, "test_failing_analyzer_never_hides_another_tools_findings")
    temp = TempProject()
    try:
        temp.write("a.js", JS_CLEAN)  # no config -> eslint fails
        temp.write("util.py", PY_WITH_ISSUE)  # a real, genuine ruff finding
        proc = run_agent([str(temp.path)], env=env_with_eslint())
        suite.check("exit 2 - a tool still failed, and that still wins the exit code",
                     proc.returncode == 2)
        suite.check("ruff's real finding survives eslint's failure",
                     "F401" in proc.stdout)
        suite.check("eslint's failure is reported too, not swallowed",
                     "Analyzer errors" in proc.stdout and "eslint" in proc.stdout)
        suite.check("both tools are still listed as attempted", "Tools:" in proc.stdout)
    finally:
        temp.__exit__()


def _watch(root, actions, env=None, settle=1.6):
    log = root / "_watch.log"
    handle = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "qa_agent", "watch", str(root)],
        cwd=str(REPO_ROOT), stdout=handle, stderr=subprocess.STDOUT, env=env,
    )
    try:
        time.sleep(2.5)  # let the observer come up
        for action in actions:
            action()
            time.sleep(settle)
        alive = proc.poll() is None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        handle.close()
    return log.read_text(encoding="utf-8"), alive


def test_watch_mode_banner_lists_both_analyzers(suite):
    """The literal proof of Part 1's stated success criterion: registering a
    second adapter changed this banner with zero __main__.py code changes -
    and by Part 5, a third and fourth (pyright, shellcheck), and by Part 7 a
    fifth (mypy), did the same. The banner is built purely from the ADAPTERS
    registry, not from checking whether each tool's binary is actually on
    PATH, so all five always appear here regardless of which fixtures are
    installed.
    """
    if not eslint_available():
        return _skip(suite, "test_watch_mode_banner_lists_both_analyzers")
    with TempProject() as root:
        output, alive = _watch(root, [], env=env_with_eslint(), settle=0)
    suite.check(
        "banner lists all five analyzers",
        "Analyzers: eslint (.js, .jsx, .ts, .tsx), mypy (.py), pyright (.py), "
        "ruff (.py), shellcheck (.sh)"
        in output,
    )
    suite.check("watcher was alive", alive)


def test_watch_mode_live_js_save_produces_a_real_finding(suite):
    if not eslint_available():
        return _skip(suite, "test_watch_mode_live_js_save_produces_a_real_finding")
    with TempProject() as root:
        (root / "eslint.config.js").write_text(ESLINT_CONFIG, encoding="utf-8")

        def save_js():
            (root / "app.js").write_text(JS_WITH_ISSUE, encoding="utf-8")

        output, alive = _watch(root, [save_js], env=env_with_eslint())
    suite.check("the watcher stayed alive through a real .js save", alive)
    suite.check("a batch was produced for the save", "Batch #" in output)
    suite.check("the real eslint finding appears in the live stream",
                 "no-unused-vars" in output)
    suite.check("attributed to eslint, not invented", "(eslint)" in output)


if __name__ == "__main__":
    suite = Suite("Multi-language integration: ESLint + ruff, real installs")
    sys.exit(suite.run([
        test_mixed_project_both_tools_run,
        test_cwd_anchoring_works_from_an_unrelated_directory,
        test_fatal_syntax_error_is_a_finding,
        test_missing_config_is_a_clear_tool_error,
        test_eslint_genuinely_not_installed,
        test_failing_analyzer_never_hides_another_tools_findings,
        test_watch_mode_banner_lists_both_analyzers,
        test_watch_mode_live_js_save_produces_a_real_finding,
    ]))
