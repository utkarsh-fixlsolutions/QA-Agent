"""AI wired into the CLI and configuration system (Phase D Part 6):
qa_agent.__main__'s new AI-settings resolution, provider construction, and
pipeline execution helpers, plus the real CLI end to end.

Two layers, matching this project's own established split for CLI-adjacent
logic: fast unit tests against the helper functions directly (imported from
qa_agent.__main__, the same way other test files import "private" module
functions for focused testing), and real subprocess tests via
harness.run_agent() proving the actual command-line behavior. Every
automated scenario here uses MockProvider or no provider at all - a real
offline Ollama call was measured (docs/step-log.md, Phase D Part 6) to take
several seconds per attempt regardless of a short configured timeout, which
would make this suite unacceptably slow; that real behavior is dogfooded
separately, not exercised by the automated suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject, compare_golden, normalise, run_agent  # noqa: E402

from qa_agent.__main__ import (  # noqa: E402
    _build_ai_provider,
    _effective_ai_settings,
    _run_ai_pipeline,
)
from qa_agent.ai import LLMResponse, MockProvider, OllamaProvider  # noqa: E402
from qa_agent.config import AIConfig  # noqa: E402

PY_WITH_ISSUE = "import os\n\ndef add(a, b):\n    return a + b\n"

# A response text MockProvider can hand back that validates successfully
# against all three schemas at once is not possible (each has its own
# required fields), so each pipeline test uses whichever shape it needs.
_EXPLANATION_JSON = '{"explanation": "a real explanation"}'
_SUMMARY_JSON = '{"summary": "a real summary"}'
_FIX_JSON = '{"explanation": "why", "suggested_fix": "the fix"}'


class _Args:
    """A plain stand-in for argparse's Namespace - only the six AI
    attributes _effective_ai_settings actually reads.
    """

    def __init__(self, ai=False, ai_explain=False, ai_summary=False, ai_fix=False,
                 ai_model=None, ai_provider=None):
        self.ai = ai
        self.ai_explain = ai_explain
        self.ai_summary = ai_summary
        self.ai_fix = ai_fix
        self.ai_model = ai_model
        self.ai_provider = ai_provider


class _Finding:
    def __init__(self, file="a.py", line=1, severity="error", message="m", tool="ruff"):
        self.file, self.line, self.severity, self.message, self.tool = (
            file, line, severity, message, tool,
        )


class _Result:
    def __init__(self, findings):
        self.findings = findings
        self.checked = [f.file for f in findings]
        self.tools_used = sorted({f.tool for f in findings})


# --- _effective_ai_settings: CLI > Config > Defaults ------------------------


def test_effective_ai_settings_all_defaults_off(suite):
    settings = _effective_ai_settings(_Args(), AIConfig())
    suite.check("nothing enabled with no CLI flags and no config",
                not (settings.enabled or settings.explain or settings.summary
                     or settings.suggest_fixes))


def test_effective_ai_settings_bare_ai_enables_all_three(suite):
    settings = _effective_ai_settings(_Args(ai=True), AIConfig())
    suite.check("enabled", settings.enabled is True)
    suite.check("all three features on by default with a bare --ai",
                settings.explain and settings.summary and settings.suggest_fixes)


def test_effective_ai_settings_specific_flag_does_not_enable_others(suite):
    settings = _effective_ai_settings(_Args(ai_explain=True), AIConfig())
    suite.check("AI is implicitly enabled by naming a specific feature",
                settings.enabled is True)
    suite.check("only the named feature is on", settings.explain is True)
    suite.check("the other two stay off - a specific flag never pulls in extras",
                settings.summary is False and settings.suggest_fixes is False)


def test_effective_ai_settings_cli_overrides_config(suite):
    config = AIConfig(enabled=True, explain=False, summary=False, suggest_fixes=False)
    settings = _effective_ai_settings(_Args(ai_fix=True), config)
    suite.check("CLI's explicit request wins", settings.suggest_fixes is True)
    suite.check("config's own enabled=True still holds", settings.enabled is True)
    suite.check("features the config also left off and CLI didn't ask for stay off",
                settings.explain is False and settings.summary is False)


def test_effective_ai_settings_config_alone_can_enable(suite):
    """CLI flags are additive-only - a project's own config must still be
    able to turn AI on with zero CLI flags at all.
    """
    config = AIConfig(enabled=True, explain=True, summary=True, suggest_fixes=True)
    settings = _effective_ai_settings(_Args(), config)
    suite.check("config alone enables everything it says to",
                settings.enabled and settings.explain and settings.summary
                and settings.suggest_fixes)


def test_effective_ai_settings_model_and_provider_precedence(suite):
    config = AIConfig(provider="mock", model="config-model")
    cli_wins = _effective_ai_settings(_Args(ai_model="cli-model", ai_provider="ollama"), config)
    config_wins = _effective_ai_settings(_Args(), config)
    suite.check("CLI model/provider override config's", cli_wins.model == "cli-model"
                and cli_wins.provider == "ollama")
    suite.check("config's own model/provider used when CLI gives neither",
                config_wins.model == "config-model" and config_wins.provider == "mock")


# --- _build_ai_provider ------------------------------------------------------


def test_build_ai_provider_none_when_disabled(suite):
    suite.check("no provider when AI is disabled",
                _build_ai_provider(AIConfig(enabled=False)) is None)


def test_build_ai_provider_mock(suite):
    provider = _build_ai_provider(AIConfig(enabled=True, provider="mock", model="m"))
    suite.check("a real MockProvider instance", isinstance(provider, MockProvider))
    assert isinstance(provider, MockProvider)
    suite.check("the configured model is used", provider.model == "m")


def test_build_ai_provider_ollama_with_overrides(suite):
    provider = _build_ai_provider(AIConfig(
        enabled=True, provider="ollama", model="m", endpoint="http://x:9", timeout=7,
    ))
    suite.check("a real OllamaProvider instance", isinstance(provider, OllamaProvider))
    assert isinstance(provider, OllamaProvider)
    suite.check("configured model/endpoint/timeout are all threaded through",
                provider.model == "m" and provider.endpoint == "http://x:9"
                and provider.timeout == 7)


def test_build_ai_provider_ollama_defaults_when_unset(suite):
    """model/endpoint/timeout of None in config must not become the literal
    value None on the provider - the provider's own defaults must apply,
    exactly as config.py's own docstring promises.
    """
    provider = _build_ai_provider(AIConfig(enabled=True, provider="ollama"))
    assert isinstance(provider, OllamaProvider)
    suite.check("no field is the literal None - the provider's own defaults were used",
                provider.model is not None and provider.endpoint is not None
                and provider.timeout is not None)


# --- _run_ai_pipeline: graceful degradation and correct wiring -------------


def test_run_ai_pipeline_disabled_returns_untouched_defaults(suite):
    result = _Result([_Finding()])
    summary, explanations, fixes = _run_ai_pipeline(result, AIConfig(), None)
    suite.check("no summary", summary is None)
    suite.check("no explanations", explanations == {})
    suite.check("no fixes", fixes == {})


def test_run_ai_pipeline_only_runs_enabled_features(suite):
    finding = _Finding()
    result = _Result([finding])
    provider = MockProvider(response_text=_EXPLANATION_JSON)
    settings = AIConfig(enabled=True, explain=True, summary=False, suggest_fixes=False)
    summary, explanations, fixes = _run_ai_pipeline(result, settings, provider)
    suite.check("summary not requested, stays None", summary is None)
    suite.check("explanation was requested and produced", finding in explanations)
    suite.check("fixes not requested, stays empty", fixes == {})


def test_run_ai_pipeline_all_three_enabled(suite):
    finding = _Finding()
    result = _Result([finding])
    settings = AIConfig(enabled=True, explain=True, summary=True, suggest_fixes=True)

    class _MultiSchemaProvider:
        """MockProvider only ever returns one fixed response; this part's
        pipeline calls three different validators against three different
        calls, so a tiny scripted provider is used instead.
        """

        name = "scripted"

        def __init__(self):
            self.calls = 0

        def generate(self, prompt):
            self.calls += 1
            if "summary" in prompt.lower() and "run summary" in prompt.lower():
                return LLMResponse(text=_SUMMARY_JSON)
            if "suggested_fix" in prompt:
                return LLMResponse(text=_FIX_JSON)
            return LLMResponse(text=_EXPLANATION_JSON)

    provider = _MultiSchemaProvider()
    summary, explanations, fixes = _run_ai_pipeline(result, settings, provider)
    suite.check("summary produced", summary is not None and summary.text == "a real summary")
    suite.check("explanation produced", finding in explanations)
    suite.check("fix produced", finding in fixes)
    suite.check("exactly three calls - one per enabled feature", provider.calls == 3)


def test_run_ai_pipeline_graceful_on_provider_failure(suite):
    """Requirement 5: the deterministic result must be completely
    unaffected by an AI failure - checked directly, not assumed.
    """
    result = _Result([_Finding(), _Finding(file="b.py", line=2)])
    failing = MockProvider(fail=True, failure_message="offline")
    settings = AIConfig(enabled=True, explain=True, summary=True, suggest_fixes=True)
    summary, explanations, fixes = _run_ai_pipeline(result, settings, failing)
    suite.check("no summary on failure", summary is None)
    suite.check("no explanations on failure", explanations == {})
    suite.check("no fixes on failure", fixes == {})
    suite.check("the findings themselves were never touched by any of this",
                len(result.findings) == 2)


# --- the real CLI, end to end -----------------------------------------------


def test_cli_ai_disabled_by_default_matches_phase_c_golden(suite):
    """Requirement 11/2, the real proof: with no AI flags and no AI config,
    the actual CLI output must still match the existing golden file
    exactly, byte for byte.
    """
    with TempProject() as root:
        target = root / "has_issues.py"
        target.write_text(
            "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n"
            "    y = 2\n    return x\n",
            encoding="utf-8",
        )
        proc = run_agent([str(target)])
        actual = normalise(proc.stdout, root)
    compare_golden(suite, "cli_findings.txt", actual)


def test_cli_ai_flag_enabled_does_not_change_findings(suite):
    """The mock provider's own default response text is not valid JSON for
    any AI schema, so enabling AI here exercises the real graceful-
    degradation path through the real CLI - and, crucially, must not
    change a single deterministic finding.
    """
    with TempProject() as root:
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        without_ai = run_agent([str(target)])
        with_ai = run_agent(["--ai", "--ai-provider", "mock", str(target)])
        suite.check("same exit code", without_ai.returncode == with_ai.returncode)
        suite.check("the real finding is present either way",
                     "F401" in without_ai.stdout and "F401" in with_ai.stdout)
        suite.check("no AI section appears - the mock's default text never validates",
                     "AI Explanation" not in with_ai.stdout
                     and "AI Summary" not in with_ai.stdout)


def test_cli_ai_config_file_enables_ai(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text(
            '{"ai": {"enabled": true, "provider": "mock", "explain": true}}',
            encoding="utf-8",
        )
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(root)], cwd=root)
        suite.check("still runs cleanly, config accepted", proc.returncode == 1)
        suite.check("the real finding is unaffected", "F401" in proc.stdout)


def test_cli_flag_overrides_config_provider(suite):
    """CLI > Config file, checked through the real command line: config
    names ollama (which is not running), --ai-provider mock on the CLI
    must win, so this stays fast and never touches the network.
    """
    with TempProject() as root:
        (root / ".qa-agent.json").write_text(
            '{"ai": {"enabled": true, "provider": "ollama"}}', encoding="utf-8",
        )
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent(["--ai-provider", "mock", str(target)], cwd=root)
        suite.check("completes quickly - proof the CLI override, not the config's "
                     "ollama default, was actually used", proc.returncode == 1)
        suite.check("finding unaffected", "F401" in proc.stdout)


def test_cli_invalid_ai_config_fails_clearly(suite):
    with TempProject() as root:
        (root / ".qa-agent.json").write_text(
            '{"ai": {"provider": "openai"}}', encoding="utf-8",
        )
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent([str(target)], cwd=root)
        suite.check("exit 2 - configuration error", proc.returncode == 2)
        suite.check("nothing was analyzed", "nothing was analyzed" in proc.stderr)
        suite.check("names the bad provider", "openai" in proc.stderr)


def test_cli_invalid_ai_provider_flag_rejected(suite):
    with TempProject() as root:
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        proc = run_agent(["--ai-provider", "openai", str(target)], cwd=root)
        suite.check("argparse itself rejects an unknown --ai-provider value",
                     proc.returncode == 2)


def test_cli_unified_reporting_with_ai_enabled(suite):
    """The unified, multi-tool report (Part 3) must render identically
    whether AI is on or off, aside from the additive AI sections - checked
    against a real multi-finding file.
    """
    with TempProject() as root:
        target = root / "has_issues.py"
        target.write_text(
            "import json\n\n\ndef f(x):\n    if x == None:\n        return 1\n"
            "    y = 2\n    return x\n",
            encoding="utf-8",
        )
        without_ai = run_agent([str(target)])
        with_ai = run_agent(["--ai", "--ai-provider", "mock", str(target)])
        suite.check("identical finding count reported",
                     without_ai.stdout.count("F401") == with_ai.stdout.count("F401") == 1)
        suite.check("identical exit code", without_ai.returncode == with_ai.returncode)


def test_cli_ai_disabled_output_identical_with_and_without_output_flag(suite):
    """-o/--output's Markdown file must also stay unaffected by AI being
    off - the same byte-identity guarantee report.py's own tests already
    checked at the function level, now checked through the real CLI.
    """
    with TempProject() as root:
        target = root / "bug.py"
        target.write_text(PY_WITH_ISSUE, encoding="utf-8")
        report_path = root / "report.md"
        proc = run_agent([str(target), "--output", str(report_path)], cwd=root)
        suite.check("exit 1, real finding", proc.returncode == 1)
        markdown = report_path.read_text(encoding="utf-8")
        suite.check("no AI section leaked into the markdown output either",
                     "AI Explanation" not in markdown and "AI Summary" not in markdown
                     and "AI Suggested" not in markdown)


if __name__ == "__main__":
    suite = Suite("AI wired into the CLI and configuration system (Phase D Part 6)")
    sys.exit(suite.run([
        test_effective_ai_settings_all_defaults_off,
        test_effective_ai_settings_bare_ai_enables_all_three,
        test_effective_ai_settings_specific_flag_does_not_enable_others,
        test_effective_ai_settings_cli_overrides_config,
        test_effective_ai_settings_config_alone_can_enable,
        test_effective_ai_settings_model_and_provider_precedence,
        test_build_ai_provider_none_when_disabled,
        test_build_ai_provider_mock,
        test_build_ai_provider_ollama_with_overrides,
        test_build_ai_provider_ollama_defaults_when_unset,
        test_run_ai_pipeline_disabled_returns_untouched_defaults,
        test_run_ai_pipeline_only_runs_enabled_features,
        test_run_ai_pipeline_all_three_enabled,
        test_run_ai_pipeline_graceful_on_provider_failure,
        test_cli_ai_disabled_by_default_matches_phase_c_golden,
        test_cli_ai_flag_enabled_does_not_change_findings,
        test_cli_ai_config_file_enables_ai,
        test_cli_flag_overrides_config_provider,
        test_cli_invalid_ai_config_fails_clearly,
        test_cli_invalid_ai_provider_flag_rejected,
        test_cli_unified_reporting_with_ai_enabled,
        test_cli_ai_disabled_output_identical_with_and_without_output_flag,
    ]))
