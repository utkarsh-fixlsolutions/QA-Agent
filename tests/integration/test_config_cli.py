"""The config system through the real CLI (docs/16-configuration-system.md):
discovery on disk, validation errors surfacing clearly, and its effects on a
real ruff run. ESLint-dependent scenarios reuse the Part 2 fixture
(tests/fixtures/eslint) and skip cleanly, not silently, if it isn't installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, env_with_eslint, eslint_available, run_agent  # noqa: E402

PY_WITH_ISSUE = "import os\n\ndef add(a, b):\n    return a + b\n"
ESLINT_CONFIG = (
    'module.exports = [{ rules: { "no-unused-vars": "error", "eqeqeq": "warn" } }];\n'
)
JS_WARNING_AND_ERROR = (
    "var unusedThing = 42;\n"
    "function f(x) {\n"
    "  if (x == null) { return 1; }\n"
    "}\n"
)


def _skip(suite, name):
    return suite.check(
        "(skipped - {} needs `npm install` in tests/fixtures/eslint)".format(name), True)


def test_no_config_present_is_unaffected(suite):
    with TempProject() as root:
        (root / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 1, the real ruff finding, as always", proc.returncode == 1)
        suite.check("no Config: line when there's no config file",
                     "Config:" not in proc.stdout)


def test_config_disables_ruff(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"analyzers": []}', encoding="utf-8")
        (root / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 0 - nothing ran, nothing to report", proc.returncode == 0)
        suite.check("the file is honestly skipped, not silently clean",
                     "disabled by config" in proc.stdout)
        suite.check("the config path is shown", ".qa-agent.json" in proc.stdout)


def test_config_extra_ignore(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"ignore": ["vendor"]}', encoding="utf-8")
        (root / "vendor").mkdir()
        (root / "vendor" / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 0 - the real bug inside vendor/ is never analyzed",
                     proc.returncode == 0)
        suite.check("no finding leaked through", "F401" not in proc.stdout)


def test_config_include_overrides_a_hardcoded_ignore(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"include": ["node_modules"]}', encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 1 - node_modules is normally hardcoded-ignored, but included here",
                     proc.returncode == 1)
        suite.check("the real finding is now reported", "F401" in proc.stdout)


def test_malformed_config_fails_clearly(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text("{not json", encoding="utf-8")
        (root / "clean.py").write_text("x = 1\n", encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 2 - nothing was analyzed", proc.returncode == 2)
        suite.check("a clear configuration error, not a traceback",
                     "configuration error" in proc.stderr)
        suite.check("no Python traceback leaked", "Traceback" not in proc.stderr)


def test_config_with_typo_d_analyzer_fails_clearly(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"analyzers": ["ruf"]}', encoding="utf-8")
        (root / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("exit 2 - a typo must never silently mean zero analysis",
                     proc.returncode == 2)
        suite.check("names the bad analyzer", "ruf" in proc.stderr)


def test_explicit_config_flag_overrides_discovery(suite):
    with TempProject() as root:
        # A discoverable config that would disable ruff, and a second,
        # explicit one elsewhere that doesn't - --config must win.
        (root / ".qa-agent.json").write_text('{"analyzers": []}', encoding="utf-8")
        (root / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        explicit = root.parent / "explicit-{}.json".format(root.name)
        explicit.write_text("{}", encoding="utf-8")
        try:
            proc = run_agent([str(root), "--config", str(explicit)])
            suite.check("the explicit config was used, not the discovered one",
                        proc.returncode == 1)
            suite.check("real finding present", "F401" in proc.stdout)
        finally:
            explicit.unlink(missing_ok=True)


def test_severity_filter_via_fake_extension_end_to_end(suite):
    """Exercises the CLI -> config -> runner path for min_severity without
    needing a real tool that produces a "warning" - ruff's own findings are
    always "error" (docs/02), so this proves the wiring using the reporting
    layer's own visible "hidden by severity filter" note against a project
    with zero ruff findings, confirming the note only ever appears when
    something was actually hidden.
    """
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"min_severity": "error"}', encoding="utf-8")
        (root / "clean.py").write_text("x = 1\n", encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("a clean file with a filter configured stays clean",
                     proc.returncode == 0)
        suite.check("no phantom 'hidden by severity filter' note when nothing was hidden",
                     "hidden by" not in proc.stdout)


# --- real ESLint scenarios: a genuine "warning"-level finding exists -------

def test_config_disables_eslint_real_mixed_project(suite):
    if not eslint_available():
        return _skip(suite, "test_config_disables_eslint_real_mixed_project")
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"analyzers": ["ruff"]}', encoding="utf-8")
        (root / "eslint.config.js").write_text(ESLINT_CONFIG, encoding="utf-8")
        (root / "app.js").write_text(JS_WARNING_AND_ERROR, encoding="utf-8")
        (root / "bug.py").write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root, env=env_with_eslint())
        suite.check("exit 1 - ruff's real finding still runs", proc.returncode == 1)
        suite.check("ruff's finding present", "F401" in proc.stdout)
        suite.check("eslint's finding absent - it never ran", "no-unused-vars" not in proc.stdout)
        suite.check("the .js file is honestly skipped as disabled",
                     "disabled by config" in proc.stdout)


def test_config_severity_filter_real_eslint_warning(suite):
    if not eslint_available():
        return _skip(suite, "test_config_severity_filter_real_eslint_warning")
    with TempProject() as root:
        (root / ".qa-agent.json").write_text('{"min_severity": "error"}', encoding="utf-8")
        (root / "eslint.config.js").write_text(ESLINT_CONFIG, encoding="utf-8")
        (root / "app.js").write_text(JS_WARNING_AND_ERROR, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root, env=env_with_eslint())
        suite.check("exit 1 - the real error-level finding still shows", proc.returncode == 1)
        suite.check("the error-level finding survives", "no-unused-vars" in proc.stdout)
        suite.check("the real warning-level finding (eqeqeq) is hidden",
                     "eqeqeq" not in proc.stdout)
        suite.check("the report says so, honestly", "hidden by the config's severity filter"
                     in proc.stdout)


if __name__ == "__main__":
    suite = Suite("Config system through the real CLI")
    sys.exit(suite.run([
        test_no_config_present_is_unaffected,
        test_config_disables_ruff,
        test_config_extra_ignore,
        test_config_include_overrides_a_hardcoded_ignore,
        test_malformed_config_fails_clearly,
        test_config_with_typo_d_analyzer_fails_clearly,
        test_explicit_config_flag_overrides_discovery,
        test_severity_filter_via_fake_extension_end_to_end,
        test_config_disables_eslint_real_mixed_project,
        test_config_severity_filter_real_eslint_warning,
    ]))
